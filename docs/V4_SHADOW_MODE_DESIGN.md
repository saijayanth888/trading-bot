# V4 shadow-mode design — the freqtrade → quanta_core cutover blueprint

**Status:** Draft · 2026-05-13
**Owner:** Operator + Claude
**Target sprint window:** 2-3 weeks from sign-off to cutover
**Related memory:** [[feedback-v4-is-additive]] · [[project-session-2026-05-12-v4-wave2-merged]]

---

## 1. Context

Quanta V4 (`src/quanta_core/`) is fully merged into `main` as additive
code: 1338 tests passing, 7 wave-2 modules + reconciled wave-1 base.
Nothing in the running stack imports it yet. Freqtrade is still **the**
live trading engine; the dashboard reads from freqtrade's REST API +
Postgres, just like before.

The operator's standing rule (2026-05-12 ~23:50 ET, recorded as
[[feedback-v4-is-additive]]) was "I don't want to use V4 I want to use
existing apps." That rule was about not breaking what's working — it
did NOT close the door on the cutover. This document plans how to get
from "V4 is dead code on disk" to "V4 is the live trading engine" with
zero operator-visible regressions and a hard rollback at every step.

## 2. Definition of shadow mode

**Shadow mode** = V4's `quanta_core.live.engine.LiveEngine` runs as a
second process alongside freqtrade, fed the same market data, executing
the same strategy class — but writing only to its own ledger
(`quanta_schema` postgres tables) and observability buffer
(`user_data/v4_runtime/*.jsonl`). **No orders are placed by V4 in
shadow mode.** Freqtrade remains the only thing that talks to Coinbase
+ Alpaca.

```
                                          ┌─ shadow-only ─┐
   Coinbase WS ──┬─► freqtrade ──► orders │              │
                 │      │                 │              ▼
                 │      └─► trades_sqlite │       quanta_schema
                 │                        │       (postgres)
                 └─► quanta_core ─► decisions ────►──┘
                       (read-only,           │
                        no orders)           ▼
                                       parity_oracle ──► user_data/v4_runtime/parity.jsonl
                                                              │
                                                              ▼
                                                /api/v4/parity dashboard card
```

## 3. Decision schema (already in the ledger)

The V4 ledger ships a `decisions` table (see `src/quanta_core/ledger/schema.sql`):

```sql
CREATE TABLE decisions (
    id        BIGSERIAL,
    ts        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    symbol    TEXT,
    strategy  TEXT,
    debate    JSONB NOT NULL,
    outcome   TEXT NOT NULL,        -- 'LONG' | 'SHORT' | 'FLAT'
    rationale TEXT,
    PRIMARY KEY (id, ts)
);
```

Shadow mode adds one column at runtime (no migration needed yet —
written to `debate.context.shadow_of_trade_id`):

- `shadow_of_trade_id`: the freqtrade trade id the V4 decision
  shadowed. NULL when freqtrade chose FLAT and V4 chose to evaluate
  the same bar anyway.

