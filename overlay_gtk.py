#!/usr/bin/env python3
"""overlay_gtk.py: camada transparente por cima de qualquer app (Linux, X11).

Desenha laser, foco (escurece tudo menos um circulo), caneta, marca-texto, cortina e confete.
Mesmo protocolo do overlay.swift: JSON, um por linha, na entrada padrao (enviados pelo baton.py).
Precisa de PyGObject com Gtk 3 e pycairo (Debian/Ubuntu: python3-gi, gir1.2-gtk-3.0, python3-gi-cairo)
e de um compositor ativo (GNOME, KDE e XFCE com composicao ligada). Em Wayland roda via XWayland.
Nao ha desfoque no Linux: o foco so escurece.
"""
import json
import math
import os
import random
import sys
import threading

os.environ.setdefault("GDK_BACKEND", "x11")
try:
    import cairo
    import gi
    gi.require_version("Gtk", "3.0")
    gi.require_version("Gdk", "3.0")
    from gi.repository import Gdk, GLib, Gtk
except Exception as e:  # sem Gtk: o Baton cai para a camada Tk
    print(f"overlay_gtk: Gtk/cairo indisponivel ({e})", file=sys.stderr)
    sys.exit(3)


def cor_hex(s, padrao=(1.0, 0.18, 0.18)):
    s = (s or "").lstrip("#")
    try:
        v = int(s, 16)
        return ((v >> 16 & 255) / 255.0, (v >> 8 & 255) / 255.0, (v & 255) / 255.0) if len(s) == 6 else padrao
    except ValueError:
        return padrao


PALETA = [(1.0, 0.23, 0.19), (1.0, 0.84, 0.04), (0.19, 0.82, 0.35), (0.04, 0.52, 1.0), (0.75, 0.35, 0.95), (1.0, 0.62, 0.04)]


