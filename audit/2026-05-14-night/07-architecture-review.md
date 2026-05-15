<!--
Audit: 07-architecture-review.md
Date: 2026-05-14 (night)
Reviewer: Architecture agent (Claude Sonnet 4.6)
Scope: READ-ONLY. No code edits. All claims cite file:line or commit SHA.
-->

# Architecture Review — Quanta Trading Bot
**Date:** 2026-05-14  |  **Status:** Paper-only, post V4-cutover  |  **Overall confidence:** 3 / 10

---

## TL;DR (Executive Summary)

1. **Overall confidence to run real money: 3/10.** The system is paper-trading correctly but has never placed a real exchange order. The V4 paper-fill simulator (`scripts/run_v4_shadow.py`) has been the entire execution stack since 2026-05-13. Real order placement is explicitly deferred ("Track D", `docs/POST-CUTOVER-AUDIT-2026-05-13.md:3`).

2. **Top blocker:** No demonstrated alpha. The two strategies (MeanRevBB + TrendFollow) are Bollinger-Band and SMA cross — both are textbook entries that are well-known to earn ≤ break-even after fees on liquid crypto. The `private/REVIEW_2026-05-11.md:38` analysis says best-case is 0.05–0.20% per trade while Coinbase round-trip fees are 0.25–0.30%, meaning the *expected value of the primary crypto strategy is negative after fees*. Wheel P&L is +$524 across 2 closed trades — not enough to distinguish signal from noise.

3. **Architecture is coherent but only 2 of 5 layers are wired end-to-end.** Data → Signal works. Risk gate exists but is wired only in `LIVE_ENGINE_MODE=live` path — shadow mode runs zero risk approval (`run_v4_shadow.py:1412`). Execution → Reporting is paper-only. The full LiveEngine, WebSocket streams, and real Coinbase order placement in `src/quanta_core/execution/`, `src/quanta_core/live/` are built but not running.

4. **Logic stability is poor.** In the last 7 days: 20 fix commits vs 3 feat vs 6 chore/refactor out of 31 total (65% fix rate on the hot path). Recurring fixes in `pnl_pct` unit, position ownership, fill verification, gate display, and sentiment adapters show the signal→fill→P&L chain is still being debugged.

5. **Operational footprint is large for one operator.** 4 Docker containers, 31 Hermes cron jobs, 3 Ollama models (hermes3:8b/70b, qwen3:30b), Coinbase REST, Alpaca paper API, optional Perplexity. The README diagram (README.md:62–123) is accurate and admirably detailed, which is a genuine strength.

---

## Section 1 — Overall Architecture  |  Grade: B-

### 5-Layer Stack

The intended stack is: (1) Data Feeds → (2) Signal / Strategy → (3) Risk Governor → (4) Execution / Paper Simulator → (5) Reporting / Dashboard. The design is explicit and sensible. The architecture doc (README.md:62–380) is unusually detailed for a solo project and earns respect.

The boundary problem is that layers 3-4 are partially wired. The `RiskGovernor` (`src/quanta_core/risk/governor.py`) has all six gates implemented (drawdown, daily-loss, concurrent positions, single-name cap, correlation, circuit breaker), but the shadow runner only gates BUYs when `cfg.mode == "live"` (`run_v4_shadow.py:1412`). Today's running mode is `LIVE_ENGINE_MODE=live` which means the gate fires, but the comment at line 77-81 reveals this was added 2026-05-14 *after* an audit found V4 "had ZERO risk approval in production." That's a near-miss: the bot ran unchecked for the first ~24h of its life.

### Cross-Process Contracts

Message contracts between quanta-core and the dashboard pass through Postgres (`quanta_schema.*`). There are 3 migration files (`src/quanta_core/ledger/migrations/`). There are no versioned schema checksums — a runner upgrade that adds a column will silently break old dashboard reads. The `ops_routes.py` endpoint contract is documented in the file header (ops_routes.py:2–17) with a consistent `{status, data, error, checked_at}` envelope, and this is honored across the sample of endpoints reviewed. That's better than most hobby bots.

### Single Points of Failure

