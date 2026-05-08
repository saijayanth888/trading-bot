---
name: regime_shift_detector
description: When the HMM regime detector reports a regime change from the previous hour, adjust open positions and document the transition.
trigger: "Regime change reported by HMM (different from previous hour)"
tools: [get_current_regime, get_regime_history, get_open_trades, get_sentiment_scores, get_onchain_signals, query_trade_journal]
---

# Regime shift response

The HMM publishes a regime label every 5 minutes; we usually only care
about transitions (regime[t] != regime[t−1h]). The strategy already
applies regime-conditional thresholds at entry/exit, but open positions
and risk levels need active adjustment when the regime flips.

## Decision matrix

| New regime → | Position-size delta | Stop adjustment | Take-profit |
|---|---|---|---|
| `trending_up` (from `mean_reverting`) | +25% on BTC/ETH only, cap 10% per pair | Widen trailing stops (−2.5%) | Let runners |
| `trending_up` (from `trending_down`) | +0% (skeptical reversal) | No change | Tighter at +1.5% |
| `trending_down` | Close any long with profit < +0.5% | Tighten to −1.5% | Skip |
| `mean_reverting` | No change | Set trailing at −1.0% | Tighter at +1.5% |
| `high_volatility` | −50% on every position | Tighten to −1.0% | At any +profit |

These are *adjustments to existing positions*; entry decisions on the
next candle are handled by the strategy's regime gates in
`config.json[regime_gating][entry_delta]`.

## Steps

1. Pull current + previous regime via `get_current_regime()` and
   `get_regime_history(days=1)`.
2. List open positions via `get_open_trades()`.
3. For each position, apply the matrix row that matches the new regime.
   Use Freqtrade's `force_exit` for closes; use `custom_stoploss` adjustments
   via the API for stops; use `force_exit_signal` with a target price for
   take-profits.
4. **Memory lookup** — query Hermes memory for previous instances of this
   exact transition (e.g. `mean_reverting → high_volatility`). If found:
     - Compare today's volume profile, sentiment score, on-chain signals
       to the historical instance.
     - Compute a similarity score (0..1) over those features.
     - If similarity ≥ 0.7 AND the historical instance was followed by
       a profitable continuation, mention this in the alert.
5. **Telegram alert** — informational priority, format:
   ```
   ↻ Regime shift: {old} → {new}
   Probability: {prob}%
   Open positions: {n_positions} ({adjustments_summary})
   Historical match: {similarity_score} ({n_historical_instances} prior)
   ```
6. Log a structured row to the journal with full transition context
   (use the journal write path the bot already exposes — *not* the MCP
   `query_trade_journal` tool which is read-only).

## Notes for Hermes

- Regime detector refits every 24h; predictions every 5 min. A "transition"
  is when the latest prediction differs from the prediction one hour earlier.
- This skill is informational + adjustment-only — it doesn't open new positions.
  Entries are still gated by the strategy's regime_gating thresholds.
- Skip the alert if the transition probability is below 0.55 (low confidence
  flip).
