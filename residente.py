"""residente.py: Baton em segundo plano.

Preferencias (ativo/pausado, modo, apps lembrados), deteccao de apresentacao em tela cheia
(macOS, Windows, Linux X11), aviso do sistema e a pagina de painel (so no proprio computador).
Somente biblioteca padrao.
"""
import json
import os
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

PREFS_ARQ = Path.home() / ".cache" / "baton" / "estado.json"
PADRAO = {"ativo": True, "modo": "perguntar", "apps_ok": [], "apps_nao": []}
MODOS = ("perguntar", "sempre", "nunca")
# apps de video e arquivos: tela cheia que quase nunca e uma apresentacao
IGNORADOS = ("vlc", "iina", "quicktime", "mpv", "finder", "explorer.exe", "dwm.exe", "steam", "netflix", "spotify")


def prefs_carregar():
    p = dict(PADRAO, apps_ok=[], apps_nao=[])
    try:
        d = json.loads(PREFS_ARQ.read_text(encoding="utf-8"))
        if isinstance(d.get("ativo"), bool):
            p["ativo"] = d["ativo"]
        if d.get("modo") in MODOS:
            p["modo"] = d["modo"]
        for k in ("apps_ok", "apps_nao"):
            if isinstance(d.get(k), list):
                p[k] = [str(x)[:80] for x in d[k]][:200]
    except (OSError, ValueError):
        pass
    return p


