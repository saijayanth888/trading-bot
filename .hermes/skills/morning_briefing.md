---
name: morning_briefing
description: At market open or first thing in the operator's day, summarize bot state, overnight P&L, open positions, and what to watch. Invoked manually via /morning_briefing.
trigger: manual
---

# Morning briefing — open-of-day operator brief

Run this when the operator types `/morning_briefing`. Goal: one terse,
phone-readable Slack block in <5 s that answers "what did the bot do
overnight and what should I keep an eye on today?".

## Steps

1. `get_combined_portfolio()` → pull `total_equity`,
   `combined_drawdown_pct`, `combined_peak_equity`,
   `circuit_breaker_active`, and any per-side fields you find.
2. `get_open_trades()` → list of crypto positions with `pair`,
   `profit_pct`, `profit_abs`, `stake_amount`.
3. `get_wheel_status()` → `wheel.open_short_puts`,
   `wheel.open_covered_calls`, `wheel.shares_held`, plus `positions[]`
   (each has `kind`, `underlying`, `strike`, `expiry`, `entry_credit`).
4. `get_current_regime()` → `regime` + `duration_hours`.
5. `get_regime_history(days=1)` → count of transitions in last 24 h,
   note the most-common label.
6. `get_daily_pnl(days=7)` → compute yesterday's row (`day == today−1`)
   and the 7-day sum.

## Output format (Slack mrkdwn, bold headers, phone-readable)

```
:sunrise: *Morning Briefing* · {HH:MM AM/PM ET}

*ACCOUNT*
  Equity:    ${total_equity:,.0f}   (DD {combined_drawdown_pct:.2f}% from peak)
  Yesterday: {±$pnl}   ({wins}W / {losses}L)
  7d trend:  {±$sum}   ({total_wins}W / {total_losses}L)
  Breaker:   ARMED | TRIPPED

*POSITIONS*
  Crypto ({N} open):
    · {PAIR} {profit_pct:+.2f}% ({±$profit_abs})
  Wheel ({M} open):
    · {underlying} {kind_short} ${strike} {expiry} +${entry_credit}

*REGIME*
  Now:       {regime} (held {duration_hours:.1f}h)
  Last 24h:  {N_transitions} transitions (most-common = {label})

*WATCH TODAY*
  · {1-2 specific things to keep an eye on}
```

## What goes in WATCH TODAY (pick 1-2, never more)

- If any crypto position is within 1% of stop-loss → call it out.
- If any short put strike is within 2% of underlying → assignment-risk
  warning ("SOFI assignment risk if dips below $X").
- If regime just flipped to `trending_up` (transition < 4 h old) →
  "expect new entries this session".
- If `combined_drawdown_pct > 5` → "DD approaching 6% breaker — review
  before adding".
- If breaker tripped → put it first, in bold, as the top line.
- If wheel cron next-fire today (Mon-Fri 11:00 ET) and `wheel_collateral
  < $30k cap` → "wheel scan eligible today".

## Hard rules

1. **No prose paragraphs.** Bullets and short labelled rows only.
2. **Never invent numbers.** If a tool returns empty/error, write
   "no data" in that row — don't fabricate.
3. **Cap WATCH TODAY at 2 items.** Operator attention is finite.
4. **All timestamps 12-hour AM/PM ET** (match the operator's preferred
   Slack-cron format documented in `_slack_helper.py`).
5. **Total skill output <25 lines.** This is a phone-scan brief, not a
   report.
