#!/usr/bin/env python3
"""Monta a pasta Baton/ para pen-drive, multi-sistema: Windows, Mac (Apple Silicon e Intel) e Linux, sem instalar nada.

O pen-drive leva tudo: os lancadores portateis (sem instalar), o Python de cada sistema e o repositorio completo (com .git),
entao tambem da para instalar de vez (`./install.sh`, `install.ps1`) e atualizar (`git pull`) como quem clonou do GitHub.

Uso (pode rodar no Mac, Linux ou Windows, com internet na primeira vez):
    python3 scripts/empacotar-pendrive.py --destino /Volumes/PENDRIVE
    python3 scripts/empacotar-pendrive.py                 # so gera dist/Baton-pendrive/
    python3 scripts/empacotar-pendrive.py --so win,mac    # sem o Linux (menor)

No computador externo, abrir direto do pen-drive: Windows `Baton.bat`, Mac `Baton.command`, Linux `Baton.sh`.
Cada sistema usa o seu Python embutido (python-build-standalone, com Tk) em runtime/; os dados (celulares pareados,
cache, notas) ficam em dados/, no proprio pen-drive; nada e gravado no perfil do computador.
"""
import argparse
import json
import posixpath
import shutil
import stat
import subprocess
import sys
import tarfile
import urllib.request
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
# pasta em runtime/ -> trecho do nome do arquivo do python-build-standalone
RUNTIMES = {"win": ("win-x64", "x86_64-pc-windows-msvc"), "mac": ("mac-arm64", "aarch64-apple-darwin"),
            "mac-intel": ("mac-x64", "x86_64-apple-darwin"), "linux": ("linux-x64", "x86_64-unknown-linux-gnu")}
API = "https://api.github.com/repos/astral-sh/python-build-standalone/releases/latest"

BAT = r'''@echo off
cd /d "%~dp0"
if not exist "dados" mkdir "dados"
start "" wscript.exe //nologo "%~dp0lancador\iniciar.vbs"
'''
VBS = r'''Set fso = CreateObject("Scripting.FileSystemObject")
raiz = fso.GetParentFolderName(fso.GetParentFolderName(WScript.ScriptFullName))
CreateObject("WScript.Shell").Run "cmd /c """ & raiz & "\lancador\rodar.bat""", 0, False
'''
RODAR = r'''@echo off
set "RAIZ=%~dp0.."
set "USERPROFILE=%RAIZ%\dados"
set "HOME=%RAIZ%\dados"
set "PYTHONIOENCODING=utf-8"
if not exist "%USERPROFILE%\.cache\baton" mkdir "%USERPROFILE%\.cache\baton"
"%RAIZ%\runtime\win-x64\python.exe" "%RAIZ%\repositorio\baton.py" --residente > "%USERPROFILE%\.cache\baton\baton.log" 2>&1
'''
UNIX = r'''#!/bin/bash
# Baton portatil: roda direto do pen-drive, com o Python embutido desta pasta.
cd "$(dirname "$0")" || exit 1
RAIZ="$(pwd)"
mkdir -p "$RAIZ/dados"
SO="$(uname -s)"
if [ "$SO" = "Darwin" ]; then
  case "$(uname -m)" in arm64) R=mac-arm64 ;; *) R=mac-x64 ;; esac
  xattr -dr com.apple.quarantine "$RAIZ" 2>/dev/null
else
  R=linux-x64
fi
PY="$RAIZ/runtime/$R/bin/python3"
if [ "$SO" = "Linux" ] && python3 -c "import gi, cairo" 2>/dev/null; then PY="$(command -v python3)"; fi  # camada Gtk precisa do python do sistema
if [ ! -f "$PY" ]; then
  echo "Este pen-drive nao tem o Python para este computador ($R). Gere de novo com scripts/empacotar-pendrive.py."
  exit 1
fi
chmod +x "$RAIZ/runtime/$R/bin/"* 2>/dev/null
[ -z "$XAUTHORITY" ] && [ -f "$HOME/.Xauthority" ] && export XAUTHORITY="$HOME/.Xauthority"  # o X precisa da credencial do HOME real
export HOME="$RAIZ/dados"
export PYTHONIOENCODING=utf-8
echo "Baton: abra o QR code no navegador que vai abrir. Para encerrar, feche esta janela ou use 'Sair do Baton' no painel."
exec "$PY" "$RAIZ/repositorio/baton.py" --residente
'''
LEIAME = """Baton portatil (Windows, Mac e Linux)
=====================================
Abra direto do pen-drive, sem instalar e sem copiar nada para o computador:
  Windows : duplo clique em Baton.bat
  Mac     : duplo clique em Baton.command (se o Mac bloquear: botao direito > Abrir)
  Linux   : bash Baton.sh

O navegador abre com o QR code: escaneie com o celular (mesma rede, hotspot ou a internet, pelo relay).
Abra http://localhost:8765/painel, solte o PDF ou PPTX na area Apresentacao: ele abre em tela cheia.
Para encerrar: botao "Sair do Baton" no painel.

Esta pasta tambem tem o repositorio completo (repositorio/, com .git), como quem clonou do GitHub:
  instalar de vez : Mac/Linux `cd repositorio && ./install.sh` (precisa de Python 3 no computador); Windows `repositorio\\install.ps1`
  atualizar       : `git pull` dentro de repositorio/ (o Baton portatil usa esse mesmo codigo)
  testes          : `python3 -m unittest discover -s tests`

Tudo fica na pasta dados/ deste pen-drive (celulares pareados, cache, notas); o computador nao guarda nada.
Windows: se o Firewall perguntar, permita redes privadas.
Mac: o Mac pede Acessibilidade (teclas) e Gravacao de Tela (zoom e previa) na primeira vez, em Ajustes do Sistema > Privacidade e Seguranca.
"""


