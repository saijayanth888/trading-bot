# V4 Integration Smoke — what's covered, how to run

**Branch:** `feat/v4-wave2-integration` (off `feat/v4-build-reconciled`)
**Owner:** Agent #I (integration engineer)
**Status:** 24 tests, all green; `ruff` clean, `mypy --strict` clean.

The integration smoke suite is the V4 "Hello, World". It proves that the
typed events flow end-to-end through the reconciled stack with the same
wiring the live engine will use in production. Coverage is not the metric
here — **shape-correctness** is.

---

## How to run

```bash
# From the worktree root, with the package installed in editable mode:
pip install -e .[dev]
pip install sqlalchemy structlog

# Run the integration suite only:
python3 -m pytest tests/integration/ -v

# Run the whole quality gate (ruff + mypy + tests):
python3 -m ruff check tests/integration/
python3 -m ruff format --check tests/integration/
python3 -m mypy tests/integration/ --strict
python3 -m pytest tests/integration/ -v
```

Expected output: `24 passed`, no warnings.

The suite is hermetic. No network, no Postgres, no Ollama, no real
brokers. It runs in ~1 second on a developer laptop.

---

## What's covered

### `test_e2e_paper_smoke.py` (7 tests)

End-to-end backtest smoke. Drives a 100-candle 1m synthetic series through
the full strategy → risk → execution → ledger pipeline.

| Scenario | Asserts |
|---|---|
| 100 bars → 10 proposals → 10 fills | Counts match exactly; risk approves all 10; ledger records 10 fills + 0 rejections |
| `client_order_id` round-trip | The id minted by the strategy survives the adapter, the execution engine, and the ledger — bit-for-bit, all 10 ids unique |
| Equity curve shape | Curve has `N_FILLS + 1` rows; timestamps non-decreasing; final equity is a finite `Decimal` |
| Empty feed | 9 bars yields zero proposals (no fill before the 10th candle) |
| Side propagation | Strategy-side `BUY` lands as `BUY` in the ledger |
| Risk rejection short-circuits | Tiny `max_notional` blocks all 10 proposals; exchange is never called; ledger is empty |
| Exchange order id | Every recorded fill carries a non-empty `exchange_order_id` |
| Idempotent replay | Re-submitting the same 10 client_order_ids does NOT produce 20 fills — the idempotency store short-circuits duplicates |

### `test_live_smoke.py` (4 tests)

`LiveEngine` end-to-end with a fake exchange yielding a fixed
`StreamEvent` sequence.

| Scenario | Asserts |
|---|---|
| Candles + fills dispatch | `on_candle` fires for each closed bar; `on_fill` fires once per stream fill; lifecycle (`open`/`close`) ran; events_processed metric is exact; ledger collects the right number of fills |
| Empty stream | Engine completes its lifecycle cleanly with zero events; no strategy hooks fire; ledger empty |
| `PositionState` math | BUY 0.5 + SELL 0.2 → 0.3 net long in the in-memory book |
| Unsubscribed symbol | A tick for a symbol the engine doesn't subscribe to is logged + dropped, not raised; strategy never sees it |

### `test_types_compat.py` (13 tests)

Cross-module type compatibility — the load-bearing wiring tests.

| Scenario | Asserts |
|---|---|
| Construct `Bar` / `Tick` / `Fill` / `Position` / `OrderProposal` | All five core dataclasses can be built with the values the live module produces |
| `OrderProposal` round-trip preserves `client_order_id` | The adapter from `util.types.OrderProposal` to `execution.engine.OrderProposal` is bit-for-bit on the id |
| Round-trip preserves `symbol` / `side` / `qty` | BUY/SELL + symbol routing survives the adapter |
| `signal_px` fallback from `limit_price` | When the strategy doesn't set a metadata signal_px, the adapter derives one so the slippage gate has a reference |
| Metadata pass-through drops internals | `client_order_id` and `signal_px` are stripped from the execution-side metadata; user keys flow through |
| Strategy ABC defaults | `on_tick` / `on_fill` default to `[]` (not `None`); `on_start` / `on_stop` complete without raising |
| Execution `OrderProposal` validation | Pydantic enforces `min_length=8` on `client_order_id` |
| Execution `OrderProposal` is frozen | Cannot mutate `qty` post-construction |
| Execution `Fill` is frozen | Cannot mutate `filled_qty` post-construction |

---

## What's NOT covered

Deliberately out of scope. These belong to other suites (or other waves):

- **Real Ollama calls.** The wave-2 agents module isn't built yet; we
  stand in for it with a `StubRiskEngine`. When the real
  `quanta_core.agents.debate` lands, the stub gets replaced and the
  smoke suite picks up the 30s deliberate-debate budget assertion.
- **Real Alpaca / Coinbase SDK calls.** Out of scope per the build spec
  ("NO real network calls"). The exchanges agent owns the
  `vcrpy`-backed adapter tests.
- **Postgres ledger.** We use an in-memory SQLite engine for the
  idempotency store (the execution engine's only required Postgres
  contract is `IntegrityError` on a unique constraint, which SQLite
  honours identically). The Postgres ledger writer lands in wave-2
  agent #E; this suite will gain a parallel `tests/integration/test_ledger_pg.py`
  marked `@pytest.mark.integration` once it does.
