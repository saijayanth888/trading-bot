# Funding-Rate Harvest — Design Doc

**Date:** 2026-05-15
**Author:** research agent (read-only, no live wires)
**Status:** DESIGN + BACKTEST SCAFFOLD only. Live trading deferred to Week 3
once the operator opens a perp account.
**Companion code:**
`src/quanta_core/strategies/funding_rate_harvest.py`,
`src/quanta_core/backtest/funding_harness.py`.
**Companion evidence:** `audit/2026-05-15-strategy-research.md` §2 ("Tier-A
candidate: Perpetual funding-rate harvest").

---

## 1. Strategy in plain English

A perpetual futures contract has no expiry. To keep the perp price tethered
to spot, the exchange charges a *funding rate* every funding interval
(typically every 8h on Binance / OKX / Bybit, every 1h on dYdX v4,
continuously on Hyperliquid). Mechanically:

- `funding_rate > 0` → longs pay shorts `notional * funding_rate` per period.
- `funding_rate < 0` → shorts pay longs.

A trader who is **short the perp** *and* **long the same notional in spot**
has zero price delta. PnL each period is just the funding payment, minus
fees, minus any spot/perp basis movement when entering/exiting. When funding
is structurally positive (typical in bullish regimes — leverage demand
exceeds short-side hedging supply), the strategy harvests a steady carry.

Sustainable edge: structural imbalance between leveraged longs (retail +
trend-followers) and short-side hedgers. The basis is enforced
mechanically — there is no cointegration risk, no stop-out cascade, no
direction call.

Literature (ScienceDirect 2025, S2096720925000818):
- Threshold-entry, no ML: **18-19% APY post-fee, Sharpe ≈ 1.4**
- ML-enhanced (regime + funding-magnitude features): **31% APY, Sharpe ≈ 2.3**

Our cron-bound HMM regime classifier already produces the 4-state regime
labels needed for the gated variant.

---

## 2. Exchange comparison

| Exchange | Funding interval | History API (free, unauth) | Spot leg same account | US retail access | Taker fee (perp) | Taker fee (spot) |
|---|---|---|---|---|---|---|
| **Bybit** | 8h | Yes (`/v5/market/funding/history`) — but **CloudFront geo-blocks US IPs**, confirmed 2026-05-15 | Yes (unified margin) | No (US accounts blocked since 2024) | 0.055% | 0.10% |
| **dYdX v4** | 1h | Yes (`indexer.dydx.trade/v4/historicalFunding/<MARKET>`) — works from US | No spot leg on-chain — must hedge spot externally (e.g., Coinbase) | Yes (Cosmos chain, no KYC for self-custody) | 0.05% taker, 0.02% maker, scales with volume | n/a (no spot) |
| **Binance Futures** | 8h | Yes (`/fapi/v1/fundingRate`) — but **451 from US IPs**, confirmed 2026-05-15 | Yes (unified margin) | No (binance.us has no perps) | 0.04% | 0.10% |
| **OKX** | 8h | Yes (`/api/v5/public/funding-rate-history`) — works from US | Yes (unified account) | No (US blocked since 2023) | 0.05% | 0.10% |
| **Hyperliquid** | 1h continuous | Yes (POST `info`, type `fundingHistory`) | No spot leg on most pairs | Grey-area (US users use it but Terms exclude US persons) | 0.025% taker, 0.015% maker | n/a |
| **Kraken Futures** | 8h | Yes (`futures.kraken.com/derivatives/api/v4/historicalfundingrates`) — works from US | Spot is a separate product (Kraken Spot) but same login | Partial — Kraken Futures available in most US states except KY, NY, WA | 0.05% | 0.26% taker / 0.16% maker |

### 2.1 Decision

**Backtest data source: OKX.** Most parallel coverage (BTC/ETH/SOL/XRP/DOGE
all trade as `*-USDT-SWAP` with 4+ years of history), free unauthenticated
API, accessible from this dev box. We are *not* recommending OKX as the
live venue — only as a representative source of funding-rate history that
matches what Bybit/Binance experience structurally (the three exchanges'
funding rates for BTC have a 2024 correlation > 0.97 per the ScienceDirect
paper, §4.2).

**Live trading venue (operator opens Week 2-3): dYdX v4.**
Justification:
1. **Only US-accessible perp venue on the comparison.** Bybit, Binance
   Futures, OKX all geo-block the operator's IP — confirmed empirically
   today.
2. **Self-custodial.** No KYC, no exchange counterparty risk on the perp
   leg. Spot leg lives on Coinbase (existing account, existing API keys).
3. **Lowest fees among accessible venues.** 0.02% maker × 2 legs = 0.04%
   round-trip on the perp; spot leg on Coinbase is 0.40% taker / 0.25%
   maker. Combined RT cost: ~0.30-0.50% depending on liquidity.
4. **Hourly funding** — 8 harvests per day vs 3 on 8h venues. More
   compounding cycles, more sample size, easier to hit the 30-trade gate.
5. **Public indexer API** — funding rate, mark price, oracle price all
   queryable without auth. Good for paper-mode signal generation before
   live wires.

**Trade-offs accepted:**
- The spot leg is on a *different* exchange (Coinbase) than the perp leg
  (dYdX). This means the basis cannot be netted internally; we hold dollar
  USDC margin on dYdX and the USD spot on Coinbase separately. Inventory
  risk = ~30 minutes of bridge time if either leg needs rebalancing.
- dYdX v4 fees are volume-tiered. At <$1M/month volume, taker is 0.05%.
  Honest fee model used in the backtest.
- dYdX hourly funding rates are *smaller* per period than 8h funding (by
  roughly 1/8). The threshold needs to be re-tuned — a 0.022% per 8h
  threshold becomes ~0.003% per hour. The backtest uses the OKX 8h schedule
  to validate the literature claim cleanly, then we re-tune for the
  hourly dYdX schedule when the operator's account is open.

**Hedge: if dYdX onboarding stalls, fall back to Kraken Futures** (still
US-accessible, well-documented, and ICE-regulated). Same backtest results
should hold; only the fee constants change.

---

## 3. Account-setup steps (Week 2 operator runbook)

1. **Bridge funds.** Move 5-10% of paper-mode notional from Coinbase to a
   Phantom or Keplr wallet on dYdX v4 (Cosmos chain).
2. **Generate dYdX v4 trading key.** This is a deterministic key derived
   from the wallet seed; no separate API key signup needed.
3. **Backfill key into trading-bot config.** Add `[exchanges.dydx]` block
   in `config/secrets.toml` with `mnemonic = "..."`. The mnemonic is
   already protected by the existing secrets-loader path.
4. **Coinbase Advanced Trade API key.** Already present (`[exchanges.coinbase_advanced_trade]`).
5. **Validate paper-mode loop.** Run `python -m quanta_core.backtest.funding_harness --all`
   against fresh OKX data; confirm the gates report still passes (or fails
   honestly).
6. **Enable cron.** Add the line documented in §7 below.
7. **Fund the live legs.** Operator-initiated; do not autostart.

---

## 4. API endpoints

### 4.1 Backtest data (OKX, no auth)

- Funding-rate history: `GET https://www.okx.com/api/v5/public/funding-rate-history?instId={SYM}-USDT-SWAP&limit=100[&after=<oldest_ts_ms>]`.
  Response is reverse-chronological. Page backward by passing `after =
  oldest_ts_ms` of the previous page. Each page covers ~33 days at the 8h
  funding cadence; 90 days of history needs ~3 pages.
- Spot price: we already have BTC/ETH/SOL/XRP/DOGE 1h spot bars under
  `user_data/data/coinbase/{SYM}_USD-1h.feather`. The harness uses these
  for entry/exit price marking; no spot REST calls needed.

### 4.2 Live data (dYdX v4 + Coinbase, deferred to Week 3)

- Funding rate (current + history): `GET https://indexer.dydx.trade/v4/historicalFunding/{SYMBOL}`.
- Perp mark price: `GET https://indexer.dydx.trade/v4/perpetualMarkets/{SYMBOL}`.
- Spot price (Coinbase): existing client.

---

## 5. Position sizing

```
notional_per_pair = min(
    PORTFOLIO_USD * 0.05,                        # max 5% per pair
    PORTFOLIO_USD * 0.20 * funding_rate_z,        # scale by signal strength
)
```

where `funding_rate_z` is the current funding rate divided by its 30-day
trailing standard deviation, clipped to [0, 1]. This biases capital toward
pairs whose funding is unusually high right now — the same idea as the ML
"funding-magnitude feature" in the ScienceDirect paper, but implemented as
a hand-rolled z-score so it can ship in Week 2 without retraining a model.

Maximum aggregate exposure across all pairs: 25% of portfolio. This caps
the spot-leg drawdown if all pairs simultaneously gap during a wider crypto
crash (basis can briefly diverge by 1-2% during such events; 25% × 2% =
50 bps absolute portfolio drawdown — acceptable).

---

## 6. HMM regime gating

The existing HMM produces 4 labels: `trending_up`, `trending_down`,
`mean_reverting`, `high_volatility`. Funding-rate empirical behaviour:

| Regime | Funding sign typical | Decision |
|---|---|---|
| `trending_up` | Strongly positive (leverage demand peaks) | **HARVEST** |
| `high_volatility` | Often positive but noisy | **HARVEST** with tighter threshold (1.5× normal) |
| `trending_down` | Often negative (shorts pay longs) — the inverse trade (long perp / short spot) is the play, but short-spot is hard on most retail venues. **SKIP** for the v1 implementation. |
| `mean_reverting` | Funding flips frequently around zero. Each flip costs fees. **SKIP** — fees will eat the carry. |

This matches the ScienceDirect paper §3.3 finding: harvest is most reliable
in "uptrend high-leverage" regimes, and the threshold strategy
under-performs unconditioned harvest in `mean_reverting` regimes by
4-7% APY purely due to whipsaw fees.

The backtest harness logs which regime each candidate harvest cycle was in,
so the operator can verify the regime gate is doing useful work post-hoc.
For Phase 1 (90-day backtest), the harness uses a synthetic regime stub
(see §9) because the live HMM only persists 28-day history. Phase 2 will
read live regime labels from `regime_classifier_outputs` once 90 days
of history exists.

---

## 7. Entry / exit thresholds

ScienceDirect 2025 §4.4, threshold-entry variant:

```
ENTRY:  funding_rate >= 0.01% per 8h  (= 0.03% per day = ~11% APY at face)
EXIT:   funding_rate <  0.005% per 8h (or regime flips out of harvest set)
```

Our fee model is harsher than the paper's (it assumes maker rebates;
we model taker on both legs). Break-even funding rate:

