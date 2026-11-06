#!/usr/bin/env python3
"""overlay_tk.py: camada transparente por cima de qualquer app (Windows; Linux como alternativa sem Gtk).

Desenha laser, foco (escurece tudo menos um circulo), caneta, marca-texto, cortina e confete.
Mesmo protocolo do overlay.swift: JSON, um por linha, na entrada padrao (enviados pelo baton.py).
So biblioteca padrao (tkinter). No Windows usa cor transparente + clique atravessando a janela.
Limites do Tk: nao ha desfoque (o foco so escurece), a borda do foco e o marca-texto usam padroes
pontilhados (stipple) no lugar de transparencia real, e a cortina "branco/preto/fim" aparece com fade
pela opacidade da janela. Em Linux sem compositor/Gtk o Tk nao tem transparencia: use overlay_gtk.py.
"""
import json
import math
import os
import queue
import random
import sys
import threading
import time
import tkinter as tk

WIN = sys.platform == "win32"
CHAVE = "#010203"  # cor que vira transparente
TESTE = os.environ.get("BATON_TK_TESTE", "")  # draw | dim | cort: mostra so uma janela, com fundo opaco
PALETA = ["#ff3b30", "#ffd60a", "#30d158", "#0a84ff", "#bf5af2", "#ff9f0a"]


def atravessar_cliques(janela):
    if not WIN:
        return
    import ctypes
    u = ctypes.windll.user32
    hwnd = u.GetParent(janela.winfo_id()) or janela.winfo_id()
    estilo = u.GetWindowLongW(hwnd, -20)  # GWL_EXSTYLE
    # LAYERED | TRANSPARENT | TOOLWINDOW | NOACTIVATE
    u.SetWindowLongW(hwnd, -20, estilo | 0x80000 | 0x20 | 0x80 | 0x08000000)


class Janela:
    def __init__(self, raiz, w, h, fundo=CHAVE, alpha=1.0, x=0, y=0):
        self.t = tk.Toplevel(raiz)
        self.t.overrideredirect(True)
        self.t.geometry(f"{w}x{h}{x:+d}{y:+d}")
        self.t.attributes("-topmost", True)
        self.c = tk.Canvas(self.t, width=w, height=h, bg="#335577" if TESTE else fundo, highlightthickness=0, bd=0)
        self.c.pack()
        if WIN:
            try:
                self.t.attributes("-transparentcolor", CHAVE)
            except tk.TclError:
                pass
        self.alpha = None
        self.definir_alpha(alpha)
        self.t.update_idletasks()
        atravessar_cliques(self.t)

    def definir_alpha(self, a):
        a = max(0.0, min(1.0, a))
        if a != self.alpha:
            self.alpha = a
            try:
                self.t.attributes("-alpha", a)
            except tk.TclError:
                pass

    def topo(self):
        self.t.attributes("-topmost", True)
        self.t.lift()


def cor_hex(s, padrao="#ff3b30"):
    s = (s or "")
    return s if (len(s) == 7 and s[0] == "#") else padrao


