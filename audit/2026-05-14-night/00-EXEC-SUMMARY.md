# Night Audit — Executive Summary
**Run:** 2026-05-14 night (UTC 00:55 → 04:00 wall)
**Teams:** 7 read-only agents in parallel · 0 mutations across the system
**Reports:** `audit/2026-05-14-night/0[1-7]-*.md` · 12 screenshots in `shots/`
**Bot state during audit:** Paper-mode live, 1 open V4 ETH/USD long, 2 wheel CSPs (NVDA, PLTR)

---

## TL;DR — 5 lines

1. **Live trading is not broken.** All 7 teams report **0 P0**. Backend APIs all 200, frontend renders cleanly (0 console errors across 12 pages), V4 ledger is consistent (354/354/354 proposals/orders/fills, zero orphans), tonight's noise gates + dashboard pill fix verified working.
2. **Highest-priority real issue: sentiment scorer has been silently zeroed for ~21 hours.** Since 2026-05-14 03:30 UTC, every `sentiment_log` row has `sentiment_score=0, confidence=0, llama_score=NULL, market_impact='neutral'` despite headlines flowing. Any agent reading sentiment is reading flat zero.
3. **Architecture review: confidence to run real money = 3/10.** Top blockers: (a) no demonstrated alpha — primary BB mean-reversion strategy is below the Coinbase fee floor per your own `private/REVIEW_2026-05-11.md:38`; (b) `RiskGovernor` fails OPEN at `scripts/run_v4_shadow.py:83-90` — if the risk module fails to import, every BUY is approved; (c) `MeanRevBB` has no stop-loss.
4. **ModelForge has structural gaps.** 5 critical tables empty (`evolution_tracks`, `evolution_runs`, `generations`, `track_generations`, `training_samples`); recurring "column does not exist" errors in `mf-postgres` log; champion `run-d4dac705` exists on disk but has no DB row.
5. **Code surface is healthy.** Frontend (tsc + eslint + npm build all clean). Python has 970 ruff issues but 0 syntax errors and only 2 undefined-name (F821) hits — both in `stocks/shark/phases/pre_market.py` for symbol `HistoricalEdge`.

---

## Severity tally across all 7 teams

| Team | Scope | P0 | P1 | P2 | P3 |
|---|---|---|---|---|---|
| T1 | Backend APIs (45 routes) | 0 | 1 | 4 | 2 |
| T2 | ModelForge stack | 0 | 3 | 3 | 6 |
| T3 | Frontend Playwright (12 pages) | 0 | 0 | 4 | 1 |
| T4 | Postgres data integrity (19 tables) | 0 | 3 | 4 | 4 |
| T5 | Cron + notifications (34 jobs, 17 logs) | 0 | 3 | 6 | 5 |
| T6 | Code quality (ruff/mypy/tsc/eslint/build) | 0 | 2 categories | many | — |
| T7 | Architecture / production-readiness review | 0 | 5 hard requirements | — | — |
| **Total** | | **0** | **17+** | **21+** | **18+** |

---

## P0 (live broken) — NONE

No tracebacks in container logs. No 5xx. No corruption. No stuck queues. No wedged crons. Tonight's three deploys (Telegram noise gate, dashboard pill gate, wheel_snapshot cadence + auto-launch) verified live.

---

## P1 — fix this week (ordered by blast radius)

### 1. Sentiment scorer silently zeroed since 2026-05-14 03:30 UTC  ·  T4-P1
**Evidence:** `audit/2026-05-14-night/04-data-integrity.md` §P1-1. 100% of last 4h of `sentiment_log` rows are zero/null. `n_headlines=60` so the news fetcher works — the LLM (Ollama hermes3:8b) or its parser is the failure point. **Impact:** every strategy that reads sentiment sees permanent neutral; this changed silently 21h ago.
**Investigate:** Ollama health on the Spark, `sentiment_engine` cron exception trace since 03:30 UTC.

### 2. RiskGovernor fail-OPEN on import failure  ·  T7
**File:** `scripts/run_v4_shadow.py:83-90`. If `quanta_core.risk.governor` fails to import, `_RISK_GOVERNOR_AVAILABLE=False` and every BUY is approved unconditionally. **This is the single largest correctness-vs-safety gap before real money.**
**Fix direction:** invert to fail-closed — log critical and refuse all BUY proposals, or hard-exit the runner.

