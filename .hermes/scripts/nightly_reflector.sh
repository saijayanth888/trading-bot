#!/usr/bin/env bash
# nightly_reflector.sh — runs scripts/nightly_reflector.py once per weekday
# evening (after market close). Writes 2-4 sentence post-mortems for the
# day's closed trades to stocks/memory/decisions.md via Qwen3-30B-A3B
# through Ollama.
#
# Cron: 30 21 * * 1-5 (21:30 ET on weekdays)
# Wired in ~/.hermes/cron/jobs.json with no_agent=true.
#
# This wrapper deliberately:
#   - Sources the unified .env so POSTGRES_PASSWORD / SLACK_WEBHOOK_URL
#     / OLLAMA_BASE_URL are present.
#   - Forces POSTGRES_HOST=localhost / POSTGRES_PORT=5434 because the
#     trade_journal table lives in the host-port-forwarded TimescaleDB.
#   - Always exits 0 — the Python script handles its own error paths and
#     posts to Slack on failure. Cron should not alarm.
#
# To install (one-time, after ollama is ready):
#   ollama pull qwen3:30b
#   cp .hermes/scripts/nightly_reflector.sh ~/.hermes/scripts/
#   chmod +x ~/.hermes/scripts/nightly_reflector.sh
#   # Then add the jobs.json entry described in HANDOFF.md and reload Hermes.

set -uo pipefail

REPO=/home/saijayanthai/Documents/trading-bot
LOG=$REPO/stocks/memory/cron-reflector.log
mkdir -p "$(dirname "$LOG")"

set -a
# shellcheck disable=SC1091
[[ -f "$REPO/.env" ]] && source "$REPO/.env"
set +a

export POSTGRES_HOST="${POSTGRES_HOST:-localhost}"
export POSTGRES_PORT="${POSTGRES_PORT:-5434}"
export OLLAMA_BASE_URL="${OLLAMA_BASE_URL:-http://localhost:11434}"

cd "$REPO"

ts=$(date -u +%Y-%m-%dT%H:%M:%SZ)
{
    echo "── nightly_reflector $ts ──"
    /home/saijayanthai/Documents/spark/envs/ml-env/bin/python3 \
        scripts/nightly_reflector.py "$@"
    echo "── exit=$? ──"
} >> "$LOG" 2>&1

# Cron MUST exit 0 even on inner errors — the script Slack-alerts itself.
exit 0
