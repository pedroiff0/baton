#!/usr/bin/env python3
"""baton: controle de slides em PDF pelo celular, via Tailscale, com laser.

Dois modos (no Mac que projeta):
    python3 baton.py slides.pdf             # a tela do Baton mostra o PDF (fade/slide/zoom)
    python3 baton.py --externo [slides.pdf] # controla QUALQUER leitor (Adobe, Preview, Keynote...)

No modo externo o celular envia teclas (setas, Home, End, B) e move o cursor do mouse (laser)
para o aplicativo em primeiro plano; o Terminal precisa de permissao de Acessibilidade.
A pagina inicial mostra um QR code para abrir o controle no celular.
Somente biblioteca padrao do Python 3.8+. pdf.js e gerador de QR sao baixados uma vez para
~/.cache/baton; sem internet nessa hora, o navegador usa a CDN.
"""
import argparse
import collections
import functools
import hashlib
import json
import math
import os
import queue
import random
import platform
import re
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import struct
import time
import urllib.request
import wave
import zlib
import signal
import http.cookies
import uuid
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import monitores
import relay as relay_mod
import residente
from urllib.parse import parse_qs, urlparse

PDFJS_VERSION = "3.11.174"
PDFJS_CDN = f"https://cdnjs.cloudflare.com/ajax/libs/pdf.js/{PDFJS_VERSION}/"
PDFJS_FILES = ("pdf.min.js", "pdf.worker.min.js")
def _migrar_cache():
    """Renomeado de passador para baton: leva token, configuracao e cache junto."""
    velho, novo = Path.home() / ".cache" / "passador", Path.home() / ".cache" / "baton"
    if velho.exists() and not novo.exists():
        try:
            velho.rename(novo)
        except OSError:
            pass


_migrar_cache()
CACHE = Path.home() / ".cache" / "baton" / f"pdfjs-{PDFJS_VERSION}"
QR_CDN = "https://cdnjs.cloudflare.com/ajax/libs/qrcode-generator/1.4.4/qrcode.min.js"
QR_FILE = Path.home() / ".cache" / "baton" / "qrcode.min.js"
VENDOR = Path(__file__).resolve().parent / "vendor"  # copia embutida (pendrive/offline): tem prioridade sobre ~/.cache


def pasta_pdfjs():
    return VENDOR / "pdfjs" if VENDOR.is_dir() else CACHE


def arquivo_qr():
    return VENDOR / "qrcode.min.js" if VENDOR.is_dir() else QR_FILE


TRANSICOES = ("fade", "slide", "zoom", "nenhuma")


CFG_PADRAO = {"laser_tam": 24, "laser_cor": "#ff2d2d", "foco_raio": 12, "foco_blur": 100, "foco_escuro": 25,
              "pen_larg": 3.5, "hl_larg": 14, "hl_alpha": 38, "meta_min": 50}
CFG_LIM = {"laser_tam": (6, 120), "foco_raio": (3, 35), "foco_blur": (0, 100), "foco_escuro": (0, 90),
           "pen_larg": (0.5, 20), "hl_larg": (3, 60), "hl_alpha": (5, 90), "meta_min": (1, 300)}
DISPOSITIVOS_ARQ = Path.home() / ".cache" / "baton" / "dispositivos.json"
MAX_DISPOSITIVOS = 8
PAREAMENTO_VALIDADE = 600  # segundos
NOTAS_ARQ = Path.home() / ".cache" / "baton" / "notas-sessao.json"
CFG_ARQ = Path.home() / ".cache" / "baton" / "config.json"


def cfg_validar(d, base=None):
    """Mantem so chaves conhecidas, limita os numeros e valida a cor."""
    cfg = dict(base or CFG_PADRAO)
    for k, (lo, hi) in CFG_LIM.items():
        if k in d:
            try:
                cfg[k] = max(lo, min(hi, float(d[k])))
            except (TypeError, ValueError):
                pass
    cor = d.get("laser_cor")
    if isinstance(cor, str) and re.fullmatch(r"#[0-9a-fA-F]{6}", cor):
        cfg["laser_cor"] = cor
    return cfg


def cfg_carregar():
    try:
        return cfg_validar(json.loads(CFG_ARQ.read_text(encoding="utf-8")))
    except (OSError, ValueError):
        return dict(CFG_PADRAO)


# ------------------------------------------------------------------ efeitos sonoros (sintetizados, sem arquivos)
EFEITOS = ("ding", "tambor", "aplausos", "fanfarra", "buzina", "suspense")
CORTINAS = ("preto", "branco", "teatro", "fim")
TAXA_SOM = 22050
SONS_DIR = Path.home() / ".cache" / "baton" / "sons"


def _mixar(trilhas):
    n = max(len(x) for x in trilhas)
    return [sum(x[i] for x in trilhas if i < len(x)) for i in range(n)]


def _nota(freq, dur, decay=3.0, harm=(1.0, 0.5, 0.25, 0.12)):
    n = int(TAXA_SOM * dur)
    out = []
    for i in range(n):
        t = i / TAXA_SOM
        env = min(1.0, t / 0.008) * math.exp(-t * decay) * min(1.0, (dur - t) / 0.03)
        out.append(env * sum(a * math.sin(2 * math.pi * freq * (k + 1) * t) for k, a in enumerate(harm)))
    return out


def _som_ding():
    out = []
    for i in range(int(TAXA_SOM * 1.6)):
        t = i / TAXA_SOM
        out.append(math.exp(-t * 3.2) * (math.sin(2 * math.pi * 880 * t) + 0.35 * math.sin(2 * math.pi * 2217 * t)
                                         + 0.15 * math.sin(2 * math.pi * 3520 * t)))
    return out


def _som_buzina():
    out = []
    for i in range(int(TAXA_SOM * 0.9)):
        t = i / TAXA_SOM
        env = min(1.0, t / 0.02) * min(1.0, (0.9 - t) / 0.08)
        v = (1.0 if math.sin(2 * math.pi * 140 * t) >= 0 else -1.0) + (1.0 if math.sin(2 * math.pi * 147 * t) >= 0 else -1.0)
        out.append(0.4 * env * v)
    return out


def _som_fanfarra():
    seq = []
    for f in (523.25, 659.25, 783.99):
        seq += _nota(f, 0.15, decay=3.0)
    return seq + _mixar([_nota(f, 1.4, decay=1.8) for f in (523.25, 659.25, 783.99, 1046.5)])


def _som_suspense():
    out, fase = [], 0.0
    dur = 2.6
    for i in range(int(TAXA_SOM * dur)):
        t = i / TAXA_SOM
        f = 110 + 330 * (t / dur) ** 2
        fase += 2 * math.pi * f / TAXA_SOM
        trem = 0.65 + 0.35 * math.sin(2 * math.pi * (4 + 10 * t / dur) * t)
        env = min(1.0, t / 0.2) * (0.25 + 0.75 * t / dur) * min(1.0, (dur - t) / 0.05)
        out.append(env * trem * (math.sin(fase) + 0.4 * math.sin(2 * fase)))
    return out


def _som_tambor():
    rnd = random.Random(7)
    out = [0.0] * int(TAXA_SOM * 3.6)
    t = 0.0
    while t < 2.0:
        ini, amp = int(t * TAXA_SOM), 0.35 + 0.45 * t / 2.0
        for i in range(int(TAXA_SOM * 0.07)):
            out[ini + i] += amp * math.exp(-i / (TAXA_SOM * 0.016)) * (0.6 * rnd.uniform(-1, 1)
                                                                       + 0.8 * math.sin(2 * math.pi * 140 * i / TAXA_SOM))
        t += 0.11 * (1 - 0.6 * t / 2.0)
    ini, ant = int(2.0 * TAXA_SOM), 0.0
    for i in range(len(out) - ini):
        r = rnd.uniform(-1, 1)
        out[ini + i] += 0.6 * (r - ant) * math.exp(-i / (TAXA_SOM * 0.7))
        ant = r
    return out


def _som_aplausos():
    rnd = random.Random(11)
    dur = 3.4
    out = [0.0] * int(TAXA_SOM * dur)
    for _ in range(int(dur * 330)):
        t = rnd.random() * dur
        if rnd.random() > min(1.0, t / 0.5) * min(1.0, (dur - t) / 1.2):
            continue
        ini, amp, tau = int(t * TAXA_SOM), rnd.uniform(0.3, 1.0), TAXA_SOM * rnd.uniform(0.002, 0.006)
        for i in range(int(TAXA_SOM * 0.03)):
            if ini + i < len(out):
                out[ini + i] += amp * math.exp(-i / tau) * rnd.uniform(-1, 1)
    return out


_GERADORES = {"ding": _som_ding, "tambor": _som_tambor, "aplausos": _som_aplausos, "fanfarra": _som_fanfarra,
              "buzina": _som_buzina, "suspense": _som_suspense}


def wav_bytes(nome):
    """Sintetiza o efeito e devolve um WAV mono de 16 bits."""
    amostras = _GERADORES[nome]()
    pico = max(1e-9, max(abs(x) for x in amostras))
    pcm = struct.pack("<%dh" % len(amostras), *[int(max(-1.0, min(1.0, x / pico * 0.85)) * 32767) for x in amostras])
    import io
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(TAXA_SOM)
        w.writeframes(pcm)
    return buf.getvalue()


