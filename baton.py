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
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

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
TRANSICOES = ("fade", "slide", "zoom", "nenhuma")


CFG_PADRAO = {"laser_tam": 24, "laser_cor": "#ff2d2d", "foco_raio": 12, "foco_blur": 100, "foco_escuro": 25,
              "pen_larg": 3.5, "hl_larg": 14, "hl_alpha": 38, "meta_min": 50}
CFG_LIM = {"laser_tam": (6, 120), "foco_raio": (3, 35), "foco_blur": (0, 100), "foco_escuro": (0, 90),
           "pen_larg": (0.5, 20), "hl_larg": (3, 60), "hl_alpha": (5, 90), "meta_min": (1, 300)}
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
        self.urls = []  # [{"nome","url"}]: enderecos do celular (rede local primeiro, Tailscale depois)
        self.proporcao = proporcao
        self.remotos = 0
        self.zoom, self.zx, self.zy = 1.0, 0.5, 0.5
        self.seq = 0
        self.vistos = collections.deque(maxlen=400)
        self.t_rodando, self.t_base, self.t_desde = False, 0.0, 0.0  # cronometro da apresentacao
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
        elif acao == "reset":
            self.t_base, self.t_rodando = 0.0, False

    def snapshot(self):
        return {"pagina": self.pagina, "total": self.total, "transicao": self.transicao, "preto": self.cortina == "preto",
                "cortina": self.cortina, "fim": {"txt": self.fim_txt[0], "sub": self.fim_txt[1]},
                "auth": self.autorizado, "ativo": self.prefs["ativo"], "pedido": self.pedido,
                "acess": self.acessibilidade(),
                "remotos": self.remotos, "zoom": self.zoom, "zx": self.zx, "zy": self.zy, "seq": self.seq,
                "timer": {"rodando": self.t_rodando, "elapsed": round(self.decorrido(), 1)}}

    def publicar(self, msg):
        for q in list(self.clientes):
            try:
                q.put_nowait(msg)
            except queue.Full:
                pass

    # ---- modo residente: pausa, consentimento e deteccao de apresentacao
    def permitido(self):
        return self.prefs["ativo"] and self.autorizado

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
            residente.notificar("Baton", f"Apresentacao detectada em {app}. Clique para abrir o Baton (QR code) ou abra no celular: {self.remote_url}",
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
        elif acao == "instalar":
            instalar_login()
        elif acao == "desinstalar":
            instalar_login(remover=True)
        else:
            return False
        self._salvar_prefs()
        return True

    def acessibilidade(self):
        return acessibilidade_mac() if self.externo else None

    def painel_estado(self):
        return {"ativo": self.prefs["ativo"], "modo": self.prefs["modo"], "autorizado": self.autorizado,
                "pedido": self.pedido, "apps_ok": self.prefs["apps_ok"], "apps_nao": self.prefs["apps_nao"],
                "residente": self.residente, "acessibilidade": self.acessibilidade(), "deck": self.deck_info(),
                "conexoes": len(self.clientes)}

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
            else:
                return False
            if self.pagina != pagina_antes:
                self.zoom, self.zx, self.zy = 1.0, 0.5, 0.5
                self.tracos = []
                if not self.t_rodando and self.t_base == 0.0:  # o cronometro comeca no primeiro avanco
                    self._timer("start")
            if cmd == "limpar":
                self.tracos = []
            self.seq += 1
            snap = self.snapshot()
        if self.externo:
            if cmd == "goto" and self.pagina != pagina_antes:
                self.externo.ir(self.pagina)
            elif cmd in ("blank", "cortina"):
                self.externo.cortina(snap["cortina"], snap["fim"]["txt"], snap["fim"]["sub"])
            elif cmd == "confete":
                self.externo.confete(seg)
            elif cmd not in ("zoom", "limpar", "timer", "goto", "efeito"):
                self.externo.comando(cmd)
            if cmd == "limpar" or self.pagina != pagina_antes:
                self.externo.limpar()
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
            self.t_base, self.t_rodando = 0.0, False
        self.carregar_notas_sessao(notas_base)
        self._mudou()
        self.publicar({"deck": 1})

    def remover_deck(self):
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
        if self.externo:
            self.externo.laser(x, y, on, self.proporcao, modo, raio)
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
        if self.externo and pts:
            self.externo.desenho(traco, pts, self.proporcao)
        self.publicar({"draw": {"id": tid, "t": traco["t"], "c": traco["c"], "p": pts}})


def baixar_pdfjs():
    CACHE.mkdir(parents=True, exist_ok=True)
    for nome in PDFJS_FILES:
        destino = CACHE / nome
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
    if QR_FILE.exists() and QR_FILE.stat().st_size > 1000:
        return True
    try:
        QR_FILE.parent.mkdir(parents=True, exist_ok=True)
        with urllib.request.urlopen(QR_CDN, timeout=15) as r:
            QR_FILE.write_bytes(r.read())
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

    def tela(self):
        return 1920.0, 1080.0


class _Mac:
    """CoreGraphics via ctypes (sem dependencias). Precisa de Acessibilidade para o Terminal."""
    CODES = {"Right": 124, "Left": 123, "Home": 115, "End": 119, "b": 11}

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

    def mover(self, x, y):
        pt = self.Pt(x, y)
        self.cg.CGWarpMouseCursorPosition(pt)
        self._post(self.cg.CGEventCreateMouseEvent(None, 5, pt, 0))  # kCGEventMouseMoved

    def tela(self):
        r = self.cg.CGDisplayBounds(self.cg.CGMainDisplayID())
        return r.s.w, r.s.h


ARQ_ACESSO = Path.home() / ".cache" / "baton" / "acessibilidade"


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
    VK = {"Right": 0x27, "Left": 0x25, "Home": 0x24, "End": 0x23, "b": 0x42}
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

    def mover(self, x, y):
        subprocess.run(["xdotool", "mousemove", str(int(x)), str(int(y))], check=False)

    def tela(self):
        out = subprocess.run(["xdotool", "getdisplaygeometry"], capture_output=True, text=True).stdout.split()
        return float(out[0]), float(out[1])


class _OverlayLog:
    """Camada de teste: so imprime as mensagens."""
    def enviar(self, obj):
        print("overlay:", json.dumps(obj), flush=True)


class _OverlayMac:
    """Camada transparente nativa (overlay.swift), compilada na primeira execucao."""

    def __init__(self, tela=0, nivel=None):
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
        args = [str(binario), "--tela", str(tela)] + (["--nivel", str(nivel)] if nivel is not None else [])
        self.p = subprocess.Popen(args, stdin=subprocess.PIPE, text=True, bufsize=1)
        self.lock = threading.Lock()

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
        self.p = subprocess.Popen([sys.executable, str(fonte), "--tela", str(tela)], stdin=subprocess.PIPE,
                                  text=True, bufsize=1)
        self.lock = threading.Lock()
        time.sleep(1.0)
        if self.p.poll() is not None:  # sem Gtk, sem compositor, sem Tk: o processo encerra sozinho
            raise RuntimeError(f"{script} encerrou (codigo {self.p.returncode}); veja a mensagem acima")

    enviar = _OverlayMac.enviar
    fechar = _OverlayMac.fechar


class Externo:
    """Traduz comandos do celular em teclas, mouse e desenho (camada) para o app em primeiro plano."""
    TECLAS = {"next": "Right", "prev": "Left", "first": "Home", "last": "End"}

    def __init__(self, backend, overlay=None, cursor=True):
        self.b = backend
        self.overlay = overlay
        self.cursor = cursor or overlay is None
        self.ultimo_mouse = 0.0
        self.tecla_preto = False

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
        w, h = self.b.tela()
        if proporcao > w / h:  # o slide ocupa a tela inteira, com barras se a proporcao diferir
            largura, altura = w, w / proporcao
        else:
            largura, altura = h * proporcao, h
        ox, oy = (w - largura) / 2, (h - altura) / 2
        px, py = ox + x * largura, oy + y * altura
        return px, py, px / w, py / h

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


def criar_externo(usar_overlay=True, tela=0, cursor=False, nivel=None):
    overlay = None
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
<key>CFBundleShortVersionString</key><string>0.1</string><key>LSUIElement</key><true/>
<key>NSAppleEventsUsageDescription</key><string>O Baton detecta apresentacoes em tela cheia para perguntar se deve controla-las.</string>
</dict></plist>
"""


MARCA_APP = Path.home() / ".cache" / "baton" / "launcher-hash"
MARCA_APP_BANDEJA = Path.home() / ".cache" / "baton" / "launcher-bandeja"


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
    if not forcar and exe.exists() and marca.exists() and marca.read_text() == h:
        return str(exe)  # nao recompila: assinatura estavel mantem a permissao ja concedida
    (app / "Contents" / "MacOS").mkdir(parents=True, exist_ok=True)
    (app / "Contents" / "Resources").mkdir(parents=True, exist_ok=True)
    r = subprocess.run([swiftc, "-O", "-DBANDEJA", str(fonte), "-o", str(exe)], capture_output=True, text=True)
    bandeja = r.returncode == 0
    if not bandeja:  # sem o icone da barra de menu, mas com o launcher (permissoes) funcionando
        print("aviso: sem icone na barra de menu (falha ao compilar a bandeja):\n" + r.stderr[-1200:], file=sys.stderr)
        r = subprocess.run([swiftc, "-O", str(fonte), "-o", str(exe)], capture_output=True, text=True)
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
    if shutil.which("codesign"):
        subprocess.run(["codesign", "--force", "--deep", "-s", "-", str(app)], check=False, capture_output=True)
    subprocess.run(["tccutil", "reset", "Accessibility", "com.baton.app"], capture_output=True)  # assinatura nova: registro antigo nao vale
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
              "assinatura: " + " | ".join(x for x in run("codesign", "-dv", str(app)).splitlines() if x.startswith(("Identifier", "Signature", "CodeDirectory"))),
              "launchd: " + (run("launchctl", "list", "com.baton") or "(nao carregado)").replace("\n", " ")[:200],
              "processos: " + (run("pgrep", "-fli", "baton") or "(nenhum)").replace("\n", " ; ")[:200],
              f"acessibilidade do Baton.app (o que importa): {acesso_do_launcher()} (None = launcher parado)",
              f"acessibilidade deste terminal (nao conta): {acessibilidade_mac()}"]
        L += ["macOS: " + run("sw_vers", "-productVersion") + " | arquitetura: " + run("uname", "-m"),
              "quarentena: " + (run("xattr", str(app)) or "(nenhuma)").replace("\n", " "),
              "binario: " + run("file", "-b", str(exe))[:120],
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


def pedir_acessibilidade():
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


def rotulo_interface(nome, ip):
    """Nome amigavel do caminho (ou None para o que nao serve: loopback, Docker, VPN, Tailscale)."""
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
    vistos, lista = set(), []
    for iface, ip in parse_interfaces(saida):
        r = rotulo_interface(iface, ip)
        if r and _ip_valido(ip) and ip not in vistos:
            vistos.add(ip)
            lista.append((r, ip))
    return lista


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
<script src="__QRJS__"></script>
<style>__CSS__</style></head><body class="__CLASSE__">__HTML__
<script>
const BASE="__PDFJS__", TOKEN="__TOKEN__", REMOTE_URL="__REMOTE__", URLS=__URLS__, TEM_PDF=__TEMPDF__, EXTERNO=__EXTERNO__, OVERLAY=__OVERLAY__;
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
HTML_TELA = """<div id="stage"></div><div id="laser"></div><div id="foco"></div><div id="cortina"><div class="cb"></div><div class="ct"></div><div class="ct r"></div><div id="fimbox"><h1 id="fimt"></h1><p id="fims"></p></div></div><canvas id="conf"></canvas>
<div id="qr"><h1>Escaneie com o iPhone para controlar</h1><div class="cartao" id="qrsvg"></div><p id="qrurl"></p><p>Q: mostrar ou ocultar este codigo</p></div>
<div id="dica">F: tela cheia &middot; setas: navegar &middot; B: preto &middot; W: branco &middot; T: teatro &middot; E: fim &middot; C: limpar &middot; Q: QR code &middot; clique: avancar</div>"""

# desenha o QR (qrcode-generator) em #qrsvg; usado pela tela e pela pagina /qr
JS_QR = r"""
function desenharQR(){
  const url=REMOTE_URL;document.getElementById('qrurl').textContent=url;
  try{const q=qrcode(0,'M');q.addData(url);q.make();document.getElementById('qrsvg').innerHTML=q.createSvgTag({cellSize:8,margin:0,scalable:true})}
  catch(e){document.getElementById('qrsvg').textContent='QR indisponivel: abra a URL abaixo'}
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
  for(let i=1;i<=d.numPages;i++){
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
    if(txt)out['p'+i]=txt;
  }
  return out;
}
async function agruparPassos(pdf){
  const n=pdf.numPages,tok=[];
  for(let i=1;i<=n;i++){
    const pg=await pdf.getPage(i),W=pg.getViewport({scale:1}).width,tc=await pg.getTextContent();const m=new Map();
    tc.items.filter(x=>!LADO||x.transform[4]<W/2).forEach(x=>{const t=x.str.trim();if(!t||/^[\d\s\/]+$/.test(t)||/^(RASCUNHO|DRAFT)$/i.test(t))return;m.set(t,(m.get(t)||0)+1)});
    tok.push(m);
  }
  const contido=(a,b)=>{let tb=0,ta=0;for(const[k,v]of b)tb+=v;for(const[k,v]of a){ta+=v;if((b.get(k)||0)<v)return false}return ta>0&&tb>ta};
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
  laserEl.style.background='radial-gradient(circle,#fff 0,'+cfg.laser_cor+' 35%,'+rgba(cfg.laser_cor,.35)+' 60%,'+rgba(cfg.laser_cor,0)+' 72%)';
  laserEl.style.boxShadow='0 0 '+t*.9+'px '+t*.35+'px '+rgba(cfg.laser_cor,.55);
  redesenhar();
}
stage.style.transition='transform .3s ease';
const ov=document.createElement('canvas');ov.id='ov';stage.appendChild(ov);
function mk(){const c=document.createElement('canvas');c.className='pg';stage.insertBefore(c,ov);return c}
async function render(n,c){
  const p=await pdf.getPage(n),v0=p.getViewport({scale:1});
  const uw=larguraUtil(v0.width),esc=Math.min(innerWidth/uw,innerHeight/v0.height),dpr=Math.min(devicePixelRatio||1,2),v=p.getViewport({scale:esc*dpr});
  c.width=larguraUtil(v.width);c.height=v.height;c.style.width=uw*esc+'px';c.style.height=v0.height*esc+'px';
  await p.render({canvasContext:c.getContext('2d'),viewport:v}).promise;
}
function geom(){const w=parseFloat(atual.style.width),h=parseFloat(atual.style.height);return{w,h,left:(innerWidth-w)/2,top:(innerHeight-h)/2}}
function aplicarZoom(){
  if(!atual)return;const z=est.zoom||1;
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
addEventListener('click',()=>{if(qrEl.style.display!=='flex')cmd('next')});
addEventListener('resize',()=>{ajustarOv();if(atual){const n=paginaAtual;paginaAtual=0;atual.remove();atual=null;ir(n)}});
setTimeout(()=>{const d=document.getElementById('dica');d.style.opacity=0},6000);
ajustarOv();desenharQR();mostrarQR(true);
let es=null,ultimaMsg=Date.now();
function conectar(){
  if(es){try{es.close()}catch(e){}}
  es=new EventSource('/events?k='+encodeURIComponent(TOKEN));ultimaMsg=Date.now();
  es.onmessage=ev=>{ultimaMsg=Date.now();msg(JSON.parse(ev.data))};
}
setInterval(()=>{if(Date.now()-ultimaMsg>12000)conectar()},3000);
document.addEventListener('visibilitychange',()=>{if(document.visibilityState==='visible')conectar()});
pdfjsLib.getDocument('/pdf?k='+encodeURIComponent(TOKEN)).promise.then(async d=>{pdf=d;try{await detectaLado(d);mapa=await agruparPassos(d)}catch(e){}conectar()});
"""

# ------------------------------------------------------------------ celular (iPhone)
CSS_REMOTO = """
body{display:flex;flex-direction:column;padding:env(safe-area-inset-top) 12px env(safe-area-inset-bottom);gap:8px;touch-action:none;user-select:none;-webkit-user-select:none}
#topo{display:flex;justify-content:space-between;align-items:center;padding-top:6px;font-size:14px;color:#aaa;flex:0 0 auto}
#pos{font-size:19px;color:#fff;font-weight:600}
#dot{width:10px;height:10px;border-radius:50%;background:#e33;display:inline-block;margin-right:6px}#dot.ok{background:#3c6}
#vista{position:relative;flex:0 0 auto;background:#111;border-radius:10px;overflow:hidden;align-self:center;touch-action:none}
#vista canvas{width:100%;height:100%;position:absolute;left:0;top:0}
#ponto{position:absolute;width:30px;height:30px;margin:-15px 0 0 -15px;border-radius:50%;pointer-events:none;display:none;
background:radial-gradient(circle,#fff 0,#ff2d2d 35%,rgba(255,0,0,.4) 60%,rgba(255,0,0,0) 72%);box-shadow:0 0 18px 6px rgba(255,40,40,.6)}
#zrect{position:absolute;border:2px solid #0a84ff;background:rgba(10,132,255,.15);pointer-events:none;display:none;border-radius:3px}
button{border:0;border-radius:12px;background:#1d1d1f;color:#fff;font-size:15px;font-weight:600;touch-action:manipulation}
button:active{background:#333}
.linha{display:flex;gap:6px;flex:0 0 auto}.linha button,.linha select{flex:1;padding:11px 0}
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
.ajc{display:flex;gap:12px;margin:8px 0 4px;align-items:center;font-size:13px;color:#ccc}.ajc i{width:26px;height:26px;border-radius:50%;border:3px solid transparent;display:block}.ajc i.sel{border-color:#fff}
#info{display:flex;gap:10px;align-items:stretch;flex:0 0 auto}
#prevbox{flex:1;display:flex;align-items:center;gap:8px;background:#111;border-radius:12px;padding:6px 10px;min-width:0}
#prevbox canvas{height:62px;max-width:60%;border-radius:6px;background:#1a1a1c}#prevlbl{font-size:12px;color:#888}
#relogio{flex:0 0 40%;background:#111;border-radius:12px;padding:6px 10px;display:flex;flex-direction:column;justify-content:center;align-items:center}
#tempo{font-size:30px;font-weight:700;font-variant-numeric:tabular-nums;color:#fff;line-height:1.1}#restante{font-size:12px;color:#888;margin-top:2px}
#relogio.alerta #tempo{color:#ffd60a}#relogio.estouro #tempo{color:#ff453a}#relogio.pausado #tempo{opacity:.55}
#irbox{position:fixed;inset:0;background:rgba(0,0,0,.94);z-index:20;display:none;flex-direction:column;padding:env(safe-area-inset-top) 12px 12px;gap:10px}
#irhead{display:flex;gap:8px;padding-top:10px}#irhead input{flex:1;background:#1d1d1f;color:#fff;border:0;border-radius:12px;font-size:20px;padding:12px;min-width:0}
#irhead button{padding:0 18px;font-size:17px}#irgo{background:#0a84ff}
#irgrid{flex:1;overflow:auto;display:grid;grid-template-columns:repeat(3,1fr);gap:10px;align-content:start;touch-action:pan-y}
#irgrid div{position:relative;background:#111;border-radius:8px;overflow:hidden;border:2px solid transparent}#irgrid div.atual{border-color:#0a84ff}
#irgrid canvas{width:100%;display:block}#irgrid span{position:absolute;left:4px;top:2px;font-size:12px;font-weight:700;color:#fff;background:rgba(0,0,0,.6);padding:1px 6px;border-radius:6px}
#notas{flex:1;min-height:70px;background:#111;border-radius:12px;display:flex;flex-direction:column;overflow:hidden}
#nhead{display:flex;justify-content:space-between;align-items:center;padding:4px 10px;font-size:12px;color:#888;flex:0 0 auto}
#nhead button{padding:3px 10px;font-size:13px;border-radius:8px;margin-left:4px}
#ntxt{flex:1;overflow:auto;padding:2px 12px 10px;line-height:1.35;touch-action:pan-y;-webkit-overflow-scrolling:touch;user-select:text;-webkit-user-select:text}
#ned{flex:1;width:100%;box-sizing:border-box;background:#1d1d1f;color:#fff;border:0;border-radius:8px;font:inherit;line-height:1.35;padding:8px 10px;resize:none;min-height:0}
#nbt{display:none;gap:6px;padding:6px 10px 8px}#nbt button{flex:1;padding:8px;font-size:14px;border-radius:10px}#nsv{background:#0a84ff}
#ntxt p{margin:0 0 .6em}#ntxt ul{margin:0 0 .6em 1.1em}#ntxt .vazio{color:#666}
#botoes{display:flex;gap:10px;height:84px;flex:0 0 auto}#botoes button{font-size:19px;border-radius:16px}
#ant{flex:1}#seg{flex:2;background:#0a84ff}#seg:active{background:#006fe0}
"""
HTML_REMOTO = """<div id="aviso"></div><div id="topo"><span><i id="dot"></i><span id="st">conectando</span></span><span id="pos">- / -</span></div>
<div id="vista"><canvas id="cv"></canvas><canvas id="ovp"></canvas><div id="zrect"></div><div id="ponto"></div></div>
<div id="info"><div id="prevbox"><canvas id="cvp"></canvas><span id="prevlbl">Proximo</span></div><div id="relogio"><div id="tempo">00:00</div><div id="restante">toque para iniciar</div></div></div>
<div class="linha" id="tools"><button id="bl">Laser</button><button id="bf">Foco</button><button id="bc">Caneta</button><button id="bm">Marca</button><button id="bz">Zoom</button></div>
<div class="linha" id="zbar"><button id="zm">&minus;</button><span id="zv">1x</span><button id="zp">+</button><button id="z1">1x</button></div>
<div class="linha" id="raiobar"><span>Raio do foco</span><input id="raio" type="range" min="4" max="30" value="12"></div>
<div id="cores"><i data-c="#ff3b30" style="background:#ff3b30"></i><i data-c="#0a84ff" style="background:#0a84ff"></i><i data-c="#30d158" style="background:#30d158"></i><i data-c="#ffd60a" style="background:#ffd60a"></i><i data-c="#ffffff" style="background:#fff"></i></div>
<div class="linha" id="tools2"><button id="bx">Limpar</button><button id="bb">Preto</button><button id="bg">Ir para</button><button id="bk">Extras</button><button id="ba">Ajustes</button></div>
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
<div id="irbox"><div id="irhead"><input id="irn" type="number" inputmode="numeric" min="1" placeholder="no do slide"><button id="irgo">Ir</button><button id="irx">Fechar</button></div><div id="irgrid"></div></div>
<div id="notas"><div id="nhead"><span id="nt">Notas do apresentador</span><span><button id="ne">Editar</button><button id="nm">A&minus;</button><button id="np">A+</button></span></div><div id="ntxt"></div><textarea id="ned" style="display:none" placeholder="Digite a nota deste slide"></textarea><div id="nbt"><button id="nsv">Salvar</button><button id="ntd">Todas</button><button id="ncn">Cancelar</button></div></div>
<div id="botoes"><button id="ant">&#9664; Anterior</button><button id="seg">Proximo &#9654;</button></div>"""
JS_REMOTO = r"""
const $=id=>document.getElementById(id);
let raioFoco=0.12;try{raioFoco=(parseInt(localStorage.getItem('baton-raio'))||12)/100}catch(e){}
let pdf=null,mapa=null,pag=1,total=0,ferr=null,ultimo=0,zoom={z:1,x:.5,y:.5},es=null,ultimaMsg=Date.now(),seqVisto=-1,notas={},notasPdf={},fonte=17,desenhado=0;
const NIVEIS=[1,1.5,2,3,4],cores={pen:'#ff3b30',hl:'#ffd60a'};
const PADRAO={meta_min:50,laser_tam:24,laser_cor:'#ff2d2d',foco_raio:12,foco_blur:100,foco_escuro:25,pen_larg:3.5,hl_larg:14,hl_alpha:38};
const DEFS=[['meta_min','Meta de tempo (min)',5,120,5],['laser_tam','Laser: tamanho',6,80,1],['foco_raio','Foco: raio',3,35,1],['foco_blur','Foco: desfoque',0,100,5],['foco_escuro','Foco: escurecer',0,90,5],['pen_larg','Caneta: espessura',0.5,12,0.5],['hl_larg','Marca: espessura',3,40,1],['hl_alpha','Marca: opacidade',10,80,5]];
let cfg=Object.assign({},PADRAO),tCfg=null;
try{fonte=parseInt(localStorage.getItem('baton-fonte'))||17}catch(e){}
if(EXTERNO){$('bz').style.display='none';$('tr').style.display='none';if(!OVERLAY){['bf','bc','bm','bx'].forEach(i=>$(i).style.display='none');document.querySelectorAll('#exk [data-k]:not([data-k=preto])').forEach(b=>b.style.display='none');document.querySelector('[data-s=confete]').style.display='none'}}
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
async function desenha(n,cv,larg){
  const p=await pdf.getPage(n),v0=p.getViewport({scale:1}),dpr=Math.min(devicePixelRatio||1,3),esc=larg*dpr/larguraUtil(v0.width),v=p.getViewport({scale:esc});
  cv.width=larguraUtil(v.width);cv.height=v.height;await p.render({canvasContext:cv.getContext('2d'),viewport:v}).promise;
}
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
  $('ntxt').style.fontSize=fonte+'px';
  $('ntxt').innerHTML=t?md(t):'<p class="vazio">Sem notas para este slide.</p>';$('ntxt').scrollTop=0;
}
async function mostra(){
  rotulo();mostraNotas();
  const larg=$('vista').parentElement.clientWidth-24;
  if(!pdf){$('vista').style.width=larg+'px';$('vista').style.height=(larg*9/16)+'px';ajustaOvp();return}
  if(desenhado===pag)return;desenhado=pag;limpaLocal();
  const p=await pdf.getPage(pag),v0=p.getViewport({scale:1});
  const uw=larguraUtil(v0.width),w=Math.min(larg,(innerHeight*0.24)*uw/v0.height);
  $('vista').style.width=w+'px';$('vista').style.height=(w*v0.height/uw)+'px';ajustaOvp();
  desenha(pag,$('cv'),w);
  previa();
}
function previa(){
  const prox=pag+1;
  if(!pdf||prox>total){const c=$('cvp'),g=c.getContext('2d');g.clearRect(0,0,c.width,c.height);c.style.visibility='hidden';
    $('prevlbl').textContent=!pdf?'sem PDF: sem previa':'ultimo slide';return}
  $('cvp').style.visibility='visible';
  const mesmo=mapa&&mapa.frameDe[prox]===mapa.frameDe[pag];
  $('prevlbl').textContent=mesmo?'Proximo passo':'Proximo slide';desenha(prox,$('cvp'),150);
}
function retangulo(){
  const r=$('zrect'),z=zoom.z;if(z<=1.001||ferr!=='zoom'){r.style.display='none';return}
  const w=1/z,cx=Math.max(w/2,Math.min(1-w/2,zoom.x)),cy=Math.max(w/2,Math.min(1-w/2,zoom.y));
  r.style.display='block';r.style.width=w*100+'%';r.style.height=w*100+'%';r.style.left=(cx-w/2)*100+'%';r.style.top=(cy-w/2)*100+'%';
}
let tm={rodando:false,elapsed:0,rec:performance.now()};
const mmss=t=>{t=Math.max(0,Math.floor(t));const h=Math.floor(t/3600),m=Math.floor(t%3600/60),s=t%60;return(h?h+':'+String(m).padStart(2,'0'):String(m).padStart(2,'0'))+':'+String(s).padStart(2,'0')};
function sincTimer(t){if(t)tm={rodando:t.rodando,elapsed:t.elapsed,rec:performance.now()}}
function tick(){
  const el=tm.elapsed+(tm.rodando?(performance.now()-tm.rec)/1000:0),meta=cfg.meta_min*60,rest=meta-el,r=$('relogio');
  $('tempo').textContent=mmss(el);
  r.classList.toggle('pausado',!tm.rodando&&el>0);r.classList.toggle('alerta',rest<=meta*0.1&&rest>0);r.classList.toggle('estouro',rest<=0);
  $('restante').textContent=el===0&&!tm.rodando?'toque para iniciar':(rest>0?'restam '+mmss(rest):'+'+mmss(-rest)+' acima da meta')+(tm.rodando?'':'  (pausado)');
}
setInterval(tick,500);
$('relogio').onclick=()=>cmd('timer','toggle');$('tz').onclick=()=>cmd('timer','reset');
const esc=x=>String(x).replace(/&/g,'&amp;').replace(/</g,'&lt;');
function avisoUI(s){
  const el=$('aviso');
  if(s.acess===false){el.innerHTML='<b>O Mac nao deixou o Baton enviar teclas.</b> No computador: Ajustes do Sistema > Privacidade e Seguranca > Acessibilidade, ative o Baton (painel em localhost:8765/painel).'}
  else if(s.ativo===false){el.innerHTML='<b>Baton pausado</b> no computador. Retome pelo painel (localhost:8765/painel).'}
  else if(s.auth!==false){el.style.display='none';return}
  else if(s.pedido){el.innerHTML='<b>Apresentacao detectada</b> ('+esc(s.pedido.app)+'). Controlar com o Baton?<div class="avb"><button data-a="sim">Controlar</button><button data-a="sempre">Sempre neste app</button><button data-a="nao">Agora nao</button><button data-a="nunca">Nunca neste app</button></div>'}
  else{el.innerHTML='Em espera: nenhuma apresentacao em tela cheia.<div class="avb"><button data-a="manual">Controlar mesmo assim</button></div>'}
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
let lsEnv=false,lsPend=null;
function envLaser(x,y,on,m){const t=performance.now();if(on&&t-ultimo<25)return;ultimo=t;lsPend={x,y,on,m,r:raioFoco};if(!lsEnv)bombeiaLaser()}
function bombeiaLaser(){
  if(!lsPend){lsEnv=false;return}lsEnv=true;const d=lsPend;lsPend=null;
  const ac=new AbortController(),to=setTimeout(()=>ac.abort(),1500);
  fetch('/laser',{method:'POST',headers:{'Content-Type':'application/json','X-Token':TOKEN},body:JSON.stringify(d),keepalive:true,signal:ac.signal}).catch(()=>{}).finally(()=>{clearTimeout(to);bombeiaLaser()})}
function pos(e){const r=$('vista').getBoundingClientRect();return{x:Math.max(0,Math.min(1,(e.clientX-r.left)/r.width)),y:Math.max(0,Math.min(1,(e.clientY-r.top)/r.height))}}
function ponto(e,on){const{x,y}=pos(e);const p=$('ponto');p.style.display=on?'block':'none';p.style.left=x*100+'%';p.style.top=y*100+'%';envLaser(x,y,on,ferr==='foco'?'foco':'laser')}
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
const toques=new Map();let dist0=0,z0=1,r0=.12;
$('vista').addEventListener('pointerdown',e=>{
  $('vista').setPointerCapture(e.pointerId);toques.set(e.pointerId,e);
  if(ferr==='laser'||ferr==='foco')ponto(e,true);
  else if(ferr==='caneta'||ferr==='marca')iniciaTraco(e);
  else if(ferr==='zoom'){if(toques.size===2){const[a,b]=[...toques.values()];dist0=Math.hypot(a.clientX-b.clientX,a.clientY-b.clientY);z0=zoom.z}
    else{const{x,y}=pos(e);enviaZoom(zoom.z<=1.001?2:zoom.z,x,y,true)}}
});
$('vista').addEventListener('pointermove',e=>{
  if(!toques.has(e.pointerId))return;toques.set(e.pointerId,e);
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
  if(ferr==='laser'||ferr==='foco')ponto(e,false);fimTraco();toques.delete(e.pointerId);if(toques.size<2)dist0=0;
  if(ferr==='zoom'&&toques.size===0)cmd('zoom',zoom)}));
$('seg').onclick=()=>cmd('next');$('ant').onclick=()=>cmd('prev');
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
  ['#ff2d2d','#0a84ff','#30d158','#ffd60a','#ffffff'].forEach(h=>{const i=document.createElement('i');i.style.background=h;i.dataset.c=h;i.onclick=()=>enviaCfg({laser_cor:h});lc.appendChild(i)});
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
function textoTodas(){const todas=Object.assign({},notasPdf,notas);return Object.keys(todas).sort((a,b)=>parseInt(a.replace(/\D/g,''))-parseInt(b.replace(/\D/g,''))).map(k=>'## '+k+'\n'+todas[k]).join('\n\n')}
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
if(TEM_PDF){pdfjsLib.getDocument('/pdf?k='+encodeURIComponent(TOKEN)).promise.then(async d=>{pdf=d;total=d.numPages;try{await detectaLado(d);notasPdf=await notasDoPdf(d);mapa=await agruparPassos(d)}catch(e){}mostraNotas();conectar()})}
else{$('vista').style.background='#1a1a1c';$('vista').insertAdjacentHTML('beforeend','<div style="position:absolute;inset:0;display:flex;align-items:center;justify-content:center;color:#666;font-size:14px;text-align:center;padding:12px;pointer-events:none">area do laser<br>(mapeada na tela do Mac)</div>');conectar()}
"""


PAGINA_QR = """<!doctype html><html lang="pt-br"><head><meta charset="utf-8"><title>Baton - QR</title>
<script src="__QRJS__"></script>
<style>__CSS__</style></head><body class="tela"><div id="qr" style="display:flex"><h1>Escaneie com o celular para controlar</h1><div id="qrlista" style="display:flex;gap:24px;flex-wrap:wrap;justify-content:center"></div>
<p>Abra a apresentacao no leitor que quiser (PowerPoint, Keynote, ReLaTeX...), deixe-a em tela cheia e use o celular. Painel: <a href="/painel" style="color:#0a84ff">/painel</a></p></div>
<script>const URLS=__URLS__;
URLS.forEach(function(u,i){const d=document.createElement('div');d.style.cssText='display:flex;flex-direction:column;align-items:center;max-width:320px';
  d.innerHTML='<h2 style="font-size:18px;margin:0 0 8px">'+u.nome+'</h2><div class="cartao" style="width:260px"></div><p style="word-break:break-all;font-size:12px;opacity:.7"></p>';
  d.querySelector('p').textContent=u.url;
  try{const q=qrcode(0,'M');q.addData(u.url);q.make();d.querySelector('.cartao').innerHTML=q.createSvgTag({cellSize:8,margin:0,scalable:true})}catch(e){d.querySelector('.cartao').textContent='QR indisponivel'}
  document.getElementById('qrlista').appendChild(d)});</script></body></html>"""


def montar_qr(estado, usar_qr_cache):
    qrjs = "/vendor/qr.js" if usar_qr_cache else QR_CDN
    return (PAGINA_QR.replace("__QRJS__", qrjs).replace("__CSS__", CSS_COMUM + CSS_TELA)
            .replace("__REMOTE__", estado.remote_url).replace("__URLS__", json.dumps(estado.urls).replace("</", "<\\/")))


def montar(modo, estado, usar_cache, usar_qr_cache=False):
    pdfjs = "/vendor/" if usar_cache else PDFJS_CDN
    qrjs = "/vendor/qr.js" if usar_qr_cache else QR_CDN
    if modo == "tela":
        css, html, js, classe = (CSS_COMUM + CSS_TELA, HTML_TELA,
                                 JS_QR + JS_PASSOS + JS_TELA.replace("__TRANSICAO__", estado.transicao), "tela")
    else:
        css, html, js, classe = CSS_COMUM + CSS_REMOTO, HTML_REMOTO, JS_PASSOS + JS_REMOTO, "remoto"
    titulo = "Baton - " + (Path(estado.pdf).stem if estado.pdf else "controle")
    return (PAGINA_BASE.replace("__TITULO__", titulo).replace("__PDFJS__", pdfjs).replace("__CSS__", css)
            .replace("__HTML__", html).replace("__JS__", js).replace("__CLASSE__", classe)
            .replace("__QRJS__", qrjs).replace("__REMOTE__", estado.remote_url)
            .replace("__URLS__", json.dumps(estado.urls).replace("</", "<\\/"))
            .replace("__TEMPDF__", "true" if estado.pdf else "false")
            .replace("__EXTERNO__", "true" if estado.externo else "false")
            .replace("__OVERLAY__", "true" if (estado.externo and estado.externo.overlay) else "false")
            .replace("__TOKEN__", estado.token if modo == "remoto" else ""))


class Servidor(ThreadingHTTPServer):
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

        def _local(self):
            return self.client_address[0] in ("127.0.0.1", "::1")

        def _leitura_ok(self, u):
            """GET com dado sensivel (PDF, eventos, manifesto): so o computador ou quem tem o token (?k=)."""
            k = parse_qs(u.query).get("k", [""])[0]
            return self._local() or secrets.compare_digest(k, estado.token)

        def _autorizado(self):
            if self._local():
                return True
            tok = self.headers.get("X-Token", "")
            return secrets.compare_digest(tok, estado.token)

        def _enviar(self, codigo, corpo, tipo="application/json", extra=None):
            if isinstance(corpo, str):
                corpo = corpo.encode("utf-8")
            self.send_response(codigo)
            self.send_header("Content-Type", tipo)
            self.send_header("Content-Length", str(len(corpo)))
            self.send_header("Cache-Control", "no-store")
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self._headers_buffer.append(b"\r\n")
            try:
                self.wfile.write(b"".join(self._headers_buffer) + corpo)  # um unico pacote TCP
                self._headers_buffer = []
            except (BrokenPipeError, ConnectionResetError, OSError):
                self.close_connection = True

        def do_GET(self):
            u = urlparse(self.path)
            if u.path in ("/", "/qr") and not self._local():
                return self._enviar(403, "abra o Baton pelo QR code ou pelo endereco do celular", "text/plain; charset=utf-8")
            if u.path == "/":
                if estado.externo or not estado.pdf:
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
            if u.path in ("/icon.png", "/icon-192.png", "/icon-512.png"):
                tam = {"/icon-192.png": 192, "/icon-512.png": 512}.get(u.path, 180)
                return self._enviar(200, icone_png(tam), "image/png", {"Cache-Control": "max-age=86400"})
            if u.path == "/manifest.json":
                if not self._leitura_ok(u):
                    return self._enviar(403, "{}")
                m = {"name": "Baton", "short_name": "Baton", "display": "standalone", "background_color": "#000000",
                     "theme_color": "#000000", "start_url": "/remote?k=" + estado.token,
                     "icons": [{"src": "/icon-192.png", "sizes": "192x192", "type": "image/png", "purpose": "any"},
                               {"src": "/icon-512.png", "sizes": "512x512", "type": "image/png", "purpose": "any"},
                               {"src": "/icon.png", "sizes": "180x180", "type": "image/png"}]}
                return self._enviar(200, json.dumps(m), "application/manifest+json")
            if u.path == "/vendor/qr.js" and usar_qr_cache:
                return self._enviar(200, QR_FILE.read_bytes(), "application/javascript")
            if u.path == "/remote":
                k = parse_qs(u.query).get("k", [""])[0]
                if not (self._local() or secrets.compare_digest(k, estado.token)):
                    return self._enviar(403, "token invalido", "text/plain; charset=utf-8")
                if not self._local():
                    estado.remotos += 1
                    estado.publicar(estado.snapshot())
                return self._enviar(200, montar("remoto", estado, usar_cache, usar_qr_cache), "text/html; charset=utf-8")
            if u.path == "/pdf" and estado.pdf:
                if not self._leitura_ok(u):
                    return self._enviar(403, "token invalido", "text/plain; charset=utf-8")
                return self._enviar(200, Path(estado.pdf).read_bytes(), "application/pdf")
            if u.path.startswith("/vendor/") and usar_cache:
                nome = u.path.split("/")[-1]
                f = CACHE / nome
                if nome in PDFJS_FILES and f.exists():
                    return self._enviar(200, f.read_bytes(), "application/javascript")
            if u.path == "/events":
                if not self._leitura_ok(u):
                    return self._enviar(403, "token invalido", "text/plain; charset=utf-8")
                return self._sse()
            if u.path == "/cfg":
                k = parse_qs(u.query).get("k", [""])[0]
                if not (self._local() or secrets.compare_digest(k, estado.token)):
                    return self._enviar(403, "{}")
                return self._enviar(200, json.dumps(estado.cfg))
            if u.path == "/notas":
                k = parse_qs(u.query).get("k", [""])[0]
                if not (self._local() or secrets.compare_digest(k, estado.token)):
                    return self._enviar(403, "{}")
                return self._enviar(200, json.dumps({"notas": estado.notas}))
            if u.path == "/estado":
                return self._enviar(200, json.dumps(estado.snapshot()))
            self._enviar(404, "nao encontrado", "text/plain; charset=utf-8")

        def _sse(self):
            q = queue.Queue(maxsize=300)
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
            if not self._local():
                return self._enviar(403, '{"erro":"local"}')
            n = int(self.headers.get("Content-Length", "0"))
            if n > 100 * 1024 * 1024:
                return self._enviar(413, '{"erro":"arquivo maior que 100 MB"}')
            dados = self.rfile.read(n)
            nome = parse_qs(urlparse(self.path).query).get("nome", ["apresentacao.pdf"])[0]
            try:
                pdf, notas = preparar_deck(dados, nome)
                if pdf:
                    estado.trocar_deck(pdf, notas)
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
            if self.path in ("/cmd", "/laser", "/draw") and not estado.permitido():
                return self._enviar(423, json.dumps(estado.snapshot()))
            if self.path == "/cmd":
                ok = estado.comando(dados.get("cmd"), dados.get("valor"), dados.get("cid"))
                return self._enviar(200 if ok else 400, json.dumps(estado.snapshot()))
            if self.path == "/laser":
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
    ap.add_argument("--sem-abrir", action="store_true", help="nao abre o navegador no Mac")
    ap.add_argument("--tela", type=int, default=0, help="modo externo: indice da tela onde a camada desenha (0 = principal)")
    ap.add_argument("--nivel", type=int, help="modo externo: nivel da janela da camada (padrao: o maximo do sistema)")
    ap.add_argument("--sem-overlay", action="store_true", help="modo externo: nao usa a camada de desenho (laser = cursor)")
    ap.add_argument("--cursor", action="store_true", help="modo externo: move tambem o cursor do mouse com o laser")
    ap.add_argument("--notas", help="arquivo Markdown com as notas do apresentador (padrao: <pdf>.notas.md)")
    ap.add_argument("--importar-notas", metavar="ARQUIVO",
                    help="importa as notas de um .pptx (notas do apresentador) ou .md para a sessao do Baton e sai")
    ap.add_argument("--tray", action="store_true", help="mostra o icone na bandeja (ja e o padrao no modo --residente)")
    ap.add_argument("--sem-tray", action="store_true", help="nao mostra o icone na bandeja")
    ap.add_argument("--recompilar-app", action="store_true", help="macOS: recompila o Baton.app e mostra os erros do compilador (a permissao de Acessibilidade precisa ser dada de novo)")
    ap.add_argument("--diagnostico", action="store_true", help="imprime um resumo do ambiente (Baton.app, permissao, log) para suporte")
    ap.add_argument("--permissao", action="store_true", help="macOS: pede a permissao de Acessibilidade para o Baton.app e abre os Ajustes")
    ap.add_argument("--rede", action="store_true", help="(sem efeito: a rede local ja e o endereco principal)")
    ap.add_argument("--residente", action="store_true",
                    help="segundo plano: fica em espera e pergunta no celular quando uma apresentacao abrir em tela cheia")
    ap.add_argument("--pausar", action="store_true", help="pausa o Baton em execucao (nao controla nada ate --retomar)")
    ap.add_argument("--retomar", action="store_true", help="retoma o Baton pausado")
    ap.add_argument("--detectar", action="store_true", help="mostra qual app esta em tela cheia agora e sai (teste)")
    ap.add_argument("--novo-token", action="store_true", help="gera outro token (invalida o QR e a pagina abertos no celular)")
    a = ap.parse_args()
    if a.instalar or a.desinstalar:
        return instalar_login(remover=a.desinstalar)
    if a.recompilar_app:
        exe = montar_app_mac(forcar=True)
        return print(f"pronto: {exe}\nAtive o Baton em Acessibilidade e rode: launchctl kickstart -k gui/$(id -u)/com.baton" if exe else "falhou (veja os avisos acima)")
    if a.diagnostico:
        return print(diagnostico())
    if a.permissao:
        return print(pedir_acessibilidade())
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
    token = token_estavel(a.novo_token)
    if a.host:
        ips = [("Rede local", a.host)]
    else:
        rota = ip_lan()  # a que tem rota para fora (Wi-Fi/cabo) vem primeiro; sem internet, a primeira que existir
        locais = sorted(ips_locais(), key=lambda x: x[1] != rota)
        ips = [(("Wi-Fi / rede local" if ip == rota else r), ip) for r, ip in locais[:4]]
        if rota and rota not in [ip for _, ip in ips]:
            ips.insert(0, ("Wi-Fi / rede local", rota))
        ips.append(("Tailscale (fora do Wi-Fi)", ip_tailscale()))
    urls = [{"nome": n, "url": f"http://{ip}:{a.porta}/remote?k={token}"} for n, ip in ips if ip]
    if not urls:
        urls = [{"nome": "Rede local", "url": f"http://IP-DO-COMPUTADOR:{a.porta}/remote?k={token}"}]
    remoto = urls[0]["url"]  # rede local primeiro: funciona sem VPN e e o caminho mais estavel
    prop = 16 / 9
    if a.proporcao:
        try:
            w, h = a.proporcao.split(":")
            prop = float(w) / float(h)
        except ValueError:
            sys.exit("use --proporcao largura:altura, por exemplo 16:9")
    elif pdf:
        prop = proporcao_pdf(pdf)
    externo = criar_externo(not a.sem_overlay, a.tela, a.cursor, a.nivel) if a.externo else None
    if externo and not pdf and not a.proporcao:
        try:
            tw, th = externo.b.tela()
            prop = tw / th  # area do laser com o formato da tela do Mac
        except Exception:
            pass
    total = contar_paginas(pdf) if pdf else 9999
    estado = Estado(pdf, total, a.transicao, token, externo, remoto, prop)
    estado.urls = urls
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
    local = f"http://localhost:{a.porta}/"
    estado.local_url = local
    tray = residente.iniciar_tray(a.porta) if (a.residente or a.tray) and not a.sem_tray else None
    print(f"\nModo: {'externo (teclas e mouse para o app em primeiro plano)' if externo else 'tela do Baton'}")
    if pdf:
        print(f"PDF: {pdf} ({estado.total} paginas)")
    print(f"QR code no Mac:   {local}")
    print(f"Painel (pausar, modo, apps): {local}painel")
    for u in urls:
        print(f"Celular ({u['nome']}): {u['url']}")
    print()
    if shutil.which("qrencode"):
        subprocess.run(["qrencode", "-t", "ANSIUTF8", remoto])
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