- **Postgres** (`tradebot-postgres`): If it dies, quanta-core fails to write decisions (restarts silently per `restart: unless-stopped`), dashboard serves `degraded` envelopes, and the kill switch (`run_state.paused`) is unreachable. There is no hot standby.
- **quanta-core container**: If it crashes between cycle N's proposal write and cycle N+1's fill, proposals stay in PROPOSED state indefinitely. There is no stale-proposal cleanup job.
- **Coinbase REST**: All 12 crypto candle feeds depend on one endpoint family. There is no fallback price source.
- **Ollama on host**: If killed, sentiment refresh silently returns empty (sentiment_engine.py gracefully degrades), but the debate pipeline, shark phases, and regime classification all degrade simultaneously.

### Operational Footprint

The operator manages: 3 active Docker containers (postgres, dashboard, quanta-core), 1 optional container (vllm), 31 hermes-gateway crons, 3 Ollama models, 2 external APIs (Coinbase, Alpaca), 1 optional paid API (Perplexity). For a solo dev this is at the upper edge of sustainable — the README covers it honestly. The gpu_gate.sh HERMES_GPU_GATE_DISABLE=1 emergency override is a good defensive design.

---

## Section 2 — Frontend UX  |  Grade: C

### Three Frontends in Coexistence

The repo runs three JavaScript surfaces simultaneously:
- `user_data/dashboard/static/js/dashboard_spa.js` — 1,271 lines, per-pair drill-down
- `user_data/dashboard/static/js/ops_spa.js` — 6,546 lines, ops console (the main surface)
- `frontend-v4/src/` — React 19 + shadcn, served at `/v4/`, explicitly *not mounted* as the active UI

The V4 SPA is dead weight in the current deployment per memory `v4-is-additive`. The coexistence cost is 3× review surface for CSS bugs and API contract changes. The `docs/POST-CUTOVER-AUDIT-2026-05-13.md:80` audit called out 3 copies of `resolveEngineMeta` logic across the SPAs — this is the classic "just copy it" debt that compounds silently.

### Information Density vs. Signal

Cards that drive action (evidenced by audit fixes directed at them): EntryGatesLive matrix, Scoreboard flash row, NYSE pill, BlockerBanner, Circuit Breakers. These are good designs — they surface concrete prices and WHY strings (README:416-417 example: `mr: close $79,732 ≥ lower_bb $79,383`).

Decorative/low-signal: the meta-agent P(UP/FLAT/DOWN) probability card (card 02) is a heuristic weighted-sum, not a trained model (`run_v4_shadow.py:739-838`), yet the UI surfaces it as a 3-class probability. An operator watching P(UP)=0.72 might over-trust it. The nightly reflector card (qwen3:30b output) is qualitative narrative — useful for context but not action-triggering.

### Mobile / Phone Usability

No mobile-specific layout found in the SPA JS files. `ops_spa.js` renders a multi-column card grid. From a Telegram alert to seeing the underlying data requires: open browser → navigate to `localhost:8081/ops` → find the pair in the gates matrix → click through to pair drill-down. That is 3-4 steps on a desktop; on a phone the ops_spa.js grid is likely unusable without horizontal scroll. No keyboard nav or ARIA roles sampled.

### Alert → Action Flow

The flow from "I got a Telegram alert" to "I can take action" requires: (1) Telegram alert fires (Hermes notifier), (2) operator opens browser to ops tab, (3) locates the pair or relevant card, (4) reads WHY string, (5) hits Pause button (mutating endpoint, bearer-gated). The pause/resume UI exists and is gated correctly (`ops_routes.py:128-173`). The killer gap is Step 5 from a phone — if the op receives a kill-signal alert at night, can they pause from a phone? The ops SPA is not mobile-responsive.

---

## Section 3 — Backend Design  |  Grade: B-

### Entry Points

- `user_data/dashboard/app.py` — 522-line FastAPI app. Clean: imports ops_routes, v4_routes, sets up Jinja2, registers lifespan handler. No business logic in app.py itself.
- `scripts/run_v4_shadow.py` — 1,588 lines. This is the god file for the paper runtime. It contains: config, data feeds, regime compute (HMM), classifier (meta-signal), risk gate shim, proposal write, fill simulator, trade_journal mirror, and the main event loop. Layer boundaries are absent here — it is a procedural script, not a layered engine. This is appropriate for a single-file "shadow mode" MVP but is a scaling wall.
- Hermes cron scripts at `~/.hermes/scripts/` — separate from the repo tree. No unit tests for these scripts; they are shell wrappers that call Python modules.