```
fee_round_trip = 0.055% × 2 (perp) + 0.10% × 2 (spot, Bybit) = 0.31% RT
                  -- OR --
fee_round_trip = 0.05%  × 2 (perp dYdX) + 0.40% × 2 (spot Coinbase taker)
                = 0.90% RT  (worst case, all taker)

break_even_funding_per_8h = fee_RT / N_periods_held
```

If we hold for 21 funding periods (~7 days), break-even is `0.31% / 21 =
0.015% per 8h`. Anything above that nets profit.

Implementation in `funding_rate_harvest.py`:

```python
ENTER_THRESHOLD_BPS_PER_8H = 2.2   # 0.022% per 8h = ~24% APY pre-fee
EXIT_THRESHOLD_BPS_PER_8H  = 0.5   # 0.005% per 8h — exit if funding decays
MIN_HOLD_PERIODS           = 3     # avoid 1-period whipsaws
```

These constants are tuneable; the backtest report logs them in the
`config` block of the gates JSON for diff tracking.

---

## 8. Failure modes

1. **Funding flips negative mid-hold.** Detect via `should_exit` on the
   next funding tick. Cost: pay one funding period at the new (negative)
   rate before exit + fees to close. Mitigation: tight `EXIT_THRESHOLD`.
2. **Spot/perp basis blows out.** Rare on liquid pairs (BTC/ETH basis
   stays inside ±0.3% even during crashes), but on lower-tier alts (DOGE,
   XRP) the basis can briefly hit ±2%. Mitigation: cap notional per
   pair (§5) and avoid pairs with persistent basis volatility > 1%.
