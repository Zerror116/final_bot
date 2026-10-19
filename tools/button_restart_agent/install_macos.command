#!/bin/zsh
cd "$(dirname "$0")"
LOG_PATH="$(pwd)/button_restart_agent.log"
INSTALL_ERROR_LOG="$(pwd)/install_error.log"
PYTHON_BIN="$(command -v python3)"

log_error() {
  print -r -- "$(date -u +"%Y-%m-%dT%H:%M:%SZ") $1" >> "$INSTALL_ERROR_LOG"
}

if [[ -z "$PYTHON_BIN" ]]; then
  log_error "python3 not found"
  exit 1
fi

if ! "$PYTHON_BIN" -m pip install --user -r requirements.txt >> "$INSTALL_ERROR_LOG" 2>&1; then
  log_error "dependency install failed"
  print -r -- "$(date -u +"%Y-%m-%dT%H:%M:%SZ") install failed: see $INSTALL_ERROR_LOG" >> "$LOG_PATH"
  exit 1
fi

if ! "$PYTHON_BIN" - <<'PY' >> "$INSTALL_ERROR_LOG" 2>&1
from pynput import keyboard
PY
then
  log_error "dependency import check failed"
  print -r -- "$(date -u +"%Y-%m-%dT%H:%M:%SZ") install failed: pynput is not importable" >> "$LOG_PATH"
  exit 1
fi

mkdir -p "$HOME/Library/LaunchAgents"
cat > "$HOME/Library/LaunchAgents/com.finalbot.buttonagent.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>com.finalbot.buttonagent</string>
  <key>ProgramArguments</key>
  <array>
    <string>${PYTHON_BIN}</string>
    <string>$(pwd)/button_restart_agent.py</string>
  </array>
  <key>WorkingDirectory</key>
  <string>$(pwd)</string>
  <key>RunAtLoad</key>
  <true/>
  <key>KeepAlive</key>
  <true/>
  <key>StandardOutPath</key>
  <string>$(pwd)/button_restart_agent.out.log</string>
  <key>StandardErrorPath</key>
  <string>$(pwd)/button_restart_agent.err.log</string>
</dict>
</plist>
PLIST
launchctl bootout "gui/$(id -u)" "$HOME/Library/LaunchAgents/com.finalbot.buttonagent.plist" >/dev/null 2>&1
if ! launchctl bootstrap "gui/$(id -u)" "$HOME/Library/LaunchAgents/com.finalbot.buttonagent.plist" >> "$INSTALL_ERROR_LOG" 2>&1; then
  log_error "launchctl bootstrap failed"
  exit 1
fi
if ! launchctl kickstart -k "gui/$(id -u)/com.finalbot.buttonagent" >> "$INSTALL_ERROR_LOG" 2>&1; then
  log_error "launchctl kickstart failed"
  exit 1
fi