class Som:
    """Toca os efeitos no Mac que projeta (e sai nas caixas da apresentacao). 'log' so imprime (teste)."""

    def __init__(self, modo=None):
        self.modo = modo if modo is not None else os.environ.get("BATON_SOM", "")
        self.cmd = None
        self.proc = None
        self.lock = threading.Lock()
        self.win = sys.platform == "win32" and self.modo != "log"
        if self.modo != "log" and not self.win:
            for exe in (("afplay",), ("paplay",), ("aplay", "-q"), ("ffplay", "-nodisp", "-autoexit", "-loglevel", "quiet")):
                if shutil.which(exe[0]):
                    self.cmd = list(exe)
                    break

    def arquivo(self, nome):
        destino = SONS_DIR / f"{nome}.wav"
        if not destino.exists() or destino.stat().st_size < 1000:
            SONS_DIR.mkdir(parents=True, exist_ok=True)
            destino.write_bytes(wav_bytes(nome))
        return destino

    def preparar(self):
        """Gera os arquivos em segundo plano, para o primeiro toque sair sem atraso."""
        if self.cmd or self.win:
            threading.Thread(target=lambda: [self.arquivo(n) for n in EFEITOS], daemon=True).start()

    def parar(self):
        if self.win:
            import winsound
            winsound.PlaySound(None, winsound.SND_PURGE)
            return
        with self.lock:
            if self.proc and self.proc.poll() is None:
                self.proc.terminate()
            self.proc = None

    def tocar(self, nome):
        if nome == "stop":
            self.parar()
            if self.modo == "log":
                print("som: stop", flush=True)
            return True
        if nome not in EFEITOS:
            return False
        if self.modo == "log":
            print("som:", nome, flush=True)
            return True
        if not self.cmd and not self.win:
            return False

        def _tocar():
            try:
                arq = str(self.arquivo(nome))
                if self.win:
                    import winsound
                    winsound.PlaySound(arq, winsound.SND_FILENAME | winsound.SND_ASYNC)
                    return
                self.parar()
                with self.lock:
                    self.proc = subprocess.Popen(self.cmd + [arq], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            except Exception as e:
                print(f"aviso: nao toquei {nome} ({e})", file=sys.stderr)
        threading.Thread(target=_tocar, daemon=True).start()
        return True


class Estado:
    def __init__(self, pdf, total, transicao, token, externo=None, remote_url="", proporcao=16 / 9):
        self.pdf = pdf
        self.total = total
        self.token = token
        self.externo = externo
        self.remote_url = remote_url
        self.local_url = ""
        self.tls_porta = 0  # porta HTTPS (sensores do celular exigem contexto seguro); 0 = desligada
        self.captura = True  # previa da tela do computador para o celular quando nao ha PDF (--sem-captura desliga)
        self.tela_desde = 0.0
        self.tela_propria, self.tela_proc = False, None  # tela cheia do proprio Baton (PDF carregado pelo painel)
        self._bases_fixas, self.bases_fn, self._bases_cache = [], None, (0.0, [])  # enderecos do computador (ver calcular_bases)
        self.dispositivos = []  # celulares pareados: {id, nome, token, criado, ultimo_visto, ua}
        self.monitor_cli = ""  # --monitor N (sobrepoe a preferencia do painel)
        self._mon_cache = (0.0, None)
        self.porta_local, self._url_derivada = 8765, ""
        self.laser_seq = {}  # (token, id da pagina) -> ultima sequencia de /laser aceita
        self.relay = None  # AgenteRelay (sessao no relay central), se configurado
        self.pareamento = None  # {"codigo", "expira"}: QR de uso unico
        self._salvo_em = 0.0
        self.falhas_pareamento = {}  # ip -> instantes de codigos errados (limite por minuto)
        self.proporcao = proporcao
        self.remotos = 0
        self.zoom, self.zx, self.zy = 1.0, 0.5, 0.5
        self.seq = 0
        self.vistos = collections.deque(maxlen=400)
        self.t_rodando, self.t_base, self.t_desde = False, 0.0, 0.0  # cronometro da apresentacao
        self.voltas = []  # tempos (s) marcados no cronometro
        self.tracos = []  # tracos da caneta/marca-texto do slide atual
        self.notas = {}
        self.notas_id = "livre"  # sessao das notas digitadas no celular (por apresentacao)
        self._edits, self._base_vazia = {}, False
        self.cfg = cfg_carregar()
        self.pagina = 1
        self.transicao = transicao
        self.cortina = ""
        self.fim_txt = ("Obrigado!", "")
        self.som = Som()
        self.residente = False
        self.autorizado = True  # modo residente comeca em espera ate alguem aceitar
        self.pedido = None
        self.sem_perguntar = None  # app que o usuario recusou nesta tela cheia
        self.prefs = residente.prefs_carregar()
        self.lock = threading.Lock()
        self.clientes = []

    def decorrido(self):
        return self.t_base + ((time.time() - self.t_desde) if self.t_rodando else 0.0)

    def _timer(self, acao):
        agora = time.time()
        if acao in ("start", "toggle") and not self.t_rodando:
            self.t_desde, self.t_rodando = agora, True
        elif acao in ("pause", "toggle") and self.t_rodando:
            self.t_base, self.t_rodando = self.decorrido(), False
        elif acao == "lap":
            if (self.t_rodando or self.t_base > 0) and len(self.voltas) < 60:
                self.voltas.append(round(self.decorrido(), 1))
        elif acao == "reset":
            self.t_base, self.t_rodando, self.voltas = 0.0, False, []

    def snapshot(self):
        return {"pagina": self.pagina, "total": self.total, "transicao": self.transicao, "preto": self.cortina == "preto",
                "cortina": self.cortina, "fim": {"txt": self.fim_txt[0], "sub": self.fim_txt[1]},
                "auth": self.autorizado, "ativo": self.prefs["ativo"], "pedido": self.pedido,
                "acess": self.acessibilidade(), "so": sys.platform,
                "tela_falha": bool(self.tela_propria and not self.tela_conectada() and time.time() - self.tela_desde > 8),
                "zoom_sistema": ("Lupa" if self.ext.overlay else (zoom_sistema_cfg() or {}).get("nome")) if self.ext else None,
                "aspecto": self.aspecto(),
                "remotos": self.remotos, "zoom": self.zoom, "zx": self.zx, "zy": self.zy, "seq": self.seq,
                "timer": {"rodando": self.t_rodando, "elapsed": round(self.decorrido(), 1), "voltas": self.voltas}}

    def publicar(self, msg):
        pesado = "laser" in msg or "draw" in msg  # so a tela local desenha; poupa a banda do celular
        for q in list(self.clientes):
            if pesado and getattr(q, "dispositivo", None) is not None:
                continue
            try:
                q.put_nowait(msg)
            except queue.Full:
                pass

    # ---- modo residente: pausa, consentimento e deteccao de apresentacao
    def tela_conectada(self):
        """True se a pagina de tela cheia do proprio Baton (computador, sem token de celular) esta com o SSE aberto."""
        return any(getattr(q, "dispositivo", 0) is None for q in list(self.clientes))

    def _tela_ativa(self):
        return self.tela_propria and self.tela_conectada()

    def permitido(self):
        return self.prefs["ativo"] and (self.autorizado or self._tela_ativa())

    @property
    def ext(self):
        """Backend de teclas do app em primeiro plano; fica desligado so enquanto a tela cheia do proprio Baton esta aberta E
        conectada (senao a tecla iria tambem para o navegador e o slide avancaria duas vezes; se a janela foi fechada, as
        teclas voltam a ir para o app)."""
        return None if self._tela_ativa() else self.externo

    def autorizar_por_toque(self):
        """Tocar em Proximo/Anterior num celular pareado e consentimento explicito: libera o controle sem exigir o
        'Controlar mesmo assim'. Nao vale se o Baton esta pausado, em modo 'nunca' ou o app esta na lista de bloqueados."""
        app = (self.pedido or {}).get("app")
        if not self.prefs["ativo"] or self.prefs["modo"] == "nunca" or (app and app in self.prefs["apps_nao"]):
            return False
        self.autorizado, self.pedido = True, None
        self._mudou()
        return True

    def apresentar(self):
        """Abre o PDF carregado em tela cheia neste computador (navegador em modo quiosque) e passa a controla-lo."""
        if not self.pdf:
            return False
        self.fechar_tela(avisar=False)
        url = (self.local_url or "http://localhost:8765/") + "apresentar"
        proc = abrir_tela_cheia(url, self.monitor())
        with self.lock:
            self.tela_propria, self.tela_proc = True, proc
            self.tela_desde = time.time()
        if proc is not None:
            def _vigia():
                proc.wait()
                if self.tela_proc is proc:
                    self.fechar_tela()
            threading.Thread(target=_vigia, daemon=True).start()
        self._mudou()
        self.publicar({"deck": 1})  # o celular recarrega e ganha laser, caneta e zoom
        return True

    def fechar_tela(self, avisar=True):
        proc, self.tela_proc = self.tela_proc, None
        if proc is not None and proc.poll() is None:
            try:
                if os.name == "posix":  # o navegador abre varios processos: encerra o grupo todo
                    os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
                else:
                    proc.terminate()
            except Exception:
                try:
                    proc.terminate()
                except OSError:
                    pass
        if self.tela_propria:
            self.tela_propria = False
            if avisar:
                self._mudou()
                self.publicar({"deck": 1})

    def tela_tamanho(self):
        try:
            return self.externo.b.tela()
        except Exception:
            return (1920.0, 1080.0)

    def recarregar_prefs(self):
        novo = residente.prefs_carregar()
        if novo != self.prefs:
            self.prefs = novo
            self._mudou()

    def _mudou(self):
        with self.lock:
            self.seq += 1
            snap = self.snapshot()
        self.publicar(snap)

    def _salvar_prefs(self):
        residente.prefs_salvar(self.prefs)
        self._mudou()

    def presentacao(self, app):
        """Chamado pelo vigia: app e o programa em tela cheia, ou None quando ela termina."""
        if app is None:
            self.pedido, self.sem_perguntar = None, None
            if self.residente and self.prefs["modo"] != "sempre":
                self.autorizado = False
            return self._mudou()
        if residente.ignorado(app) or app in self.prefs["apps_nao"] or self.prefs["modo"] == "nunca" or app == self.sem_perguntar:
            return
        if self.prefs["modo"] == "sempre" or app in self.prefs["apps_ok"]:
            self.autorizado, self.pedido = True, None
            return self._mudou()
        self.pedido = {"app": app}
        self._mudou()
        if not self.remotos:
            residente.notificar("Baton", f"Apresentacao detectada em {app}. Clique para abrir o Baton (QR code) ou abra no celular: {self.url_pareamento()}",
                                self.local_url or None)

    def consentir(self, acao):
        app = (self.pedido or {}).get("app")
        if acao in ("sim", "sempre", "manual", "autorizar"):
            self.autorizado, self.pedido = True, None
            if acao == "sempre" and app and app not in self.prefs["apps_ok"]:
                self.prefs["apps_ok"].append(app)
                self.prefs["apps_nao"] = [a for a in self.prefs["apps_nao"] if a != app]
        elif acao == "nao":
            self.sem_perguntar, self.pedido = app, None
        elif acao == "nunca":
            if app and app not in self.prefs["apps_nao"]:
                self.prefs["apps_nao"].append(app)
            self.sem_perguntar, self.pedido = app, None
        elif acao == "revogar":
            self.autorizado = False
        else:
            return False
        self._salvar_prefs()
        return True

    def painel(self, acao, valor=None):
        """Acoes da pagina /painel (so no proprio computador)."""
        p = self.prefs
        if acao == "pausar":
            p["ativo"] = False
            self.autorizado = not self.residente and self.autorizado
        elif acao == "retomar":
            p["ativo"] = True
        elif acao == "modo" and valor in residente.MODOS:
            p["modo"] = valor
        elif acao == "relay":
            p["relay_publico"] = bool(valor)
            if valor:
                self.relay_iniciar(RELAY_PADRAO)
            else:
                self.relay_parar()
        elif acao == "monitor" and (valor == "auto" or str(valor).isdigit()):
            p["monitor"] = str(valor)
            self._monitor_atualizar()
        elif acao == "esquecer":
            p["apps_ok"] = [a for a in p["apps_ok"] if a != valor]
            p["apps_nao"] = [a for a in p["apps_nao"] if a != valor]
        elif acao in ("autorizar", "revogar"):
            return self.consentir(acao)
        elif acao == "abrir_acessibilidade":
            threading.Thread(target=pedir_acessibilidade, daemon=True).start()
            return True
        elif acao == "testar_tecla":
            def _testar():  # 5 s para trazer o app (ex.: ReLaTeX em tela cheia) ao primeiro plano
                self.externo.b.tecla("Right")
                time.sleep(0.6)
                self.externo.b.tecla("Left")
            if self.externo:
                threading.Timer(5.0, _testar).start()
            return True
        elif acao == "remover_deck":
            self.remover_deck()
            return True
        elif acao == "apresentar":
            return self.apresentar()
        elif acao == "fechar_tela":
            self.fechar_tela()
            return True
        elif acao == "parear_novo":
            self.novo_pareamento()
            return True
        elif acao == "revogar_celular":
            return self.revogar(str(valor))
        elif acao == "instalar":
            instalar_login()
        elif acao == "desinstalar":
            instalar_login(remover=True)
        else:
            return False
        self._salvar_prefs()
        return True

    def acessibilidade(self):
        return acessibilidade_mac() if self.ext else None

    def relay_iniciar(self, url, chave=""):
        global RELAY_AGENTE, URL_PUBLICA
        if self.relay is not None and getattr(self.relay, "status", "") != "desligado":
            return
        self.relay = relay_mod.AgenteRelay(url, chave, self.porta_local, lambda: (
            [self.codigo_pareamento()], [relay_mod.hash_token(d["token"]) for d in list(self.dispositivos)]))
        self.relay.start()
        RELAY_AGENTE = self.relay
        if not URL_PUBLICA and "127.0.0.1" not in url and "localhost" not in url:
            URL_PUBLICA = url.replace("wss://", "https://").replace("ws://", "http://").rstrip("/")
            self._url_derivada = URL_PUBLICA

    def relay_parar(self):
        global RELAY_AGENTE, URL_PUBLICA
        if self.relay is not None:
            self.relay.desligar()
        RELAY_AGENTE = None
        if self._url_derivada and URL_PUBLICA == self._url_derivada:
            URL_PUBLICA = ""
            self._url_derivada = ""

    def aspecto(self):
        """Largura/altura da area de apontar: o monitor da projecao no modo externo, senao o slide."""
        m = self._mon_cache[1] if self.ext else None
        if m and m.get("h"):
            return round(m["w"] / m["h"], 4)
        return round(float(self.proporcao), 4)

    def monitor_pref(self):
        return self.monitor_cli or str(self.prefs.get("monitor", "auto"))

    def monitor(self):
        """Monitor da projecao (dict x, y, w, h): o da janela em tela cheia, ou o escolhido no painel. Caminho quente (laser):
        so le o valor guardado; quem recalcula (osascript/xrandr/xdotool, 100 a 300 ms no Mac) e a thread de monitor_vigiar()."""
        if self._mon_cache[1] is None:
            self._monitor_atualizar()
        return self._mon_cache[1]

    def _monitor_atualizar(self):
        try:
            m = monitores.escolher(self.monitor_pref(), anterior=self._mon_cache[1])
        except Exception:
            m = self._mon_cache[1]
        ant = self._mon_cache[1]
        self._mon_cache = (time.monotonic(), m)
        chave = lambda d: (d["x"], d["y"], d["w"], d["h"]) if d else None
        if self.ext and chave(ant) != chave(m):
            self.publicar(self.snapshot())  # celulares ajustam o formato da area de apontar

    def monitor_vigiar(self, intervalo=2.0):
        def _loop():
            while True:
                time.sleep(intervalo)
                self._monitor_atualizar()
        threading.Thread(target=_loop, daemon=True).start()

    def painel_estado(self):
        return {"ativo": self.prefs["ativo"], "modo": self.prefs["modo"], "autorizado": self.autorizado,
                "pedido": self.pedido, "apps_ok": self.prefs["apps_ok"], "apps_nao": self.prefs["apps_nao"],
                "residente": self.residente, "acessibilidade": self.acessibilidade(), "deck": self.deck_info(), "tela_propria": self.tela_propria, "tela_conectada": self.tela_conectada(), "captura": self.captura,
                "conexoes": len(self.conectados()), "dispositivos": self.dispositivos_info(),
                "plataforma": sys.platform, "rede_bluetooth": self.rede_bluetooth(),
                "pareamento_urls": self.urls_pareamento(), "relay": getattr(self.relay, "status", ""), "relay_publico": bool(self.prefs.get("relay_publico", True)), "relay_url": RELAY_PADRAO,
                "monitores": monitores.listar(), "monitor_pref": self.monitor_pref(), "monitor_ativo": (self.monitor() or {}).get("id")}

    def comando(self, cmd, valor=None, cid=None):
        if not self.permitido():
            return False
        with self.lock:
            if cid:
                if cid in self.vistos:  # repeticao de um comando que o celular reenviou por timeout
                    return True
                self.vistos.append(cid)
            pagina_antes = self.pagina
            if cmd == "next":
                self.pagina = min(self.total, self.pagina + 1)
            elif cmd == "prev":
                self.pagina = max(1, self.pagina - 1)
            elif cmd == "first":
                self.pagina = 1
            elif cmd == "last":
                self.pagina = self.total
            elif cmd == "goto":
                self.pagina = max(1, min(self.total, int(valor)))
            elif cmd == "transition" and valor in TRANSICOES:
                self.transicao = valor
            elif cmd == "blank":
                self.cortina = "" if self.cortina == "preto" else "preto"
            elif cmd == "cortina":
                k = valor.get("k") if isinstance(valor, dict) else valor
                if k not in CORTINAS and k != "off":
                    return False
                if isinstance(valor, dict) and ("txt" in valor or "sub" in valor):
                    self.fim_txt = (str(valor.get("txt", ""))[:80], str(valor.get("sub", ""))[:120])
                self.cortina = "" if (k == "off" or k == self.cortina) else k
            elif cmd == "confete":
                seg = max(1.0, min(10.0, float(valor or 4)))
            elif cmd == "efeito":
                if valor != "stop" and valor not in EFEITOS:
                    return False
            elif cmd == "limpar":
                pass
            elif cmd == "timer":
                self._timer(str(valor))
            elif cmd == "zoom":
                z = float(valor.get("z", 1))
                self.zoom = max(1.0, min(5.0, z))
                self.zx = max(0.0, min(1.0, float(valor.get("x", 0.5))))
                self.zy = max(0.0, min(1.0, float(valor.get("y", 0.5))))
                if self.ext:  # app de terceiros: usa o zoom nativo do sistema
                    self.ext.zoom_externo(self.zoom, self.zx, self.zy, self.proporcao)
            else:
                return False
            if self.pagina != pagina_antes:
                if self.ext and self.zoom > 1.001:
                    self.ext.zoom_externo(1.0, 0.5, 0.5, self.proporcao)
                self.zoom, self.zx, self.zy = 1.0, 0.5, 0.5
                self.tracos = []
                if not self.t_rodando and self.t_base == 0.0:  # o cronometro comeca no primeiro avanco
                    self._timer("start")
            if cmd == "limpar":
                self.tracos = []
            self.seq += 1
            snap = self.snapshot()
        if self.ext:
            if cmd == "goto" and self.pagina != pagina_antes:
                self.ext.ir(self.pagina)
            elif cmd in ("blank", "cortina"):
                self.ext.cortina(snap["cortina"], snap["fim"]["txt"], snap["fim"]["sub"])
            elif cmd == "confete":
                self.ext.confete(seg)
            elif cmd not in ("zoom", "limpar", "timer", "goto", "efeito"):
                self.ext.comando(cmd)
            if cmd == "limpar" or self.pagina != pagina_antes:
                self.ext.limpar()
        if cmd == "efeito":
            self.som.tocar(valor)
        if cmd == "confete":
            self.publicar({"confete": seg})
        if cmd == "limpar":
            self.publicar({"limpar": 1})
        self.publicar(snap)
        return True

    def carregar_notas_sessao(self, base):
        """Notas do arquivo/pptx (base) + o que foi digitado no celular nesta apresentacao (salvo em disco)."""
        self._base = dict(base)
        try:
            d = json.loads(NOTAS_ARQ.read_text(encoding="utf-8")).get(self.notas_id, {})
            self._edits = {str(k): str(v) for k, v in d.get("notas", {}).items()}
            self._base_vazia = bool(d.get("limpar_base"))
        except (OSError, ValueError, AttributeError):
            pass
        self._notas_efetivas()

    def _notas_efetivas(self):
        base = {} if self._base_vazia else getattr(self, "_base", {})
        self.notas = {**base, **{k: v for k, v in self._edits.items() if v}}
        for k, v in self._edits.items():
            if not v:
                self.notas.pop(k, None)  # texto apagado no celular esconde a nota do arquivo

    def _persistir_notas(self):
        try:
            todos = json.loads(NOTAS_ARQ.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            todos = {}
        todos[self.notas_id] = {"notas": self._edits, "limpar_base": self._base_vazia}
        try:
            NOTAS_ARQ.parent.mkdir(parents=True, exist_ok=True)
            NOTAS_ARQ.write_text(json.dumps(todos, ensure_ascii=False), encoding="utf-8")
        except OSError:
            pass

    def salvar_nota(self, chave, texto):
        """Grava a nota de um slide digitada no celular; devolve as notas atuais (ou None se a chave for invalida)."""
        if not re.fullmatch(r"p?\d{1,5}", str(chave)):
            return None
        with self.lock:
            chave = chave if str(chave).startswith("p") else "p" + str(chave)
            self._edits[chave] = str(texto)[:6000].strip()
            self._notas_efetivas()
            self._persistir_notas()
            return dict(self.notas)

    def salvar_todas(self, texto):
        """Substitui todas as notas por um Markdown colado no celular ('## 1', '## p12'...)."""
        with self.lock:
            self._edits = {k: v[:6000] for k, v in notas_de_texto(str(texto)[:200000]).items()}
            self._base_vazia = True
            self._notas_efetivas()
            self._persistir_notas()
            return dict(self.notas)

    # ---- celulares: pareamento por QR de uso unico e um token por dispositivo
    def carregar_dispositivos(self):
        try:
            d = json.loads(DISPOSITIVOS_ARQ.read_text(encoding="utf-8"))
            self.dispositivos = [x for x in d if isinstance(x, dict) and x.get("token") and x.get("id")][:MAX_DISPOSITIVOS]
        except (OSError, ValueError, TypeError):
            self.dispositivos = []

    def _salvar_dispositivos(self):
        try:
            DISPOSITIVOS_ARQ.parent.mkdir(parents=True, exist_ok=True)
            DISPOSITIVOS_ARQ.write_text(json.dumps(self.dispositivos, ensure_ascii=False), encoding="utf-8")
            os.chmod(DISPOSITIVOS_ARQ, 0o600)
        except OSError:
            pass
        self._salvo_em = time.time()

    def novo_pareamento(self):
        self.pareamento = {"codigo": secrets.token_urlsafe(12), "expira": time.time() + PAREAMENTO_VALIDADE}
        return self.pareamento["codigo"]

    def codigo_pareamento(self):
        if not self.pareamento or self.pareamento["expira"] < time.time():
            self.novo_pareamento()
        return self.pareamento["codigo"]

    def parear(self, codigo, ua=""):
        """Consome o codigo (uso unico) e cria o dispositivo; devolve o dispositivo ou None se o codigo nao vale."""
        with self.lock:
            p = self.pareamento
            if not p or p["expira"] < time.time() or not secrets.compare_digest(str(codigo), p["codigo"]):
                return None
            if len(self.dispositivos) >= MAX_DISPOSITIVOS:
                return None
            usados = {d["nome"] for d in self.dispositivos}
            n = next(i for i in range(1, MAX_DISPOSITIVOS + 2) if f"Celular {i}" not in usados)
            d = {"id": secrets.token_hex(4), "nome": f"Celular {n}", "token": secrets.token_urlsafe(16),
                 "criado": time.time(), "ultimo_visto": time.time(), "ua": str(ua)[:120]}
            self.dispositivos.append(d)
            self._salvar_dispositivos()
            self.novo_pareamento()  # o QR muda na hora: o proximo celular precisa do novo
        return d

    def dispositivo_do_token(self, token):
        """Dispositivo dono do token (comparacao em tempo constante) ou None."""
        achado = None
        for d in list(self.dispositivos):
            if secrets.compare_digest(str(token), d["token"]):
                achado = d
        if achado:
            agora = time.time()
            achado["ultimo_visto"] = agora
            if agora - self._salvo_em > 60:
                self._salvar_dispositivos()
        return achado

    def revogar(self, id_):
        with self.lock:
            d = next((x for x in self.dispositivos if x["id"] == id_), None)
            if not d:
                return False
            self.dispositivos.remove(d)
            self._salvar_dispositivos()
        for q in list(self.clientes):  # fecha as conexoes abertas desse celular
            if getattr(q, "dispositivo", None) == id_:
                try:
                    q.put_nowait(None)
                except queue.Full:
                    pass
        return True

    def revogar_todos(self):
        for d in list(self.dispositivos):
            self.revogar(d["id"])

    def conectados(self):
        return {getattr(q, "dispositivo", None) for q in self.clientes} - {None}

    @property
    def bases(self):
        """[{"nome","base"}]: dinamico (recalcula a cada ~3 s) para o QR ganhar a rede Bluetooth assim que o computador conectar."""
        if self.bases_fn is None:
            return self._bases_fixas
        agora = time.time()
        if agora - self._bases_cache[0] > 3:
            self._bases_cache = (agora, self.bases_fn())
        return self._bases_cache[1]

    @bases.setter
    def bases(self, valor):
        self._bases_fixas, self.bases_fn = valor, None

    def rede_bluetooth(self):
        for r, ip in ips_locais():
            if r.startswith("Bluetooth"):
                return {"interface": r, "ip": ip}
        return None

    def urls_remoto(self, token):
        return [{"nome": b["nome"], "url": f"{b['base']}/remote?k={token}"} for b in self.bases]

    def urls_pareamento(self):
        c = self.codigo_pareamento()
        return [{"nome": b["nome"], "url": f"{b['base']}/p/{c}"} for b in self.bases]

    def url_pareamento(self):
        u = self.urls_pareamento()
        return u[0]["url"] if u else ""

    def dispositivos_info(self):
        on = self.conectados()
        return [{"id": d["id"], "nome": d["nome"], "ultimo_visto": d["ultimo_visto"], "conectado": d["id"] in on}
                for d in self.dispositivos]

    def deck_info(self):
        return {"nome": Path(self.pdf).stem, "paginas": self.total} if self.pdf else None

    def trocar_deck(self, pdf, notas_base):
        """Carrega outra apresentacao sem reiniciar: o celular recarrega a pagina e mostra previa e notas."""
        total = contar_paginas(pdf)
        with self.lock:
            self.pdf, self.total, self.proporcao = pdf, total, proporcao_pdf(pdf)
            self.notas_id = hashlib.sha1(pdf.encode()).hexdigest()[:10]
            self._edits, self._base_vazia = {}, False
            self.pagina, self.zoom, self.zx, self.zy, self.tracos = 1, 1.0, 0.5, 0.5, []
            self.t_base, self.t_rodando, self.voltas = 0.0, False, []
        self.carregar_notas_sessao(notas_base)
        self._mudou()
        self.publicar({"deck": 1})

    def remover_deck(self):
        self.fechar_tela(avisar=False)
        with self.lock:
            self.pdf, self.total, self.pagina = None, 9999, 1
            self.notas_id, self._edits, self._base_vazia = "livre", {}, False
        self.carregar_notas_sessao({})
        self._mudou()
        self.publicar({"deck": 1})

    def importar_notas(self, notas):
        """Substitui as notas da sessao pelas lidas de um arquivo (.pptx ou .md) escolhido no painel ou na linha de comando."""
        with self.lock:
            self._edits = {str(k): str(v)[:6000] for k, v in notas.items() if re.fullmatch(r"p?\d{1,5}", str(k))}
            self._base_vazia = True
            self._notas_efetivas()
            self._persistir_notas()
            return dict(self.notas)

    def set_cfg(self, d):
        """Aplica ajustes de personalizacao, grava em disco e repassa para a tela e a camada."""
        with self.lock:
            self.cfg = cfg_validar(d, self.cfg)
            cfg = dict(self.cfg)
        try:
            CFG_ARQ.parent.mkdir(parents=True, exist_ok=True)
            CFG_ARQ.write_text(json.dumps(cfg), encoding="utf-8")
        except OSError:
            pass
        self.sincronizar_overlay()
        self.publicar({"cfg": cfg})
        return cfg

    def sincronizar_overlay(self):
        if self.externo and self.externo.overlay:
            self.externo.overlay.enviar(dict(self.cfg, t="cfg"))

    def laser(self, x, y, on, modo="laser", raio=None):
        if not self.permitido():
            return
        raio = self.cfg["foco_raio"] / 100.0 if raio is None else raio
        raio = max(0.03, min(0.35, float(raio)))
        if self.ext:
            self.ext.laser(x, y, on, self.proporcao, modo, raio)
        if self.tela_conectada():  # so a tela do proprio Baton usa estes pontos; os celulares os ignoram (nada de fila a toa)
            self.publicar({"laser": {"x": x, "y": y, "on": on, "m": modo, "r": raio}})

    def desenhar(self, d):
        """Recebe pontos de um traco (caneta ou marca-texto) e repassa para a tela."""
        if not self.permitido():
            return
        with self.lock:
            tid = str(d.get("id", ""))[:24]
            pts = [[max(0.0, min(1.0, float(a))), max(0.0, min(1.0, float(b)))] for a, b in d.get("p", [])][:200]
            traco = next((t for t in self.tracos if t["id"] == tid), None)
            if traco is None:
                if len(self.tracos) >= 300:
                    return
                traco = {"id": tid, "t": "hl" if d.get("t") == "hl" else "pen", "c": str(d.get("c", "#ff3b30"))[:9], "p": []}
                self.tracos.append(traco)
            if len(traco["p"]) < 6000:
                traco["p"].extend(pts)
        if self.ext and pts:
            self.ext.desenho(traco, pts, self.proporcao)
        self.publicar({"draw": {"id": tid, "t": traco["t"], "c": traco["c"], "p": pts}})


def baixar_pdfjs():
    pasta = pasta_pdfjs()
    pasta.mkdir(parents=True, exist_ok=True)
    for nome in PDFJS_FILES:
        destino = pasta / nome
        if destino.exists() and destino.stat().st_size > 1000:
            continue
        try:
            with urllib.request.urlopen(PDFJS_CDN + nome, timeout=15) as r:
                destino.write_bytes(r.read())
        except Exception as e:  # sem internet: o navegador cai na CDN
            print(f"aviso: nao baixei {nome} ({e}); o navegador usara a CDN", file=sys.stderr)
            return False
    return True


def baixar_qr():
    arq = arquivo_qr()
    if arq.exists() and arq.stat().st_size > 1000:
        return True
    try:
        arq.parent.mkdir(parents=True, exist_ok=True)
        with urllib.request.urlopen(QR_CDN, timeout=15) as r:
            arq.write_bytes(r.read())
        return True
    except Exception as e:
        print(f"aviso: nao baixei o gerador de QR ({e}); o navegador usara a CDN", file=sys.stderr)
        return False


# --------------------------------------------------------------- controle de outros leitores
class _Log:
    """Backend de teste: so imprime o que faria."""
    def tecla(self, nome):
        print("tecla:", nome, flush=True)

    def mover(self, x, y):
        print(f"mouse: {x:.0f},{y:.0f}", flush=True)

    def combo(self, mods, tecla):
        print("combo:", "+".join(list(mods) + [tecla]), flush=True)

    def tela(self):
        return 1920.0, 1080.0


class _Mac:
    """CoreGraphics via ctypes (sem dependencias). Precisa de Acessibilidade para o Terminal."""
    CODES = {"Right": 124, "Left": 123, "Home": 115, "End": 119, "b": 11, "equal": 24, "minus": 27, "0": 29, "Escape": 53}
    MODS = {"alt": 0x80000, "cmd": 0x100000, "ctrl": 0x40000, "shift": 0x20000}  # flags do CGEvent

    def __init__(self):
        import ctypes
        import ctypes.util
        ct = ctypes
        lib = ct.util.find_library("ApplicationServices") or ct.util.find_library("CoreGraphics")
        cg = ct.CDLL(lib)
        cf = ct.CDLL(ct.util.find_library("CoreFoundation"))

        class Pt(ct.Structure):
            _fields_ = [("x", ct.c_double), ("y", ct.c_double)]

        class Sz(ct.Structure):
            _fields_ = [("w", ct.c_double), ("h", ct.c_double)]

        class Rc(ct.Structure):
            _fields_ = [("o", Pt), ("s", Sz)]

        self.Pt = Pt
        cg.CGEventCreateKeyboardEvent.restype = ct.c_void_p
        cg.CGEventCreateKeyboardEvent.argtypes = [ct.c_void_p, ct.c_uint16, ct.c_bool]
        cg.CGEventCreateMouseEvent.restype = ct.c_void_p
        cg.CGEventCreateMouseEvent.argtypes = [ct.c_void_p, ct.c_uint32, Pt, ct.c_uint32]
        cg.CGEventPost.argtypes = [ct.c_uint32, ct.c_void_p]
        cg.CGEventSourceCreate.restype = ct.c_void_p
        cg.CGEventSourceCreate.argtypes = [ct.c_int32]
        cg.CGEventSetFlags.argtypes = [ct.c_void_p, ct.c_uint64]
        self.fonte = cg.CGEventSourceCreate(1)  # kCGEventSourceStateHIDSystemState: igual a um teclado de verdade
        cg.CGWarpMouseCursorPosition.argtypes = [Pt]
        cg.CGMainDisplayID.restype = ct.c_uint32
        cg.CGDisplayBounds.argtypes = [ct.c_uint32]
        cg.CGDisplayBounds.restype = Rc
        cf.CFRelease.argtypes = [ct.c_void_p]
        self.cg, self.cf = cg, cf

    def _post(self, ev):
        self.cg.CGEventPost(0, ev)
        self.cf.CFRelease(ev)

    # setas, Home e End de um teclado real chegam com estas marcas (Fn e teclado numerico); alguns apps
    # e navegadores em tela cheia ignoram eventos de seta sem elas
    FLAGS = {"Right": 0x800000 | 0x200000, "Left": 0x800000 | 0x200000, "Home": 0x800000, "End": 0x800000}

    def tecla(self, nome):
        codigo = self.CODES[nome]
        for baixo in (True, False):
            ev = self.cg.CGEventCreateKeyboardEvent(self.fonte, codigo, baixo)
            if nome in self.FLAGS:
                self.cg.CGEventSetFlags(ev, self.FLAGS[nome])
            self._post(ev)

    def combo(self, mods, tecla):
        """Tecla com modificadores (ex.: Option+Command+=, o atalho de zoom do Mac)."""
        flags = 0
        for m in mods:
            flags |= self.MODS.get(m, 0)
        for baixo in (True, False):
            ev = self.cg.CGEventCreateKeyboardEvent(self.fonte, self.CODES[tecla], baixo)
            self.cg.CGEventSetFlags(ev, flags)
            self._post(ev)

    def mover(self, x, y):
        pt = self.Pt(x, y)
        self.cg.CGWarpMouseCursorPosition(pt)
        self._post(self.cg.CGEventCreateMouseEvent(None, 5, pt, 0))  # kCGEventMouseMoved

    def tela(self):
        r = self.cg.CGDisplayBounds(self.cg.CGMainDisplayID())
        return r.s.w, r.s.h


ARQ_ACESSO = Path.home() / ".cache" / "baton" / "acessibilidade"
ARQ_ESQUEMA = Path.home() / ".cache" / "baton" / "assinatura-esquema"


def acesso_do_launcher(max_idade=15):
    """O que o Baton.app (nao o terminal!) enxerga: True/False pelo arquivo que o launcher grava a cada 2 s; None se
    o arquivo nao existe ou esta velho (launcher parado)."""
    try:
        if time.time() - ARQ_ACESSO.stat().st_mtime > max_idade:
            return None
        return ARQ_ACESSO.read_text().strip() == "1"
    except OSError:
        return None


def acessibilidade_mac():
    """True/False se o macOS deixou o Baton enviar teclas (Acessibilidade); None fora do Mac ou sem como saber.
    Pelo Baton.app o launcher grava o estado em ~/.cache/baton/acessibilidade (a permissao e do app, nao do Python filho)."""
    if sys.platform != "darwin":
        return None
    if os.environ.get("BATON_LAUNCHER"):
        try:
            return ARQ_ACESSO.read_text().strip() == "1"
        except OSError:
            return None
    try:
        import ctypes
        import ctypes.util
        lib = ctypes.CDLL(ctypes.util.find_library("ApplicationServices"))
        lib.AXIsProcessTrusted.restype = ctypes.c_bool
        return bool(lib.AXIsProcessTrusted())
    except Exception:
        return None


class _Windows:
    """user32 via ctypes (sem dependencias): teclas, cursor e tamanho da tela em pixels fisicos."""
    VK = {"Right": 0x27, "Left": 0x25, "Home": 0x24, "End": 0x23, "b": 0x42, "equal": 0xBB, "minus": 0xBD, "Escape": 0x1B, "0": 0x30}
    VK_MOD = {"win": 0x5B, "alt": 0x12, "ctrl": 0x11, "shift": 0x10}
    ESTENDIDAS = ("Right", "Left", "Home", "End")

    def __init__(self):
        import ctypes
        self.u = ctypes.windll.user32
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(2)
        except Exception:
            try:
                self.u.SetProcessDPIAware()
            except Exception:
                pass

    def tecla(self, nome):
        vk = self.VK[nome]
        ext = 1 if nome in self.ESTENDIDAS else 0  # KEYEVENTF_EXTENDEDKEY
        scan = self.u.MapVirtualKeyW(vk, 0)
        self.u.keybd_event(vk, scan, ext, 0)
        self.u.keybd_event(vk, scan, ext | 2, 0)  # KEYEVENTF_KEYUP

    def combo(self, mods, tecla):
        """Tecla com modificadores (ex.: Win+= abre e amplia a Lupa)."""
        ms = [self.VK_MOD[m] for m in mods if m in self.VK_MOD]
        for vk in ms:
            self.u.keybd_event(vk, self.u.MapVirtualKeyW(vk, 0), 1 if vk == 0x5B else 0, 0)
        vk = self.VK[tecla]
        scan = self.u.MapVirtualKeyW(vk, 0)
        self.u.keybd_event(vk, scan, 0, 0)
        self.u.keybd_event(vk, scan, 2, 0)
        for vk in reversed(ms):
            self.u.keybd_event(vk, self.u.MapVirtualKeyW(vk, 0), (1 if vk == 0x5B else 0) | 2, 0)

    def mover(self, x, y):
        self.u.SetCursorPos(int(x), int(y))

    def tela(self):
        return float(self.u.GetSystemMetrics(0)), float(self.u.GetSystemMetrics(1))


class _Xdotool:
    def __init__(self):
        if not shutil.which("xdotool"):
            raise RuntimeError("instale o xdotool para o modo externo no Linux")

    def tecla(self, nome):
        subprocess.run(["xdotool", "key", nome], check=False)

    XMOD = {"alt": "alt", "cmd": "super", "win": "super", "super": "super", "ctrl": "ctrl", "shift": "shift"}

    def combo(self, mods, tecla):
        subprocess.run(["xdotool", "key", "+".join([self.XMOD.get(m, m) for m in mods] + [tecla])], check=False, capture_output=True)

    def mover(self, x, y):
        subprocess.run(["xdotool", "mousemove", str(int(x)), str(int(y))], check=False)

    def tela(self):
        out = subprocess.run(["xdotool", "getdisplaygeometry"], capture_output=True, text=True).stdout.split()
        return float(out[0]), float(out[1])


KEYSYMS = {"Right": 0xFF53, "Left": 0xFF51, "Up": 0xFF52, "Down": 0xFF54, "Home": 0xFF50, "End": 0xFF57, "Next": 0xFF56,
           "Prior": 0xFF55, "Return": 0xFF0D, "Escape": 0xFF1B, "space": 0x20, "b": 0x62, "period": 0x2E}
PORTAL_TOKEN = Path.home() / ".cache" / "baton" / "portal-token"


class _Portal:
    """Teclas no Linux com Wayland (GNOME, KDE) pelo portal RemoteDesktop do xdg-desktop-portal: o xdotool so alcanca janelas
    X11/XWayland, o resto (LibreOffice, Okular, navegadores em Wayland) ignora. O sistema pede a permissao uma vez (o token
    fica salvo em ~/.cache/baton/portal-token); enquanto nao houver sessao, usa o xdotool como reserva."""

    def __init__(self):
        import gi  # PyGObject: python3-gi
        from gi.repository import Gio, GLib
        self.Gio, self.GLib = Gio, GLib
        self.sessao, self.estado, self.erro = None, "iniciando", None
        self.pronto = threading.Event()
        threading.Thread(target=self._rodar, daemon=True).start()
        self.pronto.wait(2.0)  # so para dar tempo de saber se o portal existe

    def _rodar(self):
        Gio, GLib = self.Gio, self.GLib
        try:
            self.ctx = GLib.MainContext.new()
            self.ctx.push_thread_default()
            self.con = Gio.bus_get_sync(Gio.BusType.SESSION, None)
            self.sender = self.con.get_unique_name()[1:].replace(".", "_")
            ok, res = self._pedido("CreateSession", None, {"session_handle_token": GLib.Variant("s", "baton" + uuid.uuid4().hex[:8])}, 15)
            if not ok:
                raise RuntimeError("CreateSession recusado")
            sessao = res["session_handle"]
            opc = {"types": GLib.Variant("u", 3), "persist_mode": GLib.Variant("u", 2)}  # teclado + ponteiro; lembra a permissao
            try:
                opc["restore_token"] = GLib.Variant("s", PORTAL_TOKEN.read_text().strip())
            except OSError:
                pass
            ok, res = self._pedido("SelectDevices", sessao, opc, 15)
            if not ok:
                raise RuntimeError("SelectDevices recusado")
            self.estado = "aguardando permissao"
            print("Wayland: o sistema vai pedir permissao para o Baton controlar o teclado (uma vez).", flush=True)
            ok, res = self._pedido("Start", sessao, {}, 300, extra="")
            if not ok:
                raise RuntimeError("permissao negada ou cancelada")
            tok = res.get("restore_token")
            if tok:
                PORTAL_TOKEN.parent.mkdir(parents=True, exist_ok=True)
                PORTAL_TOKEN.write_text(tok)
                os.chmod(PORTAL_TOKEN, 0o600)
            self.sessao, self.estado = sessao, "pronto"
            print("Wayland: controle de teclado liberado pelo portal.", flush=True)
        except Exception as e:
            self.erro, self.estado = str(e), "indisponivel"
            print(f"aviso: teclas no Wayland indisponiveis ({e}); usando xdotool (so janelas X11)", file=sys.stderr, flush=True)
        finally:
            self.pronto.set()

    def _pedido(self, metodo, sessao, opcoes, espera, extra=None):
        """Chama um metodo do RemoteDesktop e espera o sinal Response. Devolve (ok, resultados)."""
        Gio, GLib = self.Gio, self.GLib
        tok = "baton" + uuid.uuid4().hex[:10]
        opcoes = dict(opcoes, handle_token=GLib.Variant("s", tok))
        caminho = f"/org/freedesktop/portal/desktop/request/{self.sender}/{tok}"
        resp = {}
        sub = self.con.signal_subscribe("org.freedesktop.portal.Desktop", "org.freedesktop.portal.Request", "Response", caminho,
                                        None, Gio.DBusSignalFlags.NONE, lambda *a: resp.setdefault("v", a[5].unpack()))
        try:
            if sessao is None:
                args = GLib.Variant("(a{sv})", (opcoes,))
            elif extra is None:
                args = GLib.Variant("(oa{sv})", (sessao, opcoes))
            else:
                args = GLib.Variant("(osa{sv})", (sessao, extra, opcoes))
            self.con.call_sync("org.freedesktop.portal.Desktop", "/org/freedesktop/portal/desktop",
                               "org.freedesktop.portal.RemoteDesktop", metodo, args, None, Gio.DBusCallFlags.NONE, 5000, None)
            fim = time.time() + espera
            while "v" not in resp and time.time() < fim:
                self.ctx.iteration(False)
                time.sleep(0.02)
        finally:
            self.con.signal_unsubscribe(sub)
        if "v" not in resp:
            return False, {}
        codigo, resultados = resp["v"]
        return codigo == 0, resultados

    def tecla(self, nome):
        sym = KEYSYMS.get(nome)
        if self.sessao and sym:
            try:
                for estado in (1, 0):
                    self.con.call_sync("org.freedesktop.portal.Desktop", "/org/freedesktop/portal/desktop",
                                       "org.freedesktop.portal.RemoteDesktop", "NotifyKeyboardKeysym",
                                       self.GLib.Variant("(oa{sv}iu)", (self.sessao, {}, sym, estado)), None,
                                       self.Gio.DBusCallFlags.NONE, 2000, None)
                return
            except Exception as e:
                print(f"aviso: portal recusou a tecla {nome} ({e})", file=sys.stderr)
        if shutil.which("xdotool"):
            subprocess.run(["xdotool", "key", nome], check=False, capture_output=True)

    MODSYM = {"alt": 0xFFE9, "cmd": 0xFFEB, "win": 0xFFEB, "super": 0xFFEB, "ctrl": 0xFFE3, "shift": 0xFFE1}

    def combo(self, mods, tecla):
        sym = KEYSYMS.get(tecla) or {"equal": 0x3D, "minus": 0x2D, "0": 0x30}.get(tecla)
        if self.sessao and sym:
            try:
                def env(s, e):
                    self.con.call_sync("org.freedesktop.portal.Desktop", "/org/freedesktop/portal/desktop",
                                       "org.freedesktop.portal.RemoteDesktop", "NotifyKeyboardKeysym",
                                       self.GLib.Variant("(oa{sv}iu)", (self.sessao, {}, s, e)), None, self.Gio.DBusCallFlags.NONE, 2000, None)
                ms = [self.MODSYM[m] for m in mods if m in self.MODSYM]
                for m in ms:
                    env(m, 1)
                env(sym, 1)
                env(sym, 0)
                for m in reversed(ms):
                    env(m, 0)
                return
            except Exception as e:
                print(f"aviso: portal recusou o atalho {tecla} ({e})", file=sys.stderr)
        if shutil.which("xdotool"):
            subprocess.run(["xdotool", "key", "+".join([_Xdotool.XMOD.get(m, m) for m in mods] + [tecla])], check=False, capture_output=True)

    def mover(self, x, y):
        if shutil.which("xdotool"):
            subprocess.run(["xdotool", "mousemove", str(int(x)), str(int(y))], check=False, capture_output=True)

    def tela(self):
        try:
            out = subprocess.run(["xdotool", "getdisplaygeometry"], capture_output=True, text=True, timeout=3).stdout.split()
            return float(out[0]), float(out[1])
        except Exception:
            return 1920.0, 1080.0


def sessao_wayland():
    return os.environ.get("XDG_SESSION_TYPE", "").lower() == "wayland" or bool(os.environ.get("WAYLAND_DISPLAY"))


class _OverlayLog:
    """Camada de teste: so imprime as mensagens."""
    def enviar(self, obj):
        print("overlay:", json.dumps(obj), flush=True)


class _OverlayMac:
    """Camada transparente nativa (overlay.swift), compilada na primeira execucao."""

    def __init__(self, tela=0, nivel=None):
        self.nivel = nivel
        fonte = Path(__file__).with_name("overlay.swift")
        if not fonte.exists():
            raise RuntimeError("overlay.swift nao encontrado ao lado do baton.py")
        h = hashlib.sha1(fonte.read_bytes()).hexdigest()[:10]
        binario = Path.home() / ".cache" / "baton" / f"overlay-{h}"
        if not binario.exists():
            swiftc = shutil.which("swiftc")
            if not swiftc:
                raise RuntimeError("swiftc nao encontrado: rode 'xcode-select --install'")
            binario.parent.mkdir(parents=True, exist_ok=True)
            print("Compilando a camada de desenho (so na primeira vez, pode levar um minuto)...", flush=True)
            r = subprocess.run([swiftc, "-O", str(fonte), "-o", str(binario)], capture_output=True, text=True)
            if r.returncode != 0:
                raise RuntimeError("falha ao compilar overlay.swift:\n" + r.stderr[-1500:])
        self.binario = binario
        self.lock = threading.Lock()
        self._iniciar(tela)

    def _iniciar(self, tela):
        args = [str(self.binario), "--tela", str(tela)] + (["--nivel", str(self.nivel)] if self.nivel is not None else [])
        self.p = subprocess.Popen(args, stdin=subprocess.PIPE, text=True, bufsize=1)

    def reiniciar(self, mon):
        """Camada no outro monitor (o indice do NSScreen e o mesmo de monitores.listar())."""
        self.fechar()
        try:
            self.p.wait(timeout=2)
        except subprocess.TimeoutExpired:
            self.p.kill()
        self._iniciar(mon.get("id", 0))

    def enviar(self, obj):
        with self.lock:
            try:
                self.p.stdin.write(json.dumps(obj) + "\n")
                self.p.stdin.flush()
            except (BrokenPipeError, OSError, ValueError):
                pass

    def fechar(self):
        self.enviar({"t": "quit"})


class _OverlayProc:
    """Camada transparente em Python (overlay_gtk.py no Linux, overlay_tk.py no Windows), mesmo protocolo do Swift."""

    def __init__(self, script, tela=0):
        fonte = Path(__file__).with_name(script)
        if not fonte.exists():
            raise RuntimeError(f"{script} nao encontrado ao lado do baton.py")
        self.fonte = fonte
        self.lock = threading.Lock()
        self._iniciar(tela)
        time.sleep(1.0)
        if self.p.poll() is not None:  # sem Gtk, sem compositor, sem Tk: o processo encerra sozinho
            raise RuntimeError(f"{script} encerrou (codigo {self.p.returncode}); veja a mensagem acima")

    def _iniciar(self, tela):
        if isinstance(tela, dict):  # retangulo exato do monitor (x, y, largura, altura)
            args = ["--geom", f"{tela['x']},{tela['y']},{tela['w']},{tela['h']}"]
        else:
            args = ["--tela", str(tela)]
        self.p = subprocess.Popen([sys.executable, str(self.fonte), *args], stdin=subprocess.PIPE, text=True, bufsize=1)

    def reiniciar(self, mon):
        self.fechar()
        try:
            self.p.wait(timeout=2)
        except subprocess.TimeoutExpired:
            self.p.kill()
        self._iniciar(mon)

    enviar = _OverlayMac.enviar
    fechar = _OverlayMac.fechar


def zoom_sistema_cfg():
    """Atalhos do zoom nativo do sistema (o Baton nao amplia a janela de outro programa; o zoom do SO segue o cursor):
    {"mods", "mais", "menos", "reset"} ou None se o desktop nao tem um (ex.: XFCE, i3)."""
    if sys.platform == "darwin":
        return {"mods": ("alt", "cmd"), "mais": "equal", "menos": "minus", "reset": None, "nome": "Zoom de Acessibilidade"}
    if sys.platform == "win32":
        return {"mods": ("win",), "mais": "equal", "menos": "minus", "reset": (("win",), "Escape"), "nome": "Lupa do Windows"}
    if sys.platform.startswith("linux"):
        d = os.environ.get("XDG_CURRENT_DESKTOP", "").upper()
        if "GNOME" in d or "UNITY" in d or "ZORIN" in d:
            return {"mods": ("super", "alt"), "mais": "equal", "menos": "minus", "reset": None, "nome": "Ampliador do GNOME"}
        if "KDE" in d or "PLASMA" in d:
            return {"mods": ("super",), "mais": "equal", "menos": "minus", "reset": (("super",), "0"), "nome": "Zoom do KDE"}
    return None


def passos_zoom(z):
    """Nivel de zoom (1 a 5) -> numero de toques no atalho de ampliar do sistema (cada toque ~ +0,5x)."""
    return max(0, int(round((max(1.0, min(5.0, float(z))) - 1.0) * 2)))


class Externo:
    """Traduz comandos do celular em teclas, mouse e desenho (camada) para o app em primeiro plano."""
    TECLAS = {"next": "Right", "prev": "Left", "first": "Home", "last": "End"}

    def __init__(self, backend, overlay=None, cursor=True):
        self.b = backend
        self.overlay = overlay
        self.cursor = cursor or overlay is None
        self.ultimo_mouse = 0.0
        self.tecla_preto = False
        self.monitor_fn = None  # () -> monitor da projecao (dict x, y, w, h) ou None
        self.mon_atual = None
        self.zoom_passos = 0
        self.lupa_arq, self.lupa_pend, self.lupa_capturando, self.lupa_dir, self.lupa_n = None, None, False, None, 0
        self._zq = queue.Queue()
        threading.Thread(target=self._zoom_worker, daemon=True).start()

    LUPA_TAM = 0.32  # lado da lupa, em fracao da largura/altura do monitor (mesmo formato do monitor, nao circular)

    def zoom_externo(self, z, x, y, proporcao):
        """Zoom do modo externo: lupa retangular pela camada do Baton; sem camada, o zoom nativo do sistema."""
        if self.overlay:
            return self.lupa(z, x, y, proporcao)
        return self.zoom_sistema(z, x, y, proporcao)

    def lupa(self, z, x, y, proporcao):
        """Lupa retangular sobre a projecao: a camada desenha, ampliada, uma captura do monitor feita ao ligar."""
        if z <= 1.001:
            velho, self.lupa_pend, self.lupa_arq = self.lupa_arq, None, None
            self.overlay.enviar({"t": "lupa", "on": False})
            if velho:  # a camada ja soltou a imagem; apaga a captura
                threading.Timer(2.0, lambda: Path(velho).unlink(missing_ok=True)).start()
            return True
        _, _, nx, ny = self.para_tela(x, y, proporcao)
        self.lupa_pend = (z, nx, ny)
        if self.lupa_arq is None:
            if not self.lupa_capturando:
                self.lupa_capturando = True
                threading.Thread(target=self._lupa_capturar, daemon=True).start()
            return True
        self._lupa_enviar()
        return True

    def _lupa_enviar(self):
        pend, arq = self.lupa_pend, self.lupa_arq
        if pend and arq:
            self.overlay.enviar({"t": "lupa", "on": True, "img": str(arq), "x": pend[1], "y": pend[2], "z": pend[0], "w": self.LUPA_TAM})

    def _lupa_capturar(self):
        try:
            if self.lupa_dir is None:
                self.lupa_dir = tempfile.mkdtemp(prefix="baton-lupa-")
            self.lupa_n += 1
            arq = Path(self.lupa_dir) / f"lupa-{self.lupa_n}.png"
            ok = capturar_png(arq, self.monitor_fn() if self.monitor_fn else None)
            if ok and self.lupa_pend:  # se desligou durante a captura, descarta
                self.lupa_arq = arq
                self._lupa_enviar()
            elif not ok:
                print("aviso: a lupa precisa capturar a tela (no Mac: Privacidade e Seguranca > Gravacao de Tela)", file=sys.stderr)
        except Exception as e:
            print(f"aviso: lupa falhou ({e})", file=sys.stderr)
        finally:
            self.lupa_capturando = False

    def zoom_sistema(self, z, x, y, proporcao):
        """Zoom do sistema: o ponteiro vai para o ponto escolhido e os atalhos de ampliar/reduzir cobrem a diferenca de nivel.
        Roda numa thread (varias teclas) e junta pedidos seguidos: so o ultimo importa."""
        if zoom_sistema_cfg() is None:
            return False
        self._zq.put((z, x, y, proporcao))
        return True

    def _zoom_worker(self):
        while True:
            item = self._zq.get()
            try:
                while True:  # junta: fica com o pedido mais novo
                    item = self._zq.get_nowait()
            except queue.Empty:
                pass
            try:
                self._zoom_aplicar(*item)
            except Exception as e:
                print(f"aviso: zoom do sistema falhou ({e})", file=sys.stderr)

    def _zoom_aplicar(self, z, x, y, proporcao):
        cfg = zoom_sistema_cfg()
        if cfg is None:
            return
        alvo = passos_zoom(z)
        if alvo > 0:  # o zoom do SO segue o ponteiro: leva o cursor ao ponto
            px, py, _, _ = self.para_tela(x, y, proporcao)
            self.b.mover(px, py)
        delta = alvo - self.zoom_passos
        combo = getattr(self.b, "combo", None)
        if combo is None:
            return
        if alvo == 0 and (self.zoom_passos > 0 or delta):
            for _ in range(self.zoom_passos + 2):
                combo(cfg["mods"], cfg["menos"])
            if cfg["reset"]:
                combo(*cfg["reset"])
        else:
            for _ in range(abs(delta)):
                combo(cfg["mods"], cfg["mais"] if delta > 0 else cfg["menos"])
        self.zoom_passos = alvo

    def cortina(self, tipo, txt="", sub=""):
        """Cortina nativa por cima do app. Sem camada, so o preto funciona (tecla B do leitor)."""
        if self.overlay:
            self.overlay.enviar({"t": "cortina", "k": tipo or "off", "txt": txt, "sub": sub})
        elif (tipo == "preto") != self.tecla_preto:
            self.tecla_preto = tipo == "preto"
            self.b.tecla("b")

    def confete(self, seg=4.0):
        if self.overlay:
            self.overlay.enviar({"t": "confete", "seg": seg})

    def comando(self, cmd):
        tecla = self.TECLAS.get(cmd)
        if tecla:
            self.b.tecla(tecla)

    def ir(self, n):
        """Sem acesso ao leitor: volta ao inicio e avanca n-1 vezes (conta toques, nao animacoes)."""
        def _ir():
            self.b.tecla("Home")
            for _ in range(max(0, n - 1)):
                time.sleep(0.03)
                self.b.tecla("Right")
        threading.Thread(target=_ir, daemon=True).start()

    def para_tela(self, x, y, proporcao):
        """Coordenadas do slide (0 a 1) -> coordenadas da tela em pixels e normalizadas (0 a 1)."""
        mon = self._monitor()
        if mon:
            w, h, mx, my = float(mon["w"]), float(mon["h"]), float(mon["x"]), float(mon["y"])
        else:
            w, h = self.b.tela()
            mx = my = 0.0
        if mon:  # a area do toque no celular tem o formato do monitor: 0..1 cobre o monitor inteiro, cantos incluidos
            px, py = x * w, y * h
        else:
            if proporcao > w / h:  # sem dados do monitor: o slide ocupa a tela, com barras se a proporcao diferir
                largura, altura = w, w / proporcao
            else:
                largura, altura = h * proporcao, h
            px, py = (w - largura) / 2 + x * largura, (h - altura) / 2 + y * altura
        return mx + px, my + py, px / w, py / h  # pixels absolutos (cursor) e normalizados dentro do monitor (camada)

    def _monitor(self):
        """Monitor da projecao; se mudou, move a camada de desenho para ele."""
        if not self.monitor_fn:
            return None
        mon = self.monitor_fn()
        if mon and (self.mon_atual is None or (mon["x"], mon["y"], mon["w"], mon["h"], mon.get("id")) !=
                    (self.mon_atual["x"], self.mon_atual["y"], self.mon_atual["w"], self.mon_atual["h"], self.mon_atual.get("id"))):
            ant, self.mon_atual = self.mon_atual, mon
            if ant is not None and self.overlay is not None and hasattr(self.overlay, "reiniciar"):
                threading.Thread(target=self.overlay.reiniciar, args=(mon,), daemon=True).start()
        return mon

    def laser(self, x, y, on, proporcao, modo="laser", raio=0.12):
        agora = time.monotonic()
        if on and agora - self.ultimo_mouse < 0.012:
            return
        self.ultimo_mouse = agora
        px, py, nx, ny = self.para_tela(x, y, proporcao)
        if self.overlay:
            self.overlay.enviar({"t": "laser", "x": nx, "y": ny, "on": bool(on), "m": modo, "r": raio})
        if on and self.cursor and modo == "laser":
            self.b.mover(px, py)

    def desenho(self, d, pontos, proporcao):
        if not self.overlay:
            return
        pts = [list(self.para_tela(a, b, proporcao)[2:]) for a, b in pontos]
        self.overlay.enviar({"t": "draw", "id": d["id"], "hl": d["t"] == "hl", "c": d["c"], "p": pts})

    def limpar(self):
        if self.overlay:
            self.overlay.enviar({"t": "clear"})


def criar_externo(usar_overlay=True, tela=0, cursor=False, nivel=None, monitor=None):
    overlay = None
    if monitor:
        tela = monitor if sys.platform not in ("darwin",) else monitor.get("id", 0)  # Mac: indice do NSScreen; demais: retangulo
    if usar_overlay:
        try:
            if os.environ.get("BATON_OVERLAY") == "log":
                overlay = _OverlayLog()
            elif sys.platform == "darwin":
                overlay = _OverlayMac(tela, nivel)
            elif sys.platform == "win32":
                overlay = _OverlayProc("overlay_tk.py", tela)
            elif sys.platform.startswith("linux"):
                try:
                    overlay = _OverlayProc("overlay_gtk.py", tela)
                except Exception as e:
                    print(f"aviso: camada Gtk indisponivel ({e}); tentando Tk (sem transparencia real)", file=sys.stderr)
                    overlay = _OverlayProc("overlay_tk.py", tela)
        except Exception as e:
            print(f"aviso: sem camada de desenho ({e}); o laser sera so o cursor", file=sys.stderr)
    if os.environ.get("BATON_BACKEND") == "log":
        return Externo(_Log(), overlay, cursor)
    if sys.platform == "darwin":
        return Externo(_Mac(), overlay, cursor)
    if sys.platform == "win32":
        return Externo(_Windows(), overlay, cursor)
    if sys.platform.startswith("linux"):
        if sessao_wayland():
            try:
                return Externo(_Portal(), overlay, cursor)
            except Exception as e:  # sem PyGObject: so o xdotool (janelas X11/XWayland)
                print(f"aviso: sem portal Wayland ({e}); usando xdotool", file=sys.stderr)
        return Externo(_Xdotool(), overlay, cursor)
    sys.exit("modo externo suportado no macOS, Windows e Linux (X11 com xdotool)")


def proporcao_pdf(pdf):
    try:
        import re
        dados = Path(pdf).read_bytes()
        m = re.search(rb"/MediaBox\s*\[\s*([\d.\-]+)\s+([\d.\-]+)\s+([\d.\-]+)\s+([\d.\-]+)\s*\]", dados)
        if m:
            w = float(m.group(3)) - float(m.group(1))
            h = float(m.group(4)) - float(m.group(2))
            if w > 0 and h > 0:
                return w / h / 2 if w / h > 2.2 else w / h  # beamer com notas ao lado: o slide e a metade esquerda
    except Exception:
        pass
    return 16 / 9


def ler_notas(caminho):
    return notas_de_texto(Path(caminho).read_text(encoding="utf-8"))


def notas_de_texto(texto):
    """Markdown simples: '## 5' = notas do slide 5 (frames, passos de um frame contam como um);
    '## p12' = notas da pagina 12 do PDF. O texto vai ate o proximo '##'."""
    notas, chave, buf = {}, None, []
    for linha in texto.splitlines():
        m = re.match(r"^##\s+(p?\d+)\s*$", linha.strip())
        if m:
            if chave is not None:
                notas[chave] = "\n".join(buf).strip()
            chave, buf = m.group(1), []
        elif chave is not None:
            buf.append(linha)
    if chave is not None:
        notas[chave] = "\n".join(buf).strip()
    return notas


@functools.lru_cache(maxsize=4)
def icone_png(tam=180):
    """PNG simples (fundo escuro com ponto vermelho) para a tela inicial do iPhone."""
    linhas = []
    c = (tam - 1) / 2
    for y in range(tam):
        linha = bytearray([0])
        for x in range(tam):
            d = ((x - c) ** 2 + (y - c) ** 2) ** 0.5 / (tam / 2)
            if d < 0.22:
                px = (255, 255, 255)
            elif d < 0.5:
                t = (d - 0.22) / 0.28
                px = (255, int(60 * t + 45 * (1 - t)), int(60 * t + 45 * (1 - t)))
            elif d < 0.72:
                t = (d - 0.5) / 0.22
                px = (int(255 * (1 - t) + 20 * t), int(45 * (1 - t) + 20 * t), int(45 * (1 - t) + 22 * t))
            else:
                px = (20, 20, 22)
            linha += bytes(px)
        linhas.append(bytes(linha))
    raw = zlib.compress(b"".join(linhas), 9)

    def chunk(tipo, dados):
        return struct.pack(">I", len(dados)) + tipo + dados + struct.pack(">I", zlib.crc32(tipo + dados) & 0xFFFFFFFF)

    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", tam, tam, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", raw) + chunk(b"IEND", b""))


