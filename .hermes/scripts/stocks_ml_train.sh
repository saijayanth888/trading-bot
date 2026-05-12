#!/usr/bin/env bash
# stocks_ml_train.sh — train the stocks TFT on the full S&P 500 historical
# bars. Runs Sunday 11 PM ET so weights are ready before Monday's open.
#
# Detached design: Hermes cron wrappers enforce a 120s script timeout,
# which kills a 10-minute TFT training mid-flight (we hit this on
# 2026-05-10 — 4 of 25 epochs completed before SIGKILL). We work
# around it by spawning the training as a detached background process
# via setsid + nohup, so it survives the wrapper's kill. The cron
# script returns immediately with "started" status; the training runs
# to completion in the background and writes its own Slack/Telegram
# notification when done. Slack handles the completion report from the
# detached worker — there's nothing for the cron wrapper to wait on.

set -euo pipefail

# REPO defaults to two levels up from this script (so installed copies under
# $HOME/.hermes/scripts/ keep working when REPO is set in cron env). Override
# with TRADING_BOT_REPO=/abs/path if your checkout isn't at $HOME/.../trading-bot.
REPO="${TRADING_BOT_REPO:-${REPO:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." 2>/dev/null && pwd)}}"
[[ -d "$REPO/user_data" ]] || REPO="$HOME/Documents/trading-bot"
STOCKS=$REPO/stocks
LOG=$STOCKS/memory/cron-stocks-ml-train.log
STATUS=$STOCKS/memory/stocks-ml-status.json
PIDFILE=$STOCKS/memory/stocks-ml-train.pid

# If a previous training is still running, do nothing.
if [[ -f "$PIDFILE" ]] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
    echo "stocks ML train: prior run pid=$(cat "$PIDFILE") still active — skipping"
    exit 0
fi

cd "$STOCKS"
set -a
# shellcheck disable=SC1091
[[ -f "$REPO/.env" ]] && source "$REPO/.env"
set +a

# Detach the worker. setsid puts it in its own process group so the
# cron wrapper's group-kill at 120s doesn't reach it.
setsid nohup bash "$STOCKS/scripts/stocks_ml_train_worker.sh" \
    >>"$LOG" 2>&1 </dev/null &
WORKER_PID=$!
echo "$WORKER_PID" >"$PIDFILE"
disown "$WORKER_PID" 2>/dev/null || true

started_at=$(date -u +%Y-%m-%dT%H:%M:%SZ)
cat >"$STATUS" <<JSON
{
  "state": "running",
  "pid": $WORKER_PID,
  "started_at": "$started_at",
  "epochs_target": 25
}
JSON

echo "🚀 Stocks TFT training launched detached (pid=$WORKER_PID, target=25 epochs)"
echo "    log:    $LOG"
echo "    status: $STATUS"
echo "    Slack/Telegram notification fires from the worker on completion."
exit 0