def baixar(url, destino):
    print("baixando", url.rsplit("/", 1)[-1])
    req = urllib.request.Request(url, headers={"User-Agent": "baton-empacotar"})
    with urllib.request.urlopen(req, timeout=120) as r, open(destino, "wb") as f:
        shutil.copyfileobj(r, f)


def achar_assets(versao):
    """Nome e URL do python-build-standalone mais recente (install_only_stripped) de cada alvo."""
    req = urllib.request.Request(API, headers={"User-Agent": "baton-empacotar", "Accept": "application/vnd.github+json"})
    with urllib.request.urlopen(req, timeout=60) as r:
        rel = json.load(r)
    assets = rel.get("assets", [])
    saida = {}
    for chave, (_, triplo) in RUNTIMES.items():
        cand = [a for a in assets if a["name"].startswith(f"cpython-{versao}.") and triplo in a["name"]
                and a["name"].endswith("install_only_stripped.tar.gz")]
        if not cand:
            sys.exit(f"nao achei o Python {versao} para {triplo} no release {rel.get('tag_name')}; tente outra --python (ex.: 3.12)")
        saida[chave] = max(cand, key=lambda a: a["name"])
    return saida


def extrair(tar_gz, destino):
    """Extrai python/ de um tar.gz para `destino`. Links viram copias: pen-drive exFAT/FAT nao tem symlink."""
    if destino.exists():
        shutil.rmtree(destino)
    destino.mkdir(parents=True)
    with tarfile.open(tar_gz) as tf:
        membros = tf.getmembers()
        links = []
        for m in membros:
            nome = posixpath.normpath(m.name)
            if not nome.startswith("python/") and nome != "python":
                continue
            rel = nome[len("python/"):] if nome != "python" else ""
            if not rel:
                continue
            alvo = destino / rel
            if m.isdir():
                alvo.mkdir(parents=True, exist_ok=True)
            elif m.isfile():
                alvo.parent.mkdir(parents=True, exist_ok=True)
                with tf.extractfile(m) as src, open(alvo, "wb") as dst:
                    shutil.copyfileobj(src, dst)
                alvo.chmod(m.mode | stat.S_IRUSR | stat.S_IWUSR)
            elif m.issym() or m.islnk():
                links.append((rel, m))
        for _ in range(3):  # link para link: resolve em passadas
            pend = []
            for rel, m in links:
                if m.issym():
                    origem = posixpath.normpath(posixpath.join(posixpath.dirname(rel), m.linkname))
                else:
                    origem = posixpath.normpath(m.linkname)[len("python/"):]
                fonte, alvo = destino / origem, destino / rel
                if fonte.is_file():
                    alvo.parent.mkdir(parents=True, exist_ok=True)
                    try:
                        shutil.copyfile(fonte, alvo)
                        alvo.chmod(fonte.stat().st_mode | stat.S_IRUSR | stat.S_IWUSR)
                    except shutil.SameFileError:  # Mac/Windows ignoram maiusculas: n/x e N/X sao o mesmo arquivo (terminfo)
                        pass
                elif fonte.is_dir():
                    if fonte.resolve() != alvo.resolve():
                        shutil.copytree(fonte, alvo, dirs_exist_ok=True)
                else:
                    pend.append((rel, m))
            links = pend
            if not links:
                break


def copiar_com_progresso(origem, destino):
    """Copia a arvore arquivo a arquivo (sem copiar atributos: exFAT/FAT nao os guardam e isso so deixa lento), com progresso."""
    import time
    arquivos = [f for f in origem.rglob("*") if f.is_file()]
    total = sum(f.stat().st_size for f in arquivos) or 1
    feito, t0 = 0, time.monotonic()
    for i, f in enumerate(arquivos, 1):
        alvo = destino / f.relative_to(origem)
        alvo.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(f, alvo)
        if f.stat().st_mode & 0o111:  # preserva o bit de execucao (Linux/ext4); exFAT ignora
            try:
                alvo.chmod(f.stat().st_mode & 0o777)
            except OSError:
                pass
        feito += f.stat().st_size
        if i % 150 == 0 or i == len(arquivos):
            dt = max(time.monotonic() - t0, 0.001)
            print(f"  {i}/{len(arquivos)} arquivos, {feito // 1048576}/{total // 1048576} MB ({100 * feito // total}%), {feito / 1048576 / dt:.1f} MB/s", flush=True)
    for d in origem.rglob("*"):  # pastas vazias (dados/)
        if d.is_dir():
            (destino / d.relative_to(origem)).mkdir(parents=True, exist_ok=True)