### 3. MeanRevBB has no stop-loss  ·  T7
**File:** `src/quanta_core/strategy/mean_rev_bb.py:127-130`. Exits only when `close ≥ middle band`. A trade that keeps falling has no exit until recovery. Combined with #2, a trending-down regime + broken risk gate = unbounded loss.

### 4. ModelForge: 5 critical tables empty + recurring schema errors  ·  T2-P1
**Tables:** `evolution_tracks`, `evolution_runs`, `generations`, `track_generations`, `training_samples` all 0 rows. Champion `run-d4dac705` is on disk but not in DB. `mf-postgres` log shows recurring `column "has_adapter" does not exist`, `column "id" does not exist`, `column "cron_schedule" does not exist` — registry code is querying a schema the DB doesn't have. ModelForge isn't crashing but it's not learning anything either; the trading-bot dashboard ↔ mf-api integration is live but reads stub data.

### 5. Three on-chain hypertables empty, equity_snapshots empty  ·  T4-P1-2/3
`exchange_netflow`, `mvrv_ratio`, `whale_transactions` all 0 rows; `quanta_schema.equity_snapshots` empty (no daily equity → no drawdown timeseries). Decide: drop the tables (per memory the on-chain ones are deprecated for `derivatives_features`) or restore the feeders.

### 6. `/api/ops/gates` 3.7-4.0 s latency  ·  T1-P1
33 KB payload, no failures, but the dashboard polls this on the gates card. P1 because it's a UX drag and a future regression risk.

### 7. wheel positions.json non-atomic  ·  T7
`stocks/wheel/state/positions.json` written without `write-tmp + rename`. A crash mid-write corrupts the only source of truth for open positions. Backup file `positions.json.backup-pre-reconcile-2026-05-13` proves this has bitten before.

### 8. STOCKS UNTRUSTED still pings risk_monitor after-hours  ·  T5-P1-2
The dashboard pill is now hidden (T3 confirmed), and the Telegram suppression gate works (T5 verified ~77% suppression on risk_monitor). But the *first* SOFT post after market close still fires before dedup kicks in. Either widen the trust window after 16:00 ET or suppress UNTRUSTED tag entirely outside 09:00-16:30 ET.

### 9. F821 undefined name `HistoricalEdge`  ·  T6-P1
`stocks/shark/phases/pre_market.py:30` and `:284`. If hit at runtime → `NameError`. Either import or remove.

### 10. mypy 32 high-signal sites: `[index]` (21) + `[union-attr]` (11)  ·  T6-P1
Highest concentration in `user_data/dashboard/ops_routes.py` and `user_data/modules/onchain_signals.py`. Each is a potential `KeyError`/`AttributeError` at runtime.

### 11. `daily_pnl_report` cron-daily-pnl.log gap  ·  T5
The 3-day psycopg ImportError gap is fixed (FIX-F shipped today, confirmed) but no backfill of the missed daily P&L Slack messages for May 12-14. Decide: backfill manually or accept the gap.

### 12. `capital_rebalance_14d` has no schedule  ·  T5-P2-2
`enabled: true`, `schedule: null`, `last_run_at: null` since job created. Either give it a cron or delete.

---

## P2 — fix this month

13. Stocks-ML training freshness 75h (`/api/ops/stocks_ml`) — likely intentional weekly cadence, confirm.
14. V4 screening 2/5 names show `regime: unknown` (75h stale `last_setup_ts`).
15. ModelForge polls heavily on `/dashboard` — ~38 distinct API calls within seconds of load. Not failing, but watch if cost rises.
16. `cryptocurrency_cv` source returns HTTP 403 every 15 min — dead source, aggregator continues without it.
17. 124 em-dashes in `/docs` — all intentional copy, noted for completeness.
18. `<stdin>:113: DeprecationWarning datetime.utcnow()` repeats in every market_research run — Python 3.13 will break.
19. ruff 970 issues — 557 are auto-fixable safely (UP017, I001, F401, UP045, UP035, UP006, UP037). One `ruff check --fix` pass clears ~57%.
20. mypy 16 `[assignment]` errors — review individually.
21. 118/199 historical `trade_journal` rows still have `regime IS NULL` (the 2026-05-13 → 2026-05-14 gap diagnosed earlier today). Backfill SQL is in `audit/2026-05-14-regime-null-gap.md`.
22. ModelForge HF cache 180 GB — disk pressure if not pruned.
23. ModelForge tradebot auth fails (stale token), archived adapter backlog.

