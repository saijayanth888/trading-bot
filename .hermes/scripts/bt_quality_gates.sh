#!/usr/bin/env bash
# bt_quality_gates.sh — weekly strategy promotion-gate runner.
#
# Schedule: Sunday 4:00 AM ET (after the 11pm Sat/3am Sun TFT retrain) so
# any new model weights are in play when we backtest. Crontab line:
#
#   0 4 * * 0 $HOME/.hermes/scripts/bt_quality_gates.sh \
#       >>$HOME/.hermes/logs/bt_quality_gates.log 2>&1
#
# What this does
# --------------
#   1. Walks user_data/strategies/ for active strategy class names.
#   2. For each strategy, runs scripts/backtest_with_gates.py over a
#      2-year timerange (default 20240501-20260501; override with
#      $BT_GATES_TIMERANGE).
#   3. The Python wrapper writes:
#        user_data/backtest_results/gates_report_<strategy>_<TS>.json
#        user_data/backtest_results/gates_report_<strategy>_latest.json
#      The dashboard endpoint /api/ops/backtest_gates surfaces the latest.
#   4. Compares this week's promotion_eligible flag against last week's
#      saved state (~/.hermes/state-snapshots/bt_gates_<strategy>.json).
#      If a strategy regressed (was eligible, now not), POSTs a Slack
#      alert. Improvements (now eligible) are also alerted, but at a
#      friendlier severity.
#   5. POSTs the dashboard a no-op GET to warm the endpoint cache.
#
# What this does NOT do
# ---------------------
#   * It never auto-flips a strategy to live trading. Promotion is an
#     operator decision; this script only surfaces "the recommendation
#     changed" via Slack + the dashboard card.
#   * It never overwrites the freqtrade running config — backtests are
#     read-only against historical candles.

set -uo pipefail

# REPO defaults to two levels up from this script (so installed copies under
# $HOME/.hermes/scripts/ keep working when REPO is set in cron env). Override
# with TRADING_BOT_REPO=/abs/path if your checkout isn't at $HOME/.../trading-bot.
REPO="${TRADING_BOT_REPO:-${REPO:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." 2>/dev/null && pwd)}}"
[[ -d "$REPO/user_data" ]] || REPO="$HOME/Documents/trading-bot"
PY="$REPO/scripts/backtest_with_gates.py"
RESULTS_DIR="$REPO/user_data/backtest_results"
STATE_DIR="$HOME/.hermes/state-snapshots"
LOG_DIR="$HOME/.hermes/logs"
mkdir -p "$STATE_DIR" "$LOG_DIR" "$RESULTS_DIR"

# Source the repo .env for SLACK_WEBHOOK_URL and any python-side overrides.
set -a
# shellcheck disable=SC1091
[[ -f "$REPO/.env" ]] && source "$REPO/.env"
set +a

TIMERANGE="${BT_GATES_TIMERANGE:-20240501-20260501}"
CONFIG_PATH="${BT_GATES_CONFIG:-$REPO/user_data/config.json}"
ITERS="${BT_GATES_BOOTSTRAP_ITERS:-1000}"
WINDOWS="${BT_GATES_WALK_FORWARD_WINDOWS:-6}"

ts=$(date -u +%Y-%m-%dT%H:%M:%SZ)
echo "── bt_quality_gates $ts ──"
echo "  repo=$REPO  timerange=$TIMERANGE"

# Discover active strategy class names from user_data/strategies/*.py.
# We grep `class <Name>(IStrategy)` so we don't have to maintain a
# separate strategy registry. Pure-bash, no Python interpreter needed.
mapfile -t STRATEGIES < <(
    grep -hE '^class [A-Za-z_][A-Za-z0-9_]+\(IStrategy\)' \
        "$REPO"/user_data/strategies/*.py 2>/dev/null \
        | sed -E 's/^class ([A-Za-z_][A-Za-z0-9_]+)\(IStrategy\).*/\1/' \
        | sort -u
)

