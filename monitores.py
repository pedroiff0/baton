"""monitores.py: lista os monitores e escolhe o da projecao.

Cada monitor e um dict {id, nome, x, y, w, h, principal} em pixels do desktop virtual. A projecao automatica usa o monitor
da janela em primeiro plano em tela cheia; sem essa informacao, o unico monitor nao principal quando ha exatamente dois;
senao o principal. Somente biblioteca padrao.
"""
import json
import re
import shutil
import subprocess
import sys
import time

_CACHE = {"t": 0.0, "dados": None}


def _rodar(cmd, tempo=6):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=tempo).stdout
    except (OSError, subprocess.TimeoutExpired):
        return ""


def parse_xrandr(saida):
    """`xrandr --listmonitors`: ' 0: +*DP-1 1920/530x1080/300+0+455  DP-1' -> monitores."""
    mons = []
    for linha in saida.splitlines():
        m = re.match(r"\s*\d+:\s*\+?(\*?)\+?(\S+)\s+(\d+)/\d+x(\d+)/\d+\+(-?\d+)\+(-?\d+)", linha)
        if m:
            mons.append({"nome": m.group(2), "x": int(m.group(5)), "y": int(m.group(6)), "w": int(m.group(3)),
                         "h": int(m.group(4)), "principal": bool(m.group(1))})
    return mons


def parse_powershell(saida):
    """JSON de `[Screen]::AllScreens | select DeviceName,Primary,Bounds` (lista ou objeto unico)."""
    try:
        d = json.loads(saida)
    except ValueError:
        return []
    if isinstance(d, dict):
        d = [d]
    mons = []
    for s in d:
        b = s.get("Bounds") or {}
        mons.append({"nome": str(s.get("DeviceName", "")).replace("\\\\.\\", ""), "x": int(b.get("X", 0)), "y": int(b.get("Y", 0)),
                     "w": int(b.get("Width", 0)), "h": int(b.get("Height", 0)), "principal": bool(s.get("Primary"))})
    return mons


def parse_jxa(saida):
    """JSON de NSScreen.screens (frame em pontos, origem embaixo-esquerda) -> coordenadas com origem em cima-esquerda."""
    try:
        d = json.loads(saida)
    except ValueError:
        return []
    mons = []
    altura = d[0]["h"] if d else 0  # o primeiro NSScreen e o principal: define o eixo y global
    for s in d:
        mons.append({"nome": s.get("nome", ""), "x": int(s["x"]), "y": int(altura - s["y"] - s["h"]), "w": int(s["w"]), "h": int(s["h"]),
                     "principal": bool(s.get("principal"))})
    return mons


JXA = ('ObjC.import("AppKit");var o=[];var t=$.NSScreen.screens;for(var i=0;i<t.count;i++){var s=t.objectAtIndex(i),f=s.frame;'
       'o.push({nome:ObjC.unwrap(s.localizedName),x:f.origin.x,y:f.origin.y,w:f.size.width,h:f.size.height,principal:i==0})}'
       'JSON.stringify(o)')
PS = ("Add-Type -AssemblyName System.Windows.Forms;Add-Type -MemberDefinition '[DllImport(\"user32.dll\")] public static extern bool "
      "SetProcessDPIAware();' -Name D -Namespace W;[void][W.D]::SetProcessDPIAware();"
      "[System.Windows.Forms.Screen]::AllScreens | Select-Object DeviceName,Primary,Bounds | ConvertTo-Json -Compress")


def _coletar():
    mons = []
    if sys.platform == "darwin":
        mons = parse_jxa(_rodar(["osascript", "-l", "JavaScript", "-e", JXA]))
    elif sys.platform == "win32":
        mons = parse_powershell(_rodar(["powershell", "-NoProfile", "-WindowStyle", "Hidden", "-Command", PS], 12))
    elif shutil.which("xrandr"):
        mons = parse_xrandr(_rodar(["xrandr", "--listmonitors"]))
    if not mons:
        mons = [{"nome": "Tela", "x": 0, "y": 0, "w": 1920, "h": 1080, "principal": True}]
    if not any(m["principal"] for m in mons):
        mons[0]["principal"] = True
    for i, m in enumerate(mons):
        m["id"] = i
    return mons


def listar(validade=10.0):
    agora = time.monotonic()
    if _CACHE["dados"] is None or agora - _CACHE["t"] > validade:
        _CACHE["dados"], _CACHE["t"] = _coletar(), agora
    return _CACHE["dados"]


def monitor_de(mons, x, y):
    """Monitor que contem o ponto (x, y); o mais proximo se cair fora de todos."""
    melhor, dist = None, None
    for m in mons:
        dx = max(m["x"] - x, 0, x - (m["x"] + m["w"] - 1))
        dy = max(m["y"] - y, 0, y - (m["y"] + m["h"] - 1))
        d = dx * dx + dy * dy
        if dist is None or d < dist:
            melhor, dist = m, d
    return melhor


def janela_ativa():
    """(x, y, w, h) da janela em primeiro plano, ou None."""
    try:
        if sys.platform.startswith("linux") and shutil.which("xdotool"):
            d = dict(l.split("=", 1) for l in _rodar(["xdotool", "getactivewindow", "getwindowgeometry", "--shell"], 3).split() if "=" in l)
            r = (int(d["X"]), int(d["Y"]), int(d["WIDTH"]), int(d["HEIGHT"]))
            return r if r[2] > 50 and r[3] > 50 else None
        if sys.platform == "win32":
            import ctypes
            from ctypes import wintypes as wt
            u = ctypes.windll.user32
            rc = wt.RECT()
            if u.GetWindowRect(u.GetForegroundWindow(), ctypes.byref(rc)):
                return rc.left, rc.top, rc.right - rc.left, rc.bottom - rc.top
        if sys.platform == "darwin":
            s = ('tell application "System Events" to tell (first process whose frontmost is true) to get {position, size} of window 1')
            n = [int(float(v)) for v in re.findall(r"-?\d+(?:\.\d+)?", _rodar(["osascript", "-e", s], 4))]
            if len(n) == 4:
                return n[0], n[1], n[2], n[3]
    except (KeyError, ValueError, OSError, ImportError, AttributeError):
        pass
    return None


def escolher(preferencia="auto", mons=None, janela=None, anterior=None):
    """Monitor da projecao. preferencia: 'auto' ou numero do monitor (1 = primeiro da lista); anterior: ultimo escolhido (dict)."""
    mons = mons or listar()
    try:
        n = int(preferencia)
        if 1 <= n <= len(mons):
            return mons[n - 1]
    except (TypeError, ValueError):
        pass
    j = janela if janela is not None else janela_ativa()
    if j:
        m = monitor_de(mons, j[0] + j[2] // 2, j[1] + j[3] // 2)
        if j[2] >= m["w"] * 0.9 and j[3] >= m["h"] * 0.9:  # janela em tela cheia nesse monitor
            return m
    if anterior is not None:  # sem janela em tela cheia agora: mantem o ultimo monitor (nao fica pulando entre telas)
        for m in mons:
            if m["id"] == anterior.get("id"):
                return m
    outros = [m for m in mons if not m["principal"]]
    if len(mons) == 2 and len(outros) == 1:
        return outros[0]
    return next((m for m in mons if m["principal"]), mons[0])
