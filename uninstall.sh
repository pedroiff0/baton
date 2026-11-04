#!/bin/sh
# Remove o Baton do usuario atual: inicio automatico, comando e arquivos.
DEST="${BATON_DIR:-$HOME/.local/share/baton}"
[ -f "$DEST/baton.py" ] && python3 "$DEST/baton.py" --desinstalar
if [ "$(uname)" != "Darwin" ]; then pkill -f "$DEST/baton.py" 2>/dev/null || true; fi
rm -f "$HOME/.local/bin/baton"
rm -rf "$DEST"
echo "Baton removido. Preferencias e token ficam em ~/.cache/baton (apague a pasta se quiser)."
