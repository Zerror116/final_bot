#!/bin/zsh
cd "$(dirname "$0")"
LOG_PATH="$(pwd)/button_restart_agent.log"
INSTALL_ERROR_LOG="$(pwd)/install_error.log"
PYTHON_BIN="$(command -v python3)"

if [[ -z "$PYTHON_BIN" ]]; then
  print -r -- "$(date -u +"%Y-%m-%dT%H:%M:%SZ") run failed: python3 not found" >> "$LOG_PATH"
  exit 1
fi

if ! "$PYTHON_BIN" -m pip install --user -r requirements.txt >> "$INSTALL_ERROR_LOG" 2>&1; then
  print -r -- "$(date -u +"%Y-%m-%dT%H:%M:%SZ") run failed: dependency install failed, see $INSTALL_ERROR_LOG" >> "$LOG_PATH"
  exit 1
fi

if ! "$PYTHON_BIN" - <<'PY' >> "$INSTALL_ERROR_LOG" 2>&1
from pynput import keyboard
PY
then
  print -r -- "$(date -u +"%Y-%m-%dT%H:%M:%SZ") run failed: pynput is not importable" >> "$LOG_PATH"
  exit 1
fi

"$PYTHON_BIN" button_restart_agent.py >/dev/null 2>&1 &
