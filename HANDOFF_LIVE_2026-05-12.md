# Live module build agent — handoff, 2026-05-12

**Branch:** `feat/v4-build-live` (off `main`, NOT pushed)
**Scope:** `src/quanta_core/live/` plus the minimal sibling scaffolding
needed to compile + test the module in isolation.
**Status:** all gates green; ready for review.

---

## 1. Files added

### Source — `src/quanta_core/`

| Path | LOC | Role |
|------|----:|------|
| `__init__.py` | 10 | Package version |
| `util/__init__.py` | 9 | Subpackage marker |
| `util/types.py` | 148 | Bar / Tick / Fill / Position / OrderProposal dataclasses + Side/Venue/Timeframe aliases |
| `util/errors.py` | 31 | QuantaError hierarchy (StaleFeedError, LateTickError, ReconciliationDriftError) |
| `exchanges/__init__.py` | 16 | Re-exports the ABC surface |
| `exchanges/base.py` | 89 | **Exchange ABC + ExchangeStream ABC + StreamEvent dataclass** — the interface the live module depends on; concrete impls are owned by the exchanges agent |
| `strategy/__init__.py` | 11 | Re-exports `Strategy` |
| `strategy/base.py` | 70 | Strategy ABC with `on_candle` (mandatory) + `on_tick` / `on_fill` / `on_start` / `on_stop` (default no-op) |
| `observability/__init__.py` | 7 | Re-exports Notifier surface |
| `observability/notifier.py` | 48 | `Notifier` Protocol + `NullNotifier` |
| `observability/ledger_anomaly.py` | 56 | `record_anomaly` — append-only JSONL anomaly writer |
| `live/__init__.py` | 30 | Public surface of the live module |
| `live/tick_aggregator.py` | 226 | `TickAggregator` — per-symbol, multi-timeframe; UTC-epoch-aligned boundaries; VWAP + trade-count; late-tick counter |
| `live/dispatcher.py` | 219 | `StrategyDispatcher` + `OrderSink` Protocol + `DispatcherMetrics`; 30-second per-hook budget via `anyio.fail_after`; exceptions and sink failures isolated |
| `live/reconciler.py` | 243 | `PositionState` + `Reconciler` — REST sweep on 60s cadence; drift → Slack `:warning:` + JSONL anomaly row; never auto-corrects |
| `live/engine.py` | 303 | `LiveEngine` + `EngineConfig` + `EngineMetrics`; structured `anyio.create_task_group` of (consumer, reconciler, heartbeat); SIGINT/SIGTERM → request_stop; **no auto-close on shutdown** |

**Source total:** 1,516 LOC across 16 files.

### Tests — `tests/live/`

| Path | LOC | Tests |
|------|----:|------:|
| `conftest.py` | 11 | path setup |
| `test_tick_aggregator.py` | 192 | 11 |
| `test_dispatcher.py` | 258 | 9 |
| `test_reconciler.py` | 226 | 7 |
| `test_engine.py` | 392 | 7 |
| `test_misc.py` | 103 | 3 |
| `pytest.ini` | 4 | – |

**Test total:** 1,182 LOC, **37 tests**.

### Config

- `pyproject.toml` (root) — hatchling build backend + ruff (py312 / line=100 / strict ruleset incl. ASYNC + B + UP + I) + mypy --strict scoped to `src/quanta_core` + tests/live opt-out from strict typing. Scoped via `extend-exclude` so the legacy tree is untouched.

---

## 2. Gates run

```
$ ruff check src/quanta_core tests/live
All checks passed!

$ ruff format --check src/quanta_core tests/live
23 files already formatted

$ mypy --strict src/quanta_core
Success: no issues found in 16 source files

$ mypy --strict tests/live
Success: no issues found in 7 source files

$ pytest tests/live -p anyio
37 passed in 1.34s
```

### Coverage

```
src/quanta_core/exchanges/base.py                    23      0   100%
src/quanta_core/live/__init__.py                      6      0   100%
src/quanta_core/live/dispatcher.py                   83      4    95%
src/quanta_core/live/engine.py                      128     15    88%
src/quanta_core/live/reconciler.py                  100      5    95%
src/quanta_core/live/tick_aggregator.py              86      0   100%
src/quanta_core/observability/ledger_anomaly.py      13      0   100%
src/quanta_core/observability/notifier.py            12      0   100%
src/quanta_core/strategy/base.py                     19      0   100%
src/quanta_core/util/errors.py                        6      0   100%
src/quanta_core/util/types.py                        64      0   100%
TOTAL                                               554     24    96%
```

96% total, every module ≥88% — past the 85% bar in the brief.

### Legacy regression check

Pre-existing failures on `main` (4 failures in unrelated legacy test files
covering TFT serialization and weekly training endpoints) reproduce
identically on this branch. **No new test failures introduced.**

---

## 3. Interface assumptions (load-bearing)

These are the contracts I depend on. The exchanges + execution sibling
agents must satisfy them, OR negotiate a change here.

### `quanta_core.exchanges.base.Exchange` (sibling agent owns concrete impls)

```python
class Exchange(ABC):
    name: Venue                               # "alpaca" | "coinbase" | "paper"

    async def open(self) -> ExchangeStream    # returns an async-iterable
    async def list_positions(self) -> list[Position]   # REST snapshot, every 60s
    async def close(self) -> None
```

### `quanta_core.exchanges.base.ExchangeStream`

