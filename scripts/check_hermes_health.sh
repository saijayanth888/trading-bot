#!/usr/bin/env bash
# check_hermes_health.sh — one-shot status check for all Hermes services.
#
# Usage:
#   bash scripts/check_hermes_health.sh
#
# Output: a compact report of every Hermes-related service + heartbeat.

set -u

bold() { printf '\033[1m%s\033[0m\n' "$1"; }
ok() { printf '  \033[32m✓\033[0m %s\n' "$1"; }
bad() { printf '  \033[31m✗\033[0m %s\n' "$1"; }
warn() { printf '  \033[33m⚠\033[0m %s\n' "$1"; }

bold "═══ Hermes health · $(date '+%Y-%m-%d %H:%M:%S %Z') ═══"

# 1. User gateway
bold "Gateway (user)"
state=$(systemctl --user is-active hermes-gateway.service 2>&1)
nrestarts=$(systemctl --user show hermes-gateway.service --property=NRestarts --value)
pid=$(systemctl --user show hermes-gateway.service --property=MainPID --value)
case "$state" in
    active) ok "state=$state · PID=$pid · NRestarts=$nrestarts" ;;
    activating) warn "state=$state (transitional)" ;;
    *) bad "state=$state · NRestarts=$nrestarts" ;;
esac
if [[ "$pid" != "0" && -d "/proc/$pid" ]]; then
    uptime=$(ps -o etime= -p "$pid" 2>/dev/null | xargs)
    [[ -n "$uptime" ]] && printf '    uptime: %s\n' "$uptime"
fi

# 2. System gateway (should NOT exist after the 2026-05-11 cleanup)
bold "Gateway (system) — should be gone post-cleanup"
if [[ -f /etc/systemd/system/hermes-gateway.service ]]; then
    bad "unit file STILL exists at /etc/systemd/system/hermes-gateway.service"
    bad "  → this WILL cause restart cycles. Remove with: sudo rm /etc/systemd/system/hermes-gateway.service && sudo systemctl daemon-reload"
else
    ok "unit file removed (cannot cause ping-pong)"
fi

# 3. Heartbeat file (what dashboard reads)
REPO="${TRADING_BOT_REPO:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." 2>/dev/null && pwd)}"
[[ -d "$REPO/user_data" ]] || REPO="$HOME/Documents/trading-bot"
HB="$REPO/user_data/state/hermes-gateway.alive"
bold "Heartbeat file"
if [[ -f "$HB" ]]; then
    age=$(( $(date +%s) - $(stat -c %Y "$HB") ))
    content=$(cat "$HB")
    case "$content" in
        active|activating|reloading) ok "content='$content' · age=${age}s" ;;
        *) bad "content='$content' · age=${age}s" ;;
    esac
else
    bad "missing: $HB"
fi

# 4. MCP + dashboard
bold "Sibling services"
for svc in hermes-mcp.service hermes-dashboard.service; do
    # User-scope check first (dashboard is user-scope)
    if systemctl --user is-active "$svc" --quiet 2>/dev/null; then
        ok "$svc (user) active"
    elif systemctl is-active "$svc" --quiet 2>/dev/null; then
        ok "$svc (system) active"
    else
        bad "$svc not active"
    fi
done

# 5. Hermes patches present
bold "Local patches (lost on \`hermes update\` if not reapplied)"
HERMES_REPO="${HERMES_REPO:-$HOME/.hermes/hermes-agent}"
SCHED="$HERMES_REPO/cron/scheduler.py"
GW="$HERMES_REPO/gateway/run.py"
if grep -q "_workdir_exec_lock" "$SCHED" 2>/dev/null; then
    ok "scheduler worker-pool patch present"
else
    bad "scheduler worker-pool patch MISSING — run: bash scripts/reapply_hermes_patches.sh"
fi
if grep -q "_cron_worker_loop" "$GW" 2>/dev/null; then
    ok "gateway worker-pool patch present"
else
    bad "gateway worker-pool patch MISSING — run: bash scripts/reapply_hermes_patches.sh"
fi
if grep -q "DIAG stop() invoked" "$GW" 2>/dev/null; then
    ok "DIAG stack-trace logging present"
else
    bad "DIAG logging MISSING — run: bash scripts/reapply_hermes_patches.sh"
fi

# 6. Recent Telegram noise
bold "Telegram shutdown noise (last hour)"
log="${HERMES_AGENT_LOG:-$HOME/.hermes/logs/agent.log}"
if [[ -f "$log" ]]; then
    since=$(date -d '1 hour ago' '+%Y-%m-%d %H:%M')
    count=$(awk -v cut="$since" '$0 >= cut && /Sent shutdown notification/' "$log" | wc -l)
    if (( count == 0 )); then
        ok "0 shutdown notifications in last hour"
    elif (( count <= 2 )); then
        warn "$count shutdown notifications in last hour (manual restart?)"
    else
        bad "$count shutdown notifications — gateway is cycling"
    fi
fi

echo
bold "Done · run again any time: bash scripts/check_hermes_health.sh"
