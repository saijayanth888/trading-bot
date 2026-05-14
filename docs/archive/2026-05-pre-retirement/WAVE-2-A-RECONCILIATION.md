# WAVE-2-A — V4 Build Wave 1 Reconciliation

**Branch:** `feat/v4-build-reconciled` (off `feat/v4-build`)
**Date:** 2026-05-12 (UTC late afternoon)
**Status:** All gates green. ZERO push to remote. Ready for review + merge to `feat/v4-build`.

## TL;DR

| Metric | Value |
|---|---|
| Branches merged | 6 of 6 (execution · live · exchanges · risk · foundation · models) |
| Layout | Canonical root `src/quanta_core/` per doc #10 §1.2 |
| Tests | **564 passed · 2 skipped** (CuPy-GPU, expected) |
| Coverage | **95%** (target ≥85%) |
| ruff check | clean |
| ruff format --check | clean |
| mypy --strict src/ | clean |
| LOC merged | ~3,300 source · ~6,000 test · 81 V4 Python files |

## Branches merged (in order)

| # | Branch | Tip | Layout in | Result |
|---|---|---|---|---|
| 1 | `feat/v4-build-execution` | `d1620e1` | root | Clean merge → 134 tests |
| 2 | `feat/v4-build-live` | `86e1b4e` | root | Conflict on `pyproject.toml` + `__init__.py` (resolved: union deps, 0.4.0.dev0 version) |
| 3 | `feat/v4-build-exchanges` | `837a2f4` | root | Conflict on `exchanges/base.py` + `exchanges/__init__.py` (resolved: see SEMANTIC DECISIONS) |
| 4 | `feat/v4-build-risk` | `3926cbb` | **nested** | Relocated `quanta_core/src/quanta_core/risk/` → `src/quanta_core/risk/`; tests → `tests/risk/` |
| 5 | `feat/v4-build-foundation` | `cb87f3a` | **nested** | Relocated; conflict on `strategy/base.py` (resolved: see SEMANTIC DECISIONS) |
| 6 | `feat/v4-build-models` | `44522f4` | **nested** | Relocated `quanta_core/src/quanta_core/models/` → `src/quanta_core/models/`; tests → `tests/models/` |

**Note on the wave-1 status report:** the conflict-report claimed risk was at root level. In fact risk landed nested at `quanta_core/src/quanta_core/risk/` (verified via `git ls-tree`); we relocated it like foundation and models.

## Conflicts encountered + resolution

### Mechanical conflicts

- **`pyproject.toml`** (3 add/add conflicts across live, exchanges, foundation): each agent wrote their own. Resolved by taking the **union of dependencies** + **union of dev-deps** + harmonising version/description/license/authors:
  - Version: `0.4.0.dev0`
  - License: `MIT`
  - Final dep list: anyio, pydantic, pydantic-settings, sqlalchemy, psycopg, structlog, httpx, safetensors, alpaca-py, coinbase-advanced-py, numpy, pandas, pyyaml, torch
  - ruff/mypy config: superset of every agent's per-tool ignores (RUF001/002/003/012, B008/B027, PERF401, PT006, SIM105/108, UP046) so each agent's clean state stays clean
- **`src/quanta_core/__init__.py`** (live, exchanges, foundation): bumped to `0.4.0.dev0`; merged docstrings.
- **`tests/conftest.py`** (root): identical across branches — no action.
- **`HANDOFF.md`** (root): execution wrote one; renamed to `HANDOFF_EXECUTION_2026-05-12.md`. Foundation, risk, models had their own; renamed to `HANDOFF_{module}_2026-05-12.md`. Live wrote `HANDOFF_LIVE_2026-05-12.md` (no rename needed).
- **Foundation placeholder `__init__.py` files**: `agents/`, `backtest/`, `hermes/`, `ledger/`, `lora/` came across as empty directories (cp swallowed empty content); recreated with `__all__: list[str] = []` to satisfy `test_package_layout::test_placeholder_package_imports`.
- **`test_package_layout.py`**: foundation's smoke test expected ALL submodules to have `__all__ == []`. Post-reconciliation, exchanges/execution/live/models/risk/strategy/util are FILLED. Split the test into two parametrised lists (`PLACEHOLDER_PACKAGES` vs `FILLED_PACKAGES`) and broadened the version assertion to `("0.1.", "0.4.")`.

### Semantic decisions

These required choosing between equally-valid agent implementations. Each is documented inline in the relevant source file:

#### 1. `src/quanta_core/exchanges/base.py` — Exchange ABC contract

**Conflict**: Live built a narrow `Exchange` ABC (`open` / `list_positions` / `close` + `name`, with a `StreamEvent` + `ExchangeStream` async-iterator facade). Exchanges built a full broker-adapter `Exchange` ABC (10 abstract methods: `connect` / `disconnect` / `get_account` / `get_positions` / `submit_order` / `cancel_order` / `get_orders` / `stream_ticks` / `stream_fills` / `stream_orderbook`, plus full value-type dataclasses). The two are semantically incompatible — concrete Alpaca/Coinbase adapters cannot satisfy both shapes simultaneously.

