import json
import os
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import baton as P  # noqa: E402
import monitores as MON  # noqa: E402
import relay as RL  # noqa: E402
import residente as R  # noqa: E402

_TMP = tempfile.mkdtemp()
R.PREFS_ARQ = Path(_TMP) / "estado.json"  # os testes nao tocam nas preferencias reais



class FakeBackend:
    def __init__(self):
        self.teclas, self.movs = [], []

    def tecla(self, n):
        self.teclas.append(n)

    def mover(self, x, y):
        self.movs.append((x, y))

    def tela(self):
        return 1920.0, 1080.0


class FakeOverlay:
    def __init__(self):
        self.msgs = []

    def enviar(self, o):
        self.msgs.append(o)


class NotasPptxTest(unittest.TestCase):
    def test_notas_na_ordem_dos_slides(self):
        import zipfile
        R_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
        rel = lambda i, tipo, alvo: f'<Relationship Id="{i}" Type="{R_NS}/{tipo}" Target="{alvo}"/>'
        cab = '<?xml version="1.0"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        nota = lambda t: ('<p:notes xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main" '
                          'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"><p:cSld><p:spTree>'
                          '<p:sp><p:nvSpPr><p:nvPr><p:ph type="sldImg"/></p:nvPr></p:nvSpPr></p:sp>'
                          '<p:sp><p:nvSpPr><p:nvPr><p:ph type="body" idx="1"/></p:nvPr></p:nvSpPr><p:txBody>'
                          f'<a:p><a:r><a:t>{t}</a:t></a:r></a:p></p:txBody></p:sp></p:spTree></p:cSld></p:notes>')
        with tempfile.TemporaryDirectory() as d:
            f = os.path.join(d, "x.pptx")
            with zipfile.ZipFile(f, "w") as z:
                z.writestr("ppt/presentation.xml", '<p:presentation xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main" '
                           f'xmlns:r="{R_NS}"><p:sldIdLst><p:sldId id="256" r:id="rId2"/><p:sldId id="257" r:id="rId1"/></p:sldIdLst></p:presentation>')
                z.writestr("ppt/_rels/presentation.xml.rels", cab + rel("rId1", "slide", "slides/slide1.xml") + rel("rId2", "slide", "slides/slide2.xml") + "</Relationships>")
                z.writestr("ppt/slides/_rels/slide2.xml.rels", cab + rel("rId1", "notesSlide", "../notesSlides/notesSlide2.xml") + "</Relationships>")
                z.writestr("ppt/slides/slide2.xml", "<x/>")
                z.writestr("ppt/slides/slide1.xml", "<x/>")
                z.writestr("ppt/notesSlides/notesSlide2.xml", nota("fala do primeiro slide da ordem"))
            self.assertEqual(P.notas_pptx(f), {"p1": "fala do primeiro slide da ordem"})


class VoltasTest(unittest.TestCase):
    def test_voltas_e_zerar(self):
        e = P.Estado(None, 9999, "slide", "tok")
        e.comando("timer", "lap")
        self.assertEqual(e.voltas, [])  # cronometro parado e zerado: sem volta
        e.comando("timer", "start")
        time.sleep(0.05)
        e.comando("timer", "lap")
        e.comando("timer", "lap")
        self.assertEqual(len(e.snapshot()["timer"]["voltas"]), 2)
        e.comando("timer", "reset")
        self.assertEqual((e.voltas, e.t_rodando, e.t_base), ([], False, 0.0))


class PareamentoTest(unittest.TestCase):
    def estado(self, d):
        P.DISPOSITIVOS_ARQ = Path(d) / "dispositivos.json"
        e = P.Estado(None, 9999, "slide", "tok")
        e.bases = [{"nome": "Wi-Fi", "base": "http://192.168.0.2:8765"}, {"nome": "Tailscale", "base": "http://100.64.0.2:8765"}]
        e.carregar_dispositivos()
        return e

    def test_qr_de_uso_unico_e_token_por_celular(self):
        with tempfile.TemporaryDirectory() as d:
            e = self.estado(d)
            c1 = e.codigo_pareamento()
            a = e.parear(c1, "iPhone")
            self.assertEqual(a["nome"], "Celular 1")
            self.assertIsNone(e.parear(c1, "Android"))  # o mesmo QR nao serve de novo
            c2 = e.codigo_pareamento()
            self.assertNotEqual(c1, c2)
            b = e.parear(c2, "Android")
            self.assertEqual(b["nome"], "Celular 2")
            self.assertNotEqual(a["token"], b["token"])
            self.assertEqual(e.dispositivo_do_token(a["token"])["id"], a["id"])
            self.assertEqual(e.dispositivo_do_token(b["token"])["id"], b["id"])
            self.assertIsNone(e.dispositivo_do_token("qualquer"))
            self.assertIsNone(e.dispositivo_do_token(""))

    def test_revogar_persistir_e_expirar(self):
        with tempfile.TemporaryDirectory() as d:
            e = self.estado(d)
            a = e.parear(e.codigo_pareamento())
            e2 = self.estado(d)  # recarrega do disco
            self.assertEqual(e2.dispositivo_do_token(a["token"])["id"], a["id"])
            self.assertTrue(oct(os.stat(P.DISPOSITIVOS_ARQ).st_mode)[-3:] == "600")
            q = __import__("queue").Queue()
            q.dispositivo = a["id"]
            e2.clientes.append(q)
            self.assertTrue(e2.revogar(a["id"]))
            self.assertIsNone(q.get_nowait())  # fecha o SSE do celular
            self.assertIsNone(e2.dispositivo_do_token(a["token"]))
            self.assertFalse(e2.revogar("naoexiste"))
            c = e2.codigo_pareamento()
            e2.pareamento["expira"] = time.time() - 1
            self.assertIsNone(e2.parear(c))  # expirado
            self.assertNotEqual(e2.codigo_pareamento(), c)

    def test_limite_e_urls(self):
        with tempfile.TemporaryDirectory() as d:
            e = self.estado(d)
            for _ in range(P.MAX_DISPOSITIVOS):
                self.assertIsNotNone(e.parear(e.codigo_pareamento()))
            self.assertIsNone(e.parear(e.codigo_pareamento()))
            self.assertEqual(len(e.dispositivos), P.MAX_DISPOSITIVOS)
            self.assertEqual(e.urls_remoto("T")[1]["url"], "http://100.64.0.2:8765/remote?k=T")
            self.assertTrue(e.urls_pareamento()[0]["url"].startswith("http://192.168.0.2:8765/p/"))
            self.assertEqual(len(e.dispositivos_info()), P.MAX_DISPOSITIVOS)


class RedesTest(unittest.TestCase):
    MAC = ("lo0: flags=8049<UP,LOOPBACK> mtu 16384\n\tinet 127.0.0.1 netmask 0xff000000\n"
           "en0: flags=8863<UP,BROADCAST> mtu 1500\n\tinet 192.168.0.5 netmask 0xffffff00 broadcast 192.168.0.255\n"
           "bridge100: flags=8863<UP> mtu 1500\n\tinet 192.168.2.1 netmask 0xffffff00\n"
           "utun4: flags=8051<UP> mtu 1380\n\tinet 100.101.2.3 --> 100.101.2.3 netmask 0xffffffff\n"
           "bnep0: flags=8863<UP> mtu 1500\n\tinet 10.251.0.2 netmask 0xffffff00\n")
    LINUX = ("1: lo    inet 127.0.0.1/8 scope host lo\n2: wlan0    inet 192.168.0.33/24 brd 192.168.0.255 scope global wlan0\n"
             "3: docker0    inet 172.17.0.1/16 scope global docker0\n4: bnep0    inet 10.251.0.1/24 scope global bnep0\n")

    def lista(self, saida):
        return [(P.rotulo_interface(i, ip), ip) for i, ip in P.parse_interfaces(saida) if P.rotulo_interface(i, ip)]

    def test_mac(self):
        self.assertEqual(self.lista(self.MAC), [("Rede (en0)", "192.168.0.5"), ("Hotspot (bridge100)", "192.168.2.1"),
                                                ("Bluetooth (bnep0)", "10.251.0.2")])

    def test_linux(self):
        self.assertEqual(self.lista(self.LINUX), [("Rede (wlan0)", "192.168.0.33"), ("Bluetooth (bnep0)", "10.251.0.1")])

    def test_windows(self):
        w = ("Adaptador de Rede sem Fio Wi-Fi:\n\n   Endereco IPv4. . . . . . . . . . . . . . : 192.168.1.7\n"
             "Adaptador Ethernet vEthernet (WSL):\n   Endereco IPv4. . . : 172.20.0.1\n")
        self.assertEqual(self.lista(w), [("Rede (Adaptador de Rede sem Fio Wi-Fi)", "192.168.1.7")])


