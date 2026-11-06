#!/usr/bin/env python3
"""Gera dist/Baton-windows.zip: pasta portatil para Windows (Python embutido + Baton), sem instalar nada.

Uso: python3 scripts/empacotar-windows.py [--python 3.12.8]
No Windows: extrair o zip e abrir Baton.bat. Os dados (celulares pareados, cache, notas) ficam em dados/, ao lado;
nada e gravado no perfil do usuario do PC.
"""
import argparse
import os
import shutil
import subprocess
import sys
import urllib.request
import zipfile
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
ARQUIVOS = ["baton.py", "residente.py", "relay.py", "overlay_tk.py", "tray_win.ps1", "LICENSE"]

BAT = r'''@echo off
cd /d "%~dp0"
if not exist "dados" mkdir "dados"
start "" wscript.exe //nologo "%~dp0baton\iniciar.vbs"
'''

VBS = r'''Set fso = CreateObject("Scripting.FileSystemObject")
raiz = fso.GetParentFolderName(fso.GetParentFolderName(WScript.ScriptFullName))
CreateObject("WScript.Shell").Run "cmd /c """ & raiz & "\baton\rodar.bat""", 0, False
'''

RODAR = r'''@echo off
set "RAIZ=%~dp0.."
set "USERPROFILE=%RAIZ%\dados"
set "HOME=%RAIZ%\dados"
set "PYTHONIOENCODING=utf-8"
if not exist "%USERPROFILE%\.cache\baton" mkdir "%USERPROFILE%\.cache\baton"
"%RAIZ%\python\python.exe" "%RAIZ%\baton\baton.py" --residente > "%USERPROFILE%\.cache\baton\baton.log" 2>&1
'''

LEIAME = """Baton portatil (Windows)
========================
1. Abra Baton.bat (nao precisa de instalacao nem de administrador).
2. O navegador abre com o QR code: escaneie com o celular (mesma rede, hotspot ou rede Bluetooth).
3. Abra http://localhost:8765/painel, solte o PDF ou PPTX na area Apresentacao: ele abre em tela cheia.
4. Para encerrar: botao "Sair do Baton" no painel (ou encerre python.exe no Gerenciador de Tarefas).

Tudo fica nesta pasta (dados/): para nao deixar rastro no computador, apague-a ao terminar.
Se o Windows perguntar sobre o Firewall, permita redes privadas; se o celular nao conectar, a rede pode estar como
Publica (Configuracoes > Rede > Propriedades > Perfil Privado).
Log: dados\\.cache\\baton\\baton.log
"""


def baixar(url, destino):
    print("baixando", url)
    with urllib.request.urlopen(url, timeout=60) as r, open(destino, "wb") as f:
        shutil.copyfileobj(r, f)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--python", default="3.12.8", help="versao do Python embutido (amd64)")
    a = ap.parse_args()
    dist = RAIZ / "dist"
    pasta = dist / "Baton"
    if pasta.exists():
        shutil.rmtree(pasta)
    (pasta / "baton").mkdir(parents=True)
    (pasta / "dados").mkdir()
    emb = dist / f"python-{a.python}-embed-amd64.zip"
    if not emb.exists():
        dist.mkdir(exist_ok=True)
        baixar(f"https://www.python.org/ftp/python/{a.python}/python-{a.python}-embed-amd64.zip", emb)
    with zipfile.ZipFile(emb) as z:
        z.extractall(pasta / "python")
    # o embutido ignora o diretorio do script: acrescenta a pasta do Baton ao caminho de importacao
    for pth in (pasta / "python").glob("python*._pth"):
        linhas = [l for l in pth.read_text().splitlines() if l.strip() and not l.startswith("#")]
        linhas = [l for l in linhas if l != "import site"] + ["..\\baton", "import site"]
        pth.write_text("\r\n".join(linhas) + "\r\n")
    for nome in ARQUIVOS:
        shutil.copy2(RAIZ / nome, pasta / "baton" / nome)
    try:
        versao = subprocess.run(["git", "-C", str(RAIZ), "rev-parse", "--short", "HEAD"], capture_output=True, text=True).stdout.strip()
    except OSError:
        versao = ""
    (pasta / "baton" / "VERSION").write_text(versao + "\n")
    # pdf.js e gerador de QR embutidos: o celular funciona sem internet no PC do instituto
    r = subprocess.run([sys.executable, str(pasta / "baton" / "baton.py"), "--preparar-offline", "--vendor"],
                       env=dict(os.environ, HOME=str(dist / "home-build")), capture_output=True, text=True)
    print((r.stdout + r.stderr).strip())
    if r.returncode != 0:
        sys.exit("nao consegui baixar o pdf.js/QR (precisa de internet para empacotar)")
    for f in (pasta / "baton").rglob("__pycache__"):
        shutil.rmtree(f, ignore_errors=True)
    (pasta / "Baton.bat").write_text(BAT.replace("\n", "\r\n"), encoding="ascii")
    (pasta / "baton" / "iniciar.vbs").write_text(VBS.replace("\n", "\r\n"), encoding="ascii")
    (pasta / "baton" / "rodar.bat").write_text(RODAR.replace("\n", "\r\n"), encoding="ascii")
    (pasta / "LEIA-ME.txt").write_text(LEIAME.replace("\n", "\r\n"), encoding="ascii", errors="replace")
    saida = dist / "Baton-windows.zip"
    saida.unlink(missing_ok=True)
    with zipfile.ZipFile(saida, "w", zipfile.ZIP_DEFLATED) as z:
        for f in sorted(pasta.rglob("*")):
            if f.is_file():
                z.write(f, Path("Baton") / f.relative_to(pasta))
            elif not any(f.iterdir()):
                z.write(f, str(Path("Baton") / f.relative_to(pasta)) + "/")
    shutil.rmtree(dist / "home-build", ignore_errors=True)
    print(f"pronto: {saida} ({saida.stat().st_size // 1024} KB)")


if __name__ == "__main__":
    main()
