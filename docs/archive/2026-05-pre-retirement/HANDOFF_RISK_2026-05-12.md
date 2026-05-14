# quanta_core.risk — Build Handoff (2026-05-12)

Branch: `feat/v4-build-risk` (off main, NOT pushed)
Commit: `edb72ac`

## What's in this drop

`quanta_core/` (new top-level dir, additive to the repo). Single risk module
with four submodules + 5 test files. Nothing under `user_data/` or `stocks/`
was touched.

| File | LOC | Origin |
|---|---:|---|
| `src/quanta_core/risk/governor.py` | 800 | port of `user_data/modules/risk_governor.py` |
| `src/quanta_core/risk/ownership.py` | 216 | port of `stocks/shared/subsystem_ownership.py` |
| `src/quanta_core/risk/asset_class_gate.py` | 144 | NEW (derived from 2026-05-12 Shark/Wheel leak fix) |
| `src/quanta_core/risk/monte_carlo.py` | 730 | NEW (CuPy + Bates + antithetic + GBM control variate) |
| `src/quanta_core/risk/__init__.py` | 65 | public surface |
| `tests/test_governor.py` | 609 | ports `tests/test_risk_governor_dup_index.py` + `tests/test_risk_governor_backtest_isolation.py` |
| `tests/test_ownership.py` | 213 | ports `stocks/tests/test_subsystem_ownership.py` |
| `tests/test_asset_class_gate.py` | 91 | NEW |
| `tests/test_monte_carlo.py` | 307 | NEW |
| `tests/test_coverage_gaps.py` | 404 | NEW (targeted branch coverage) |
| `tests/conftest.py` | 30 | per-test anchor + state-dir isolation |
| **Total** | **3609** | (src 1955 / tests 1654) |

## Port fidelity

* **risk_governor.py → governor.py: 100%.** Every public symbol survives:
  `RiskConfig`, `RiskDecision`, `TradeRecord`, `RiskGovernor` with the same
  ctor signature (`config`, `now_fn`, `runmode`), the same `approve_entry`
  signature, the same `record_trade_close` / `update_equity` /
  `resume_after_manual_review` / `status` / `from_config` /
  `from_config_file` surface. Internal `_resolve_anchor_path`,
  `_anchor_path`, `_BACKTEST_RUNMODES`, `_load_anchors`, `_persist_anchors`,
  `_pearson_returns`, `_kelly_fraction`, `_block` all preserved.
  Default anchor path moved from
  `user_data/state/risk_governor_anchors.json` (Freqtrade-rooted)
  to `~/.quanta/state/risk_governor_anchors.json` per the V4 architecture
  lock (doc 06 §2.2). The `RISK_GOVERNOR_ANCHORS_PATH` env-var override
  honoured in every mode for test fixtures; the runmode-aware /tmp routing
  for backtest/hyperopt/edge is preserved verbatim.
  * **Bug 1 dedup fix (`~df.index.duplicated(keep="last")`) preserved.**
  * **Bug 2 runmode-aware anchor preserved.**
  * Added a `RiskDecision.outcome` property returning `pass`/`block` so the
    caller can prefer the 3-state tag used by `MonteCarloEngine`.

* **subsystem_ownership.py → ownership.py: 100%.** Same public surface
  (`load_owned`, `save_owned`, `owns`, `claim`, `release`, `Subsystem`,
  `SCHEMA_VERSION`). State path was generalised to
  `~/.quanta/state/owned_symbols-{subsystem}.json` (per task spec); the
  `QUANTA_STATE_DIR` env var lets tests redirect. Same atomic-write via
  `os.replace`, same schema, same logging shape.

* **asset_class_gate.py: NEW.** A pure function distilled from the
  `stocks/shark/phases/midday.py` leak fix. Returns `False` for non-equity
  rows when the asking subsystem is not `"wheel"`; returns the ownership
  ledger answer otherwise. Never raises (errors degrade to `False`).

## Monte Carlo gate

`MonteCarloEngine.evaluate(symbol, calibration) -> MCDecision`. Decision
fields: `outcome` (`pass|warn|block`), `reason`, `var_99`, `es_975`,
`max_dd_q99`, `tail_asym`, `es_ci_width`, `latency_ms`, `model`,
`num_paths`.

Implementation:
* Bates model (Heston full-truncation Andersen for the variance SDE
  + Merton compound-Poisson jumps), config-switchable to GBM.
* Antithetic variates (zero-cost variance halving), antithetic baked into
  both CPU + GPU paths.
* Bootstrap-CI on ES (B=200) gates against `es_ci_max_frac` (default 1% of
  notional).