class Camada(Gtk.Window):
    def __init__(self, indice):
        super().__init__(type=Gtk.WindowType.POPUP)  # sem decoracao, sem foco, acima de janelas em tela cheia
        tela = Gdk.Screen.get_default()
        if not os.environ.get("BATON_OVERLAY_PNG") and (not tela.is_composited() or tela.get_rgba_visual() is None):
            print("overlay_gtk: sem compositor (janela transparente indisponivel)", file=sys.stderr)
            sys.exit(4)
        if tela.get_rgba_visual() is not None:
            self.set_visual(tela.get_rgba_visual())
        self.set_app_paintable(True)
        disp = Gdk.Display.get_default()
        mon = disp.get_monitor(indice) if 0 <= indice < disp.get_n_monitors() else disp.get_primary_monitor()
        g = mon.get_geometry()
        self.w, self.h = g.width, g.height
        self.move(g.x, g.y)
        self.set_default_size(self.w, self.h)
        self.set_accept_focus(False)
        self.set_keep_above(True)
        self.connect("draw", self.desenhar)
        self.realize()
        self.get_window().input_shape_combine_region(cairo.Region(), 0, 0)  # cliques passam para o app
        self.show_all()
        self.get_window().raise_()
        # estado
        self.tracos, self.ordem = {}, []
        self.laser_on, self.foco_on, self.pt, self.raio = False, False, (0.5, 0.5), 0.12
        self.aj = {"laser_tam": 24, "laser_cor": (1.0, 0.18, 0.18), "foco_escuro": 25, "pen_larg": 3.5,
                   "hl_larg": 14, "hl_alpha": 38}
        self.cort, self.cort_vis, self.prog = "", "", 0.0
        self.txt, self.sub = "", ""
        self.parts, self.emitindo, self.fim_emit = [], False, 0.0
        self.anim = False
        GLib.timeout_add(500, self._topo)

    def _topo(self):
        self.get_window().raise_()
        return True

    # --------------------------------------------------------------- desenho
    def desenhar(self, _w, cr):
        cr.set_operator(cairo.OPERATOR_CLEAR)
        cr.paint()
        cr.set_operator(cairo.OPERATOR_OVER)
        w, h = self.w, self.h
        if self.foco_on and self.aj["foco_escuro"] > 0.5:
            cr.push_group()
            cr.set_source_rgba(0, 0, 0, self.aj["foco_escuro"] / 100.0)
            cr.paint()
            cx, cy, r1 = self.pt[0] * w, self.pt[1] * h, w * self.raio
            gr = cairo.RadialGradient(cx, cy, 0, cx, cy, r1)
            gr.add_color_stop_rgba(0.0, 0, 0, 0, 1)
            gr.add_color_stop_rgba(0.65, 0, 0, 0, 1)
            gr.add_color_stop_rgba(1.0, 0, 0, 0, 0)
            cr.set_operator(cairo.OPERATOR_DEST_OUT)
            cr.set_source(gr)
            cr.paint()
            cr.set_operator(cairo.OPERATOR_OVER)
            cr.pop_group_to_source()
            cr.paint()
        for id_ in self.ordem:
            t = self.tracos[id_]
            if not t["p"]:
                continue
            cr.set_line_cap(cairo.LINE_CAP_ROUND)
            cr.set_line_join(cairo.LINE_JOIN_ROUND)
            cr.set_line_width(max(8.0, w * self.aj["hl_larg"] / 1000.0) if t["hl"] else max(2.0, w * self.aj["pen_larg"] / 1000.0))
            r, g, b = t["c"]
            cr.set_source_rgba(r, g, b, self.aj["hl_alpha"] / 100.0 if t["hl"] else 1.0)
            cr.move_to(t["p"][0][0] * w, t["p"][0][1] * h)
            if len(t["p"]) == 1:
                cr.line_to(t["p"][0][0] * w + 0.1, t["p"][0][1] * h)
            for x, y in t["p"][1:]:
                cr.line_to(x * w, y * h)
            cr.stroke()
        if self.laser_on:
            cx, cy, r = self.pt[0] * w, self.pt[1] * h, self.aj["laser_tam"]
            gr = cairo.RadialGradient(cx, cy, 0, cx, cy, r)
            lr, lg, lb = self.aj["laser_cor"]
            gr.add_color_stop_rgba(0.0, 1, 1, 1, 1)
            gr.add_color_stop_rgba(0.35, lr, lg, lb, 1)
            gr.add_color_stop_rgba(1.0, lr, lg, lb, 0)
            cr.set_source(gr)
            cr.arc(cx, cy, r, 0, 2 * math.pi)
            cr.fill()
        if self.prog > 0.001:
            self.desenhar_cortina(cr)
        for p in self.parts:
            cr.save()
            cr.translate(p["x"], p["y"])
            cr.rotate(p["r"])
            cr.set_source_rgb(*p["c"])
            cr.rectangle(-p["w"] / 2, -p["h"] / 2, p["w"], p["h"])
            cr.fill()
            cr.restore()
        return False

    def desenhar_cortina(self, cr):
        w, h, k, a = self.w, self.h, self.cort_vis, self.prog
        if k == "teatro":
            e = a * a * (3 - 2 * a)
            larg = w / 2 + 2
            for lado in (0, 1):
                x0 = (-larg + e * larg) if lado == 0 else (w - e * larg)
                gr = cairo.LinearGradient(x0, 0, x0 + larg, 0)
                n = 14
                for i in range(n + 1):
                    c = (0.42, 0.04, 0.08) if i % 2 == 0 else (0.72, 0.12, 0.16)
                    gr.add_color_stop_rgb(i / n, *c)
                cr.set_source(gr)
                cr.rectangle(x0, 0, larg, h)
                cr.fill()
            return
        if k == "branco":
            cr.set_source_rgba(1, 1, 1, a)
        elif k == "fim":
            gr = cairo.RadialGradient(w / 2, h * 0.4, 0, w / 2, h * 0.4, max(w, h) * 0.7)
            gr.add_color_stop_rgba(0, 0.11, 0.16, 0.32, a)
            gr.add_color_stop_rgba(1, 0.02, 0.03, 0.06, a)
            cr.set_source(gr)
        else:
            cr.set_source_rgba(0, 0, 0, a)
        cr.paint()
        if k == "fim" and a > 0.2:
            cr.select_font_face("Sans", cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_BOLD)
            for texto, tam, y, cor in ((self.txt, h * 0.11, h * 0.5, (1, 1, 1)), (self.sub, h * 0.04, h * 0.5 + h * 0.08, (0.8, 0.84, 0.93))):
                if not texto:
                    continue
                cr.set_font_size(tam)
                ext = cr.text_extents(texto)
                cr.set_source_rgba(*cor, a)
                cr.move_to((w - ext.width) / 2 - ext.x_bearing, y)
                cr.show_text(texto)

    # --------------------------------------------------------------- animacao
    def _ligar_anim(self):
        if not self.anim:
            self.anim = True
            GLib.timeout_add(16, self._passo)

    def _passo(self):
        dt = 1 / 60.0
        alvo = 1.0 if self.cort else 0.0
        vel = (1 / 1.3) if self.cort_vis == "teatro" else (1 / 0.35)
        if self.prog < alvo:
            self.prog = min(alvo, self.prog + vel * dt)
        elif self.prog > alvo:
            self.prog = max(alvo, self.prog - vel * dt)
        import time
        if self.emitindo and time.monotonic() < self.fim_emit:
            for _ in range(5):
                self.parts.append({"x": random.uniform(0, self.w), "y": -20.0, "vx": random.uniform(-120, 120),
                                   "vy": random.uniform(80, 200), "r": random.uniform(0, 6.28), "vr": random.uniform(-9, 9),
                                   "w": random.uniform(8, 16), "h": random.uniform(5, 11), "c": random.choice(PALETA)})
        else:
            self.emitindo = False
        for p in self.parts:
            p["vy"] = min(p["vy"] + 260 * dt, 340)
            p["x"] += (p["vx"] + math.sin(p["y"] / 45) * 40) * dt
            p["y"] += p["vy"] * dt
            p["r"] += p["vr"] * dt
        self.parts = [p for p in self.parts if p["y"] < self.h + 30]
        self.queue_draw()
        ocupado = self.prog != alvo or self.parts or self.emitindo
        if not ocupado:
            self.anim = False
        return bool(ocupado)

    # --------------------------------------------------------------- mensagens
    def tratar(self, m):
        import time
        t = m.get("t")
        if t == "laser":
            self.pt = (float(m.get("x", 0.5)), float(m.get("y", 0.5)))
            ligado = bool(m.get("on"))
            self.raio = min(0.35, max(0.03, float(m.get("r", 0.12))))
            if m.get("m") == "foco":
                self.laser_on, self.foco_on = False, ligado
            else:
                self.laser_on, self.foco_on = ligado, False
            self.queue_draw()
        elif t == "draw":
            id_ = str(m.get("id", "?"))
            pts = [(float(a), float(b)) for a, b in m.get("p", [])]
            if id_ in self.tracos:
                self.tracos[id_]["p"].extend(pts)
            else:
                self.tracos[id_] = {"hl": bool(m.get("hl")), "c": cor_hex(m.get("c"), (1.0, 0.23, 0.19)), "p": pts}
                self.ordem.append(id_)
            self.queue_draw()
        elif t == "cfg":
            a = self.aj
            for k, lo, hi in (("laser_tam", 6, 120), ("foco_escuro", 0, 90), ("pen_larg", 0.5, 20), ("hl_larg", 3, 60), ("hl_alpha", 5, 90)):
                if isinstance(m.get(k), (int, float)):
                    a[k] = min(hi, max(lo, float(m[k])))
            if isinstance(m.get("laser_cor"), str):
                a["laser_cor"] = cor_hex(m["laser_cor"])
            self.queue_draw()
        elif t == "clear":
            self.tracos.clear()
            self.ordem.clear()
            self.queue_draw()
        elif t == "cortina":
            k = m.get("k", "off")
            if k not in ("preto", "branco", "teatro", "fim", "off"):
                return
            self.cort = "" if k == "off" else k
            if self.cort:
                if self.cort_vis == "teatro" and k != "teatro":
                    self.prog = 0.0
                self.cort_vis = k
                self.txt, self.sub = str(m.get("txt", "")), str(m.get("sub", ""))
                if k == "fim":
                    self.emitindo, self.fim_emit = True, time.monotonic() + 6
            self._ligar_anim()
        elif t == "confete":
            self.emitindo, self.fim_emit = True, time.monotonic() + min(10.0, max(1.0, float(m.get("seg", 4))))
            self._ligar_anim()
        elif t == "quit":
            Gtk.main_quit()


def salvar_png(camada):
    """Teste sem tela real: BATON_OVERLAY_PNG=arquivo grava o quadro final e encerra."""
    destino = os.environ.get("BATON_OVERLAY_PNG")
    if not destino:
        Gtk.main_quit()  # pai fechou a entrada: encerra
        return

    def gravar():
        sup = cairo.ImageSurface(cairo.FORMAT_ARGB32, camada.w, camada.h)
        camada.desenhar(None, cairo.Context(sup))
        sup.write_to_png(destino)
        Gtk.main_quit()
        return False
    GLib.timeout_add(int(float(os.environ.get("BATON_OVERLAY_ESPERA", "0")) * 1000), gravar)


def main():
    indice = 0
    if "--tela" in sys.argv:
        try:
            indice = int(sys.argv[sys.argv.index("--tela") + 1])
        except (IndexError, ValueError):
            pass
    camada = Camada(indice)

    def ler():
        for linha in sys.stdin:
            try:
                msg = json.loads(linha)
            except ValueError:
                continue
            GLib.idle_add(camada.tratar, msg)
        GLib.idle_add(salvar_png, camada)

    threading.Thread(target=ler, daemon=True).start()
    Gtk.main()


if __name__ == "__main__":
    main()
