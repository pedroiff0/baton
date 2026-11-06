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
        self.assertEqual(set(P._Windows.VK), {"Right", "Left", "Home", "End", "b"})
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