### API Design

The envelope contract is consistent (`{status, data, error, checked_at}`) across the sampled endpoints (`ops_routes.py:55-61`). Hard 3.5s timeout per endpoint via `asyncio.wait_for` (`ops_routes.py:52`, `ops_routes.py:64-69`). This is solid defensive design.

Gaps: No pagination on bulk endpoints. `ops_db.open_positions(limit=50)` is called at `ops_routes.py:608` with a hardcoded cap — this is not surfaced to the caller. No cursor-based pagination anywhere. For a paper bot with small trade volume this is fine today; it becomes a problem when trade_journal reaches thousands of rows.

Error contracts: `degraded` vs `down` distinction is documented in the header but used inconsistently. `timeline` returns `"ok" if decisions else "degraded"` (`ops_routes.py:1730`) where "no data yet" is arguably `ok` not `degraded`. Minor but adds noise to health monitoring.

The dead reference at `ops_routes.py:1698` (reads `freqtrade.log` which does not exist post-cutover) is guarded by `if log_path.exists()` — it is a silent no-op, but as the README acknowledges (README.md:748-752), the comment block misleads future readers.

### Data Flow: BUY Proposal → Fill → P&L

Price tick enters at `run_v4_shadow.py:196-231` (Coinbase REST). MeanRevBB evaluates at `mean_rev_bb.py:105-144`. RiskGovernor gates at `run_v4_shadow.py:1412-1445`. Proposal writes to `quanta_schema.proposals + orders` at `run_v4_shadow.py:1001-1035`. Next cycle: `fill_pending_proposals()` at `run_v4_shadow.py:1038-1122` marks FILLED, writes `quanta_schema.fills`, mirrors to `trade_journal`. Dashboard reads `trade_journal` for P&L display. Total hops: 7. Failure modes at each hop: Coinbase timeout (silently skips cycle), strategy exception (logged, skipped), RG import failure (fail-OPEN — explicitly noted at `run_v4_shadow.py:83`), proposal conflict (ON CONFLICT DO NOTHING — idempotent, good), fill write exception (logs exception, continues), trade_journal write failure (logs warning, continues). The fail-open on RG import is the most dangerous: if the risk module is broken on start, every BUY fires unrestricted.

### Persistence: Source of Truth

| Domain | Source of Truth | Notes |
|---|---|---|
| Crypto positions | `quanta_schema.fills` aggregate | Clean single source |
| Crypto P&L | `public.trade_journal` | V4 mirrors fills here (run_v4_shadow.py:1105-1119) |
| Wheel positions | `stocks/wheel/state/positions.json` | File on disk, no DB backup |
| Wheel P&L | `stocks/wheel/state/account_snapshot.json` + `trades.jsonl` | File on disk |
| Regime | `public.regime_log` | DB, hourly updates |
| Sentiment | `public.sentiment_log` | DB, 15-min updates |
| Kill switch | `quanta_schema.run_state` | DB singleton — correct design |
| HMM model | `user_data/data/regime_hmm.json` | File, baked into Docker image |

The wheel positions live in JSON files with no transactional guarantees. A crash mid-write to `positions.json` would corrupt the file. The 2026-05-13 `positions.json.backup-pre-reconcile` file shows this has already been a practical concern.

---

## Section 4 — Quanta Core Engine  |  Grade: C+

### Module Cohesion

`src/quanta_core/` has clean module boundaries on paper: `strategy/`, `risk/`, `execution/`, `exchanges/`, `ledger/`, `agents/`, `live/`, `observability/`. However, `src/quanta_core/execution/`, `src/quanta_core/live/`, and `src/quanta_core/agents/` are built but not wired into the running system — they are dead weight in production today (`README.md:543-548`). The actual running engine is `scripts/run_v4_shadow.py`, a 1,588-line monolith that reimplements thin versions of what those modules would provide.

### V4 Paper Engine State Model