PLIST = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
<key>Label</key><string>com.baton</string>
<key>ProgramArguments</key><array>__ARGS__</array>
<key>RunAtLoad</key><true/><key>KeepAlive</key><dict><key>SuccessfulExit</key><false/></dict><key>ThrottleInterval</key><integer>10</integer>
<key>StandardOutPath</key><string>__LOG__</string><key>StandardErrorPath</key><string>__LOG__</string>
</dict></plist>
"""


def plist_launchd(args, log):
    from xml.sax.saxutils import escape
    return PLIST.replace("__ARGS__", "".join(f"<string>{escape(str(a))}</string>" for a in args)).replace("__LOG__", escape(str(log)))


INFO_PLIST_APP = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
<key>CFBundleName</key><string>Baton</string><key>CFBundleDisplayName</key><string>Baton</string>
<key>CFBundleIdentifier</key><string>com.baton.app</string><key>CFBundleExecutable</key><string>Baton</string>
<key>CFBundlePackageType</key><string>APPL</string><key>CFBundleIconFile</key><string>Baton</string>
<key>CFBundleVersion</key><string>1</string><key>LSMinimumSystemVersion</key><string>12.0</string>
<key>CFBundleShortVersionString</key><string>0.1</string><key>LSUIElement</key><true/>
<key>NSAppleEventsUsageDescription</key><string>O Baton detecta apresentacoes em tela cheia para perguntar se deve controla-las.</string>
</dict></plist>
"""


MARCA_APP = Path.home() / ".cache" / "baton" / "launcher-hash"
MARCA_APP_BANDEJA = Path.home() / ".cache" / "baton" / "launcher-bandeja"


REQUISITO_APP = 'designated => identifier "com.baton.app"'
ESQUEMA_ASSINATURA = "dr1"  # mudou a forma de assinar: o registro antigo da Acessibilidade e zerado UMA vez


def assinar_app(app):
    """Assina o Baton.app com requisito designado FIXO (so o identificador). A assinatura ad-hoc comum usa o hash do binario
    como requisito: cada recompilacao vira outro 'app' para o macOS e a permissao some. Com o requisito fixo, a permissao
    sobrevive a atualizacoes. Devolve True se assinou com o requisito fixo."""
    if not shutil.which("codesign"):
        return False
    r = subprocess.run(["codesign", "--force", "--deep", "-s", "-", "-r", "=" + REQUISITO_APP, str(app)], capture_output=True, text=True)
    if r.returncode == 0:
        return True
    print("aviso: nao consegui fixar o requisito da assinatura (" + (r.stderr or "").strip()[-200:] + "); a permissao pode se perder ao atualizar", file=sys.stderr)
    subprocess.run(["codesign", "--force", "--deep", "-s", "-", str(app)], check=False, capture_output=True)
    return False


def montar_app_mac(forcar=False):
    """Cria ~/Applications/Baton.app (miolo em Swift): a lista de permissoes do macOS passa a mostrar 'Baton'."""
    base = Path(__file__).resolve().parent
    fonte = base / "launcher.swift"
    swiftc = shutil.which("swiftc")
    if not fonte.exists() or not swiftc:
        print("aviso: sem swiftc/launcher.swift; a permissao aparecera como o nome do Python", file=sys.stderr)
        return None
    app = Path.home() / "Applications" / "Baton.app"
    # os marcadores ficam FORA do pacote: arquivo novo dentro do .app depois do codesign invalida a assinatura
    # ("a sealed resource is missing or invalid") e o macOS nao lista o app em Acessibilidade
    exe, marca = app / "Contents" / "MacOS" / "Baton", MARCA_APP
    marca.parent.mkdir(parents=True, exist_ok=True)
    h = hashlib.sha1(fonte.read_bytes() + INFO_PLIST_APP.encode()).hexdigest()[:10]
    antigo = marca.read_text() if marca.exists() else ""
    esquema_novo = not (ARQ_ESQUEMA.exists() and ARQ_ESQUEMA.read_text() == ESQUEMA_ASSINATURA)
    if not forcar and exe.exists() and antigo == h and not esquema_novo:
        return str(exe)  # nao recompila: assinatura estavel mantem a permissao ja concedida
    (app / "Contents" / "MacOS").mkdir(parents=True, exist_ok=True)
    (app / "Contents" / "Resources").mkdir(parents=True, exist_ok=True)
    # alvo fixo (macOS 12+): sem isso o swiftc usa a versao do SDK instalado, que pode ser mais nova que o macOS em uso e o
    # Finder/Ajustes recusam o app ("You can't use this version of the application with this version of macOS")
    try:  # arquitetura do hardware (um Python sob Rosetta diria x86_64 num Mac arm64)
        arm = subprocess.run(["sysctl", "-n", "hw.optional.arm64"], capture_output=True, text=True, timeout=5).stdout.strip() == "1"
    except Exception:
        arm = platform.machine() == "arm64"
    alvo = ["-target", f"{'arm64' if arm else 'x86_64'}-apple-macos12.0"]
    r = subprocess.run([swiftc, "-O", *alvo, "-DBANDEJA", str(fonte), "-o", str(exe)], capture_output=True, text=True)
    bandeja = r.returncode == 0
    if not bandeja:  # sem o icone da barra de menu, mas com o launcher (permissoes) funcionando
        print("aviso: sem icone na barra de menu (falha ao compilar a bandeja):\n" + r.stderr[-1200:], file=sys.stderr)
        r = subprocess.run([swiftc, "-O", *alvo, str(fonte), "-o", str(exe)], capture_output=True, text=True)
    if r.returncode != 0:
        print("aviso: falha ao compilar launcher.swift:\n" + r.stderr[-800:], file=sys.stderr)
        return None
    (app / "Contents" / "Info.plist").write_text(INFO_PLIST_APP)
    if shutil.which("iconutil"):
        try:
            with tempfile.TemporaryDirectory() as d:
                iset = Path(d) / "Baton.iconset"
                iset.mkdir()
                for tam in (16, 32, 128, 256, 512):
                    (iset / f"icon_{tam}x{tam}.png").write_bytes(icone_png(tam))
                    (iset / f"icon_{tam}x{tam}@2x.png").write_bytes(icone_png(min(1024, tam * 2)))
                subprocess.run(["iconutil", "-c", "icns", str(iset), "-o", str(app / "Contents" / "Resources" / "Baton.icns")],
                               check=False, capture_output=True)
        except Exception:
            pass
    fixo = assinar_app(app)
    if fixo and esquema_novo:  # so na migracao: a entrada antiga esta presa ao hash do binario antigo e nunca mais vale
        subprocess.run(["tccutil", "reset", "Accessibility", "com.baton.app"], capture_output=True)
        print("Acessibilidade: a assinatura agora e estavel; libere o Baton uma ultima vez e ela passa a sobreviver as atualizacoes.", flush=True)
    if fixo:
        ARQ_ESQUEMA.write_text(ESQUEMA_ASSINATURA)
    elif not forcar and not antigo:
        subprocess.run(["tccutil", "reset", "Accessibility", "com.baton.app"], capture_output=True)
    marca.write_text(h)
    MARCA_APP_BANDEJA.write_text("1" if bandeja else "0")
    v = subprocess.run(["codesign", "--verify", "--deep", "--strict", str(app)], capture_output=True, text=True) if shutil.which("codesign") else None
    if v is not None and v.returncode != 0:
        print("aviso: assinatura do Baton.app invalida: " + (v.stderr or "").strip()[-300:], file=sys.stderr)
    print("Baton.app compilado " + ("(com icone na barra de menu)" if bandeja else "(SEM icone na barra de menu)"), flush=True)
    return str(exe)


def diagnostico():
    """Resumo para colar numa conversa: versao, Baton.app, assinatura, swiftc, launchd, log e permissao."""
    def run(*cmd):
        try:
            r = subprocess.run(list(cmd), capture_output=True, text=True, timeout=10)
            return ((r.stdout or "") + (r.stderr or "")).strip()
        except Exception as e:
            return f"(falhou: {e})"
    aqui = Path(__file__).resolve().parent
    versao = (aqui / "VERSION").read_text().strip() if (aqui / "VERSION").exists() else "(sem VERSION: instalado antes do suporte a versao)"
    L = [f"baton.py: {Path(__file__).resolve()} | versao: {versao}",
         f"launcher.swift: {hashlib.sha1((aqui / 'launcher.swift').read_bytes()).hexdigest()[:10] if (aqui / 'launcher.swift').exists() else 'AUSENTE'}",
         f"tray: {'sim' if (aqui / 'tray_gtk.py').exists() else 'nao'} | python: {sys.version.split()[0]} | sistema: {sys.platform}"]
    if sys.platform == "darwin":
        app = Path.home() / "Applications" / "Baton.app"
        exe = app / "Contents" / "MacOS" / "Baton"
        L += [f"Baton.app: {'existe' if app.exists() else 'NAO EXISTE'} | executavel: {'ok' if exe.exists() else 'NAO EXISTE'}",
              f"swiftc: {shutil.which('swiftc') or 'NAO ENCONTRADO (xcode-select --install)'}",
              "executavel compilado em: " + run("stat", "-f", "%Sm", str(exe)),
              "Baton.app atualizado: " + ("sim" if MARCA_APP.exists() and MARCA_APP.read_text() == hashlib.sha1((aqui / "launcher.swift").read_bytes() + INFO_PLIST_APP.encode()).hexdigest()[:10] else "NAO (rode: baton --recompilar-app)"),
              "icone na barra de menu: " + {"1": "compilado", "0": "NAO compilado (veja o erro em: baton --recompilar-app)"}.get(
                  MARCA_APP_BANDEJA.read_text().strip() if MARCA_APP_BANDEJA.exists() else "", "desconhecido"),
              "assinatura valida: " + (run("codesign", "--verify", "--deep", "--strict", str(app)) or "sim"),
              "bundle id: " + run("/usr/libexec/PlistBuddy", "-c", "Print :CFBundleIdentifier", str(app / "Contents" / "Info.plist")),
              "requisito: " + (run("codesign", "-d", "-r-", str(app)).split("designated =>")[-1].strip()[:90] or "?"),
              "assinatura: " + " | ".join(x for x in run("codesign", "-dv", str(app)).splitlines() if x.startswith(("Identifier", "Signature", "CodeDirectory"))),
              "launchd: " + (run("launchctl", "list", "com.baton") or "(nao carregado)").replace("\n", " ")[:200],
              "processos: " + (run("pgrep", "-fli", "baton") or "(nenhum)").replace("\n", " ; ")[:200],
              f"acessibilidade do Baton.app (o que importa): {acesso_do_launcher()} (None = launcher parado)",
              f"acessibilidade deste terminal (nao conta): {acessibilidade_mac()}"]
        L += ["macOS: " + run("sw_vers", "-productVersion") + " | arquitetura: " + run("uname", "-m"),
              "quarentena: " + (run("xattr", str(app)) or "(nenhuma)").replace("\n", " "),
              "binario: " + run("file", "-b", str(exe))[:120],
              "versao minima do macOS no binario: " + " ".join(x.strip() for x in run("otool", "-l", str(exe)).splitlines()
                                                              if x.strip().startswith(("minos", "sdk")) )[:80],
              "TCC (ultimos 10 min, com.baton.app):\n" + (run("log", "show", "--last", "10m", "--style", "compact", "--predicate",
                                                                 'eventMessage CONTAINS "com.baton.app"') or "(nada)").replace("Filtering the log data using", "")[-900:]]
        log = Path.home() / ".cache" / "baton" / "baton.log"
        L.append("log (ultimas linhas):\n" + "\n".join(log.read_text(errors="replace").splitlines()[-12:]) if log.exists() else "log: ausente")
    return "\n".join(L)


def reiniciar_baton_mac():
    """Reinicia o Baton.app pelo launchd (assim o pedido de Acessibilidade e atribuido ao proprio app, nao ao terminal)."""
    plist = Path.home() / "Library" / "LaunchAgents" / "com.baton.plist"
    r = subprocess.run(["launchctl", "kickstart", "-k", f"gui/{os.getuid()}/com.baton"], capture_output=True, text=True)
    if r.returncode != 0 and plist.exists():
        subprocess.run(["launchctl", "unload", str(plist)], capture_output=True)
        subprocess.run(["launchctl", "load", str(plist)], capture_output=True)


def pedir_acessibilidade(resetar=False):
    """macOS: garante o Baton.app, limpa o registro antigo da permissao (cada recompilacao ad-hoc muda a assinatura e deixa
    a entrada velha 'ligada' mas sem efeito), reinicia o app pelo launchd para ele pedir a permissao e abre os Ajustes."""
    if sys.platform != "darwin":
        return "so no macOS"
    app = Path.home() / "Applications" / "Baton.app"
    exe = montar_app_mac()
    if not exe or not app.exists():
        return ("nao consegui montar o Baton.app (precisa do swiftc: rode 'xcode-select --install' e tente de novo); "
                "veja ~/.cache/baton/baton.log")
    if acesso_do_launcher() is True:
        return "o Baton.app ja esta liberado em Acessibilidade (o launcher confirma)."
    # abre o Finder e os Ajustes antes de reiniciar: quando chamado pelo painel, o reinicio encerra este proprio processo
    subprocess.Popen(["open", "-R", str(app)])
    subprocess.Popen(["open", "x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility"])
    if resetar:  # so por pedido explicito (--permissao --resetar): zerar apaga a entrada que o usuario acabou de adicionar
        subprocess.run(["tccutil", "reset", "Accessibility", "com.baton.app"], capture_output=True)
    reiniciar_baton_mac()
    return ("pedi a permissao pelo Baton.app (reiniciado pelo launchd). Na lista de Acessibilidade ative o Baton; se ele nao "
            f"aparecer, clique em + e escolha {app} (ja mostrado no Finder). Depois rode: baton --diagnostico")


def instalar_login(remover=False):
    """Inicia o Baton no login (como o receptor USB do passador de slides): launchd, Inicializar do Windows ou autostart."""
    script = os.path.abspath(__file__)
    if sys.platform == "win32":
        pasta = Path(os.environ.get("APPDATA", str(Path.home()))) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"
        alvo = pasta / "baton.bat"
        if remover:
            alvo.unlink(missing_ok=True)
            return print("baton removido da inicializacao")
        pythonw = Path(sys.executable).with_name("pythonw.exe")
        py = pythonw if pythonw.exists() else Path(sys.executable)
        pasta.mkdir(parents=True, exist_ok=True)
        alvo.write_text(f'@echo off\r\nstart "" /b "{py}" "{script}" --residente\r\n')
        return print(f"instalado: {alvo}\nAgora ele liga sozinho no login do Windows.")
    if sys.platform.startswith("linux"):
        alvo = Path.home() / ".config" / "autostart" / "baton.desktop"
        if remover:
            alvo.unlink(missing_ok=True)
            return print("baton removido do autostart")
        alvo.parent.mkdir(parents=True, exist_ok=True)
        alvo.write_text("[Desktop Entry]\nType=Application\nName=Baton\n"
                        f"Exec={sys.executable} {script} --residente\nX-GNOME-Autostart-enabled=true\n")
        return print(f"instalado: {alvo}\nAgora ele liga sozinho no login (sessao grafica X11).")
    if sys.platform != "darwin":
        sys.exit("--instalar funciona no macOS, Windows e Linux")
    plist = Path.home() / "Library" / "LaunchAgents" / "com.baton.plist"
    if remover:
        subprocess.run(["launchctl", "unload", str(plist)], check=False)
        plist.unlink(missing_ok=True)
        shutil.rmtree(Path.home() / "Applications" / "Baton.app", ignore_errors=True)
        print("baton removido do login")
        return
    log = Path.home() / ".cache" / "baton" / "baton.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    plist.parent.mkdir(parents=True, exist_ok=True)
    app = montar_app_mac()
    args = ([app] if app else []) + [sys.executable, script, "--residente"]
    plist.write_text(plist_launchd(args, log))
    subprocess.run(["launchctl", "unload", str(plist)], check=False)
    subprocess.run(["launchctl", "load", str(plist)], check=False)
    print(f"instalado: {plist}\nAgora ele liga sozinho no login.")
    if app:
        print("Libere o \"Baton\" em Ajustes do Sistema > Privacidade e Seguranca > Acessibilidade "
              "(ele aparece na lista sozinho; se nao, clique em + e escolha ~/Applications/Baton.app).")
    else:
        print("Libere o Python em Acessibilidade na primeira vez.")


def token_estavel(novo=False):
    """O token fica salvo: se o script reiniciar, a pagina aberta no celular continua valendo."""
    f = Path.home() / ".cache" / "baton" / "token"
    try:
        if not novo and f.exists() and len(f.read_text().strip()) >= 8:
            return f.read_text().strip()
        f.parent.mkdir(parents=True, exist_ok=True)
        t = secrets.token_urlsafe(9)
        f.write_text(t)
        os.chmod(f, 0o600)
        return t
    except OSError:
        return secrets.token_urlsafe(9)


def _ip_valido(s):
    import ipaddress
    try:
        return str(ipaddress.IPv4Address(s))
    except ValueError:
        return None


def ip_tailscale():
    """IP da tailnet: pela CLI do Tailscale (so aceita IPv4 valido, a CLI pode imprimir erro) ou pelas interfaces (100.64.0.0/10)."""
    candidatos = [shutil.which("tailscale"), "/Applications/Tailscale.app/Contents/MacOS/Tailscale",
                  r"C:\Program Files\Tailscale\tailscale.exe", r"C:\Program Files (x86)\Tailscale\tailscale.exe"]
    for exe in candidatos:
        if exe and os.path.exists(exe):
            try:
                for item in subprocess.run([exe, "ip", "-4"], capture_output=True, text=True, timeout=5).stdout.split():
                    if _ip_valido(item):
                        return item
            except Exception:
                pass
    for cmd in (["ifconfig"], ["ip", "-o", "-4", "addr"], ["ipconfig"]):
        if shutil.which(cmd[0]):
            try:
                saida = subprocess.run(cmd, capture_output=True, text=True, timeout=5).stdout
                for m in re.finditer(r"\b100\.(6[4-9]|[7-9]\d|1[01]\d|12[0-7])\.\d{1,3}\.\d{1,3}\b", saida):
                    if _ip_valido(m.group(0)):
                        return m.group(0)
            except Exception:
                pass
    return None


IGNORAR_IFACE = ("lo", "docker", "veth", "br-", "virbr", "vmnet", "vboxnet", "utun", "tailscale", "awdl", "llw", "gif", "stf",
                 "vethernet", "loopback", "wsl")


def parse_interfaces(saida):
    """Saida do ifconfig (macOS), `ip -o -4 addr` (Linux) ou ipconfig (Windows) -> [(interface, ip)] sem filtrar."""
    achados, atual = [], ""
    for linha in saida.splitlines():
        m = re.match(r"^\d+:\s+(\S+)\s+inet\s+(\d+\.\d+\.\d+\.\d+)", linha)  # ip -o -4 addr
        if m:
            achados.append((m.group(1), m.group(2)))
            continue
        m = re.match(r"^(\S[^:]*):\s*(flags=.*)?$", linha)  # "en0: flags=..." (ifconfig) ou "Adaptador ...:" (ipconfig)
        if m and not linha.startswith((" ", "\t")):
            atual = m.group(1).strip()
            continue
        m = re.search(r"\binet\s+(\d+\.\d+\.\d+\.\d+)", linha) or re.search(r"IPv4.*?:\s*(\d+\.\d+\.\d+\.\d+)", linha)
        if m and atual:
            achados.append((atual, m.group(1)))
    return achados


def contexto_tls():
    """Contexto TLS com certificado autoassinado (gerado uma vez com o openssl) ou None. O iOS e o Chrome so liberam os sensores
    de movimento (laser por apontamento) em paginas HTTPS; o celular mostra um aviso de certificado na primeira vez."""
    import ssl
    pasta = Path.home() / ".cache" / "baton" / "tls"
    cert, chave = pasta / "cert.pem", pasta / "key.pem"
    if not (cert.exists() and chave.exists()):
        if not shutil.which("openssl"):
            return None
        pasta.mkdir(parents=True, exist_ok=True)
        base = ["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", str(chave), "-out", str(cert), "-days", "825",
                "-subj", "/CN=baton"]
        for extra in (["-addext", "subjectAltName=DNS:baton.local"], []):
            if subprocess.run(base + extra, capture_output=True, timeout=60).returncode == 0:
                break
        if not (cert.exists() and chave.exists()):
            return None
        try:
            os.chmod(chave, 0o600)
        except OSError:
            pass
    try:
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(str(cert), str(chave))
        return ctx
    except (ssl.SSLError, OSError):
        return None


MONITOR_FN = None  # () -> monitor da projecao; a previa por captura mostra so ele


def capturar_png(destino, mon=None):
    """PNG sem perda do monitor da projecao (fonte da lupa). True se gravou o arquivo."""
    teste = os.environ.get("BATON_CAPTURA_PNG")  # PNG fixo, para testes
    if teste:
        try:
            shutil.copyfile(teste, destino)
            return True
        except OSError:
            return False
    try:
        from PIL import ImageGrab
        (ImageGrab.grab(bbox=(mon["x"], mon["y"], mon["x"] + mon["w"], mon["y"] + mon["h"])) if mon else ImageGrab.grab()).convert("RGB").save(destino, "PNG")
        return True
    except Exception:
        pass
    arq = str(destino)
    try:
        if sys.platform == "darwin":
            subprocess.run(["screencapture", "-x", "-t", "png"] + (["-R", f"{mon['x']},{mon['y']},{mon['w']},{mon['h']}"] if mon else []) + [arq],
                           capture_output=True, timeout=8)
        elif sys.platform == "win32":
            ps = ("Add-Type -AssemblyName System.Windows.Forms,System.Drawing;"
                  "Add-Type -MemberDefinition '[DllImport(\"user32.dll\")] public static extern bool SetProcessDPIAware();' -Name D -Namespace W;"
                  "[void][W.D]::SetProcessDPIAware();" +
                  (f"$b=New-Object System.Drawing.Rectangle {mon['x']},{mon['y']},{mon['w']},{mon['h']};" if mon else "$b=[System.Windows.Forms.Screen]::PrimaryScreen.Bounds;") +
                  "$bmp=New-Object System.Drawing.Bitmap $b.Width,$b.Height;"
                  "$g=[System.Drawing.Graphics]::FromImage($bmp);$g.CopyFromScreen($b.Location,[System.Drawing.Point]::Empty,$b.Size);"
                  f"$bmp.Save('{arq}',[System.Drawing.Imaging.ImageFormat]::Png)")
            subprocess.run(["powershell", "-NoProfile", "-WindowStyle", "Hidden", "-Command", ps], capture_output=True, timeout=15, creationflags=0x08000000)
        else:
            g = f"{mon['w']}x{mon['h']}+{mon['x']}+{mon['y']}" if mon else ""
            for cmd in ((["grim"] + (["-g", f"{mon['x']},{mon['y']} {mon['w']}x{mon['h']}"] if mon else []) + [arq]) if os.environ.get("WAYLAND_DISPLAY") else None,
                        ["import", "-silent", "-window", "root"] + (["-crop", g, "+repage"] if mon else []) + [arq],
                        ["maim", "-m", "7"] + (["-g", g] if mon else []) + [arq],
                        ["scrot", "-o"] + (["-a", f"{mon['x']},{mon['y']},{mon['w']},{mon['h']}"] if mon else []) + [arq]):
                if cmd and shutil.which(cmd[0]):
                    subprocess.run(cmd, capture_output=True, timeout=8)
                    if Path(arq).exists() and Path(arq).stat().st_size > 0:
                        break
    except (OSError, subprocess.TimeoutExpired):
        return False
    return Path(arq).exists() and Path(arq).stat().st_size > 0


def capturar_tela(largura=960, qualidade=60):
    """JPEG da tela principal (para a previa do celular quando nao ha PDF) ou None se nao ha como capturar.
    Usa o Pillow se existir; senao ferramentas do sistema (screencapture, PowerShell, grim, import, maim, scrot)."""
    teste = os.environ.get("BATON_CAPTURA")  # arquivo JPEG fixo, para testes
    if teste:
        try:
            return Path(teste).read_bytes()
        except OSError:
            return None
    try:
        mon = MONITOR_FN() if MONITOR_FN else None
    except Exception:
        mon = None
    try:
        from PIL import ImageGrab
        import io
        img = (ImageGrab.grab(bbox=(mon["x"], mon["y"], mon["x"] + mon["w"], mon["y"] + mon["h"])) if mon else ImageGrab.grab()).convert("RGB")
        img.thumbnail((largura, largura))
        buf = io.BytesIO()
        img.save(buf, "JPEG", quality=qualidade)
        return buf.getvalue()
    except Exception:
        pass
    with tempfile.TemporaryDirectory() as d:
        arq = Path(d) / "t.jpg"
        q = str(qualidade)
        try:
            if sys.platform == "darwin":
                subprocess.run(["screencapture", "-x", "-t", "jpg"] + (["-R", f"{mon['x']},{mon['y']},{mon['w']},{mon['h']}"] if mon else []) + [str(arq)],
                               capture_output=True, timeout=8)
                subprocess.run(["sips", "-Z", str(largura), "--setProperty", "formatOptions", q, str(arq)], capture_output=True, timeout=8)
            elif sys.platform == "win32":
                ps = ("Add-Type -AssemblyName System.Windows.Forms,System.Drawing;"
                      "Add-Type -MemberDefinition '[DllImport(\"user32.dll\")] public static extern bool SetProcessDPIAware();' -Name D -Namespace W;"
                      "[void][W.D]::SetProcessDPIAware();" +
                      (f"$b=New-Object System.Drawing.Rectangle {mon['x']},{mon['y']},{mon['w']},{mon['h']};" if mon else "$b=[System.Windows.Forms.Screen]::PrimaryScreen.Bounds;") +
                      "$bmp=New-Object System.Drawing.Bitmap $b.Width,$b.Height;"
                      "$g=[System.Drawing.Graphics]::FromImage($bmp);$g.CopyFromScreen($b.Location,[System.Drawing.Point]::Empty,$b.Size);"
                      f"$h=[int]($b.Height*{largura}/$b.Width);$o=New-Object System.Drawing.Bitmap $bmp,{largura},$h;"
                      "$c=[System.Drawing.Imaging.ImageCodecInfo]::GetImageEncoders()|Where-Object{$_.MimeType -eq 'image/jpeg'};"
                      f"$p=New-Object System.Drawing.Imaging.EncoderParameters 1;$p.Param[0]=New-Object System.Drawing.Imaging.EncoderParameter ([System.Drawing.Imaging.Encoder]::Quality),{q}L;"
                      f"$o.Save('{arq}',$c,$p)")
                subprocess.run(["powershell", "-NoProfile", "-WindowStyle", "Hidden", "-Command", ps], capture_output=True,
                               timeout=15, creationflags=0x08000000)
            else:
                g = f"{mon['w']}x{mon['h']}+{mon['x']}+{mon['y']}" if mon else ""
                for cmd in ((["grim", "-t", "jpeg", "-q", q, "-s", "0.5"] + (["-g", f"{mon['x']},{mon['y']} {mon['w']}x{mon['h']}"] if mon else []) + [str(arq)]
                             if os.environ.get("WAYLAND_DISPLAY") else None),
                            ["import", "-silent", "-window", "root"] + (["-crop", g, "+repage"] if mon else []) + ["-resize", f"{largura}x", "-quality", q, str(arq)],
                            ["maim", "-f", "jpg", "-m", "7"] + (["-g", g] if mon else []) + [str(arq)],
                            ["scrot", "-q", q] + (["-a", f"{mon['x']},{mon['y']},{mon['w']},{mon['h']}"] if mon else []) + [str(arq)],
                            ["gnome-screenshot", "-f", str(arq)]):
                    if cmd and shutil.which(cmd[0]):
                        subprocess.run(cmd, capture_output=True, timeout=8)
                        if arq.exists() and arq.stat().st_size > 0:
                            break
            return arq.read_bytes() if arq.exists() and arq.stat().st_size > 0 else None
        except (OSError, subprocess.TimeoutExpired):
            return None


_CAPTURA = {"t": 0.0, "dados": None, "lock": threading.Lock()}


def tela_em_cache(validade=1.2):
    """Ultima captura (no maximo uma a cada `validade` s, mesmo com varios celulares pedindo)."""
    with _CAPTURA["lock"]:
        if time.time() - _CAPTURA["t"] > validade:
            _CAPTURA["dados"], _CAPTURA["t"] = capturar_tela(), time.time()
        return _CAPTURA["dados"]


def navegadores_chromium():
    """Navegadores baseados em Chromium instalados (aceitam --kiosk e --app)."""
    cand = []
    if sys.platform == "darwin":
        for n in ("Google Chrome", "Microsoft Edge", "Brave Browser", "Chromium"):
            cand.append(f"/Applications/{n}.app/Contents/MacOS/{n}")
    elif sys.platform == "win32":
        for var in ("ProgramFiles", "ProgramFiles(x86)", "LocalAppData"):
            raiz = os.environ.get(var)
            if raiz:
                for rel in (r"Google\Chrome\Application\chrome.exe", r"Microsoft\Edge\Application\msedge.exe",
                            r"BraveSoftware\Brave-Browser\Application\brave.exe"):
                    cand.append(os.path.join(raiz, rel))
    else:
        for n in ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser", "microsoft-edge", "brave-browser"):
            cand.append(shutil.which(n))
    return [c for c in cand if c and os.path.exists(c)]


def abrir_tela_cheia(url, mon=None):
    """Abre a URL em tela cheia (quiosque) num navegador Chromium com perfil proprio; devolve o processo, ou None se so deu
    para abrir no navegador padrao (a pagina entao entra em tela cheia no primeiro toque/tecla)."""
    if os.environ.get("BATON_SEM_TELA"):  # testes: nao abre janela nem navegador
        return None
    perfil = Path.home() / ".cache" / "baton" / "navegador"
    for exe in navegadores_chromium():
        try:
            # --start-fullscreen (e nao --kiosk): o kiosk tranca a janela e nem Esc nem F11 saem; assim a pagina fecha com Esc/botao,
            # e F11 e Alt+F4 continuam funcionando
            pos = [f"--window-position={mon['x']},{mon['y']}", f"--window-size={mon['w']},{mon['h']}"] if mon else []  # abre no monitor da projecao
            return subprocess.Popen([exe, f"--app={url}", *pos, "--start-fullscreen", f"--user-data-dir={perfil}", "--no-first-run",
                                     "--no-default-browser-check", "--disable-infobars", "--autoplay-policy=no-user-gesture-required"],
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL,
                                    start_new_session=(os.name == "posix"))
        except OSError:
            continue
    try:
        webbrowser.open(url + "?fs=1")
    except Exception:
        pass
    return None


def faixa_http(cabecalho, total):
    """Range 'bytes=a-b' -> (a, b) inclusivo; None se nao ha Range; 'invalida' se fora do arquivo."""
    if not cabecalho:
        return None
    m = re.fullmatch(r"\s*bytes=(\d*)-(\d*)\s*", cabecalho)
    if not m or (m.group(1) == "" and m.group(2) == ""):
        return None
    if m.group(1) == "":
        n = int(m.group(2))
        return (max(0, total - n), total - 1) if n > 0 and total else "invalida"
    a = int(m.group(1))
    b = int(m.group(2)) if m.group(2) else total - 1
    if a >= total or b < a:
        return "invalida"
    return (a, min(b, total - 1))


def portas_mac():
    """macOS: {dispositivo: nome da porta} pelo networksetup ('en6' -> 'Bluetooth PAN'); a PAN nao se chama bnep no Mac."""
    if sys.platform != "darwin" or not shutil.which("networksetup"):
        return {}
    try:
        saida = subprocess.run(["networksetup", "-listallhardwareports"], capture_output=True, text=True, timeout=5).stdout
    except Exception:
        return {}
    return {m.group(2): m.group(1).strip() for m in re.finditer(r"Hardware Port:\s*(.+)\nDevice:\s*(\S+)", saida)}


def rotulo_interface(nome, ip, portas=None):
    """Nome amigavel do caminho (ou None para o que nao serve: loopback, Docker, VPN, Tailscale)."""
    porta = ((portas or {}).get(nome) or "").lower()
    if "bluetooth" in porta:
        return f"Bluetooth ({nome})"
    if "iphone" in porta or "hotspot" in porta or "android" in porta:
        return f"Hotspot ({nome})"
    n = nome.lower()
    if (n.startswith(IGNORAR_IFACE) or any(k in n for k in ("vethernet", "wsl", "docker", "vmware", "virtualbox", "loopback", "tailscale"))
            or ip.startswith("127.") or re.match(r"100\.(6[4-9]|[7-9]\d|1[01]\d|12[0-7])\.", ip)):
        return None
    if "bnep" in n or "bluetooth" in n:
        return f"Bluetooth ({nome})"
    if n.startswith(("bridge", "ap", "ppp")) or "hotspot" in n or "hospedada" in n:
        return f"Hotspot ({nome})"
    return f"Rede ({nome})"


