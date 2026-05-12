#!/usr/bin/env bash
# gpu_resume.sh — close out a reservation window. Called by cron at
# reservation_end + grace_minutes (or by ModelForge once it emits a "done"
# signal in Phase 2).
#
# Steps:
#   1. Remove ~/.hermes/state-snapshots/gpu_yielded_at.ts so other scripts
#      know the trading bot is free to use the GPU again.
#   2. Compute yield duration for the Slack message.
#   3. Slack: ":vertical_traffic_light: GPU resumed — sentiment + risk_debate
#      crons re-enabled. Yielded for {N}m."
#   4. Pre-warm hermes3:8b with a 1-token generate so the next cron caller
#      doesn't eat cold-start latency. Best-effort; failure is non-fatal.

set -uo pipefail

OLLAMA_BASE="${OLLAMA_BASE_URL:-http://localhost:11434}"
STATE_DIR="${HERMES_STATE_DIR:-$HOME/.hermes/state-snapshots}"
LOG_FILE="${HERMES_GPU_GATE_LOG:-$HOME/.hermes/logs/gpu_gate.log}"
YIELDED_TS_FILE="$STATE_DIR/gpu_yielded_at.ts"
PREWARM_MODEL="${GPU_RESUME_PREWARM_MODEL:-hermes3:8b}"

mkdir -p "$STATE_DIR" "$(dirname "$LOG_FILE")"

REPO="${TRADING_BOT_REPO:-$HOME/Documents/trading-bot}"
if [[ -f "$REPO/.env" ]]; then
    set -a
    # shellcheck disable=SC1091
    source "$REPO/.env"
    set +a
fi

ts() { date -Is; }
_log() { echo "[$(ts)] gpu_resume: $*" >> "$LOG_FILE"; }

post_slack() {
    local msg="$1"
    if [[ -z "${SLACK_WEBHOOK_URL:-}" ]]; then
        _log "no SLACK_WEBHOOK_URL — skipping post: $msg"
        return 0
    fi
    local payload
    payload=$(python3 -c 'import json,sys;print(json.dumps({"text": sys.argv[1]}))' "$msg" 2>/dev/null || echo "{\"text\":\"$msg\"}")
    curl -fsS -X POST -H "Content-Type: application/json" \
        --max-time 10 \
        -d "$payload" \
        "$SLACK_WEBHOOK_URL" >/dev/null 2>>"$LOG_FILE" || _log "slack post failed for: $msg"
}

duration_minutes() {
    if [[ ! -f "$YIELDED_TS_FILE" ]]; then
        echo "?"
        return
    fi
    local yielded_at end_at start_epoch end_epoch
    yielded_at=$(cat "$YIELDED_TS_FILE" 2>/dev/null || echo "")
    if [[ -z "$yielded_at" ]]; then
        echo "?"
        return
    fi
    start_epoch=$(date -d "$yielded_at" +%s 2>/dev/null || echo "")
    end_epoch=$(date +%s)
    if [[ -z "$start_epoch" ]]; then
        echo "?"
        return
    fi
    echo $(( (end_epoch - start_epoch) / 60 ))
}

prewarm() {
    # Best-effort 1-token generate; ignore failures.
    _log "prewarming $PREWARM_MODEL"
    local payload
    payload=$(python3 -c 'import json,sys; print(json.dumps({"model": sys.argv[1], "prompt": "ok", "stream": False, "options": {"num_predict": 1}}))' "$PREWARM_MODEL" 2>/dev/null) || {
        _log "prewarm payload build failed"
        return 0
    }
    curl -fsS --max-time 60 \
        -X POST "$OLLAMA_BASE/api/generate" \
        -H "Content-Type: application/json" \
        -d "$payload" \
        >/dev/null 2>>"$LOG_FILE" \
        && _log "prewarm OK" \
        || _log "prewarm failed (non-fatal)"
}

main() {
    _log "starting resume"
    local dur
    dur=$(duration_minutes)

    if [[ -f "$YIELDED_TS_FILE" ]]; then
        rm -f "$YIELDED_TS_FILE"
        _log "cleared yielded_at marker"
    else
        _log "no yielded_at marker — resume called without a prior yield"
    fi

    post_slack ":vertical_traffic_light: GPU resumed — sentiment + risk_debate crons re-enabled. Yielded for ${dur}m."

    prewarm

    _log "done"
    return 0
}

main "$@"