def clonar_repositorio(destino):
    """Repositorio completo (com .git, origin = GitHub) em `destino`; sem git, copia os arquivos."""
    def git(*args, pasta=RAIZ):
        return subprocess.run(["git", "-C", str(pasta), *args], capture_output=True, text=True)
    try:
        ok = (RAIZ / ".git").exists() and subprocess.run(["git", "clone", "--quiet", "--no-hardlinks", str(RAIZ), str(destino)],
                                                          capture_output=True).returncode == 0
    except OSError:
        ok = False
    if ok:
        origem = git("remote", "get-url", "origin").stdout.strip()
        if origem:
            git("remote", "set-url", "origin", origem, pasta=destino)
        if git("status", "--porcelain").stdout.strip():
            print("aviso: ha alteracoes nao commitadas aqui; o pen-drive leva so o ultimo commit")
        return
    shutil.copytree(RAIZ, destino, ignore=shutil.ignore_patterns("dist", ".git", "__pycache__", "*.pyc", ".DS_Store"))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--destino", default="", metavar="PASTA", help="copia a pasta pronta para PASTA/Baton (ex.: /Volumes/PENDRIVE); dados/ ja existente e mantido")
    ap.add_argument("--so", default="win,mac,linux", help="sistemas incluidos: win, mac (Apple Silicon e Intel), linux")
    ap.add_argument("--python", default="3.12", help="versao do Python embutido (padrao: 3.12)")
    ap.add_argument("--convite", default="", metavar="HOST/CODIGO", help="convite de uso unico do relay (baton --criar-convite), opcional")
    a = ap.parse_args()
    escolhidos = {s.strip() for s in a.so.split(",") if s.strip()}
    quais = [k for k in RUNTIMES if k.split("-")[0] in escolhidos]
    if not quais:
        sys.exit("use --so com win, mac e/ou linux")
    dist = RAIZ / "dist"
    cache = dist / "runtime-cache"
    cache.mkdir(parents=True, exist_ok=True)
    pasta = dist / "Baton-pendrive" / "Baton"
    if pasta.parent.exists():
        shutil.rmtree(pasta.parent)
    (pasta / "lancador").mkdir(parents=True)
    (pasta / "dados").mkdir()
    assets = achar_assets(a.python)
    for k in quais:
        asset = assets[k]
        arq = cache / asset["name"]
        if not arq.exists():
            baixar(asset["browser_download_url"], arq)
        print("extraindo", RUNTIMES[k][0])
        extrair(arq, pasta / "runtime" / RUNTIMES[k][0])
    repo = pasta / "repositorio"
    clonar_repositorio(repo)
    if a.convite:
        (repo / "convite.txt").write_text(a.convite.strip() + "\n", encoding="utf-8")
    (pasta / "Baton.bat").write_text(BAT.replace("\n", "\r\n"), encoding="ascii")
    (pasta / "lancador" / "iniciar.vbs").write_text(VBS.replace("\n", "\r\n"), encoding="ascii")
    (pasta / "lancador" / "rodar.bat").write_text(RODAR.replace("\n", "\r\n"), encoding="ascii")
    for nome in ("Baton.command", "Baton.sh"):
        f = pasta / nome
        f.write_text(UNIX, encoding="utf-8", newline="\n")
        f.chmod(0o755)
    (pasta / "LEIA-ME.txt").write_text(LEIAME.replace("\n", "\r\n"), encoding="ascii", errors="replace")
    for f in repo.rglob("__pycache__"):
        shutil.rmtree(f, ignore_errors=True)
    print(f"pronto: {pasta} ({sum(f.stat().st_size for f in pasta.rglob('*') if f.is_file()) // 1048576} MB)")
    if a.destino:
        alvo = Path(a.destino).expanduser() / "Baton"
        print(f"copiando para {alvo} (milhares de arquivos pequenos: em pen-drive USB leva alguns minutos; o progresso aparece abaixo)")
        for sub in ("runtime", "repositorio", "lancador", "baton"):  # codigo novo por cima do velho; dados/ nao e tocado
            if (alvo / sub).exists():
                print(f"removendo versao antiga de {sub}/ ...", flush=True)
                shutil.rmtree(alvo / sub, ignore_errors=True)
        copiar_com_progresso(pasta, alvo)
        print(f"copiado para {alvo}:")
        for f in sorted(alvo.iterdir()):
            print("  ", f.name + ("/" if f.is_dir() else ""))
        print("abra Baton.bat (Windows), Baton.command (Mac) ou Baton.sh (Linux) direto do pen-drive")


if __name__ == "__main__":
    main()
