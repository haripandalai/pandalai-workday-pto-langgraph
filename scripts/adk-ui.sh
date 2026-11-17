#!/bin/sh
# Start/stop the local ADK dev UI from any terminal (no Claude session needed).
#   scripts/adk-ui.sh start | stop | status | logs
# Needs ~/.pto_service_key (the Render SERVICE_KEY) and ~/.gemini_key. Read-only: writes are never enabled here.
set -eu
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
VENV="$ROOT/.venv-adk"
PIDF="$HOME/.adk_ui.pid"
LOG="$HOME/.adk_ui.log"
PORT="${ADK_PORT:-8090}"

running() { [ -f "$PIDF" ] && kill -0 "$(cat "$PIDF")" 2>/dev/null; }

case "${1:-}" in
  start)
    if running; then echo "Already running (pid $(cat "$PIDF")) at http://localhost:$PORT"; exit 0; fi
    [ -s "$HOME/.pto_service_key" ] || { echo "Missing ~/.pto_service_key (copy SERVICE_KEY from Render > Environment)"; exit 1; }
    [ -s "$HOME/.gemini_key" ] || { echo "Missing ~/.gemini_key"; exit 1; }
    if [ ! -x "$VENV/bin/adk" ]; then
      echo "Creating $VENV (first run only)..."
      python3 -m venv "$VENV"
      "$VENV/bin/pip" install -q -r "$ROOT/examples/requirements.txt"
    fi
    cd "$ROOT/examples/adk_ui"
    PTO_SERVICE_KEY="$(cat "$HOME/.pto_service_key")" GOOGLE_API_KEY="$(cat "$HOME/.gemini_key")" \
      nohup "$VENV/bin/adk" web --host 127.0.0.1 --port "$PORT" . >"$LOG" 2>&1 &
    echo $! >"$PIDF"; chmod 600 "$PIDF"
    sleep 3
    if running; then echo "Started (pid $(cat "$PIDF")): http://localhost:$PORT  (pick pto_assistant)"; else echo "Failed to start; see $LOG"; rm -f "$PIDF"; exit 1; fi ;;
  stop)
    if running; then kill "$(cat "$PIDF")"; rm -f "$PIDF"; echo "Stopped."; else rm -f "$PIDF"; echo "Not running."; fi ;;
  status)
    if running; then echo "Running (pid $(cat "$PIDF")) at http://localhost:$PORT"; else echo "Not running."; fi ;;
  logs) tail -n 40 "$LOG" ;;
  *) echo "Usage: $0 start|stop|status|logs"; exit 2 ;;
esac
