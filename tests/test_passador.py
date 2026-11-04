import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import passador as P  # noqa: E402


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


if __name__ == "__main__":
    unittest.main()