**Resolution** (aligns with DESIGN-LOCK §2: "Strategy never imports `exchanges/` or `ledger/` directly. `Context` mediates"):

- Kept the **canonical full-API Exchange contract** from the exchanges branch as `BrokerExchange` (concrete Alpaca/Coinbase subclass this).
- Kept the **narrow live-engine view** from the live branch as `Exchange` (open / list_positions / close). `_FakeExchange` test fixtures and the live engine's reconciler use it.
- `BrokerExchange` inherits from `Exchange`, providing default implementations of `open()` (raises NotImplementedError — adapters override to wrap their `stream_ticks` + `stream_fills` into a `StreamEvent` iterator), `list_positions()` (delegates to `get_positions`), `close()` (alias for `disconnect`).
- This composability path means concrete adapters MAY back the live engine later by overriding `open()` only; today they only need to back the execution engine via the full `BrokerExchange` API.
- Updated `AlpacaExchange` / `CoinbaseExchange` to subclass `BrokerExchange`.

#### 2. `src/quanta_core/strategy/base.py` — Strategy ABC sync vs async

**Conflict**: Foundation defined a **synchronous** Strategy ABC (per DESIGN-LOCK §5: "Strategy ABC is sync (not async per doc #6) — operator-locked from DESIGN-LOCK §5; preserves backtest determinism"). Live defined an **async** Strategy ABC where every hook is `async def`; the live dispatcher does `await method(event, ctx)` with per-hook anyio budgets.