def ips_locais():
    """[(rotulo, ip)] de todas as redes do computador (Wi-Fi, cabo, Bluetooth, hotspot): funciona sem internet nem roteador."""
    saida = ""
    for cmd in (["ifconfig"], ["ip", "-o", "-4", "addr"], ["ipconfig"]):
        if shutil.which(cmd[0]):
            try:
                saida = subprocess.run(cmd, capture_output=True, text=True, timeout=5).stdout
                if saida:
                    break
            except Exception:
                pass
    vistos, lista, portas = set(), [], portas_mac()
    for iface, ip in parse_interfaces(saida):
        r = rotulo_interface(iface, ip, portas)
        if r and _ip_valido(ip) and ip not in vistos:
            vistos.add(ip)
            lista.append((r, ip))
    return lista


_TS_CACHE = [0.0, None]


def ip_tailscale_cache(validade=30):
    if time.time() - _TS_CACHE[0] > validade:
        _TS_CACHE[:] = [time.time(), ip_tailscale()]
    return _TS_CACHE[1]


def e_rede_lenta(rotulo):
    return rotulo.startswith(("Bluetooth", "Hotspot"))


URL_PUBLICA = ""  # ex.: https://baton.exemplo.com (Cloudflare Tunnel); vem de --url-publica, BATON_URL_PUBLICA ou do arquivo salvo
ARQ_URL_PUBLICA = Path.home() / ".cache" / "baton" / "url-publica"


def url_publica_salva():
    try:
        return ARQ_URL_PUBLICA.read_text().strip()
    except OSError:
        return ""


def salvar_url_publica(url):
    """Grava (ou, com '-', apaga) a URL publica: o Baton iniciado no login (--residente) passa a usa-la como padrao."""
    try:
        if url in ("", "-"):
            ARQ_URL_PUBLICA.unlink(missing_ok=True)
        else:
            ARQ_URL_PUBLICA.parent.mkdir(parents=True, exist_ok=True)
            ARQ_URL_PUBLICA.write_text(url.strip().rstrip("/") + "\n")
    except OSError:
        pass


RELAY_PADRAO = "wss://baton.phrandrade.com"  # relay publico e aberto (sem login, sem coleta); desligue com --relay - ou no painel
ARQ_RELAY = Path.home() / ".cache" / "baton" / "relay.json"
ARQ_CHAVE_RELAY = Path.home() / ".config" / "baton" / "relay-chave"


def relay_salvo():
    """Relay padrao: o salvo com --instalar ou o relay.json que acompanha o pacote (ao lado do baton.py; fora do git, com a chave).
    O usuario final nao configura nada: abre o app e o Baton ja se registra no relay."""
    for arq in (ARQ_RELAY, Path(__file__).resolve().parent / "relay.json"):
        try:
            d = json.loads(arq.read_text())
            if d.get("url"):
                return str(d["url"]), str(d.get("chave", ""))
        except (OSError, ValueError):
            continue
    return "", ""


def salvar_relay(url, chave):
    """Grava (ou, com '-', apaga) o relay padrao do Baton iniciado no login (--residente)."""
    try:
        if url in ("", "-"):
            ARQ_RELAY.unlink(missing_ok=True)
        else:
            ARQ_RELAY.parent.mkdir(parents=True, exist_ok=True)
            ARQ_RELAY.write_text(json.dumps({"url": url.strip().rstrip("/"), "chave": chave}))
            os.chmod(ARQ_RELAY, 0o600)
    except OSError:
        pass


def resgatar_convite_do_pacote():
    """convite.txt ao lado do baton.py (pacote feito com --convite): usa uma vez, no primeiro uso, e apaga."""
    arq = Path(__file__).resolve().parent / "convite.txt"
    if not arq.exists() or relay_salvo()[1]:
        return
    try:
        url, chave = relay_mod.resgatar_convite(arq.read_text(encoding="utf-8"))
        salvar_relay(url, chave)
        arq.unlink(missing_ok=True)
        print(f"Convite aceito: este computador entra no relay {url}.", file=sys.stderr)
    except ValueError:
        arq.unlink(missing_ok=True)  # convite expirado/usado: nao tenta de novo
    except OSError:
        pass  # sem rede agora: tenta na proxima vez


def chave_do_relay(pedida=""):
    if pedida:
        return pedida
    if os.environ.get("BATON_RELAY_CHAVE"):
        return os.environ["BATON_RELAY_CHAVE"]
    try:
        return ARQ_CHAVE_RELAY.read_text().strip()
    except OSError:
        return ""


RELAY_AGENTE = None  # AgenteRelay deste Baton (definido no main)
_PUBLICA = {"t": 0.0, "ok": True, "rodando": False}


def publica_viva():
    """True se a URL publica deve ser oferecida: com relay, so enquanto o agente esta conectado; sem relay, se um teste HTTP
    recente (feito aqui, em segundo plano, a cada 30 s) alcancou a URL. Na rede do instituto (internet fraca ou filtrada)
    ela some do QR e do redirecionamento, e o Baton segue pela rede local."""
    if not URL_PUBLICA:
        return False
    if RELAY_AGENTE is not None:
        return getattr(RELAY_AGENTE, "status", "") == "conectado"
    agora = time.monotonic()
    if agora - _PUBLICA["t"] > 30 and not _PUBLICA["rodando"]:
        _PUBLICA["rodando"] = True

        def _testar():
            try:
                urllib.request.urlopen(URL_PUBLICA.rstrip("/") + "/icon.png", timeout=3).read(1)
                _PUBLICA["ok"] = True
            except urllib.error.HTTPError:
                _PUBLICA["ok"] = True  # respondeu (o servidor existe, mesmo com 403/404)
            except Exception:
                _PUBLICA["ok"] = False
            _PUBLICA["t"], _PUBLICA["rodando"] = time.monotonic() - (0 if _PUBLICA["ok"] else 25), False  # falhou: tenta de novo em 5 s
        threading.Thread(target=_testar, daemon=True).start()
        _PUBLICA["t"] = agora - 25  # reavalia em 5 s se ainda nao respondeu
    return _PUBLICA["ok"]


def calcular_bases(porta, host=None):
    """Enderecos do computador, do melhor para o pior: Bluetooth > hotspot do celular > (internet HTTPS, se configurada) >
    Wi-Fi/rota padrao > outras > Tailscale."""
    bases = _bases_locais(porta, host)
    if URL_PUBLICA:
        pub = {"nome": "Internet (HTTPS)", "base": URL_PUBLICA.rstrip("/")}
        if not publica_viva():  # sem internet ou relay fora do ar: a rede local vem primeiro e a URL publica fica por ultimo
            pub["nome"] = "Internet (HTTPS) - indisponivel agora"
            bases.append(pub)
            return bases
        i = 0
        while i < len(bases) and bases[i]["nome"].startswith("Bluetooth"):
            i += 1
        bases.insert(i, pub)
    return bases


def _bases_locais(porta, host=None):
    if host:
        return [{"nome": "Rede local", "base": f"http://{host}:{porta}"}]
    rota = ip_lan()

    def prioridade(item):
        r, ip = item
        return 0 if r.startswith("Bluetooth") else 1 if r.startswith("Hotspot") else 2 if ip == rota else 3
    locais = sorted(ips_locais(), key=prioridade)
    bases = []
    for r, ip in locais[:4]:
        nome = "Wi-Fi / rede local" if (ip == rota and not e_rede_lenta(r)) else r
        if r.startswith("Bluetooth"):
            nome = r + " - recomendado sem internet"
        bases.append({"nome": nome, "base": f"http://{ip}:{porta}"})
    if rota and rota not in [b["base"].split("//")[1].split(":")[0] for b in bases]:
        bases.insert(0, {"nome": "Wi-Fi / rede local", "base": f"http://{rota}:{porta}"})
    ts = ip_tailscale_cache()
    if ts:
        bases.append({"nome": "Tailscale (fora do Wi-Fi)", "base": f"http://{ts}:{porta}"})
    return bases or [{"nome": "Rede local", "base": f"http://IP-DO-COMPUTADOR:{porta}"}]


def rede_lenta_do_ip(ip_local):
    """True se o IP (o da interface por onde o celular se conectou) e de uma rede Bluetooth/hotspot."""
    return any(e_rede_lenta(r) and i == ip_local for r, i in ips_locais())


