# Post-cutover audit consolidated — 2026-05-13

Four parallel audit agents reviewed the codebase ~1 hour after the V4 cutover. This doc collapses their 4 reports into one ranked fix plan.

## TL;DR

| Track | LoC | Time | Impact |
|---|---|---|---|
| **A. trade_journal writes from V4 runner** | ~80 LoC | 2-3h | Unlocks 6 dashboard endpoints at once |
| **B. Pause/Resume → quanta_schema.run_state** | ~120 LoC | 2-3h | Kill switch works again |
| **C. Frontend cleanup — delete dead FreqAI/TFT/EPT cards** | -230 LoC (delete) | 1-2h | Visible IDLE/null pollution gone |
| **D. Real Coinbase REST order placement** | ~100 LoC | 1-2 days | V4 actually trades on the exchange |
| **E. Hermes: detach nightly_reflector + git pull fix** | <15 LoC each | 30 min | Stops error notifications |

Total ship-this-week budget: **~3 days**. Each track is independent and committable on its own.

---

## TRACK A — V4 runner writes trade_journal (highest leverage)

Source: Backend audit §10, V4-readiness audit §2.

**Problem:** Six dashboard endpoints read from `trade_journal` table (`/api/ops/{readiness,rebalance,slack_preview,explainability/*}`, `/api/ops/trades_risk` live-tape, `/api/state.recent_trades`). The table is written only by `user_data/modules/trade_journal.py`, which was a freqtrade strategy hook. Post-cutover it's silently dark.

**Fix:** In `scripts/run_v4_shadow.py` around lines 540-580 (the `fill_pending_proposals` → fill-handling block), INSERT a `trade_journal` row for every closed paper fill. Schema is well-known — mirror `user_data/modules/trade_journal.py`.

**Where:** `scripts/run_v4_shadow.py:540-580`
**Test:** After ship, curl `/api/ops/readiness`, `/api/ops/slack_preview`, `/api/state.recent_trades`. All should populate without code change to dashboard.

---

## TRACK B — Pause / Resume / Kill switch (BLOCKING)

Source: Backend audit §4-§5.

**Problem:** `POST /api/ops/pause` (`ops_routes.py:735`) POSTs `/api/v1/stop` to dead freqtrade → 502. Same for `/api/ops/resume` (`ops_routes.py:1204`). The dashboard's kill-switch button, "Pause/Flatten" command palette items in `ops_spa.js:2889` and `dashboard_spa.js:203` all dead-end. **Today the operator has no way to pause V4.**

**Fix (3 steps, ~120 LoC total):**

1. New table:
   ```sql
   CREATE TABLE quanta_schema.run_state (
     id           SMALLINT PRIMARY KEY DEFAULT 1 CHECK (id = 1),
     paused       BOOLEAN NOT NULL DEFAULT false,
     paused_reason TEXT,
     paused_at    TIMESTAMPTZ,
     set_by       TEXT
   );
   INSERT INTO quanta_schema.run_state (id) VALUES (1);
   ```
2. Dashboard `pause`/`resume` handlers UPSERT this row instead of POSTing to freqtrade. Keep the existing drawdown/circuit-breaker preflight (`ops_routes.py:1192-1202`) + anchor-clear (`:1214-1230`).
3. `run_v4_shadow.py:run_cycle` reads `paused` at top of each cycle; if true, log + skip proposals (still update regime + sentiment).

**Where:** `src/quanta_core/ledger/migrations/003_run_state.sql` (new) + `user_data/dashboard/ops_routes.py:705-770, 1180-1240` + `scripts/run_v4_shadow.py:run_cycle`

---

## TRACK C — Frontend cleanup (delete dead UI)

Source: Frontend audit §1, §3, §5.

**Quick deletes** (no logic risk, just remove):

| File:line | What | Effect |
|---|---|---|
| `ops_spa.js:2989-3048` + mount at `:6149` | Card 17 "Training · FreqAI / TFT retrain status" | The literal "IDLE" card the operator quoted |
| `ops_spa.js:828-946` + mount at `:6141` | TrainingHealthLive "TFT model health per pair" | Reads `pair_dictionary` (FreqAI artifact) |
| `ops_spa.js:1315-1316` | Dead freqtrade fallback branches in engine pill | -2 LoC |
| `ops_spa.js:1317` | `STRATEGY: EPT` fallback (V4 always wins now) | -1 LoC |
| `qc_react.js:1322-1628` | Dual Topbar component (dashboard_spa has its own) | -300+ LoC |
| `ops_spa.js:1361-1370` | Agent timeline cron rows referencing EPT/TFT/DRL | repurpose to V4 cadence |
| `ops_spa.js:3141` | Regime config tooltip "triggers freqtrade reload" | one string |
| `docs.js:111-318` | FreqAI/EPT/TFT/DRL glossary entries | rewrite for V4 |

**Quick repairs:**