class BluetoothTest(unittest.TestCase):
    HW = ("Hardware Port: Wi-Fi\nDevice: en0\nEthernet Address: aa\n\n"
          "Hardware Port: Bluetooth PAN\nDevice: en6\nEthernet Address: bb\n\n"
          "Hardware Port: iPhone USB\nDevice: en7\nEthernet Address: cc\n")

    def test_portas_mac_e_rotulo(self):
        run = mock.Mock(stdout=self.HW)
        with mock.patch.object(P.sys, "platform", "darwin"), mock.patch.object(P.shutil, "which", return_value="/x"), \
                mock.patch.object(P.subprocess, "run", return_value=run):
            portas = P.portas_mac()
        self.assertEqual(portas, {"en0": "Wi-Fi", "en6": "Bluetooth PAN", "en7": "iPhone USB"})
        self.assertEqual(P.rotulo_interface("en6", "10.0.0.2", portas), "Bluetooth (en6)")
        self.assertEqual(P.rotulo_interface("en7", "172.20.10.2", portas), "Hotspot (en7)")
        self.assertEqual(P.rotulo_interface("en0", "192.168.0.5", portas), "Rede (en0)")

    def test_ordem_das_bases(self):
        ips = [("Rede (en0)", "192.168.0.5"), ("Hotspot (en7)", "172.20.10.2"), ("Bluetooth (en6)", "10.0.0.2")]
        with mock.patch.object(P, "ips_locais", return_value=ips), mock.patch.object(P, "ip_lan", return_value="192.168.0.5"), \
                mock.patch.object(P, "ip_tailscale_cache", return_value="100.64.0.1"):
            b = P.calcular_bases(8765)
        self.assertEqual([x["base"].split("//")[1].split(":")[0] for x in b],
                         ["10.0.0.2", "172.20.10.2", "192.168.0.5", "100.64.0.1"])
        self.assertIn("Bluetooth", b[0]["nome"])
        self.assertIn("Tailscale", b[-1]["nome"])

    def test_rede_lenta_pelo_ip_local(self):
        ips = [("Rede (en0)", "192.168.0.5"), ("Bluetooth (en6)", "10.0.0.2")]
        with mock.patch.object(P, "ips_locais", return_value=ips):
            self.assertTrue(P.rede_lenta_do_ip("10.0.0.2"))
            self.assertFalse(P.rede_lenta_do_ip("192.168.0.5"))

    def test_faixa_http(self):
        self.assertIsNone(P.faixa_http(None, 100))
        self.assertEqual(P.faixa_http("bytes=0-9", 100), (0, 9))
        self.assertEqual(P.faixa_http("bytes=90-", 100), (90, 99))
        self.assertEqual(P.faixa_http("bytes=50-500", 100), (50, 99))
        self.assertEqual(P.faixa_http("bytes=-10", 100), (90, 99))
        self.assertEqual(P.faixa_http("bytes=100-110", 100), "invalida")

    def test_publicar_poupa_celular(self):
        q_tela, q_cel = __import__("queue").Queue(), __import__("queue").Queue()
        q_tela.dispositivo, q_cel.dispositivo = None, "d1"
        e = P.Estado(None, 9999, "slide", "tok")
        e.clientes += [q_tela, q_cel]
        e.publicar({"laser": {"x": 1}})
        e.publicar({"hb": 1})
        self.assertEqual(q_tela.qsize(), 2)
        self.assertEqual(q_cel.qsize(), 1)

    def test_estado_painel_traz_bluetooth(self):
        e = P.Estado(None, 9999, "slide", "tok")
        with mock.patch.object(P, "ips_locais", return_value=[("Bluetooth (bnep0)", "10.251.0.1")]):
            self.assertEqual(e.painel_estado()["rede_bluetooth"], {"interface": "Bluetooth (bnep0)", "ip": "10.251.0.1"})


class AssinaturaTest(unittest.TestCase):
    def test_requisito_fixo_e_fallback(self):
        ok = mock.Mock(returncode=0, stderr="")
        with mock.patch.object(P.shutil, "which", return_value="/usr/bin/codesign"), mock.patch.object(P.subprocess, "run", return_value=ok) as run:
            self.assertTrue(P.assinar_app("/x/Baton.app"))
        cmd = run.call_args_list[0][0][0]
        self.assertIn("-r", cmd)
        self.assertEqual(cmd[cmd.index("-r") + 1], '=designated => identifier "com.baton.app"')
        falha = mock.Mock(returncode=1, stderr="erro")
        with mock.patch.object(P.shutil, "which", return_value="/usr/bin/codesign"), mock.patch.object(P.subprocess, "run", return_value=falha) as run:
            self.assertFalse(P.assinar_app("/x/Baton.app"))
            self.assertEqual(run.call_count, 2)  # tenta de novo sem o requisito
        with mock.patch.object(P.shutil, "which", return_value=None):
            self.assertFalse(P.assinar_app("/x/Baton.app"))


class TelaPropriaTest(unittest.TestCase):
    def test_apresentar_desliga_teclas_externas_e_libera_laser(self):
        e = P.Estado("x.pdf", 5, "slide", "tok", externo=mock.Mock())
        e.prefs["ativo"], e.autorizado = True, False
        self.assertFalse(e.permitido())
        self.assertIsNotNone(e.ext)
        proc = mock.Mock()
        proc.poll.return_value = None
        proc.wait.side_effect = lambda: time.sleep(5)
        tela = __import__("queue").Queue()
        tela.dispositivo = None  # pagina de tela cheia do proprio computador conectada
        with mock.patch.object(P, "abrir_tela_cheia", return_value=proc) as ab:
            self.assertTrue(e.apresentar())
        self.assertTrue(e.tela_propria)
        self.assertFalse(e.permitido())  # janela ainda nao conectou: teclas continuam indo ao app
        self.assertIsNotNone(e.ext)
        e.clientes.append(tela)
        self.assertTrue(ab.call_args[0][0].endswith("/apresentar"))
        self.assertTrue(e.permitido())
        self.assertIsNone(e.ext)  # a tecla nao vai tambem para o navegador
        e.fechar_tela()
        proc.terminate.assert_called()
        self.assertFalse(e.tela_propria)
        self.assertIsNotNone(e.ext)

    def test_toque_em_proximo_autoriza(self):
        e = P.Estado(None, 9999, "slide", "tok")
        e.prefs.update(ativo=True, modo="perguntar", apps_nao=["Blocos"])
        e.autorizado, e.pedido = False, {"app": "PowerPoint"}
        self.assertTrue(e.autorizar_por_toque())
        self.assertTrue(e.permitido())
        self.assertIsNone(e.pedido)
        e.autorizado, e.pedido = False, {"app": "Blocos"}
        self.assertFalse(e.autorizar_por_toque())  # app bloqueado pelo usuario
        e.pedido, e.prefs["modo"] = None, "nunca"
        self.assertFalse(e.autorizar_por_toque())

    def test_captura_de_teste(self):
        with tempfile.NamedTemporaryFile(suffix=".jpg") as f:
            f.write(b"\xff\xd8jpg")
            f.flush()
            with mock.patch.dict(os.environ, {"BATON_CAPTURA": f.name}):
                self.assertEqual(P.capturar_tela(), b"\xff\xd8jpg")

    def test_sem_pdf_nao_apresenta(self):
        self.assertFalse(P.Estado(None, 9999, "slide", "tok").apresentar())

    def test_navegador_ausente_usa_padrao(self):
        with mock.patch.object(P, "navegadores_chromium", return_value=[]), mock.patch.object(P.webbrowser, "open") as w:
            self.assertIsNone(P.abrir_tela_cheia("http://localhost:8765/apresentar"))
        self.assertIn("fs=1", w.call_args[0][0])


