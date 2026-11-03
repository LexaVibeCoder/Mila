#!/usr/bin/env bash
# Launch the Mila Observatory web panel (Linux / macOS counterpart of
# Mila_Observatory.bat).
#
# Starts Mila_observatory.server in the background using the project venv,
# waits until the panel answers, then opens the configured URL in a browser.
# Host and port come from .env (OBSERVATORY_HOST / OBSERVATORY_PORT); the
# defaults are 127.0.0.1:8080.
#
# Usage:
#   ./Mila_Observatory.sh             # start in background + open browser
#   ./Mila_Observatory.sh --stop      # stop a panel started by this script
#   ./Mila_Observatory.sh --foreground  # run attached to the terminal (Ctrl-C to stop)
#
# Output goes to logs/observatory.log; the PID is written to logs/observatory.pid.
set -u -o pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$SCRIPT_DIR"
VENV_PY="$PROJECT_ROOT/.venv/bin/python"
LOG_DIR="$PROJECT_ROOT/logs"
LOG_FILE="$LOG_DIR/observatory.log"
PID_FILE="$LOG_DIR/observatory.pid"

info() { echo "--> $*"; }
die() {
    echo "ERROR: $*" >&2
    exit 1
}

MODE="background"
case "${1:-}" in
    --stop)
        MODE="stop"
        ;;
    --foreground|-f)
        MODE="foreground"
        ;;
    "" )
        ;;
    *)
        die "unknown argument: $1 (use --stop or --foreground)"
        ;;
esac

[ -x "$VENV_PY" ] || die "venv not found at $VENV_PY
       Run ./start/install.sh first."

if [ ! -f "$PROJECT_ROOT/Mila_observatory/frontend/dist/index.html" ]; then
    info "Frontend is not built; the panel will start without UI."
    info "Build it: cd '$PROJECT_ROOT/Mila_observatory/frontend' && npm ci && npm run build"
    info "Or use Docker: ./start/docker.sh"
fi

# Ports/address come from .env; never hardcoded here.
panel_url() {
    "$VENV_PY" - <<'PY' 2>/dev/null
from config import settings
from Mila_observatory.auth import resolve_host

host = resolve_host()
# A server bound to 0.0.0.0 is reachable on loopback from this machine.
if host in ("0.0.0.0", "::"):
    host = "127.0.0.1"
print(f"http://{host}:{settings.OBSERVATORY_PORT}")
PY
}

is_running() {
    [ -f "$PID_FILE" ] || return 1
    local pid
    pid="$(cat "$PID_FILE" 2>/dev/null)" || return 1
    [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null
}

stop_panel() {
    if ! is_running; then
        info "Observatory is not running (no live process in $PID_FILE)"
        rm -f "$PID_FILE"
        return 0
    fi
    local pid
    pid="$(cat "$PID_FILE")"
    info "Stopping Observatory (pid $pid)"
    kill "$pid" 2>/dev/null || true
    for _ in $(seq 1 20); do
        kill -0 "$pid" 2>/dev/null || break
        sleep 0.25
    done
    if kill -0 "$pid" 2>/dev/null; then
        info "Process did not exit, sending SIGKILL"
        kill -9 "$pid" 2>/dev/null || true
    fi
    rm -f "$PID_FILE"
    info "Stopped"
}

open_browser() {
    local url="$1"
    if command -v xdg-open >/dev/null 2>&1; then
        xdg-open "$url" >/dev/null 2>&1 &
    elif command -v open >/dev/null 2>&1; then
        open "$url" >/dev/null 2>&1 &
    elif command -v wslview >/dev/null 2>&1; then
        wslview "$url" >/dev/null 2>&1 &
    else
        info "Open this URL manually: $url"
    fi
}

wait_for_panel() {
    local url="$1"
    if command -v curl >/dev/null 2>&1; then
        for _ in $(seq 1 40); do
            curl -fsS -o /dev/null --max-time 1 "$url/api/health" 2>/dev/null && return 0
            sleep 0.5
        done
    else
        sleep 3
        return 0
    fi
    return 1
}

case "$MODE" in
    stop)
        stop_panel
        exit 0
        ;;
    foreground)
        cd "$PROJECT_ROOT" || die "cannot cd to $PROJECT_ROOT"
        info "Starting Observatory in the foreground (Ctrl-C to stop)"
        exec "$VENV_PY" -m Mila_observatory.server
        ;;
esac

cd "$PROJECT_ROOT" || die "cannot cd to $PROJECT_ROOT"
mkdir -p "$LOG_DIR"

if is_running; then
    info "Observatory is already running (pid $(cat "$PID_FILE"))"
else
    # nohup keeps the panel alive after the terminal closes.
    nohup "$VENV_PY" -m Mila_observatory.server >>"$LOG_FILE" 2>&1 &
    PANEL_PID=$!
    echo "$PANEL_PID" >"$PID_FILE"
    info "Starting Observatory (pid $PANEL_PID), log: $LOG_FILE"
fi

URL="$(panel_url)" || URL=""
if [ -z "$URL" ]; then
    echo "ERROR: cannot resolve Observatory URL from .env." >&2
    echo "       Check OBSERVATORY_HOST/OBSERVATORY_PORT or run start/check_config.py." >&2
    exit 1
fi

if ! wait_for_panel "$URL"; then
    if [ -f "$PID_FILE" ] && ! is_running; then
        echo "ERROR: Observatory exited during startup. Last log lines:" >&2
        tail -n 20 "$LOG_FILE" >&2 || true
        rm -f "$PID_FILE"
        exit 1
    fi
    echo "WARNING: panel is not answering at $URL yet; see $LOG_FILE" >&2
fi

info "Panel: $URL"
open_browser "$URL"
info "Stop it with: $SCRIPT_DIR/$(basename "${BASH_SOURCE[0]}") --stop"