- `ops_spa.js:2138-2199` (PairTelemetryLive): when a pair's `closes:[]`, show `[no candles yet]` chip per row instead of silent flat line. The `v3DeterministicCloses` PRNG at `:2115-2136` exists but isn't wired — either wire it as fallback or delete.
- `ops_spa.js:1901-1930` (EntryGates): hide `freqai_predict / tft_confidence / high_vol_confidence / up_prob_threshold / meta_signal_up / meta_confidence` columns when `engine==='quanta_core'`. Operator sees 4 useful gates instead of 11 grayed cells.
- `dashboard_spa.js:140-184`: `dayPct === 0` should render neutral pill, not green "up".
- Extract `resolveEngineMeta(mode, services)` ONCE (currently three copies in `ops_spa.js:1311-1317`, `dashboard_spa.js:147-153`, `qc_react.js:1477-1500`).

**Bump cache-buster after** to ensure browsers pick up the JS.

---

## TRACK D — Real Coinbase REST order placement

Source: V4-readiness audit §5 (chosen Option A), Backend audit §2.

**Problem:** `scripts/run_v4_shadow.py:517 fill_pending_proposals` writes synthetic paper fills at next-bar close. The 5000+ LoC of production V4 code (`ExecutionEngine`, `CoinbaseExchange.submit_order`, slippage gate, idempotency store) is fully tested but unused.

**Fix (1-week sprint, ~100 LoC):**

1. Instantiate `CoinbaseConfig.from_env(mode="paper")` + `CoinbaseExchange` at runner startup.
2. Replace `write_proposal_and_order` / `fill_pending_proposals` with `submit_order` + `get_orders(status="filled")` poll.
3. Add `slippage_gate.passes(...)` before submit using the latest bar's close as `current_mid`.
4. Persist real `venue_order_id` from Coinbase response.
5. Test: `tests/exchanges/test_coinbase.py` covering `submit_order` / `_order_to_ack` (currently zero direct tests).

Why not WebSocket streams now? `coinbase.py:424,436,456` `stream_ticks/stream_fills/stream_orderbook` are still stubs (`if False: yield`). That's ~2-3 weeks of work plus the sync→async Strategy ABC port. Defer to V4.1.

Why not debate orchestrator now? `agents/debate.py:140` adds ~30s latency per BUY (hermes3:70b call). Premature until #1-#4 give us a paper P&L baseline.

---

## TRACK E — Hermes cron fixes (5 quick wins)

Source: Hermes cron audit.

1. **`nightly_reflector` 120s timeout** → edit `~/.hermes/scripts/nightly_reflector.sh` to detach: `setsid nohup python3 scripts/nightly_reflector.py >>"$LOG" 2>&1 < /dev/null & disown; exit 0`. Pattern already used by `stocks_ml_train.sh`. Pre-warm qwen3:30b in `ollama_health.sh` (every 5 min) with `ollama run qwen3:30b ""`.
2. **`shark git pull --rebase` conflict every 15 min** → patch `stocks/shark/run.py:259` from `git pull --rebase` to `git fetch && git merge --ff-only`. One-time: `git stash -u && git pull --rebase && git stash pop` to clear the 90-commit backlog seen in cron logs (already self-recovered, no rebase active now).
3. **`pip install yfinance`** into `/home/saijayanthai/Documents/spark/envs/ml-env/bin/pip` — reflector's alpha-vs-benchmark math is degraded.
4. **`SlackAlerter.notify_daily_summary(date=)` signature mismatch** → drop `date=` at caller in `stocks/shark/phases/daily_summary.py`. One line.
5. **Delete dormant `resample_4h`** job from `jobs.json` (references stopped freqtrade) OR leave disabled if rollback path matters.

---

## Items deferred to next sprint

From V4-readiness audit:
- WebSocket live streams (Coinbase + Alpaca) — stubs today
- Sync→Async Strategy ABC unification (blocker to swapping in `LiveEngine`)
- `Reconciler` for live position-state vs exchange diff
- `DebateOrchestrator` wiring for BUY decisions
- `MonteCarloEngine` VaR check before high-conviction trades
- `IdempotencyStore` (currently `uuid.uuid4()` per cycle)
- `high_volatility` regime strategy (today: no entries in any of 4 regimes when high_vol)

From Backend audit:
- New `quanta_schema.signals` table for per-pair TFT/meta predictions (drives `/api/state.tft.*`, gates per-pair eval)
- Re-home `sentiment_engine.py` as sidecar or inline in V4 runner hourly tick (currently 1.5h stale)
- Delete ~1200 LoC of `ops_routes.py` endpoints with zero `frontend-v4` consumers once SPA migration is final

---

## Execution order (recommended)

1. **Track E** (Hermes) — 30 min, stops the error notifications. Independent.
2. **Track C** (Frontend cleanup) — 1-2h, biggest visible improvement, no logic risk.
3. **Track A** (trade_journal) — 2-3h, unlocks 6 endpoints. Pairs naturally with C.
4. **Track B** (Pause/Resume) — 2-3h, restores kill switch. Higher risk (new schema migration + runner gate).
5. **Track D** (Real Coinbase REST orders) — 1-2 days, the real V4 production-grade upgrade.

Ship A+C+E in one work-day; B the next; D the rest of the week.