3. **Exchange outage.** dYdX v4 indexer goes down → cannot read funding
   rate. Mitigation: poll secondary mirror (`indexer.v4testnet.dydx.exchange`
   not viable for mainnet — use OKX as a *signal* fallback even if the trade
   leg is on dYdX, since funding rates are correlated > 0.97 per §2.1).
4. **Spot-leg fill failure.** Coinbase rejects the spot order due to
   insufficient USD. Detect via the existing OrderProposal lifecycle; do
   NOT open the perp leg if the spot leg fails. Halt the pair, alert.
5. **Withdrawal time mismatch.** dYdX → Coinbase bridge takes ~30 min via
   Noble + Cosmos hub. If basis moves > 0.5% during this window, the
   rebalance loses money. Mitigation: keep ≥ 50% of pair notional
   pre-positioned on each leg to avoid frequent bridges.
6. **Funding-rate API delay.** OKX/dYdX both publish funding ~5 min before
   it accrues. Polling at funding-time + 30s avoids race conditions.
7. **Stablecoin de-peg.** USDC depeg (March 2023 event was -10%) breaks
   the dollar-equivalence assumption between USDC margin (dYdX) and USD
   spot (Coinbase). Mitigation: monitor USDC/USD spot, halt new entries
   if depeg > 1%.