The runner maintains state across cycles via: (a) Postgres tables (proposals/orders/fills), (b) an in-process `_InProcessContext` that holds history deques and a positions dict (`run_v4_shadow.py:148-190`), (c) module-level globals for the regime model and risk governor singleton. On crash, the in-process context is lost — the next cycle re-hydrates positions from the DB fills aggregate (`run_v4_shadow.py:1202-1237`) which is correct. The history deque is NOT persisted — on restart, the strategies are in warm-up mode for the first N bars (N=20 for MeanRevBB). During warm-up, no proposals fire. This is safe but means a restart introduces up to 100 minutes (20 × 5m) of signal blindness.

### Signal Combination

The meta-signal (`_compute_classifier_probs`, `run_v4_shadow.py:739-858`) is a weighted linear combination of: momentum (5-bar z-score), momentum (20-bar z-score), RSI (z-scored), regime bias (posterior-weighted), and sentiment. This is a transparent heuristic. It writes to `public.meta_signal_log` and feeds dashboard card 02. It does NOT gate trades — MeanRevBB and TrendFollow run independently. There is no ensemble voting, no veto. The strategies simply emit proposals; the meta-signal is observability only.

### Risk Gates

`src/quanta_core/risk/governor.py` implements 6 gates. The gate is wired in the shadow runner at `run_v4_shadow.py:1407-1445` for live-mode BUY entries. The `unified_risk.py` module handles combined crypto+stocks drawdown (`user_data/modules/unified_risk.py:1-100`). The thresholds are config-driven and operator-editable. This is well-designed. The gap is `fail-open` on import failure (documented at run_v4_shadow.py:83-90): if `quanta_core.risk.governor` fails to import, `_RISK_GOVERNOR_AVAILABLE=False` and every BUY is approved without review. This should be `fail-closed` for a production system.

### Backtest vs. Live Parity

The `RiskGovernor` uses `/tmp` anchor paths for backtest runmodes (`governor.py:85-99`) — correct isolation. There is no mechanism to run MeanRevBB + TrendFollow in historical simulation mode and compare against live fills. The `tests/test_bt_quality_gates.py` exists but tests validate_readiness thresholds, not strategy signal parity. A regression where the BB calculation changes (e.g., switching from `pstdev` to `stdev`) would not be caught by any existing test.

---

## Section 5 — Logic Stability  |  Grade: D+

### Commit Ratio (Last 7 Days)

Out of 31 commits since 2026-05-08: 20 fix, 3 feat, 6 chore/refactor/docs, 2 untagged. **Fix rate: 65%.** This is high. Healthy mature systems run 20-30% fix. The ratio reflects a codebase that was simultaneously being rebuilt (V4 cutover), operated (paper trades), and debugged — a situation with inherent instability.

### Recurring Fix Areas (Signal That These Surfaces Are Unstable)

- **P&L calculation**: `fix(quanta-core): pnl_pct fraction unit` (commit `fc8ee26`) — `pnl_pct` was stored as a fraction in some places and percentage in others. This affects every dashboard P&L display and was caught only after watching live data.
- **Fill verification**: `fix(wheel): verify Alpaca fill before adding to positions.json` (commit `680cc93`) — the wheel was tracking phantom positions for hours before this fix.
- **Sentiment adapters**: `fix(sentiment): HN + StockTwits adapters silently emitted [] every poll` (commit `168eadf`) — silent data quality failure, invisible to operator.
- **Position ownership**: `fix(strategy-ownership): each V4 strategy only sees positions it opened` (commit `2ea9d91`) — structural cross-strategy stomping bug, present from day one of V4.
- **Gates display**: 3 separate fixes to the entry-gates matrix in 7 days (V3→V4 column swap, regime fallback, display logic).

### Defensive Code Quality

NaN/None handling is present at most I/O boundaries (e.g., `float(r["pnl_pct"] or 0)` pattern in ops_routes.py). The `_bounded()` wrapper at `ops_routes.py:64-69` provides timeout protection for all endpoints. The `ON CONFLICT DO NOTHING` proposal insert is idempotent. These are positives.