class App:
    def __init__(self, raiz, geom=None):
        self.raiz = raiz
        gx, gy, self.w, self.h = geom if geom else (0, 0, raiz.winfo_screenwidth(), raiz.winfo_screenheight())
        self.dim = Janela(raiz, self.w, self.h, fundo="black", alpha=0.0, x=gx, y=gy)
        self.desenho = Janela(raiz, self.w, self.h, x=gx, y=gy)
        self.cort = Janela(raiz, self.w, self.h, alpha=0.0, x=gx, y=gy)
        if TESTE:
            for nome, j in (("dim", self.dim), ("draw", self.desenho), ("cort", self.cort)):
                if nome != TESTE:
                    j.t.withdraw()
                else:
                    j.definir_alpha(1.0)
        self.aj = {"laser_tam": 24, "laser_cor": "#ff2d2d", "foco_escuro": 25, "pen_larg": 3.5, "hl_larg": 14, "hl_alpha": 38}
        self.tracos = {}
        self.foco_on = False
        self.cort_tipo, self.cort_vis, self.prog = "", "", 0.0
        self.txt, self.sub = "", ""
        self.parts, self.emitindo, self.fim_emit = [], False, 0.0
        self.fila = queue.Queue()
        raiz.after(8, self.poll)
        raiz.after(33, self.passo)
        raiz.after(500, self.manter_topo)

    def manter_topo(self):
        for j in (self.dim, self.desenho, self.cort):
            j.topo()
        self.raiz.after(500, self.manter_topo)

    # ---------------------------------------------------------------- laser e foco
    def laser(self, m):
        w, h = self.w, self.h
        x, y = float(m.get("x", 0.5)) * w, float(m.get("y", 0.5)) * h
        ligado = bool(m.get("on"))
        raio = min(0.35, max(0.03, float(m.get("r", 0.12))))
        c = self.desenho.c
        c.delete("laser")
        self.dim.c.delete("furo")
        foco = m.get("m") == "foco"
        self.foco_on = ligado and foco
        self.dim.definir_alpha(self.aj["foco_escuro"] / 100.0 if self.foco_on else 0.0)
        if not ligado:
            return
        if foco:
            r1 = w * raio
            for f, st in ((1.0, "gray12"), (0.93, "gray25"), (0.86, "gray50"), (0.79, "gray75"), (0.72, "")):
                r = r1 * f
                opts = {"stipple": st} if st else {}
                self.dim.c.create_oval(x - r, y - r, x + r, y + r, fill=CHAVE, outline="", tags="furo", **opts)
        else:
            r, cor = self.aj["laser_tam"], self.aj["laser_cor"]
            rgb = [int(cor[i:i + 2], 16) for i in (1, 3, 5)]
            escuro = 0.299 * rgb[0] + 0.587 * rgb[1] + 0.114 * rgb[2] < 76
            if escuro:  # cor escura (preto): disco branco por baixo para aparecer em fundo escuro
                c.create_oval(x - r * .9, y - r * .9, x + r * .9, y + r * .9, fill="white", outline="", stipple="gray50", tags="laser")
                c.create_oval(x - r * .65, y - r * .65, x + r * .65, y + r * .65, fill="white", outline="", tags="laser")
            else:
                c.create_oval(x - r, y - r, x + r, y + r, fill=cor, outline="", stipple="gray50", tags="laser")
            c.create_oval(x - r * .55, y - r * .55, x + r * .55, y + r * .55, fill=cor, outline="", tags="laser")
            c.create_oval(x - r * .2, y - r * .2, x + r * .2, y + r * .2, fill=("#555555" if escuro else "white"), outline="", tags="laser")

    # ---------------------------------------------------------------- caneta e marca-texto
    def desenhar(self, m):
        id_ = str(m.get("id", "?"))
        novos = [(float(a) * self.w, float(b) * self.h) for a, b in m.get("p", [])]
        if not novos:
            return
        t = self.tracos.get(id_)
        if t is None:
            t = self.tracos[id_] = {"hl": bool(m.get("hl")), "c": cor_hex(m.get("c")), "p": [], "item": None}
        c = self.desenho.c
        antes = len(t["p"])
        t["p"].extend(novos)
        if t["hl"]:
            r = max(8.0, self.w * self.aj["hl_larg"] / 1000.0) / 2
            a = self.aj["hl_alpha"]
            st = "gray75" if a >= 60 else ("gray50" if a >= 35 else "gray25")
            ini = max(0, antes - 1)
            for i in range(ini, len(t["p"])):
                x1, y1 = t["p"][i]
                x0, y0 = t["p"][i - 1] if i > 0 else (x1, y1)
                n = max(1, int(math.hypot(x1 - x0, y1 - y0) / (r / 2)))
                for k in range(1, n + 1):
                    px, py = x0 + (x1 - x0) * k / n, y0 + (y1 - y0) * k / n
                    c.create_oval(px - r, py - r, px + r, py + r, fill=t["c"], outline="", stipple=st, tags="pen")
        else:
            larg = max(2.0, self.w * self.aj["pen_larg"] / 1000.0)
            if len(t["p"]) == 1:
                x, y = t["p"][0]
                c.create_oval(x - larg / 2, y - larg / 2, x + larg / 2, y + larg / 2, fill=t["c"], outline="", tags="pen")
            elif t["item"] is None:
                t["item"] = c.create_line(*[v for p in t["p"] for v in p], fill=t["c"], width=larg, capstyle="round",
                                          joinstyle="round", tags="pen")
            else:
                c.coords(t["item"], *[v for p in t["p"] for v in p])

    # ---------------------------------------------------------------- cortina e confete
    def cortina(self, m):
        k = m.get("k", "off")
        if k not in ("preto", "branco", "teatro", "fim", "off"):
            return
        self.cort_tipo = "" if k == "off" else k
        if self.cort_tipo:
            if self.cort_vis == "teatro" and k != "teatro":
                self.prog = 0.0
            self.cort_vis = k
            self.txt, self.sub = str(m.get("txt", "")), str(m.get("sub", ""))
            if k == "fim":
                self.emitindo, self.fim_emit = True, time.monotonic() + 6

    def desenhar_cortina(self):
        c = self.cort.c
        c.delete("cur")
        k, a, w, h = self.cort_vis, self.prog, self.w, self.h
        if a <= 0.001:
            return
        if k == "teatro":
            e = a * a * (3 - 2 * a)
            larg = w / 2 + 2
            n = 14
            for lado in (0, 1):
                x0 = (-larg + e * larg) if lado == 0 else (w - e * larg)
                for i in range(n):
                    cor = "#6b0a14" if i % 2 == 0 else "#b8202a"
                    c.create_rectangle(x0 + larg * i / n, 0, x0 + larg * (i + 1) / n + 1, h, fill=cor, outline="", tags="cur")
        else:
            cor = {"branco": "#ffffff", "fim": "#080c1c"}.get(k, "#000000")
            c.create_rectangle(0, 0, w, h, fill=cor, outline="", tags="cur")
            if k == "fim":
                c.create_text(w / 2, h * 0.45, text=self.txt, fill="white", font=("Helvetica", int(h * 0.09), "bold"), tags="cur")
                if self.sub:
                    c.create_text(w / 2, h * 0.58, text=self.sub, fill="#cfd6ee", font=("Helvetica", int(h * 0.035)), tags="cur")
        for p in self.parts:
            c.tag_raise(p["id"])

    def passo(self):
        dt = 1 / 30.0
        alvo = 1.0 if self.cort_tipo else 0.0
        vel = (1 / 1.3) if self.cort_vis == "teatro" else (1 / 0.35)
        antes = self.prog
        if self.prog < alvo:
            self.prog = min(alvo, self.prog + vel * dt)
        elif self.prog > alvo:
            self.prog = max(alvo, self.prog - vel * dt)
        if self.prog != antes or self.cort_tipo:
            self.desenhar_cortina()
        # confete
        c = self.cort.c
        if self.emitindo and time.monotonic() < self.fim_emit:
            for _ in range(9):
                tam = random.uniform(7, 14)
                self.parts.append({"x": random.uniform(0, self.w), "y": -20.0, "vx": random.uniform(-120, 120),
                                   "vy": random.uniform(80, 200), "r": random.uniform(0, 6.28), "vr": random.uniform(-9, 9),
                                   "w": tam, "h": tam * 0.6, "id": c.create_polygon(0, 0, 0, 0, 0, 0, 0, 0,
                                                                                   fill=random.choice(PALETA), outline="")})
        else:
            self.emitindo = False
        vivos = []
        for p in self.parts:
            p["vy"] = min(p["vy"] + 260 * dt, 340)
            p["x"] += (p["vx"] + math.sin(p["y"] / 45) * 40) * dt
            p["y"] += p["vy"] * dt
            p["r"] += p["vr"] * dt
            if p["y"] > self.h + 30:
                c.delete(p["id"])
                continue
            co, si = math.cos(p["r"]), math.sin(p["r"])
            pts = []
            for dx, dy in ((-p["w"] / 2, -p["h"] / 2), (p["w"] / 2, -p["h"] / 2), (p["w"] / 2, p["h"] / 2), (-p["w"] / 2, p["h"] / 2)):
                pts += [p["x"] + dx * co - dy * si, p["y"] + dx * si + dy * co]
            c.coords(p["id"], *pts)
            vivos.append(p)
        self.parts = vivos
        if self.cort_vis == "teatro" and (self.prog > 0 or self.parts):
            self.cort.definir_alpha(1.0)
        elif self.parts:
            self.cort.definir_alpha(1.0)
        else:
            self.cort.definir_alpha(self.prog)
        self.raiz.after(33, self.passo)

    # ---------------------------------------------------------------- mensagens
    def tratar(self, m):
        t = m.get("t")
        if t == "laser":
            self.laser(m)
        elif t == "draw":
            self.desenhar(m)
        elif t == "cfg":
            for k, lo, hi in (("laser_tam", 6, 120), ("foco_escuro", 0, 90), ("pen_larg", 0.5, 20), ("hl_larg", 3, 60), ("hl_alpha", 5, 90)):
                if isinstance(m.get(k), (int, float)):
                    self.aj[k] = min(hi, max(lo, float(m[k])))
            if isinstance(m.get("laser_cor"), str):
                self.aj["laser_cor"] = cor_hex(m["laser_cor"], self.aj["laser_cor"])
            if self.foco_on:
                self.dim.definir_alpha(self.aj["foco_escuro"] / 100.0)
        elif t == "clear":
            self.desenho.c.delete("pen")
            self.tracos.clear()
        elif t == "cortina":
            self.cortina(m)
        elif t == "confete":
            self.emitindo, self.fim_emit = True, time.monotonic() + min(10.0, max(1.0, float(m.get("seg", 4))))
        elif t == "quit":
            self.raiz.quit()

    def poll(self):
        try:
            while True:
                m = self.fila.get_nowait()
                if m is None:
                    self.raiz.quit()
                    return
                self.tratar(m)
        except queue.Empty:
            pass
        self.raiz.after(8, self.poll)


def main():
    if WIN:
        try:
            import ctypes
            ctypes.windll.shcore.SetProcessDpiAwareness(2)  # pixels fisicos, igual ao backend de teclas e mouse
        except Exception:
            pass
    try:
        raiz = tk.Tk()
    except tk.TclError as e:
        print(f"overlay_tk: sem tela ({e})", file=sys.stderr)
        sys.exit(3)
    raiz.withdraw()
    geom = None
    if "--geom" in sys.argv:
        try:
            geom = tuple(int(v) for v in sys.argv[sys.argv.index("--geom") + 1].split(","))
            if len(geom) != 4:
                geom = None
        except (IndexError, ValueError):
            geom = None
    app = App(raiz, geom)

    def ler():
        for linha in sys.stdin:
            try:
                app.fila.put(json.loads(linha))
            except ValueError:
                continue
        app.fila.put(None)  # pai fechou a entrada: encerra

    threading.Thread(target=ler, daemon=True).start()
    raiz.mainloop()


if __name__ == "__main__":
    main()
