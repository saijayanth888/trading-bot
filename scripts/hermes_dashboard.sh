#!/usr/bin/env bash
#
# Start / stop / status the Hermes Agent web dashboard.
#
# Usage:
#   ./scripts/hermes_dashboard.sh start    # background launch on 127.0.0.1:9119
#   ./scripts/hermes_dashboard.sh stop
#   ./scripts/hermes_dashboard.sh status
#   ./scripts/hermes_dashboard.sh tail
#
# The dashboard is *not* exposed on 0.0.0.0 by default — Hermes' own
# `--insecure` flag is required for that, and we deliberately don't pass
# it here. Reach it from the host via http://localhost:9119 or from
# another machine via SSH port-forward (`ssh -L 9119:localhost:9119 spark`).

set -euo pipefail

HERMES="${HOME}/.local/bin/hermes"
LOG="/tmp/hermes_dashboard.log"
PORT="${HERMES_DASHBOARD_PORT:-9119}"
HOST="${HERMES_DASHBOARD_HOST:-127.0.0.1}"

if [[ ! -x "$HERMES" ]]; then
    echo "[hermes] binary missing at $HERMES — run the installer first" >&2
    exit 1
fi

case "${1:-start}" in
    start)
        if "$HERMES" dashboard --status 2>&1 | grep -q "running"; then
            echo "[hermes-dashboard] already running"
            "$HERMES" dashboard --status
            exit 0
        fi
        echo "[hermes-dashboard] launching on http://${HOST}:${PORT}"
        nohup "$HERMES" dashboard --tui --no-open \
            --host "$HOST" --port "$PORT" \
            >"$LOG" 2>&1 &
        echo "[hermes-dashboard] PID $!  log → $LOG"
        sleep 3
        "$HERMES" dashboard --status || true
        ;;
    stop)
        "$HERMES" dashboard --stop || true
        ;;
    status)
        "$HERMES" dashboard --status || true
        ;;
    tail)
        tail -f "$LOG"
        ;;
    *)
        echo "usage: $0 {start|stop|status|tail}" >&2
        exit 2
        ;;
esac