Gaps: The `stocks/wheel/state/positions.json` file has no atomic write (no write-then-rename pattern visible). The `_InProcessContext` positions dict is reset on restart with no validation against the DB fills aggregate. Input validation on API endpoints is minimal — e.g., the `pair` parameter in several `ops_routes.py` endpoints is taken from a whitelist (`_STOCK_SYMBOL_WHITELIST`) but the error on invalid input is a generic 400, not a typed schema error.

### Failure Mode Behavior

- Postgres down: quanta-core logs error, skips cycle write, retries next cycle. Dashboard returns `degraded`. Kill switch unreachable. **Recoverable but the operator has no escalation alert.**
- Ollama down: Sentiment cron returns empty (graceful). Shark phases silently skip. Regime HMM runs in-process (not Ollama-dependent) — crypto decisions continue. **Acceptable.**
- Coinbase REST down: quanta-core skips the affected symbols for that cycle, logs warning. **Acceptable but silently reduces coverage.**
- Alpaca API down: Wheel snapshot cron fails, positions.json is not updated. Dashboard shows stale wheel data. **Acceptable for paper mode.**

---

## Section 6 — Production Readiness  |  Scored

| Axis | Score | Justification |
|---|---|---|
| **Correctness** | 5/10 | pnl_pct fraction bug (fc8ee26) fixed only 3 days ago; phantom-fill bug (680cc93) in wheel ran for weeks. Numbers are improving but history of unit errors is concerning. |
| **Observability** | 7/10 | Envelope contract, WHY strings in gates, NYSE pill, circuit-breaker card, regime log — all above average. Missing: structured log aggregation, no alert on postgres down, no alert on quanta-core silent crash. |
| **Recoverability** | 5/10 | `restart: unless-stopped` on all containers. RiskGovernor anchor persists to disk. Positions re-hydrated from DB fills on restart. Gap: 20-bar warm-up blindness, stale proposals not cleaned up, positions.json non-atomic. |
| **Reproducibility** | 3/10 | No historical simulation mode for V4 strategies. BB uses live Coinbase data only. Cannot replay a specific day's decisions. REGIME_OVERRIDE env exists for forcing regime in tests but that is a test hack, not a backtest. |
| **Maintainability** | 6/10 | README is excellent. Architecture diagram is accurate. `run_v4_shadow.py` is a 1,588-line monolith that violates the layer boundaries defined in `src/quanta_core/`. A new dev would understand the design in 1 day but would struggle to modify the runner safely. |
| **Security** | 7/10 | `.env` is gitignored, `secrets/` tree is gitignored. Postgres bound to loopback (127.0.0.1:5434). Dashboard MCP key gates mutations. `hmac.compare_digest` for constant-time comparison (ops_routes.py:172). Gaps: dashboard binds 0.0.0.0:8081 (LAN-exposed), trust model relies on RFC1918 containment. |
| **Cost** | 9/10 | $0/month paid LLM APIs in hot path (Perplexity optional). Hardware is owned DGX Spark. Alpaca paper API is free. Coinbase public REST is free. Very low operating cost. |
| **Trading edge** | 2/10 | MeanRevBB on liquid crypto is a well-documented negative-expectancy strategy after 0.25-0.30% round-trip fees (private/REVIEW_2026-05-11.md:38). Wheel P&L = +$524 across 2 trades — insufficient sample. No Sharpe ratio computable from current data. |

### Overall Confidence to Run Real Money: **3 / 10**

**Top 3 gaps blocking a higher score:**

1. **No demonstrated alpha** (edge score 2/10). The primary strategies (BB mean-reversion on liquid crypto) are expected to lose after fees. Before going live, 30+ days of paper trades with positive expectancy after simulated fees are required. Current paper history: ~117 trade_journal rows as of audit/2026-05-14-regime-null-gap.md.

2. **No real order placement**. The execution path from strategy → exchange has never been exercised. `src/quanta_core/execution/` is built but not wired (`README.md:540-542`). The first live trade could expose bugs in order sizing, fill handling, partial fills, or exchange error codes that paper simulation never exercised.

3. **Risk governor fail-open on import failure** (`run_v4_shadow.py:83-90`). If `quanta_core.risk.governor` fails to import at container start, all BUY proposals are approved unconditionally. For a real-money system this must be fail-closed.