class WaylandTest(unittest.TestCase):
    def test_teclas_do_externo_tem_keysym(self):
        for tecla in list(P.Externo.TECLAS.values()) + ["Home", "Right", "b"]:
            self.assertIn(tecla, P.KEYSYMS)

    def test_sessao_wayland(self):
        with mock.patch.dict(os.environ, {"XDG_SESSION_TYPE": "wayland"}, clear=False):
            self.assertTrue(P.sessao_wayland())
        with mock.patch.dict(os.environ, {"XDG_SESSION_TYPE": "x11"}, clear=True):
            self.assertFalse(P.sessao_wayland())

    def test_externo_usa_portal_no_wayland_e_cai_para_xdotool(self):
        with mock.patch.object(P.sys, "platform", "linux"), mock.patch.object(P, "sessao_wayland", return_value=True), \
                mock.patch.dict(os.environ, {"BATON_OVERLAY": "log"}), mock.patch.object(P, "_Portal") as portal:
            self.assertIs(P.criar_externo().b, portal.return_value)
        with mock.patch.object(P.sys, "platform", "linux"), mock.patch.object(P, "sessao_wayland", return_value=True), \
                mock.patch.dict(os.environ, {"BATON_OVERLAY": "log"}), mock.patch.object(P, "_Portal", side_effect=ImportError("gi")), \
                mock.patch.object(P.shutil, "which", return_value="/usr/bin/xdotool"):
            self.assertIsInstance(P.criar_externo().b, P._Xdotool)


class ProxyTest(unittest.TestCase):
    def test_bases_com_url_publica(self):
        ips = [("Bluetooth (en6)", "10.0.0.2"), ("Rede (en0)", "192.168.0.5")]
        with mock.patch.object(P, "ips_locais", return_value=ips), mock.patch.object(P, "ip_lan", return_value="192.168.0.5"), \
                mock.patch.object(P, "ip_tailscale_cache", return_value=None), mock.patch.object(P, "URL_PUBLICA", "https://baton.exemplo.com/"):
            b = P.calcular_bases(8765)
        self.assertEqual(b[0]["base"], "http://10.0.0.2:8765")
        self.assertEqual(b[1], {"nome": "Internet (HTTPS)", "base": "https://baton.exemplo.com"})


class PublicaVivaTest(unittest.TestCase):
    def test_url_publica_so_primeiro_se_viva(self):
        ips = [("Rede (en0)", "10.64.3.102")]
        with mock.patch.object(P, "ips_locais", return_value=ips), mock.patch.object(P, "ip_lan", return_value="10.64.3.102"), \
                mock.patch.object(P, "ip_tailscale_cache", return_value=None), mock.patch.object(P, "URL_PUBLICA", "https://baton.exemplo.com"):
            with mock.patch.object(P, "publica_viva", return_value=True):
                self.assertEqual(P.calcular_bases(8765)[0]["base"], "https://baton.exemplo.com")
            with mock.patch.object(P, "publica_viva", return_value=False):
                b = P.calcular_bases(8765)
                self.assertEqual(b[0]["base"], "http://10.64.3.102:8765")
                self.assertIn("indisponivel", b[-1]["nome"])

    def test_relay_desconectado_derruba_url_publica(self):
        class Ag:
            status = "desconectado (ConnectionError)"
        with mock.patch.object(P, "URL_PUBLICA", "https://baton.exemplo.com"), mock.patch.object(P, "RELAY_AGENTE", Ag()):
            self.assertFalse(P.publica_viva())
            P.RELAY_AGENTE.status = "conectado"
            self.assertTrue(P.publica_viva())


class SimplesTest(unittest.TestCase):
    def test_toque_define_posicao_absoluta(self):
        self.assertIn("posPad", P.PAGINA_SIMPLES)
        self.assertIn("getBoundingClientRect", P.PAGINA_SIMPLES)
        self.assertNotIn("ptoque", P.PAGINA_SIMPLES)

    def test_pagina_simples(self):
        self.assertIn("__TOKEN__", P.PAGINA_SIMPLES)
        self.assertIn("deviceorientation", P.PAGINA_SIMPLES)
        self.assertNotIn("__HTTPS__\"; ", "")  # placeholder existe uma vez, entre aspas
        self.assertEqual(P.PAGINA_SIMPLES.count("__HTTPS__"), 1)

    def test_tls_sem_openssl(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.object(P.Path, "home", return_value=Path(d)), \
                mock.patch.object(P.shutil, "which", return_value=None):
            self.assertIsNone(P.contexto_tls())

    def test_toque_no_laser_autoriza(self):
        e = P.Estado(None, 9999, "slide", "tok")
        e.prefs.update(ativo=True, modo="perguntar")
        e.autorizado = False
        self.assertTrue(e.autorizar_por_toque())
        self.assertTrue(e.permitido())


class BandejaTest(unittest.TestCase):
    def test_rotulo_por_estado(self):
        import tray_gtk as T
        self.assertEqual(T.rotulo(None)[0], "Baton: sem resposta")
        self.assertIn("pendente", T.rotulo({"ativo": True, "acessibilidade": False})[0])
        self.assertEqual(T.rotulo({"ativo": False})[0], "Baton: pausado")
        self.assertIn("Keynote", T.rotulo({"ativo": True, "pedido": {"app": "Keynote"}})[0])
        self.assertEqual(T.rotulo({"ativo": True, "autorizado": True})[0], "Baton: controlando")
        self.assertEqual(T.rotulo({"ativo": True, "autorizado": False})[0], "Baton: em espera")

    def test_iniciar_tray_por_plataforma(self):
        with mock.patch.object(R.sys, "platform", "darwin"):
            self.assertIsNone(R.iniciar_tray(8765))  # no Mac a bandeja e o Baton.app
        with mock.patch.object(R.sys, "platform", "linux"), mock.patch.object(R.subprocess, "Popen") as po:
            p = mock.Mock()
            p.wait.side_effect = R.subprocess.TimeoutExpired("x", 1.5)
            po.return_value = p
            self.assertIs(R.iniciar_tray(8765), p)
            self.assertIn("--porta", po.call_args[0][0])
        with mock.patch.object(R.sys, "platform", "linux"), mock.patch.object(R.subprocess, "Popen") as po:
            p = mock.Mock()
            p.wait.return_value = 3  # Gtk indisponivel: encerra logo
            p.stderr.read.return_value = "sem Gtk"
            po.return_value = p
            self.assertIsNone(R.iniciar_tray(8765))

    def test_cliente_contra_o_servidor(self):
        import tray_gtk as T
        with tempfile.TemporaryDirectory() as d:
            P.NOTAS_ARQ = Path(d) / "notas.json"
            P.CACHE = Path(d) / "pdfjs"
            e = P.Estado(None, 9999, "slide", "tok")
            srv = P.Servidor(("127.0.0.1", 0), P.criar_handler(e, False, False))
            porta = srv.server_address[1]
            threading.Thread(target=srv.serve_forever, daemon=True).start()
            try:
                est = T.estado(porta)
                self.assertTrue(est["ativo"])
                self.assertEqual(est["conexoes"], 0)
                T.acao(porta, "pausar")
                self.assertFalse(T.estado(porta)["ativo"])
                T.acao(porta, "retomar")
                self.assertTrue(T.estado(porta)["ativo"])
                pdf = Path(d) / "x.pdf"
                pdf.write_bytes(DeckTest.PDF)
                ok, msg = T.enviar_deck(porta, str(pdf))
                self.assertTrue(ok, msg)
                self.assertIn("3 paginas", msg)
                self.assertEqual(T.estado(porta)["deck"]["paginas"], 3)
                ok, msg = T.enviar_deck(porta, str(Path(d) / "x.exe"))
                self.assertFalse(ok)
            finally:
                srv.shutdown()
            self.assertIsNone(T.estado(porta, timeout=0.5))


class DeckTest(unittest.TestCase):
    PDF = (b"%PDF-1.4\n1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n2 0 obj<</Type/Pages/Count 3/Kids[3 0 R]>>endobj\n"
           b"3 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 960 540]>>endobj\ntrailer<</Root 1 0 R>>\n%%EOF")

    def test_trocar_e_remover_deck(self):
        with tempfile.TemporaryDirectory() as d:
            P.NOTAS_ARQ = Path(d) / "notas.json"
            P.CACHE = Path(d) / "pdfjs"
            pdf, notas = P.preparar_deck(self.PDF, "meu deck.pdf")
            self.assertTrue(pdf.endswith(".pdf") and notas == {})
            e = P.Estado(None, 9999, "slide", "tok")
            vistos = []
            q = __import__("queue").Queue()
            e.clientes.append(q)
            e.trocar_deck(pdf, {"p1": "nota"})
            self.assertEqual((e.total, e.pagina, e.notas), (3, 1, {"p1": "nota"}))
            self.assertAlmostEqual(e.proporcao, 16 / 9)
            self.assertEqual(e.deck_info()["paginas"], 3)
            while not q.empty():
                vistos.append(q.get())
            self.assertIn({"deck": 1}, vistos)
            e.remover_deck()
            self.assertIsNone(e.deck_info())
            self.assertEqual(e.notas, {})

    def test_formato_invalido_e_md(self):
        with tempfile.TemporaryDirectory() as d:
            P.CACHE = Path(d) / "pdfjs"
            with self.assertRaises(RuntimeError):
                P.preparar_deck(b"x", "virus.exe")
            pdf, notas = P.preparar_deck("## 2\nsegunda".encode(), "notas.md")
            self.assertIsNone(pdf)
            self.assertEqual(notas, {"2": "segunda"})

    def test_sem_libreoffice_vira_erro_tratado(self):
        with tempfile.TemporaryDirectory() as d:
            P.CACHE = Path(d) / "pdfjs"
            with mock.patch("shutil.which", return_value=None), mock.patch("os.path.exists", return_value=False):
                with self.assertRaises(RuntimeError):
                    P.preparar_deck(b"PK", "a.pptx")


class NotasSessaoTest(unittest.TestCase):
    def test_salvar_persistir_e_mesclar_com_o_arquivo(self):
        with tempfile.TemporaryDirectory() as d:
            P.NOTAS_ARQ = Path(d) / "notas.json"
            e = P.Estado(None, 9999, "slide", "tok")
            e.carregar_notas_sessao({"p1": "do arquivo", "p2": "outra"})
            self.assertIsNone(e.salvar_nota("abc", "x"))
            self.assertEqual(e.salvar_nota("1", "digitada")["p1"], "digitada")
            self.assertEqual(e.notas["p2"], "outra")
            e2 = P.Estado(None, 9999, "slide", "tok")
            e2.carregar_notas_sessao({"p1": "do arquivo", "p2": "outra"})
            self.assertEqual(e2.notas, {"p1": "digitada", "p2": "outra"})
            e2.salvar_nota("p2", "")
            self.assertNotIn("p2", e2.notas)
            n = e2.salvar_todas("## 3\nsó esta\n\n## p4\nquarta")
            self.assertEqual(n, {"3": "só esta", "p4": "quarta"})
            e3 = P.Estado(None, 9999, "slide", "tok")
            e3.carregar_notas_sessao({"p1": "do arquivo"})
            self.assertEqual(e3.notas, {"3": "só esta", "p4": "quarta"})


class NotasTest(unittest.TestCase):
    def test_ler_notas(self):
        with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False, encoding="utf-8") as f:
            f.write("# t\n\n## 1\nabertura\n- a\n\n## p5\npagina cinco\n## 7\nsete\n")
        n = P.ler_notas(f.name)
        os.unlink(f.name)
        self.assertEqual(set(n), {"1", "p5", "7"})
        self.assertIn("abertura", n["1"])
        self.assertEqual(n["7"], "sete")


