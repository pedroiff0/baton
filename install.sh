#!/bin/sh
# Instala o Baton para o usuario atual (macOS e Linux): copia os arquivos, cria o comando `baton`
# e liga o modo residente no login. Desfaz com ./uninstall.sh. Nao precisa de sudo.
set -e
DEST="${BATON_DIR:-$HOME/.local/share/baton}"
SRC=$(cd "$(dirname "$0")" && pwd)

command -v python3 >/dev/null 2>&1 || { echo "Python 3 nao encontrado. Instale o Python 3.8 ou mais novo e rode de novo." >&2; exit 1; }
python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 8) else 1)' || { echo "Precisa do Python 3.8 ou mais novo." >&2; exit 1; }

mkdir -p "$DEST" "$HOME/.local/bin"
cp "$SRC"/baton.py "$SRC"/residente.py "$SRC"/overlay.swift "$SRC"/overlay_gtk.py "$SRC"/overlay_tk.py "$SRC"/LICENSE "$DEST"/
printf '#!/bin/sh\nexec python3 "%s/baton.py" "$@"\n' "$DEST" > "$HOME/.local/bin/baton"
chmod +x "$HOME/.local/bin/baton"

python3 "$DEST/baton.py" --instalar
if [ "$(uname)" != "Darwin" ] && [ -z "$BATON_SEM_INICIAR" ]; then
  nohup python3 "$DEST/baton.py" --residente >"$HOME/.cache/baton.log" 2>&1 &  # no Mac o launchd ja iniciou
fi

echo
echo "Baton instalado em $DEST (comando: baton; confira se ~/.local/bin esta no PATH)."
echo "Painel (pausar, modo, apps lembrados): http://localhost:8765/painel"
echo "Pausar:   baton --pausar     Retomar: baton --retomar     Remover: ./uninstall.sh"