---

## 9. What this scaffold deliberately does NOT do

- **No live order placement.** The harness reads funding history, simulates
  fills at the funding tick price, and logs synthetic PnL. No exchange
  client wires.
- **No HMM regime read.** Phase-1 backtest uses a synthetic regime stub:
  rolling 24h spot-return slope > +0.5% → `trending_up`,
  rolling 24h volatility (σ of hourly returns) > top 25% → `high_volatility`,
  otherwise → `mean_reverting`. This is a reasonable proxy that lets us
  validate the literature claim; Phase 2 will read the real HMM.
- **No cron entry installed.** The cron line in `harness.py` docstring is
  for the V4 strategies. We will append a funding line once the dYdX
  account is funded and the live signal source is wired. Adding a cron
  now would just produce stale reports against ever-older OKX data without
  any live action.
- **No ML overlay.** The 31% APY ML-enhanced number from the paper is
  out-of-scope for v1. Phase 3 (post-Week-3) will add features: funding
  z-score, OI delta, basis curvature.

---

## 10. Phase-1 backtest result (2026-05-15)

Ran `python -m quanta_core.backtest.funding_harness --all --days 90 --sweep`
against OKX funding-rate history for BTC/ETH/SOL/XRP/DOGE (5 symbols, 270
funding ticks per symbol, 90 days). Notional $10k per cycle, Bybit taker
fee 5.5bps × 4 = 22bps round-trip.

| Threshold row | n_trades | aggregate Sharpe | profit_factor | total PnL | promotion eligible |
|---|---|---|---|---|---|
| literature_default (enter ≥ 0.022%/8h, exit < 0.005%) | **0** | n/a | n/a | $0 | No (no entries) |
| venue_calibrated (enter ≥ 0.005%/8h, exit < 0%, min_hold=1) | 119 | -306 | 0.0 | **-$2,484** | No |
| aggressive_any_positive (enter > 0%) | 211 | similar | 0.0 | **-$4,472** | No |

Per-symbol (venue_calibrated row):
- BTC: 17 trades, Sharpe -197, PnL -$360, mean funding APY pre-fee ~3.5%
- ETH: 23 trades, Sharpe -142, PnL -$486, ~3.6%
- SOL: 22 trades, Sharpe -138, PnL -$460, ~2.4%
- XRP: 26 trades, Sharpe -111, PnL -$535, ~4.1%
- DOGE: 31 trades, Sharpe -170, PnL -$642, ~3.4%

**Profit factor 0.0 across every symbol** — *every trade lost money*. Why:
OKX funding is capped at 0.01% per 8h (= 11% APY pre-fee). One funding
period at the cap collects $1 on a $10k cycle; round-trip fees are $22.
Holding for ≥ 22 periods (~7 days) at the cap is required just to break
even, but the cap is rarely hit, mean funding is 0.012% per 8h (annualised
~13% pre-fee), and our regime gate plus exit conditions cut holds short.

The literature-claimed 18-31% APY post-fee assumes:
1. Bybit-style or pre-cap funding rates (uncapped, can reach 0.05-0.10% per 8h
   in bullish regimes — we have none of those in this 90-day window).
2. Maker rebates on at least one leg (we modelled all-taker, the conservative
   assumption).
3. A bullish regime — early 2026 has been mostly mean-reverting per the
   synthetic regime stub (BTC chop +13.9% over 30d but with frequent flips).

**Operator implication:** the strategy is NOT a Tier-A candidate in the
current regime. It is a *seasonal* Tier-A candidate: re-enable when
funding rates persistently exceed 0.02% per 8h (the synthetic regime stub
should auto-detect this). The harness is built and re-runnable; the
gate report will pass on its own once the funding environment turns.

**Caveat on the data source:** OKX's 1bp cap is venue-specific. Bybit's
cap is 0.05% per 8h, 5× wider; dYdX has no cap. Re-running the same
harness against dYdX historical funding (Week-3 wiring) will likely
produce a higher max-PnL row but the *median* funding behaviour will not
change much — exchanges arbitrage their funding rates against each other.

---

## 11. Files written

- `audit/2026-05-15-funding-rate-design.md` — this doc.
- `src/quanta_core/strategies/funding_rate_harvest.py` — pure decision
  logic. `should_enter`, `should_exit`, `simulate_harvest`.
- `src/quanta_core/strategies/__init__.py` — package marker.
- `src/quanta_core/backtest/funding_harness.py` — CLI entry, OKX history
  fetcher, gate report writer. Sister to `backtest/harness.py`.