def prefs_salvar(p):
    try:
        PREFS_ARQ.parent.mkdir(parents=True, exist_ok=True)
        PREFS_ARQ.write_text(json.dumps(p, ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass


def ignorado(app):
    a = (app or "").lower()
    return any(i in a for i in IGNORADOS)


# ------------------------------------------------------------------ deteccao de tela cheia
def parse_osascript(saida, tela):
    """'App|x|y|largura|altura' (System Events) -> nome do app se a janela cobre a tela inteira."""
    partes = saida.strip().split("|")
    if len(partes) != 5:
        return None
    try:
        w, h = float(partes[3]), float(partes[4])
    except ValueError:
        return None
    return partes[0] if w >= tela[0] - 2 and h >= tela[1] - 2 else None


def parse_xprop(saida):
    """Saida do xprop (_NET_WM_STATE, WM_CLASS) -> classe da janela se ela esta em tela cheia."""
    cheia, classe = False, None
    for linha in saida.splitlines():
        if linha.startswith("_NET_WM_STATE") and "_NET_WM_STATE_FULLSCREEN" in linha:
            cheia = True
        elif linha.startswith("WM_CLASS") and "=" in linha:
            valores = [v.strip().strip('"') for v in linha.split("=", 1)[1].split(",")]
            classe = valores[-1] if valores and valores[-1] else None
    return (classe or "janela") if cheia else None


_APPLESCRIPT = '''tell application "System Events"
  set p to first application process whose frontmost is true
  set n to name of p
  try
    set w to front window of p
    set {x, y} to position of w
    set {ww, hh} to size of w
    return n & "|" & x & "|" & y & "|" & ww & "|" & hh
  on error
    return n & "|-"
  end try
end tell'''


def detectar_mac(tela):
    r = subprocess.run(["osascript", "-e", _APPLESCRIPT], capture_output=True, text=True, timeout=5)
    return parse_osascript(r.stdout, tela) if r.returncode == 0 else None


def detectar_windows():
    import ctypes
    from ctypes import wintypes as wt
    u, k = ctypes.windll.user32, ctypes.windll.kernel32
    u.GetForegroundWindow.restype = wt.HWND
    u.MonitorFromWindow.argtypes = [wt.HWND, wt.DWORD]
    u.MonitorFromWindow.restype = wt.HANDLE
    k.OpenProcess.restype = wt.HANDLE
    hwnd = u.GetForegroundWindow()
    if not hwnd:
        return None
    cls = ctypes.create_unicode_buffer(256)
    u.GetClassNameW(hwnd, cls, 256)
    if cls.value in ("Progman", "WorkerW", "Shell_TrayWnd"):  # area de trabalho e barra de tarefas
        return None
    r = wt.RECT()
    u.GetWindowRect(hwnd, ctypes.byref(r))

    class MI(ctypes.Structure):
        _fields_ = [("cbSize", wt.DWORD), ("rcMonitor", wt.RECT), ("rcWork", wt.RECT), ("dwFlags", wt.DWORD)]
    mi = MI()
    mi.cbSize = ctypes.sizeof(MI)
    u.GetMonitorInfoW(u.MonitorFromWindow(hwnd, 2), ctypes.byref(mi))
    m = mi.rcMonitor
    if not (r.left <= m.left and r.top <= m.top and r.right >= m.right and r.bottom >= m.bottom):
        return None
    pid = wt.DWORD()
    u.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    h = k.OpenProcess(0x1000, False, pid.value)  # PROCESS_QUERY_LIMITED_INFORMATION
    buf, n = ctypes.create_unicode_buffer(520), wt.DWORD(520)
    k.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(n))
    k.CloseHandle(h)
    return os.path.basename(buf.value) or cls.value


def detectar_linux():
    if not (shutil.which("xdotool") and shutil.which("xprop")):
        return None
    jid = subprocess.run(["xdotool", "getactivewindow"], capture_output=True, text=True, timeout=5).stdout.strip()
    if not jid:
        return None
    out = subprocess.run(["xprop", "-id", jid, "_NET_WM_STATE", "WM_CLASS"], capture_output=True, text=True, timeout=5).stdout
    return parse_xprop(out)


def detectar(tela=(1920, 1080)):
    """Nome do app em primeiro plano se ele ocupa a tela inteira; senao None."""
    try:
        if sys.platform == "darwin":
            return detectar_mac(tela)
        if sys.platform == "win32":
            return detectar_windows()
        if sys.platform.startswith("linux"):
            return detectar_linux()
    except Exception:
        return None
    return None


class Vigia(threading.Thread):
    """Olha a janela em primeiro plano a cada 1 s; exige 2 leituras iguais antes de avisar o Estado."""

    def __init__(self, estado, detector=None, intervalo=1.0):
        super().__init__(daemon=True)
        self.estado, self.intervalo = estado, intervalo
        self.detector = detector or (lambda: detectar(self.estado.tela_tamanho()))
        self.parar = threading.Event()
        self._ult, self._atual = None, None

    def passo(self):
        self.estado.recarregar_prefs()
        app = self.detector() if self.estado.prefs["ativo"] else None
        if app == self._ult and app != self._atual:
            self._atual = app
            self.estado.presentacao(app)
        self._ult = app

    def run(self):
        while not self.parar.wait(self.intervalo):
            try:
                self.passo()
            except Exception as e:
                print(f"aviso: vigia falhou ({e})", file=sys.stderr)


def _abrir(url):
    try:
        import webbrowser
        webbrowser.open(url)
    except Exception:
        pass


def _notificar_linux(titulo, texto, url):
    """notify-send com acao: clicar no aviso (ou em 'Abrir') abre a pagina. Sem suporte a --action, aviso simples."""
    base = ["notify-send", "-a", "Baton", "-t", "10000"]
    if url:
        try:
            r = subprocess.run(base + ["--action=default=Abrir", "--action=abrir=Abrir", "-w", titulo, texto],
                               capture_output=True, text=True, timeout=30)
            if r.returncode == 0:
                if r.stdout.strip() in ("default", "abrir"):
                    _abrir(url)
                return
        except Exception:
            pass
    subprocess.run(base + [titulo, texto], check=False)


def _notificar_windows(titulo, texto, url):
    q = lambda s: (s or "").replace("'", "")
    ps = ("Add-Type -AssemblyName System.Windows.Forms,System.Drawing;$n=New-Object System.Windows.Forms.NotifyIcon;"
          "$n.Icon=[System.Drawing.SystemIcons]::Information;$n.Visible=$true;"
          f"$u='{q(url)}';"
          "if($u){Register-ObjectEvent -InputObject $n -EventName BalloonTipClicked -Action {Start-Process $u} | Out-Null};"
          f"$n.ShowBalloonTip(8000,'{q(titulo)}','{q(texto)}',[System.Windows.Forms.ToolTipIcon]::Info);"
          "$t=[DateTime]::Now.AddSeconds(12);while([DateTime]::Now -lt $t){[System.Windows.Forms.Application]::DoEvents();Start-Sleep -Milliseconds 100};$n.Dispose()")
    subprocess.Popen(["powershell", "-NoProfile", "-WindowStyle", "Hidden", "-Command", ps])


def notificar(titulo, texto, url=None):
    """Aviso do sistema (melhor esforco). Com url, clicar no aviso abre a pagina local do Baton.

    macOS: precisa do terminal-notifier (brew install terminal-notifier) para o clique; sem ele o aviso e so informativo.
    Alguns sistemas suprimem avisos durante apresentacoes."""
    try:
        if sys.platform == "darwin":
            tn = shutil.which("terminal-notifier")
            if tn:
                cmd = [tn, "-title", titulo, "-message", texto, "-group", "baton", "-sender", "com.apple.Terminal"]
                if url:
                    cmd += ["-open", url]
                subprocess.Popen(cmd)
            else:
                esc = lambda s: s.replace("\\", "\\\\").replace('"', '\\"')
                subprocess.Popen(["osascript", "-e", f'display notification "{esc(texto)}" with title "{esc(titulo)}"'])
        elif sys.platform == "win32":
            _notificar_windows(titulo, texto, url)
        elif shutil.which("notify-send"):
            threading.Thread(target=_notificar_linux, args=(titulo, texto, url), daemon=True).start()
    except Exception:
        pass


def iniciar_tray(porta):
    """Sobe o icone da bandeja (Windows: tray_win.ps1; Linux: tray_gtk.py). No macOS a bandeja e o proprio Baton.app.
    Devolve o processo ou None (sem helper, sem Gtk, ja encerrou)."""
    base = Path(__file__).resolve().parent
    try:
        if sys.platform == "win32":
            ps1 = base / "tray_win.ps1"
            if not ps1.exists():
                return None
            return subprocess.Popen(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-WindowStyle", "Hidden",
                                     "-File", str(ps1), "-Porta", str(porta)])
        if sys.platform.startswith("linux"):
            py = base / "tray_gtk.py"
            if not py.exists():
                return None
            p = subprocess.Popen([sys.executable, str(py), "--porta", str(porta)], stderr=subprocess.PIPE, text=True)
            try:
                p.wait(timeout=1.5)  # Gtk indisponivel: sai logo com codigo 3
                print("aviso: sem icone na bandeja (" + (p.stderr.read() or "").strip() + ")", file=sys.stderr)
                return None
            except subprocess.TimeoutExpired:
                return p
    except OSError:
        return None
    return None


