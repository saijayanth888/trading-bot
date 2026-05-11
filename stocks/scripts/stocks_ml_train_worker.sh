#!/usr/bin/env bash
# stocks_ml_train_worker.sh — the long-running half of stocks_ml_train.
#
# Launched by ../../.hermes/scripts/stocks_ml_train.sh via setsid+nohup
# so it survives the Hermes cron wrapper's 120s timeout. Runs the full
# TFT training, optionally records an EPT generation off the new
# weights, then posts a completion summary to Slack.

set -euo pipefail

REPO=/home/saijayanthai/Documents/trading-bot
STOCKS=$REPO/stocks
LOG=$STOCKS/memory/cron-stocks-ml-train.log
STATUS=$STOCKS/memory/stocks-ml-status.json
PIDFILE=$STOCKS/memory/stocks-ml-train.pid

cd "$STOCKS"
# shellcheck disable=SC1091
source venv/bin/activate

set -a
# shellcheck disable=SC1091
[[ -f "$REPO/.env" ]] && source "$REPO/.env"
set +a

start=$(date -u +%s)

cleanup() {
    rc=$?
    elapsed=$(( $(date -u +%s) - start ))

    # Pull val_acc + best_epoch from the summary JSON the trainer writes.
    val_acc="?"; best_epoch="?"; n_train="?"; n_val="?"
    summary="$STOCKS/kb/models/tft/stock_tft_v1_summary.json"
    if [[ -f "$summary" ]]; then
        val_acc=$(python3 -c "import json,sys; d=json.load(open('$summary')); print(d.get('best_val_acc','?'))" 2>/dev/null || echo "?")
        best_epoch=$(python3 -c "import json,sys; d=json.load(open('$summary')); print(d.get('best_epoch','?'))" 2>/dev/null || echo "?")
        n_train=$(python3 -c "import json,sys; d=json.load(open('$summary')); print(d.get('n_train','?'))" 2>/dev/null || echo "?")
        n_val=$(python3 -c "import json,sys; d=json.load(open('$summary')); print(d.get('n_val','?'))" 2>/dev/null || echo "?")
    fi

    # Persist status — the dashboard reads this to render the Stocks ML card.
    state="ok"
    if [[ $rc -ne 0 ]]; then state="error"; fi
    finished_at=$(date -u +%Y-%m-%dT%H:%M:%SZ)
    cat >"$STATUS" <<JSON
{
  "state": "$state",
  "exit_code": $rc,
  "started_at": "$(jq -r '.started_at // ""' "$STATUS" 2>/dev/null || echo "")",
  "finished_at": "$finished_at",
  "elapsed_seconds": $elapsed,
  "best_val_acc": $( [[ "$val_acc" == "?" ]] && echo "null" || echo "$val_acc" ),
  "best_epoch":   $( [[ "$best_epoch" == "?" ]] && echo "null" || echo "$best_epoch" ),
  "n_train": $( [[ "$n_train" == "?" ]] && echo "null" || echo "$n_train" ),
  "n_val":   $( [[ "$n_val" == "?" ]] && echo "null" || echo "$n_val" )
}
JSON

    # Slack post — clean monospaced table.
    if [[ -n "${SLACK_WEBHOOK_URL:-}" ]]; then
        if [[ $rc -eq 0 ]]; then
            emoji="📊"; headline="Stocks TFT training complete"
        else
            emoji="🚨"; headline="Stocks TFT training FAILED"
        fi
        payload=$(python3 - <<PY
import json, os
text = ("$emoji *$headline*\n"
        "\`\`\`\n"
        "val_acc:     ${val_acc}\n"
        "best_epoch:  ${best_epoch}\n"
        "n_train:     ${n_train}\n"
        "n_val:       ${n_val}\n"
        "elapsed:     ${elapsed}s\n"
        "exit_code:   $rc\n"
        "\`\`\`")
print(json.dumps({"text": text}))
PY
)
        curl -fsS --max-time 10 -X POST -H 'Content-Type: application/json' \
            -d "$payload" "${SLACK_WEBHOOK_URL}" >/dev/null 2>&1 || true
    fi

    rm -f "$PIDFILE"
    echo "── stocks ML train worker exit=$rc, elapsed=${elapsed}s, val_acc=${val_acc} ──" >>"$LOG"
}
trap cleanup EXIT

echo "── stocks ML train WORKER started $(date -u +%Y-%m-%dT%H:%M:%SZ) pid=$$ ──" >>"$LOG"
# STOCKS_ML_TRAIN_ARGS lets the caller force --no-early-stop / longer epochs
# for diagnostic runs. Defaults to a normal production training cycle.
EXTRA_ARGS="${STOCKS_ML_TRAIN_ARGS:-}"
echo "── extra args: ${EXTRA_ARGS:-(none)} ──" >>"$LOG"
# shellcheck disable=SC2086
python -m shark.ml.cli train_tft --epochs 25 ${EXTRA_ARGS} 2>&1 | tee -a "$LOG"

# Optional: record an EPT generation off the freshly-trained weights.
# Best-effort — failure here doesn't fail the training run.
python -m shark.ml.cli ept_generation 2>&1 | tail -5 >>"$LOG" || true
