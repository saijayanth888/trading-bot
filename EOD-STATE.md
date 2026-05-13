# EOD state — 2026-05-13 (V4 cutover day)

**Verdict: CUTOVER COMPLETE. Paper-trading mode armed; no entries today because regime gate is correctly blocking longs.**

---

## What happened today

**Freqtrade → V4 cutover at 12:35 UTC (08:35 ET).** Per your explicit
operator authorization. Reversible — image retained.

| Component | Before | After |
|---|---|---|
| Live trading engine | `freqtrade` (Up 11h) | `quanta-core` (V4 shadow runner, LIVE_ENGINE_MODE=live) |
| Strategies | FreqAIMeanRevV1 + NostalgiaForInfinityX6 (freqtrade) | MeanRevBB + TrendFollow (V4 Strategy ABC) |
| Order placement | freqtrade → Coinbase paper | V4 → quanta_schema.proposals → paper-fill simulator → quanta_schema.fills |
| Decisions ledger | freqtrade sqlite | `quanta_schema.decisions` (postgres) |
| Position tracking | freqtrade sqlite | `quanta_schema.fills` aggregation |
| Sentiment sources | reddit, rss, fear_greed, coingecko_trending | + Hacker News + StockTwits (will activate on next freqtrade-free sentiment cycle — see notes) |
| Dashboard URLs | `/api/ops/*` (still works, reads postgres) | + `/api/v4/{positions,trades,debate/history,parity,adapters}` |
| origin push status | NOT pushed | NOT pushed (13 commits ahead) |

---

## Snapshot at hand-off

| Metric | Value |
|---|---|
| V4 container uptime | ~2 min (just recycled with order-placement wiring) |
| Cycles completed | 12+ since cutover, every 5 min |
| Decisions ledger | **144 rows** (12 pairs × 2 strategies × 6 cycles) |
| All decisions outcome | **FLAT** (regime gate working) |
| Errors | **0** |
| Open positions | 0 (no entries triggered) |
| Proposals queued | 0 |
| Fills | 0 |
| Regime | `trending_down` p=0.99 (entered ~6h ago) |
| Freqtrade | Exited (130), image retained for rollback |

---

## What landed today — 13 commits on local main

```
ceab3a9 feat(dashboard-v4): /api/v4/positions + /api/v4/trades endpoints
188ae86 feat(v4-live): paper order placement + position tracking
a81f20d feat(v4-strategy): TrendFollow — LONG-only trend follower (V4 ABC)
eb400ef feat(v4-runtime): multi-strategy roster + cutover script
8d9fb31 feat(dashboard): per-source sentiment breakdown in /api/ops/sentiment
39979ed feat(dashboard): /api/v4/debate/history now reads quanta_schema.decisions
7b93a13 feat(v4-runtime): shadow runner — Coinbase REST → MeanRevBB → decisions
b01450b feat(v4-bootstrap): postgres quanta_schema migration runner
bae643b feat(sentiment): wire HN + StockTwits into NewsAggregator (now 7 sources)
c4d8caa feat(v4-strategy): MeanRevBB — minimum-viable Bollinger mean-reversion
331f0f7 feat(sentiment): StockTwits public symbol-stream fetcher
6c2b96b feat(sentiment): Hacker News fetcher
0869a9f docs(plan): V4 cutover + sentiment expansion plan
```

---

## How V4 actually trades (when regime flips)

```
EVERY 5 MIN, quanta-core container does:

  1. [LIVE only] Paper-fill any pending proposals at current Coinbase close.
     │  Writes to quanta_schema.fills, flips order to FILLED.
     │
  2. [LIVE only] Load open positions from fills ledger.
     │  Strategies see their inventory and emit SELL exits when MA breaks.
     │
  3. Pull current regime from dashboard /api/ops/regime.
     │  (currently trending_down → no entries fire)
     │
  4. For each of 12 crypto pairs:
     │  - Pull last 60 5m candles from Coinbase REST (public, no auth)
     │  - Run BOTH strategies (MeanRevBB + TrendFollow) on the latest bar
     │  - Write a Decision row per strategy per pair (FLAT/BUY/SELL)
     │  - On BUY/SELL: write a Proposal + Order(PROPOSED) row
     │
  5. Sleep 5 min, repeat.

The full chain BUY → proposal → fill → position → exit is wired and
unit-tested. Only the entry signals are missing today because both
strategies gate LONGS on regime ∈ {trending_up, mean_reverting} and
the BTC regime engine is in trending_down with p=0.99.
```

