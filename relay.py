"""relay.py: relay de sessoes do Baton.

Varios computadores, cada um com seu Baton, atras de UMA URL publica (ex.: https://baton.exemplo.com).
O relay roda no servidor (`baton --servir-relay`); cada Baton e um agente que se conecta de saida por WebSocket
e recebe as requisicoes dos celulares. O roteamento usa a credencial do proprio celular (codigo do QR em /p/<codigo>
ou token), entao as paginas continuam com caminhos absolutos e nada muda no celular. Somente biblioteca padrao.
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
            self.s.close()
        except OSError:
            pass


def quadro(rid, tipo, dados=b""):
    return struct.pack(">IB", rid, tipo) + dados


# ------------------------------------------------------------------ servidor (relay)

class Agente:
    def __init__(self, ws, ip):
        self.ws, self.ip, self.sid = ws, ip, secrets.token_urlsafe(8)
        self.codigos, self.tokens, self.pend, self.lock, self._id = set(), set(), {}, threading.Lock(), 0
        self.vivo = True

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
    def __init__(self, chave, max_sessoes=50):
        self.chave, self.max_sessoes, self.agentes, self.lock = chave, max_sessoes, {}, threading.Lock()

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
        return ags[0] if (estatico and ags) else None


def _cookie(cabecalhos, nome):
    try:
        c = SimpleCookie(cabecalhos.get("Cookie", ""))
        return c[nome].value if nome in c else ""
    except Exception:
        return ""


def criar_handler(relay):
    class H(BaseHTTPRequestHandler):
        server_version = "BatonRelay"

        def log_message(self, *a):
            pass

        def _texto(self, status, msg):
            corpo = msg.encode()
            self.send_response(status)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(corpo)))
            self.end_headers()
            self.wfile.write(corpo)

        def _agente_ws(self):
            if not secrets.compare_digest(self.headers.get("X-Baton-Chave", ""), relay.chave):
                time.sleep(1)
                return self._texto(403, "chave do relay invalida")
            k = self.headers.get("Sec-WebSocket-Key", "")
            if not k or "websocket" not in self.headers.get("Upgrade", "").lower():
                return self._texto(400, "WebSocket esperado")
            ws = WS(self.connection, False)
            ag = Agente(ws, self.client_address[0])
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
            try:
                ag.ws.enviar(2, quadro(rid, T_CAB, json.dumps({"m": self.command, "p": self.path, "h": hs}).encode()))
                for i in range(0, len(corpo), PEDACO):
                    ag.ws.enviar(2, quadro(rid, T_DADOS, corpo[i:i + PEDACO]))
                ag.ws.enviar(2, quadro(rid, T_FIM))
                self._responder(ag, rid, fila)
            except OSError:
                pass
            finally:
                with ag.lock:
                    ag.pend.pop(rid, None)

        def _responder(self, ag, rid, fila):
            comecou = False
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
                    self.send_response_only(int(cab["status"]))
                    for k, v in cab["h"]:
                        if k.lower() not in HOP:
                            self.send_header(k, v)
                    self.send_header("Set-Cookie", f"baton_s={ag.sid}; Path=/; Max-Age=31536000; SameSite=Lax; HttpOnly; Secure")
                    self.send_header("Connection", "close")
                    self.end_headers()
                    comecou = True
                elif tipo == T_DADOS and comecou:
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


def servir(porta, chave, max_sessoes=50, host="0.0.0.0"):
    relay = Relay(chave, max_sessoes)
    srv = ServidorRelay((host, porta), criar_handler(relay))
    srv.relay = relay
    return srv


# ------------------------------------------------------------------ agente (dentro do Baton)

class AgenteRelay(threading.Thread):
    """Mantem a conexao de saida com o relay e repassa cada requisicao ao Baton local (127.0.0.1:porta)."""

    def __init__(self, url, chave, porta, anuncio_fn):
        super().__init__(daemon=True)
        self.url, self.chave, self.porta, self.anuncio_fn = url.rstrip("/"), chave, porta, anuncio_fn
        self.status = "conectando"
        self.reqs, self.conns, self.parar = {}, {}, False

    def run(self):
        espera = 1
        while not self.parar:
            try:
                self._sessao()
                espera = 1
            except Exception as e:
                self.status = f"desconectado ({type(e).__name__})"
            time.sleep(espera)
            espera = min(30, espera * 2)

    def _conectar(self):
        u = urlsplit(self.url.replace("wss://", "https://").replace("ws://", "http://"))
        tls = u.scheme == "https"
        sock = socket.create_connection((u.hostname, u.port or (443 if tls else 80)), timeout=10)
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
        sock.settimeout(None)
        return WS(sock, True, resto)

    def _sessao(self):
        ws = self._conectar()
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
                vivo.wait(1.0)

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
