#!/usr/bin/env bash
# Launcher del server Whisper locale (OpenAI-compatible su 127.0.0.1).
#
# Sceglie automaticamente l'interprete: preferisce il venv dedicato
# ($XDG_DATA_HOME/bravoric-stt-clipboard/whisper-venv) se esiste, altrimenti
# ripiega sul python3 di sistema (che vede la user site-packages).
#
# Uso: bin/whisper-server.sh [--model small] [--port 8080] [...]
# Launcher of the local Whisper server (OpenAI-compatible on 127.0.0.1).
#
# It picks the interpreter automatically: it prefers the dedicated venv
# ($XDG_DATA_HOME/bravoric-stt-clipboard/whisper-venv) if it exists,
# otherwise it falls back on the system python3 (which sees the user
# site-packages).
#
# Usage: bin/whisper-server.sh [--model small] [--port 8080] [...]
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DATA_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/bravoric-stt-clipboard"
WHISPER_VENV="$DATA_DIR/whisper-venv"

if [ -x "$WHISPER_VENV/bin/python" ]; then
    PY="$WHISPER_VENV/bin/python"
elif command -v python3 >/dev/null 2>&1; then
    PY="python3"
else
    case "${LANGUAGE:-${LC_ALL:-${LC_MESSAGES:-${LANG:-}}}}" in
        it*) echo "Errore: python3 non trovato" >&2 ;;
        *) echo "Error: python3 not found" >&2 ;;
    esac
    exit 1
fi

exec "$PY" "$PROJECT_DIR/bin/whisper-server.py" "$@"
