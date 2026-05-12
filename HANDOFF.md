# Wave 2 · Ledger + Observability — Build Agent #E HANDOFF

**Branch:** `feat/v4-wave2-ledger`
**Worktree:** `.claude/worktrees/agent-a28c77b60839f6a4a/`
**Status:** all gates green; NOT pushed (per rules)

## What shipped

```
src/quanta_core/
├── __init__.py
├── py.typed
├── ledger/
│   ├── __init__.py            # public exports: PostgresLedger, Proposal, Fill, Decision, errors
│   ├── errors.py              # LedgerError, ReservationConflictError, UnknownOrderError
│   ├── types.py               # frozen dataclasses (Proposal, Fill, Decision) with UTC validation
│   ├── postgres.py            # async psycopg 3 wrapper — ONLY module that touches psycopg
│   ├── schema.sql             # composed schema for inspection / one-shot bootstrap
│   └── migrations/
│       ├── 001_initial.sql    # core tables: reservations, proposals, orders, fills, decisions, equity_snapshots
│       └── 002_add_indices.sql # perf indices + TimescaleDB hypertables (gated by extension presence)
└── observability/
    ├── __init__.py
    ├── metrics.py             # Counter / Gauge / Histogram + JSONL audit sink
    ├── notifier.py            # SlackNotifier (httpx) + LogOnlyNotifier; dedup window; severity routing
    └── healthcheck_publisher.py  # stdlib HTTP server serving /health from ~/.quanta/state/*.json
```

Plus tests under `tests/ledger/` (`test_types.py`, `test_errors.py`, `test_migrations.py`,
`test_postgres.py`, `_fake_pg.py`, `conftest.py`) and `tests/observability/`
(`test_metrics.py`, `test_notifier.py`, `test_healthcheck_publisher.py`).

`pyproject.toml` added at the worktree root with `asyncio_mode=auto`, ruff/mypy strict
config, and the canonical V4 dependencies (`psycopg[binary,pool]>=3.2,<4`, `httpx>=0.27,<1`).

## Schema diagram

```
            ┌─────────────────────────────┐
            │      reservations           │  PK: client_order_id (TEXT)
            │  (idempotency reserve slot) │
            └─────────────────────────────┘
                       (separate; not FK)

            ┌─────────────────────────────┐         ┌──────────────────────────────┐
            │         proposals           │ ──FK──> │           orders             │
            │  PK: client_order_id        │         │  PK: client_order_id         │
            │  venue, symbol, side, qty,  │         │  status PROPOSED → ACKED →   │
            │  limit_price, strategy,     │         │         PARTIAL → FILLED |   │
            │  intent JSONB, created_at   │         │         CANCELLED | REJECTED │
            └──────────────┬──────────────┘         │  exchange_order_id,          │
                           │                        │  cancel_reason, last_update  │
                           │                        └──────────────────────────────┘
                           │
                           ▼
            ┌─────────────────────────────┐
            │           fills             │  PK: (id, ts) — TimescaleDB-hypertable-ready
            │  FK: client_order_id        │
            │  qty, price, fee, side, ts  │
            └─────────────────────────────┘

            ┌─────────────────────────────┐         ┌──────────────────────────────┐
            │         decisions           │         │      equity_snapshots        │
            │  PK: (id, ts) hypertable    │         │  PK: ts hypertable           │
            │  debate JSONB, outcome,     │         │  equity, unrealized,         │
            │  rationale                  │         │  drawdown_pct, cash          │
            └─────────────────────────────┘         └──────────────────────────────┘
```

## Migration list

| Version | File | Purpose | Idempotent |
|---|---|---|---|
| 001 | `001_initial.sql` | Six core tables + `quanta_schema_version` log | yes (`IF NOT EXISTS`) |
| 002 | `002_add_indices.sql` | 8 perf indices + optional Timescale hypertables (extension-gated) | yes |

The migration runner (`PostgresLedger.migrate()`) reads `migrations/*.sql` in lexical
order, applies any file whose numeric prefix is greater than the highest version in
`quanta_schema_version`, and INSERTs the version on success. Re-running migrate
after a successful pass returns `[]`.

## Test coverage

```
src/quanta_core/ledger/errors.py                       100%
src/quanta_core/ledger/postgres.py                     100%   (defensive RuntimeError branches marked pragma: no cover)
src/quanta_core/ledger/types.py                        100%
src/quanta_core/observability/healthcheck_publisher.py  99%
src/quanta_core/observability/metrics.py                99%
src/quanta_core/observability/notifier.py              100%
TOTAL                                                   99%
```

121 tests pass.

## Verification gates

* `ruff check src/quanta_core tests/ledger tests/observability` → `All checks passed!`
* `ruff format --check src/quanta_core tests/ledger tests/observability` → clean
* `mypy --strict src/quanta_core tests/ledger tests/observability` → `Success: no issues found in 20 source files`
* `pytest tests/ledger tests/observability` → 121 passed, 0 failed
* Existing repo tests (`tests/`, `stocks/tests/`) unaffected — 3 pre-existing
  failures are present on `main` and are NOT introduced by this branch
  (`tests/test_tft_pickle.py::test_torch_save_roundtrip_via_wrapper`,
  `stocks/tests/test_llm_logger.py::TestNoFalsePositives::test_normal_url_not_path_redacted`,
  `stocks/tests/test_multi_agent.py::TestRiskDebate::test_no_api_key_skips`).

## Test backend

No real Postgres or `pytest-postgresql` was available in the build environment,
so the ledger tests run against `tests/ledger/_fake_pg.py` — an in-process fake
of the `psycopg.AsyncConnectionPool` surface. The fake dispatches by SQL
fragment (not a parser): any new SQL shape in `PostgresLedger` MUST add a
handler. When `QUANTA_TEST_POSTGRES_DSN` is set the fixtures in
`tests/ledger/conftest.py` ALSO run the same suite against a real
TimescaleDB; the in-process fake is the CI baseline per the build brief
("use testcontainers/pytest-postgresql for real DB roundtrip; otherwise mock").

## What the next agent needs to know

* `PostgresLedger` is the only module in the codebase allowed to import
  `psycopg` / `psycopg_pool`. Strategy / execution / risk talk to the ledger
  through this class.
* Idempotency rule: callers `await ledger.reserve(client_order_id, intent)` BEFORE
  any external side-effect. `ReservationConflictError` means "already in flight,
  nothing to do" — DO NOT retry.
* `MetricsRegistry` is process-global; obtain it via
  `quanta_core.observability.get_registry()`. The 4 canonical V4 metrics
  (`trades_total`, `risk_block_total`, `latency_seconds`, `ollama_latency_seconds`)
  are pre-registered.
* `SlackNotifier.notify()` returns `bool` — `False` means "dedup suppressed OR
  transport failed (logged)". Trading code MUST NOT rely on the return value to
  block a decision.
* `HealthcheckPublisher` reads `~/.quanta/state/*.json` produced by Hermes Layer 8
  state writers. It is intentionally stdlib-only (no FastAPI dep) so the
  healthcheck stays up even when the main engine is degraded.

## Commit shas

* `6332298` — feat(quanta_core): wave-2 ledger + observability — Postgres
  single-source-of-truth + metrics/notifier/healthcheck.
