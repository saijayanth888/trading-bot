# HANDOFF — feat/v4-build-execution

**Branch:** `feat/v4-build-execution` (off `main`)
**Scope:** `src/quanta_core/execution/` module + `tests/execution/` + project scaffold
**Status:** All gates green; ready for review + merge into `feat/v4-build`.

---

## Deliverables

| File | LOC | Purpose |
|---|---|---|
| `src/quanta_core/execution/engine.py` | 736 | `ExecutionEngine.submit / cancel / cancel_all`; port of `user_data/modules/execution_engine.py` with both P0 fixes |
| `src/quanta_core/execution/idempotency.py` | 298 | `IdempotencyStore` — reserve / commit / find_existing / cleanup, SQLAlchemy 2.x + psycopg 3 |
| `src/quanta_core/execution/slippage_gate.py` | 114 | Pure `passes(...)` function — drift + stale-quote + market-order bypass |
| `src/quanta_core/execution/order_state_machine.py` | 160 | Strict NEW → SENT → ACK → PARTIAL_FILL → FILLED state model |
| `src/quanta_core/execution/__init__.py` | 67 | Public surface — `__all__` only |
| `tests/execution/conftest.py` | 195 | In-memory SQLite engine + FakeExchange + FakeLedger |
| `tests/execution/test_engine.py` | 897 | Happy path, gates, retry, P0-4, replay, cancel_all, edge cases |
| `tests/execution/test_idempotency.py` | 221 | Reserve/commit/abandon/find/cleanup, TTL semantics |
| `tests/execution/test_order_state_machine.py` | 139 | Every legal edge + every illegal pair (exhaustive) |
| `tests/execution/test_slippage_gate.py` | 210 | Boundary cases + stale + market + invalid prices |
| **Total** | **3037** | (~1075 src + ~1662 tests + 300 scaffold) |

Plus scaffold added at the project root (this branch is the first build agent to land):

- `pyproject.toml` — PEP 621 single config (hatchling + ruff + mypy + pytest + coverage)
- `src/quanta_core/__init__.py`, `py.typed`
- `.gitignore` — appended `.venv-exec/`, caches

## Port % from `user_data/modules/execution_engine.py`

| Legacy capability | Port location | Status |
|---|---|---|
| Slippage gate | `slippage_gate.passes` | Ported + extracted into pure function; adds stale-quote + market-order bypass |
| Exponential-backoff retry | `engine._place_with_retry` + `_RetryPolicy` | Ported, **P0 fix applied** (see below) |
| `client_order_id` idempotency | `idempotency.IdempotencyStore` | Promoted from in-memory dict → durable Postgres row + unique index + 7d TTL |
| Audit log (rotating file) | `logger = logging.getLogger(...)` calls throughout | Demoted to structlog hook; `~/.quanta/logs/quanta-core.jsonl` is owned by `quanta_core/logging.py` (next agent) |
| Threading.Lock per-order poll | **Dropped** — sync API; monitor loop moves to `live.engine` per DESIGN-LOCK §1.3 (no `threading.Timer`) |
| Dry-run path | **Dropped** — replaced by adapter swap (`paper=True` in alpaca-py / Coinbase sandbox); the engine doesn't know live vs paper |
| `_extract_order_id` (SDK shape munging) | Moves into each `ExchangeAdapter`; engine takes a normalised `OrderResponse` |

**Port coverage:** ~85 % of legacy behaviour preserved; ~15 % deliberately dropped per DESIGN-LOCK (threading, dry-run, SDK plumbing — those move to per-venue adapters and `quanta_core/live/engine.py`).

## P0 fixes applied

### P0-4a — `_cancel` no longer ignores the venue response

**Legacy bug** (`user_data/modules/execution_engine.py:566-570`):

```python
def _cancel(self, order_id, client_order_id):
    if self.cfg.dry_run:
        return
    client = self._ensure_client()
    client.cancel_orders(order_ids=[order_id])  # response thrown away
```

If the cancel raced a fill, the fill was lost from the ledger. We lost ~$340 in unaccounted fills in 2026-04 before catching it.

**Fix** (`engine.cancel`, lines 459–490):

- Engine reads `response.status` after the cancel call.
- If status ∈ `{FILLED, PARTIAL, PARTIALLY_FILLED, PARTIAL_FILL}` the engine:
  1. Builds a `Fill` object,
  2. Calls `ledger.record_fill(fill)`,
  3. Promotes the idempotency row to `committed`,
  4. Returns `CancelOutcome.ALREADY_FILLED`.
- `cancel_all` routes through the same logic.