class EstadoTest(unittest.TestCase):
    def novo(self, **kw):
        return P.Estado("x.pdf", 10, "slide", "tok", **kw)

    def test_navegacao_e_limites(self):
        e = self.novo()
        e.comando("prev")
        self.assertEqual(e.pagina, 1)
        e.comando("goto", 99)
        self.assertEqual(e.pagina, 10)
        e.comando("next")
        self.assertEqual(e.pagina, 10)

    def test_comando_repetido_nao_avanca(self):
        e = self.novo()
        e.comando("next", cid="a")
        e.comando("next", cid="a")
        self.assertEqual(e.pagina, 2)

    def test_zoom_limitado_e_resetado_ao_trocar_de_slide(self):
        e = self.novo()
        e.comando("zoom", {"z": 99, "x": 2, "y": -1})
        self.assertEqual((e.zoom, e.zx, e.zy), (5.0, 1.0, 0.0))
        e.comando("next")
        self.assertEqual(e.zoom, 1.0)

    def test_comando_invalido(self):
        self.assertFalse(self.novo().comando("xyz"))

    def test_tracos_acumulam_e_limpam(self):
        e = self.novo()
        e.desenhar({"id": "a", "t": "pen", "c": "#fff", "p": [[0.1, 0.1], [0.2, 0.2]]})
        e.desenhar({"id": "a", "t": "pen", "c": "#fff", "p": [[0.3, 0.3]]})
        self.assertEqual(len(e.tracos), 1)
        self.assertEqual(len(e.tracos[0]["p"]), 3)
        e.comando("limpar")
        self.assertEqual(e.tracos, [])


class ExternoTest(unittest.TestCase):
    def test_teclas(self):
        b = FakeBackend()
        x = P.Externo(b)
        x.comando("next")
        x.comando("prev")
        self.assertEqual(b.teclas, ["Right", "Left"])

    def test_mapeamento_com_barras(self):
        x = P.Externo(FakeBackend())
        # slide 4:3 numa tela 16:9: barras laterais; o centro continua no centro
        px, py, nx, ny = x.para_tela(0.5, 0.5, 4 / 3)
        self.assertAlmostEqual(nx, 0.5)
        self.assertAlmostEqual(ny, 0.5)
        _, _, nx0, _ = x.para_tela(0.0, 0.5, 4 / 3)
        self.assertGreater(nx0, 0.1)

    def test_overlay_recebe_laser_e_desenho_sem_cursor(self):
        b, o = FakeBackend(), FakeOverlay()
        x = P.Externo(b, o, cursor=False)
        x.laser(0.5, 0.5, True, 16 / 9, "foco")
        x.desenho({"id": "a", "t": "hl", "c": "#ff0"}, [[0.1, 0.1]], 16 / 9)
        x.limpar()
        self.assertEqual([m["t"] for m in o.msgs], ["laser", "draw", "clear"])
        self.assertEqual(b.movs, [])
        self.assertTrue(o.msgs[1]["hl"])


class RaioTest(unittest.TestCase):
    def test_raio_do_foco_limitado_e_repassado(self):
        o = FakeOverlay()
        e = P.Estado(None, 9999, "slide", "tok", P.Externo(FakeBackend(), o, cursor=False))
        e.laser(0.5, 0.5, True, "foco", 9)
        time.sleep(0.02)  # o laser limita a taxa de envio (12 ms)
        e.laser(0.5, 0.5, True, "foco", 0.01)
        self.assertEqual([m["r"] for m in o.msgs], [0.35, 0.03])


class TimerEIrParaTest(unittest.TestCase):
    def test_cronometro_comeca_no_primeiro_avanco_e_pausa(self):
        e = P.Estado("x.pdf", 10, "slide", "tok")
        self.assertFalse(e.snapshot()["timer"]["rodando"])
        e.comando("next")
        self.assertTrue(e.snapshot()["timer"]["rodando"])
        e.comando("timer", "pause")
        self.assertFalse(e.snapshot()["timer"]["rodando"])
        e.comando("timer", "reset")
        self.assertEqual(e.snapshot()["timer"]["elapsed"], 0)

    def test_ir_para_no_modo_externo_volta_ao_inicio_e_avanca(self):
        b = FakeBackend()
        e = P.Estado(None, 9999, "slide", "tok", P.Externo(b, None))
        e.comando("goto", 4)
        time.sleep(0.3)
        self.assertEqual(b.teclas, ["Home", "Right", "Right", "Right"])
        self.assertEqual(e.pagina, 4)