**Resolution** (aligns with DESIGN-LOCK §5 + the prompt's explicit "foundation's content takes precedence"):

- **Canonical (sync) `Strategy`** lives at `src/quanta_core/strategy/base.py` — foundation's version verbatim. Backtest engine + concrete strategies port to this.
- **Live-engine async variant** lives at `src/quanta_core/strategy/async_strategy.py` as `AsyncStrategy`. Live dispatcher / engine / tests rewrote their imports as `from quanta_core.strategy.async_strategy import AsyncStrategy as Strategy` (alias keeps existing code unchanged within the live module).
- Both are re-exported from `quanta_core.strategy.__init__` so external callers can pick the right one.
- Open question for V4.1: unify under a single ABC once the executor design is final (the prompt agreed to defer).

#### 3. `src/quanta_core/types.py` (foundation, Pydantic models) vs `src/quanta_core/util/types.py` (live, dataclasses)

**Conflict**: Foundation built top-level `types.py` with Pydantic v2 models (Bar/Tick/Fill/Position/OrderProposal + Context Protocol). Live built `util/types.py` with frozen dataclasses (overlapping names + Bar/Timeframe + ClientOrderId/VenueOrderId NewTypes).

**Resolution**: Kept both. Foundation's `types.py` is canonical (per doc #06 §3); `util/types.py` is the live-engine internal vocabulary (used only inside `quanta_core.live.*` and its tests). They share the `Symbol`/`Venue`/`Side` aliases by value. A future refactor will collapse them once the AsyncStrategy/Strategy split is resolved.

## Test outcomes

| Test tree | Pass | Skip | Source coverage |
|---|---|---|---|
| `tests/execution/` | 134 | 0 | engine 99% · idempotency 100% · order_state 100% · slippage 100% |
| `tests/exchanges/` | 51 | 0 | alpaca 91% · coinbase 85% · base 95% · idempotency 95% |
| `tests/foundation/` | 105 | 0 | config 100% · logging_setup 100% · strategy/base 100% · types 100% |
| `tests/live/` | 37 | 0 | dispatcher 92% · engine 86% · reconciler 94% · tick_aggregator 100% |
| `tests/models/` | 78 | 0 | tft 97% · tft_architecture 97% · ollama 91% · registry 97% |
| `tests/risk/` | 159 | 2 | governor 97% · monte_carlo 99% · ownership 100% · asset_class_gate 100% |
| **TOTAL** | **564** | **2** | **95% overall** |

The 2 skips are both `tests/risk/test_monte_carlo.py` GPU-only tests (`CuPy not installed`) — expected on this host, will run on the GB10.

## Tests modified during reconciliation

1. `tests/foundation/test_package_layout.py` — split into `PLACEHOLDER_PACKAGES` vs `FILLED_PACKAGES`; broadened version stamp to accept `0.4.`. **No skips.**
2. Live module: `tests/live/test_engine.py`, `tests/live/test_dispatcher.py`, `tests/live/test_misc.py` had their Strategy import rewritten to `from quanta_core.strategy.async_strategy import AsyncStrategy as Strategy`. **No skips.**

No tests were skipped or quarantined to make the suite green.

## Commit SHAs (reconciliation chain, off `feat/v4-build` @ `d5d1fd7`)

```
4f7b76b merge(reconcile): execution at root layout
e2f7ee7 merge(reconcile): live at root layout (union pyproject deps + ruff/mypy/pytest)
77204c0 merge(reconcile): exchanges at root layout
020b15c merge(reconcile): risk relocated to root layout (src/quanta_core/risk + tests/risk)
36ff703 merge(reconcile): foundation relocated to root layout
56ede9b merge(reconcile): models relocated to root layout (src/quanta_core/models + tests/models)
c0de229 chore(reconcile): apply ruff --fix + ruff format; placeholder __init__.py; broaden mypy + ruff ignores
```

(The current commit adds this WAVE-2-A document.)

## Resulting tree (top-level V4 paths)

```
src/quanta_core/
├── __init__.py                 # version 0.4.0.dev0
├── py.typed
├── config.py                   # foundation
├── logging_setup.py            # foundation
├── types.py                    # foundation (Pydantic v2 models)
├── agents/__init__.py          # placeholder (foundation)
├── backtest/__init__.py        # placeholder (foundation)
├── hermes/__init__.py          # placeholder (foundation)
├── ledger/__init__.py          # placeholder (foundation)
├── lora/__init__.py            # placeholder (foundation)
├── exchanges/                  # exchanges branch (canonical) + live's facade additions
│   ├── __init__.py
│   ├── base.py                 # Exchange (narrow) + BrokerExchange (full) + ExchangeStream/StreamEvent
│   ├── alpaca.py
│   ├── coinbase.py
│   └── idempotency.py
├── execution/                  # execution branch
│   ├── __init__.py
│   ├── engine.py
│   ├── idempotency.py
│   ├── order_state_machine.py
│   └── slippage_gate.py
├── live/                       # live branch
│   ├── __init__.py
│   ├── engine.py
│   ├── dispatcher.py
│   ├── reconciler.py
│   └── tick_aggregator.py
├── models/                     # models branch
│   ├── __init__.py
│   ├── microstructure.py
│   ├── ollama_client.py
│   ├── registry.py
│   ├── sentiment.py
│   ├── tft.py
│   └── tft_architecture.py
├── observability/              # live branch
│   ├── __init__.py
│   ├── ledger_anomaly.py
│   └── notifier.py
├── risk/                       # risk branch (relocated)
│   ├── __init__.py
│   ├── asset_class_gate.py
│   ├── governor.py
│   ├── monte_carlo.py
│   └── ownership.py
├── strategy/
│   ├── __init__.py             # exports both Strategy + AsyncStrategy
│   ├── base.py                 # foundation: sync Strategy (canonical)
│   └── async_strategy.py       # live: AsyncStrategy (live-engine variant)
└── util/                       # live branch (internal vocabulary)
    ├── __init__.py
    ├── errors.py
    └── types.py                # dataclass Tick/Bar/Fill/Position/OrderProposal

tests/
├── conftest.py                 # legacy (unchanged)
├── execution/                  # 134 tests
├── exchanges/                  # 51 tests (incl. 7 vcrpy/JSON cassettes)
├── foundation/                 # 105 tests
├── live/                       # 37 tests
├── models/                     # 78 tests
└── risk/                       # 159 tests (2 GPU skipped)
```

## Verification commands (rerun from worktree root)

```bash
# All three gates clean:
ruff check src/quanta_core/ tests/exchanges/ tests/execution/ tests/foundation/ tests/live/ tests/models/ tests/risk/
ruff format --check src/quanta_core/ tests/exchanges/ tests/execution/ tests/foundation/ tests/live/ tests/models/ tests/risk/
mypy --strict src/quanta_core/

# 564 pass, 2 skip:
PYTHONPATH=src pytest src/quanta_core/ tests/exchanges/ tests/execution/ tests/foundation/ tests/live/ tests/models/ tests/risk/ -q

# 95% coverage:
PYTHONPATH=src pytest src/quanta_core/ tests/exchanges/ tests/execution/ tests/foundation/ tests/live/ tests/models/ tests/risk/ --cov=src/quanta_core
```

## Next steps for the operator review

1. `git log --oneline feat/v4-build..feat/v4-build-reconciled` — confirms 8 reconciliation commits (7 merges + 1 lint cleanup + this doc).
2. Review the two semantic decisions above (Exchange split + Strategy sync/async). Both are reversible if you disagree.
3. Merge `feat/v4-build-reconciled` into `feat/v4-build` (or fast-forward).
4. Wave-2 build can now resume — the remaining doc-06 modules (`backtest/engine.py`, `agents/debate.py`, `lora/online.py`, `ledger/{reader,writer}.py`, `ops/{routes,state_files}.py`, `hermes/*`, `cli.py`) build on top of this canonical layout.

— reconciliation agent (paused after WAVE-2-A handoff, no push)