Covered by tests:
- `test_cancel_records_fill_on_partial_fill_race`
- `test_cancel_records_fill_on_full_fill_race`
- `test_cancel_all_mixed_outcomes`
- `test_cancel_filled_without_reservation_still_records`
- `test_cancel_all_filled_without_reservation_records_anyway`

### P0-4b — `_retry_order` distinguishes 5xx from 4xx

**Legacy bug** (`user_data/modules/execution_engine.py:297-318`): the retry loop catches `except Exception`, retrying everything. A 422 "duplicate client_order_id" from Alpaca would be retried 3× with backoff — creating the phantom-order class of bug since the venue had actually accepted the first attempt.

**Fix** (`engine._RetryPolicy.should_retry`, lines 250–262):

```python
def should_retry(self, exc: BaseException) -> bool:
    if isinstance(exc, RetryableError):     # 5xx
        return True
    if isinstance(exc, ExchangeError):      # 4xx — any
        return False
    return isinstance(exc, (TimeoutError, ConnectionError, OSError))
```

The new exception hierarchy:

- `ExchangeError(status_code, message)` — base class for any venue-side error.
- `RetryableError(ExchangeError)` — used only for 5xx; safe to retry.
- 4xx (auth, validation, rate-limit-throttle, duplicate) raises `ExchangeError` directly → terminal rejection.

Covered by tests:
- `test_5xx_retries_then_succeeds` (3 attempts, succeeds on 3rd)
- `test_5xx_exhausts_retries` (3 attempts, surface as `http_503`)
- `test_4xx_never_retries` (422 → no retry)
- `test_4xx_401_no_retry`
- `test_connection_error_retries`
- `test_timeout_retries`
- `test_retry_policy_classification` (unit test of the predicate alone)

## Test + coverage summary

```
134 tests passed in 0.63 s

src/quanta_core/execution/__init__.py             100 %
src/quanta_core/execution/engine.py                99 %  (1 partial branch — defensive _lookup_side fallback)
src/quanta_core/execution/idempotency.py          100 %
src/quanta_core/execution/order_state_machine.py  100 %
src/quanta_core/execution/slippage_gate.py        100 %
─────────────────────────────────────────────────────────
TOTAL                                              99.80 %   (gate: 95 %)
```

The single remaining partial branch is `_lookup_side` falling through to the `BUY` default when an idempotency row exists but its `intent_json` has no `side` key — defensive forensic code that's effectively unreachable in production.

## Quality gates all green

```bash
$ ruff check src/quanta_core/execution/ tests/execution/    → All checks passed!
$ ruff format --check src/quanta_core/execution/ tests/...   → 11 files already formatted
$ mypy --strict src/quanta_core/execution                    → Success: no issues found in 5 source files
$ pytest tests/execution/ --cov=src/quanta_core/execution --cov-fail-under=95
                                                             → 134 passed, 99.80 % coverage
```

## Commit shas

(See `git log feat/v4-build-execution --oneline`.)

## How to run locally

```bash
cd <repo>
uv venv --python 3.12 .venv-exec
uv pip install --python .venv-exec/bin/python -e '.[dev]'
.venv-exec/bin/python -m pytest tests/execution/ --cov=src/quanta_core/execution
```

## Open items / notes for the next agent

1. **`quanta_core/logging.py` not in scope here.** The engine uses stdlib `logging.getLogger(__name__)`; swap to `structlog.get_logger()` once the logging module lands. No call-site changes required if structlog's `LoggerFactory` is bound to stdlib (per `docs/.../10-CODE_PATTERNS.md §1.5`).
2. **Live-vs-paper routing** stays out of the engine. The wiring agent (`feat/v4-build-live`) chooses which `Exchange` adapter to inject; this engine is venue-agnostic by design.
3. **Async wrap-up.** When the live engine's asyncio task group calls `submit()`, wrap it in `asyncio.to_thread(engine.submit, proposal)`. The sync API is deliberate — the existing legacy module is sync, the SDK calls are sync, and threading-vs-asyncio is a `live/engine.py` concern.
4. **Schema migration.** `IdempotencyStore.create_all()` is for dev / test only. Production needs a numbered migration in `postgres/init/0NN_execution_idempotency.sql`. Out of scope for this branch.
5. **DESIGN-LOCK note about asyncpg.** Doc 06 §3.17 says "No module other than `quanta_core/ledger/postgres.py` is allowed to import asyncpg." This module imports `sqlalchemy + psycopg`, not asyncpg, per the build spec — consistent with the broader pattern. The ledger module will own its own driver choice.

## What I did NOT touch

- `user_data/` / `stocks/` — live freqtrade stack untouched per DESIGN-LOCK §7.
- Hermes crons, ModelForge, dashboard — all integration concerns deferred.
- No push to remote. Branch is local; reviewer to merge into `feat/v4-build`.
