#!/bin/sh
# Remove o Baton do usuario atual: inicio automatico, processos, comando, arquivos e (no macOS) o registro da Acessibilidade.
DEST="${BATON_DIR:-$HOME/.local/share/baton}"
[ -f "$DEST/baton.py" ] && python3 "$DEST/baton.py" --desinstalar
if [ "$(uname)" = "Darwin" ]; then
  launchctl bootout "gui/$(id -u)/com.baton" 2>/dev/null || true
  launchctl unload "$HOME/Library/LaunchAgents/com.baton.plist" 2>/dev/null || true
  pkill -f "Baton.app/Contents/MacOS/Baton" 2>/dev/null || true
  pkill -f "$DEST/baton.py" 2>/dev/null || true
  tccutil reset Accessibility com.baton.app >/dev/null 2>&1 || true
  rm -rf "$HOME/Applications/Baton.app" "$HOME/Library/LaunchAgents/com.baton.plist"
else
  pkill -f "$DEST/baton.py" 2>/dev/null || true
fi
rm -f "$HOME/.local/bin/baton"
rm -rf "$DEST"
echo "Baton removido. Preferencias e token ficam em ~/.cache/baton (apague a pasta se quiser)."
