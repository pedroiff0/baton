"""relay.py: relay de sessoes do Baton.

Varios computadores, cada um com seu Baton, atras de UMA URL publica (ex.: https://baton.exemplo.com).
O relay roda no servidor (`baton --servir-relay`); cada Baton e um agente que se conecta de saida por WebSocket
e recebe as requisicoes dos celulares. O roteamento usa a credencial do proprio celular (codigo do QR em /p/<codigo>
ou token), entao as paginas continuam com caminhos absolutos e nada muda no celular. Somente biblioteca padrao.

Privacidade: sem login, sem conta, sem analytics. O relay nao registra IP (so conta, em memoria, conexoes simultaneas por IP no registro aberto) nem le o conteudo (so repassa); nao recebe nome,
sistema ou qualquer dado do computador. Guarda em disco apenas o hash da chave de cada computador autorizado e o rotulo
que o administrador deu ao convite; o resto (quem esta online, fluxos abertos) existe so em memoria.
"""
import base64
import hashlib
import http.client
import json
import os
import queue
import secrets
import socket
import ssl
import struct
import threading
import time
from pathlib import Path
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
PEDACO = 64 * 1024
MAX_CORPO = 64 * 1024 * 1024
HOP = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "te", "trailers", "transfer-encoding", "upgrade"}
ESTATICOS = ("/vendor/", "/icon")
T_CAB, T_DADOS, T_FIM = 0, 1, 2  # tipos dos quadros binarios: cabecalho (JSON), pedaco do corpo, fim


def hash_token(t):
    return hashlib.sha256(str(t).encode()).hexdigest()