class CfgTest(unittest.TestCase):
    def test_validacao(self):
        c = P.cfg_validar({"laser_tam": 999, "foco_blur": -5, "laser_cor": "#0a84ff", "x": 1})
        self.assertEqual(c["laser_tam"], 120)
        self.assertEqual(c["foco_blur"], 0)
        self.assertEqual(c["laser_cor"], "#0a84ff")
        self.assertNotIn("x", c)
        self.assertEqual(P.cfg_validar({"laser_cor": "azul"})["laser_cor"], P.CFG_PADRAO["laser_cor"])

    def test_set_cfg_repassa_para_a_camada(self):
        o = FakeOverlay()
        e = P.Estado(None, 9999, "slide", "tok", P.Externo(FakeBackend(), o, cursor=False))
        antigo = P.CFG_ARQ
        with tempfile.TemporaryDirectory() as d:
            P.CFG_ARQ = Path(d) / "config.json"
            try:
                e.set_cfg({"foco_escuro": 60})
                self.assertTrue(P.CFG_ARQ.exists())
            finally:
                P.CFG_ARQ = antigo
        self.assertEqual(o.msgs[-1]["t"], "cfg")
        self.assertEqual(o.msgs[-1]["foco_escuro"], 60)


class MiscTest(unittest.TestCase):
    def test_icone_png(self):
        self.assertEqual(P.icone_png(32)[:8], b"\x89PNG\r\n\x1a\n")

    def test_token_estavel(self):
        with tempfile.TemporaryDirectory() as d:
            antigo = os.environ.get("HOME")
            os.environ["HOME"] = d
            try:
                t1 = P.token_estavel()
                self.assertEqual(t1, P.token_estavel())
                self.assertNotEqual(t1, P.token_estavel(novo=True))
            finally:
                os.environ["HOME"] = antigo

    def test_pagina_remota_tem_notas_e_ferramentas(self):
        e = P.Estado(None, 9999, "slide", "tok", P.Externo(FakeBackend()), "http://x/remote?k=tok")
        html = P.montar("remoto", e, False, False)
        for trecho in ("Notas do apresentador", 'id="bf"', "EXTERNO=true", "OVERLAY=false"):
            self.assertIn(trecho, html)


