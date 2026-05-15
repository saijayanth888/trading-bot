#!/usr/bin/env bash
# stocks_day_runner.sh — fire stocks crons on schedule today, bypassing Hermes.
#
# Why this exists:
#   Hermes scheduler holds .tick.lock while LLM-driven crons (risk_monitor_15min,
#   market_research_30min) make 10-30 min calls to hermes3:70b. During that
#   window, no other crons can fire — script-only stocks crons get silently
#   fast-forwarded past their grace windows.
#
#   See ~/.hermes/hermes-agent/cron/scheduler.py and the 09:24:11 fast-forward
#   evidence in ~/.hermes/logs/agent.log for the full trace.
#
# This runner is a no-cost shim: it sleeps until each scheduled time, then
# fires the matching .sh script. Designed to run in the background with
# `nohup` until market close (16:00 ET).
#
# Usage:
#   nohup bash scripts/stocks_day_runner.sh > /tmp/stocks_day_runner.log 2>&1 &
#
# Or via Monitor for streaming notifications.

set -euo pipefail

# Hermes installs its scripts under $HOME/.hermes/scripts by default.
# REPO defaults to one level up from this script; override TRADING_BOT_REPO
# in cron env if you keep the checkout somewhere else.
HERMES_SCRIPTS="${HERMES_SCRIPTS:-$HOME/.hermes/scripts}"
REPO="${TRADING_BOT_REPO:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." 2>/dev/null && pwd)}"
[[ -d "$REPO/user_data" ]] || REPO="$HOME/Documents/trading-bot"
LOG="$REPO/stocks/memory/stocks_day_runner.log"

mkdir -p "$(dirname "$LOG")"

log() {
    printf '[%s ET] %s\n' "$(TZ=America/New_York date '+%H:%M:%S')" "$1" | tee -a "$LOG"
}

# Schedule for today (Mon-Fri only — guard against accidental weekend runs)
dow=$(TZ=America/New_York date '+%u')  # 1=Mon..7=Sun
if [ "$dow" -gt 5 ]; then
    log "weekend — exiting (no stocks crons fire Sat/Sun)"
    exit 0
fi

# Today's schedule: [HH:MM:script]
# Mirrors ~/.hermes/cron/jobs.json wheel_* and shark_* entries for weekdays.
schedule=(
    # Pre-market context
    "09:00:shark_pre_market.sh"
    # Candle refresh — every 5 min, but the runner just fires twice (operator
    # can call manually for finer cadence if needed)
    "09:05:wheel_candles.sh"
    "09:30:wheel_snapshot.sh"
    "09:35:shark_market_open.sh"
    "09:45:wheel_snapshot.sh"
    "10:00:wheel_snapshot.sh"
    "10:00:wheel_profit_take.sh"
    "10:15:wheel_snapshot.sh"
    "10:30:wheel_snapshot.sh"
    "10:45:wheel_snapshot.sh"
    "11:00:wheel_snapshot.sh"
    "11:00:wheel_sell_calls.sh"     # Monday-only — sell covered calls
    "11:15:wheel_snapshot.sh"
    "11:30:wheel_snapshot.sh"
    "11:45:wheel_snapshot.sh"
    "12:00:wheel_snapshot.sh"
    "12:15:wheel_snapshot.sh"
    "12:30:wheel_snapshot.sh"
    "12:45:wheel_snapshot.sh"
    "13:00:wheel_snapshot.sh"
    "13:00:shark_midday.sh"
    "13:15:wheel_snapshot.sh"
    "13:30:wheel_snapshot.sh"
    "13:45:wheel_snapshot.sh"
    "14:00:wheel_snapshot.sh"
    "14:00:wheel_profit_take.sh"    # afternoon profit-take pass
    "14:15:wheel_snapshot.sh"
    "14:30:wheel_snapshot.sh"
    "14:45:wheel_snapshot.sh"
    "15:00:wheel_snapshot.sh"
    "15:15:wheel_snapshot.sh"
    "15:30:wheel_snapshot.sh"
    "15:30:wheel_candles.sh"        # final candle refresh before close
    "15:45:wheel_snapshot.sh"
    "16:00:wheel_snapshot.sh"       # market close snapshot
    "17:30:shark_daily_summary.sh"
    "21:30:shark_kb_update.sh"
)

now_hm=$(TZ=America/New_York date '+%H:%M')

# Skip past slots — they've already passed; operator either already fired
# them manually or accepts the miss.
log "stocks_day_runner started · now=$now_hm ET · day=$dow"

for entry in "${schedule[@]}"; do
    slot=${entry%%:*}
    script=${entry#*:*:}
    slot=${entry%%:*}
    rest=${entry#*:}
    slot_h=${slot}
    slot_m=${rest%%:*}
    script_name=${rest#*:}
    target="${slot_h}:${slot_m}"

    if [[ "$target" < "$now_hm" ]]; then
        log "skip $target $script_name (already past)"
        continue
    fi

    # Sleep until target time
    target_epoch=$(TZ=America/New_York date -d "today $target" +%s)
    now_epoch=$(date +%s)
    sleep_s=$((target_epoch - now_epoch))
    if [ "$sleep_s" -gt 0 ]; then
        log "wait ${sleep_s}s until $target for $script_name"
        sleep "$sleep_s"
    fi

    script_path="$HERMES_SCRIPTS/$script_name"
    if [ ! -x "$script_path" ]; then
        log "ERROR $script_name not found or not executable: $script_path"
        continue
    fi

    log "FIRING $script_name"
    if bash "$script_path" >>"$LOG" 2>&1; then
        log "OK $script_name"
    else
        log "FAIL $script_name (exit=$?)"
    fi
    now_hm=$(TZ=America/New_York date '+%H:%M')
done

log "stocks_day_runner finished — last script run at $now_hm ET"