PAINEL_HTML = """<!doctype html><html lang="pt-br"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Baton - painel</title><style>
body{font-family:-apple-system,system-ui,sans-serif;background:#111;color:#eee;max-width:560px;margin:32px auto;padding:0 16px}
h1{font-size:24px}h2{font-size:15px;color:#999;margin:24px 0 8px;font-weight:600}
.cartao{background:#1c1c1e;border-radius:12px;padding:14px;margin-bottom:10px}
button{background:#0a84ff;color:#fff;border:0;border-radius:10px;padding:10px 16px;font-size:15px;margin:4px 6px 4px 0;cursor:pointer}
button.cinza{background:#3a3a3c}button.verm{background:#ff453a}
select{background:#3a3a3c;color:#fff;border:0;border-radius:10px;padding:10px;font-size:15px}
.ok{color:#30d158}.pausa{color:#ffd60a}li{margin:6px 0}small{color:#888}
</style></head><body><h1>Baton</h1><div class="cartao" id="st">...</div>
<div class="cartao" id="acess" style="display:none;border:1px solid #ff453a"><b>O Mac nao deixou o Baton enviar teclas</b>
<p><small>Sem essa permissao o Proximo/Anterior do celular nao funciona. Ative o Baton em Acessibilidade (se houver uma entrada antiga, remova e adicione de novo).</small></p>
<button id="bac">Pedir permissao e abrir Ajustes</button></div>
<h2>Apresentacao</h2><div class="cartao"><div id="drop" style="border:2px dashed #3a3a3c;border-radius:12px;padding:22px;text-align:center;cursor:pointer">
<b>Solte aqui o arquivo da apresentacao</b><br><small>PDF, PowerPoint (.pptx), ODP, ou um .md de notas. Ou clique para escolher.</small></div>
<input type="file" id="nf" accept=".pdf,.pptx,.ppt,.odp,.md,.txt" style="display:none">
<p id="dk"></p><p id="dmsg"></p>
<button id="bap" style="display:none">Apresentar em tela cheia</button> <button class="cinza" id="bdk" style="display:none">Remover apresentacao</button>
<button class="cinza" id="btt">Testar tecla (em 5 s)</button>
<p><small>Com a apresentacao carregada o celular mostra o slide atual, o proximo e as notas; as teclas continuam indo para o app em primeiro plano (PowerPoint, Keynote, ReLaTeX...). Notas tambem podem ser digitadas no celular.</small></p></div>
<h2>Celulares</h2><div class="cartao"><ul id="cels"></ul>
<button id="bpn">Parear novo celular</button> <button class="cinza" id="bqr2">Mostrar QR code</button>
<p><small>Cada QR vale para um celular e muda depois de usado. Desconectar tira o acesso daquele celular.</small></p></div>
<h2>Sem internet (Bluetooth)</h2><div class="cartao"><div id="btst"></div>
<div style="margin:8px 0"><button class="cinza" id="tbi">iPhone</button> <button class="cinza" id="tba">Android</button></div>
<ol id="btpassos"></ol>
<button class="cinza" id="bbq">Mostrar QR code desta rede</button>
<p><small>O celular compartilha a internet por Bluetooth e o computador entra nessa rede: nao precisa de Wi-Fi nem de Tailscale. A conexao e lenta (1 a 2 Mb/s), por isso o celular entra em modo leve: passa slides e mostra notas; o PDF e lido aos poucos.</small></p></div>
<h2>Execucao</h2><div class="cartao"><button id="bp">Pausar</button><button class="cinza" id="bq">Mostrar QR code</button>
<button class="cinza" id="ba">Autorizar agora</button></div>
<h2>Quando uma apresentacao em tela cheia abrir</h2><div class="cartao"><select id="modo">
<option value="perguntar">Perguntar no celular</option><option value="sempre">Controlar sempre, sem perguntar</option><option value="nunca">Nunca controlar</option></select>
<p><small>Ate alguem aceitar, o Baton nao envia nenhuma tecla ao computador.</small></p></div>
<h2>Apps lembrados</h2><div class="cartao"><b>Sempre controlar</b><ul id="ok"></ul><b>Nunca controlar</b><ul id="nao"></ul></div>
<h2>Inicio com o sistema</h2><div class="cartao"><button id="bi">Iniciar com o sistema</button><button class="cinza" id="br">Remover do inicio</button></div>
<h2>Encerrar</h2><div class="cartao"><button class="verm" id="bs">Sair do Baton</button><p><small>Para voltar, abra o Baton de novo (ou reinicie a sessao se estiver instalado).</small></p></div>
<script>
const $=id=>document.getElementById(id);let est={};
async function acao(a,v){const r=await fetch('/painel/acao',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({acao:a,valor:v})});if(r.ok)est=await r.json();ver()}
function lista(el,itens,tipo){el.innerHTML='';if(!itens.length){el.innerHTML='<li><small>nenhum</small></li>';return}
  itens.forEach(a=>{const li=document.createElement('li');li.textContent=a+' ';const b=document.createElement('button');b.className='cinza';b.textContent='Esquecer';b.onclick=()=>acao('esquecer',a);li.appendChild(b);el.appendChild(li)})}
const BT={iphone:['Pareie o iPhone com o computador (Ajustes do Bluetooth nos dois).','No iPhone: Ajustes > Ponto de Acesso Pessoal > ative "Permitir que outros entrem".','__CONECTAR__','Quando este cartao mostrar "Conectado por Bluetooth", escaneie o QR code desta rede.'],
android:['Pareie o Android com o computador (Bluetooth nos dois).','No Android: Ajustes > Conexoes (ou Rede e internet) > Ponto de acesso e compartilhamento > ative "Tethering Bluetooth".','__CONECTAR__','Quando este cartao mostrar "Conectado por Bluetooth", escaneie o QR code desta rede.']};
const CONECTAR={darwin:'No Mac: Ajustes do Sistema > Bluetooth > ao lado do celular, "Conectar a rede" (ou clique no icone de Bluetooth na barra de menus).',
win32:'No Windows: Configuracoes > Bluetooth e dispositivos > Dispositivos > Mais configuracoes > Dispositivos e Impressoras > botao direito no celular > Conectar usando > Ponto de acesso.',
linux:'No Linux: Configuracoes > Bluetooth > clique no celular > Conectar a rede (ou: nmcli device connect <MAC>).'};
let btAba='iphone';
function verBt(){
  const b=est.rede_bluetooth;$('btst').innerHTML=b?'<span class="ok">Conectado por Bluetooth</span> <small>('+b.interface+', '+b.ip+')</small>':'<span class="pausa">Aguardando o computador entrar na rede do celular...</span>';
  $('btpassos').innerHTML=BT[btAba].map(t=>'<li>'+(t==='__CONECTAR__'?(CONECTAR[est.plataforma]||CONECTAR.linux):t)+'</li>').join('');
  $('tbi').className=btAba==='iphone'?'':'cinza';$('tba').className=btAba==='android'?'':'cinza'}
$('tbi').onclick=()=>{btAba='iphone';verBt()};$('tba').onclick=()=>{btAba='android';verBt()};$('bbq').onclick=()=>window.open('/qr','_blank');
function ver(){
  $('st').innerHTML=est.ativo?'<span class="ok">Ativo</span>'+(est.autorizado?' - controlando':' - em espera')+(est.pedido?'<br><small>Pedido pendente: '+est.pedido.app+'</small>':''):'<span class="pausa">Pausado</span>';
  $('bp').textContent=est.ativo?'Pausar':'Retomar';$('modo').value=est.modo;
  $('acess').style.display=est.acessibilidade===false?'block':'none';
  $('dk').innerHTML=est.deck?'<b>'+est.deck.nome.replace(/</g,'&lt;')+'</b> - '+est.deck.paginas+' paginas':'<small>Nenhuma apresentacao carregada: o celular so passa os slides, sem previa nem notas.</small>';$('bdk').style.display=est.deck?'inline-block':'none';
  const cl=$('cels');cl.innerHTML='';(est.dispositivos||[]).forEach(d=>{const li=document.createElement('li');
    li.textContent=d.nome+(d.conectado?' - conectado':' - desconectado')+' ';const b=document.createElement('button');b.className='cinza';
    b.textContent='Desconectar';b.onclick=()=>acao('revogar_celular',d.id);li.appendChild(b);cl.appendChild(li)});
  if(!(est.dispositivos||[]).length)cl.innerHTML='<li><small>nenhum celular pareado</small></li>';
  verBt();
  $('bap').style.display=est.deck?'inline-block':'none';$('bap').textContent=est.tela_propria?'Fechar tela cheia':'Apresentar em tela cheia';
  lista($('ok'),est.apps_ok||[]);lista($('nao'),est.apps_nao||[]);$('ba').textContent=est.autorizado?'Revogar autorizacao':'Autorizar agora'}
$('bp').onclick=()=>acao(est.ativo?'pausar':'retomar');$('ba').onclick=()=>acao(est.autorizado?'revogar':'autorizar');
$('modo').onchange=e=>acao('modo',e.target.value);$('bi').onclick=()=>acao('instalar');$('br').onclick=()=>acao('desinstalar');
$('bq').onclick=()=>window.open('/qr','_blank');$('bs').onclick=()=>{if(confirm('Encerrar o Baton?'))acao('sair')};
async function enviarDeck(f){if(!f)return;$('dmsg').textContent='Carregando '+f.name+' (arquivos pptx sao convertidos, pode levar um minuto)...';
  try{const r=await fetch('/painel/deck?nome='+encodeURIComponent(f.name),{method:'POST',body:await f.arrayBuffer()});const j=await r.json();
    if(r.ok){est=j;$('dmsg').textContent='';ver()}else $('dmsg').textContent='Falhou: '+(j.erro||r.status)}catch(x){$('dmsg').textContent='Falhou: '+x}}
$('drop').onclick=()=>$('nf').click();$('nf').onchange=e=>{enviarDeck(e.target.files[0]);e.target.value=''};
['dragover','dragenter'].forEach(t=>$('drop').addEventListener(t,e=>{e.preventDefault();$('drop').style.borderColor='#0a84ff'}));
['dragleave','drop'].forEach(t=>$('drop').addEventListener(t,e=>{e.preventDefault();$('drop').style.borderColor='#3a3a3c'}));
$('drop').addEventListener('drop',e=>enviarDeck(e.dataTransfer.files[0]));
$('bdk').onclick=()=>acao('remover_deck');$('bap').onclick=()=>acao(est.tela_propria?'fechar_tela':'apresentar');$('btt').onclick=()=>{acao('testar_tecla');$('dmsg').textContent='Troque para o app da apresentacao: em 5 s o Baton envia seta direita e esquerda.'};
$('bac').onclick=()=>acao('abrir_acessibilidade');
$('bpn').onclick=async()=>{await acao('parear_novo');window.open('/qr','_blank')};$('bqr2').onclick=()=>window.open('/qr','_blank');
fetch('/painel/estado').then(r=>r.json()).then(e=>{est=e;ver()});setInterval(()=>fetch('/painel/estado').then(r=>r.json()).then(e=>{est=e;ver()}).catch(()=>{}),3000);
</script></body></html>"""
