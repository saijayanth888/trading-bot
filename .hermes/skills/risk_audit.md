---
name: risk_audit
description: Check all safety perimeters — portfolio breaker, service breakers, drawdown, exposure caps, stops-in-place. Invoked manually via /risk_audit.
trigger: manual
---

# Risk audit — 4-question safety perimeter check

Run on `/risk_audit`. One terse Slack block answering 4 questions in
<5 s: *what* state, *what's exposed*, *what changed*, *what to do*.
Matches the operator's WHAT / EXPOSURE / CHANGED / ACT format from
`_slack_helper.py`.

## Steps

1. `get_risk_status()` → `open_positions`, `trade_count`,
   `winning_trades`, `total_pnl_closed`.
2. `get_combined_portfolio()` → `total_equity`,
   `combined_drawdown_pct`, `circuit_breaker_active`,
   `combined_peak_equity`, plus any per-side staleness fields
   (`stocks_snapshot_age_seconds`, `stocks_data_stale`).
3. `get_open_trades()` → sum `stake_amount` for crypto exposure;
   count slots used vs `max_open_trades` (6 in config).
4. `get_wheel_status()` → for each `positions[]` entry, compute
   `collateral = strike * 100 * abs(qty)` for short puts; sum to get
   wheel collateral. Read `wheel.cumulative_premium_usd`.
5. Config knobs to cross-check: crypto `stoploss=-0.05`, trailing-stop
   armed in trending_up, `WHEEL_MAX_TOTAL_COLLATERAL=$30,000`, 30d-DD
   threshold `-6%`, combined breaker `10%`, stocks-data freshness `600s`.
6. Diff against prior `/risk_audit` if one exists in memory <24 h old.

## Output (Slack mrkdwn — WHAT / EXPOSURE / CHANGED / ACT)

```
:shield: *Risk Audit* · {HH:MM AM/PM ET}

*WHAT*
  Portfolio breaker:  ARMED | TRIPPED
  Service breakers:   {N_open} open / {N_total} total
  Drawdown 30d:       {X.X}%  (threshold -6%)
  Drawdown combined:  {X.X}%  (threshold 10%)

*EXPOSURE*
  Crypto stake total: ${X,XXX}   ({N}/6 open slots)
  Wheel collateral:   ${X,XXX} / $30,000 cap
  Stocks data age:    {Xs}      (limit 600s)

*CHANGED* (since last audit)
  · {if any breaker flipped, OR drawdown crossed a threshold band, OR
     exposure grew >$5k, list it here. Otherwise: "no material change."}

*ACT*
  · {explicit recommendation — see decision table below}
```

## Decision table for ACT (pick exactly one)

| Condition                                          | ACT line                                          |
|----------------------------------------------------|---------------------------------------------------|
| Breaker tripped                                    | `PAUSE: breaker tripped — inspect logs before resume.` |
| `combined_drawdown_pct ≥ 8`                        | `INSPECT: DD {X}% — within 2pp of 10% breaker.`   |
| Stocks data age > 600 s                            | `INSPECT: stocks snapshot stale {Xs} — cron stuck?` |
| Wheel collateral > 90% of cap                      | `INSPECT: wheel {X}% of cap — no new CSPs today.` |
| Crypto stake ≥ 80% of max_open_trades              | `INSPECT: {N}/6 crypto slots — near capacity.`    |
| All four checks green                              | `all clear.`                                      |

If multiple INSPECT conditions fire, list each on its own bullet (max 3).
TRIPPED breaker always wins and prints alone in bold.

## Hard rules

1. **Read-only.** This skill *audits* — it never calls `pause_trading`
   or `resume_trading`. The operator decides.
2. **Never invent thresholds.** The numbers above (-6%, 10%, $30k, 600s,
   6 slots) are the config-of-record as of 2026-05-11. If you can't
   confirm a knob's value, omit that row rather than fabricate.
3. **CHANGED section is optional.** If there's no prior audit in memory,
   write `· first audit in this window.` rather than make up a delta.
4. **Total output <20 lines.** Phone-scan first, deep-dive second.
