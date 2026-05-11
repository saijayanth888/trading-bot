---
name: trade_review
description: Review last N closed trades — pattern, win/loss split, exit reasons, regime correlations. Invoked manually via /trade_review or with a window arg like /trade_review 7d.
trigger: manual
---

# Trade review — closed-trade pattern audit

Run on `/trade_review [window]`. Default = **24 h**. Accepts `Nd`,
`Nh`, or `Ntrades` (count → no time filter, just `LIMIT N`). One terse
Slack block — win-rate, exit-reason mix, regime correlation, one
actionable insight in <5 s.

## Pull via `query_trade_journal` (3 queries)

```sql
-- Headline rows
SELECT pnl, pnl_pct, duration_min, regime, exit_reason
FROM trade_journal
WHERE closed_at > NOW() - INTERVAL '{window}';

-- Exit-reason mix (top 6)
SELECT exit_reason, COUNT(*) AS n,
       ROUND(SUM(pnl)::numeric,2) AS total_pnl,
       ROUND(AVG(pnl)::numeric,2) AS avg_pnl
FROM trade_journal WHERE closed_at > NOW() - INTERVAL '{window}'
GROUP BY exit_reason ORDER BY n DESC LIMIT 6;

-- Regime mix (top 5)
SELECT regime, COUNT(*) AS n, ROUND(SUM(pnl)::numeric,2) AS total_pnl,
       ROUND(100.0*SUM(CASE WHEN pnl>0 THEN 1 ELSE 0 END)/COUNT(*),0)
         AS win_rate_pct
FROM trade_journal WHERE closed_at > NOW() - INTERVAL '{window}'
GROUP BY regime ORDER BY n DESC LIMIT 5;
```

Compute: `total`, `wins`, `losses`, `win_rate`, `total_pnl_usd`,
`avg_pnl_usd`, `avg_held_min` from query 1.

## Output (Slack mrkdwn)

```
:bar_chart: *Trade Review* · {window} · {HH:MM AM/PM ET}

*RESULTS*
  Total:  {N} trades  ({W}W / {L}L)  win rate {X}%
  P&L:    {±$pnl}  (avg {±$avg} per trade)
  Held:   avg {Z} min

*EXIT REASONS*
  reason          count   total_pnl   avg_pnl
  ─────────────   ─────   ─────────   ───────
  {reason}        {n}     {±$X}       {±$Y}

*BY REGIME*
  regime          count   total_pnl   win_rate
  ─────────────   ─────   ─────────   ────────
  {regime}        {n}     {±$X}       {X}%

*PATTERN*
  · {1-2 sentence insight naming a config knob the operator owns}
```

## PATTERN — pick one (else "no clear pattern")

- All losers exited via `freqai_down_regime` <60 min → name
  `exit_delta.trending_down` (now `-0.05` post 2026-05-11; loosen
  further only if pattern persists).
- Stoploss >50% of losses → review `stoploss = -0.05` / trailing.
- One regime drives >70% of losses → tighten its `entry_delta`.
- TFT-only mode + win rate <40% → flag possible TFT drift.
- None of the above → "no actionable pattern — looks like noise."

## Hard rules

1. Read-only — never auto-apply config.
2. `total == 0` in window → output `_no closed trades in {window} — try a wider window._` and stop.
3. Cap PATTERN at 2 sentences; tables at 6 rows.
