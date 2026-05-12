#!/usr/bin/env bash
# gpu_yield_now.sh — evict all Ollama models from VRAM so ModelForge's weekly
# LoRA training has the full DGX Spark. Called by cron 5 minutes before each
# reservation start (see user_data/config/recommended_crons_gpu_reservation.txt).
#
# Behavior:
#   1. Snapshot resident models via /api/ps
#   2. For each model: POST /api/generate with keep_alive=0 + empty prompt
#      (Ollama's supported way to force eviction without restarting the daemon)
#   3. Verify eviction via /api/ps. Retry once with `ollama stop` if needed.
#   4. Sleep 10s for VRAM to settle.
#   5. Post Slack notification listing evicted models.
#   6. Write timestamp to ~/.hermes/state-snapshots/gpu_yielded_at.ts so the
#      resume hook can report duration.
#
# Exit codes:
#   0 — eviction completed (or no models were resident)
#   1 — at least one model could not be evicted after retry

set -uo pipefail

OLLAMA_BASE="${OLLAMA_BASE_URL:-http://localhost:11434}"
STATE_DIR="${HERMES_STATE_DIR:-$HOME/.hermes/state-snapshots}"
LOG_FILE="${HERMES_GPU_GATE_LOG:-$HOME/.hermes/logs/gpu_gate.log}"
YIELDED_TS_FILE="$STATE_DIR/gpu_yielded_at.ts"

mkdir -p "$STATE_DIR" "$(dirname "$LOG_FILE")"

# Source .env if present so SLACK_WEBHOOK_URL is available
REPO="${TRADING_BOT_REPO:-$HOME/Documents/trading-bot}"
if [[ -f "$REPO/.env" ]]; then
    set -a
    # shellcheck disable=SC1091
    source "$REPO/.env"
    set +a
fi

ts() { date -Is; }
_log() { echo "[$(ts)] gpu_yield_now: $*" >> "$LOG_FILE"; }

post_slack() {
    local msg="$1"
    if [[ -z "${SLACK_WEBHOOK_URL:-}" ]]; then
        _log "no SLACK_WEBHOOK_URL — skipping post: $msg"
        return 0
    fi
    # Slack expects {"text": "..."} — escape backslashes/quotes/newlines minimally
    local payload
    payload=$(python3 -c 'import json,sys;print(json.dumps({"text": sys.argv[1]}))' "$msg" 2>/dev/null || echo "{\"text\":\"$msg\"}")
    curl -fsS -X POST -H "Content-Type: application/json" \
        --max-time 10 \
        -d "$payload" \
        "$SLACK_WEBHOOK_URL" >/dev/null 2>>"$LOG_FILE" || _log "slack post failed for: $msg"
}

list_resident_models() {
    # /api/ps returns {"models":[{"name":"hermes3:8b",...},...]}
    curl -fsS --max-time 5 "$OLLAMA_BASE/api/ps" 2>>"$LOG_FILE" \
        | python3 -c 'import json,sys
try:
    data = json.load(sys.stdin)
    for m in data.get("models", []):
        n = m.get("name") or m.get("model")
        if n: print(n)
except Exception as e:
    sys.stderr.write(f"parse error: {e}\n")
    sys.exit(0)
' 2>>"$LOG_FILE"
}

evict_model() {
    local name="$1"
    # Use Ollama's keep_alive=0 trick — empty prompt + immediate eviction
    curl -fsS --max-time 15 \
        -X POST "$OLLAMA_BASE/api/generate" \
        -H "Content-Type: application/json" \
        -d "$(python3 -c 'import json,sys; print(json.dumps({"model": sys.argv[1], "keep_alive": 0, "prompt": "", "stream": False}))' "$name")" \
        >/dev/null 2>>"$LOG_FILE"
}

main() {
    _log "starting yield"
    local models
    models=$(list_resident_models)
    if [[ -z "$models" ]]; then
        _log "no models resident"
        ts > "$YIELDED_TS_FILE"
        post_slack ":zzz: GPU yield triggered — no Ollama models resident (already idle). ModelForge training window begins now."
        return 0
    fi

    _log "resident models: $(echo "$models" | tr '\n' ',' | sed 's/,$//')"

    local evicted=()
    local failed=()
    while IFS= read -r m; do
        [[ -z "$m" ]] && continue
        _log "evicting $m"
        evict_model "$m" || _log "evict_model api call failed for $m"
    done <<< "$models"

    # Wait briefly for Ollama to release VRAM
    sleep 2

    # Verify — retry stragglers with `ollama stop`
    local still
    still=$(list_resident_models)
    if [[ -n "$still" ]]; then
        _log "after first pass, still resident: $(echo "$still" | tr '\n' ',' | sed 's/,$//')"
        while IFS= read -r m; do
            [[ -z "$m" ]] && continue
            _log "retry via ollama stop $m"
            if command -v ollama >/dev/null 2>&1; then
                ollama stop "$m" >>"$LOG_FILE" 2>&1 || _log "ollama stop failed for $m"
            else
                _log "ollama CLI not in PATH — cannot retry $m"
            fi
        done <<< "$still"
    fi

    # Final settle
    sleep 10

    local final
    final=$(list_resident_models)
    while IFS= read -r m; do
        [[ -z "$m" ]] && continue
        failed+=("$m")
    done <<< "$final"

    # Compute evicted = original models minus failed
    while IFS= read -r m; do
        [[ -z "$m" ]] && continue
        local was_failed=0
        for f in "${failed[@]:-}"; do
            [[ "$m" == "$f" ]] && was_failed=1
        done
        if [[ "$was_failed" == "0" ]]; then
            evicted+=("$m")
        fi
    done <<< "$models"

    ts > "$YIELDED_TS_FILE"

    local evicted_csv="${evicted[*]:-none}"
    evicted_csv="${evicted_csv// /, }"

    if [[ "${#failed[@]}" -gt 0 ]]; then
        local failed_csv="${failed[*]}"
        failed_csv="${failed_csv// /, }"
        _log "DONE — evicted: $evicted_csv | FAILED: $failed_csv"
        post_slack ":warning: GPU yield partial — ModelForge training window begins now. Evicted: ${evicted_csv}. Could not evict: ${failed_csv} (operator may need to restart ollama)."
        return 1
    fi

    _log "DONE — evicted: $evicted_csv"
    post_slack ":zzz: GPU yielded — ModelForge training window begins now. Evicted: ${evicted_csv}."
    return 0
}

main "$@"