---

## P3 — observation only (18+ items)

Captured per-team in their respective reports. Mostly: missing optional crons (Sat/Mon-only), expected weekly cadences, deprecation warnings, dead-code references guarded by `if exists`, etc.

---

## What's in good shape (preserve these)

- **V4 paper ledger integrity** — `quanta_schema.proposals → orders → fills` all 354 rows, 0 orphans, joins cleanly to `trade_journal` for last 24h (T4).
- **Frontend** — 0 console errors across 12 visited pages, real Playwright browser, 0 NaN/undefined leakage. Tonight's STOCKS-UNTRUSTED pill fix verified hidden when `market_open_now=false` (T3).
- **Backend envelope contract** — 45/45 routes return the `{status, data, error, checked_at}` shape with hard 3.5s timeout. More disciplined than most production APIs (T1, T7).
- **Notification suppression gates** — Tonight's deploys are firing in production: 77% suppression on `risk_monitor_15min`, 26% on `market_research_30min`. Telegram noise problem solved (T5).
- **Frontend code quality** — `tsc` + `eslint` + `npm run build` all clean. `node --check` clean on all 43 dashboard JS files (T6).
- **Operational discipline** — `.env` gitignored, secrets gitignored, postgres bound to loopback, dashboard MCP key gates mutations with `hmac.compare_digest`, READMEs are accurate (T7).
- **Cost** — $0/month paid LLM in hot path. Hardware owned. APIs free-tier (T7 score 9/10).

---

## What I would do tomorrow morning, in order

**Before market open (09:30 ET):**
1. **Investigate sentiment scorer** (#1) — check Ollama on the Spark, look at `sentiment_engine` logs since 03:30 UTC. This is hot — the bot has been trading on flat-zero sentiment for 21h.
2. **Decide whether to backfill the 118 NULL regime rows** (#21) — SQL is ready in `audit/2026-05-14-regime-null-gap.md`.

**This week:**
3. **Flip RiskGovernor to fail-closed** (#2) — 1-2h, must-do before any live trading.
4. **Add stop-loss to MeanRevBB** (#3) — 1-2 days, must-do before any live trading.
5. **Atomic-write positions.json** (#7) — 2-4h.
6. **ModelForge schema reconciliation** (#4) — investigate why registry queries reference columns the DB doesn't have. Possibly an alembic migration that was never applied.

**This month:**
7. **30-day paper-trading baseline with positive expectancy after simulated fees** before any real-money flip (T7 hard requirement #1).
8. **Auto-fix ruff modernization issues** (~557 of 970) — single `ruff check --fix --select UP017,I001,F401,UP045,UP035,UP006,UP037` pass.
9. **Mobile-friendly ops surface** — operator gets Telegram alerts on phone but can't pause from phone. T7 §2.
10. **Build integration test against Coinbase sandbox** before wiring real order placement (T7 hard requirement #4).

---

## What you decided NOT to fix tonight (per scope)

- No fixes were applied. All 7 teams are read-only by design. Synthesis is the deliverable.
- Sentiment scorer is broken right now but the operator review-before-push policy applies — diagnosed and surfaced for tomorrow.
- ModelForge schema mismatches are real but mf-api isn't crashing; investigation needed before action.

---

## Cross-references

- **Today's fix work (already shipped):** commits `0663b10` + `170fecd` on main.
- **Regime-NULL gap (yesterday's diagnosis, shipped tonight):** `audit/2026-05-14-regime-null-gap.md`.
- **Per-team detailed reports:** `audit/2026-05-14-night/01-...07-*.md`.
- **Frontend screenshots:** `audit/2026-05-14-night/shots/01-12-*.png`.

---

*Synthesis written by orchestration agent after all 7 teams returned. Total compute: ~25 min agent wall clock across parallel team execution; ~5 min orchestration. No mutations performed.*
