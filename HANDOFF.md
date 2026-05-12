# Wave-2 Backtest — Handoff

**Branch:** `feat/v4-wave2-backtest`
**Layout:** ROOT (`src/quanta_core/`, `pyproject.toml` at repo root, `tests/backtest/`)
**Sprint:** WAVE-2 backtest agent · 2026-05-12
**Status:** Source + tests landed; quality gates all green. No push to remote.

---

## What shipped

Backtest module — the **parity oracle** for Quanta Core V4. Backtest replays
historical candles through the SAME Strategy ABC the live engine uses; the
invariant that proposals match bar-for-bar is the central test of the design.

### Source files

| Path | Purpose | LOC |
|---|---|---|
| `src/quanta_core/types.py` | Foundation Pydantic v2 contracts: `Bar`, `Tick`, `Fill`, `Position`, `OrderProposal`, `Context` protocol. | 266 |
| `src/quanta_core/strategy/base.py` | Strategy ABC stub matching `feat/v4-build-foundation` (sync, ctx-injected, on_candle mandatory). | 100 |
| `src/quanta_core/strategy/__init__.py` | Re-export `Strategy`. | 9 |
| `src/quanta_core/backtest/__init__.py` | Public surface — engine, candle sources, result models, walk-forward. | 58 |
| `src/quanta_core/backtest/engine.py` | `BacktestEngine(strategy_class, config, candle_source)` · `run(start, end)` · `step_once(bar)` · pluggable slippage · paper next-bar-open fills · Decimal ledger. | 830 |
| `src/quanta_core/backtest/walk_forward.py` | `WalkForwardRunner` rolling train/test sliding window · per-fold `train_hook` · aggregated `WalkForwardReport`. | 406 |
| `src/quanta_core/backtest/result.py` | Pydantic v2 `BacktestResult` · JSONL round-trip · `summary_table()`. | 244 |
| `src/quanta_core/backtest/candle_source.py` | `CandleSource` ABC · `FeatherCandleSource` (read-only `user_data/data/...`) · `SyntheticCandleSource` (seeded determinism) · `InMemoryCandleSource`. | 353 |

**Source total:** ~2 257 LOC (1 991 in `backtest/` proper, plus 266 in the
foundation stub which collapses on merge with `feat/v4-build-foundation`).

### Test files

| Path | Purpose | LOC |
|---|---|---|
| `tests/backtest/test_live_backtest_parity.py` | **THE parity oracle** — 8 scenarios, independent mock-live engine. Includes a regression-style test that proves the parity assertion catches non-determinism. | 643 |
| `tests/backtest/test_engine.py` | 60+ engine tests: validation, slippage, lifecycle hooks, context-submission, limit fills (BUY/SELL), pyramiding (long/short), partial close, history view, reset, empty run, slippage integration. | 1 102 |
| `tests/backtest/test_walk_forward.py` | Constructor validation, fold-boundary math (step/zero/custom step), aggregated report, empty-fold-in-gap edge case, train_hook delivery, summary table renderer. | 406 |
| `tests/backtest/test_candle_source.py` | Synthetic determinism, slice semantics, feather + parquet I/O, missing-root/file/column errors, duplicate-timestamp dedup, in-memory invariants, `_coerce_utc` numpy.datetime64 + tz-aware coverage. | 372 |
| `tests/backtest/test_result.py` | Pydantic validators, JSONL round-trip + append + blank-line tolerance, parent-dir auto-create, summary table renderer, `_json_default` Decimal/datetime/unknown paths. | 253 |
| `tests/backtest/test_strategy_base.py` | ABC instantiation guard, default-hook behaviour, Context runtime_checkable, repr, ctx/config storage. | 117 |
| `tests/backtest/conftest.py` | Shared fixtures (symbols, fixed_start, synthetic_source, simple/flat strategy classes, `make_bar`). | 174 |

**Test total:** ~3 068 LOC.

**Grand total:** ~5 325 LOC (source + tests + pyproject).

---

## Quality gates — all green

| Gate | Result | Command |
|---|---|---|
| pytest | **117 passed in 0.80s** | `PYTHONPATH=src pytest tests/backtest/` |
| Coverage (line) | **100%** (all statements hit) | `PYTHONPATH=src pytest tests/backtest/ --cov=quanta_core.backtest --cov=quanta_core.strategy` |
| Coverage (branch) | **99.7%** (3 partial branches: defensive None-fallbacks and one assert-narrow path) | same |
| ruff check | **CLEAN** | `ruff check src/quanta_core tests/backtest` |
| ruff format | **CLEAN** | `ruff format --check src/quanta_core tests/backtest` |
| mypy --strict (src) | **CLEAN** (10 files checked) | `PYTHONPATH=src mypy src/quanta_core` |

### Per-module coverage breakdown