```python
class ExchangeStream(ABC):
    def __aiter__(self) -> AsyncIterator[StreamEvent]
    async def aclose(self) -> None
```

Each `StreamEvent` is mutually exclusive: exactly one of `tick` / `fill`
is populated. (Future channels — quotes, news — will be additive payload
kinds; the engine will need a minor switch.)

### `quanta_core.live.dispatcher.OrderSink` (execution agent owns)

```python
class OrderSink(Protocol):
    async def submit(self, proposal: OrderProposal) -> None
```

The dispatcher catches every exception from `submit`. Execution may raise
freely; the loop won't crash. Idempotency, risk gating, and venue dispatch
all live below the sink (per design lock §2).

### `quanta_core.observability.notifier.Notifier`

```python
class Notifier(Protocol):
    async def warning(self, subject: str, body: str) -> None
    async def info(self, subject: str, body: str) -> None
```

Implementations must be non-blocking on a slow channel — drop, don't
back-pressure. Hermes Layer 8 supplies the production impl.

### `Strategy` ABC

- `name`, `symbols`, `timeframes` are class attributes; the dispatcher uses
  them to gate dispatch.
- `wants_ticks: bool` — opt-in to `on_tick` for perf.
- Hook return type is `list[OrderProposal]`. Defaults return `[]`.

---

## 4. Design decisions worth flagging

1. **No threading.** Engine uses `anyio.create_task_group` only. SIGINT/SIGTERM
   handled via `anyio.open_signal_receiver`.
2. **30-second per-hook budget** is enforced via `anyio.fail_after` per
   dispatch call (mirrors the deliberate-debate budget locked in
   `DESIGN-LOCK.md §1`). A blown budget drops the result + bumps a counter;
   no orders are placed for that bar.
3. **Stale-feed alerts are notify-only.** The watchdog fires Slack but does
   NOT pause trading. Intermittent quiet outside RTH is normal.
4. **No auto-close on shutdown.** Per design lock: "stop placing new
   orders" — that's exactly what `request_stop` does (cancels the consumer +
   heartbeat + reconciler; the position state is left untouched). The
   `test_engine_does_not_close_positions_on_shutdown` test enforces this
   invariant.
5. **Reconciler never auto-corrects.** Drift → alert + anomaly row. Operator
   investigates manually. Auto-healing is a footgun; we mirror the
   risk-governor pattern.
6. **VWAP epsilon.** Reconciler treats `|venue_qty - local_qty| <= 1e-8` as
   no drift. Override per-instance.
7. **UTC-epoch boundary alignment** for the aggregator (a 5m bar covers
   `[unix_ts - unix_ts % 300, unix_ts - unix_ts % 300 + 300)`). Matches
   what most venues + Polygon emit so backtest parity holds without
   special-casing.
8. **Late ticks are dropped, not back-applied.** `late_tick_count` is the
   visible signal. We don't extrapolate or open earlier bars.
9. **No-volume bars are not silently zeroed.** If volume==0 the bar's
   `vwap` field is set to the last observed price (which equals close).

---

## 5. What's NOT in scope here

Sibling-agent owned — touched only at the interface:

- The concrete `AlpacaConn` / `CoinbaseConn` / `PaperVenue` adapters (exchanges agent).
- The execution `OrderSink` (execution agent owns `client_order_id` derivation, idempotency, retries, slippage gating).
- The risk governor (risk agent owns gates).
- The ModelRegistry + LoRA (models agent).
- The ledger Postgres writer (ledger agent).

I touched none of the existing `user_data/` or `stocks/` trees. The new
`pyproject.toml` is scoped via `extend-exclude` so legacy code is not
type-checked or linted by these gates.

---

## 6. Commit shas

(filled in by the commit step — see `git log feat/v4-build-live`)

---

## 7. Next agent's pickup list

1. **Exchanges agent** — fulfil `Exchange` ABC; the engine will import your
   concrete adapter and pass it to `LiveEngine(...)`. Your stream MUST yield
   `StreamEvent(tick=...)` or `StreamEvent(fill=...)` (mutually exclusive).
2. **Execution agent** — provide an `OrderSink` impl. The dispatcher will
   call `await sink.submit(proposal)`; you own everything past that line.
3. **Risk agent** — risk gates sit *inside* the sink (recommended), or
   between dispatcher and sink (alternative). Decide and document.
4. **Strategy agent** — port `MeanRevTFT` etc. as subclasses of
   `quanta_core.strategy.base.Strategy`; the engine will dispatch automatically
   once registered via `engine.register([strategy])`.
5. **Integration test (parity oracle)** — once at least one concrete
   exchange + execution sink + strategy are present, wire up
   `tests/integration/test_backtest_matches_live.py` per the architecture
   doc (06-ARCHITECTURE §3.5).

---

## 8. Open questions / TODOs

- The `EngineConfig.anomaly_path` defaults to `~/.quanta/logs/anomalies.jsonl`.
  Hermes nightly reflector needs to start reading this path (per
  `10-CODE_PATTERNS.md §2.2`); coordinate when that flips on.
- `EngineMetrics` is a plain dataclass — wire it into the prometheus
  registry when the observability agent stands one up.
- The reconciler currently only diffs **positions**. The architecture doc
  also calls for open-order reconciliation; that becomes a sibling method
  once the execution agent declares the in-memory open-order map.
- We import nothing from the legacy tree.