class WS:
    """WebSocket minimo (RFC 6455): quadros inteiros ou fragmentados, ping/pong, mascara do cliente."""

    def __init__(self, sock, cliente, inicio=b""):
        self.s, self.cliente, self.buf, self.lock = sock, cliente, inicio, threading.Lock()

    def _ler(self, n):
        while len(self.buf) < n:
            d = self.s.recv(65536)
            if not d:
                raise ConnectionError("conexao fechada")
            self.buf += d
        r, self.buf = self.buf[:n], self.buf[n:]
        return r

    def enviar(self, op, dados=b""):
        if isinstance(dados, str):
            dados = dados.encode()
        n = len(dados)
        cab = bytes([0x80 | op])
        m = 0x80 if self.cliente else 0
        cab += bytes([m | n]) if n < 126 else (bytes([m | 126]) + struct.pack(">H", n) if n < 65536 else bytes([m | 127]) + struct.pack(">Q", n))
        if self.cliente:
            chave = os.urandom(4)
            if n:
                dados = (int.from_bytes(dados, "big") ^ int.from_bytes((chave * (n // 4 + 1))[:n], "big")).to_bytes(n, "big")
            cab += chave
        with self.lock:
            self.s.sendall(cab + dados)

    def receber(self):
        """Proxima mensagem de dados: (1, texto) ou (2, bytes). Responde ping; levanta ConnectionError no close."""
        op_msg, partes = None, []
        while True:
            b0, b1 = self._ler(2)
            fim, op, n = b0 & 0x80, b0 & 0x0F, b1 & 0x7F
            if n == 126:
                n = struct.unpack(">H", self._ler(2))[0]
            elif n == 127:
                n = struct.unpack(">Q", self._ler(8))[0]
            if n > 2 * PEDACO + 4096:
                raise ConnectionError("quadro grande demais")
            chave = self._ler(4) if b1 & 0x80 else None
            dados = self._ler(n) if n else b""
            if chave and n:
                dados = (int.from_bytes(dados, "big") ^ int.from_bytes((chave * (n // 4 + 1))[:n], "big")).to_bytes(n, "big")
            if op == 0x8:
                raise ConnectionError("close")
            if op == 0x9:
                self.enviar(0xA, dados)
                continue
            if op == 0xA:
                continue
            if op in (1, 2):
                op_msg, partes = op, [dados]
            else:
                partes.append(dados)
            if fim:
                corpo = b"".join(partes)
                return op_msg, (corpo.decode("utf-8", "replace") if op_msg == 1 else corpo)

    def fechar(self):
        try:
            self.s.shutdown(socket.SHUT_RDWR)  # acorda quem esta bloqueado em recv() em outra thread
        except OSError:
            pass
        try:
            self.s.close()
        except OSError:
            pass


def quadro(rid, tipo, dados=b""):
    return struct.pack(">IB", rid, tipo) + dados


# ------------------------------------------------------------------ servidor (relay)

ALFABETO = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # sem 0/O/1/I


class Registro:
    """Computadores autorizados (hash da chave + rotulo do admin) e convites de uso unico. Anonimo: nada sobre quem usa."""

    def __init__(self, arquivo=None):
        self.arq = Path(arquivo) if arquivo else None
        self.lock = threading.RLock()
        self.d = {"computadores": [], "convites": [], "aberto": True}  # aberto: registro sem convite (chave vazia), com limites
        try:
            if self.arq:
                self.d.update(json.loads(self.arq.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            pass

    def _salvar(self):
        if not self.arq:
            return
        try:
            self.arq.parent.mkdir(parents=True, exist_ok=True)
            self.arq.write_text(json.dumps(self.d, ensure_ascii=False), encoding="utf-8")
            os.chmod(self.arq, 0o600)
        except OSError:
            pass

    @property
    def aberto(self):
        return bool(self.d.get("aberto", True))

    def definir_aberto(self, valor):
        with self.lock:
            self.d["aberto"] = bool(valor)
            self._salvar()

    @staticmethod
    def _norm(codigo):
        return "".join(c for c in str(codigo).upper() if c.isalnum())

    def criar_convite(self, rotulo="", validade=86400):
        codigo = "".join(secrets.choice(ALFABETO) for _ in range(12))
        with self.lock:
            agora = time.time()
            self.d["convites"] = [c for c in self.d["convites"] if c["expira"] > agora][-49:]
            self.d["convites"].append({"id": secrets.token_hex(3), "h": hash_token(codigo), "rotulo": str(rotulo)[:40], "expira": agora + validade})
            self._salvar()
        return "-".join(codigo[i:i + 4] for i in range(0, 12, 4))

    def resgatar(self, codigo):
        """Troca um convite valido por uma chave nova (devolvida uma unica vez); None se invalido/expirado/usado."""
        h = hash_token(self._norm(codigo))
        with self.lock:
            agora = time.time()
            for c in self.d["convites"]:
                if secrets.compare_digest(c["h"], h) and c["expira"] > agora:
                    self.d["convites"].remove(c)
                    chave = secrets.token_urlsafe(24)
                    cid = secrets.token_hex(4)
                    self.d["computadores"].append({"id": cid, "rotulo": c["rotulo"], "h": hash_token(chave), "criado": agora})
                    self._salvar()
                    return cid, chave
        return None

    def autenticar(self, chave):
        h = hash_token(chave)
        with self.lock:
            for c in self.d["computadores"]:
                if secrets.compare_digest(c["h"], h):
                    return dict(c)
        return None

    def revogar(self, cid):
        with self.lock:
            n = len(self.d["computadores"])
            self.d["computadores"] = [c for c in self.d["computadores"] if c["id"] != cid]
            self._salvar()
            return len(self.d["computadores"]) < n

    def revogar_convite(self, iid):
        with self.lock:
            n = len(self.d["convites"])
            self.d["convites"] = [c for c in self.d["convites"] if c["id"] != iid]
            self._salvar()
            return len(self.d["convites"]) < n

    def rotular(self, cid, rotulo):
        with self.lock:
            for c in self.d["computadores"]:
                if c["id"] == cid:
                    c["rotulo"] = str(rotulo)[:40]
                    self._salvar()
                    return True
        return False


class Agente:
    def __init__(self, ws, cid=""):
        self.ws, self.cid, self.sid = ws, cid, secrets.token_urlsafe(8)  # cid vazio = chave de administrador (este servidor)
        self.codigos, self.tokens, self.pend, self.lock, self._id = set(), set(), {}, threading.Lock(), 0
        self.vivo, self.desde, self.fluxos = True, time.time(), 0  # fluxos: /events abertos (celulares conectados), so em memoria
        self.aberto, self.ip_chave = False, ""  # aberto: entrou sem convite; ip_chave so conta conexoes simultaneas (apagado ao sair)

    def novo_id(self):
        with self.lock:
            self._id += 1
            return self._id

    def cair(self):
        self.vivo = False
        with self.lock:
            filas = list(self.pend.values())
        for f in filas:
            f.put(("erro", None))
        self.ws.fechar()


class Relay:
    def __init__(self, chave, max_sessoes=50, registro=None, publico=""):
        self.chave, self.max_sessoes, self.agentes, self.lock = chave, max_sessoes, {}, threading.Lock()
        self.registro, self.publico = registro or Registro(), publico
        self.falhas = {}  # ip -> instantes de tentativas erradas (limite por minuto; nao e gravado)

    MAX_ABERTOS_POR_IP = 3

    def abertos_do_ip(self, ip):
        with self.lock:
            return sum(1 for a in self.agentes.values() if a.ip_chave == ip)

    def vagas_abertas(self):
        """Registro aberto nunca ocupa as ultimas 20% das vagas: sobram para computadores com convite."""
        with self.lock:
            n = sum(1 for a in self.agentes.values() if a.aberto)
        return n < int(self.max_sessoes * 0.8)

    def derrubar(self, sid):
        with self.lock:
            a = self.agentes.get(sid)
        if a:
            a.cair()
        return bool(a)

    def desconectar(self, cid):
        with self.lock:
            ags = [a for a in self.agentes.values() if a.cid == cid]
        for a in ags:
            a.cair()

    def limite(self, ip, maximo=10):
        agora = time.time()
        f = [t for t in self.falhas.get(ip, []) if agora - t < 60]
        self.falhas[ip] = f
        return len(f) >= maximo

    def falhou(self, ip):
        self.falhas.setdefault(ip, []).append(time.time())

    def registrar(self, ag):
        with self.lock:
            if len(self.agentes) >= self.max_sessoes:
                return False
            self.agentes[ag.sid] = ag
            return True

    def remover(self, ag):
        with self.lock:
            self.agentes.pop(ag.sid, None)
        ag.cair()

    def achar(self, codigo="", token="", sid="", estatico=False):
        with self.lock:
            ags = [a for a in self.agentes.values() if a.vivo]
        if codigo:
            for a in ags:
                if codigo in a.codigos:
                    return a
        if token:
            h = hash_token(token)
            for a in ags:
                if h in a.tokens:
                    return a
        for a in ags:
            if sid and a.sid == sid:
                return a
        return None  # nunca "qualquer agente": um agente aberto serviria scripts sob o dominio de outro


def _igual(a, b):
    try:
        return secrets.compare_digest(str(a).encode(), str(b).encode())
    except TypeError:
        return False


def _cookie(cabecalhos, nome):
    try:
        c = SimpleCookie(cabecalhos.get("Cookie", ""))
        return c[nome].value if nome in c else ""
    except Exception:
        return ""


PAINEL_RELAY = r"""<!doctype html><html lang="pt-br"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="robots" content="noindex"><title>Baton central</title>
<style>
:root{--bg:#0b0b0d;--card:#17171a;--linha:#2a2a2e;--tx:#f2f2f4;--mut:#8e8e93;--az:#0a84ff;--vd:#30d158;--vm:#ff453a}
@media (prefers-color-scheme:light){:root{--bg:#f4f4f6;--card:#fff;--linha:#dcdce0;--tx:#18181b;--mut:#6b6b73}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--tx);font:15px/1.45 -apple-system,system-ui,sans-serif;padding:max(18px,env(safe-area-inset-top)) 16px 40px}
main{max-width:760px;margin:0 auto}h1{font-size:24px;margin:6px 0 2px}h2{font-size:13px;letter-spacing:.06em;text-transform:uppercase;color:var(--mut);margin:26px 0 8px}
.sub{color:var(--mut);font-size:13px;margin:0 0 4px}.card{background:var(--card);border:1px solid var(--linha);border-radius:14px;padding:12px 14px;margin:8px 0}
.lin{display:flex;align-items:center;gap:10px;flex-wrap:wrap}.lin>.g{flex:1;min-width:0}
.dot{width:10px;height:10px;border-radius:50%;background:var(--mut);flex:none}.dot.on{background:var(--vd);box-shadow:0 0 8px var(--vd)}
b{font-weight:600}small,.mut{color:var(--mut);font-size:13px}code{font:13px ui-monospace,Menlo,monospace;word-break:break-all}
button,input{font:inherit;border-radius:10px;border:1px solid var(--linha);background:var(--card);color:var(--tx);padding:8px 12px}
button{cursor:pointer}button.az{background:var(--az);border-color:var(--az);color:#fff;font-weight:600}button.vm{color:var(--vm)}
input{width:100%}.cod{font:600 26px ui-monospace,Menlo,monospace;letter-spacing:.08em;text-align:center;padding:10px 0}
pre{margin:6px 0;padding:10px;border-radius:10px;background:var(--bg);border:1px solid var(--linha);white-space:pre-wrap;word-break:break-all;font:12.5px ui-monospace,Menlo,monospace}
#login{display:none}.vazio{color:var(--mut);padding:6px 2px}
</style></head><body><main>
<h1>Baton central</h1><p class="sub" id="resumo">Entre com a chave de administrador deste relay.</p>
<div id="login" class="card"><div class="lin"><div class="g"><input id="chave" type="password" placeholder="Chave de administrador" autocomplete="current-password"></div><button class="az" id="entrar">Entrar</button></div><small id="erro" style="color:var(--vm)"></small></div>
<div id="app" style="display:none">
<h2>Computadores</h2><div id="comps"></div>
<h2>Registro aberto</h2><div class="card"><div class="lin"><div class="g"><b id="abtit"></b><br><small>Aberto: qualquer Baton entra sem convite, com limite por IP e vagas reservadas. Fechado: so com convite.</small></div><button id="abtog"></button></div><div id="abertos"></div></div>
<h2>Novo computador</h2><div class="card"><div class="lin"><div class="g"><input id="rotulo" placeholder="Rotulo so seu, opcional (ex.: Mac do instituto)" maxlength="40"></div><button class="az" id="criar">Criar convite</button></div>
<div id="novo"></div></div>
<h2>Convites pendentes</h2><div id="convs"></div>
<p class="mut" style="margin-top:28px">Este relay nao tem login de usuarios e nao guarda IP, nome, conta nem conteudo: so repassa o trafego entre o celular e o computador. Em disco ficam apenas o hash da chave de cada computador autorizado e o rotulo que voce deu. Quem esta online e quantos celulares estao conectados existem so em memoria.</p>
</div></main>
<script>
const $=id=>document.getElementById(id);let K=sessionStorage.getItem('baton-adm')||'',ES=null;
const esc=t=>String(t).replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
async function api(c,d){const r=await fetch('/relay/api/'+c,{method:d?'POST':'GET',headers:{'X-Baton-Admin':K,'Content-Type':'application/json'},body:d?JSON.stringify(d):undefined});
  if(r.status===403){sessionStorage.removeItem('baton-adm');K='';mostra(false);$('erro').textContent='Chave invalida.';throw 0}return r.json()}
function mostra(ok){$('login').style.display=ok?'none':'block';$('app').style.display=ok?'block':'none'}
const dur=s=>s<60?s+' s':s<3600?Math.floor(s/60)+' min':Math.floor(s/3600)+' h '+Math.floor(s%3600/60)+' min';
const host=()=>(ES&&ES.publico?ES.publico.replace(/^https?:\/\//,''):location.host);
async function ver(){try{ES=await api('estado')}catch(e){return}mostra(true);
  const on=ES.computadores.filter(c=>c.online).length+ES.locais.length+ES.abertos.length;
  $('resumo').textContent=on+' de '+ES.max+' sessoes online - '+(ES.computadores.reduce((a,c)=>a+c.celulares,0)+ES.locais.reduce((a,c)=>a+c.celulares,0)+ES.abertos.reduce((a,c)=>a+c.celulares,0))+' celulares conectados agora';
  $('comps').innerHTML=(ES.locais.map(l=>'<div class="card lin"><span class="dot on"></span><div class="g"><b>Este servidor</b> <small>(chave de administrador)</small><br><small>online ha '+dur(l.online_ha)+' - '+l.celulares+' celular(es)</small></div></div>').join('')+
   ES.computadores.map(c=>'<div class="card lin"><span class="dot '+(c.online?'on':'')+'"></span><div class="g"><b>'+esc(c.rotulo||'Computador '+c.id)+'</b> <small>'+c.id+'</small><br><small>'+(c.online?'online ha '+dur(c.online_ha)+' - '+c.celulares+' celular(es)':'offline')+'</small></div>'+
   '<button data-r="'+c.id+'">Renomear</button><button class="vm" data-v="'+c.id+'">Revogar</button></div>').join(''))||'<div class="vazio">Nenhum computador ainda. Crie um convite abaixo.</div>';
  $('abtit').textContent=ES.aberto?'Ligado ('+ES.abertos.length+' online)':'Desligado';$('abtog').textContent=ES.aberto?'Fechar registro':'Abrir registro';$('abtog').className=ES.aberto?'vm':'az';
  $('abertos').innerHTML=ES.abertos.map(a=>'<div class="lin" style="margin-top:8px"><span class="dot on"></span><div class="g"><b>Sessao '+a.sid.slice(0,4)+'</b> <small>online ha '+dur(a.online_ha)+' - '+a.celulares+' celular(es)</small></div><button class="vm" data-d="'+a.sid+'">Derrubar</button></div>').join('');
  document.querySelectorAll('[data-d]').forEach(b=>b.onclick=async()=>{await api('derrubar',{sid:b.dataset.d});ver()});
  $('convs').innerHTML=ES.convites.map(c=>'<div class="card lin"><div class="g"><b>'+esc(c.rotulo||'Sem rotulo')+'</b><br><small>expira em '+dur(Math.max(0,Math.floor(c.expira-Date.now()/1000)))+'</small></div><button class="vm" data-i="'+c.id+'">Cancelar</button></div>').join('')||'<div class="vazio">Nenhum.</div>';
  document.querySelectorAll('[data-v]').forEach(b=>b.onclick=async()=>{if(confirm('Revogar este computador? Ele para de conectar agora.')){await api('revogar',{id:b.dataset.v});ver()}});
  document.querySelectorAll('[data-r]').forEach(b=>b.onclick=async()=>{const n=prompt('Novo rotulo:');if(n!==null){await api('rotular',{id:b.dataset.r,rotulo:n});ver()}});
  document.querySelectorAll('[data-i]').forEach(b=>b.onclick=async()=>{await api('revogar-convite',{id:b.dataset.i});ver()})}
$('abtog').onclick=async()=>{await api('aberto',{valor:!ES.aberto});ver()};
$('entrar').onclick=()=>{K=$('chave').value;sessionStorage.setItem('baton-adm',K);$('erro').textContent='';ver()};
$('chave').onkeydown=e=>{if(e.key==='Enter')$('entrar').click()};
$('criar').onclick=async()=>{const r=await api('convite',{rotulo:$('rotulo').value});$('rotulo').value='';const h=r.publico?r.publico.replace(/^https?:\/\//,''):location.host,c=h+'/'+r.codigo;
  const cmds=[['Qualquer sistema (Baton ja instalado)','baton --convite '+c],['Mac ou Linux (instala e conecta)','git clone https://github.com/pedroiff0/baton && cd baton && BATON_CONVITE='+c+' ./install.sh'],
   ['Windows (PowerShell, na pasta do Baton)','$env:BATON_CONVITE="'+c+'"; .\\install.ps1']];
  $('novo').innerHTML='<div class="cod">'+r.codigo+'</div><small>Vale por 24 h e uma unica vez. Mande um dos comandos para a pessoa:</small>'+cmds.map((x,i)=>'<div class="mut" style="margin-top:8px">'+x[0]+'</div><pre id="p'+i+'">'+esc(x[1])+'</pre><button data-c="'+i+'">Copiar</button>').join('');
  document.querySelectorAll('[data-c]').forEach(b=>b.onclick=()=>{navigator.clipboard&&navigator.clipboard.writeText($('p'+b.dataset.c).textContent);b.textContent='Copiado'});ver()};
if(K)ver();else mostra(false);setInterval(()=>{if(K&&document.visibilityState==='visible')ver()},4000);
</script></body></html>"""


def criar_handler(relay):
    class H(BaseHTTPRequestHandler):
        server_version = "BatonRelay"
        disable_nagle_algorithm = True  # quadros pequenos (comandos, laser) saem na hora, sem esperar o ACK

        def log_message(self, *a):
            pass

        def _texto(self, status, msg):
            corpo = msg.encode()
            self.send_response(status)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(corpo)))
            self.end_headers()
            self.wfile.write(corpo)

        def _ip(self):
            return self.headers.get("Cf-Connecting-Ip") or self.client_address[0]

        def _json(self, status, obj):
            corpo = json.dumps(obj).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(corpo)))
            self.end_headers()
            self.wfile.write(corpo)

        def _corpo_json(self):
            try:
                n = min(int(self.headers.get("Content-Length") or 0), 4096)
                return json.loads(self.rfile.read(n) or b"{}")
            except ValueError:
                return {}

        def _admin_ok(self):
            if relay.limite(self._ip()):
                return False
            ok = _igual(self.headers.get("X-Baton-Admin", ""), relay.chave)
            if not ok:
                relay.falhou(self._ip())
                time.sleep(1)
            return ok

        def _resgatar(self):
            """POST /relay/convite {codigo}: troca o convite (uso unico) por uma chave do computador. Anonimo: nada e perguntado."""
            if relay.limite(self._ip()):
                return self._json(429, {"erro": "muitas tentativas; aguarde um minuto"})
            r = relay.registro.resgatar(self._corpo_json().get("codigo", ""))
            if r is None:
                relay.falhou(self._ip())
                time.sleep(1)
                return self._json(403, {"erro": "convite invalido, expirado ou ja usado"})
            self._json(200, {"id": r[0], "chave": r[1]})

        def _api(self, caminho):
            if not self._admin_ok():
                return self._json(403, {"erro": "chave de administrador invalida"})
            if self.command == "GET" and caminho == "estado":
                agora = time.time()
                with relay.lock:
                    ags = list(relay.agentes.values())
                reg = relay.registro
                with reg.lock:
                    comps = [dict(c) for c in reg.d["computadores"]]
                    convs = [dict(c) for c in reg.d["convites"] if c["expira"] > agora]
                online = {a.cid: a for a in ags if a.vivo}
                saida = []
                for c in comps:
                    a = online.get(c["id"])
                    saida.append({"id": c["id"], "rotulo": c["rotulo"], "criado": c["criado"], "online": bool(a),
                                  "online_ha": int(agora - a.desde) if a else 0, "celulares": a.fluxos if a else 0})
                locais = [{"online_ha": int(agora - a.desde), "celulares": a.fluxos} for a in ags if a.vivo and not a.cid and not a.aberto]
                abertos = [{"sid": a.sid, "online_ha": int(agora - a.desde), "celulares": a.fluxos} for a in ags if a.vivo and a.aberto]
                return self._json(200, {"computadores": saida, "locais": locais, "abertos": abertos, "aberto": reg.aberto,
                                        "max": relay.max_sessoes, "publico": relay.publico,
                                        "convites": [{"id": c["id"], "rotulo": c["rotulo"], "expira": c["expira"]} for c in convs]})
            if self.command == "POST":
                d = self._corpo_json()
                if caminho == "convite":
                    return self._json(200, {"codigo": relay.registro.criar_convite(d.get("rotulo", "")), "publico": relay.publico})
                if caminho == "revogar":
                    ok = relay.registro.revogar(str(d.get("id", "")))
                    relay.desconectar(str(d.get("id", "")))
                    return self._json(200, {"ok": ok})
                if caminho == "revogar-convite":
                    return self._json(200, {"ok": relay.registro.revogar_convite(str(d.get("id", "")))})
                if caminho == "aberto":
                    relay.registro.definir_aberto(bool(d.get("valor")))
                    return self._json(200, {"aberto": relay.registro.aberto})
                if caminho == "derrubar":
                    return self._json(200, {"ok": relay.derrubar(str(d.get("sid", "")))})
                if caminho == "rotular":
                    return self._json(200, {"ok": relay.registro.rotular(str(d.get("id", "")), d.get("rotulo", ""))})
            return self._json(404, {"erro": "rota desconhecida"})

        def _agente_ws(self):
            chave = self.headers.get("X-Baton-Chave", "")
            if relay.limite(self._ip()):
                return self._texto(429, "muitas tentativas")
            cid, aberto = "", False
            if chave and not _igual(chave, relay.chave):  # chave de administrador (este servidor) ou de um computador convidado
                comp = relay.registro.autenticar(chave)
                if comp is None:
                    relay.falhou(self._ip())
                    time.sleep(1)
                    return self._texto(403, "chave do relay invalida ou revogada")
                cid = comp["id"]
            elif not chave:  # sem chave: registro aberto (Baton padrao, sem configurar), com limites
                ip = self._ip()
                if not relay.registro.aberto:
                    return self._texto(403, "este relay exige convite")
                if relay.abertos_do_ip(ip) >= relay.MAX_ABERTOS_POR_IP or not relay.vagas_abertas():
                    return self._texto(503, "relay aberto lotado; tente de novo em instantes")
                aberto = True
            k = self.headers.get("Sec-WebSocket-Key", "")
            if not k or "websocket" not in self.headers.get("Upgrade", "").lower():
                return self._texto(400, "WebSocket esperado")
            ws = WS(self.connection, False)
            ag = Agente(ws, cid)
            ag.aberto = aberto
            if aberto:
                ag.ip_chave = self._ip()
            if not relay.registrar(ag):
                return self._texto(503, "relay lotado")
            acc = base64.b64encode(hashlib.sha1((k + GUID).encode()).digest()).decode()
            self.wfile.write(("HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                              f"Sec-WebSocket-Accept: {acc}\r\n\r\n").encode())
            self.connection.settimeout(90)  # o agente manda ping a cada 20 s
            try:
                while True:
                    op, dados = ws.receber()
                    if op == 1:
                        m = json.loads(dados)
                        if m.get("t") == "anuncio":
                            with relay.lock:
                                ag.codigos = set(map(str, m.get("codigos", [])))
                                ag.tokens = set(map(str, m.get("tokens", [])))
                    elif op == 2 and len(dados) >= 5:
                        rid, tipo = struct.unpack(">IB", dados[:5])
                        f = ag.pend.get(rid)
                        if f is not None:
                            f.put((tipo, dados[5:]))
            except (OSError, ValueError):
                pass
            finally:
                relay.remover(ag)
            self.close_connection = True

        def _rotear(self):
            u = urlsplit(self.path)
            if u.path == "/relay/ws":
                return self._agente_ws()
            if u.path == "/relay/convite" and self.command == "POST":
                return self._resgatar()
            if u.path in ("/relay/painel", "/relay/painel/"):
                corpo = PAINEL_RELAY.encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(corpo)))
                self.end_headers()
                return self.wfile.write(corpo)
            if u.path.startswith("/relay/api/"):
                return self._api(u.path[len("/relay/api/"):])
            if u.path.startswith("/relay/"):
                return self._texto(404, "not found")
            q = parse_qs(u.query)
            token = q.get("k", [""])[0] or self.headers.get("X-Token", "") or _cookie(self.headers, "baton_k")
            codigo = u.path[3:].split("/")[0] if u.path.startswith("/p/") else ""
            est = u.path.startswith(ESTATICOS) or u.path == "/sw.js"
            ag = relay.achar(codigo, token, _cookie(self.headers, "baton_s"), est)
            if ag is None:
                return self._texto(404, "Sessao do Baton nao encontrada: o computador esta desligado, o QR expirou ou o celular nao foi pareado. Escaneie o QR atual no computador.")
            try:
                n = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                n = 0
            if n > MAX_CORPO:
                return self._texto(413, "corpo grande demais")
            corpo = self.rfile.read(n) if n else b""
            ip = self.headers.get("Cf-Connecting-Ip") or self.client_address[0]
            hs = [(k, v) for k, v in self.headers.items() if k.lower() not in HOP | {"x-forwarded-for", "x-real-ip", "content-length"}]
            hs.append(("X-Forwarded-For", ip))
            rid = ag.novo_id()
            fila = queue.Queue()
            with ag.lock:
                ag.pend[rid] = fila
            evento = u.path == "/events"
            if evento:
                ag.fluxos += 1
            try:
                ag.ws.enviar(2, quadro(rid, T_CAB, json.dumps({"m": self.command, "p": self.path, "h": hs}).encode()))
                for i in range(0, len(corpo), PEDACO):
                    ag.ws.enviar(2, quadro(rid, T_DADOS, corpo[i:i + PEDACO]))
                ag.ws.enviar(2, quadro(rid, T_FIM))
                self._responder(ag, rid, fila)
            except OSError:
                pass
            finally:
                if evento:
                    ag.fluxos = max(0, ag.fluxos - 1)
                with ag.lock:
                    ag.pend.pop(rid, None)

        def _responder(self, ag, rid, fila):
            comecou, html, janela = False, False, b""
            while True:
                try:
                    tipo, dados = fila.get(timeout=90 if comecou else 30)
                except queue.Empty:
                    if not comecou:
                        self._texto(504, "o Baton do computador nao respondeu")
                    break
                if tipo == "erro":
                    if not comecou:
                        self._texto(502, "o computador desconectou do relay")
                    break
                if tipo == T_CAB:
                    cab = json.loads(dados)
                    html = any(k.lower() == "content-type" and "html" in v.lower() for k, v in cab["h"])
                    self.send_response_only(int(cab["status"]))
                    for k, v in cab["h"]:
                        if k.lower() not in HOP:
                            self.send_header(k, v)
                    self.send_header("Set-Cookie", f"baton_s={ag.sid}; Path=/; Max-Age=31536000; SameSite=Lax; HttpOnly; Secure")
                    self.send_header("Connection", "close")
                    self.end_headers()
                    comecou = True
                elif tipo == T_DADOS and comecou:
                    if ag.aberto and html:  # o Baton nunca tem campo de senha: agente aberto servindo um e tentativa de phishing
                        janela = (janela + dados.lower())[-4096:]
                        if b"type=\"password\"" in janela or b"type=password" in janela or b"type='password'" in janela:
                            ag.cair()
                            break
                    try:
                        self.wfile.write(dados)
                        self.wfile.flush()
                    except OSError:  # celular saiu (ex.: fechou o SSE): o agente fecha a conexao local
                        try:
                            ag.ws.enviar(1, json.dumps({"t": "cancel", "id": rid}))
                        except OSError:
                            pass
                        break
                elif tipo == T_FIM:
                    break
            self.close_connection = True

        do_GET = do_POST = do_PUT = do_DELETE = do_HEAD = do_OPTIONS = _rotear

    return H


class ServidorRelay(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    request_queue_size = 128

    def handle_error(self, request, client_address):
        pass


def servir(porta, chave, max_sessoes=50, host="0.0.0.0", registro=None, publico=""):
    relay = Relay(chave, max_sessoes, registro, publico)
    srv = ServidorRelay((host, porta), criar_handler(relay))
    srv.relay = relay
    return srv


# ------------------------------------------------------------------ cliente de convite (linha de comando)

def _base_http(host):
    host = host.strip().rstrip("/")
    if host.startswith(("http://", "https://")):
        return host
    local = host.startswith(("localhost", "127.", "192.168.", "10.")) or ":" in host
    return ("http://" if local else "https://") + host


def resgatar_convite(texto, padrao=""):
    """`host/CODIGO` (ou so CODIGO com o relay padrao) -> (url wss do relay, chave do computador). Levanta ValueError/OSError."""
    texto = texto.strip()
    if "/" in texto.replace("://", ""):
        host, codigo = texto.rsplit("/", 1)
    else:
        host, codigo = padrao, texto
    if not host:
        raise ValueError("informe o convite completo (host/CODIGO) ou --relay")
    base = _base_http(host)
    import urllib.request
    import urllib.error
    req = urllib.request.Request(base + "/relay/convite", data=json.dumps({"codigo": codigo}).encode(), method="POST",
                                 headers={"Content-Type": "application/json"})
    try:
        r = json.loads(urllib.request.urlopen(req, timeout=15).read())
    except urllib.error.HTTPError as e:
        try:
            raise ValueError(json.loads(e.read()).get("erro", "convite recusado"))
        except ValueError as v:
            raise v
    return base.replace("https://", "wss://").replace("http://", "ws://"), r["chave"]


def criar_convite_local(porta, chave, rotulo=""):
    import urllib.request
    req = urllib.request.Request(f"http://127.0.0.1:{porta}/relay/api/convite", data=json.dumps({"rotulo": rotulo}).encode(), method="POST",
                                 headers={"Content-Type": "application/json", "X-Baton-Admin": chave})
    return json.loads(urllib.request.urlopen(req, timeout=10).read())


# ------------------------------------------------------------------ agente (dentro do Baton)

class AgenteRelay(threading.Thread):
    """Mantem a conexao de saida com o relay e repassa cada requisicao ao Baton local (127.0.0.1:porta)."""

    def __init__(self, url, chave, porta, anuncio_fn):
        super().__init__(daemon=True)
        self.url, self.chave, self.porta, self.anuncio_fn = url.rstrip("/"), chave, porta, anuncio_fn
        self.status = "conectando"
        self.reqs, self.conns, self.parar, self.ws_atual = {}, {}, False, None

    def desligar(self):
        self.parar = True
        self.status = "desligado"
        if self.ws_atual is not None:
            self.ws_atual.fechar()

    def run(self):
        espera = 1
        while not self.parar:
            try:
                self._sessao()
                espera = 1
            except Exception as e:
                if not self.parar:
                    self.status = f"desconectado ({type(e).__name__})"
            if self.parar:
                return
            time.sleep(espera)
            espera = min(30, espera * 2)

    def _conectar(self):
        u = urlsplit(self.url.replace("wss://", "https://").replace("ws://", "http://"))
        tls = u.scheme == "https"
        sock = socket.create_connection((u.hostname, u.port or (443 if tls else 80)), timeout=10)
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        if tls:
            sock = ssl.create_default_context().wrap_socket(sock, server_hostname=u.hostname)
        chave = base64.b64encode(os.urandom(16)).decode()
        host = u.hostname + (f":{u.port}" if u.port else "")
        sock.sendall((f"GET /relay/ws HTTP/1.1\r\nHost: {host}\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                      f"Sec-WebSocket-Key: {chave}\r\nSec-WebSocket-Version: 13\r\nX-Baton-Chave: {self.chave}\r\n\r\n").encode())
        buf = b""
        while b"\r\n\r\n" not in buf:
            d = sock.recv(4096)
            if not d:
                raise ConnectionError("relay fechou")
            buf += d
        cab, resto = buf.split(b"\r\n\r\n", 1)
        if b" 101 " not in cab.split(b"\r\n")[0]:
            raise ConnectionError("relay recusou: " + cab.split(b"\r\n")[0].decode("latin-1"))
        acc = base64.b64encode(hashlib.sha1((chave + GUID).encode()).digest()).decode()
        if acc.encode() not in cab:
            raise ConnectionError("handshake invalido")
        sock.settimeout(70)  # o agente manda ping a cada 20 s e o relay responde: 70 s sem nada = caminho morto (rede trocou, PC dormiu): reconecta
        return WS(sock, True, resto)

    def _sessao(self):
        ws = self._conectar()
        self.ws_atual = ws
        if self.parar:
            ws.fechar()
            return
        self.status = "conectado"
        vivo = threading.Event()

        def pulso():
            ultimo = None
            t0 = time.time()
            while not vivo.is_set():
                try:
                    cod, toks = self.anuncio_fn()
                    atual = (tuple(sorted(cod)), tuple(sorted(toks)))
                    if atual != ultimo:
                        ws.enviar(1, json.dumps({"t": "anuncio", "codigos": list(cod), "tokens": list(toks)}))
                        ultimo = atual
                    if time.time() - t0 > 20:
                        ws.enviar(0x9, b"")
                        t0 = time.time()
                except OSError:
                    ws.fechar()
                    return
                vivo.wait(0.25)  # codigo/token novos chegam ao relay em ate 0,25 s (QR escaneado e logo usado)

        threading.Thread(target=pulso, daemon=True).start()
        try:
            while True:
                op, dados = ws.receber()
                if op == 1:
                    m = json.loads(dados)
                    if m.get("t") == "cancel":
                        c = self.conns.get(m.get("id"))
                        if c is not None:
                            try:
                                c.close()
                            except OSError:
                                pass
                elif op == 2 and len(dados) >= 5:
                    rid, tipo = struct.unpack(">IB", dados[:5])
                    if tipo == T_CAB:
                        self.reqs[rid] = {"cab": json.loads(dados[5:]), "corpo": []}
                    elif tipo == T_DADOS and rid in self.reqs:
                        self.reqs[rid]["corpo"].append(dados[5:])
                    elif tipo == T_FIM and rid in self.reqs:
                        r = self.reqs.pop(rid)
                        threading.Thread(target=self._servir, args=(ws, rid, r["cab"], b"".join(r["corpo"])), daemon=True).start()
        finally:
            vivo.set()
            ws.fechar()
            self.reqs.clear()
            for c in list(self.conns.values()):
                try:
                    c.close()
                except OSError:
                    pass

    def _servir(self, ws, rid, cab, corpo):
        enviou = False
        conn = None
        try:
            conn = http.client.HTTPConnection("127.0.0.1", self.porta, timeout=120)
            self.conns[rid] = conn
            conn.request(cab["m"], cab["p"], body=corpo or None, headers=dict(cab["h"]))
            r = conn.getresponse()
            ws.enviar(2, quadro(rid, T_CAB, json.dumps({"status": r.status, "h": r.getheaders()}).encode()))
            enviou = True
            while True:
                d = r.read1(PEDACO)
                if not d:
                    break
                ws.enviar(2, quadro(rid, T_DADOS, d))
        except (OSError, http.client.HTTPException):
            if not enviou:
                try:
                    ws.enviar(2, quadro(rid, T_CAB, json.dumps({"status": 502, "h": [["Content-Type", "text/plain"]]}).encode()))
                    ws.enviar(2, quadro(rid, T_DADOS, b"Baton local indisponivel"))
                except OSError:
                    pass
        finally:
            self.conns.pop(rid, None)
            try:
                ws.enviar(2, quadro(rid, T_FIM))
            except OSError:
                pass
            if conn is not None:
                try:
                    conn.close()
                except OSError:
                    pass