if [[ ${#STRATEGIES[@]} -eq 0 ]]; then
    echo "  no strategies discovered — nothing to gate."
    msg=":warning: *[bt_quality_gates]* discovered 0 strategies in $REPO/user_data/strategies/ — nothing to gate."
    if [[ -n "${SLACK_WEBHOOK_URL:-}" ]]; then
        curl -fsS -X POST -H "Content-Type: application/json" \
            -d "{\"text\":\"$msg\"}" "$SLACK_WEBHOOK_URL" >/dev/null || true
    fi
    exit 0
fi

echo "  strategies: ${STRATEGIES[*]}"

regressions=()
promotions=()
errors=()

for STRAT in "${STRATEGIES[@]}"; do
    echo
    echo "── $STRAT ──"
    state_file="$STATE_DIR/bt_gates_${STRAT}.json"

    # Read previous eligibility from the state file (if any).
    prev_elig=""
    if [[ -f "$state_file" ]]; then
        prev_elig=$(python3 -c "import json,sys; print(json.load(open(sys.argv[1])).get('promotion_eligible',''))" "$state_file" 2>/dev/null || echo "")
    fi

    # Run the wrapper. Redirect freqtrade's noisy stdout to the log; we
    # still see the Python summary print on stdout (the wrapper writes
    # the JSON summary at the end).
    if ! python3 "$PY" \
        --strategy "$STRAT" \
        --timerange "$TIMERANGE" \
        --config "$CONFIG_PATH" \
        --results-dir "$RESULTS_DIR" \
        --bootstrap-iters "$ITERS" \
        --walk-forward-windows "$WINDOWS" \
        --cwd "$REPO" \
        --quiet
    then
        rc=$?
        # rc=1 → at least one gate failed but report was still written.
        # rc=2 → infrastructure error; no report.
        if [[ $rc -eq 2 ]]; then
            errors+=("$STRAT")
            continue
        fi
    fi

    latest="$RESULTS_DIR/gates_report_${STRAT}_latest.json"
    if [[ ! -f "$latest" ]]; then
        errors+=("$STRAT")
        continue
    fi
    new_elig=$(python3 -c "import json,sys; print(json.load(open(sys.argv[1])).get('promotion_eligible',''))" "$latest" 2>/dev/null || echo "")
    echo "  prev_eligible=$prev_elig  new_eligible=$new_elig"

    # Save current state for next week's diff.
    cp "$latest" "$state_file"

    # Detect transitions (only if we had a prior reading — first run is silent).
    if [[ "$prev_elig" == "True" && "$new_elig" == "False" ]]; then
        regressions+=("$STRAT")
    elif [[ "$prev_elig" == "False" && "$new_elig" == "True" ]]; then
        promotions+=("$STRAT")
    fi
done

# Warm the dashboard endpoint cache so the first operator visit after the
# cron renders fresh data without a fetch round-trip.
curl -fsS "http://localhost:8081/api/ops/backtest_gates" >/dev/null 2>&1 || true

# ── Slack summary ──
if [[ -z "${SLACK_WEBHOOK_URL:-}" ]]; then
    echo "  no SLACK_WEBHOOK_URL — skipping Slack post"
    exit 0
fi

et_now=$(TZ=America/New_York date "+%Y-%m-%d %H:%M %Z")

# Severity:
#   regression → :rotating_light: (operator should investigate before next week's live trades)
#   promotion  → :white_check_mark: (worth reviewing a manual flip to live)
#   errors     → :warning:
#   else (no transitions, all clean) → :bell: low-priority status pulse

lines=()
if [[ ${#regressions[@]} -gt 0 ]]; then
    icon=":rotating_light:"
    sev="REGRESSION"
elif [[ ${#promotions[@]} -gt 0 ]]; then
    icon=":white_check_mark:"
    sev="NEW PROMOTION CANDIDATE"
elif [[ ${#errors[@]} -gt 0 ]]; then
    icon=":warning:"
    sev="ERRORS"
else
    icon=":bell:"
    sev="OK"
fi

# Signal-only: only post when something changed or errored.
# Operator has the BacktestGatesLive dashboard card for the no-op view.
if [[ ${#regressions[@]} -gt 0 || ${#promotions[@]} -gt 0 || ${#errors[@]} -gt 0 ]]; then
    lines+=("$icon *[bt_quality_gates]* · *$sev* · $et_now")
    lines+=("strategies tested: ${STRATEGIES[*]}")
    [[ ${#regressions[@]} -gt 0 ]] && lines+=(":rotating_light: REGRESSED (was promotion-eligible, now not): ${regressions[*]}")
    [[ ${#promotions[@]} -gt 0 ]] && lines+=(":white_check_mark: NEW eligibility: ${promotions[*]}  ←  consider manual flip")
    [[ ${#errors[@]} -gt 0 ]] && lines+=(":warning: backtest errored: ${errors[*]}")
    lines+=("see /ops · BacktestGatesLive card for the per-gate breakdown")

    # Compose & post
    msg=$(printf '%s\\n' "${lines[@]}")
    curl -fsS -X POST -H "Content-Type: application/json" \
        -d "$(python3 -c 'import json,sys; print(json.dumps({"text": sys.argv[1]}))' "$msg")" \
        "$SLACK_WEBHOOK_URL" >/dev/null || echo "  slack post failed"
else
    echo "  bt_quality_gates: no transitions (OK) — skipping Slack post (use dashboard)"
fi

echo "── bt_quality_gates done $(date -u +%Y-%m-%dT%H:%M:%SZ) ──"
exit 0
