---
name: flash_crash_defense
description: Emergency stop-loss + entry blackout when any pair drops >5% in 60 seconds OR when BTC drops >3% in 60 seconds.
trigger: "Pair drops >5% in 60s OR BTC drops >3% in 60s"
tools: [get_risk_status, get_open_trades, pause_trading, get_current_regime]
priority: critical
---

# Flash crash emergency protocol

This is the kill-switch protocol. A flash crash on BTC tends to drag the
whole crypto book; on a single pair it's usually exchange-specific (delisting,
hack, oracle failure). Either way, the response is the same: stop trading,
defend, document.

## Steps

1. **Immediate state check** — `get_risk_status()` via MCP.
2. **Drawdown approaching limit?**
   - If portfolio drawdown ≥ 7% → call `pause_trading("flash_crash_defense")`.
   - Risk governor should auto-pause at 8%, but call it ourselves at 7% to
     avoid the race.
3. **Per-position emergency stops** — for each open trade returned by
   `get_open_trades()`:
     - If the trade is on the crashing pair → close at any price now.
       (Use Freqtrade's `force_exit` API with `order_type: market` is
       acceptable here despite the strategy's default of limit-only —
       this protocol explicitly trades certainty for slippage.)
     - For other pairs → tighten stop-loss to `−1%` from the *current*
       price (not entry price) to cap further bleed.
4. **Telegram alert** — CRITICAL priority. Format:
   ```
   🚨 FLASH CRASH on {pair}: {magnitude_pct}% in 60s.
   Drawdown: {dd_pct}%. {n_positions} positions defended.
   Trading paused. Manual review required.
   ```
5. **30-minute trade blackout** — do NOT re-enter for 30 minutes regardless
   of signal quality. Hermes can hold the resume request and ignore any
   bot signals that fire during this window.
6. **Cool-down resume condition** — only re-enable trading when ALL of:
     - 30 min elapsed since the crash bar
     - 1h ATR has returned below 2× its daily average
     - Regime detector has produced a fresh `predict` (not stale from before
       the crash)
     - Operator confirms via Telegram (`/resume` command)
7. **Memory entry** — write a structured memory record with:
   ```json
   { "ts": "...", "pair": "...", "magnitude_pct": ...,
     "trigger": "flash_crash_defense",
     "positions_at_event": [...],
     "actions_taken": [...],
     "regime_before": "...", "regime_after": "...",
     "recovery_pattern": "v_shape | l_shape | continuation",
     "operator_notes": "..." }
   ```

## Notes for Hermes

- **Confirm before resuming.** Even if the technical conditions are met,
  do not auto-resume — require an operator `/resume` Telegram command.
  The 30 min cooldown is a floor, not a ceiling.
- **This skill is mutually exclusive with `squeeze_survival`** — if both
  trigger, this one wins because it implies a larger market event.
- Slack gets a delayed structured report (within 5 minutes); Telegram is
  the immediate channel.
