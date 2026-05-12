#!/usr/bin/env bash
# reapply_hermes_patches.sh — re-apply our local Hermes customizations.
#
# Run this AFTER every `hermes update` to restore:
#   1. Cron worker-pool dispatch (fixes the tick-lock-blocks-stocks-crons bug)
#   2. DIAG stack-trace logging in gateway/run.py:stop()
#
# If patches are already applied (detected by marker greps), this exits
# cleanly with no changes. Idempotent.
#
# Why we need this:
#   `hermes update` runs `git pull --ff-only` on ~/.hermes/hermes-agent
#   which overwrites local edits. Our two customizations are NOT yet
#   upstreamed, so they get wiped on every update. This script
#   detects + re-applies + restarts the gateway.
#
# Usage:
#   bash scripts/reapply_hermes_patches.sh

set -euo pipefail

# AUDIT 2026-05-12 Critical #1: $HOME-relative paths replace hardcoded ones.
REPO="${TRADING_BOT_REPO:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." 2>/dev/null && pwd)}"
[[ -d "$REPO/user_data" ]] || REPO="$HOME/Documents/trading-bot"
HERMES_REPO="${HERMES_REPO:-$HOME/.hermes/hermes-agent}"
PATCHES_DIR="$REPO/hermes_patches"

log() { printf '[reapply-hermes %s] %s\n' "$(date '+%H:%M:%S')" "$1"; }

if [[ ! -d "$HERMES_REPO/.git" ]]; then
    echo "ERROR: $HERMES_REPO is not a git repo" >&2
    exit 1
fi

if [[ ! -d "$PATCHES_DIR" ]]; then
    echo "ERROR: patches dir $PATCHES_DIR missing" >&2
    exit 1
fi

# Marker detection — these strings appear ONLY in our patches.
SCHEDULER="$HERMES_REPO/cron/scheduler.py"
GATEWAY="$HERMES_REPO/gateway/run.py"

scheduler_patched=false
gateway_diag_patched=false
gateway_workerpool_patched=false

if grep -q "_workdir_exec_lock" "$SCHEDULER" 2>/dev/null; then
    scheduler_patched=true
fi
if grep -q "DIAG stop() invoked" "$GATEWAY" 2>/dev/null; then
    gateway_diag_patched=true
fi
if grep -q "_cron_worker_loop" "$GATEWAY" 2>/dev/null; then
    gateway_workerpool_patched=true
fi

log "Pre-check: scheduler_patched=$scheduler_patched"
log "Pre-check: gateway_diag_patched=$gateway_diag_patched"
log "Pre-check: gateway_workerpool_patched=$gateway_workerpool_patched"

if $scheduler_patched && $gateway_diag_patched && $gateway_workerpool_patched; then
    log "All patches already applied — nothing to do."
    exit 0
fi

cd "$HERMES_REPO"

# Apply each .patch via git am if working tree is clean, else use patch -p1
# as a fallback (won't create commits but applies the diff).
if [[ -n "$(git status --porcelain)" ]]; then
    log "WARNING: working tree dirty — using 'patch -p1' fallback (no commits)"
    USE_PATCH=true
else
    USE_PATCH=false
fi

for patch in "$PATCHES_DIR"/*.patch; do
    [[ -f "$patch" ]] || continue
    log "Applying $(basename "$patch")"
    if $USE_PATCH; then
        if patch -p1 --dry-run --silent < "$patch" >/dev/null 2>&1; then
            patch -p1 --silent < "$patch"
            log "  ✓ applied (no commit)"
        else
            log "  ⚠ already applied or conflicts — skipping"
        fi
    else
        if git am --3way "$patch" 2>&1 | tail -3; then
            log "  ✓ applied (committed)"
        else
            log "  ⚠ am failed — aborting and trying patch -p1"
            git am --abort 2>/dev/null || true
            if patch -p1 --dry-run --silent < "$patch" >/dev/null 2>&1; then
                patch -p1 --silent < "$patch"
                log "  ✓ patch -p1 applied"
            else
                log "  ✗ ALSO failed — manual intervention needed for $(basename "$patch")"
            fi
        fi
    fi
done

# Sanity: run syntax check on modified files before restart
log "Syntax check..."
if "$HERMES_REPO/venv/bin/python" -c "
import ast
for f in ['$SCHEDULER', '$GATEWAY']:
    ast.parse(open(f).read())
" 2>&1; then
    log "  ✓ syntax OK"
else
    log "  ✗ SYNTAX ERROR — NOT restarting gateway. Inspect manually."
    exit 2
fi

log "Restarting gateway..."
systemctl --user restart hermes-gateway.service
sleep 5
state=$(systemctl --user is-active hermes-gateway.service)
log "Gateway state: $state"

if [[ "$state" == "active" ]]; then
    log "DONE. Worker-pool log marker:"
    sleep 3
    tail -50 "$HOME/.hermes/logs/agent.log" 2>/dev/null \
        | grep -E "Cron ticker started" | tail -1 | sed 's/^/    /'
    exit 0
else
    log "Gateway did not become active. Check: systemctl --user status hermes-gateway.service"
    exit 3
fi
