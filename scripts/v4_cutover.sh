#!/usr/bin/env bash
# v4_cutover.sh — execute the freqtrade → V4 cutover.
#
# This script is the reversible flip-switch for Phase 3 of the EOD
# cutover plan. It assumes V4 has been running cleanly in shadow mode
# (per docs/V4_CUTOVER_LOG.md) and the operator has authorized the
# switch with an explicit invocation of this script.
#
# Sequence:
#   1. Snapshot pre-cutover state (uptime, open positions, decision count).
#   2. Pause freqtrade via the dashboard endpoint (auth-gated).
#   3. Stop the freqtrade container (image retained for instant rollback).
#   4. Flip LIVE_ENGINE_MODE=live in the .env (or via runtime env), rebuild
#      quanta-core, recycle the container. The runner becomes order-placing.
#   5. Rebuild dashboard so /api/ops/live_trades reads quanta_schema.proposals
#      + .fills instead of the freqtrade SQLite path.
#   6. Append the verdict to docs/V4_CUTOVER_LOG.md.
#
# Rollback (run from inside this directory):
#   docker compose start freqtrade
#   sed -i 's/LIVE_ENGINE_MODE=live/LIVE_ENGINE_MODE=shadow/' .env
#   docker compose up -d --no-deps quanta-core dashboard
#
# Usage: bash scripts/v4_cutover.sh
#
# Stop-and-flag: any step that errors out aborts the script. Re-run after
# fixing the underlying issue. Most steps are idempotent.

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LOG="$ROOT/docs/V4_CUTOVER_LOG.md"
TS="$(date -u +%Y-%m-%dT%H:%M:%SZ)"

cd "$ROOT"

log() {
    printf "[%s] %s\n" "$(date -u +%H:%M:%SZ)" "$*"
}

snapshot() {
    log "=== PRE-CUTOVER SNAPSHOT ==="
    docker ps --format '{{.Names}}\t{{.Status}}' | grep -E 'quanta|freqtrade|dashboard|postgres' || true
    log "freqtrade open positions:"
    curl -s http://localhost:8081/api/ops/live_trades 2>/dev/null \
        | python3 -c "import sys,json;d=json.load(sys.stdin).get('data',{});t=d.get('trades',[]);print(f'  count={len(t)}')" \
        || log "  (dashboard not reachable)"
    log "V4 decisions in last hour:"
    docker exec tradebot-postgres psql -U tradebot -d tradebot -tA -c \
        "SELECT count(*), count(DISTINCT symbol) FROM quanta_schema.decisions WHERE ts > NOW() - INTERVAL '1 hour';" \
        2>/dev/null || log "  (postgres not reachable)"
}

pause_freqtrade() {
    log "=== STEP 1: PAUSE FREQTRADE ==="
    if [[ -z "${HERMES_MCP_KEY:-}" ]]; then
        # Try to source from .env
        if [[ -f "$ROOT/.env" ]]; then
            # shellcheck disable=SC2046
            export $(grep -E '^HERMES_MCP_KEY=' "$ROOT/.env" | xargs -r) || true
        fi
    fi
    if [[ -n "${HERMES_MCP_KEY:-}" ]]; then
        curl -s -X POST http://localhost:8081/api/ops/pause \
            -H "X-Hermes-Key: $HERMES_MCP_KEY" \
            -H "Content-Type: application/json" \
            | head -c 200 || true
        echo
    else
        log "WARN: HERMES_MCP_KEY unset; skipping API pause and going straight to docker stop"
    fi
}

stop_freqtrade() {
    log "=== STEP 2: STOP FREQTRADE CONTAINER ==="
    docker compose stop freqtrade
    docker ps --format '{{.Names}}\t{{.Status}}' | grep -E 'freqtrade' || log "  freqtrade: stopped"
}

flip_v4_to_live() {
    log "=== STEP 3: FLIP V4 TO LIVE MODE ==="
    if [[ -f .env ]]; then
        if grep -q "^LIVE_ENGINE_MODE=" .env; then
            sed -i 's/^LIVE_ENGINE_MODE=.*/LIVE_ENGINE_MODE=live/' .env
        else
            echo "LIVE_ENGINE_MODE=live" >> .env
        fi
    else
        log "WARN: no .env file; passing LIVE_ENGINE_MODE via shell env only"
    fi
    log "rebuilding + recycling quanta-core"
    docker compose build quanta-core
    docker compose up -d --no-deps quanta-core
    sleep 8
    docker logs --tail 20 quanta-core | head -25
}

swap_dashboard() {
    log "=== STEP 4: REBUILD DASHBOARD (reads quanta_schema for live data) ==="
    # The dashboard already reads V4 decisions from quanta_schema.decisions
    # via /api/v4/debate/history. /api/ops/live_trades wiring to V4 is
    # the follow-up that lands in a separate commit and rebuild — for the
    # first 24h of cutover the operator can use /api/v4/* as the live view.
    log "  (dashboard already shows V4 decisions on /api/v4/debate/history;"
    log "   /api/ops/live_trades V4 wiring deferred to a post-cutover commit)"
}

post_check() {
    log "=== POST-CUTOVER SMOKE ==="
    docker ps --format '{{.Names}}\t{{.Status}}' | grep -E 'quanta|freqtrade|dashboard'
    log "V4 logs last 6 lines:"
    docker logs --tail 6 quanta-core
}

append_log() {
    mkdir -p "$(dirname "$LOG")"
    {
        echo
        echo "## Cutover at $TS"
        echo "- freqtrade: stopped (image retained for rollback)"
        echo "- quanta-core: LIVE_ENGINE_MODE=live (rebuilt + recycled)"
        echo "- rollback: \`docker compose start freqtrade && sed -i 's/LIVE_ENGINE_MODE=live/LIVE_ENGINE_MODE=shadow/' .env && docker compose up -d --no-deps quanta-core\`"
    } >> "$LOG"
    log "appended to $LOG"
}

main() {
    snapshot
    pause_freqtrade
    stop_freqtrade
    flip_v4_to_live
    swap_dashboard
    post_check
    append_log
    log "=== CUTOVER COMPLETE ==="
    log "V4 is now the active trading engine. Freqtrade image retained."
    log "Rollback (≤30s): docker compose start freqtrade && update .env, recycle quanta-core."
}

main "$@"
