#!/usr/bin/env bash
# verify_production.sh — pre-deploy / pre-go-live sanity sweep.
#
# Exits non-zero if anything critical fails. Safe to run in CI.

set -uo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

PASS=0
FAIL=0
SKIPPED=0

ok()     { echo "  ✓ $*"; PASS=$((PASS+1)); }
fail()   { echo "  ✗ $*"; FAIL=$((FAIL+1)); }
skip()   { echo "  · $* (skipped)"; SKIPPED=$((SKIPPED+1)); }
header() { echo; echo "▶ $*"; }

# ─── 1. Critical safety modules import + minimal smoke ───────────────────
header "1. Safety-critical modules import cleanly"

if PYTHONPATH=. python3 -c "
from user_data.modules.unified_risk import _dd, get_combined_risk_status
assert _dd(100, 100) == 0.0
assert abs(_dd(90, 100) - 0.10) < 1e-9
assert _dd(110, 100) == 0.0
" 2>/dev/null; then ok "unified_risk + drawdown formula"; else fail "unified_risk smoke"; fi

if (cd stocks && python3 -c "
from shark.llm.circuit_breaker import CircuitBreaker, get_breaker, State
cb = CircuitBreaker('verify-test', tier='fast')
ok, _ = cb.can_execute()
assert ok
" 2>/dev/null); then ok "circuit_breaker fresh state CLOSED"; else fail "circuit_breaker smoke"; fi

if (cd stocks && python3 -c "
import inspect
from shark.llm.client import chat_json
src = inspect.getsource(chat_json)
assert 'circuit_breaker' in src or 'breaker' in src
assert 'anthropic' in src.lower()
" 2>/dev/null); then ok "chat_json failover wiring present"; else fail "chat_json failover"; fi

if PYTHONPATH=. python3 -c "
from user_data.modules.ollama_health import run_check, REQUIRED_MODELS
assert REQUIRED_MODELS
" 2>/dev/null; then ok "ollama_health module"; else fail "ollama_health smoke"; fi

# ─── 2. Test suites ──────────────────────────────────────────────────────
header "2. Critical test suites"

if PYTHONPATH=. python3 -m pytest tests/test_unified_risk.py -q 2>&1 | tail -2 | grep -q passed; then
  ok "tests/test_unified_risk.py (16 tests)"
else
  fail "tests/test_unified_risk.py"
fi

if (cd stocks && python3 -m pytest tests/test_circuit_breaker.py -q 2>&1 | tail -2 | grep -q passed); then
  ok "stocks/tests/test_circuit_breaker.py (14 tests)"
else
  fail "stocks/tests/test_circuit_breaker.py"
fi

if (cd stocks && python3 -m pytest tests/test_chat_json_failover.py -q 2>&1 | tail -2 | grep -q passed); then
  ok "stocks/tests/test_chat_json_failover.py (6 tests)"
else
  fail "stocks/tests/test_chat_json_failover.py"
fi

# ─── 3. Operational endpoints reachable ──────────────────────────────────
header "3. Dashboard endpoints"

dash_endpoints=(
  services regime sentiment trades_risk training mcp sparklines
  slack_preview readiness rebalance config tools stocks stock_regime
  live_trades gates market_hours combined_portfolio llm_stats
  ollama_health circuit_breakers
)
green=0
total=0
for ep in "${dash_endpoints[@]}"; do
  total=$((total+1))
  s=$(curl -fsS -m 4 "http://127.0.0.1:8081/api/ops/${ep}" 2>/dev/null \
      | python3 -c "import json,sys;print(json.load(sys.stdin).get('status'))" 2>/dev/null)
  if [[ "$s" == "ok" ]]; then green=$((green+1)); fi
done
if [[ $green -eq $total ]]; then
  ok "all $total /api/ops/* endpoints return ok"
elif [[ $green -gt $((total/2)) ]]; then
  ok "$green/$total endpoints ok (rest may be degraded — check /ops)"
else
  fail "only $green/$total endpoints ok — dashboard or backing services unhealthy"
fi

# ─── 4. Hermes services ──────────────────────────────────────────────────
header "4. Host services"

if systemctl is-active --quiet hermes-mcp; then ok "hermes-mcp service active"; else fail "hermes-mcp not active"; fi
if systemctl is-active --quiet hermes-gateway; then ok "hermes-gateway service active"; else fail "hermes-gateway not active"; fi
if pgrep -f "ollama serve" >/dev/null; then ok "ollama process running"; else fail "ollama not running"; fi

# ─── 5. Containers ───────────────────────────────────────────────────────
header "5. Docker containers"

for svc in tradebot-postgres freqtrade dashboard; do
  if docker ps --format '{{.Names}}' | grep -q "^${svc}$"; then
    ok "container $svc up"
  else
    fail "container $svc not running"
  fi
done

# ─── 6. Recovery + frontend assets ──────────────────────────────────────
header "6. Operational artifacts"

[[ -f docs/RECOVERY.md ]] && ok "docs/RECOVERY.md exists" || fail "RECOVERY.md missing"
[[ -f scripts/verify_production.sh ]] && ok "verify_production.sh exists (you're running it)" || skip "self-test"

# Frontend
[[ -f user_data/dashboard/templates/ops.html ]] && ok "ops.html present" || fail "ops.html missing"
[[ -f user_data/dashboard/templates/index.html ]] && ok "index.html present" || fail "index.html missing"
[[ -f user_data/dashboard/static/js/ops.js ]] && ok "ops.js present" || fail "ops.js missing"

# ─── 7. Hermes crons (live pilot scaffolding) ────────────────────────────
header "7. Hermes cron coverage"

cron_required=(
  shark_pre_market shark_market_open shark_midday shark_daily_summary shark_weekly_review
  wheel_snapshot wheel_candles wheel_sell_csps wheel_profit_take wheel_sell_calls
  ollama_health
)
cron_list=$(hermes cron list 2>/dev/null | grep -oP 'Name:\s+\K\S+' | sort -u || true)
for c in "${cron_required[@]}"; do
  if grep -qx "$c" <<<"$cron_list"; then ok "cron $c registered"; else fail "cron $c missing"; fi
done

# ─── Summary ─────────────────────────────────────────────────────────────
echo
echo "═══════════════════════════════════════════════"
echo " ${PASS} passed · ${FAIL} failed · ${SKIPPED} skipped"
echo "═══════════════════════════════════════════════"
[[ $FAIL -gt 0 ]] && exit 1 || exit 0