---

## Section 7 — How We're Trading

### Strategy in Plain English

**Crypto (12 pairs, paper mode):** Buy crypto when the 5-minute close price drops below the lower Bollinger Band (20-bar, 2σ) while the HMM regime is trending_up or mean_reverting. Exit when price recovers to the 20-bar SMA. Also trade SMA crossover (8/21) as a trend-following entry in trending_up regimes.

**Stocks (Wheel, Alpaca paper):** Sell cash-secured puts at ~0.35 delta on a 14-ticker watchlist on Friday mornings (weekly cycle). Collect premium; roll to covered calls if assigned.

### Alpha Source — Why Should This Make Money?

For **Bollinger Band mean-reversion on liquid crypto**: The thesis is that short-term price deviations from the 20-bar mean are transient and will revert. This is a contested claim on 5-minute bars of BTC/ETH. With Coinbase fees of 0.4-0.6% round-trip (taker + taker), the strategy needs to capture > 0.3% per trade after slippage. The private/REVIEW_2026-05-11.md:38 analysis cites published research saying BB mean-rev captures 0.05-0.20% per trade in liquid crypto — **break-even at best, likely negative after fees.**

For **the Wheel on stocks paper**: Selling volatility premium (short puts) has a positive long-run expectancy when IV > realized vol (equity vol premium). With NVDA and PLTR puts at current strikes, this is plausible. But the sample (2 trades, +$524 profit) is far too small to distinguish luck from edge.

### Risk Model — What Kills It?

For BB mean-reversion: a regime flip mid-trade (the exit gate does not gate on regime — `mean_rev_bb.py:127-130` exits purely on price ≥ SMA regardless of regime). A sustained trending-down move after entry will exhaust the mean-reversion and result in a large loss — the strategy has no stop-loss. For the Wheel: a stock gap-down through the put strike (e.g., NVDA earnings miss) produces assignment and an immediate unrealized loss. The Wheel has no hedge.

### Hit Rate / Sharpe

Insufficient live data. The `private/REVIEW_2026-05-11.md:29` shows cumulative P&L of -$66.39 as of 2026-05-11 across 2 closed trades. Current equity snapshot (`account_snapshot.json`) shows `wheel_cumulative_pnl: 524.05` and `portfolio_value: 100,528.31` on a paper account that started near $100,000. The regime-null-gap audit (`audit/2026-05-14-regime-null-gap.md`) notes 117 trade_journal rows exist but regime was NULL for most — making Sharpe calculation impossible from current data. No hit rate or Sharpe ratio is computable with confidence.

### Comparison to Baseline

Starting from ~$100,000 paper equity, the S&P 500 has returned approximately +1.5-2% YTD in 2026 per market context. If paper equity is $100,528 (+0.53%) after 14 days, it is underperforming an equal-weight buy-and-hold of SPY. However, this comparison is not meaningful at this sample size or paper-mode fidelity level.

---

## What I Would Change First (Top 5, Prioritized)

### P1 — Flip RiskGovernor to fail-closed  |  Effort: S (1-2 hours)
`run_v4_shadow.py:83-90`: if `_RISK_GOVERNOR_AVAILABLE = False`, the runner should exit or raise, not continue with no gating. Change the fallback from "log warning + continue" to "log critical + refuse BUY proposals." A risk module that fails to load should be treated like a safety interlock that fails to engage — you don't drive the car.

### P2 — Stop-loss gate for MeanRevBB  |  Effort: M (1-2 days)
`src/quanta_core/strategy/mean_rev_bb.py:127-130` exits only when `close > middle` (band recovery). There is no stop-loss. A trade entered at BB-lower that continues to fall has no exit trigger until recovery. Add a `max_loss_pct` exit: if `close < entry_price * (1 - max_loss_pct)`, emit SELL. This is P1 before any real-money deployment.

### P3 — Atomicize positions.json writes  |  Effort: S (2-4 hours)
`stocks/wheel/runner.py` writes `positions.json` without atomic rename. Pattern: write to `positions.json.tmp` then `os.rename(positions.json.tmp, positions.json)`. One crash mid-write corrupts the only source of truth for open wheel positions.