```
Name                                        Stmts   Miss Branch BrPart  Cover
---------------------------------------------------------------------------------------
src/quanta_core/backtest/__init__.py            5      0      0      0 100.0%
src/quanta_core/backtest/candle_source.py     141      0     46      0 100.0%
src/quanta_core/backtest/engine.py            354      0     98      3  99.3%
src/quanta_core/backtest/result.py            113      0     16      0 100.0%
src/quanta_core/backtest/walk_forward.py      134      0     22      0 100.0%
src/quanta_core/strategy/__init__.py            2      0      0      0 100.0%
src/quanta_core/strategy/base.py               18      0      0      0 100.0%
---------------------------------------------------------------------------------------
TOTAL                                         767      0    182      3  99.7%
```

All seven files are at 100% line coverage. The three uncovered branch arcs in
`engine.py` are defensive fall-throughs (None-fallback assertions narrowed by
mypy) that have no observable behaviour difference. Well above the 90%
backtest-correctness floor the task statement requires.

---

## Parity test — the load-bearing invariant

`tests/backtest/test_live_backtest_parity.py` — 8 scenarios, all **PASSING**:

```
tests/backtest/test_live_backtest_parity.py::test_parity_single_trade PASSED
tests/backtest/test_live_backtest_parity.py::test_parity_pyramid PASSED
tests/backtest/test_live_backtest_parity.py::test_parity_history_aware PASSED
tests/backtest/test_live_backtest_parity.py::test_parity_unreachable_limit PASSED
tests/backtest/test_live_backtest_parity.py::test_parity_strategy_independence PASSED
tests/backtest/test_live_backtest_parity.py::test_parity_clock_alignment PASSED
tests/backtest/test_live_backtest_parity.py::test_parity_history_view_alignment PASSED
tests/backtest/test_live_backtest_parity.py::test_parity_catches_nondeterminism PASSED

8 passed in 0.06s
```

The test file ships its own **mock live engine** (`run_mock_live`) so the
parity property cannot be smuggled in via shared code paths. The mock and
the real `BacktestEngine` share only the Strategy ABC + the type contracts +
the Context protocol. Once `feat/v4-build-live` lands, swap `run_mock_live`
for the real `LiveEngine` and the test becomes the true end-to-end parity
oracle.

The `test_parity_catches_nondeterminism` case is the test-of-the-test: it
runs a strategy with a class-level shared counter, proves the proposal
streams diverge between live and backtest, and confirms
`_assert_proposals_identical` raises `AssertionError` with "PARITY BROKEN".
If anyone ever introduces non-determinism into a strategy, this test class
of bug fires at CI time.

---

## Commit SHAs

| Commit | Purpose |
|---|---|
| `cc5bba466d8ed243354e1803428e7652296a014c` | `feat(v4-backtest): parity-oracle engine + walk-forward + candle sources` — the whole module + tests + pyproject. |

(This handoff itself will land in a follow-up commit.)

---

## How to verify locally

```bash
git checkout feat/v4-wave2-backtest
cd $(git rev-parse --show-toplevel)
PYTHONPATH=src pytest tests/backtest/ -q
PYTHONPATH=src pytest tests/backtest/ --cov=quanta_core.backtest --cov=quanta_core.strategy --cov-report=term-missing
PYTHONPATH=src mypy src/quanta_core
ruff check src/quanta_core tests/backtest
ruff format --check src/quanta_core tests/backtest
```

---

## What's deliberately NOT included

- **No async.** The Strategy ABC is locked synchronous per `DESIGN-LOCK.md`
  §5; the engine matches it. Async hooks can be added in a future revision
  once the live engine settles.
- **No GPU.** TFT inference, debate, and LoRA all run via `ctx.predict` in
  the real wired-up engine — backtest stays CPU-only.
- **No `user_data/data/coinbase/` rewrites.** The task statement is explicit
  that legacy data is read-only; `FeatherCandleSource` only reads.
- **No live engine.** That's `feat/v4-build-live` and `feat/v4-wave2-live`.
  The parity test stubs it with a deliberately-different mock so the two
  implementations cannot accidentally agree by sharing a bug.
- **No port of `FreqAIMeanRevV1`.** That's the wave-3 strategy port; this
  agent only needs the ABC.

---

## Morning merge notes (operator)

The wave-2 morning merge sequence in `docs/quanta-core-v4-rev2/WAVE-2-PLAN.md`
puts foundation → ledger → hermes → agents → backtest. Two minor merge
considerations:

1. **`src/quanta_core/types.py` is duplicated** with the foundation branch's
   `quanta_core/src/quanta_core/types.py` (nested layout). The schemas are
   byte-identical by construction (this file mirrors the foundation file
   line-for-line). Pick one path; the other deletes cleanly with no diff.
2. **`src/quanta_core/strategy/base.py` is duplicated** with the foundation
   branch the same way; same line-for-line guarantee. Pick one path.
3. **`pyproject.toml` at root** vs the foundation's
   `quanta_core/pyproject.toml`: keep ONE at root (this agent's location is
   the wave-2 ROOT layout per the task statement and matches the
   reconciliation work on `feat/v4-build-reconciled`).

If the foundation branch lands first and writes `src/quanta_core/types.py`
and `src/quanta_core/strategy/base.py`, this agent's writes for those two
files are pure no-ops at the diff level; the merge resolves clean by
accepting either side.

---

— wave-2 backtest agent · 2026-05-12