The parity oracle reads from both `quanta_schema.decisions` and
`tradesv3.sqlite.trades` (freqtrade's ledger), correlates on
(symbol, ts_bucket=5min), and writes diffs to
`user_data/v4_runtime/parity.jsonl`.

## 4. Parity rules (from `src/quanta_core/observability/parity_oracle.py`)

For each correlated pair `(freqtrade.side, v4.side)`:

| freqtrade | V4    | verdict    |
|-----------|-------|------------|
| LONG      | LONG  | `agree`    |
| SHORT     | SHORT | `agree`    |
| FLAT      | FLAT  | `agree`    |
| LONG      | SHORT | `conflict` |
| SHORT     | LONG  | `conflict` |
| LONG      | FLAT  | `abstain`  |
| SHORT     | FLAT  | `abstain`  |
| FLAT      | LONG  | `abstain`  |
| FLAT      | SHORT | `abstain`  |

`agree` counts as parity; `conflict` is a hard fail; `abstain` is
graded weight 0.5 (V4 might be more conservative, which we'll tune).

## 5. Cutover gate

V4 is promoted from shadow to live ONLY when ALL of these hold for
5 consecutive calendar days:

1. **Parity rate ≥ 85%** measured over the last 7 days.
2. **Zero V4 crashes** in `quanta_core` logs (`docker logs trading-bot-v4`).
3. **Ledger writes match**: every V4 `decision` row has a corresponding
   non-FLAT freqtrade trade within 5 min (no orphan shadow decisions).
4. **No conflicts on high-conviction trades**: any V4 conviction > 0.80
   that disagrees with freqtrade halts the gate and requires an
   operator review.
5. **Dashboard parity card is green**: `/api/v4/parity` returns
   `consecutive_days_ok ≥ 5`.

The gate is checked daily by `scripts/v4_parity_gate.sh` (to be
written in week 2) which writes its verdict to
`user_data/v4_runtime/cutover_gate.json`. The dashboard surfaces this
as a YES/NO card; cutover requires operator sign-off, never automatic.

## 6. Cutover mechanics

When the operator types "cutover V4 now":

1. **Pause freqtrade** via existing `POST /api/ops/pause` (auth gated).
2. **Drain freqtrade open positions** to their natural exit (or operator
   forces FLAT-ALL via existing kill-switch).
3. **Flip env**: `LIVE_ENGINE=quanta_core` (new env, default `freqtrade`).
4. **Start `trading-bot-v4` container** (Dockerfile already exists at
   `Dockerfile.quanta_core`).
5. **Stop freqtrade container** (keep image; rollback path).
6. **Dashboard auto-discovers** V4 via the existing service-probe envelope.

Rollback (any step fails or operator says "rollback"):

1. `LIVE_ENGINE=freqtrade` (the default), `docker compose up -d freqtrade`.
2. V4 decisions become read-only / advisory; V4 container stays up but
   stops calling `execution.engine.place_order`.

## 7. Risks

| Risk | Mitigation |
|------|-----------|
| Ollama hermes3:70b debate latency > 30s blocks decisions during volatile bars | Pre-cutover: tune to hermes3:8b for time-critical roles; 70b only for slow strategies (wheel, weekly) |
| Postgres contention between freqtrade SQLite and V4 postgres | V4 ledger is on `quanta_schema`; no shared rows. SQLite stays on freqtrade. |
| Dashboard renders both feeds during transition — confusing | `/api/ops/*` is freqtrade-only by definition; `/api/v4/*` is V4-only. SPA already segments. |
| GPU contention if Hermes is busy with LoRA training | [[reference-gpu-reservation]] already gates LoRA window; debate path goes around it. |
| Cold-start: V4 has no historical decisions; parity card shows 0/5 days | Backfill: run V4 in dry-run mode against past 14 days of candles, populate `decisions` table from history, then start shadow live. |

## 8. Timeline

**Week 1 (this week, after operator sign-off):**
- Day 1: ship `scripts/v4_shadow_runner.sh` — cron-fired every 5 min,
  invokes `quanta_core.live.engine.LiveEngine.run_once()` with read-only
  exchange adapter.
- Day 2: ship parity oracle cron (`scripts/v4_parity_oracle.sh`).
- Day 3-7: monitor; flag any V4 crash via existing Slack notifier.

**Week 2:**
- Day 1: dashboard parity card (`/api/v4/parity` already exists; add
  `consecutive_days_ok` real computation, not the mock).
- Day 2: cutover gate script (`scripts/v4_parity_gate.sh`).
- Day 3-5: daily gate verdicts; tune parity threshold if needed.

**Week 3 (cutover candidate):**
- Day 1: operator review of gate verdict.
- Day 2: dry-run cutover into a staging compose (no orders).
- Day 3: production cutover (if gate green).

## 9. What we DON'T do tonight

This is a planning doc, not a runtime change. Tonight (2026-05-13
overnight session) we landed:

- ✅ `src/quanta_core/observability/v4_buffer.py` (live-data substrate)
- ✅ `src/quanta_core/observability/parity_oracle.py` (`compare_decisions`)
- ✅ `/api/v4/{debate/history,parity,montecarlo}` read live buffer + mock fallback
- ❌ Did NOT start a V4 process (no `LiveEngine.run_once()` cron yet)
- ❌ Did NOT touch freqtrade
- ❌ Did NOT migrate the ledger or create the v4 container

Next operator green-light triggers Week 1 Day 1 of the timeline above.

## 10. References

- `src/quanta_core/live/engine.py` — `LiveEngine` class (134 tests, 99.80% cov)
- `src/quanta_core/ledger/schema.sql` — `decisions` table + migrations
- `src/quanta_core/observability/parity_oracle.py` — `compare_decisions` (this session)
- `user_data/dashboard/v4_routes.py` — dashboard surface (`/api/v4/*`)
- `docs/superpowers/plans/2026-05-13-overnight-v4-wiring-and-bug-free-paper-trading.md` — the executing plan this doc sits inside