class CortinaESomTest(unittest.TestCase):
    def estado(self, overlay=True):
        b, o = FakeBackend(), FakeOverlay()
        e = P.Estado(None, 9999, "slide", "tok", P.Externo(b, o if overlay else None, cursor=False))
        e.som = P.Som("log")
        return e, b, o

    def test_preto_usa_cortina_e_nao_a_tecla(self):
        e, b, o = self.estado()
        self.assertTrue(e.comando("blank"))
        self.assertEqual(b.teclas, [])
        self.assertEqual(o.msgs[-1]["t"], "cortina")
        self.assertEqual(o.msgs[-1]["k"], "preto")
        self.assertTrue(e.snapshot()["preto"])
        e.comando("blank")
        self.assertEqual(o.msgs[-1]["k"], "off")
        self.assertFalse(e.snapshot()["preto"])

    def test_preto_sem_camada_cai_na_tecla_b_uma_vez_por_troca(self):
        e, b, _ = self.estado(overlay=False)
        e.comando("blank")
        e.comando("blank")
        self.assertEqual(b.teclas, ["b", "b"])
        e.comando("cortina", "branco")
        self.assertEqual(b.teclas, ["b", "b"])

    def test_cortinas_alternam_e_validam(self):
        e, _, o = self.estado()
        for k in ("branco", "teatro"):
            self.assertTrue(e.comando("cortina", k))
            self.assertEqual(e.snapshot()["cortina"], k)
        e.comando("cortina", "teatro")
        self.assertEqual(e.snapshot()["cortina"], "")
        self.assertFalse(e.comando("cortina", "xyz"))
        e.comando("cortina", "branco")
        e.comando("cortina", "off")
        self.assertEqual(o.msgs[-1]["k"], "off")

    def test_fim_guarda_o_texto_e_manda_para_a_camada(self):
        e, _, o = self.estado()
        e.comando("cortina", {"k": "fim", "txt": "Obrigado!", "sub": "Perguntas?"})
        self.assertEqual(o.msgs[-1], {"t": "cortina", "k": "fim", "txt": "Obrigado!", "sub": "Perguntas?"})
        self.assertEqual(e.snapshot()["fim"], {"txt": "Obrigado!", "sub": "Perguntas?"})

    def test_confete_limita_a_duracao(self):
        e, _, o = self.estado()
        e.comando("confete", 99)
        self.assertEqual(o.msgs[-1], {"t": "confete", "seg": 10.0})

    def test_efeito_valida_o_nome(self):
        e, b, _ = self.estado()
        self.assertTrue(e.comando("efeito", "ding"))
        self.assertTrue(e.comando("efeito", "stop"))
        self.assertFalse(e.comando("efeito", "../../etc"))
        self.assertEqual(b.teclas, [])

    def test_efeitos_sao_wav_validos(self):
        import io
        import wave
        for nome in P.EFEITOS:
            with wave.open(io.BytesIO(P.wav_bytes(nome)), "rb") as w:
                self.assertEqual(w.getnchannels(), 1)
                self.assertGreater(w.getnframes(), P.TAXA_SOM // 2, nome)


class PlataformasTest(unittest.TestCase):
    def test_icones_para_android(self):
        import struct
        for tam in (180, 192, 512):
            png = P.icone_png(tam)
            self.assertEqual(png[:8], b"\x89PNG\r\n\x1a\n")
            self.assertEqual(struct.unpack(">II", png[16:24]), (tam, tam))

    def test_autostart_no_linux(self):
        from unittest import mock
        with tempfile.TemporaryDirectory() as d, mock.patch.object(P.sys, "platform", "linux"), \
                mock.patch.object(P.Path, "home", return_value=Path(d)):
            P.instalar_login()
            alvo = Path(d) / ".config" / "autostart" / "baton.desktop"
            self.assertIn("--residente", alvo.read_text())
            P.instalar_login(remover=True)
            self.assertFalse(alvo.exists())

    def test_backend_windows_mapeia_as_teclas(self):
        self.assertTrue({"Right", "Left", "Home", "End", "b", "equal", "minus", "Escape"} <= set(P._Windows.VK))
        self.assertTrue(set(P.Externo.TECLAS.values()) <= set(P._Windows.VK))
        self.assertTrue(set(P.Externo.TECLAS.values()) <= set(P._Mac.CODES))

    def test_overlays_python_existem_e_compilam(self):
        import py_compile
        for nome in ("overlay_gtk.py", "overlay_tk.py"):
            py_compile.compile(str(Path(P.__file__).with_name(nome)), doraise=True)


class ResidenteTest(unittest.TestCase):
    def setUp(self):
        R.PREFS_ARQ.unlink(missing_ok=True)

    def tearDown(self):
        R.PREFS_ARQ.unlink(missing_ok=True)

    def estado(self, residente=True):
        b, o = FakeBackend(), FakeOverlay()
        e = P.Estado(None, 9999, "slide", "tok", P.Externo(b, o, cursor=False), "http://x/remote?k=tok")
        e.som = P.Som("log")
        e.residente, e.autorizado = residente, not residente
        return e, b

    def test_em_espera_nao_envia_tecla(self):
        e, b = self.estado()
        self.assertFalse(e.comando("next"))
        e.laser(0.5, 0.5, True)
        self.assertEqual(b.teclas, [])
        self.assertFalse(e.snapshot()["auth"])

    def test_apresentacao_gera_pedido_e_sim_autoriza(self):
        e, b = self.estado()
        e.remotos = 1
        e.presentacao("POWERPNT.EXE")
        self.assertEqual(e.snapshot()["pedido"], {"app": "POWERPNT.EXE"})
        self.assertTrue(e.consentir("sim"))
        self.assertTrue(e.comando("next"))
        self.assertEqual(b.teclas, ["Right"])
        e.presentacao(None)  # tela cheia acabou: volta a espera
        self.assertFalse(e.snapshot()["auth"])

    def test_sempre_lembra_o_app_e_autoriza_sozinho(self):
        e, _ = self.estado()
        e.remotos = 1
        e.presentacao("Keynote")
        e.consentir("sempre")
        e.presentacao(None)
        e.presentacao("Keynote")
        self.assertTrue(e.snapshot()["auth"])
        self.assertIn("Keynote", R.prefs_carregar()["apps_ok"])

    def test_nao_e_nunca(self):
        e, _ = self.estado()
        e.remotos = 1
        e.presentacao("Acrobat")
        e.consentir("nao")
        e.presentacao("Acrobat")  # mesma tela cheia: nao pergunta de novo
        self.assertIsNone(e.snapshot()["pedido"])
        e.presentacao(None)
        e.presentacao("Acrobat")
        self.assertIsNotNone(e.snapshot()["pedido"])
        e.consentir("nunca")
        e.presentacao(None)
        e.presentacao("Acrobat")
        self.assertIsNone(e.snapshot()["pedido"])
        self.assertIn("Acrobat", R.prefs_carregar()["apps_nao"])

    def test_ignorados_e_modos(self):
        e, _ = self.estado()
        e.remotos = 1
        e.presentacao("VLC")
        self.assertIsNone(e.snapshot()["pedido"])
        e.prefs["modo"] = "sempre"
        e.presentacao("Impress")
        self.assertTrue(e.snapshot()["auth"])
        e2, _ = self.estado()
        e2.prefs["modo"] = "nunca"
        e2.presentacao("Impress")
        self.assertFalse(e2.snapshot()["auth"])

    def test_pausar_bloqueia_ate_retomar(self):
        e, b = self.estado(residente=False)
        self.assertTrue(e.comando("next"))
        e.painel("pausar")
        self.assertFalse(e.comando("next"))
        self.assertFalse(e.snapshot()["ativo"])
        e.painel("retomar")
        self.assertTrue(e.comando("prev"))
        self.assertTrue(e.painel("modo", "sempre"))
        self.assertFalse(e.painel("modo", "xyz"))
        self.assertFalse(e.painel("inexistente"))

    def test_vigia_exige_duas_leituras_iguais(self):
        e, _ = self.estado()
        e.remotos = 1
        leituras = iter(["App", "App", None, None])
        v = R.Vigia(e, detector=lambda: next(leituras))
        v.passo()
        self.assertIsNone(e.snapshot()["pedido"])
        v.passo()
        self.assertEqual(e.snapshot()["pedido"], {"app": "App"})
        v.passo()
        self.assertIsNotNone(e.snapshot()["pedido"])
        v.passo()
        self.assertIsNone(e.snapshot()["pedido"])

    def test_vigia_pausado_nao_detecta(self):
        e, _ = self.estado()
        e.prefs["ativo"] = False
        R.prefs_salvar(e.prefs)
        v = R.Vigia(e, detector=lambda: "App")
        v.passo()
        v.passo()
        self.assertIsNone(e.snapshot()["pedido"])

    def test_parsers_de_deteccao(self):
        self.assertEqual(R.parse_osascript("Keynote|0|0|1920|1080\n", (1920, 1080)), "Keynote")
        self.assertIsNone(R.parse_osascript("Safari|0|38|1920|1042", (1920, 1080)))
        self.assertIsNone(R.parse_osascript("Finder|-", (1920, 1080)))
        cheia = '_NET_WM_STATE(ATOM) = _NET_WM_STATE_FULLSCREEN\nWM_CLASS(STRING) = "soffice", "libreoffice-impress"\n'
        self.assertEqual(R.parse_xprop(cheia), "libreoffice-impress")
        self.assertIsNone(R.parse_xprop('_NET_WM_STATE(ATOM) = \nWM_CLASS(STRING) = "a", "b"\n'))
        self.assertTrue(R.ignorado("VLC media player"))
        self.assertFalse(R.ignorado("POWERPNT.EXE"))

    def test_prefs_persistem_e_validam(self):
        R.PREFS_ARQ.write_text('{"ativo": false, "modo": "xyz", "apps_ok": ["A"]}')
        p = R.prefs_carregar()
        self.assertFalse(p["ativo"])
        self.assertEqual((p["modo"], p["apps_ok"]), ("perguntar", ["A"]))


class IpTest(unittest.TestCase):
    def test_ignora_saida_de_erro_da_cli(self):
        from unittest import mock
        erro = mock.Mock(stdout="The Tailscale CLI failed to start: Failed to load preferences.\n")
        ok = mock.Mock(stdout="inet 100.101.2.3 netmask 0xffffffff\n")
        with mock.patch.object(P.shutil, "which", side_effect=lambda x: "/bin/" + x), \
                mock.patch.object(P.os.path, "exists", return_value=True), \
                mock.patch.object(P.subprocess, "run", side_effect=[erro, erro, erro, ok]):
            self.assertEqual(P.ip_tailscale(), "100.101.2.3")
        self.assertEqual(P._ip_valido("The"), None)
        self.assertEqual(P._ip_valido("100.1.2.3"), "100.1.2.3")


class LaunchdTest(unittest.TestCase):
    def test_plist_escapa_e_usa_o_launcher(self):
        x = P.plist_launchd(["/Users/a b/Applications/Baton.app/Contents/MacOS/Baton", "/usr/bin/python3", "/x/baton.py", "--residente"], "/l&g.log")
        self.assertIn("<string>/Users/a b/Applications/Baton.app/Contents/MacOS/Baton</string>", x)
        self.assertIn("<string>--residente</string>", x)
        self.assertIn("/l&amp;g.log", x)
        import plistlib
        self.assertEqual(plistlib.loads(x.encode())["ProgramArguments"][-1], "--residente")
        self.assertEqual(plistlib.loads(P.INFO_PLIST_APP.encode())["CFBundleName"], "Baton")


if __name__ == "__main__":
    unittest.main()


class RelayTest(unittest.TestCase):
    def _par(self):
        import socket as so
        a, b = so.socketpair()
        return RL.WS(a, True), RL.WS(b, False)

    def test_ws_mascarado_e_fragmentado(self):
        c, s = self._par()
        c.enviar(1, "ola")
        self.assertEqual(s.receber(), (1, "ola"))
        grande = os.urandom(RL.PEDACO)
        threading.Thread(target=lambda: c.enviar(2, grande), daemon=True).start()
        self.assertEqual(s.receber(), (2, grande))
        # fragmentado: texto "ab" + "cd"; ping no meio e respondido
        c.s.sendall(bytes([0x01, 0x82]) + b"\0\0\0\0" + b"ab")
        c.s.sendall(bytes([0x89, 0x80]) + b"\0\0\0\0")
        c.s.sendall(bytes([0x80, 0x82]) + b"\0\0\0\0" + b"cd")
        self.assertEqual(s.receber(), (1, "abcd"))

    def test_roteamento_por_credencial(self):
        r = RL.Relay("k" * 12)
        a1, a2 = RL.Agente(None, "x"), RL.Agente(None, "y")
        a1.codigos, a1.tokens = {"cod1"}, {RL.hash_token("tok1")}
        a2.codigos, a2.tokens = {"cod2"}, {RL.hash_token("tok2")}
        r.agentes = {a1.sid: a1, a2.sid: a2}
        self.assertIs(r.achar(codigo="cod2"), a2)
        self.assertIs(r.achar(token="tok1"), a1)
        self.assertIsNone(r.achar(token="outro"))
        self.assertIs(r.achar(sid=a2.sid), a2)
        self.assertIsNone(r.achar(estatico=True))  # estatico sem credencial: nunca "qualquer agente"
        a1.vivo = a2.vivo = False
        self.assertIsNone(r.achar(sid=a2.sid))

    def test_relay_json_do_pacote(self):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "relay.json").write_text(json.dumps({"url": "wss://x.exemplo.com", "chave": "c" * 12}))
            with mock.patch.object(P, "ARQ_RELAY", Path(d) / "nao-existe.json"), mock.patch.object(P, "__file__", str(Path(d) / "baton.py")):
                self.assertEqual(P.relay_salvo(), ("wss://x.exemplo.com", "c" * 12))

    def test_convite_uso_unico_e_revogacao(self):
        with tempfile.TemporaryDirectory() as d:
            reg = RL.Registro(Path(d) / "r.json")
            cod = reg.criar_convite("Mac do instituto")
            self.assertIsNone(reg.resgatar("AAAA-BBBB-CCCC"))
            r = reg.resgatar(cod.lower())  # maiusculas/hifens nao importam
            self.assertIsNotNone(r)
            self.assertIsNone(reg.resgatar(cod))  # uso unico
            cid, chave = r
            self.assertEqual(reg.autenticar(chave)["id"], cid)
            self.assertNotIn(chave, (Path(d) / "r.json").read_text())  # so o hash vai para o disco
            self.assertNotIn(cod.replace("-", ""), (Path(d) / "r.json").read_text())
            self.assertEqual(oct((Path(d) / "r.json").stat().st_mode & 0o777), "0o600")
            self.assertTrue(reg.revogar(cid))
            self.assertIsNone(reg.autenticar(chave))
            reg.criar_convite("x", validade=-1)  # expirado
            self.assertIsNone(reg.resgatar(reg.d["convites"][-1]["h"]))

    def test_relay_com_convite_ponta_a_ponta(self):
        import http.client
        with tempfile.TemporaryDirectory() as d:
            srv = RL.servir(0, "admin-chave-de-teste-1", host="127.0.0.1", registro=RL.Registro(Path(d) / "r.json"), publico="https://x.exemplo.com")
            porta = srv.server_address[1]
            threading.Thread(target=srv.serve_forever, daemon=True).start()

            def req(metodo, caminho, corpo=None, cab=None):
                c = http.client.HTTPConnection("127.0.0.1", porta, timeout=5)
                c.request(metodo, caminho, body=json.dumps(corpo) if corpo is not None else None, headers=cab or {})
                r = c.getresponse()
                return r.status, r.read()
            self.assertEqual(req("GET", "/relay/api/estado")[0], 403)  # sem chave de administrador
            st, corpo = req("POST", "/relay/api/convite", {"rotulo": "teste"}, {"X-Baton-Admin": "admin-chave-de-teste-1"})
            self.assertEqual(st, 200)
            cod = json.loads(corpo)["codigo"]
            self.assertEqual(req("POST", "/relay/convite", {"codigo": "ERRADO-ERRADO"})[0], 403)
            url, chave = RL.resgatar_convite(f"127.0.0.1:{porta}/{cod}")
            self.assertTrue(url.startswith("ws://127.0.0.1"))
            with self.assertRaises(ValueError):
                RL.resgatar_convite(f"127.0.0.1:{porta}/{cod}")  # ja usado
            ag = RL.AgenteRelay(url, chave, 1, lambda: (["c1"], []))
            ag.start()
            for _ in range(50):
                if srv.relay.agentes:
                    break
                time.sleep(0.1)
            st, corpo = req("GET", "/relay/api/estado", cab={"X-Baton-Admin": "admin-chave-de-teste-1"})
            est = json.loads(corpo)
            self.assertTrue(est["computadores"][0]["online"])
            self.assertEqual(est["computadores"][0]["rotulo"], "teste")
            self.assertNotIn("ip", corpo.decode().lower().replace("rotulo", ""))  # nada de IP no painel
            cid = est["computadores"][0]["id"]
            req("POST", "/relay/api/revogar", {"id": cid}, {"X-Baton-Admin": "admin-chave-de-teste-1"})
            for _ in range(30):
                if not srv.relay.agentes:
                    break
                time.sleep(0.1)
            self.assertFalse(srv.relay.agentes)  # revogado: desconectado na hora
            ag.parar = True
            srv.shutdown()

    def test_registro_aberto_com_limites(self):
        import http.client
        with tempfile.TemporaryDirectory() as d:
            srv = RL.servir(0, "admin-chave-de-teste-1", max_sessoes=10, host="127.0.0.1", registro=RL.Registro(Path(d) / "r.json"))
            porta = srv.server_address[1]
            threading.Thread(target=srv.serve_forever, daemon=True).start()
            ags = []
            for _ in range(RL.Relay.MAX_ABERTOS_POR_IP):  # sem chave: entra (registro aberto)
                a = RL.AgenteRelay(f"ws://127.0.0.1:{porta}", "", 1, lambda: ([], []))
                a._ws = a._conectar()
                ags.append(a)
            with self.assertRaises(ConnectionError):  # 4o do mesmo IP: recusado
                RL.AgenteRelay(f"ws://127.0.0.1:{porta}", "", 1, lambda: ([], []))._conectar()
            for a in ags:
                a._ws.fechar()
            time.sleep(0.3)
            srv.relay.registro.definir_aberto(False)
            with self.assertRaises(ConnectionError):  # fechado: so com convite
                RL.AgenteRelay(f"ws://127.0.0.1:{porta}", "", 1, lambda: ([], []))._conectar()
            srv.shutdown()

    def test_limite_de_sessoes(self):
        r = RL.Relay("k" * 12, max_sessoes=1)
        self.assertTrue(r.registrar(RL.Agente(None, "a")))
        self.assertFalse(r.registrar(RL.Agente(None, "b")))

    def test_relay_ponta_a_ponta_e_chave(self):
        import http.client
        srv = RL.servir(0, "chave-de-teste-123456", host="127.0.0.1")
        porta = srv.server_address[1]
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        # chave errada: recusada
        ag = RL.AgenteRelay(f"ws://127.0.0.1:{porta}", "errada", 1, lambda: ([], []))
        with self.assertRaises(ConnectionError):
            ag._conectar()
        # baton local de mentira
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

        class L(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                corpo = ("xff=" + str(self.headers.get("X-Forwarded-For"))).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(corpo)))
                self.send_header("Set-Cookie", "a=1")
                self.end_headers()
                self.wfile.write(corpo)

        loc = ThreadingHTTPServer(("127.0.0.1", 0), L)
        threading.Thread(target=loc.serve_forever, daemon=True).start()
        ag = RL.AgenteRelay(f"ws://127.0.0.1:{porta}", "chave-de-teste-123456", loc.server_address[1], lambda: (["abc"], [RL.hash_token("t1")]))
        ag.start()
        for _ in range(50):
            if srv.relay.agentes and next(iter(srv.relay.agentes.values())).codigos:
                break
            time.sleep(0.1)
        c = http.client.HTTPConnection("127.0.0.1", porta, timeout=5)
        c.request("GET", "/x?k=t1", headers={"X-Forwarded-For": "6.6.6.6", "Cf-Connecting-Ip": "9.9.9.9"})
        r = c.getresponse()
        self.assertEqual(r.status, 200)
        self.assertEqual(r.read(), b"xff=9.9.9.9")  # o relay impoe o IP real; XFF do cliente e descartado
        self.assertTrue(any(k == "Set-Cookie" and v.startswith("baton_s=") for k, v in r.getheaders()))
        c = http.client.HTTPConnection("127.0.0.1", porta, timeout=5)
        c.request("GET", "/x?k=desconhecido")
        self.assertEqual(c.getresponse().status, 404)
        ag.parar = True
        srv.shutdown()
        loc.shutdown()


