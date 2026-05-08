---
name: squeeze_survival
description: When the trading bot detects a >3% price move in under 5 minutes on any active pair, protect open positions and avoid stepping into volatility.
trigger: "Price move >3% in under 5 minutes on any active pair"
tools: [get_open_trades, get_current_regime, get_risk_status, query_trade_journal]
---

# Squeeze survival protocol

A 3% candle in 5 minutes on a major pair (BTC/ETH/SOL/ADA) is almost
always a liquidation cascade or news-driven shock. The model's signals
become unreliable in those windows, so the protocol is conservative:
defend what's open, refuse to chase, document the event.

## Steps

1. **Pull state** — call `get_open_trades()` and `get_current_regime()` via MCP.
2. **Open position on the squeezing pair?**
   - **Yes + profit ≥ +1%** — tighten stop-loss to capture 50% of current profit.
     (Use Freqtrade's `force_exit` with limit-order at `entry_rate × (1 + 0.5 × current_profit_pct)`
     OR call the bot's `custom_stoploss` adjustment via the API.)
   - **Yes + loss ≥ −2%** — close immediately at limit-near-bid; do not let it
     widen to the configured `stoploss: -5%` floor.
   - **Yes + within ±2% of entry** — leave it alone, the regular stop-loss handles it.
3. **No open position on the squeezing pair** — DO NOT enter. Wait for ATR(5m)
   to fall back below 2× its 1h SMA (typically 15-30 min after the impulse).
4. **Log the event** — INSERT into `trade_journal` is read-only via MCP, so
   instead log via a `pause_trading` reason payload that mentions the squeeze,
   or have Hermes write a memory entry with: `{ts, pair, magnitude_pct,
   action_taken, open_positions_at_event}`.
5. **Reversal check** — once volatility settles (>15 min of <0.5% candles),
   inspect the regime. Only consider a reversal entry if regime is
   `mean_reverting` AND the meta-agent confidence ≥ 0.70. Otherwise stand down.

## Notes for Hermes

- This protocol overrides normal entry conditions — call `pause_trading` with
  reason `squeeze_survival_<pair>` if you can't confidently apply the stop
  adjustments via the freqtrade API alone.
- Use Telegram for the alert, not Slack — operators usually want immediate
  visibility on these events.
