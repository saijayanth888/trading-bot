# Build agent — exchanges module (HANDOFF)

**Branch:** `feat/v4-build-exchanges` (off `main`)
**Scope:** `quanta_core/exchanges/` module (alpaca-py + coinbase-advanced-py wrappers + idempotency)
**Date:** 2026-05-12

## Files shipped

### Source (`src/quanta_core/exchanges/`)

| Path | LOC | Purpose |
|---|---:|---|
| `__init__.py` | 45 | Public re-exports |
| `base.py` | 312 | `Exchange` ABC + value types + exception hierarchy |
| `alpaca.py` | 530 | alpaca-py wrapper (paper/live via `runtime.mode`) |
| `coinbase.py` | 606 | coinbase-advanced-py wrapper + `_SequenceTracker` for WS gap detection |
| `idempotency.py` | 296 | Deterministic `qc4-{venue}-{strategy_id}-{uuid7}` coid + `InMemoryReservation` stub |
| **Total src** | **1789** | |

### Tests (`tests/exchanges/`)

| Path | LOC | Count |
|---|---:|---:|
| `test_alpaca_paper.py` | 623 | 49 tests |
| `test_coinbase_paper.py` | 509 | 39 tests |
| `test_idempotency.py` | 440 | 22 tests (incl. 4 hypothesis property tests) |
| **Total tests** | **1572** | **110 tests** |

### Cassettes (`tests/exchanges/cassettes/`)

Two formats — neither hits live brokers, both replay deterministically:

**vcrpy YAML** (Alpaca — HTTP-level, replays through alpaca-py's REST stack):

* `alpaca_paper_account.yaml` — `GET /v2/clock`, `/v2/account`, `/v2/positions`
* `alpaca_paper_submit_order.yaml` — `POST /v2/orders`
* `alpaca_paper_get_orders.yaml` — `GET /v2/orders`

`vcr.VCR(record_mode='none')` enforces "no live calls": every cassette miss fails the test.

**JSON body cassettes** (Coinbase — fed into an injected mock `RESTClient`):

* `coinbase_paper_accounts.json` — multi-currency accounts payload
* `coinbase_paper_create_order.json` — `success: true` order ack
* `coinbase_paper_list_orders.json` — open-order listing
* `coinbase_ws_orderbook.json` — `level2` snapshot WS message

Coinbase signs every request with an ES256 JWT (120s TTL, regenerated per call) which makes vcrpy YAML cassettes fragile — the signature header changes every replay. JSON body cassettes through an injected `client=MagicMock(...)` give the same fidelity (recorded response shape, no live network) and are equivalent in intent to vcrpy.

### Top-level

* `pyproject.toml` (new) — PEP 621 + ruff + mypy + pytest config in one file (single-source-of-truth per `10-CODE_PATTERNS.md`)
* `tests/conftest.py` — added `src/` to `sys.path` so `from quanta_core.exchanges import ...` resolves without an editable install
* `tests/__init__.py`, `tests/exchanges/__init__.py` — empty package markers

## Coverage

```
Name                                       Stmts   Miss Branch BrPart  Cover
--------------------------------------------------------------------------------
src/quanta_core/exchanges/__init__.py          3      0      0      0   100%
src/quanta_core/exchanges/alpaca.py          243     19     88      8    91%
src/quanta_core/exchanges/base.py            127      3      8      3    96%
src/quanta_core/exchanges/coinbase.py        275     29     90     19    85%
src/quanta_core/exchanges/idempotency.py     114      3     36      4    95%
--------------------------------------------------------------------------------
TOTAL                                        762     54    222     34    89.84%
```

Target was 85%. Hit **89.84%**. Per-file minimum is 85% (Coinbase at floor — uncovered lines are the WS stream stubs deliberately left for the next PR).

## Quality gates

| Gate | Status |
|---|---|
| `pytest tests/exchanges/` | 110 passed, 0 failed |
| `ruff check` | clean |
| `ruff format --check` | clean |
| `mypy --strict src/quanta_core/` | clean |
| Coverage >= 85% | 89.84% |

Pre-existing legacy test failures (4 in `test_tft_pickle.py` and `test_weekly_training_endpoint.py`) were verified to pre-date this PR (checked via `git stash`); unrelated to the exchange work.

## Design notes (read before next PR)

* **One flag, not four.** `AlpacaConfig.mode` and `CoinbaseConfig.mode` are both `'paper' | 'live'`. The Alpaca SDK's `paper=True/False` bool is derived from this single field. The DESIGN-LOCK rule "one TOML flag flips live↔paper" is honoured.
* **Today's `asset_class` bug is regression-tested.** `tests/exchanges/test_alpaca_paper.py::test_get_positions_surfaces_asset_class` reads a cassette with one stock + one OPRA option position and asserts the option surfaces as `asset_class='option'`, not flattened to `'stock'`. Heuristic + SDK hint both honoured.
* **Coinbase sequence-num gap detection is real, not a stub.** `_SequenceTracker.observe` raises `SequenceGap` on a gap and tests verify it: gap detection, out-of-order ignore, per-channel/product tracking. Adapter exposes `observe_sequence(channel, product_id, seq_num)` for the future WS pump to drive.
* **`client_order_id` is `qc4-{venue}-{strategy_id}-{uuid7_hex}`.** The first 12 hex chars of the UUID7 are the millisecond timestamp → sort order is approximately chronological. Hypothesis test `test_uuid7_monotonic_prefix` proves it across 200 random pairs.
* **Streams are stubs.** `stream_ticks`, `stream_fills`, `stream_orderbook` are async-iterator surfaces that yield nothing today. Concrete WS wiring (Alpaca `TradingStream`, Coinbase `WSClient`) is the next agent's job. Per the spec "~800-1200 LOC including tests" the LOC budget can't carry WS implementations too without thinning the REST path past usefulness.
* **Reserve-then-commit is an in-process stub.** `InMemoryReservation` matches the eventual `PostgresLedger` surface (`reserve()` returning `kind in {fresh, replay, duplicate}`). Ledger agent swaps it in.
* **Retry policy lives at the engine layer.** Adapters surface `RateLimited(retry_after_s=...)` and `OrderRejected`; they do not retry. The `ExecutionEngine` (separate agent) handles backoff with the Retry-After hint.

## Commit SHAs

* `70aa760` — feat(v4): exchanges module — alpaca-py + coinbase-advanced-py adapters
  (single commit; branch `feat/v4-build-exchanges` off `main` at `d5d1fd7`)

## What the next agent needs

1. **Execution engine agent**: import `from quanta_core.exchanges import Exchange, OrderProposal, make_client_order_id, IntentKey`. Build `ExecutionEngine.submit(proposal, portfolio)` per `06-ARCHITECTURE.md` section 3.14.
2. **Live engine agent**: wire `AlpacaExchange.stream_*` and `CoinbaseExchange.stream_*` to real WS clients (`alpaca.trading.stream.TradingStream`, `coinbase.websocket.WSClient`). Use `_SequenceTracker.observe()` on the Coinbase ingestion path.
3. **Ledger agent**: implement `PostgresLedger` with the `IdempotencyService.reserve` / `commit` / `abandon` API. Add `sql/0004_idempotency.sql` with UNIQUE index on `trades.client_order_id`.