class OfflineTest(unittest.TestCase):
    def test_leitor_de_pdf_e_qr_vem_no_repositorio(self):
        raiz = Path(P.__file__).resolve().parent / "vendor"
        for arq in ("pdfjs/pdf.min.js", "pdfjs/pdf.worker.min.js", "qrcode.min.js", "LICENSES.md"):
            self.assertGreater((raiz / arq).stat().st_size, 200, arq)
        self.assertEqual(P.pasta_pdfjs(), raiz / "pdfjs")  # clonou, funciona sem internet

    def test_pagina_segue_conectada_sem_pdfjs(self):
        self.assertIn("window.PDFJS_FALHA=true", P.PAGINA_BASE)
        self.assertIn("conectar();  // o SSE nao depende do leitor de PDF", P.JS_TELA)


class ZoomSistemaTest(unittest.TestCase):
    def test_tabela_por_sistema(self):
        with mock.patch.object(P.sys, "platform", "darwin"):
            c = P.zoom_sistema_cfg()
            self.assertEqual((c["mods"], c["mais"], c["menos"]), (("alt", "cmd"), "equal", "minus"))
        with mock.patch.object(P.sys, "platform", "win32"):
            self.assertEqual(P.zoom_sistema_cfg()["mods"], ("win",))
        with mock.patch.object(P.sys, "platform", "linux"):
            with mock.patch.dict(P.os.environ, {"XDG_CURRENT_DESKTOP": "ubuntu:GNOME"}):
                self.assertEqual(P.zoom_sistema_cfg()["mods"], ("super", "alt"))
            with mock.patch.dict(P.os.environ, {"XDG_CURRENT_DESKTOP": "KDE"}):
                self.assertEqual(P.zoom_sistema_cfg()["mods"], ("super",))
            with mock.patch.dict(P.os.environ, {"XDG_CURRENT_DESKTOP": "XFCE"}):
                self.assertIsNone(P.zoom_sistema_cfg())

    def test_passos(self):
        self.assertEqual([P.passos_zoom(z) for z in (1, 1.5, 2, 3, 4, 5, 9)], [0, 1, 2, 4, 6, 8, 8])

    def test_zoom_move_cursor_e_envia_atalhos(self):
        class B(FakeBackend):
            def __init__(self):
                super().__init__()
                self.combos = []

            def combo(self, mods, tecla):
                self.combos.append((tuple(mods), tecla))
        b = B()
        e = P.Externo(b, None, True)
        with mock.patch.object(P.sys, "platform", "darwin"):
            e._zoom_aplicar(3, 0.5, 0.5, 16 / 9)  # 1x -> 3x: 4 toques de ampliar, cursor no centro
            self.assertEqual(b.combos, [(("alt", "cmd"), "equal")] * 4)
            self.assertEqual(b.movs[-1], (960.0, 540.0))
            b.combos.clear()
            e._zoom_aplicar(2, 0.5, 0.5, 16 / 9)  # 3x -> 2x: 2 de reduzir
            self.assertEqual(b.combos, [(("alt", "cmd"), "minus")] * 2)
            b.combos.clear()
            e._zoom_aplicar(1, 0.5, 0.5, 16 / 9)  # volta a 1x: reduz tudo (+2 de folga)
            self.assertEqual(len(b.combos), 4)
            self.assertEqual(e.zoom_passos, 0)

    def test_laser_nao_chama_monitor_no_caminho_quente(self):
        e = P.Estado(None, 9999, "slide", "tok")
        e._mon_cache = (0.0, {"id": 0, "x": 0, "y": 0, "w": 1920, "h": 1080})
        with mock.patch.object(P.monitores, "escolher", side_effect=AssertionError("recalculou no caminho do laser")):
            for _ in range(3):
                self.assertEqual(e.monitor()["w"], 1920)

    def test_laser_so_publica_com_tela_propria(self):
        e = P.Estado(None, 9999, "slide", "tok")
        e.prefs.update(ativo=True)
        e.autorizado = True
        pubs = []
        e.publicar = lambda m: pubs.append(m)
        e.laser(0.5, 0.5, True)
        self.assertEqual(pubs, [])  # so celulares: nada a enfileirar