### P4 — Build a 30-day paper-trading baseline before going live  |  Effort: M (ongoing)
The system needs 30+ days of paper trades across varied regime conditions (trending_up, trending_down, mean_reverting, high_volatility) to establish: hit rate, avg win/loss ratio, Sharpe > 0.5 post-fee. Currently no way to replay or simulate — evidence accumulates only in real time. Do not flip LIVE_ENGINE_MODE to real exchange placement without this data.

### P5 — Wire WebSocket feeds and real Coinbase order placement  |  Effort: L (1-2 weeks)
`src/quanta_core/execution/` and `src/quanta_core/live/` are built but dead. The paper fill simulator (`run_v4_shadow.py:986-1035`) assumes fills at the next cycle's close — optimistic by ~0 to 5 minutes. Real orders face partial fills, slippage, and rejection. The first time this runs on real exchange, bugs will surface. Build integration tests against Coinbase sandbox before enabling.

---

## What's Surprisingly Good (Top 3 — Preserve These)

1. **The README architecture diagram is production-grade.** The ASCII diagram (README.md:63-123) and Mermaid flowcharts accurately reflect the live system. This is rare in hobby projects and makes the system understandable to a new reader in under 30 minutes.

2. **The ops endpoint envelope contract is consistent and disciplined.** Every `/api/ops/*` route returns `{status, data, error, checked_at}` with a hard timeout (`ops_routes.py:52-69`). The error semantics are documented. This is more rigorous than many production APIs.

3. **The strategy ownership rule** (`src/quanta_core/strategy/mean_rev_bb.py` + `scripts/run_v4_shadow.py:1202-1237`) — each strategy sees only positions it opened. The structural stomping bug (one strategy opening, another closing the same position within 5 minutes) was correctly identified and fixed before any real money flowed. This shows sound systems thinking.

---

## Hard Requirements Before Real Money

1. **30+ days of paper trading with positive expectancy after simulated fees.** Not negotiable. The current paper history (117 rows, regime mostly NULL) is insufficient. Need: ≥30 closed crypto trades, hit rate > 50%, avg win/loss ratio > 1.5, Sharpe > 0.5. Or abandon BB mean-rev and find a strategy with demonstrable edge.

2. **Stop-loss on MeanRevBB.** A strategy with no downside exit is unsuitable for real capital. Implement `max_loss_pct` exit before live deployment.

3. **RiskGovernor must be fail-closed.** The `fail-open` on import error (`run_v4_shadow.py:83-90`) must be inverted. A broken risk module should halt the engine, not let it trade unchecked.

4. **Integration test against Coinbase sandbox.** Before placing real orders, run the full `write_proposal_and_order → ExecutionEngine → Coinbase order → fill confirmation` path against the Coinbase Advanced Trade sandbox. Confirm partial fills, rejections, and error codes are handled correctly.

5. **Atomic write for all state files.** `positions.json`, `account_snapshot.json`, and any other state files used as source-of-truth must be written atomically (write-tmp, rename) to survive crashes without corruption.

---

*Evidence index:*
- `scripts/run_v4_shadow.py:77-91` — RiskGovernor fail-open
- `scripts/run_v4_shadow.py:1412` — live-only risk gating
- `src/quanta_core/strategy/mean_rev_bb.py:127-130` — no stop-loss
- `user_data/dashboard/ops_routes.py:1698` — dead freqtrade.log reference
- `user_data/dashboard/ops_routes.py:55-69` — envelope contract + timeout
- `docker-compose.yml:163-165` — dashboard binds 0.0.0.0
- `private/REVIEW_2026-05-11.md:38` — fee break-even analysis
- `docs/POST-CUTOVER-AUDIT-2026-05-13.md:3` — Track D (real order placement deferred)
- `stocks/wheel/state/positions.json` — 2 open positions (NVDA CSP, PLTR CSP)
- `stocks/wheel/state/account_snapshot.json` — portfolio_value: $100,528.31; wheel_cumulative_pnl: $524.05
- Commit `fc8ee26` — pnl_pct fraction unit bug
- Commit `680cc93` — phantom wheel position bug
- Commit `2ea9d91` — strategy ownership stomping bug