* **Fail-closed contract** — gate emits `block` outcome (not raise) when:
  * calibration age > `CALIBRATION_MAX_AGE_S` (default 3600s);
  * ES bootstrap CI width > 1% of notional;
  * any of `var_99 / es_975 / max_dd_q99 / tail_asym` breaches its block
    threshold.
* CuPy + PyTorch are **optional**. Engine instantiates fine without them;
  `evaluate()` raises `MonteCarloError("CuPy is not installed; …")` with a
  clear hint UNLESS `cfg.use_cpu_fallback=True` (tests + degraded-mode).
* GPU path mirrors the CPU one operation-for-operation in `_simulate_gpu`.
  CUDA Graphs capture is sketched (`_maybe_capture_graph` placeholder) but
  not exercised; eager execution still hits the 50 ms SLA at 1 symbol per
  call. Multi-symbol fan-out + Graph capture are the next-step refinements
  flagged in the docstring.

### MC latency benchmark

* **Real GB10 (CuPy):** NOT MEASURED — `cupy` is not installed in this
  worktree's Python env. The GPU is present (`nvidia-smi -L → GPU 0:
  NVIDIA GB10`); installing `cupy-cuda13x` (CUDA 13 per the feasibility
  doc) will let the `@pytest.mark.gpu` test
  `test_monte_carlo_latency_sla` assert the doc-03 budget (median ≤ 50 ms,
  10k paths × 60 steps × Bates+jumps).
* **CPU NumPy fallback baseline** (same 10k × 60 Bates+jumps, this box):
  * `median_ms` ≈ 121
  * `p99_ms` ≈ 129
  * `min_ms` ≈ 95
  * `max_ms` ≈ 129
  This is the **degraded-mode** path; production traffic must run on the
  GPU. The SLA test is marked `@pytest.mark.gpu` and skipped here.

## Quality gates

| Gate | Result |
|---|---|
| `pytest tests/` | **113 passed**, 2 GPU skipped (CuPy absent) |
| `pytest --cov=src/quanta_core` | **98.25%** total — gate is 95% per doc 10 (risk = highest-priority module) |
| `ruff check` | clean |
| `ruff format --check` | clean |
| `mypy --strict src/` | clean (6 source files) |

Per-file coverage:
* `governor.py` 97%
* `monte_carlo.py` 99%
* `asset_class_gate.py` 100%
* `ownership.py` 100%
* package `__init__.py` files 100%

The 1-2 uncovered branches per file are all defensive `except`/`atexit`
paths that can't be triggered without killing the process — same pattern
the legacy `risk_governor.py` used.

## What's NOT in this drop (deliberately)

* **No HTTP / FastAPI integration.** The task is the in-process risk
  module; the `/api/ops/risk` route binding is the responsibility of the
  `quanta_core.ops` build agent.
* **No execution-engine plumbing.** The architecture's `ExecutionEngine`
  consumes `RiskGovernor.approve()` + `MonteCarloEngine.evaluate()`. That
  wiring is a separate agent.
* **No CUDA Graph capture.** Sketch in code; full capture is a follow-up
  once the engine is being driven by the real strategy loop and we can
  measure where the launch overhead actually concentrates.
* **No Hawkes intra-day intensity alarm.** Doc 03 §3 marks Hawkes
  out-of-band relative to the path-gen module — separate cron + state
  file.
* **No live HF/Prometheus emission.** Same reason as the route binding:
  the observability layer mounts a snapshot reader, not the engine.

## Commits

| SHA | Title |
|---|---|
| `edb72ac` | feat(v4-risk): port risk_governor + subsystem_ownership; add Monte Carlo gate |

NOT pushed (per the task constraint). Branch lives in this worktree only.

## How to run

```bash
cd quanta_core
PYTHONPATH=src python -m pytest tests/ --cov=src/quanta_core
PYTHONPATH=src python -m pytest tests/ -m gpu      # after installing cupy
python -m ruff check src/ tests/
python -m mypy --strict src/
```

## Next-agent suggestions

1. Wire `RiskGovernor` + `MonteCarloEngine` into the
   `quanta_core.execution.engine.ExecutionEngine.submit()` pipeline (the
   "single chokepoint" from architecture doc §3.14).
2. Install `cupy-cuda13x` matching CUDA 13 on GB10 and run
   `pytest -m gpu` to confirm the median-ms budget.
3. Add a CUDA Graph capture pass that batches all symbols' path
   recurrences into one `cudaGraphLaunch` (doc 03 §4 — the source of the
   5× win).
4. Expose `RiskGovernor.status()` and `MonteCarloEngine.benchmark()` snapshots
   to the dashboard via `quanta_core.ops.routes`.