- **Strategy parity (backtest vs live).** The DESIGN-LOCK calls for a
  parity oracle that runs the same Strategy class through both engines
  and asserts equal Fill streams; that's a separate test file
  (`test_parity_oracle.py`) once the wave-2 backtest module lands.
- **Reconciler drift detection.** Covered by `tests/live/test_reconciler.py`
  with the existing in-process fake; not duplicated here.
- **Real cron / Hermes Layer 8.** Out of scope; wave-2 agent #C owns
  the cron-as-learning test surface.
- **Performance / latency.** Out of scope for shape-correctness.
  Risk Monte Carlo p95 + execution submit p95 belong in
  `tests/perf/` (not yet authored).

---

## Coverage matrix — V4 modules × integration tests

| V4 module | `test_e2e_paper_smoke` | `test_live_smoke` | `test_types_compat` |
|---|:---:|:---:|:---:|
| `quanta_core.util.types` | ✓ | ✓ | ✓ |
| `quanta_core.strategy.base` | ✓ | ✓ | ✓ |
| `quanta_core.live.engine` |   | ✓ |   |
| `quanta_core.live.dispatcher` |   | ✓ |   |
| `quanta_core.live.tick_aggregator` |   | ✓ |   |
| `quanta_core.live.reconciler` (PositionState) |   | ✓ |   |
| `quanta_core.exchanges.base` |   | ✓ |   |
| `quanta_core.execution.engine` | ✓ |   | ✓ |
| `quanta_core.execution.idempotency` | ✓ |   |   |
| `quanta_core.execution.slippage_gate` | ✓ |   |   |
| `quanta_core.execution.order_state_machine` | ✓ |   |   |
| `quanta_core.observability.notifier` |   | ✓ |   |
| `quanta_core.risk` (stub — module not yet reconciled) | ✓ |   |   |
| `quanta_core.ledger` (in-memory — wave-2 #E not yet built) | ✓ | ✓ |   |
| `quanta_core.backtest` (fake — wave-2 #B not yet built) | ✓ |   |   |
| `quanta_core.agents` (stub — wave-2 #D not yet built) | n/a | n/a | n/a |
| `quanta_core.hermes` (out of test scope — Layer 8) | n/a | n/a | n/a |

---

## Fakes + stubs (where to look)

All in `tests/integration/conftest.py`:

| Fake | Stands in for | Notes |
|---|---|---|
| `InMemoryLedger` | `quanta_core.ledger.writer.Writer` | Records fills + rejections; tracks a naive equity curve |
| `PaperExecExchange` | Alpaca / Coinbase adapter | Fills every order at the requested limit; bumps mid 0.001% per fill |
| `StubRiskEngine` | `quanta_core.risk.RiskEngine` | Approves below `max_notional`; rejects otherwise |
| `RiskGatedExecutionSink` | The production wiring shim | Adapts `util.types.OrderProposal` → `execution.engine.OrderProposal`; this is the load-bearing translator under test |
| `FakeBacktestEngine` | `quanta_core.backtest.engine.BacktestEngine` | Replays a `list[Bar]` through `Strategy.on_candle` and the same sink the live engine uses |
| `FakeLiveExchange` | Live WebSocket connector | Yields a fixed sequence of `StreamEvent` instances |

When the real modules land, the swap is mechanical — the test scenarios
don't change because they were written against the documented
contracts, not the implementations.

---

## Known wiring hazards surfaced by these tests

1. **Two `OrderProposal` models.** `util.types.OrderProposal` (dataclass,
   used by live + strategies) is distinct from
   `execution.engine.OrderProposal` (Pydantic, used by execution). The
   foundation agent needs to unify these or the
   `RiskGatedExecutionSink._adapt` shim becomes permanent. The
   round-trip tests assert the shim preserves the load-bearing
   fields; they will catch any future drift.
2. **`signal_px` is mandatory at the execution boundary.** The strategy
   has no native field for it (we squat in `metadata`). The cleanest
   fix is a follow-up PR that adds `signal_px` to the live-side
   proposal; until then the shim must fall back to `limit_price` and
   tests assert it does.
3. **`client_order_id` lifecycle.** The strategy mints it; the adapter
   forwards it; the idempotency store reserves it; the execution
   engine commits it; the ledger records it. Five layers. The smoke
   suite asserts the id flows through all five intact and that a
   replay does not double-fill.

---

## Future work — when wave-2 lands

| When wave-2 module lands | Replace fake | Add tests |
|---|---|---|
| #B backtest | `FakeBacktestEngine` → real `BacktestEngine` | Parity oracle: same Strategy class, backtest vs live, equal Fill streams |
| #E ledger + observability | `InMemoryLedger` → real `LedgerWriter` (Postgres) | `test_ledger_pg.py` marked `@pytest.mark.integration` |
| #C hermes | (no fake — out of test scope) | Hermes cron-as-learning state-file round-trip |
| #D agents | `StubRiskEngine` → real debate-driven risk gate | 30s deliberate-debate budget; unanimous-of-4 gate |

The branch `feat/v4-wave2-integration` is ready to merge once #B and #E
are reconciled into `feat/v4-build`. The smoke suite is forward-compatible:
swapping the fakes does not change any of the assertions.