class MonitoresTest(unittest.TestCase):
    XR = """Monitors: 2
 0: +*DP-1 1920/530x1080/300+0+455  DP-1
 1: +HDMI-1 1080/530x1920/300+1920+0  HDMI-1
"""

    def test_parse_xrandr(self):
        m = MON.parse_xrandr(self.XR)
        self.assertEqual([(x["nome"], x["x"], x["y"], x["w"], x["h"], x["principal"]) for x in m],
                         [("DP-1", 0, 455, 1920, 1080, True), ("HDMI-1", 1920, 0, 1080, 1920, False)])

    def test_parse_powershell(self):
        dados = [{"DeviceName": "\\\\.\\DISPLAY1", "Primary": True, "Bounds": {"X": 0, "Y": 0, "Width": 1920, "Height": 1080}},
                 {"DeviceName": "\\\\.\\DISPLAY2", "Primary": False, "Bounds": {"X": 1920, "Y": -200, "Width": 1280, "Height": 720}}]
        m = MON.parse_powershell(json.dumps(dados))
        self.assertEqual((m[1]["x"], m[1]["y"], m[1]["w"], m[1]["h"], m[0]["principal"]), (1920, -200, 1280, 720, True))

    def test_parse_jxa_inverte_y(self):
        m = MON.parse_jxa('[{"nome":"A","x":0,"y":0,"w":1440,"h":900,"principal":true},{"nome":"B","x":1440,"y":0,"w":1920,"h":1080}]')
        self.assertEqual((m[1]["x"], m[1]["y"]), (1440, -180))  # monitor mais alto que o principal fica acima (y negativo)

    def test_escolher(self):
        mons = [dict(m, id=i) for i, m in enumerate(MON.parse_xrandr(self.XR))]
        self.assertEqual(MON.escolher("2", mons)["nome"], "HDMI-1")
        self.assertEqual(MON.escolher("auto", mons, janela=(0, 455, 1920, 1080))["nome"], "DP-1")  # tela cheia no principal
        self.assertEqual(MON.escolher("auto", mons, janela=(1920, 0, 1080, 1920))["nome"], "HDMI-1")
        self.assertEqual(MON.escolher("auto", mons, janela=(100, 500, 800, 600))["nome"], "HDMI-1")  # sem tela cheia: o secundario
        self.assertEqual(MON.escolher("auto", mons, janela=(100, 500, 800, 600), anterior=mons[0])["nome"], "DP-1")  # mantem o ultimo
        self.assertEqual(MON.escolher("9", mons, janela=(100, 500, 800, 600))["nome"], "HDMI-1")  # numero invalido cai no auto

    def test_zoom_captura_uma_vez_e_segue_o_ponto(self):
        import tempfile
        o = FakeOverlay()
        e = P.Externo(FakeBackend(), o, False)
        e.monitor_fn = lambda: {"id": 0, "x": 0, "y": 0, "w": 1000, "h": 500}
        with tempfile.NamedTemporaryFile(suffix=".png") as f:
            f.write(b"\x89PNG fake")
            f.flush()
            with mock.patch.dict(os.environ, {"BATON_CAPTURA_PNG": f.name}):
                self.assertTrue(e.zoom_externo(2.0, 0.25, 0.5, 16 / 9))
                for _ in range(100):
                    if any(m.get("t") == "lupa" for m in o.msgs):
                        break
                    time.sleep(0.02)
                e.zoom_externo(3.0, 0.5, 0.5, 16 / 9)
        lupas = [m for m in o.msgs if m.get("t") == "lupa"]
        self.assertTrue(lupas[0]["on"] and lupas[0]["img"].endswith(".png"))
        self.assertEqual((lupas[-1]["x"], lupas[-1]["z"]), (0.5, 3.0))
        self.assertEqual(len({m["img"] for m in lupas}), 1)  # a captura e uma so
        self.assertEqual(lupas[0]["w"], 1.0)  # cobre a tela inteira
        e.zoom_externo(1.0, 0.5, 0.5, 16 / 9)
        self.assertEqual(o.msgs[-1], {"t": "lupa", "on": False})

    def test_zoom_externo_sem_camada_usa_o_zoom_do_sistema(self):
        e = P.Externo(FakeBackend(), None, True)
        with mock.patch.object(e, "zoom_sistema", return_value=True) as zs:
            e.zoom_externo(2.0, 0.5, 0.5, 16 / 9)
        zs.assert_called_once()

    def test_cantos_alcancaveis_em_monitor_retrato(self):
        mon = {"id": 1, "nome": "HDMI", "x": 1920, "y": 0, "w": 1080, "h": 1920, "principal": False}
        e = P.Externo(FakeBackend(), None, True)
        e.monitor_fn = lambda: mon
        self.assertEqual(e.para_tela(0.0, 0.0, 16 / 9)[:2], (1920, 0))  # proporcao do slide nao cria barras: canto exato
        self.assertEqual(e.para_tela(1.0, 1.0, 16 / 9)[2:], (1.0, 1.0))

    def test_aspecto_segue_o_monitor_da_projecao(self):
        est = P.Estado(None, 9999, "fade", "t", P.Externo(FakeBackend()), "", 16 / 9)
        est._mon_cache = (0, {"id": 1, "x": 0, "y": 0, "w": 1080, "h": 1920})
        self.assertAlmostEqual(est.aspecto(), 1080 / 1920, places=3)
        self.assertAlmostEqual(est.snapshot()["aspecto"], 1080 / 1920, places=3)

    def test_laser_preto_valido_e_com_halo(self):
        self.assertEqual(P.cfg_validar({"laser_cor": "#000000"})["laser_cor"], "#000000")
        for arq in ("overlay_gtk.py", "overlay_tk.py", "overlay.swift"):
            self.assertIn("escuro", (Path(__file__).resolve().parent.parent / arq).read_text(encoding="utf-8"), arq)

    def test_para_tela_no_monitor_deslocado(self):
        mons = [{"id": 1, "nome": "HDMI", "x": 1920, "y": 0, "w": 1280, "h": 720, "principal": False}]
        e = P.Externo(FakeBackend(), None, True)
        e.monitor_fn = lambda: mons[0]
        px, py, nx, ny = e.para_tela(0.5, 0.5, 16 / 9)
        self.assertEqual((px, py, nx, ny), (1920 + 640, 360, 0.5, 0.5))
        px, py, nx, ny = e.para_tela(1.0, 1.0, 16 / 9)
        self.assertEqual((px, py), (1920 + 1280, 720))