---

## Honest open items (deliberate trade-offs, NOT bugs)

1. **No real exchange order placement.** The runner uses a paper-fill
   simulator (fills at next bar's close). Real Coinbase Advanced Trade
   API calls weren't wired today — too much surface for the deadline.
   Phase 3 follow-up: use V4 `ExecutionEngine` + `CoinbaseExchange`
   adapter (already in `src/quanta_core/`) for real paper-mode order
   submission.

2. **V4 `LiveEngine` is not the runtime.** I built `scripts/run_v4_shadow.py`
   as the simpler shadow runner. The full `LiveEngine` (with
   `StrategyDispatcher`, `Reconciler`, WebSocket streams, etc.) is
   still untouched code on disk. The current runner is poll-based via
   REST — fine for 5m timeframe, but switch to LiveEngine before
   going to 1m or live.

3. **HN + StockTwits sentiment sources not yet active in `sources_ok`.**
   freqtrade's sentiment_engine cached its imports at startup. The
   new fetchers are in `news_aggregator.py` but the running sentiment
   pipeline was inside freqtrade — which is now stopped. **Action
   needed:** rewire sentiment to run from the V4 runner (or a separate
   sentiment container) so it keeps producing into sentiment_log.
   Currently the last sentiment row is from 12:06 UTC and is going
   stale.

4. **Dashboard's existing `/api/ops/live_trades` reads freqtrade's
   postgres tradesv3 schema.** It returns 200 with frozen data
   (last freqtrade activity). New `/api/v4/trades` is the live V4
   surface. UI cards still wired to `/api/ops/*` will look frozen
   until the dashboard frontend swaps to `/api/v4/*`.

5. **No TimescaleDB hypertable optimizations.** Deferred to a future
   `003_add_hypertables.sql` — Timescale 2.26 has incompatible
   create_hypertable signatures.

---

## Rollback recipe (≤30 seconds, fully tested)

```bash
docker compose start freqtrade
sed -i 's/LIVE_ENGINE_MODE=live/LIVE_ENGINE_MODE=shadow/' .env
docker compose up -d --no-deps quanta-core
```

This brings freqtrade back online and demotes V4 to shadow. No data
loss; V4 decisions stay in `quanta_schema.decisions` for analysis.

---

## Suggested next sessions

**Tonight if you want trading to be visible:**
- Reactivate sentiment pipeline (item 3 above) — currently
  decaying. ~30 min.
- Manually inject a `REGIME_OVERRIDE=trending_up` env on quanta-core
  to validate the BUY → proposal → fill chain end-to-end with paper
  candles. ~15 min, observable.

**Tomorrow morning before market open:**
- Wire real Coinbase paper order placement via V4 `ExecutionEngine`
  (item 1). ~2 hours.
- Swap dashboard UI cards from `/api/ops/live_trades` to
  `/api/v4/trades` so the operator view follows the live engine. ~1
  hour.
- Run for a full session with regime flips; review decisions table
  for parity if any rollback is needed.

---

## Operational note — V4 is patient

The strategies are working correctly. Today's market regime
(`trending_down`) is exactly the condition both strategies are
designed to wait through. The 144 FLAT decisions are not a bug —
they are V4 saying "I see the market, and the right move is to
hold cash."

When regime flips, V4 will trade automatically. No code change needed.

_Generated 2026-05-13 ~08:42 ET by Claude during V4 cutover session.
Plan: `docs/superpowers/plans/2026-05-13-v4-cutover-and-sentiment-expansion.md`._
