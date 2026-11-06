#!/bin/sh
# Instala o Baton para o usuario atual (macOS e Linux): copia os arquivos, cria o comando `baton`
# e liga o modo residente no login. Desfaz com ./uninstall.sh. Nao precisa de sudo.
set -e
DEST="${BATON_DIR:-$HOME/.local/share/baton}"
SRC=$(cd "$(dirname "$0")" && pwd)

command -v python3 >/dev/null 2>&1 || { echo "Python 3 nao encontrado. Instale o Python 3.8 ou mais novo e rode de novo." >&2; exit 1; }
python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 8) else 1)' || { echo "Precisa do Python 3.8 ou mais novo." >&2; exit 1; }

mkdir -p "$DEST" "$HOME/.local/bin"
cp "$SRC"/baton.py "$SRC"/residente.py "$SRC"/relay.py "$SRC"/monitores.py "$SRC"/launcher.swift "$SRC"/overlay.swift "$SRC"/overlay_gtk.py "$SRC"/overlay_tk.py "$SRC"/tray_gtk.py "$SRC"/LICENSE "$DEST"/
[ -f "$SRC/relay.json" ] && cp "$SRC/relay.json" "$DEST/" && chmod 600 "$DEST/relay.json"
[ -n "$BATON_CONVITE" ] && python3 "$DEST/baton.py" --convite "$BATON_CONVITE" || true
git -C "$SRC" rev-parse --short HEAD > "$DEST/VERSION" 2>/dev/null || true
printf '#!/bin/sh\nexec python3 "%s/baton.py" "$@"\n' "$DEST" > "$HOME/.local/bin/baton"
chmod +x "$HOME/.local/bin/baton"

# Linux: teclas (xdotool) e deteccao da apresentacao (xprop, do x11-utils). Pede a senha do sudo uma vez; BATON_SEM_DEPS=1 pula.
if [ "$(uname)" = "Linux" ] && [ -z "$BATON_SEM_DEPS" ] && { ! command -v xdotool >/dev/null 2>&1 || ! command -v xprop >/dev/null 2>&1; }; then
  echo "Instalando dependencias do Linux (xdotool, x11-utils)..."
  if command -v apt-get >/dev/null 2>&1; then sudo apt-get install -y xdotool x11-utils
  elif command -v dnf >/dev/null 2>&1; then sudo dnf install -y xdotool xorg-x11-utils
  elif command -v pacman >/dev/null 2>&1; then sudo pacman -S --needed --noconfirm xdotool xorg-xprop
  elif command -v zypper >/dev/null 2>&1; then sudo zypper install -y xdotool xprop
  else echo "aviso: gerenciador de pacotes nao reconhecido; instale xdotool e xprop a mao." >&2; fi \
    || echo "aviso: nao instalei xdotool/x11-utils; o Baton funciona sem eles, mas sem enviar teclas ao app em primeiro plano." >&2
fi

python3 "$DEST/baton.py" --instalar
python3 "$DEST/baton.py" --preparar-offline || echo "aviso: sem internet agora; rode 'baton --preparar-offline' quando estiver online (o celular usa a CDN ate la)"
if [ "$(uname)" != "Darwin" ] && [ -z "$BATON_SEM_INICIAR" ]; then
  nohup python3 "$DEST/baton.py" --residente >"$HOME/.cache/baton.log" 2>&1 &  # no Mac o launchd ja iniciou
fi

# Linux: icone na bandeja. O GNOME nao tem bandeja: precisa do AppIndicator (biblioteca + extensao do GNOME Shell).
if [ "$(uname)" = "Linux" ] && [ -z "$BATON_SEM_DEPS" ] && command -v apt-get >/dev/null 2>&1; then
  if ! python3 -c 'import gi; gi.require_version("AyatanaAppIndicator3","0.1")' >/dev/null 2>&1; then
    echo "Instalando o suporte a icone na bandeja (AppIndicator)..."
    sudo apt-get install -y gir1.2-ayatanaappindicator3-0.1 python3-gi || echo "aviso: sem suporte a AppIndicator; o icone pode nao aparecer." >&2
  fi
  case "${XDG_CURRENT_DESKTOP:-}" in *GNOME*)
    if ! gnome-extensions list 2>/dev/null | grep -qi appindicator; then
      sudo apt-get install -y gnome-shell-extension-appindicator || true
    fi
    gnome-extensions enable appindicatorsupport@rgcjonas.gmail.com 2>/dev/null || true
    echo "GNOME: se o icone nao aparecer, saia e entre na sessao de novo (a extensao AppIndicator so carrega no login)." ;;
  esac
fi

echo
echo "Baton instalado em $DEST (comando: baton; confira se ~/.local/bin esta no PATH)."
if [ "$(uname)" = "Darwin" ]; then
  echo "macOS: em Ajustes do Sistema > Privacidade e Seguranca > Acessibilidade, ative o Baton (se houver uma entrada antiga, remova e adicione de novo)."
fi
echo "Painel (pausar, modo, apps lembrados): http://localhost:8765/painel"
echo "Pausar:   baton --pausar     Retomar: baton --retomar     Remover: ./uninstall.sh"