def ip_lan():
    """IP do computador na rede local (Wi-Fi/cabo): descobre pela rota padrao, sem enviar nenhum pacote."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sk:
            sk.connect(("192.0.2.1", 9))
            return _ip_valido(sk.getsockname()[0])
    except OSError:
        return None


PAGINA_BASE = """<!doctype html><html lang="pt-br"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover,user-scalable=no">
<meta name="theme-color" content="#000"><title>__TITULO__</title>
<link rel="manifest" href="/manifest.json?k=__TOKEN__"><link rel="apple-touch-icon" href="/icon.png">
<meta name="apple-mobile-web-app-capable" content="yes"><meta name="apple-mobile-web-app-status-bar-style" content="black"><meta name="apple-mobile-web-app-title" content="Baton">
<script src="__PDFJS__pdf.min.js"></script>
<script>if(typeof pdfjsLib==='undefined'){window.PDFJS_FALHA=true;window.pdfjsLib={GlobalWorkerOptions:{},getDocument:function(){return{promise:Promise.reject(new Error('pdf.js nao carregou'))}}}}  // sem o leitor de PDF a pagina segue conectada e avisa</script>
<script src="__QRJS__"></script>
<style>__CSS__</style></head><body class="__CLASSE__">__HTML__
<script>
const BASE="__PDFJS__", TOKEN="__TOKEN__", REMOTE_URL="__REMOTE__", URLS=__URLS__, TEM_PDF=__TEMPDF__, LEVE=__LEVE__, EXTERNO=__EXTERNO__, OVERLAY=__OVERLAY__;
pdfjsLib.GlobalWorkerOptions.workerSrc = BASE+"pdf.worker.min.js";
__JS__
</script></body></html>"""

CSS_COMUM = """
*{box-sizing:border-box;margin:0;padding:0;-webkit-tap-highlight-color:transparent}
html,body{height:100%;background:#000;color:#eee;font-family:-apple-system,system-ui,sans-serif;overflow:hidden}
canvas{display:block}
"""

# ------------------------------------------------------------------ tela (Mac)
CSS_TELA = """
#sair{position:fixed;top:16px;right:16px;z-index:60;opacity:0;pointer-events:none;transition:opacity .25s;background:rgba(0,0,0,.72);
color:#fff;border:1px solid rgba(255,255,255,.35);border-radius:12px;padding:10px 16px;font:600 15px -apple-system,system-ui,sans-serif;cursor:pointer;display:none}
body.mexeu #sair{opacity:1;pointer-events:auto}
body{cursor:none}
#stage{position:fixed;inset:0;display:flex;align-items:center;justify-content:center}
.pg{position:absolute;box-shadow:0 0 60px rgba(0,0,0,.6)}
.pg.leave-fade{animation:fadeOut .5s ease forwards}.pg.enter-fade{animation:fadeIn .5s ease forwards}
.pg.leave-slide{animation:slideOutL .5s cubic-bezier(.65,0,.35,1) forwards}.pg.enter-slide{animation:slideInR .5s cubic-bezier(.65,0,.35,1) forwards}
.pg.leave-slide.back{animation:slideOutR .5s cubic-bezier(.65,0,.35,1) forwards}.pg.enter-slide.back{animation:slideInL .5s cubic-bezier(.65,0,.35,1) forwards}
.pg.leave-zoom{animation:zoomOut .5s ease forwards}.pg.enter-zoom{animation:zoomIn .5s ease forwards}
@keyframes fadeIn{from{opacity:0}to{opacity:1}}@keyframes fadeOut{from{opacity:1}to{opacity:0}}
@keyframes slideInR{from{transform:translateX(100%)}to{transform:none}}@keyframes slideOutL{from{transform:none}to{transform:translateX(-100%)}}
@keyframes slideInL{from{transform:translateX(-100%)}to{transform:none}}@keyframes slideOutR{from{transform:none}to{transform:translateX(100%)}}
@keyframes zoomIn{from{opacity:0;transform:scale(.92)}to{opacity:1;transform:none}}@keyframes zoomOut{from{opacity:1;transform:none}to{opacity:0;transform:scale(1.06)}}
#laser{position:fixed;width:26px;height:26px;margin:-13px 0 0 -13px;border-radius:50%;pointer-events:none;display:none;
background:radial-gradient(circle,#fff 0,#ff2d2d 35%,rgba(255,0,0,.35) 60%,rgba(255,0,0,0) 72%);box-shadow:0 0 22px 8px rgba(255,40,40,.55)}
#cortina{position:fixed;inset:0;display:none;z-index:7;overflow:hidden}
#cortina .cb{position:absolute;inset:0;opacity:0;transition:opacity .35s}
#cortina.k-preto .cb{background:#000;opacity:1}#cortina.k-branco .cb{background:#fff;opacity:1}
#cortina.k-fim .cb{background:radial-gradient(circle at 50% 40%,#1b2a52,#05070f 70%);opacity:1}
#cortina .ct{position:absolute;top:0;bottom:0;left:0;width:50.5%;transform:translateX(-101%);transition:transform 1.3s cubic-bezier(.65,0,.35,1);
background:repeating-linear-gradient(90deg,#6b0a14 0,#b8202a 3.5%,#6b0a14 7%);box-shadow:0 0 40px rgba(0,0,0,.6)}
#cortina .ct.r{left:auto;right:0;transform:translateX(101%)}#cortina.k-teatro .ct{transform:none}
#fimbox{position:absolute;inset:0;display:none;flex-direction:column;align-items:center;justify-content:center;text-align:center;color:#fff;gap:2vh}
#cortina.k-fim #fimbox{display:flex}#fimbox h1{font-size:12vh;font-weight:700}#fimbox p{font-size:4.5vh;color:#cfd6ee}
#conf{position:fixed;inset:0;width:100%;height:100%;pointer-events:none;z-index:8}
#ov{position:absolute;left:0;top:0;pointer-events:none}
#foco{position:fixed;inset:0;display:none;pointer-events:none;z-index:3}
#qr{position:fixed;inset:0;display:none;align-items:center;justify-content:center;flex-direction:column;gap:18px;background:rgba(0,0,0,.88);color:#fff;text-align:center;z-index:5}
#qr .cartao{background:#fff;padding:18px;border-radius:16px;width:min(46vh,46vw);aspect-ratio:1}#qr svg{width:100%;height:100%;display:block}
#qr h1{font-size:28px;font-weight:600}#qr p{font-size:15px;color:#bbb;max-width:80vw;word-break:break-all}
#dica{position:fixed;left:50%;bottom:24px;transform:translateX(-50%);background:rgba(20,20,20,.8);color:#ddd;padding:10px 18px;border-radius:10px;font-size:15px;transition:opacity .6s}
"""
HTML_TELA = """<div id="stage"></div><div id="laser"></div><div id="foco"></div><div id="cortina"><div class="cb"></div><div class="ct"></div><div class="ct r"></div><div id="fimbox"><h1 id="fimt"></h1><p id="fims"></p></div></div><canvas id="conf"></canvas><button id="sair">Sair da tela cheia (Esc)</button>
<div id="qr"><h1>Escaneie com o iPhone para controlar</h1><div class="cartao" id="qrsvg"></div><p id="qrurl"></p><p>Q: mostrar ou ocultar este codigo</p></div>
<div id="dica">F: tela cheia &middot; setas: navegar &middot; B: preto &middot; W: branco &middot; T: teatro &middot; E: fim &middot; C: limpar &middot; Q: QR code &middot; clique: avancar</div>"""

# desenha o QR (qrcode-generator) em #qrsvg; usado pela tela e pela pagina /qr
JS_QR = r"""
function desenharQR(){
  fetch('/painel/estado').then(r=>r.json()).then(j=>(j.pareamento_urls||[])[0]).catch(()=>null).then(u=>{  // QR de uso unico: sempre o codigo atual
    const url=u?u.url:REMOTE_URL;document.getElementById('qrurl').textContent=url;
    try{const q=qrcode(0,'M');q.addData(url);q.make();document.getElementById('qrsvg').innerHTML=q.createSvgTag({cellSize:8,margin:0,scalable:true})}
    catch(e){document.getElementById('qrsvg').textContent='QR indisponivel: abra a URL abaixo'}
  });
}
"""
JS_PASSOS = r"""
// Agrupa paginas do PDF em frames: as paginas de um frame com aparicao progressiva (\\pause, <+->) mostram o mesmo
// conteudo com mais itens a cada passo. Ignora numeros de rodape e a marca de rascunho.
let LADO=false;
const larguraUtil=w=>LADO?w/2:w;
async function detectaLado(d){const v=(await d.getPage(1)).getViewport({scale:1});LADO=v.width/v.height>2.2}
async function notasDoPdf(d){
  if(!LADO)return{};const out={};
  for(let i=1;i<=d.numPages;i++){const t=await notaDaPagina(d,i);if(t)out['p'+i]=t}
  return out;
}
async function notaDaPagina(d,i){
  {
    const p=await d.getPage(i),v=p.getViewport({scale:1}),W=v.width,H=v.height,tc=await p.getTextContent();
    let it=tc.items.filter(x=>x.str.trim()&&x.transform[4]>=W/2&&x.transform[5]<=H*0.75);
    if(!it.length)it=tc.items.filter(x=>x.str.trim()&&x.transform[4]>=W/2);
    it.sort((a,b)=>b.transform[5]-a.transform[5]||a.transform[4]-b.transform[4]);
    const linhas=[];
    for(const x of it){const y=x.transform[5],l=linhas[linhas.length-1];
      if(l&&Math.abs(l.y-y)<2.5){l.t.push(x.str);l.h=Math.max(l.h,x.height||0)}else linhas.push({y,t:[x.str],h:x.height||10})}
    let txt='',prev=null;
    for(const l of linhas){
      const s=l.t.join(' ').replace(/\s+/g,' ').trim().replace(/^[•▶‣▸◦·]\s*/,'- ');
      if(!s)continue;if(prev!==null)txt+=(prev-l.y>l.h*2.2?'\n\n':'\n');txt+=s;prev=l.y}
    return txt;
  }
}
// Numero impresso no rodape de cada pagina (no beamer e o numero do frame: as paginas de um mesmo frame repetem o numero).
// So vale se quase todas as paginas tem numero e a sequencia nunca anda para tras nem salta; senao devolve null e o
// agrupamento por conteudo assume.
async function numerosDeRodape(pdf){
  const n=pdf.numPages,cand=[[]];
  for(let i=1;i<=n;i++){
    const pg=await pdf.getPage(i),v=pg.getViewport({scale:1}),W=v.width,H=v.height,tc=await pg.getTextContent();
    const it=tc.items.filter(x=>x.str.trim()&&x.transform[5]<H*0.14&&(!LADO||x.transform[4]<W/2)).sort((a,b)=>a.transform[4]-b.transform[4]);
    const nums=[];
    for(const x of it){const t=x.str.trim(),m=t.match(/^(\d{1,4})$/)||t.match(/^(\d{1,4})\s*\/\s*\d{1,4}$/);if(m)nums.push(parseInt(m[1]))}
    cand.push(nums);
  }
  // o numero do frame e a coluna do rodape cuja sequencia nunca anda para tras nem salta (o total de frames e constante)
  let melhor=null;
  for(let j=0;j<4;j++){
    const r=[0];let com=0,ant=0,ok=true;
    for(let i=1;i<=n;i++){let x=cand[i][j]||0;if(x)com++;else x=ant||1;if(x<ant||(ant>0&&x>ant+1)){ok=false;break}r.push(x);ant=x}
    if(ok&&com>=n*0.8&&ant>1&&(!melhor||ant>melhor[n]))melhor=r;
  }
  return melhor;
}
async function agruparPassos(pdf){
  const n=pdf.numPages,tok=[];
  for(let i=1;i<=n;i++){
    const pg=await pdf.getPage(i),W=pg.getViewport({scale:1}).width,tc=await pg.getTextContent();const m=new Map();
    tc.items.filter(x=>!LADO||x.transform[4]<W/2).forEach(x=>{const t=x.str.trim();if(!t||/^[\d\s\/]+$/.test(t)||/^(RASCUNHO|DRAFT)$/i.test(t))return;m.set(t,(m.get(t)||0)+1)});
    tok.push(m);
  }
  const contido=(a,b)=>{let tb=0,ta=0;for(const[k,v]of b)tb+=v;for(const[k,v]of a){ta+=v;if((b.get(k)||0)<v)return false}return ta>0&&tb>ta};
  const rod=await numerosDeRodape(pdf);
  if(rod){const paginas=[];let tot=0;for(let i=1;i<=n;i++){const f=rod[i];(paginas[f]=paginas[f]||[]).push(i);tot=Math.max(tot,f)}
    return{frameDe:[0].concat(rod.slice(1)),paginas,total:tot,rodape:true}}
  const frameDe=[0],paginas=[[]];let f=0;
  for(let i=1;i<=n;i++){if(i===1||!contido(tok[i-2],tok[i-1])){f++;paginas[f]=[]}frameDe[i]=f;paginas[f].push(i)}
  return{frameDe,paginas,total:f};
}
"""

JS_TELA = r"""
const qrEl=document.getElementById('qr');
function mostrarQR(v){qrEl.style.display=v?'flex':'none'}
const stage=document.getElementById('stage'),laserEl=document.getElementById('laser'),focoEl=document.getElementById('foco');
let pdf=null,mapa=null,atual=null,paginaAtual=0,modo='__TRANSICAO__',trabalhando=false,pendente=null,est={zoom:1,zx:.5,zy:.5};
let tracos=[],cfg={laser_tam:24,laser_cor:'#ff2d2d',foco_raio:12,foco_blur:100,foco_escuro:25,pen_larg:3.5,hl_larg:14,hl_alpha:38};
const rgba=(h,a)=>{const n=parseInt(h.slice(1),16);return 'rgba('+(n>>16&255)+','+(n>>8&255)+','+(n&255)+','+a+')'};
function aplicarCfg(){
  const t=cfg.laser_tam;laserEl.style.width=laserEl.style.height=(t*2)+'px';laserEl.style.margin=(-t)+'px 0 0 '+(-t)+'px';
  const n=parseInt(cfg.laser_cor.slice(1),16),escuro=.299*(n>>16&255)+.587*(n>>8&255)+.114*(n&255)<76;
  laserEl.style.background='radial-gradient(circle,'+(escuro?cfg.laser_cor:'#fff')+' 0,'+cfg.laser_cor+' 35%,'+rgba(cfg.laser_cor,.35)+' 60%,'+rgba(cfg.laser_cor,0)+' 72%)';
  laserEl.style.boxShadow=escuro?'0 0 '+t*.5+'px '+t*.3+'px rgba(255,255,255,.85)':'0 0 '+t*.9+'px '+t*.35+'px '+rgba(cfg.laser_cor,.55);
  redesenhar();
}
stage.style.transition='transform .3s ease';
const ov=document.createElement('canvas');ov.id='ov';stage.appendChild(ov);
function mk(){const c=document.createElement('canvas');c.className='pg';stage.insertBefore(c,ov);return c}
async function render(n,c,f){
  f=f||1;const p=await pdf.getPage(n),v0=p.getViewport({scale:1});
  const uw=larguraUtil(v0.width),esc=Math.min(innerWidth/uw,innerHeight/v0.height),dpr=Math.min(devicePixelRatio||1,2);
  const bw=uw*esc*dpr,bh=v0.height*esc*dpr;  // tamanho base em pixels (1920x1080 -> 1920x1080; Retina 1440x900 -> 2880x1620)
  f=Math.max(1,Math.min(f,Math.sqrt(AREA_MAX/(bw*bh)),16384/Math.max(bw,bh)));  // com zoom o canvas e redesenhado maior (nitido), limitado por area para caber em qualquer GPU/Safari
  const v=p.getViewport({scale:esc*dpr*f});
  const alvo=f>1?document.createElement('canvas'):c;  // zoom: desenha fora da tela e troca de uma vez, sem piscar
  alvo.width=Math.ceil(larguraUtil(v.width));alvo.height=Math.ceil(v.height);c.style.width=uw*esc+'px';c.style.height=v0.height*esc+'px';
  await p.render({canvasContext:alvo.getContext('2d'),viewport:v}).promise;
  if(alvo!==c){c.width=alvo.width;c.height=alvo.height;c.getContext('2d').drawImage(alvo,0,0);alvo.width=alvo.height=0}
  c._f=f;
}
const AREA_MAX=36e6;  // ~36 Mpx (144 MB): 1920x1080 chega a 4x; Retina a ~2,8x
let nitT=null;
function nitidez(){clearTimeout(nitT);nitT=setTimeout(async()=>{
  if(!atual||trabalhando||!pdf)return;const z=est.zoom||1,alvo=z<=1.001?1:Math.min(z,4);
  if(Math.abs((atual._f||1)-alvo)<0.05)return;const c=atual,n=paginaAtual,z0=z;
  try{await render(n,c,alvo)}catch(e){}
  if(atual===c&&(est.zoom||1)!==z0)nitidez()},350)}
function geom(){const w=parseFloat(atual.style.width),h=parseFloat(atual.style.height);return{w,h,left:(innerWidth-w)/2,top:(innerHeight-h)/2}}
function aplicarZoom(){
  if(!atual)return;const z=est.zoom||1;nitidez();
  if(z<=1.001){stage.style.transform='';return}
  const{w,h,left,top}=geom();
  const hx=Math.min(w/2,innerWidth/(2*z)),hy=Math.min(h/2,innerHeight/(2*z));
  const px=Math.max(left+hx,Math.min(left+w-hx,left+est.zx*w)),py=Math.max(top+hy,Math.min(top+h-hy,top+est.zy*h));
  stage.style.transformOrigin=px+'px '+py+'px';
  stage.style.transform='translate('+(innerWidth/2-px)+'px,'+(innerHeight/2-py)+'px) scale('+z+')';
}
// ---- caneta e marca-texto: canvas por cima do slide, dentro do palco (acompanha o zoom)
function ajustarOv(){const dpr=Math.min(devicePixelRatio||1,2);ov.width=innerWidth*dpr;ov.height=innerHeight*dpr;ov.style.width=innerWidth+'px';ov.style.height=innerHeight+'px'}
function tracar(t,de){
  if(!atual)return;const g=ov.getContext('2d'),{w,h,left,top}=geom(),dpr=ov.width/innerWidth,p=t.p;
  if(p.length<1)return;g.save();g.scale(dpr,dpr);g.lineCap='round';g.lineJoin='round';
  g.lineWidth=t.t==='hl'?Math.max(8,w*cfg.hl_larg/1000):Math.max(2,w*cfg.pen_larg/1000);g.strokeStyle=t.c;
  if(t.t==='hl'){g.globalAlpha=cfg.hl_alpha/100}
  g.beginPath();const ini=Math.max(0,de-1);g.moveTo(left+p[ini][0]*w,top+p[ini][1]*h);
  for(let i=ini+1;i<p.length;i++)g.lineTo(left+p[i][0]*w,top+p[i][1]*h);
  if(p.length===1)g.lineTo(left+p[0][0]*w+.1,top+p[0][1]*h);g.stroke();g.restore();
}
function redesenhar(){ov.getContext('2d').clearRect(0,0,ov.width,ov.height);tracos.forEach(t=>tracar(t,0))}
function limparTracos(){tracos=[];ov.getContext('2d').clearRect(0,0,ov.width,ov.height)}
async function ir(n,modoLocal){
  if(!pdf)return; if(trabalhando){pendente=[n,modoLocal];return} trabalhando=true;
  const dir=n<paginaAtual?'back':'';const prox=mk();await render(n,prox);
  const mesmoFrame=mapa&&paginaAtual&&mapa.frameDe[n]===mapa.frameDe[paginaAtual];
  if(atual&&!mesmoFrame&&modo!=='nenhuma'&&paginaAtual!==n){
    const velho=atual;velho.className='pg leave-'+modo+' '+dir;prox.className='pg enter-'+modo+' '+dir;
    await new Promise(r=>setTimeout(r,520));velho.remove();prox.className='pg';
  }else{if(atual)atual.remove()}
  atual=prox;if(paginaAtual&&!mesmoFrame)limparTracos();paginaAtual=n;trabalhando=false;aplicarZoom();redesenhar();
  if(pendente){const[p,m]=pendente;pendente=null;ir(p,m)}
}
let viuRemoto=false;
function aplicar(s){
  if(s.remotos>0&&!viuRemoto){viuRemoto=true;mostrarQR(false)}
  modo=s.transicao;cortina(s.cortina||'',s.fim||{txt:'',sub:''});
  est={zoom:s.zoom||1,zx:s.zx,zy:s.zy};
  if(s.pagina!==paginaAtual)ir(s.pagina);else aplicarZoom();
}
let cortK='';
function cortina(k,fim){
  const el=document.getElementById('cortina');
  if(k===cortK&&(k!=='fim'||document.getElementById('fimt').textContent===fim.txt))return;
  const antes=cortK;cortK=k;
  if(k){el.style.display='block';void el.offsetWidth;el.className='k-'+k;
    if(k==='fim'){document.getElementById('fimt').textContent=fim.txt;document.getElementById('fims').textContent=fim.sub;confete(6)}}
  else{el.className='';setTimeout(()=>{if(!cortK)el.style.display='none'},antes==='teatro'?1400:400)}
}
let confP=[],confT=null,confFim=0;
function confete(seg){
  const c=document.getElementById('conf'),g=c.getContext('2d');c.width=innerWidth;c.height=innerHeight;
  confFim=performance.now()+seg*1000;if(confT)return;
  const cores=['#ff3b30','#ffd60a','#30d158','#0a84ff','#bf5af2','#ff9f0a'];
  confT=setInterval(()=>{
    const agora=performance.now();
    if(agora<confFim)for(let i=0;i<5;i++)confP.push({x:Math.random()*c.width,y:-20,vx:(Math.random()-.5)*4,vy:2+Math.random()*3,r:Math.random()*6.28,vr:(Math.random()-.5)*.3,w:8+Math.random()*8,h:5+Math.random()*6,c:cores[Math.random()*6|0]});
    g.clearRect(0,0,c.width,c.height);confP=confP.filter(p=>p.y<c.height+30);
    confP.forEach(p=>{p.x+=p.vx+Math.sin(p.y/40);p.y+=p.vy;p.vy=Math.min(p.vy+.04,6);p.r+=p.vr;g.save();g.translate(p.x,p.y);g.rotate(p.r);g.fillStyle=p.c;g.fillRect(-p.w/2,-p.h/2,p.w,p.h);g.restore()});
    if(!confP.length&&agora>=confFim){clearInterval(confT);confT=null}
  },16);
}
function laser(l){
  if(!l.on||!atual){laserEl.style.display='none';focoEl.style.display='none';return}
  const r=atual.getBoundingClientRect(),x=r.left+l.x*r.width,y=r.top+l.y*r.height;
  if(l.m==='foco'){laserEl.style.display='none';focoEl.style.display='block';
    const rr=Math.max(40,r.width*(l.r||cfg.foco_raio/100));
    const mask='radial-gradient(circle at '+x+'px '+y+'px, transparent '+rr*0.65+'px, #000 '+rr+'px)';
    const b=cfg.foco_blur/100*14;
    focoEl.style.background='rgba(0,0,0,'+cfg.foco_escuro/100+')';
    focoEl.style.webkitMaskImage=focoEl.style.maskImage=mask;
    focoEl.style.backdropFilter=focoEl.style.webkitBackdropFilter=b>0.3?'blur('+b+'px)':'none'}
  else{focoEl.style.display='none';laserEl.style.left=x+'px';laserEl.style.top=y+'px';laserEl.style.display='block'}
}
function msg(m){
  if(m.hb)return;
  if(m.laser)return laser(m.laser);
  if(m.draw){let t=tracos.find(x=>x.id===m.draw.id);const de=t?t.p.length:0;
    if(!t){t={id:m.draw.id,t:m.draw.t,c:m.draw.c,p:[]};tracos.push(t)}t.p.push(...m.draw.p);tracar(t,de);return}
  if(m.tracos){tracos=m.tracos;redesenhar();return}
  if(m.limpar){limparTracos();return}
  if(m.confete){confete(m.confete);return}
  if(m.cfg){cfg=m.cfg;aplicarCfg();return}
  aplicar(m);
}
function cmd(c,v){fetch('/cmd',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({cmd:c,valor:v})}).catch(()=>{})}
addEventListener('keydown',e=>{
  if(['ArrowRight','ArrowDown','PageDown',' ','Enter'].includes(e.key)){cmd('next');e.preventDefault()}
  else if(['ArrowLeft','ArrowUp','PageUp','Backspace'].includes(e.key)){cmd('prev');e.preventDefault()}
  else if(e.key==='Home')cmd('first');else if(e.key==='End')cmd('last');
  else if(e.key==='b'||e.key==='B'||e.key==='.')cmd('blank');
  else if(e.key==='w'||e.key==='W')cmd('cortina','branco');
  else if(e.key==='t'||e.key==='T')cmd('cortina','teatro');
  else if(e.key==='e'||e.key==='E')cmd('cortina',{k:'fim',txt:'Obrigado!',sub:'Perguntas?'});
  else if(e.key==='c'||e.key==='C')cmd('limpar');
  else if(e.key==='q'||e.key==='Q'){mostrarQR(qrEl.style.display!=='flex')}
  else if(e.key==='f'||e.key==='F'){document.fullscreenElement?document.exitFullscreen():document.documentElement.requestFullscreen()}
});
// tela cheia aberta pelo Baton (/apresentar): Esc ou o botao no canto fecham a janela e o Baton volta a mandar teclas ao app
function fecharTela(){fetch('/painel/acao',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({acao:'fechar_tela'})}).catch(()=>{}).finally(()=>{try{window.close()}catch(x){}try{if(document.fullscreenElement)document.exitFullscreen()}catch(x){}})}
if(location.pathname==='/apresentar'){
  addEventListener('keydown',e=>{if(e.key==='Escape'){e.preventDefault();e.stopPropagation();fecharTela()}},true);
  const bs=document.getElementById('sair');bs.style.display='block';bs.onclick=e=>{e.stopPropagation();fecharTela()};
  let tMex=null;addEventListener('mousemove',()=>{document.body.classList.add('mexeu');clearTimeout(tMex);tMex=setTimeout(()=>document.body.classList.remove('mexeu'),2600)});
}
const QUER_FS=location.search.indexOf('fs=1')>=0;
addEventListener('click',()=>{if(QUER_FS&&!document.fullscreenElement)document.documentElement.requestFullscreen().catch(()=>{});if(qrEl.style.display!=='flex')cmd('next')});
addEventListener('resize',()=>{ajustarOv();if(atual){const n=paginaAtual;paginaAtual=0;atual.remove();atual=null;ir(n)}});
setTimeout(()=>{const d=document.getElementById('dica');d.style.opacity=0},6000);
ajustarOv();desenharQR();mostrarQR(true);
let es=null,ultimaMsg=Date.now(),ultimoSnap=null;
function conectar(){
  if(es){try{es.close()}catch(e){}}
  es=new EventSource('/events?k='+encodeURIComponent(TOKEN));ultimaMsg=Date.now();
  es.onmessage=ev=>{ultimaMsg=Date.now();const m=JSON.parse(ev.data);if(m.pagina!==undefined)ultimoSnap=m;msg(m)};
}
setInterval(()=>{if(Date.now()-ultimaMsg>12000)conectar()},3000);
document.addEventListener('visibilitychange',()=>{if(document.visibilityState==='visible')conectar()});
conectar();  // o SSE nao depende do leitor de PDF: sem ele o Baton achava que a tela nao conectou e mandava teclas a outro app
pdfjsLib.getDocument('/pdf?k='+encodeURIComponent(TOKEN)).promise.then(async d=>{pdf=d;try{await detectaLado(d);mapa=await agruparPassos(d)}catch(e){}if(ultimoSnap)msg(ultimoSnap)}).catch(()=>{const d=document.getElementById('dica');if(d){d.style.opacity=1;d.textContent='Leitor de PDF indisponivel (sem internet?). O Baton segue conectado; abra o PDF no seu leitor.'}});
"""

# ------------------------------------------------------------------ celular (iPhone)
CSS_REMOTO = """
body{display:flex;flex-direction:column;padding:calc(env(safe-area-inset-top) + 10px) max(12px,env(safe-area-inset-right)) env(safe-area-inset-bottom) max(12px,env(safe-area-inset-left));gap:8px;touch-action:none;user-select:none;-webkit-user-select:none}
#app,#colA,#colB{display:contents}
#colA>*,#colB>*{order:3}#topo{order:0}#vista{order:1}#info{order:2}#colB>#ajustes,#colB>#extras,#colB>#notas,#colB>#botoes{order:4}
#topo{display:flex;justify-content:space-between;align-items:center;padding-top:6px;font-size:14px;color:#aaa;flex:0 0 auto}
#pos{font-size:19px;color:#fff;font-weight:600}
#dot{width:10px;height:10px;border-radius:50%;background:#e33;display:inline-block;margin-right:6px}#dot.ok{background:#3c6}
#vista{position:relative;flex:0 0 auto;background:#111;border-radius:10px;overflow:hidden;align-self:center;touch-action:none}
#vista canvas{width:100%;height:100%;position:absolute;left:0;top:0}
#cap{display:none;position:absolute;left:0;top:0;width:100%;height:100%;object-fit:contain;background:#000;pointer-events:none}
#ponto{position:absolute;width:30px;height:30px;margin:-15px 0 0 -15px;border-radius:50%;pointer-events:none;display:none;
background:radial-gradient(circle,#fff 0,#ff2d2d 35%,rgba(255,0,0,.4) 60%,rgba(255,0,0,0) 72%);box-shadow:0 0 18px 6px rgba(255,40,40,.6)}
#zflut{position:absolute;right:6px;bottom:6px;z-index:5;display:flex;align-items:center;gap:2px;background:rgba(0,0,0,.62);border-radius:16px;padding:2px 4px}
#bpdf{background:#0a84ff;padding:8px 10px;font-size:13px;border-radius:10px;flex:0 0 auto}
#zflut button{width:30px;height:28px;padding:0;font-size:18px;line-height:1;border-radius:14px;background:transparent}#zflut span{min-width:30px;text-align:center;font-size:12px;color:#fff;font-weight:600}
#zrect{position:absolute;border:2px solid #0a84ff;background:rgba(10,132,255,.15);pointer-events:none;display:none;border-radius:3px}
button{border:0;border-radius:12px;background:#1d1d1f;color:#fff;font-size:15px;font-weight:600;touch-action:manipulation}
button:active{background:#333}
.linha{display:flex;gap:6px;flex:0 0 auto}.linha button,.linha select{flex:1;padding:13px 0}
.linha button.on{background:#ff3b30}.linha button#bz.on,.linha button#bf.on{background:#0a84ff}.linha button#bm.on{background:#b8860b}
select{background:#1d1d1f;color:#fff;border:0;border-radius:12px;font-size:14px;padding:0 6px}
#raiobar{display:none;align-items:center;color:#aaa;font-size:14px;gap:12px}#raiobar input{flex:1;accent-color:#0a84ff}
#zbar{display:none}#zbar span{flex:1;text-align:center;font-size:17px;color:#fff;font-weight:600;align-self:center}
#cores{display:none;gap:14px;justify-content:center;flex:0 0 auto}#cores i{width:30px;height:30px;border-radius:50%;border:3px solid transparent;display:block}#cores i.sel{border-color:#fff}
#aviso{display:none;position:fixed;left:12px;right:12px;top:calc(env(safe-area-inset-top) + 8px);z-index:30;background:#2c2c2e;border:1px solid #0a84ff;border-radius:14px;padding:12px;font-size:14px;line-height:1.35}
.avb{display:grid;grid-template-columns:1fr 1fr;gap:6px;margin-top:8px}.avb button{padding:10px 0;font-size:14px}
#ajustes,#extras{display:none;flex:1;min-height:70px;background:#111;border-radius:12px;flex-direction:column;overflow:hidden}
#ajhead,#exhead{display:flex;justify-content:space-between;align-items:center;padding:6px 10px;font-size:13px;color:#888;flex:0 0 auto}
#ajhead button,#exhead button{padding:5px 10px;font-size:12px;border-radius:8px}
#excorpo{flex:1;overflow:auto;padding:2px 12px 10px;touch-action:pan-y}.exl{font-size:12px;color:#888;margin:10px 0 5px}
.exg{display:grid;grid-template-columns:repeat(3,1fr);gap:6px}.exg button{padding:12px 0;font-size:14px}.exg button.on{background:#0a84ff}
#excorpo input{width:100%;background:#1d1d1f;color:#fff;border:0;border-radius:10px;font-size:15px;padding:10px;margin-bottom:6px;user-select:text;-webkit-user-select:text}
#ajcorpo{flex:1;overflow:auto;padding:2px 12px 10px;touch-action:pan-y}
.ajl{display:flex;align-items:center;gap:10px;font-size:13px;color:#ccc;margin:7px 0}.ajl label{flex:0 0 118px}.ajl input{flex:1;accent-color:#0a84ff}.ajl output{flex:0 0 38px;text-align:right;color:#888;font-variant-numeric:tabular-nums}
.ajc{display:flex;gap:12px;margin:8px 0 4px;align-items:center;font-size:13px;color:#ccc}.ajc i{width:26px;height:26px;border-radius:50%;border:3px solid transparent;display:block;box-shadow:0 0 0 1px #666}.ajc i.sel{border-color:#fff}
#info{display:flex;gap:10px;align-items:stretch;flex:0 0 auto}
#prevbox{flex:1;display:flex;align-items:center;gap:10px;background:#111;border-radius:12px;padding:8px 10px;min-width:0}
#prevbox canvas{height:92px;max-width:68%;border-radius:6px;background:#1a1a1c}#prevlbl{font-size:13px;color:#999;line-height:1.25}
#relogio{flex:0 0 36%;background:#111;border-radius:12px;padding:6px 10px;display:flex;flex-direction:column;justify-content:center;align-items:center}
#tempo{font-size:32px;font-weight:700;font-variant-numeric:tabular-nums;color:#fff;line-height:1.1}#restante{font-size:12px;color:#888;margin-top:2px}
#rlbt{display:flex;gap:6px;margin-top:5px;width:100%}#rlbt button{flex:1;padding:5px 0;font-size:12px;border-radius:8px}
#voltas{width:100%;max-height:38px;overflow:auto;font-size:11px;color:#aaa;font-variant-numeric:tabular-nums;text-align:center;margin-top:3px;touch-action:pan-y}
#voltas div{line-height:1.25}#voltas:empty{display:none}
#relogio.alerta #tempo{color:#ffd60a}#relogio.estouro #tempo{color:#ff453a}#relogio.pausado #tempo{opacity:.55}
#irbox{position:fixed;inset:0;background:rgba(0,0,0,.94);z-index:20;display:none;flex-direction:column;padding:env(safe-area-inset-top) 12px 12px;gap:10px}
#irhead{display:flex;gap:8px;padding-top:10px}#irhead input{flex:1;background:#1d1d1f;color:#fff;border:0;border-radius:12px;font-size:20px;padding:12px;min-width:0}
#irhead button{padding:0 18px;font-size:17px}#irgo{background:#0a84ff}
#irgrid{flex:1;overflow:auto;display:grid;grid-template-columns:repeat(3,1fr);gap:10px;align-content:start;touch-action:pan-y}
#irgrid div{position:relative;background:#111;border-radius:8px;overflow:hidden;border:2px solid transparent}#irgrid div.atual{border-color:#0a84ff}
#irgrid canvas{width:100%;display:block}#irgrid span{position:absolute;left:4px;top:2px;font-size:12px;font-weight:700;color:#fff;background:rgba(0,0,0,.6);padding:1px 6px;border-radius:6px}
#notas{flex:1;min-height:70px;background:#111;border-radius:12px;display:flex;flex-direction:column;overflow:hidden}
#nhead{display:flex;justify-content:space-between;align-items:center;padding:4px 10px;font-size:12px;color:#888;flex:0 0 auto}
#nhead{gap:4px;white-space:nowrap}#nhead>span{overflow:hidden;text-overflow:ellipsis;min-width:0}#nhead button{padding:3px 10px;font-size:13px;border-radius:8px;margin-left:4px}
#ntxt{flex:1;overflow:auto;padding:2px 12px 10px;line-height:1.35;touch-action:pan-y;-webkit-overflow-scrolling:touch;user-select:text;-webkit-user-select:text}
#ned{flex:1;width:100%;box-sizing:border-box;background:#1d1d1f;color:#fff;border:0;border-radius:8px;font:inherit;line-height:1.35;padding:8px 10px;resize:none;min-height:0}
#nbt{display:none;gap:6px;padding:6px 10px 8px}#nbt button{flex:1;padding:8px;font-size:14px;border-radius:10px}#nsv{background:#0a84ff}
#ntxt p{margin:0 0 .6em}#ntxt ul{margin:0 0 .6em 1.1em}#ntxt .vazio{color:#666}
#botoes{display:flex;gap:10px;height:84px;flex:0 0 auto}#botoes button{font-size:19px;border-radius:16px}
#ant{flex:1}#seg{flex:2;background:#0a84ff}#seg:active{background:#006fe0}
/* celular na horizontal: slide atual a esquerda; proximo, cronometro, notas e botoes a direita */
@media (orientation:landscape) and (max-height:560px){
  body{gap:0;padding-top:max(4px,env(safe-area-inset-top));padding-bottom:max(4px,env(safe-area-inset-bottom))}
  #app{display:grid;grid-template-columns:minmax(0,1.3fr) minmax(0,1fr);gap:8px;flex:1;min-height:0}
  #colA,#colB{display:flex;flex-direction:column;gap:5px;min-height:0}
  #colA>*,#colB>*,#topo,#vista,#info{order:0}
  #topo{padding-top:0}#pos{font-size:16px}
  #vista{align-self:center}
  .linha button,.linha select{padding:7px 0;font-size:13px}
  #info{gap:6px}#prevbox{padding:4px 8px;gap:8px}#prevbox canvas{height:58px;max-width:70%}#prevlbl{font-size:11px}#relogio{flex:0 0 44%;padding:3px 8px}#tempo{font-size:24px}
  #restante{font-size:11px;margin-top:0}#rlbt{margin-top:3px}#rlbt button{padding:3px 0}
  #cores i{width:24px;height:24px}
  #notas,#ajustes,#extras{min-height:0}
  #botoes{height:54px;gap:8px}#botoes button{font-size:16px;border-radius:14px}
  #irgrid{grid-template-columns:repeat(5,1fr)}
  body.grande #notas,body.grande #ajustes,body.grande #extras{display:none!important}
  body.grande #app{grid-template-columns:minmax(0,2.4fr) minmax(0,1fr)}
  body.grande .linha button{padding:5px 0;font-size:12px}
  body.grande #info{flex-direction:column;flex:1;min-height:0}
  body.grande #prevbox{flex:1;flex-direction:column;align-items:stretch;justify-content:center;min-height:0;gap:3px}
  body.grande #prevbox canvas{height:auto;width:auto;max-width:100%;max-height:100%;min-height:0;align-self:center;object-fit:contain}
  body.grande #relogio{flex:0 0 auto;flex-direction:row;gap:10px;justify-content:space-between}
  body.grande #rlbt{width:auto;margin:0}
}
"""
HTML_REMOTO = """<div id="aviso"></div><div id="app"><div id="colA"><div id="topo"><span><i id="dot"></i><span id="st">conectando</span><a id="lns" style="margin-left:12px;color:#0a84ff;font-size:13px;text-decoration:none">modo simples</a></span><span id="pos">- / -</span></div>
<div id="vista"><img id="cap" alt=""><canvas id="cv"></canvas><canvas id="ovp"></canvas><div id="zrect"></div><div id="ponto"></div><div id="zflut"><button id="zfg" title="Ampliar a previa">&#x26F6;</button><button id="zfm">&minus;</button><span id="zfv">1x</span><button id="zfp">+</button></div></div>
<div class="linha" id="tools"><button id="bl">Laser</button><button id="bf">Foco</button><button id="bc">Caneta</button><button id="bm">Marca</button><button id="bz">Zoom</button></div>
<div class="linha" id="zbar"><button id="zm">&minus;</button><span id="zv">1x</span><button id="zp">+</button><button id="z1">1x</button></div>
<div class="linha" id="raiobar"><span>Raio do foco</span><input id="raio" type="range" min="4" max="30" value="12"></div>
<div id="cores"><i data-c="#ff3b30" style="background:#ff3b30"></i><i data-c="#0a84ff" style="background:#0a84ff"></i><i data-c="#30d158" style="background:#30d158"></i><i data-c="#ffd60a" style="background:#ffd60a"></i><i data-c="#ffffff" style="background:#fff"></i></div>
<div class="linha" id="tools2"><button id="bx">Limpar</button><button id="bb">Preto</button><button id="bg">Ir para</button><button id="bk">Extras</button><button id="ba">Ajustes</button></div>
</div><div id="colB">
<div id="info"><div id="prevbox"><canvas id="cvp"></canvas><span id="prevlbl">Proximo</span><button id="bpdf" style="display:none">Carregar PDF</button><input id="fpdf" type="file" accept=".pdf,.pptx,.ppt,.odp" style="display:none"></div><div id="relogio"><div id="tempo">00:00</div><div id="restante">toque para iniciar</div>
<div id="rlbt"><button id="rvolta">Volta</button><button id="rzera">Zerar</button></div><div id="voltas"></div></div></div>
<div id="ajustes"><div id="ajhead"><span>Ajustes</span><button id="ajrst">Restaurar padrao</button></div>
<div class="ajc" id="ajtr"><span>Transicao</span><select id="tr"><option value="fade">Fade</option><option value="slide">Deslizar</option><option value="zoom">Zoom</option><option value="nenhuma">Sem transicao</option></select><button id="tz">Zerar cronometro</button></div>
<div id="ajcorpo"></div></div>
<div id="extras"><div id="exhead"><span>Cortina e efeitos</span><button id="exx">Fechar</button></div><div id="excorpo">
<div class="exl">Cortina (toque de novo para abrir)</div>
<div class="exg" id="exk"><button data-k="preto">Preto</button><button data-k="branco">Branco</button><button data-k="teatro">Teatro</button><button data-k="fim">Fim</button></div>
<div class="exl">Texto do fim</div><input id="fimtxt" value="Obrigado!" maxlength="80"><input id="fimsub" value="Perguntas?" maxlength="120">
<div class="exl">Efeitos</div>
<div class="exg" id="exs"><button data-s="confete">Confete</button><button data-s="ding">Ding</button><button data-s="tambor">Tambor</button><button data-s="aplausos">Aplausos</button><button data-s="fanfarra">Fanfarra</button><button data-s="buzina">Buzina</button><button data-s="suspense">Suspense</button><button data-s="stop">Parar som</button></div>
</div></div>
<div id="notas"><div id="nhead"><span id="nt">Notas do apresentador</span><span><button id="ne">Editar</button><button id="nm">A&minus;</button><button id="np">A+</button></span></div><div id="ntxt"></div><textarea id="ned" style="display:none" placeholder="Digite a nota deste slide"></textarea><div id="nbt"><button id="nsv">Salvar</button><button id="ntd">Todas</button><button id="ncn">Cancelar</button></div></div>
<div id="botoes"><button id="ant">&#9664; Anterior</button><button id="seg">Proximo &#9654;</button></div>
</div></div>
<div id="irbox"><div id="irhead"><input id="irn" type="number" inputmode="numeric" min="1" placeholder="no do slide"><button id="irgo">Ir</button><button id="irx">Fechar</button></div><div id="irgrid"></div></div>
"""
JS_MIDIA = r"""
// controles de midia (tela bloqueada, fones, relogio): um audio mudo em loop mantem a sessao de midia ativa
let midiaOk=false;
function iniciaMidia(){
  if(midiaOk||!('mediaSession' in navigator))return;midiaOk=true;
  try{
    const n=8000,b=new ArrayBuffer(44+n),v=new DataView(b),w=(o,t)=>{for(let i=0;i<t.length;i++)v.setUint8(o+i,t.charCodeAt(i))};
    w(0,'RIFF');v.setUint32(4,36+n,true);w(8,'WAVEfmt ');v.setUint32(16,16,true);v.setUint16(20,1,true);v.setUint16(22,1,true);v.setUint32(24,8000,true);
    v.setUint32(28,8000,true);v.setUint16(32,1,true);v.setUint16(34,8,true);w(36,'data');v.setUint32(40,n,true);for(let i=0;i<n;i++)v.setUint8(44+i,128);
    const a=new Audio(URL.createObjectURL(new Blob([b],{type:'audio/wav'})));a.loop=true;a.volume=0.01;
    a.play().then(()=>{
      navigator.mediaSession.metadata=new MediaMetadata({title:'Baton',artist:'Controle de slides'});
      const h=(k,f)=>{try{navigator.mediaSession.setActionHandler(k,f)}catch(x){}};
      h('nexttrack',()=>cmd('next'));h('previoustrack',()=>cmd('prev'));h('seekforward',()=>cmd('next'));h('seekbackward',()=>cmd('prev'));
      h('play',()=>{a.play()});h('pause',()=>{a.play()});
    }).catch(()=>{midiaOk=false});
  }catch(x){midiaOk=false}
}
addEventListener('pointerdown',iniciaMidia,{once:true});
"""

JS_REMOTO = r"""
const $=id=>document.getElementById(id);
let raioFoco=0.12;try{raioFoco=(parseInt(localStorage.getItem('baton-raio'))||12)/100}catch(e){}
let pdf=null,mapa=null,pag=1,total=0,ferr=null,ultimo=0,zoom={z:1,x:.5,y:.5},es=null,ultimaMsg=Date.now(),seqVisto=-1,notas={},notasPdf={},fonte=17,desenhado=0;
const NIVEIS=[1,1.5,2,3,4],cores={pen:'#ff3b30',hl:'#ffd60a'};
const PADRAO={meta_min:50,laser_tam:24,laser_cor:'#ff2d2d',foco_raio:12,foco_blur:100,foco_escuro:25,pen_larg:3.5,hl_larg:14,hl_alpha:38};
const DEFS=[['meta_min','Meta de tempo (min)',5,120,5],['laser_tam','Laser: tamanho',6,80,1],['foco_raio','Foco: raio',3,35,1],['foco_blur','Foco: desfoque',0,100,5],['foco_escuro','Foco: escurecer',0,90,5],['pen_larg','Caneta: espessura',0.5,12,0.5],['hl_larg','Marca: espessura',3,40,1],['hl_alpha','Marca: opacidade',10,80,5]];
let cfg=Object.assign({},PADRAO),tCfg=null;
try{fonte=parseInt(localStorage.getItem('baton-fonte'))||17}catch(e){}
if(EXTERNO){$('bz').style.display='none';['zfm','zfp','zfv'].forEach(i=>$(i).style.display='none');$('tr').style.display='none';if(!OVERLAY){['bf','bc','bm','bx'].forEach(i=>$(i).style.display='none');document.querySelectorAll('#exk [data-k]:not([data-k=preto])').forEach(b=>b.style.display='none');document.querySelector('[data-s=confete]').style.display='none'}}
function status(ok,txt){$('dot').classList.toggle('ok',ok);$('st').textContent=(ok&&txt==='conectado'&&typeof rtt==='number'&&rtt>0)?txt+' - '+rtt+' ms'+(viaNome?' - '+viaNome.split(' ')[0]:''):txt}
const cid=()=>Date.now().toString(36)+Math.random().toString(36).slice(2,8);
async function cmd(c,v){
  const id=cid();
  for(let t=0;t<5;t++){
    const ac=new AbortController(),to=setTimeout(()=>ac.abort(),2500);
    try{
      const r=await fetch('/cmd',{method:'POST',headers:{'Content-Type':'application/json','X-Token':TOKEN},body:JSON.stringify({cmd:c,valor:v,cid:id}),signal:ac.signal});
      clearTimeout(to);if(r.status===423){aplicar(await r.json());status(true,'em espera');return false}
      if(r.ok){const s=await r.json();aplicar(s);status(true,'conectado');return true}
    }catch(e){clearTimeout(to)}
    status(false,'reenviando...');await new Promise(r=>setTimeout(r,250*(t+1)));
  }
  status(false,'sem conexao');conectar();return false;
}
async function desenha(n,cv,larg,f){
  f=f||1;const p=await pdf.getPage(n),v0=p.getViewport({scale:1}),dpr=Math.min(devicePixelRatio||1,3);
  const ph=larg*dpr*v0.height/larguraUtil(v0.width);f=Math.max(1,Math.min(f,Math.sqrt(16e6/(larg*dpr*ph))));  // celular (iOS limita o canvas a ~16,7 Mpx)
  const esc=larg*dpr*f/larguraUtil(v0.width),v=p.getViewport({scale:esc});
  const alvo=f>1?document.createElement('canvas'):cv;alvo.width=Math.ceil(larguraUtil(v.width));alvo.height=Math.ceil(v.height);
  await p.render({canvasContext:alvo.getContext('2d'),viewport:v}).promise;
  if(alvo!==cv){cv.width=alvo.width;cv.height=alvo.height;cv.getContext('2d').drawImage(alvo,0,0);alvo.width=alvo.height=0}
  cv._f=f;cv._n=n;cv._l=larg;
}
let nitP=null;
function nitidezPrev(){clearTimeout(nitP);nitP=setTimeout(async()=>{  // previa ampliada: redesenha o slide maior para nao ficar borrado
  const cv=$('cv');if(!pdf||!TEM_PDF||!cv._n)return;const alvo=vistaZ()?Math.min(zoom.z,4):1;
  if(Math.abs((cv._f||1)-alvo)<0.05)return;const z0=zoom.z;
  try{await desenha(cv._n,cv,cv._l,alvo)}catch(e){}
  if(zoom.z!==z0)nitidezPrev()},300)}
function fq(){if(!mapa)return{f:pag,t:total,passo:1,np:1};const f=mapa.frameDe[pag],ps=mapa.paginas[f];return{f,t:mapa.total,passo:ps.indexOf(pag)+1,np:ps.length}}
function rotulo(){const q=fq();$('pos').textContent=(TEM_PDF?q.f+' / '+q.t:'slide '+pag)+(q.np>1?'  passo '+q.passo+'/'+q.np:'')}
function md(t){
  const esc=x=>x.replace(/&/g,'&amp;').replace(/</g,'&lt;');let h='',lista=false;
  const il=x=>esc(x).replace(/\*\*(.+?)\*\*/g,'<b>$1</b>').replace(/\*(.+?)\*/g,'<i>$1</i>');
  t.split('\n').forEach(l=>{const m=l.match(/^\s*[-*]\s+(.*)/);
    if(m){if(!lista){h+='<ul>';lista=true}h+='<li>'+il(m[1])+'</li>'}
    else{if(lista){h+='</ul>';lista=false}if(l.trim())h+='<p>'+il(l)+'</p>'}});
  return h+(lista?'</ul>':'');
}
let editando=null;
function mostraNotas(){
  if(editando)return;
  const q=fq(),t=notas['p'+pag]??notas[String(q.f)]??notasPdf['p'+pag];
  if(LEVE&&LADO&&pdf&&notasPdf['p'+pag]===undefined){const p=pag;notasPdf['p'+p]=null;notaDaPagina(pdf,p).then(x=>{notasPdf['p'+p]=x||'';if(pag===p)mostraNotas()}).catch(()=>{})}
  $('ntxt').style.fontSize=fonte+'px';
  $('ntxt').innerHTML=t?md(t):'<p class="vazio">Sem notas para este slide.</p>';$('ntxt').scrollTop=0;
}
async function mostra(){
  rotulo();mostraNotas();
  const paisagem=innerWidth>innerHeight&&innerHeight<=560;
  const larg=paisagem?$('colA').clientWidth:document.body.clientWidth-24;
  // paisagem: o slide ocupa a altura que sobra na coluna (mede o resto); retrato: ate 33% da tela (56% no modo ampliado)
  let hmax=innerHeight*(document.body.classList.contains('grande')?0.56:0.33);
  if(paisagem){let o=0,n=0;for(const c of $('colA').children){if(c.id==='vista'||getComputedStyle(c).display==='none')continue;o+=c.offsetHeight;n++}
    const bs=getComputedStyle(document.body);hmax=Math.max(90,innerHeight-o-n*5-parseFloat(bs.paddingTop)-parseFloat(bs.paddingBottom)-2)}
  if(!pdf){const w0=Math.min(larg,hmax*16/9);$('vista').style.width=w0+'px';$('vista').style.height=(w0*9/16)+'px';ajustaOvp();if(!TEM_PDF){$('cvp').style.display='none';$('prevlbl').textContent='Proximo slide: so com o PDF carregado';$('bpdf').style.display='block'}capturaTela(450);return}
  if(desenhado===pag&&!window.__remedir)return;desenhado=pag;window.__remedir=false;limpaLocal();
  const p=await pdf.getPage(pag),v0=p.getViewport({scale:1});
  const uw=larguraUtil(v0.width),w=Math.min(larg,hmax*uw/v0.height);
  $('vista').style.width=w+'px';$('vista').style.height=(w*v0.height/uw)+'px';ajustaOvp();
  desenha(pag,$('cv'),w).then(nitidezPrev);
  previa();
}
function remedir(){window.__remedir=true;mostra()}
if(window.ResizeObserver){const ro=new ResizeObserver(()=>{clearTimeout(window.__ro);window.__ro=setTimeout(()=>{if(innerWidth>innerHeight&&innerHeight<=560)remedir()},120)});
  document.querySelectorAll('#colA>*').forEach(c=>{if(c.id!=='vista')ro.observe(c)})}
addEventListener('resize',()=>{clearTimeout(window.__rt);window.__rt=setTimeout(remedir,150)});
addEventListener('orientationchange',()=>setTimeout(remedir,250));
// sem PDF o Baton nao tem a imagem do slide: mostra uma captura da tela do computador (a apresentacao em tela cheia)
let capOcupada=false,capTimer=null,capSemSuporte=false;
function capturaTela(atraso){
  if(TEM_PDF||capSemSuporte)return;clearTimeout(capTimer);
  capTimer=setTimeout(async()=>{
    if(capOcupada||document.visibilityState!=='visible'){capturaTela(3000);return}
    capOcupada=true;
    try{const r=await fetch('/tela.jpg?k='+encodeURIComponent(TOKEN)+'&t='+Date.now());
      if(r.status===404){capSemSuporte=true;$('prevlbl').textContent='Sem previa: nao consegui capturar a tela do computador';return}
      if(r.ok){const url=URL.createObjectURL(await r.blob()),im=$('cap');const velha=im.src;im.src=url;im.style.display='block';const dz=$('lzdica');if(dz)dz.style.display='none';if(velha.startsWith('blob:'))URL.revokeObjectURL(velha)}
    }catch(e){}
    capOcupada=false;capturaTela(LEVE?6000:2500);
  },atraso===undefined?0:atraso)
}
function previa(){
  const prox=pag+1;
  if(!pdf||prox>total){const c=$('cvp'),g=c.getContext('2d');g.clearRect(0,0,c.width,c.height);c.style.visibility='hidden';
    $('prevlbl').textContent=!pdf?'Proximo slide: so com o PDF carregado':'ultimo slide';return}
  $('cvp').style.visibility='visible';$('cvp').style.display='';$('bpdf').style.display='none';
  const mesmo=mapa&&mapa.frameDe[prox]===mapa.frameDe[pag];
  $('prevlbl').textContent=mesmo?'Proximo passo':'Proximo slide';desenha(prox,$('cvp'),document.body.classList.contains('grande')?320:150);
}
// com zoom, a previa do celular mostra a mesma regiao ampliada (menos na ferramenta Zoom, que escolhe a regiao no slide inteiro):
// assim laser, foco, caneta e marca-texto apontam e escrevem direto na parte ampliada
function vistaZ(){
  if(zoom.z<=1.001||ferr==='zoom')return null;
  const w=1/zoom.z,cx=Math.max(w/2,Math.min(1-w/2,zoom.x)),cy=Math.max(w/2,Math.min(1-w/2,zoom.y));return{w,x0:cx-w/2,y0:cy-w/2};
}
function aplicaVistaZoom(){
  const V=vistaZ();nitidezPrev();
  [$('cv'),$('ovp'),$('cap')].forEach(el=>{if(!el)return;el.style.transformOrigin='0 0';
    el.style.transform=V?'scale('+zoom.z+') translate('+(-V.x0*100)+'%,'+(-V.y0*100)+'%)':''});
}
function retangulo(){
  aplicaVistaZoom();$('zv').textContent=$('zfv').textContent=(zoom.z%1?zoom.z.toFixed(1):zoom.z)+'x';
  const r=$('zrect'),z=zoom.z;if(z<=1.001||ferr!=='zoom'){r.style.display='none';return}
  const w=1/z,cx=Math.max(w/2,Math.min(1-w/2,zoom.x)),cy=Math.max(w/2,Math.min(1-w/2,zoom.y));
  r.style.display='block';r.style.width=w*100+'%';r.style.height=w*100+'%';r.style.left=(cx-w/2)*100+'%';r.style.top=(cy-w/2)*100+'%';
}
let tm={rodando:false,elapsed:0,rec:performance.now()};
const mmss=t=>{t=Math.max(0,Math.floor(t));const h=Math.floor(t/3600),m=Math.floor(t%3600/60),s=t%60;return(h?h+':'+String(m).padStart(2,'0'):String(m).padStart(2,'0'))+':'+String(s).padStart(2,'0')};
function sincTimer(t){if(t){tm={rodando:t.rodando,elapsed:t.elapsed,rec:performance.now()};mostraVoltas(t.voltas||[])}}
let voltasVistas='';
function mostraVoltas(v){
  const chave=v.join(',');if(chave===voltasVistas)return;voltasVistas=chave;
  const el=$('voltas');if(!el)return;el.innerHTML='';
  v.forEach((x,i)=>{const d=document.createElement('div');d.textContent='V'+(i+1)+'  '+mmss(x)+'  (+'+mmss(x-(i?v[i-1]:0))+')';el.appendChild(d)});
  el.scrollTop=el.scrollHeight;
}
function tick(){
  const el=tm.elapsed+(tm.rodando?(performance.now()-tm.rec)/1000:0),meta=cfg.meta_min*60,rest=meta-el,r=$('relogio');
  $('tempo').textContent=mmss(el);
  r.classList.toggle('pausado',!tm.rodando&&el>0);r.classList.toggle('alerta',rest<=meta*0.1&&rest>0);r.classList.toggle('estouro',rest<=0);
  $('restante').textContent=el===0&&!tm.rodando?'toque para iniciar':(rest>0?'restam '+mmss(rest):'+'+mmss(-rest)+' acima da meta')+(tm.rodando?'':'  (pausado)');
}
setInterval(tick,500);
$('relogio').onclick=()=>cmd('timer','toggle');$('tz').onclick=()=>cmd('timer','reset');
$('rvolta').onclick=e=>{e.stopPropagation();cmd('timer','lap')};$('rzera').onclick=e=>{e.stopPropagation();cmd('timer','reset')};
const esc=x=>String(x).replace(/&/g,'&amp;').replace(/</g,'&lt;');
function avisoUI(s){
  const el=$('aviso');
  if(s.acess===false){el.innerHTML='<b>O Mac nao deixou o Baton enviar teclas.</b> No computador: Ajustes do Sistema > Privacidade e Seguranca > Acessibilidade, ative o Baton (painel em localhost:8765/painel).'}
  else if(s.ativo===false){el.innerHTML='<b>Baton pausado</b> no computador. Retome pelo painel (localhost:8765/painel).'}
  else if(s.tela_falha){el.innerHTML='<b>A tela cheia do Baton nao conectou.</b> Feche a janela e use Apresentar de novo no painel, ou abra o PDF no seu leitor.'}
  else if(s.auth!==false){el.style.display='none';return}
  else if(s.pedido){el.innerHTML='<b>Apresentacao detectada</b> ('+esc(s.pedido.app)+'). Controlar com o Baton?<div class="avb"><button data-a="sim">Controlar</button><button data-a="sempre">Sempre neste app</button><button data-a="nao">Agora nao</button><button data-a="nunca">Nunca neste app</button></div>'}
  else{el.style.display='none';return}  // em espera sem apresentacao detectada: tocar em Proximo/Anterior ja libera o controle
  el.style.display='block';el.querySelectorAll('button').forEach(b=>b.onclick=()=>consentir(b.dataset.a));
}
function consentir(a){fetch('/consent',{method:'POST',headers:{'Content-Type':'application/json','X-Token':TOKEN},body:JSON.stringify({acao:a})}).then(r=>r.json()).then(aplicar).catch(()=>{})}
function aplicar(s){
  if(s.seq!==undefined&&s.seq<seqVisto)return;avisoUI(s);sincTimer(s.timer);if(s.seq!==undefined)seqVisto=s.seq;
  pag=s.pagina;total=s.total;$('tr').value=s.transicao;$('bb').classList.toggle('on',s.preto);document.querySelectorAll('#exk [data-k]').forEach(b=>b.classList.toggle('on',b.dataset.k===s.cortina));
  zoom={z:s.zoom||1,x:s.zx,y:s.zy};$('zv').textContent=(zoom.z%1?zoom.z.toFixed(1):zoom.z)+'x';retangulo();mostra()
}
// ---- miniatura: desenho local de caneta e marca-texto
const ovp=$('ovp');
function ajustaOvp(){const v=$('vista'),dpr=Math.min(devicePixelRatio||1,2);ovp.width=v.clientWidth*dpr;ovp.height=v.clientHeight*dpr;ovp.style.width='100%';ovp.style.height='100%'}
function limpaLocal(){ovp.getContext('2d').clearRect(0,0,ovp.width,ovp.height)}
function riscaLocal(t,pts,de){
  const g=ovp.getContext('2d'),w=ovp.width,h=ovp.height;g.save();g.lineCap='round';g.lineJoin='round';
  g.lineWidth=t==='hl'?Math.max(6,w*0.022):Math.max(2,w*0.004);g.strokeStyle=cores[t==='hl'?'hl':'pen'];if(t==='hl')g.globalAlpha=.4;
  g.beginPath();g.moveTo(pts[de][0]*w,pts[de][1]*h);for(let i=de+1;i<pts.length;i++)g.lineTo(pts[i][0]*w,pts[i][1]*h);if(pts.length===1)g.lineTo(pts[0][0]*w+.1,pts[0][1]*h);g.stroke();g.restore();
}
let lsAb=0,lsPend=null,lsSeq=0;const lsQid=Math.random().toString(36).slice(2,8);
function envLaser(x,y,on,m){const t=performance.now();if(on&&t-ultimo<25)return;ultimo=t;lsPend={x,y,on,m,r:raioFoco};bombeiaLaser()}
// ate 3 envios em voo: com RTT alto (relay, internet fraca) a taxa de pontos nao cai para 1/RTT; o servidor descarta os fora de ordem (q)
function bombeiaLaser(){
  while(lsPend&&lsAb<3){const d=lsPend;lsPend=null;d.q=++lsSeq;d.i=lsQid;lsAb++;
    const ac=new AbortController(),to=setTimeout(()=>ac.abort(),2500);
    fetch('/laser',{method:'POST',headers:{'Content-Type':'application/json','X-Token':TOKEN},body:JSON.stringify(d),keepalive:true,signal:ac.signal}).catch(()=>{}).finally(()=>{clearTimeout(to);lsAb--;bombeiaLaser()})}}
function posTela(e){const r=$('vista').getBoundingClientRect();return{x:Math.max(0,Math.min(1,(e.clientX-r.left)/r.width)),y:Math.max(0,Math.min(1,(e.clientY-r.top)/r.height))}}
function pos(e){const p=posTela(e),V=vistaZ();return V?{x:V.x0+p.x*V.w,y:V.y0+p.y*V.w}:p}  // coordenada no slide (considera o zoom)
function ponto(e,on){const t=posTela(e),{x,y}=pos(e);const p=$('ponto');p.style.display=on?'block':'none';p.style.left=t.x*100+'%';p.style.top=t.y*100+'%';envLaser(x,y,on,ferr==='foco'?'foco':'laser')}
let ultZ=0;
function enviaZoom(z,x,y,forcar){const t=performance.now();zoom={z,x,y};retangulo();$('zv').textContent=(z%1?z.toFixed(1):z)+'x';if(!forcar&&t-ultZ<90)return;ultZ=t;cmd('zoom',{z,x,y})}
// traco: acumula pontos e envia em lotes ordenados
let fila=Promise.resolve(),traco=null,bufTraco=[],timerTraco=null;
function enviaLote(fim){
  if(!traco)return;const p=bufTraco.splice(0);if(!p.length&&!fim)return;const body=JSON.stringify({id:traco.id,t:traco.t,c:cores[traco.t==='hl'?'hl':'pen'],p});
  fila=fila.then(()=>fetch('/draw',{method:'POST',headers:{'Content-Type':'application/json','X-Token':TOKEN},body,keepalive:true}).catch(()=>{}));
}
function iniciaTraco(e){const{x,y}=pos(e);traco={id:cid(),t:ferr==='marca'?'hl':'pen',pts:[[x,y]]};bufTraco=[[x,y]];riscaLocal(traco.t,traco.pts,0);timerTraco=setInterval(()=>enviaLote(false),60)}
function seguraTraco(e){const{x,y}=pos(e);const n=traco.pts.length;traco.pts.push([x,y]);bufTraco.push([x,y]);riscaLocal(traco.t,traco.pts,n-1)}
function fimTraco(){if(!traco)return;clearInterval(timerTraco);enviaLote(true);traco=null}
const toques=new Map();let dist0=0,z0=1,r0=.12,gest=null,pan=null;
const ZOOM_LIVRE=()=>!EXTERNO&&ferr!=='zoom'&&ferr!=='foco';  // pinca e arrasto de zoom com qualquer ferramenta (foco usa a pinca para o raio)
function distToques(){const[a,b]=[...toques.values()];return{d:Math.hypot(a.clientX-b.clientX,a.clientY-b.clientY),mx:(a.clientX+b.clientX)/2,my:(a.clientY+b.clientY)/2}}
$('vista').addEventListener('pointerdown',e=>{
  try{$('vista').setPointerCapture(e.pointerId)}catch(x){}toques.set(e.pointerId,e);
  if(toques.size===2&&ZOOM_LIVRE()){fimTraco();$('ponto').style.display='none';const g=distToques(),r=$('vista').getBoundingClientRect();
    gest={d0:g.d,z0:zoom.z,m0x:(g.mx-r.left)/r.width,m0y:(g.my-r.top)/r.height,cx:zoom.x,cy:zoom.y};return}
  if(gest)return;
  if(ferr===null&&!EXTERNO&&zoom.z>1.001){pan={x:e.clientX,y:e.clientY,cx:zoom.x,cy:zoom.y};return}
  if(ferr==='laser'||ferr==='foco')ponto(e,true);
  else if(ferr==='caneta'||ferr==='marca')iniciaTraco(e);
  else if(ferr==='zoom'){if(toques.size===2){const[a,b]=[...toques.values()];dist0=Math.hypot(a.clientX-b.clientX,a.clientY-b.clientY);z0=zoom.z}
    else{const{x,y}=pos(e);enviaZoom(zoom.z<=1.001?2:zoom.z,x,y,true)}}
});
$('vista').addEventListener('pointermove',e=>{
  if(!toques.has(e.pointerId))return;toques.set(e.pointerId,e);
  if(gest){if(toques.size>=2){const g=distToques(),r=$('vista').getBoundingClientRect(),z=Math.max(1,Math.min(5,Math.round(gest.z0*g.d/gest.d0*10)/10));
      const dx=(g.mx-r.left)/r.width-gest.m0x,dy=(g.my-r.top)/r.height-gest.m0y;
      enviaZoom(z,Math.max(0,Math.min(1,gest.cx-dx/z)),Math.max(0,Math.min(1,gest.cy-dy/z)))}return}
  if(pan){const r=$('vista').getBoundingClientRect(),w=1/zoom.z;
    enviaZoom(zoom.z,Math.max(0,Math.min(1,pan.cx-(e.clientX-pan.x)/r.width*w)),Math.max(0,Math.min(1,pan.cy-(e.clientY-pan.y)/r.height*w)));return}
  if(ferr==='foco'&&toques.size>=2){const[a,b]=[...toques.values()];const d=Math.hypot(a.clientX-b.clientX,a.clientY-b.clientY);
    if(dist0>0){raioFoco=Math.max(.04,Math.min(.3,r0*d/dist0));$('raio').value=Math.round(raioFoco*100);cfg.foco_raio=Math.round(raioFoco*100);enviaCfg({foco_raio:cfg.foco_raio});ponto(e,true)}else{dist0=d;r0=raioFoco}}
  else if(ferr==='laser'||ferr==='foco')ponto(e,true);
  else if((ferr==='caneta'||ferr==='marca')&&traco)seguraTraco(e);
  else if(ferr==='zoom'){
    if(toques.size>=2&&dist0>0){const[a,b]=[...toques.values()];const d=Math.hypot(a.clientX-b.clientX,a.clientY-b.clientY);
      enviaZoom(Math.max(1,Math.min(5,Math.round(z0*d/dist0*10)/10)),zoom.x,zoom.y)}
    else{const{x,y}=pos(e);enviaZoom(zoom.z,x,y)}}
});
['pointerup','pointercancel'].forEach(t=>$('vista').addEventListener(t,e=>{
  const eraGest=gest||pan;
  if(!eraGest&&(ferr==='laser'||ferr==='foco'))ponto(e,false);fimTraco();toques.delete(e.pointerId);if(toques.size<2)dist0=0;
  if(toques.size===0){gest=pan=null;if(eraGest||ferr==='zoom')cmd('zoom',zoom)}}));
['zfg','zfm','zfp'].forEach(i=>$(i).addEventListener('pointerdown',e=>e.stopPropagation()));
function aplicaGrande(){try{document.body.classList.toggle('grande',localStorage.getItem('baton-grande')==='1')}catch(e){}}
aplicaGrande();
$('zfg').onclick=()=>{const g=!document.body.classList.contains('grande');document.body.classList.toggle('grande',g);try{localStorage.setItem('baton-grande',g?'1':'0')}catch(e){}remedir()};
$('bpdf').onclick=()=>$('fpdf').click();
$('lns').href='/simples?k='+encodeURIComponent(TOKEN);
$('fpdf').onchange=async e=>{const f=e.target.files[0];if(!f)return;const l=$('prevlbl');l.textContent='Enviando '+f.name+'...';$('bpdf').style.display='none';
  try{const r=await fetch('/painel/deck?nome='+encodeURIComponent(f.name),{method:'POST',headers:{'X-Token':TOKEN},body:await f.arrayBuffer()});
    if(!r.ok){let m='';try{m=(await r.json()).erro}catch(x){}l.textContent='Falhou: '+(m||r.status);$('bpdf').style.display='block'}}
  catch(x){l.textContent='Falhou o envio';$('bpdf').style.display='block'}e.target.value=''};
$('zfm').onclick=()=>passo(-1);$('zfp').onclick=()=>passo(1);
$('seg').onclick=()=>cmd('next');$('ant').onclick=()=>cmd('prev');
// teclas de volume e de midia (alguns navegadores Android e teclados/fones Bluetooth as repassam a pagina; no iOS e no Chrome
// Android o sistema consome o botao de volume e nenhuma pagina web o recebe)
addEventListener('keydown',e=>{
  if(e.target&&/^(INPUT|TEXTAREA|SELECT)$/.test(e.target.tagName))return;
  if(['AudioVolumeUp','VolumeUp','MediaTrackNext','PageDown','ArrowRight'].includes(e.key)){cmd('next');e.preventDefault()}
  else if(['AudioVolumeDown','VolumeDown','MediaTrackPrevious','PageUp','ArrowLeft'].includes(e.key)){cmd('prev');e.preventDefault()}
});
__JSMIDIA__
if('serviceWorker' in navigator&&isSecureContext)navigator.serviceWorker.register('/sw.js').catch(()=>{});
$('bb').onclick=()=>cmd('blank');$('tr').onchange=e=>cmd('transition',e.target.value);
$('bx').onclick=()=>{limpaLocal();cmd('limpar')};
const BOTAO={laser:'bl',foco:'bf',caneta:'bc',marca:'bm',zoom:'bz'};
function marcaCores(){const t=ferr==='marca'?'hl':'pen';document.querySelectorAll('#cores i').forEach(i=>i.classList.toggle('sel',i.dataset.c===cores[t]))}
function ferramenta(t){
  ferr=ferr===t?null:t;for(const k in BOTAO)$(BOTAO[k]).classList.toggle('on',ferr===k);
  $('zbar').style.display=ferr==='zoom'?'flex':'none';
  $('cores').style.display=(ferr==='caneta'||ferr==='marca')?'flex':'none';
  $('raiobar').style.display=ferr==='foco'?'flex':'none';marcaCores();retangulo();$('ponto').style.display='none';
}
for(const k in BOTAO)$(BOTAO[k]).onclick=()=>ferramenta(k);
document.querySelectorAll('#cores i').forEach(i=>i.onclick=()=>{cores[ferr==='marca'?'hl':'pen']=i.dataset.c;marcaCores()});
$('raio').value=Math.round(raioFoco*100);
$('raio').oninput=e=>enviaCfg({foco_raio:parseInt(e.target.value)});
function montaAjustes(){
  const c=$('ajcorpo');c.innerHTML='';
  DEFS.forEach(([k,rot,lo,hi,st])=>{const d=document.createElement('div');d.className='ajl';
    d.innerHTML='<label>'+rot+'</label><input type="range" id="aj_'+k+'" min="'+lo+'" max="'+hi+'" step="'+st+'"><output id="av_'+k+'"></output>';c.appendChild(d);
    d.querySelector('input').oninput=e=>enviaCfg({[k]:parseFloat(e.target.value)})});
  const lc=document.createElement('div');lc.className='ajc';lc.innerHTML='<span>Cor do laser</span>';
  ['#ff2d2d','#0a84ff','#30d158','#ffd60a','#ffffff','#000000'].forEach(h=>{const i=document.createElement('i');i.style.background=h;i.dataset.c=h;i.onclick=()=>enviaCfg({laser_cor:h});lc.appendChild(i)});
  c.appendChild(lc);mostraCfg();
}
function mostraCfg(){
  DEFS.forEach(([k])=>{const i=$('aj_'+k);if(i){i.value=cfg[k];$('av_'+k).textContent=cfg[k]}});
  document.querySelectorAll('.ajc i').forEach(i=>i.classList.toggle('sel',i.dataset.c===cfg.laser_cor));
  raioFoco=cfg.foco_raio/100;$('raio').value=Math.round(cfg.foco_raio);
}
function enviaCfg(parte){
  Object.assign(cfg,parte);mostraCfg();clearTimeout(tCfg);
  tCfg=setTimeout(()=>fetch('/cfg',{method:'POST',headers:{'Content-Type':'application/json','X-Token':TOKEN},body:JSON.stringify(parte)}).catch(()=>{}),120);
}
function recebeCfg(c){cfg=Object.assign({},PADRAO,c);mostraCfg()}
function painel(nome){
  const alvo=$(nome).style.display==='flex'?null:nome;
  ['ajustes','extras'].forEach(p=>$(p).style.display=p===alvo?'flex':'none');$('notas').style.display=alvo?'none':'flex';
  $('ba').classList.toggle('on',alvo==='ajustes');$('bk').classList.toggle('on',alvo==='extras');
}
$('ba').onclick=()=>painel('ajustes');$('bk').onclick=()=>painel('extras');$('exx').onclick=()=>painel('extras');
['fimtxt','fimsub'].forEach(i=>{try{const v=localStorage.getItem('baton-'+i);if(v!==null)$(i).value=v}catch(e){}$(i).oninput=e=>{try{localStorage.setItem('baton-'+i,e.target.value)}catch(x){}}});
document.querySelectorAll('#exk [data-k]').forEach(b=>b.onclick=()=>cmd('cortina',b.dataset.k==='fim'?{k:'fim',txt:$('fimtxt').value,sub:$('fimsub').value}:b.dataset.k));
document.querySelectorAll('#exs [data-s]').forEach(b=>b.onclick=()=>b.dataset.s==='confete'?cmd('confete',4):cmd('efeito',b.dataset.s));
$('ajrst').onclick=()=>{cfg=Object.assign({},PADRAO);mostraCfg();fetch('/cfg',{method:'POST',headers:{'Content-Type':'application/json','X-Token':TOKEN},body:JSON.stringify(PADRAO)}).catch(()=>{})};
let gradeFeita=false;
function irPara(n){
  n=parseInt(n);if(!n||n<1)return;
  const pagina=(TEM_PDF&&mapa)?(mapa.paginas[Math.min(n,mapa.total)]||[n])[0]:n;
  cmd('goto',pagina);fechaIr();
}
function fechaIr(){$('irbox').style.display='none'}
async function abreIr(){
  $('irbox').style.display='flex';$('irn').value='';
  if(TEM_PDF&&pdf&&mapa){
    const g=$('irgrid'),atual=mapa.frameDe[pag];
    if(!gradeFeita){gradeFeita=true;g.innerHTML='';
      for(let f=1;f<=mapa.total;f++){const d=document.createElement('div'),c=document.createElement('canvas'),sp=document.createElement('span');
        sp.textContent=f;d.dataset.f=f;d.appendChild(c);d.appendChild(sp);d.onclick=()=>irPara(f);g.appendChild(d)}
      for(let f=1;f<=mapa.total;f++){const c=g.children[f-1].querySelector('canvas');await desenha(mapa.paginas[f][0],c,150)}}
    [...g.children].forEach(d=>d.classList.toggle('atual',+d.dataset.f===atual));
    const alvo=g.children[atual-1];if(alvo)alvo.scrollIntoView({block:'center'});
  }else $('irgrid').innerHTML='<p style="color:#888;font-size:14px;grid-column:1/-1">Digite o numero do slide e toque em Ir.'+(EXTERNO?' No modo externo o Baton volta ao inicio e avanca esse numero de vezes.':'')+'</p>';
}
$('bg').onclick=abreIr;$('irx').onclick=fechaIr;$('irgo').onclick=()=>irPara($('irn').value);
$('irn').addEventListener('keydown',e=>{if(e.key==='Enter')irPara($('irn').value)});
function passo(d){let i=NIVEIS.findIndex(n=>n>=zoom.z-1e-6);if(i<0)i=NIVEIS.length-1;i=Math.max(0,Math.min(NIVEIS.length-1,i+d));enviaZoom(NIVEIS[i],zoom.x,zoom.y,true)}
$('zp').onclick=()=>passo(1);$('zm').onclick=()=>passo(-1);$('z1').onclick=()=>enviaZoom(1,.5,.5,true);
function tam(d){fonte=Math.max(12,Math.min(34,fonte+d));try{localStorage.setItem('baton-fonte',fonte)}catch(e){}$('ntxt').style.fontSize=fonte+'px'}
$('np').onclick=()=>tam(2);$('nm').onclick=()=>tam(-2);
function textoTodas(){const todas=Object.assign({},notasPdf,notas);return Object.keys(todas).filter(k=>todas[k]).sort((a,b)=>parseInt(a.replace(/\D/g,''))-parseInt(b.replace(/\D/g,''))).map(k=>'## '+k+'\n'+todas[k]).join('\n\n')}
function editor(modo){
  editando=modo;const q=fq(),ed=$('ned');
  ed.value=modo==='todas'?textoTodas():(notas['p'+pag]??notas[String(q.f)]??notasPdf['p'+pag]??'');ed.style.fontSize=fonte+'px';
  ed.placeholder=modo==='todas'?'## 1\nnota do slide 1\n\n## 2\nnota do slide 2':'Digite a nota do slide '+pag;
  $('ntxt').style.display='none';ed.style.display='block';$('nbt').style.display='flex';$('ne').style.display='none';
  $('ntd').textContent=modo==='todas'?'So este':'Todas';$('nt').textContent=modo==='todas'?'Todas as notas (Markdown)':'Editando slide '+pag;ed.focus();
}
function fechaEditor(){editando=null;$('ned').style.display='none';$('nbt').style.display='none';$('ntxt').style.display='';$('ne').style.display='';$('nt').textContent='Notas do apresentador';mostraNotas()}
$('ne').onclick=()=>editor('slide');
$('ntd').onclick=()=>{if(editando==='todas'){fechaEditor();editor('slide')}else editor('todas')};
$('ncn').onclick=fechaEditor;
$('nsv').onclick=async()=>{
  const corpo=editando==='todas'?{tudo:$('ned').value}:{p:'p'+pag,t:$('ned').value};
  try{const r=await fetch('/notas',{method:'POST',headers:{'Content-Type':'application/json','X-Token':TOKEN},body:JSON.stringify(corpo)});
    if(r.ok){notas=(await r.json()).notas||{};fechaEditor();return}}catch(e){}
  $('nt').textContent='Falha ao salvar: tente de novo';
};
$('pos').onclick=abreIr;
async function carregaCfg(){try{const r=await fetch('/cfg?k='+encodeURIComponent(TOKEN));if(r.ok)recebeCfg(await r.json())}catch(e){}}
async function carregaNotas(){try{const r=await fetch('/notas?k='+encodeURIComponent(TOKEN));if(r.ok){notas=(await r.json()).notas||{};mostraNotas()}}catch(e){}}
async function wake(){try{if('wakeLock' in navigator)await navigator.wakeLock.request('screen')}catch(e){}}
function conectar(){
  if(es){try{es.close()}catch(e){}}
  status(false,'conectando...');es=new EventSource('/events?k='+encodeURIComponent(TOKEN));ultimaMsg=Date.now();
  es.onopen=()=>status(true,'conectado');
  es.onerror=()=>status(false,'reconectando...');
  es.onmessage=ev=>{ultimaMsg=Date.now();const m=JSON.parse(ev.data);if(m.deck){location.reload();return}if(m.hb){status(true,'conectado');sincTimer(m.timer);return}if(m.limpar){limpaLocal();return}if(m.cfg){recebeCfg(m.cfg);return}if(m.laser||m.draw||m.tracos)return;aplicar(m)};
}
setInterval(()=>{if(Date.now()-ultimaMsg>12000)conectar()},3000);
// rede: mede o atraso e, sem conexao por ~6 s, troca para outro endereco do computador que esteja respondendo
var rtt=0,viaNome='';
function viaAtual(){const u=URLS.find(x=>{try{return new URL(x.url).origin===location.origin}catch(e){return false}});return u?u.nome:''}
function sondar(origem,ms){const ac=new AbortController(),t=setTimeout(()=>ac.abort(),ms),t0=performance.now();
  return fetch(origem+'/estado',{mode:origem===location.origin?'same-origin':'no-cors',cache:'no-store',signal:ac.signal}).then(()=>{clearTimeout(t);return performance.now()-t0}).catch(()=>{clearTimeout(t);return -1})}
async function medeRtt(){const r=await sondar(location.origin,2500);if(r>=0){rtt=Math.round(r);viaNome=viaAtual();status(true,'conectado')}}
setInterval(()=>{if(Date.now()-ultimaMsg<6000)medeRtt()},5000);
let trocando=false;
setInterval(async()=>{
  if(trocando||URLS.length<2||Date.now()-ultimaMsg<6000)return;trocando=true;
  for(const u of URLS){let o;try{o=new URL(u.url).origin}catch(e){continue}if(o===location.origin)continue;
    if(await sondar(o,2000)>=0){location.replace(u.url);return}}
  trocando=false},4000);
document.addEventListener('visibilitychange',()=>{if(document.visibilityState==='visible'){wake();conectar()}});
addEventListener('online',conectar);addEventListener('pageshow',conectar);wake();carregaNotas();montaAjustes();carregaCfg();
if(TEM_PDF){pdfjsLib.getDocument(Object.assign({url:'/pdf?k='+encodeURIComponent(TOKEN)},LEVE?{disableAutoFetch:true,disableStream:true,rangeChunkSize:65536}:{})).promise.then(async d=>{pdf=d;total=d.numPages;try{await detectaLado(d);if(!LEVE){notasPdf=await notasDoPdf(d);mapa=await agruparPassos(d)}}catch(e){}mostraNotas();conectar()})}
else{$('vista').style.background='#1a1a1c';$('vista').insertAdjacentHTML('beforeend','<div id="lzdica" style="position:absolute;inset:0;display:flex;align-items:center;justify-content:center;color:#666;font-size:14px;text-align:center;padding:12px;pointer-events:none">area do laser<br>(mapeada na tela do Mac)</div>');conectar()}
"""
JS_REMOTO = JS_REMOTO.replace("__JSMIDIA__", JS_MIDIA)


PAGINA_QR = """<!doctype html><html lang="pt-br"><head><meta charset="utf-8"><title>Baton - QR</title>
<script src="__QRJS__"></script>
<style>__CSS__</style></head><body class="tela"><div id="qr" style="display:flex"><h1>Escaneie com o celular para controlar</h1><div id="qrlista" style="display:flex;gap:24px;flex-wrap:wrap;justify-content:center"></div>
<p style="opacity:.8">Cada QR vale para um celular e muda depois de usado. <span id="cel"></span></p>
<p>Abra a apresentacao no leitor que quiser (PowerPoint, Keynote, ReLaTeX...), deixe-a em tela cheia e use o celular. Painel: <a href="/painel" style="color:#0a84ff">/painel</a></p></div>
<script>
let atual='';
function desenhar(urls){
  const chave=urls.map(function(u){return u.url}).join('|');if(chave===atual)return;atual=chave;
  const lista=document.getElementById('qrlista');lista.innerHTML='';
  urls.forEach(function(u){const d=document.createElement('div');d.style.cssText='display:flex;flex-direction:column;align-items:center;max-width:320px';
    d.innerHTML='<h2 style="font-size:18px;margin:0 0 8px">'+u.nome+'</h2><div class="cartao" style="width:260px"></div><p style="word-break:break-all;font-size:12px;opacity:.7"></p>';
    d.querySelector('p').textContent=u.url;
    try{const q=qrcode(0,'M');q.addData(u.url);q.make();d.querySelector('.cartao').innerHTML=q.createSvgTag({cellSize:8,margin:0,scalable:true})}catch(e){d.querySelector('.cartao').textContent='QR indisponivel'}
    lista.appendChild(d)});
}
desenhar(__URLS__);
async function ciclo(){try{const j=await (await fetch('/painel/estado')).json();desenhar(j.pareamento_urls||[]);
  document.getElementById('cel').textContent=((j.dispositivos||[]).length?('Celulares pareados: '+j.dispositivos.map(function(d){return d.nome+(d.conectado?' (conectado)':'')}).join(', ')):'Nenhum celular pareado ainda.')+(j.relay?'  |  Relay: '+j.relay:'')}catch(e){}}
ciclo();setInterval(ciclo,2000);
</script></body></html>"""


PAGINA_SIMPLES = r"""<!doctype html><html lang="pt-br"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1,user-scalable=no,viewport-fit=cover">
<meta name="apple-mobile-web-app-capable" content="yes"><meta name="mobile-web-app-capable" content="yes"><meta name="apple-mobile-web-app-status-bar-style" content="black">
<meta name="apple-mobile-web-app-title" content="Baton"><meta name="theme-color" content="#000000">
<link rel="manifest" href="/manifest.json?k=__TOKEN__&m=simples"><link rel="apple-touch-icon" href="/icon.png"><title>Baton simples</title>
<style>
*{box-sizing:border-box;-webkit-tap-highlight-color:transparent;-webkit-touch-callout:none}
html,body{margin:0;height:100%;background:#000;color:#fff;font-family:-apple-system,system-ui,sans-serif;touch-action:none;user-select:none;-webkit-user-select:none;overflow:hidden}
body{display:grid;grid-template-columns:minmax(0,1fr);grid-template-rows:auto 1fr;gap:8px;padding:max(22px,calc(env(safe-area-inset-top) + 14px)) max(10px,env(safe-area-inset-right)) max(14px,calc(env(safe-area-inset-bottom) + 6px)) max(10px,env(safe-area-inset-left))}
#topo{display:flex;justify-content:space-between;align-items:center;gap:6px;font-size:13px;color:#aaa;min-height:34px;min-width:0}
#dot.ok+#st{display:none}#ms{margin-left:2px;font-variant-numeric:tabular-nums}#ms.lento{color:#ffd60a}#ms.ruim{color:#ff453a}#topo>span:first-child{min-width:0;white-space:nowrap}
#dot{width:10px;height:10px;border-radius:50%;background:#e33;display:inline-block;margin-right:6px}#dot.ok{background:#3c6}
#topo .acoes{display:flex;gap:6px;align-items:center}
#tempo{font-variant-numeric:tabular-nums;font-size:15px;letter-spacing:.5px;min-width:68px;text-align:center;touch-action:manipulation}
#tempo.rodando{background:#12301c}#tempo.alerta{color:#ffd60a}#tempo.estouro{color:#ff453a}#tempo.pausado{opacity:.6}
#folha .tm{display:flex;gap:8px;align-items:center;margin:10px 0 2px;font-size:13px;color:#aaa}#folha .tm button{padding:9px 14px;font-size:13px;border-radius:12px;border:0;background:#1d1d1f;color:#fff;font-weight:600}
#voltas{font-size:12px;color:#888;max-height:54px;overflow:auto;margin-top:4px;font-variant-numeric:tabular-nums}
.chip{border:0;background:#1d1d1f;color:#fff;border-radius:14px;padding:7px 10px;font-size:13px;font-weight:600;text-decoration:none;display:inline-block;white-space:nowrap}
.chip.az{background:#0a84ff}
#corpo{display:grid;grid-template-columns:minmax(0,1fr);grid-template-rows:minmax(0,.9fr) minmax(0,1.05fr) minmax(0,.9fr) minmax(0,1.2fr);gap:10px;min-height:0;
  grid-template-areas:"nav" "zoom" "foco" "laser"}
button.g{border:0;border-radius:24px;color:#fff;font-weight:700;touch-action:manipulation;font-size:clamp(20px,6.2vmin,36px);position:relative;min-height:0}
#navrow{grid-area:nav;display:flex;gap:10px;min-height:0}#navrow button{min-height:0}
#ant{background:#1d1d1f;flex:.8}#seg{background:#0a84ff;flex:1.2}#seg:active{background:#006fe0}#ant:active{background:#333}
#zrow{grid-area:zoom;display:flex;flex-direction:column;gap:8px;min-height:0}
#zctl{display:flex;gap:8px;align-items:center;flex:none;height:46px}
#zctl button{width:50px;height:46px;border:0;border-radius:14px;background:#1d1d1f;color:#fff;font-weight:700;font-size:22px;touch-action:manipulation}
#zctl button:active{background:#333}#zctl #z1{width:58px;font-size:15px}
#zniv{flex:1;min-width:0;accent-color:#0a84ff;touch-action:pan-x;height:34px}
#zrow #zaim{flex:1;min-height:0;border-radius:24px;color:#fff;font-weight:700;position:relative;background:#10223a;border:2px solid #0a84ff;touch-action:none;display:flex;flex-direction:column;align-items:center;justify-content:center;font-size:17px;line-height:1.15}
#zrow #zaim small{position:static;display:block;font-size:11px;font-weight:500;color:#9fb4cc;padding:0}
#zrow.ativo #zaim{background:#16335a}#zrow #zaim.on{background:#0a84ff}#zrow #zaim.on small{color:#fff}
#foco{grid-area:foco}#laser{grid-area:laser}
#laser,#foco{touch-action:none}
#laser{background:#3a1212;border:3px solid #ff3b30}#laser.on{background:#ff3b30}
#foco{background:#10223a;border:3px solid #0a84ff}#foco.on{background:#0a84ff}
body.canhoto #navrow,body.canhoto #zctl{flex-direction:row-reverse}
.dica{position:absolute;left:0;right:0;bottom:9px;font-size:12px;font-weight:500;color:#cfd3d8;padding:0 8px;line-height:1.25}
.on .dica{color:#fff}
.mapa{display:none;position:absolute;inset:0;pointer-events:none}
.mapa i{position:absolute;width:14px;height:14px;margin:-7px 0 0 -7px;border-radius:50%;background:#fff;box-shadow:0 0 8px 3px rgba(255,255,255,.7)}
.on .mapa{display:block}
#laser.on,#foco.on{display:flex;align-items:center;justify-content:flex-start;padding-left:26px}
#folha{display:none;position:fixed;left:0;right:0;bottom:0;background:#141416;border-radius:22px 22px 0 0;padding:14px 16px max(16px,env(safe-area-inset-bottom));z-index:10;box-shadow:0 -8px 30px rgba(0,0,0,.6)}
#folha.aberta{display:block}
#folha label{display:flex;align-items:center;gap:10px;font-size:14px;color:#ccc;margin:10px 0}#folha input{flex:1;min-width:0;accent-color:#0a84ff}#folha output{min-width:34px;text-align:right;color:#888}
#folha h3{margin:0 0 4px;font-size:15px;color:#fff;display:flex;justify-content:space-between;align-items:center}
#folha .mao{display:flex;gap:8px;margin-top:10px}#folha .mao button{flex:1;padding:11px 0;font-size:14px;border-radius:12px;border:0;background:#1d1d1f;color:#fff;font-weight:600}#folha .mao button.sel{background:#0a84ff}
#aviso{display:none;position:fixed;left:10px;right:10px;top:max(60px,calc(env(safe-area-inset-top) + 48px));z-index:12;background:#3a1212;border:1px solid #ff453a;border-radius:14px;padding:11px 13px;font-size:14px;line-height:1.35}
#dicainst{display:none;position:fixed;left:12px;right:12px;top:max(50px,env(safe-area-inset-top));background:#2c2c2e;border:1px solid #0a84ff;border-radius:14px;padding:12px;font-size:14px;z-index:11;line-height:1.4}
@media (orientation:landscape){
  body{padding-top:max(10px,env(safe-area-inset-top))}
  #corpo{grid-template-columns:minmax(0,1fr) minmax(0,1.5fr);grid-template-rows:minmax(0,1.2fr) minmax(0,1fr);grid-template-areas:"nav foco" "zoom laser"}
  body.canhoto #corpo{grid-template-columns:minmax(0,1.5fr) minmax(0,1fr);grid-template-areas:"foco nav" "laser zoom"}
}
</style></head><body class="destro">
<div id="topo"><span><i id="dot"></i><span id="st">conectando</span><small id="ms"></small></span>
  <button class="chip" id="tempo" title="Toque: iniciar/pausar. Segure: volta">00:00</button>
  <span class="acoes"><button class="chip az" id="binst" style="display:none">Instalar</button><button class="chip" id="bcfg">Ajustes</button><a class="chip" href="/remote?k=__TOKEN__">Completo</a></span></div>
<div id="corpo">
  <div id="navrow"><button class="g" id="ant">&#9664; Anterior</button><button class="g" id="seg">Proximo &#9654;</button></div>
  <div id="zrow"><div id="zctl"><button id="zm">&minus;</button><input id="zniv" type="range" min="1" max="5" step="0.1" value="1"><button id="zp">+</button><button id="z1">1x</button></div><button id="zaim"><div class="mapa"><i></i></div><span id="zt">Zoom 1x</span><small class="dica">toque onde quer ampliar</small></button></div>
  <button class="g" id="foco">FOCO<div class="mapa"><i></i></div><div class="dica">Toque onde quer e aponte; 2o dedo: raio</div></button>
  <button class="g" id="laser">LASER<div class="mapa"><i></i></div><div class="dica">Toque onde quer e aponte</div></button>
</div>
<div id="folha">
  <h3>Ajustes <button class="chip" id="bfecha">Fechar</button></h3>
  <div class="tm"><span>Cronometro</span><button id="tvolta">Volta</button><button id="tzera">Zerar</button><span id="trest"></span></div><div id="voltas"></div>
  <label>Meta do cronometro (min)<input id="s_meta" type="range" min="1" max="180" value="50"><output>50</output></label>
  <div class="mao" id="metas"><button data-m="10">10</button><button data-m="15">15</button><button data-m="20">20</button><button data-m="30">30</button><button data-m="45">45</button><button data-m="60">60</button></div>
  <label>Laser: tamanho<input id="s_laser_tam" type="range" min="6" max="120" value="24"><output>24</output></label>
  <label>Foco: raio<input id="raio" type="range" min="3" max="35" value="12"><output id="rv">12</output></label>
  <label>Foco: desfoque<input id="s_foco_blur" type="range" min="0" max="100" value="100"><output>100</output></label>
  <label>Foco: escuro<input id="s_foco_escuro" type="range" min="0" max="90" value="25"><output>25</output></label>
  <label>Resposta do apontador<input id="s_resp" type="range" min="0" max="100" value="60"><output>60</output></label>
  <label>Alcance do apontador (graus)<input id="s_gan" type="range" min="15" max="80" value="35"><output>35</output></label>
  <div class="mao"><button id="mdestro">Destro</button><button id="mcanhoto">Canhoto</button></div>
</div>
<div id="dicainst"></div><div id="aviso"></div>
<script>
const TOKEN="__TOKEN__",HTTPS="__HTTPS__",AUTO="__AUTO__"==="1",EXTERNO="__EXTERNO__"==="1",$=id=>document.getElementById(id);
let ultimaMsg=Date.now(),es=null;
function status(ok,t){$('dot').classList.toggle('ok',ok);$('st').textContent=t}
const cid=()=>Date.now().toString(36)+Math.random().toString(36).slice(2,8);
// ---- zoom: laser/foco apontam na tela; com zoom, a tela mostra so uma regiao do slide, e o ponto precisa cair nela
let zoom={z:1,x:.5,y:.5},ultSlide={x:.5,y:.5},ASP=16/9;
const NIVEIS=[1,1.5,2,3,4,5];
function regiao(){const w=1/Math.max(1,zoom.z),cx=Math.max(w/2,Math.min(1-w/2,zoom.x)),cy=Math.max(w/2,Math.min(1-w/2,zoom.y));return{w,x0:cx-w/2,y0:cy-w/2}}
function paraSlide(x,y){if(zoom.z<=1.001)return{x,y};const r=regiao();return{x:r.x0+x*r.w,y:r.y0+y*r.w}}
function mostraZoom(){$('zt').textContent='Zoom '+(zoom.z%1?zoom.z.toFixed(1):zoom.z)+'x';$('zrow').classList.toggle('ativo',zoom.z>1.001);if(!arrastaNiv)$('zniv').value=zoom.z}
let arrastaNiv=false;
$('zniv').addEventListener('pointerdown',()=>{arrastaNiv=true;dicaZoomSistema()});
['pointerup','pointercancel'].forEach(t=>$('zniv').addEventListener(t,()=>{arrastaNiv=false}));
$('zniv').oninput=e=>{const z=Math.round(parseFloat(e.target.value)*10)/10,c=zoom.z<=1.001&&z>1?ultSlide:{x:zoom.x,y:zoom.y};
  zoom={z,x:z>1?c.x:.5,y:z>1?c.y:.5};mostraZoom();zPend={z:zoom.z,x:zoom.x,y:zoom.y};if(!zEnv)bombeiaZoom()};
$('zniv').onchange=()=>{arrastaNiv=false;mostraZoom();cmd('zoom',zoom)};
function passoZoom(d){dicaZoomSistema();let i=NIVEIS.findIndex(n=>n>=zoom.z-1e-6);if(i<0)i=NIVEIS.length-1;i=Math.max(0,Math.min(NIVEIS.length-1,i+d));const z=NIVEIS[i];
  const c=zoom.z<=1.001&&z>1?ultSlide:{x:zoom.x,y:zoom.y};  // ao ampliar a partir de 1x, centraliza onde voce apontou por ultimo
  zoom={z,x:z>1?c.x:.5,y:z>1?c.y:.5};mostraZoom();cmd('zoom',zoom)}
$('zp').onclick=()=>{passoZoom(1);navigator.vibrate&&navigator.vibrate(8)};$('zm').onclick=()=>{passoZoom(-1);navigator.vibrate&&navigator.vibrate(8)};
$('z1').onclick=()=>{zoom={z:1,x:.5,y:.5};mostraZoom();cmd('zoom',zoom)};
// modo externo (app de terceiros): o Baton nao amplia a janela do outro programa; usa o zoom nativo do sistema (atalhos), que segue o cursor
let zsisNome=null;
function zoomSistemaUI(s){if(!EXTERNO||s.zoom_sistema===undefined)return;zsisNome=s.zoom_sistema;
  $('zrow').style.display=s.zoom_sistema?'flex':'none';
  if(s.zoom_sistema)$('zaim').querySelector('small').textContent=s.zoom_sistema==='Lupa'?'lupa: toque e arraste onde quer ampliar':'zoom do sistema: toque onde quer ampliar'}
function dicaZoomSistema(){let k=null;try{k=localStorage.getItem('baton-zsis')}catch(e){}if(k||!EXTERNO||!zsisNome||zsisNome==='Lupa')return;
  try{localStorage.setItem('baton-zsis','1')}catch(e){}
  const so=window.SO_PC||'',t=so==='darwin'?'No Mac, para o zoom funcionar: Ajustes do Sistema > Acessibilidade > Zoom > ative "Usar atalhos de teclado para zoom".':
    so==='win32'?'No Windows o zoom abre a Lupa do sistema; para fechar, volte a 1x.':'Zoom pelo '+zsisNome+'; se nao responder, ative o recurso nas configuracoes de acessibilidade.';
  const el=$('aviso');el.innerHTML='<b>Zoom do sistema</b><br>'+t;el.style.display='block';setTimeout(()=>{if(/Zoom do sistema/.test(el.innerHTML))el.style.display='none'},9000)}
function avisoUI(s){const el=$('aviso');let t='';
  if(s.acess===false)t='<b>O Mac nao deixou o Baton enviar teclas.</b> No Mac: Ajustes do Sistema > Privacidade e Seguranca > Acessibilidade, ative o Baton.';
  else if(s.ativo===false)t='<b>Baton pausado</b> no computador. Retome em localhost:8765/painel.';
  else if(s.tela_falha)t='<b>A tela cheia do Baton nao conectou.</b> Feche a janela e use Apresentar de novo no painel, ou abra o PDF no seu leitor.';
  if(!t&&/Zoom do sistema/.test(el.innerHTML))return;el.innerHTML=t;el.style.display=t?'block':'none'}
async function medeMs(){if(document.visibilityState!=='visible')return;const t0=performance.now();
  try{const r=await fetch('/estado',{cache:'no-store',headers:{'X-Token':TOKEN}});await r.text();const ms=Math.round(performance.now()-t0);
    const e=$('ms');e.textContent=ms+' ms';e.className=ms>300?'ruim':ms>120?'lento':''}catch(x){$('ms').textContent=''}}
setInterval(medeMs,5000);setTimeout(medeMs,1500);
function aplicaSnap(s){if(s.so)window.SO_PC=s.so;if(s.aspecto>0)ASP=s.aspecto;zoomSistemaUI(s);if(s.acess!==undefined||s.ativo!==undefined)avisoUI(s);if(s.cfg){mostraAj(s.cfg);tick();return}if(s.timer)sincTimer(s.timer);if(s.zoom!==undefined&&ativo!=='zoom'){zoom={z:s.zoom||1,x:s.zx===undefined?.5:s.zx,y:s.zy===undefined?.5:s.zy};mostraZoom()}}
// ---- cronometro (o mesmo do controle completo: vive no computador, todos os celulares veem o mesmo tempo)
let tm={rodando:false,elapsed:0,rec:performance.now()},metaMin=50,voltasVistas='';
const mmss=t=>{t=Math.max(0,Math.floor(t));const h=Math.floor(t/3600),m=Math.floor(t%3600/60),s=t%60;return(h?h+':'+String(m).padStart(2,'0'):String(m).padStart(2,'0'))+':'+String(s).padStart(2,'0')};
function sincTimer(t){tm={rodando:t.rodando,elapsed:t.elapsed,rec:performance.now()};const v=t.voltas||[],ch=v.join(',');
  if(ch!==voltasVistas){voltasVistas=ch;$('voltas').innerHTML=v.map((x,i)=>'V'+(i+1)+'  '+mmss(x)+'  (+'+mmss(x-(i?v[i-1]:0))+')').reverse().join('<br>')}}
function tick(){const el=tm.elapsed+(tm.rodando?(performance.now()-tm.rec)/1000:0),meta=metaMin*60,rest=meta-el,b=$('tempo');
  b.textContent=mmss(el);b.classList.toggle('rodando',tm.rodando);b.classList.toggle('pausado',!tm.rodando&&el>0);b.classList.toggle('alerta',rest<=meta*.1&&rest>0);b.classList.toggle('estouro',rest<=0);
  $('trest').textContent=el===0&&!tm.rodando?'':(rest>0?'restam '+mmss(rest):'+'+mmss(-rest)+' acima da meta')}
setInterval(tick,500);tick();
let tLongo=null,longo=false;
$('tempo').addEventListener('pointerdown',()=>{longo=false;tLongo=setTimeout(()=>{longo=true;cmd('timer','lap');navigator.vibrate&&navigator.vibrate(20)},600)});
['pointerup','pointercancel','pointerleave'].forEach(t=>$('tempo').addEventListener(t,()=>clearTimeout(tLongo)));
$('tempo').addEventListener('click',()=>{if(!longo){cmd('timer','toggle');navigator.vibrate&&navigator.vibrate(8)}});
$('tvolta').onclick=()=>cmd('timer','lap');$('tzera').onclick=()=>cmd('timer','reset');
async function cmd(c,v){const id=cid();
  for(let t=0;t<4;t++){const ac=new AbortController(),to=setTimeout(()=>ac.abort(),2000);
    try{const r=await fetch('/cmd',{method:'POST',headers:{'Content-Type':'application/json','X-Token':TOKEN},body:JSON.stringify({cmd:c,valor:v,cid:id}),signal:ac.signal});clearTimeout(to);
      if(r.ok||r.status===423){status(true,'conectado');try{aplicaSnap(await r.clone().json())}catch(e){}return r.ok}}catch(e){clearTimeout(to)}
    status(false,'reenviando...');await new Promise(r=>setTimeout(r,200*(t+1)))}
  status(false,'sem conexao');return false}
$('seg').onclick=()=>{cmd('next');navigator.vibrate&&navigator.vibrate(8)};$('ant').onclick=()=>{cmd('prev');navigator.vibrate&&navigator.vibrate(8)};
addEventListener('keydown',e=>{
  if(['AudioVolumeUp','VolumeUp','MediaTrackNext','PageDown','ArrowRight'].includes(e.key)){cmd('next');e.preventDefault()}
  else if(['AudioVolumeDown','VolumeDown','MediaTrackPrevious','PageUp','ArrowLeft'].includes(e.key)){cmd('prev');e.preventDefault()}});
__JSMIDIA__
function conectar(){if(es){try{es.close()}catch(e){}}es=new EventSource('/events?k='+encodeURIComponent(TOKEN));ultimaMsg=Date.now();
  es.onmessage=ev=>{ultimaMsg=Date.now();status(true,'conectado');try{aplicaSnap(JSON.parse(ev.data))}catch(e){}};es.onerror=()=>status(false,'reconectando...')}
setInterval(()=>{if(Date.now()-ultimaMsg>12000)conectar()},3000);conectar();
async function wake(){try{if('wakeLock' in navigator)await navigator.wakeLock.request('screen')}catch(e){}}
document.addEventListener('visibilitychange',()=>{if(document.visibilityState==='visible'){wake();conectar()}});wake();

// ---- mao dominante
let mao='destro';try{mao=localStorage.getItem('baton-mao')||'destro'}catch(e){}
function poeMao(m){mao=m;document.body.classList.toggle('destro',m==='destro');document.body.classList.toggle('canhoto',m==='canhoto');
  $('mdestro').classList.toggle('sel',m==='destro');$('mcanhoto').classList.toggle('sel',m==='canhoto');try{localStorage.setItem('baton-mao',m)}catch(e){}}
$('mdestro').onclick=()=>poeMao('destro');$('mcanhoto').onclick=()=>poeMao('canhoto');poeMao(mao);

// ---- ajustes
let resp=60;try{resp=parseFloat(localStorage.getItem('baton-resp')||'60')}catch(e){}
let raio=.12,tCfg=null,pend={},gan=35;try{gan=parseFloat(localStorage.getItem('baton-gan2')||'35')}catch(e){}
$('bcfg').onclick=()=>$('folha').classList.toggle('aberta');$('bfecha').onclick=()=>$('folha').classList.remove('aberta');
const AJ={meta_min:'s_meta',laser_tam:'s_laser_tam',foco_raio:'raio',foco_blur:'s_foco_blur',foco_escuro:'s_foco_escuro'};
function mostraAj(c){if(c.meta_min)metaMin=c.meta_min;for(const k in AJ){if(c[k]===undefined)continue;const el=$(AJ[k]);el.value=c[k];el.parentNode.querySelector('output').textContent=Math.round(c[k])}if(c.foco_raio)raio=c.foco_raio/100}
fetch('/cfg?k='+encodeURIComponent(TOKEN)).then(r=>r.json()).then(mostraAj).catch(()=>{});
function salvaCfg(k,v){pend[k]=v;clearTimeout(tCfg);tCfg=setTimeout(()=>{const d=pend;pend={};
  fetch('/cfg',{method:'POST',headers:{'Content-Type':'application/json','X-Token':TOKEN},body:JSON.stringify(d)}).catch(()=>{})},150)}
for(const k in AJ)$(AJ[k]).oninput=e=>{const v=parseFloat(e.target.value);e.target.parentNode.querySelector('output').textContent=Math.round(v);if(k==='foco_raio')raio=v/100;if(k==='meta_min'){metaMin=v;tick()}salvaCfg(k,v)};
document.querySelectorAll('#metas button').forEach(b=>b.onclick=()=>{const v=parseFloat(b.dataset.m);$('s_meta').value=v;$('s_meta').parentNode.querySelector('output').textContent=v;metaMin=v;tick();salvaCfg('meta_min',v)});
$('s_resp').value=resp;$('s_resp').parentNode.querySelector('output').textContent=Math.round(resp);
$('s_resp').oninput=e=>{resp=parseFloat(e.target.value);e.target.parentNode.querySelector('output').textContent=Math.round(resp);criaFiltros();try{localStorage.setItem('baton-resp',String(resp))}catch(x){}};
$('s_gan').value=gan;$('s_gan').parentNode.querySelector('output').textContent=Math.round(gan);
$('s_gan').oninput=e=>{gan=parseFloat(e.target.value);e.target.parentNode.querySelector('output').textContent=Math.round(gan);try{localStorage.setItem('baton-gan2',String(gan))}catch(x){}};
function poeRaio(r,salva){raio=Math.max(.04,Math.min(.3,r));const p=Math.round(raio*100);$('raio').value=p;$('rv').textContent=p;if(salva)salvaCfg('foco_raio',p)}

// ---- apontador: gira o celular e o ponto acompanha (a pose ao apertar o botao vira o centro da tela)
const rad=Math.PI/180;
function mul(a,b){const r=[[0,0,0],[0,0,0],[0,0,0]];for(let i=0;i<3;i++)for(let j=0;j<3;j++)for(let k=0;k<3;k++)r[i][j]+=a[i][k]*b[k][j];return r}
const Rz=a=>[[Math.cos(a),-Math.sin(a),0],[Math.sin(a),Math.cos(a),0],[0,0,1]];
const Rx=a=>[[1,0,0],[0,Math.cos(a),-Math.sin(a)],[0,Math.sin(a),Math.cos(a)]];
const Ry=a=>[[Math.cos(a),0,Math.sin(a)],[0,1,0],[-Math.sin(a),0,Math.cos(a)]];
function direcao(o,eixo){const R=mul(mul(Rz(o.a*rad),Rx(o.b*rad)),Ry(o.g*rad));const v=eixo==='z'?[0,0,-1]:[0,1,0];
  return[R[0][0]*v[0]+R[0][1]*v[1]+R[0][2]*v[2],R[1][0]*v[0]+R[1][1]*v[1]+R[1][2]*v[2],R[2][0]*v[0]+R[2][1]*v[1]+R[2][2]*v[2]]}
const angulos=f=>({yaw:Math.atan2(f[0],f[1])/rad,pitch:Math.asin(Math.max(-1,Math.min(1,f[2])))/rad});
const dif=a=>{while(a>180)a-=360;while(a<-180)a+=360;return a};
// filtro One Euro: parado suaviza o tremor da mao; em movimento rapido quase nao atrasa
function umEuro(mc,beta,dc){let xp=null,dxp=0,tp=0;
  const al=(c,dt)=>1/(1+1/(2*Math.PI*c)/dt);
  return{reset(){xp=null},f(x,t){if(xp===null){xp=x;dxp=0;tp=t;return x}
    const dt=Math.min(.1,Math.max(.004,(t-tp)/1000));tp=t;const dx=(x-xp)/dt;dxp+=(dx-dxp)*al(dc,dt);
    const c=mc+beta*Math.abs(dxp);xp+=(x-xp)*al(c,dt);return xp}}}
// "Resposta do apontador": mais alto = menos suavizacao em movimento (ponteiro mais rapido), um pouco mais de tremor parado
let fx=null,fy=null;function criaFiltros(){const r=resp/100,mc=0.8+r*2.4,beta=6+r*50;fx=umEuro(mc,beta,1.2);fy=umEuro(mc,beta,1.2)}criaFiltros();
let ori=null,semSensor=true,base=null,sx=.5,sy=.5,ativo=null,pad={x:.5,y:.5},padId=null,modoUlt='laser';
function aoOrientar(e){if(e.alpha===null||e.alpha===undefined)return;semSensor=false;ori={a:e.alpha,b:e.beta,g:e.gamma};if(ativo)atualiza()}
addEventListener('deviceorientation',aoOrientar,true);
const sensor=()=>!semSensor;
let lsAb=0,lsPend=null,lsSeq=0,ultimo=0;const lsQid=Math.random().toString(36).slice(2,8);
function envLaser(x,y,on){const t=performance.now();if(on&&t-ultimo<2)return;ultimo=t;const p=paraSlide(x,y);ultSlide=p;lsPend={x:p.x,y:p.y,on,m:modoUlt,r:raio};bombeia()}
// ate 3 envios em voo (RTT alto no relay): o servidor descarta os fora de ordem (q)
function bombeia(){
  while(lsPend&&lsAb<3){const d=lsPend;lsPend=null;d.q=++lsSeq;d.i=lsQid;lsAb++;
    fetch('/laser',{method:'POST',headers:{'Content-Type':'application/json','X-Token':TOKEN},body:JSON.stringify(d),keepalive:true}).catch(()=>{}).finally(()=>{lsAb--;bombeia()})}}
// zoom apontado: segurar o botao e apontar leva o centro da ampliacao para onde voce aponta (a pose ao apertar = centro atual)
let zbase=null,zEnv=false,zPend=null,zUlt=0;
function moveZoom(){
  if(ativo!=='zoom')return;const cx=sx,cy=sy;
  zoom={z:zoom.z,x:cx,y:cy};const t=performance.now();if(t-zUlt<40)return;zUlt=t;zPend={z:zoom.z,x:cx,y:cy};if(!zEnv)bombeiaZoom()}
function bombeiaZoom(){if(!zPend){zEnv=false;return}zEnv=true;const d=zPend;zPend=null;
  fetch('/cmd',{method:'POST',headers:{'Content-Type':'application/json','X-Token':TOKEN},body:JSON.stringify({cmd:'zoom',valor:d,cid:cid()}),keepalive:true}).catch(()=>{}).finally(bombeiaZoom)}
function mostraMapa(){const el=$(modoUlt==='zoom'?'zaim':modoUlt).querySelector('.mapa i');el.style.left=(sx*100)+'%';el.style.top=(sy*100)+'%'}
function atualiza(){
  if(!base||!ori)return;const f=direcao(ori,base.eixo),an=angulos(f);
  const x=pad.x+dif(an.yaw-base.yaw)/gan,y=pad.y-(an.pitch-base.pitch)*ASP/gan;
  const t=performance.now(),ov=(v)=>(v-.5)*1.06+.5;  // 6% de folga: encostar o celular no limite leva o ponteiro ate a borda
  sx=Math.max(0,Math.min(1,ov(fx.f(Math.max(0,Math.min(1,x)),t))));sy=Math.max(0,Math.min(1,ov(fy.f(Math.max(0,Math.min(1,y)),t))));
  mostraMapa();if(ativo==='zoom'){moveZoom();return}
  envLaser(sx,sy,true)}
async function permissao(){
  if(typeof DeviceOrientationEvent!=='undefined'&&typeof DeviceOrientationEvent.requestPermission==='function'){
    try{const r=await DeviceOrientationEvent.requestPermission();return r==='granted'}catch(e){return false}}
  return true}
let pediu=false;
const DICAS={laser:'Toque onde quer e aponte',foco:'Toque onde quer e aponte; 2o dedo: raio',zoom:'toque onde quer ampliar'};
const texto=(el,t)=>{const d=el.querySelector('.dica');if(d)d.textContent=t};
const dedos=new Map();let pinca=null;
function distDedos(){const[a,b]=[...dedos.values()];return Math.hypot(a.x-b.x,a.y-b.y)}
function posPad(el,e){const r=el.getBoundingClientRect();pad={x:Math.max(0,Math.min(1,(e.clientX-r.left)/r.width)),y:Math.max(0,Math.min(1,(e.clientY-r.top)/r.height))}}
function liga(id,modo){
  const el=$(id);
  el.addEventListener('pointerdown',async e=>{
    e.preventDefault();try{el.setPointerCapture(e.pointerId)}catch(x){}
    if(ativo)return;
    if(!pediu){pediu=true;const ok=await permissao();await new Promise(r=>setTimeout(r,200));if(!ok&&typeof DeviceOrientationEvent!=='undefined'&&DeviceOrientationEvent.requestPermission){
      texto(el,'Permita o movimento e segure de novo');return}}
    ativo=modo;modoUlt=modo;el.classList.add('on');navigator.vibrate&&navigator.vibrate(12);fx.reset();fy.reset();
    padId=e.pointerId;posPad(el,e);sx=pad.x;sy=pad.y;  // onde o dedo toca no botao = onde cai na tela (extremos do botao = cantos da tela)
    if(modo==='zoom'){  // o centro da ampliacao e a posicao tocada no botao; de 1x, entra em 2x
      zoom={z:zoom.z<=1.001?2:zoom.z,x:sx,y:sy};mostraZoom();cmd('zoom',zoom)}
    if(sensor()&&ori){const eixo=Math.abs(ori.b)>50?'z':'y',an=angulos(direcao(ori,eixo));base={eixo,yaw:an.yaw,pitch:an.pitch};
      texto(el,'Toque + gire');mostraMapa();if(modo!=='zoom')envLaser(sx,sy,true)}
    else{base=null;
      texto(el,HTTPS&&!isSecureContext?'Sem sensores aqui: abra o endereco HTTPS':'Sem sensores: arraste o dedo');mostraMapa();if(modo!=='zoom')envLaser(sx,sy,true)}
  });
  el.addEventListener('pointermove',e=>{if(ativo!==modo||e.pointerId!==padId)return;posPad(el,e);
    if(sensor()&&base){atualiza();return}  // com sensor: o dedo desloca a base e o giro refina
    sx=pad.x;sy=pad.y;mostraMapa();if(modo==='zoom')moveZoom();else envLaser(sx,sy,true)});
  ['pointerup','pointercancel'].forEach(t=>el.addEventListener(t,()=>{if(ativo!==modo)return;ativo=null;base=null;padId=null;pinca=null;el.classList.remove('on');
    texto(el,DICAS[modo]);
    if(modo==='zoom'){zPend=null;cmd('zoom',zoom);mostraZoom()}else{lsPend=null;envLaser(sx,sy,false)}}));
  el.addEventListener('contextmenu',e=>e.preventDefault());
}
liga('laser','laser');liga('foco','foco');liga('zaim','zoom');
// foco: com o botao apertado, um segundo dedo em qualquer lugar abre e fecha o raio (pinca)
document.addEventListener('pointerdown',e=>{dedos.set(e.pointerId,{x:e.clientX,y:e.clientY});
  if(ativo==='foco'&&dedos.size===2)pinca={d0:distDedos(),r0:raio}},true);
document.addEventListener('pointermove',e=>{if(!dedos.has(e.pointerId))return;dedos.set(e.pointerId,{x:e.clientX,y:e.clientY});
  if(pinca&&dedos.size>=2&&pinca.d0>0)poeRaio(pinca.r0*distDedos()/pinca.d0,true)},true);
['pointerup','pointercancel'].forEach(t=>document.addEventListener(t,e=>{dedos.delete(e.pointerId);if(dedos.size<2)pinca=null},true));
// vai para o HTTPS com certificado valido (sensores de movimento so funcionam em pagina segura)
const iosUA=/iphone|ipad|ipod/i.test(navigator.userAgent)||(navigator.platform==='MacIntel'&&navigator.maxTouchPoints>1);
const publicoHttps=HTTPS&&!/^https:\/\/[\d.]+:\d+\//.test(HTTPS);  // URL publica (certificado valido) x porta HTTPS local (autoassinado)
function semHttps(){const e=$('st');e.textContent='sem sensores (HTTPS indisponivel nesta rede)';$('dot').classList.remove('ok')}
if(HTTPS&&!isSecureContext&&AUTO&&!sessionStorage.getItem('baton-https-tentou')){try{sessionStorage.setItem('baton-https-tentou','1')}catch(e){}
  // so vai se a URL responde: na rede do instituto (internet fraca ou filtrada) ficar em HTTP e usar o dedo e melhor que uma pagina quebrada
  const ac=new AbortController(),tt=setTimeout(()=>ac.abort(),2500);
  fetch(new URL(HTTPS).origin+'/icon.png',{mode:'no-cors',cache:'no-store',signal:ac.signal}).then(()=>{clearTimeout(tt);location.replace(HTTPS)}).catch(()=>{clearTimeout(tt);setTimeout(semHttps,1200)})}
if(HTTPS&&!isSecureContext&&!AUTO&&(publicoHttps||!iosUA)){$('st').insertAdjacentHTML('afterend',' <a href="'+HTTPS+'" style="color:#0a84ff">(abrir em HTTPS)</a>')}
// ---- app na tela inicial (PWA)
const standalone=matchMedia('(display-mode: standalone)').matches||navigator.standalone===true;
if('serviceWorker' in navigator&&isSecureContext)navigator.serviceWorker.register('/sw.js').catch(()=>{});
let instEv=null;
addEventListener('beforeinstallprompt',e=>{e.preventDefault();instEv=e;if(!standalone)$('binst').style.display='inline-block'});
const ios=/iphone|ipad|ipod/i.test(navigator.userAgent)||(navigator.platform==='MacIntel'&&navigator.maxTouchPoints>1);
if(ios&&!standalone)$('binst').style.display='inline-block';
$('binst').onclick=async()=>{
  if(instEv){instEv.prompt();try{await instEv.userChoice}catch(e){}instEv=null;$('binst').style.display='none';return}
  const d=$('dicainst');d.innerHTML=ios?'Toque em <b>Compartilhar</b> (quadrado com seta) e depois em <b>Adicionar a Tela de Inicio</b>. Abra pelo icone: fica em tela cheia.':'No menu do navegador (tres pontos) escolha <b>Instalar app</b> ou <b>Adicionar a tela inicial</b>.';
  d.style.display=d.style.display==='block'?'none':'block'};
</script></body></html>
"""

PAGINA_SIMPLES = PAGINA_SIMPLES.replace("__JSMIDIA__", JS_MIDIA)

PAGINA_QR_USADO = """<!doctype html><html lang="pt-br"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Baton</title></head><body style="font-family:-apple-system,system-ui,sans-serif;background:#111;color:#eee;text-align:center;padding:48px 24px">
<h2>QR code ja usado ou expirado</h2><p>Cada QR vale para um celular. No computador, abra o Baton (icone da barra, ou localhost:8765) e escaneie o QR code atual.</p></body></html>"""


def montar_qr(estado, usar_qr_cache):
    qrjs = "/vendor/qr.js" if usar_qr_cache else QR_CDN
    return (PAGINA_QR.replace("__QRJS__", qrjs).replace("__CSS__", CSS_COMUM + CSS_TELA)
            .replace("__REMOTE__", estado.url_pareamento()).replace("__URLS__", json.dumps(estado.urls_pareamento()).replace("</", "<\\/")))


def montar(modo, estado, usar_cache, usar_qr_cache=False, token="", leve=False):
    pdfjs = "/vendor/" if usar_cache else PDFJS_CDN
    qrjs = "/vendor/qr.js" if usar_qr_cache else QR_CDN
    if modo == "tela":
        css, html, js, classe = (CSS_COMUM + CSS_TELA, HTML_TELA,
                                 JS_QR + JS_PASSOS + JS_TELA.replace("__TRANSICAO__", estado.transicao), "tela")
    else:
        css, html, js, classe = CSS_COMUM + CSS_REMOTO, HTML_REMOTO, JS_PASSOS + JS_REMOTO, "remoto"
    titulo = "Baton - " + (Path(estado.pdf).stem if estado.pdf else "controle")
    urls = estado.urls_remoto(token) if modo == "remoto" else estado.urls_pareamento()  # celular: o token DELE; tela: o QR atual
    return (PAGINA_BASE.replace("__TITULO__", titulo).replace("__PDFJS__", pdfjs).replace("__CSS__", css)
            .replace("__HTML__", html).replace("__JS__", js).replace("__CLASSE__", classe)
            .replace("__QRJS__", qrjs).replace("__REMOTE__", (urls[0]["url"] if urls else ""))
            .replace("__URLS__", json.dumps(urls).replace("</", "<\\/"))
            .replace("__TEMPDF__", "true" if estado.pdf else "false")
            .replace("__LEVE__", "true" if leve else "false")
            .replace("__EXTERNO__", "true" if estado.ext else "false")
            .replace("__OVERLAY__", "true" if (estado.ext and estado.ext.overlay) else "false")
            .replace("__TOKEN__", token if modo == "remoto" else ""))


class Servidor(ThreadingHTTPServer):
    tls = False
    daemon_threads = True
    allow_reuse_address = True
    request_queue_size = 64

    def handle_error(self, request, client_address):
        """Celular que fecha a aba, troca de rede ou suspende a tela derruba a conexao: nao e erro, nao polui o log."""
        if isinstance(sys.exc_info()[1], (ConnectionError, TimeoutError, BrokenPipeError)):
            return
        super().handle_error(request, client_address)


def ajustar_socket(sock):
    """Mantem a conexao viva em Wi-Fi/Tailscale: sem atraso de Nagle e com keepalive curto."""
    try:
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
        if hasattr(socket, "TCP_KEEPIDLE"):  # Linux
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPIDLE, 10)
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPINTVL, 5)
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPCNT, 4)
        elif hasattr(socket, "TCP_KEEPALIVE"):  # macOS
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPALIVE, 10)
    except OSError:
        pass


def criar_handler(estado, usar_cache, usar_qr_cache):
    class H(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        timeout = 300

        def setup(self):
            super().setup()
            ajustar_socket(self.request)

        def log_message(self, *a):
            pass

        CABECALHOS_PROXY = ("Cf-Connecting-Ip", "Cf-Ray", "Cf-Visitor", "X-Forwarded-For", "X-Forwarded-Proto", "Forwarded", "X-Real-Ip")

        def _proxiado(self):
            """True se a requisicao veio por um proxy/tunel (Cloudflare Tunnel, nginx...): ele conecta de 127.0.0.1, mas o cliente
            real esta na internet. Um navegador no proprio computador nunca envia esses cabecalhos."""
            return any(self.headers.get(h) for h in self.CABECALHOS_PROXY)

        def _local(self):
            return self.client_address[0] in ("127.0.0.1", "::1") and not self._proxiado()

        def _ip_cliente(self):
            h = self.headers
            return (h.get("Cf-Connecting-Ip") or (h.get("X-Forwarded-For") or "").split(",")[0].strip()
                    or h.get("X-Real-Ip") or self.client_address[0])

        def _dispositivo(self, u=None):
            """Celular dono do token: ?k= na URL ou cabecalho X-Token. None se o token nao e de nenhum celular pareado."""
            k = parse_qs(u.query).get("k", [""])[0] if u is not None else ""
            return (estado.dispositivo_do_token(k or self.headers.get("X-Token", ""))
                    or estado.dispositivo_do_token(self._cookie("baton_k")))  # token velho/ausente na URL: vale o cookie

        def _cookie(self, nome):
            """Valor de um cookie da requisicao ('' se nao ha): o celular pareado reaparece sozinho, sem novo QR nem ?k= na URL."""
            try:
                c = http.cookies.SimpleCookie(self.headers.get("Cookie", ""))
                return c[nome].value if nome in c else ""
            except Exception:
                return ""

        def _set_cookie(self, nome, valor):
            seguro = "; Secure" if (self.headers.get("X-Forwarded-Proto") == "https" or "https" in (self.headers.get("Cf-Visitor") or "")
                                    or self.server.tls) else ""
            return f"{nome}={valor}; Path=/; Max-Age=31536000; SameSite=Lax; HttpOnly{seguro}"

        def _leitura_ok(self, u):
            """GET com dado sensivel (PDF, eventos, manifesto): so o computador ou um celular pareado (?k=)."""
            return self._local() or self._dispositivo(u) is not None

        def _parear(self, codigo):
            """QR de uso unico: valido -> o celular vira um dispositivo (token proprio) e segue para o controle."""
            ip = self._ip_cliente()
            agora = time.time()
            falhas = [t for t in estado.falhas_pareamento.get(ip, []) if agora - t < 60]
            if len(falhas) >= 8:
                return self._enviar(429, "muitas tentativas: aguarde um minuto", "text/plain; charset=utf-8")
            ja = estado.dispositivo_do_token(self._cookie("baton_k"))
            if ja is not None:  # celular ja pareado escaneando de novo: mantem o mesmo aparelho (o app instalado continua valendo)
                return self._ir_para_app(ja["token"])
            d = estado.parear(codigo, self.headers.get("User-Agent", ""))
            if d is None:
                estado.falhas_pareamento[ip] = falhas + [agora]
                if self._proxiado():
                    return self._enviar(404, "not found", "text/plain; charset=utf-8")
                return self._enviar(403, PAGINA_QR_USADO, "text/html; charset=utf-8")
            return self._ir_para_app(d["token"], novo=True)

        def _ir_para_app(self, token, novo=False):
            modo = self._cookie("baton_modo")
            rota = "/simples" if modo == "simples" else "/remote"
            self.send_response(302)
            self.send_header("Location", f"{rota}?k={token}")
            self.send_header("Set-Cookie", self._set_cookie("baton_k", token))
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", "0")
            self.end_headers()

        def _autorizado(self):
            return self._local() or self._dispositivo() is not None

        def _enviar(self, codigo, corpo, tipo="application/json", extra=None):
            if isinstance(corpo, str):
                corpo = corpo.encode("utf-8")
            self.send_response(codigo)
            self.send_header("Content-Type", tipo)
            self.send_header("Content-Length", str(len(corpo)))
            self.send_header("Cache-Control", "no-store")
            for k, v in (extra or {}).items():
                for item in (v if isinstance(v, list) else [v]):
                    self.send_header(k, item)
            self._headers_buffer.append(b"\r\n")
            try:
                self.wfile.write(b"".join(self._headers_buffer) + corpo)  # um unico pacote TCP
                self._headers_buffer = []
            except (BrokenPipeError, ConnectionResetError, OSError):
                self.close_connection = True

        def _pdf(self):
            dados = Path(estado.pdf).read_bytes()
            total = len(dados)
            faixa = faixa_http(self.headers.get("Range"), total)
            if faixa is None:  # em pedacos: o pdf.js cancela o GET inteiro ao ver que o servidor aceita Range, sem gastar a banda
                self.send_response(200)
                self.send_header("Content-Type", "application/pdf")
                self.send_header("Content-Length", str(total))
                self.send_header("Accept-Ranges", "bytes")
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                try:
                    for i in range(0, total, 32768):
                        self.wfile.write(dados[i:i + 32768])
                except (BrokenPipeError, ConnectionResetError, OSError):
                    self.close_connection = True
                return
            if faixa == "invalida":
                return self._enviar(416, b"", "application/pdf", {"Content-Range": f"bytes */{total}"})
            a, b = faixa
            return self._enviar(206, dados[a:b + 1], "application/pdf",
                                {"Accept-Ranges": "bytes", "Content-Range": f"bytes {a}-{b}/{total}"})

        def do_GET(self):
            u = urlparse(self.path)
            # pela internet (tunel/proxy) so entra quem traz o id do QR (/p/<codigo>, uso unico) ou o token de um celular ja
            # pareado; todo o resto some (404), sem revelar que ha um Baton ali
            if self._proxiado() and not (u.path.startswith(("/p/", "/vendor/", "/icon", "/sw.js")) or self._dispositivo(u) is not None):
                return self._enviar(404, "not found", "text/plain; charset=utf-8")
            if u.path == "/" and not self._local():
                d = self._dispositivo(u)
                if d is not None:  # abrir o endereco puro (ou o icone do app) leva direto ao controle do celular ja pareado
                    return self._ir_para_app(d["token"])
            if u.path in ("/", "/qr", "/apresentar") and not self._local():
                return self._enviar(403, "abra o Baton pelo QR code ou pelo endereco do celular", "text/plain; charset=utf-8")
            if u.path == "/":
                if estado.externo or not estado.pdf:
                    return self._enviar(200, montar_qr(estado, usar_qr_cache), "text/html; charset=utf-8")
                return self._enviar(200, montar("tela", estado, usar_cache, usar_qr_cache), "text/html; charset=utf-8")
            if u.path == "/apresentar":
                if not estado.pdf:
                    return self._enviar(200, montar_qr(estado, usar_qr_cache), "text/html; charset=utf-8")
                return self._enviar(200, montar("tela", estado, usar_cache, usar_qr_cache), "text/html; charset=utf-8")
            if u.path == "/qr":
                return self._enviar(200, montar_qr(estado, usar_qr_cache), "text/html; charset=utf-8")
            if u.path in ("/painel", "/painel/estado"):
                if not self._local():
                    return self._enviar(403, "painel so no proprio computador", "text/plain; charset=utf-8")
                if u.path == "/painel":
                    return self._enviar(200, residente.PAINEL_HTML, "text/html; charset=utf-8")
                return self._enviar(200, json.dumps(estado.painel_estado()))
            if u.path == "/sw.js":  # service worker minimo: so existe para o navegador oferecer "instalar o app"
                return self._enviar(200, "self.addEventListener('install',()=>self.skipWaiting());"
                                         "self.addEventListener('activate',e=>e.waitUntil(self.clients.claim()));"
                                         "self.addEventListener('fetch',()=>{});", "application/javascript",
                                    {"Service-Worker-Allowed": "/"})
            if u.path in ("/icon.png", "/icon-192.png", "/icon-512.png"):
                tam = {"/icon-192.png": 192, "/icon-512.png": 512}.get(u.path, 180)
                return self._enviar(200, icone_png(tam), "image/png", {"Cache-Control": "max-age=86400"})
            if u.path == "/manifest.json":
                if not self._leitura_ok(u):
                    return self._enviar(403, "{}")
                q = parse_qs(u.query)
                k, modo = q.get("k", [""])[0], q.get("m", [""])[0]
                rota = "/simples" if modo == "simples" else "/remote"
                m = {"name": "Baton simples" if modo == "simples" else "Baton", "short_name": "Baton", "display": "standalone",
                     "orientation": "any", "background_color": "#000000", "theme_color": "#000000", "scope": "/",
                     "id": rota, "start_url": f"{rota}?k={k}",
                     "icons": [{"src": "/icon-192.png", "sizes": "192x192", "type": "image/png", "purpose": "any"},
                               {"src": "/icon-512.png", "sizes": "512x512", "type": "image/png", "purpose": "any"},
                               {"src": "/icon.png", "sizes": "180x180", "type": "image/png"}]}
                return self._enviar(200, json.dumps(m), "application/manifest+json")
            if u.path == "/vendor/qr.js" and usar_qr_cache:
                return self._enviar(200, arquivo_qr().read_bytes(), "application/javascript")
            if u.path.startswith("/p/"):
                return self._parear(u.path[3:])
            if u.path == "/remote":
                k = (self._dispositivo(u) or {}).get("token") or parse_qs(u.query).get("k", [""])[0]  # token valido (URL ou cookie)
                if not (self._local() or self._dispositivo(u) is not None):
                    return self._enviar(403, "celular nao pareado: escaneie o QR code no computador", "text/plain; charset=utf-8")
                if not self._local():
                    estado.remotos += 1
                    estado.publicar(estado.snapshot())
                leve = rede_lenta_do_ip(self.connection.getsockname()[0]) if not self._local() else False
                return self._enviar(200, montar("remoto", estado, usar_cache, usar_qr_cache, k, leve), "text/html; charset=utf-8",
                                    {"Set-Cookie": [self._set_cookie("baton_modo", "remote")] + ([self._set_cookie("baton_k", k)] if self._dispositivo(u) else [])})
            if u.path == "/pdf" and estado.pdf:
                if not self._leitura_ok(u):
                    return self._enviar(403, "token invalido", "text/plain; charset=utf-8")
                return self._pdf()
            if u.path.startswith("/vendor/") and usar_cache:
                nome = u.path.split("/")[-1]
                f = pasta_pdfjs() / nome
                if nome in PDFJS_FILES and f.exists():
                    return self._enviar(200, f.read_bytes(), "application/javascript")
            if u.path == "/events":
                if not self._leitura_ok(u):
                    return self._enviar(403, "token invalido", "text/plain; charset=utf-8")
                d = self._dispositivo(u)
                return self._sse(d["id"] if d else None)
            if u.path == "/tela.jpg":
                if not self._leitura_ok(u):
                    return self._enviar(403, "token invalido", "text/plain; charset=utf-8")
                if not estado.captura:
                    return self._enviar(404, "captura desligada (--sem-captura)", "text/plain; charset=utf-8")
                jpg = tela_em_cache()
                if not jpg:
                    return self._enviar(404, "sem como capturar a tela neste sistema", "text/plain; charset=utf-8")
                return self._enviar(200, jpg, "image/jpeg")
            if u.path == "/simples":
                if not (self._local() or self._dispositivo(u) is not None):
                    return self._enviar(403, "celular nao pareado: escaneie o QR code no computador", "text/plain; charset=utf-8")
                k = (self._dispositivo(u) or {}).get("token") or parse_qs(u.query).get("k", [""])[0]  # token valido (URL ou cookie)
                host = (self.headers.get("Host") or "").rsplit(":", 1)[0] or "localhost"
                if URL_PUBLICA and not self._proxiado():  # certificado valido (sem aviso): preferido para liberar os sensores
                    # a pagina testa se o celular alcanca a URL antes de ir (internet fraca/filtrada: fica em HTTP, sem sensores)
                    https, auto = f"{URL_PUBLICA.rstrip('/')}/simples?k={k}", ("1" if publica_viva() else "0")
                else:
                    https = (f"https://{host}:{estado.tls_porta}/simples?k={k}" if estado.tls_porta and not self.server.tls else "")
                    auto = "0"
                return self._enviar(200, PAGINA_SIMPLES.replace("__TOKEN__", k).replace("__HTTPS__", https).replace("__AUTO__", auto).replace("__EXTERNO__", "1" if estado.ext else "0"),
                                    "text/html; charset=utf-8",
                                    {"Set-Cookie": [self._set_cookie("baton_modo", "simples")] + ([self._set_cookie("baton_k", k)] if self._dispositivo(u) else [])})
            if u.path == "/cfg":
                if not self._leitura_ok(u):
                    return self._enviar(403, "{}")
                return self._enviar(200, json.dumps(estado.cfg))
            if u.path == "/notas":
                if not self._leitura_ok(u):
                    return self._enviar(403, "{}")
                return self._enviar(200, json.dumps({"notas": estado.notas}))
            if u.path == "/estado":
                return self._enviar(200, json.dumps(estado.snapshot()))
            self._enviar(404, "nao encontrado", "text/plain; charset=utf-8")

        def _sse(self, dispositivo=None):
            q = queue.Queue(maxsize=300)
            q.dispositivo = dispositivo
            estado.clientes.append(q)
            try:
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Connection", "keep-alive")
                self.send_header("X-Accel-Buffering", "no")
                self.end_headers()
                self.wfile.write(("retry: 1000\n\ndata: " + json.dumps(estado.snapshot()) + "\n\n").encode())
                self.wfile.write(("data: " + json.dumps({"cfg": estado.cfg}) + "\n\n").encode())
                if estado.tracos:
                    self.wfile.write(("data: " + json.dumps({"tracos": estado.tracos}) + "\n\n").encode())
                self.wfile.flush()
                while True:
                    try:
                        msg = q.get(timeout=4)
                        if msg is None:  # celular desconectado pelo painel
                            break
                        self.wfile.write(("data: " + json.dumps(msg) + "\n\n").encode())
                    except queue.Empty:
                        self.wfile.write(("data: " + json.dumps({"hb": 1, "timer": estado.snapshot()["timer"]}) + "\n\n").encode())
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass
            finally:
                if q in estado.clientes:
                    estado.clientes.remove(q)
            self.close_connection = True

        def _importar(self):
            if not self._local():
                return self._enviar(403, '{"erro":"local"}')
            n = int(self.headers.get("Content-Length", "0"))
            if n > 60 * 1024 * 1024:
                return self._enviar(413, '{"erro":"arquivo grande"}')
            dados = self.rfile.read(n)
            nome = parse_qs(urlparse(self.path).query).get("nome", ["x"])[0]
            ext = Path(nome).suffix.lower()[:8]
            with tempfile.TemporaryDirectory() as d:
                arq = Path(d) / ("notas" + ext)
                arq.write_bytes(dados)
                try:
                    notas = notas_de_arquivo(arq)
                except Exception as e:
                    return self._enviar(400, json.dumps({"erro": str(e)[:120]}))
            if not notas:
                return self._enviar(422, '{"erro":"nenhuma nota encontrada no arquivo"}')
            return self._enviar(200, json.dumps({"importadas": len(estado.importar_notas(notas))}))

        def _deck(self):
            n = int(self.headers.get("Content-Length", "0"))
            if n > 100 * 1024 * 1024:
                return self._enviar(413, '{"erro":"arquivo maior que 100 MB"}')
            dados = self.rfile.read(n)
            nome = parse_qs(urlparse(self.path).query).get("nome", ["apresentacao.pdf"])[0]
            try:
                pdf, notas = preparar_deck(dados, nome)
                if pdf:
                    estado.trocar_deck(pdf, notas)
                    estado.apresentar()
                else:
                    estado.importar_notas(notas)
            except (RuntimeError, ValueError, OSError, subprocess.TimeoutExpired) as e:
                return self._enviar(400, json.dumps({"erro": str(e)[:200]}, ensure_ascii=False))
            return self._enviar(200, json.dumps(estado.painel_estado()))

        def do_POST(self):
            if not self._autorizado():
                return self._enviar(403, '{"erro":"token"}')
            if urlparse(self.path).path == "/painel/deck":
                try:
                    return self._deck()
                except (ValueError, OSError):
                    return self._enviar(400, '{"erro":"leitura"}')
            if urlparse(self.path).path == "/painel/importar":
                try:
                    return self._importar()
                except (ValueError, OSError):
                    return self._enviar(400, '{"erro":"leitura"}')
            try:
                n = int(self.headers.get("Content-Length", "0"))
                dados = json.loads(self.rfile.read(n) or b"{}")
            except Exception:
                return self._enviar(400, '{"erro":"json"}')
            if self.path == "/consent":
                ok = estado.consentir(dados.get("acao"))
                return self._enviar(200 if ok else 400, json.dumps(estado.snapshot()))
            if self.path == "/painel/acao":
                if not self._local():
                    return self._enviar(403, '{"erro":"local"}')
                ok = estado.painel(dados.get("acao"), dados.get("valor"))
                corpo = json.dumps(estado.painel_estado())
                self._enviar(200 if ok or dados.get("acao") == "sair" else 400, corpo)
                if dados.get("acao") == "sair":
                    threading.Timer(0.3, lambda: os._exit(0)).start()
                return
            if (self.path == "/cmd" and dados.get("cmd") in ("next", "prev", "first", "last", "goto", "timer", "zoom")
                    and not estado.permitido()):
                estado.autorizar_por_toque()
            if self.path == "/laser" and dados.get("on") and not estado.permitido():
                estado.autorizar_por_toque()
            if self.path in ("/cmd", "/laser", "/draw") and not estado.permitido():
                return self._enviar(423, json.dumps(estado.snapshot()))
            if self.path == "/cmd":
                ok = estado.comando(dados.get("cmd"), dados.get("valor"), dados.get("cid"))
                return self._enviar(200 if ok else 400, json.dumps(estado.snapshot()))
            if self.path == "/laser":
                chave = (self.headers.get("X-Token") or self.client_address[0], str(dados.get("i", "")))
                q = dados.get("q")
                if isinstance(q, int):
                    ult = estado.laser_seq.get(chave, 0)
                    if q <= ult:
                        return self._enviar(200, "{}")  # chegou depois de um ponto mais novo: descarta
                    estado.laser_seq[chave] = q
                    if len(estado.laser_seq) > 64:
                        estado.laser_seq.clear()
                x, y = float(dados.get("x", 0)), float(dados.get("y", 0))
                estado.laser(x, y, bool(dados.get("on")), "foco" if dados.get("m") == "foco" else "laser",
                             dados.get("r"))
                return self._enviar(200, "{}")
            if self.path == "/notas":
                if "tudo" in dados:
                    notas = estado.salvar_todas(dados["tudo"])
                else:
                    notas = estado.salvar_nota(dados.get("p"), dados.get("t", ""))
                if notas is None:
                    return self._enviar(400, '{"erro":"chave"}')
                return self._enviar(200, json.dumps({"notas": notas}))
            if self.path == "/cfg":
                return self._enviar(200, json.dumps(estado.set_cfg(dados)))
            if self.path == "/draw":
                estado.desenhar(dados)
                return self._enviar(200, "{}")
            self._enviar(404, "{}")

    return H


DECKS = (".pptx", ".ppt", ".odp", ".key")


def notas_de_arquivo(caminho):
    """Notas de um .pptx (notas do apresentador) ou de um .md (## 1, ## p12). Outros formatos: ValueError."""
    c = str(caminho).lower()
    if c.endswith(".pptx"):
        return notas_pptx(caminho)
    if c.endswith((".md", ".txt")):
        return ler_notas(caminho)
    raise ValueError("formato sem notas legiveis: use .pptx ou .md")


def notas_pptx(caminho):
    """Notas do apresentador de um .pptx (ppt/notesSlides) na ordem dos slides: {'p1': texto, ...}."""
    import posixpath
    import zipfile
    import xml.etree.ElementTree as ET
    ns = {"p": "http://schemas.openxmlformats.org/presentationml/2006/main",
          "a": "http://schemas.openxmlformats.org/drawingml/2006/main",
          "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
          "rel": "http://schemas.openxmlformats.org/package/2006/relationships"}
    notas = {}
    with zipfile.ZipFile(caminho) as z:
        def rels(parte):
            pasta, nome = posixpath.split(parte)
            arq = posixpath.join(pasta, "_rels", nome + ".rels")
            if arq not in z.namelist():
                return {}
            return {r.get("Id"): (r.get("Type", ""), posixpath.normpath(posixpath.join(pasta, r.get("Target", ""))))
                    for r in ET.fromstring(z.read(arq)).findall("rel:Relationship", ns)}
        pres = ET.fromstring(z.read("ppt/presentation.xml"))
        rp = rels("ppt/presentation.xml")
        for n, sld in enumerate(pres.findall(".//p:sldId", ns), 1):
            alvo = rp.get(sld.get("{%s}id" % ns["r"]))
            if not alvo:
                continue
            nota = next((t for tipo, t in rels(alvo[1]).values() if tipo.endswith("/notesSlide")), None)
            if not nota or nota not in z.namelist():
                continue
            raiz = ET.fromstring(z.read(nota))
            paragrafos = []
            for sp in raiz.findall(".//p:sp", ns):
                ph = sp.find(".//p:nvPr/p:ph", ns)
                if ph is not None and ph.get("type") in ("sldImg", "sldNum", "hdr", "ftr", "dt"):
                    continue
                for par in sp.findall(".//a:p", ns):
                    t = "".join(x.text or "" for x in par.findall(".//a:t", ns)).strip()
                    if t:
                        paragrafos.append(t)
            if paragrafos:
                notas[f"p{n}"] = "\n\n".join(paragrafos)
    return notas


def deck_para_pdf(caminho):
    """Converte .pptx/.ppt/.odp em PDF com o LibreOffice (cache em ~/.cache/baton/decks); devolve o caminho do PDF."""
    exe = shutil.which("soffice") or shutil.which("libreoffice") or next(
        (p for p in ("/Applications/LibreOffice.app/Contents/MacOS/soffice",
                     r"C:\Program Files\LibreOffice\program\soffice.exe") if os.path.exists(p)), None)
    if not exe:
        raise RuntimeError("para abrir .pptx instale o LibreOffice (ou exporte o PDF no PowerPoint/Keynote e use o PDF)")
    src = Path(caminho)
    h = hashlib.sha1(src.read_bytes()).hexdigest()[:10]
    pasta = CACHE.parent / "decks" / h
    pdf = pasta / (src.stem + ".pdf")
    if not pdf.exists():
        pasta.mkdir(parents=True, exist_ok=True)
        print("Convertendo a apresentacao em PDF (so na primeira vez)...", flush=True)
        r = subprocess.run([exe, "--headless", "--convert-to", "pdf", "--outdir", str(pasta), str(src)],
                           capture_output=True, text=True, timeout=300)
        if not pdf.exists():
            raise RuntimeError("falha ao converter a apresentacao: " + (r.stderr or r.stdout)[-300:])
    return str(pdf)


def preparar_deck(dados, nome):
    """Bytes de um arquivo enviado pela interface -> (caminho do PDF, notas). Aceita PDF, pptx/ppt/odp e md/txt (so notas).
    Levanta RuntimeError com a mensagem para mostrar ao usuario."""
    ext = Path(nome).suffix.lower()
    seguro = re.sub(r"[^\w.\- ]", "_", Path(nome).name)[:80] or "apresentacao"
    pasta = CACHE.parent / "decks" / hashlib.sha1(dados).hexdigest()[:10]
    if ext not in (".pdf", ".md", ".txt") + DECKS:
        raise RuntimeError("formato nao suportado: use PDF, PPTX, PPT, ODP ou MD (notas)")
    pasta.mkdir(parents=True, exist_ok=True)
    arq = pasta / seguro
    arq.write_bytes(dados)
    if ext in (".md", ".txt"):
        return None, ler_notas(arq)
    notas = {}
    if ext == ".pptx":
        try:
            notas = notas_pptx(arq)
        except Exception:
            pass
    pdf = str(arq) if ext == ".pdf" else deck_para_pdf(arq)
    return pdf, notas


def contar_paginas(pdf):
    for exe in ("pdfinfo", "mdls"):
        if not shutil.which(exe):
            continue
        try:
            if exe == "pdfinfo":
                out = subprocess.run(["pdfinfo", pdf], capture_output=True, text=True).stdout
                for linha in out.splitlines():
                    if linha.startswith("Pages:"):
                        return int(linha.split()[1])
            else:
                out = subprocess.run(["mdls", "-name", "kMDItemNumberOfPages", "-raw", pdf], capture_output=True, text=True).stdout
                if out.strip().isdigit():
                    return int(out.strip())
        except Exception:
            pass
    dados = Path(pdf).read_bytes()
    import re
    achados = re.findall(rb"/Type\s*/Pages\b[^>]*?/Count\s+(\d+)", dados)
    return max(map(int, achados)) if achados else 1


def main():
    global MAX_DISPOSITIVOS, MONITOR_FN, RELAY_AGENTE
    ap = argparse.ArgumentParser(description="Baton de slides pelo celular (Tailscale), com laser e QR code.")
    ap.add_argument("pdf", nargs="?", help="PDF dos slides (sem PDF, o modo e 'externo': o celular controla o app em primeiro plano)")
    ap.add_argument("--instalar", action="store_true", help="macOS: liga o Baton sozinho no login (modo externo)")
    ap.add_argument("--desinstalar", action="store_true", help="macOS: remove do login")
    ap.add_argument("--externo", action="store_true",
                    help="controla qualquer leitor (Adobe, Preview, Keynote...) com teclas e mouse; exige Acessibilidade")
    ap.add_argument("--porta", type=int, default=8765)
    ap.add_argument("--transicao", choices=TRANSICOES, default="slide")
    ap.add_argument("--host", help="IP extra para escutar (padrao: IP do Tailscale e 127.0.0.1)")
    ap.add_argument("--proporcao", help="proporcao do slide no modo externo, ex.: 16:9 (padrao: le do PDF)")
    ap.add_argument("--url-publica", default=os.environ.get("BATON_URL_PUBLICA", ""), metavar="URL",
                    help="endereco HTTPS publico (ex.: https://baton.exemplo.com, via Cloudflare Tunnel apontando para esta porta); entra nos QR codes. "
                         "Com --instalar vira o padrao do Baton iniciado no login; '-' remove o padrao")
    ap.add_argument("--relay", default=os.environ.get("BATON_RELAY", ""), metavar="URL",
                    help="conecta este Baton a um relay central (ex.: wss://baton.exemplo.com): varios computadores atras da mesma URL publica, "
                         "cada um com sua sessao. Com --instalar vira o padrao; '-' remove")
    ap.add_argument("--sem-relay-publico", action="store_true", help="nao usa o relay publico padrao (so rede local e a URL publica que voce configurar)")
    ap.add_argument("--chave-relay", default="", metavar="CHAVE", help="chave de registro do relay (ou BATON_RELAY_CHAVE)")
    ap.add_argument("--convite", default="", metavar="HOST/CODIGO",
                    help="entra no relay com um convite de uso unico (ex.: baton.exemplo.com/ABCD-EFGH-JKLM); sem login, nada e coletado")
    ap.add_argument("--criar-convite", nargs="?", const="", default=None, metavar="ROTULO",
                    help="no servidor do relay: cria um convite de uso unico (vale 24 h) e mostra os comandos para a pessoa")
    ap.add_argument("--servir-relay", action="store_true", help="roda o relay central (aponte o Cloudflare Tunnel para --relay-porta); exige a chave do relay")
    ap.add_argument("--relay-porta", type=int, default=8780, help="porta do relay com --servir-relay")
    ap.add_argument("--relay-max", type=int, default=50, help="maximo de computadores (sessoes) no relay")
    ap.add_argument("--max-celulares", type=int, default=MAX_DISPOSITIVOS, help="celulares pareados por sessao")
    ap.add_argument("--sem-https", action="store_true", help="nao abre a porta HTTPS (porta+1), usada pelo laser por apontamento com sensores")
    ap.add_argument("--sem-captura", action="store_true", help="nao envia ao celular a previa (captura) da tela do computador quando nao ha PDF")
    ap.add_argument("--sem-abrir", action="store_true", help="nao abre o navegador no Mac")
    ap.add_argument("--tela", type=int, default=0, help="(apelido antigo de --monitor, base 0) modo externo: indice da tela da camada")
    ap.add_argument("--monitor", default="", metavar="N|auto",
                    help="monitor da projecao: numero (1 = primeiro da lista do painel) ou auto (o da janela em tela cheia). Padrao: a escolha do painel")
    ap.add_argument("--nivel", type=int, help="modo externo: nivel da janela da camada (padrao: o maximo do sistema)")
    ap.add_argument("--sem-overlay", action="store_true", help="modo externo: nao usa a camada de desenho (laser = cursor)")
    ap.add_argument("--cursor", action="store_true", help="modo externo: move tambem o cursor do mouse com o laser")
    ap.add_argument("--notas", help="arquivo Markdown com as notas do apresentador (padrao: <pdf>.notas.md)")
    ap.add_argument("--importar-notas", metavar="ARQUIVO",
                    help="importa as notas de um .pptx (notas do apresentador) ou .md para a sessao do Baton e sai")
    ap.add_argument("--tray", action="store_true", help="mostra o icone na bandeja (ja e o padrao no modo --residente)")
    ap.add_argument("--sem-tray", action="store_true", help="nao mostra o icone na bandeja")
    ap.add_argument("--recompilar-app", action="store_true", help="macOS: recompila o Baton.app e mostra os erros do compilador (a permissao de Acessibilidade precisa ser dada de novo)")
    ap.add_argument("--preparar-offline", action="store_true", help="baixa pdf.js e o gerador de QR para o Baton funcionar sem internet")
    ap.add_argument("--vendor", action="store_true", help="com --preparar-offline: grava em <pasta do baton>/vendor (pendrive)")
    ap.add_argument("--diagnostico", action="store_true", help="imprime um resumo do ambiente (Baton.app, permissao, log) para suporte")
    ap.add_argument("--resetar", action="store_true", help="com --permissao: zera antes o registro de Acessibilidade do Baton.app")
    ap.add_argument("--permissao", action="store_true", help="macOS: pede a permissao de Acessibilidade para o Baton.app e abre os Ajustes")
    ap.add_argument("--rede", action="store_true", help="(sem efeito: a rede local ja e o endereco principal)")
    ap.add_argument("--residente", action="store_true",
                    help="segundo plano: fica em espera e pergunta no celular quando uma apresentacao abrir em tela cheia")
    ap.add_argument("--pausar", action="store_true", help="pausa o Baton em execucao (nao controla nada ate --retomar)")
    ap.add_argument("--retomar", action="store_true", help="retoma o Baton pausado")
    ap.add_argument("--detectar", action="store_true", help="mostra qual app esta em tela cheia agora e sai (teste)")
    ap.add_argument("--novo-token", action="store_true", help="desfaz o pareamento de todos os celulares (cada um precisa escanear um QR novo)")
    a = ap.parse_args()
    if a.instalar and a.relay:
        salvar_relay(a.relay, chave_do_relay(a.chave_relay))
        print(f"Relay padrao: {a.relay}" if a.relay != "-" else "Relay removido")
    if a.convite:
        try:
            url, chave = relay_mod.resgatar_convite(a.convite, a.relay if a.relay not in ("", "-") else "")
        except (ValueError, OSError) as e:
            sys.exit(f"convite recusado: {e}")
        salvar_relay(url, chave)
        return print(f"Pronto: este computador entra no relay {url}. Reinicie o Baton (feche e abra de novo) para valer; nada sobre voce foi enviado.")
    if a.criar_convite is not None:
        chave = chave_do_relay(a.chave_relay)
        try:
            r = relay_mod.criar_convite_local(a.relay_porta, chave, a.criar_convite)
        except Exception as e:
            sys.exit(f"nao consegui falar com o relay local na porta {a.relay_porta} ({e}); ele esta rodando (baton --servir-relay)?")
        h = (r.get("publico") or url_publica_salva() or "baton.exemplo.com").replace("https://", "").replace("http://", "")
        c = f"{h}/{r['codigo']}"
        return print(f"Convite: {r['codigo']} (24 h, uso unico)\n\nbaton --convite {c}\n"
                     f"git clone https://github.com/pedroiff0/baton && cd baton && BATON_CONVITE={c} ./install.sh\n"
                     f"Windows: $env:BATON_CONVITE=\"{c}\"; .\\install.ps1")
    if a.servir_relay:
        chave = chave_do_relay(a.chave_relay)
        if len(chave) < 12:
            sys.exit("defina a chave do relay (>= 12 caracteres): --chave-relay, BATON_RELAY_CHAVE ou o arquivo ~/.config/baton/relay-chave")
        srv = relay_mod.servir(a.relay_porta, chave, a.relay_max, registro=relay_mod.Registro(Path.home() / ".config" / "baton" / "relay-estado.json"),
                               publico=url_publica_salva() or "")
        print(f"Relay do Baton em 0.0.0.0:{a.relay_porta} (ate {a.relay_max} sessoes). Aponte o tunel/proxy para esta porta; "
              f"painel: /relay/painel (chave de administrador); convites: baton --criar-convite.")
        try:
            srv.serve_forever()
        except KeyboardInterrupt:
            pass
        return
    if a.instalar and a.url_publica:
        salvar_url_publica(a.url_publica)
        print(f"URL publica padrao: {a.url_publica}" if a.url_publica != "-" else "URL publica removida")
    if a.instalar or a.desinstalar:
        return instalar_login(remover=a.desinstalar)
    if a.recompilar_app:
        exe = montar_app_mac(forcar=True)
        return print(f"pronto: {exe}\nAtive o Baton em Acessibilidade e rode: launchctl kickstart -k gui/$(id -u)/com.baton" if exe else "falhou (veja os avisos acima)")
    if a.diagnostico:
        return print(diagnostico())
    if a.preparar_offline:
        if a.vendor:
            VENDOR.mkdir(exist_ok=True)
        ok = baixar_pdfjs() and baixar_qr()
        print(f"pronto para uso sem internet (arquivos em {pasta_pdfjs().parent})" if ok
              else "falhou: conecte-se a internet uma vez e rode de novo", file=sys.stdout if ok else sys.stderr)
        return 0 if ok else 1
    if a.permissao:
        return print(pedir_acessibilidade(resetar=a.resetar))
    if a.pausar or a.retomar:
        p = residente.prefs_carregar()
        p["ativo"] = a.retomar
        residente.prefs_salvar(p)
        return print("Baton retomado" if a.retomar else "Baton pausado")
    if a.importar_notas:
        try:
            notas = notas_de_arquivo(a.importar_notas)
        except (OSError, ValueError, KeyError) as e:
            sys.exit(f"nao consegui ler as notas: {e}")
        if not notas:
            sys.exit("nenhuma nota encontrada no arquivo")
        corpo = Path(a.importar_notas).read_bytes()
        try:  # Baton rodando: importa nele (vale na hora); senao grava na sessao em disco
            rq = urllib.request.Request(f"http://127.0.0.1:{a.porta}/painel/importar?nome={Path(a.importar_notas).name}", data=corpo, method="POST")
            urllib.request.urlopen(rq, timeout=10).read()
            return print(f"{len(notas)} notas importadas no Baton em execucao")
        except OSError:
            e = Estado(None, 9999, "slide", "")
            e.importar_notas(notas)
            return print(f"{len(notas)} notas salvas na sessao; valem na proxima vez que o Baton abrir")
    if a.detectar:
        print(residente.detectar() or "nenhum app em tela cheia")
        return
    if a.residente:
        a.externo, a.sem_abrir = True, True
    if not a.pdf:
        a.externo = True  # sem PDF: plug and play, como um passador de slides USB
    pdf, notas_deck = None, {}
    if a.pdf:
        pdf = os.path.abspath(a.pdf)
        if not os.path.isfile(pdf):
            sys.exit(f"arquivo nao encontrado: {pdf}")
        if pdf.lower().endswith(DECKS):
            if pdf.lower().endswith(".pptx"):
                try:
                    notas_deck = notas_pptx(pdf)
                except Exception as e:
                    print(f"aviso: nao consegui ler as notas do pptx ({e})", file=sys.stderr)
            try:
                pdf = deck_para_pdf(pdf)
            except RuntimeError as e:
                sys.exit(str(e))
    token = secrets.token_urlsafe(9)  # so por compatibilidade: os celulares usam o token do proprio dispositivo
    prop = 16 / 9
    if a.proporcao:
        try:
            w, h = a.proporcao.split(":")
            prop = float(w) / float(h)
        except ValueError:
            sys.exit("use --proporcao largura:altura, por exemplo 16:9")
    elif pdf:
        prop = proporcao_pdf(pdf)
    mon_cli = a.monitor if (a.monitor == "auto" or a.monitor.isdigit()) else (str(a.tela + 1) if a.tela else "")
    mon0 = None
    if a.externo:
        try:
            mon0 = monitores.escolher(mon_cli or residente.prefs_carregar().get("monitor", "auto"))
        except Exception:
            mon0 = None
    externo = criar_externo(not a.sem_overlay, a.tela, a.cursor, a.nivel, mon0) if a.externo else None
    total = contar_paginas(pdf) if pdf else 9999
    estado = Estado(pdf, total, a.transicao, token, externo, "", prop)
    estado.monitor_cli = "" if mon_cli == "auto" else mon_cli
    if externo:
        externo.monitor_fn = estado.monitor
    MONITOR_FN = estado.monitor
    estado._monitor_atualizar()  # primeira leitura aqui, fora do caminho do laser
    estado.monitor_vigiar()
    global URL_PUBLICA
    MAX_DISPOSITIVOS = max(1, min(64, a.max_celulares))
    pedido = a.url_publica.strip()
    URL_PUBLICA = "" if pedido == "-" else (pedido or url_publica_salva())
    resgatar_convite_do_pacote()
    relay_url, relay_chave = (a.relay.strip(), chave_do_relay(a.chave_relay)) if a.relay.strip() else relay_salvo()
    if relay_url == "-":
        relay_url = ""
    elif not relay_url and not os.environ.get("BATON_SEM_RELAY") and not a.sem_relay_publico and residente.prefs_carregar().get("relay_publico", True):
        relay_url = RELAY_PADRAO  # padrao: clonar e usar; se a internet falhar, o QR segue pela rede local
    if relay_url and not URL_PUBLICA and "127.0.0.1" not in relay_url and "localhost" not in relay_url:
        URL_PUBLICA = relay_url.replace("wss://", "https://").replace("ws://", "http://").rstrip("/")  # QR e HTTPS pela URL do relay
    estado.captura = not a.sem_captura
    estado.bases_fn = lambda: calcular_bases(a.porta, a.host)
    estado.carregar_dispositivos()
    if a.novo_token:
        estado.revogar_todos()  # --novo-token: desfaz o pareamento de todos os celulares
    caminho_notas = a.notas or (str(Path(pdf).with_suffix(".notas.md")) if pdf else None)
    base = {}
    if notas_deck and not a.notas:
        base = notas_deck
        print(f"Notas do pptx: {len(notas_deck)} slides")
    elif caminho_notas and os.path.isfile(caminho_notas):
        base = ler_notas(caminho_notas)
        print(f"Notas: {caminho_notas} ({len(base)} entradas)")
    elif a.notas:
        sys.exit(f"arquivo de notas nao encontrado: {a.notas}")
    if pdf:
        estado.notas_id = hashlib.sha1(pdf.encode()).hexdigest()[:10]
    estado.carregar_notas_sessao(base)
    estado.sincronizar_overlay()
    estado.som.preparar()
    if a.residente:
        estado.residente, estado.autorizado = True, False
        residente.Vigia(estado).start()
        if os.environ.get("BATON_LAUNCHER"):
            def _orfao():  # o Baton.app morreu (ex.: launchctl kickstart -k): nao deixa este processo segurando a porta
                while True:
                    time.sleep(2)
                    if os.getppid() == 1:
                        os._exit(0)
            threading.Thread(target=_orfao, daemon=True).start()
    usar_cache = baixar_pdfjs()
    usar_qr = baixar_qr()
    hosts = ["0.0.0.0"]  # todas as interfaces: Wi-Fi e Tailscale; tudo que importa exige o token
    print("Escutando em todas as interfaces (Wi-Fi e Tailscale). O token vai sem criptografia (HTTP) na rede local: "
          "use so em Wi-Fi de confianca.", file=sys.stderr)
    handler = criar_handler(estado, usar_cache, usar_qr)
    servidores = []
    for h in hosts:
        try:
            for tentativa in range(8):  # a copia anterior pode estar saindo (reinicio pelo launchd): espera ate ~7 s
                try:
                    s = Servidor((h, a.porta), handler)
                    break
                except OSError as e:
                    if e.errno != 48 and e.errno != 98 or tentativa == 7:  # EADDRINUSE no macOS e no Linux
                        raise
                    time.sleep(1)
        except OSError as e:
            print(f"aviso: nao consegui escutar em {h}:{a.porta} ({e})", file=sys.stderr)
            if h in ("127.0.0.1", "0.0.0.0"):
                try:  # ja ha um Baton nessa porta: sai sem erro (o launchd nao fica reiniciando)
                    urllib.request.urlopen(f"http://127.0.0.1:{a.porta}/painel/estado", timeout=2).read()
                    print(f"Baton ja esta rodando na porta {a.porta}; esta copia encerra.", file=sys.stderr)
                    sys.exit(0)
                except OSError:
                    pass
                sys.exit(f"a porta {a.porta} esta ocupada por outro programa (use --porta)")
            continue
        threading.Thread(target=s.serve_forever, daemon=True).start()
        servidores.append(s)
    if not a.sem_https:
        try:
            ctx = contexto_tls()
            if ctx:
                st = Servidor((hosts[0], a.porta + 1), handler)
                st.socket = ctx.wrap_socket(st.socket, server_side=True, do_handshake_on_connect=False)
                st.tls = True
                threading.Thread(target=st.serve_forever, daemon=True).start()
                servidores.append(st)
                estado.tls_porta = a.porta + 1
        except OSError as e:
            print(f"aviso: sem HTTPS na porta {a.porta + 1} ({e}); o laser por apontamento volta ao dedo", file=sys.stderr)
    local = f"http://localhost:{a.porta}/"
    estado.local_url = local
    estado.porta_local = a.porta
    if relay_url:
        estado.relay_iniciar(relay_url, relay_chave)
        print(f"Relay: {relay_url} (sessao propria; QR e celulares isolados dos outros computadores)")
    tray = residente.iniciar_tray(a.porta) if (a.residente or a.tray) and not a.sem_tray else None
    print(f"\nModo: {'externo (teclas e mouse para o app em primeiro plano)' if externo else 'tela do Baton'}")
    if pdf:
        print(f"PDF: {pdf} ({estado.total} paginas)")
    print(f"QR code no Mac:   {local}")
    print(f"Painel (pausar, modo, apps): {local}painel")
    for u in estado.urls_pareamento():
        print(f"Celular ({u['nome']}): {u['url']}   (QR de uso unico: vale para um celular)")
    print()
    if shutil.which("qrencode"):
        subprocess.run(["qrencode", "-t", "ANSIUTF8", estado.url_pareamento()])
    if externo:
        print("Dica: no Mac, libere o Terminal em Ajustes > Privacidade e Seguranca > Acessibilidade;")
        print("      abra o PDF no leitor, deixe em tela cheia e clique nele antes de usar o celular.\n")
    if not a.sem_abrir:
        webbrowser.open(local)
    print("Ctrl+C para encerrar.")
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        if tray and tray.poll() is None:
            tray.terminate()
        for s in servidores:
            s.shutdown()
        if externo and externo.overlay and hasattr(externo.overlay, "fechar"):
            externo.overlay.fechar()


if __name__ == "__main__":
    main()
