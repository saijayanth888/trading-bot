# V4 Build Wave 1 — Status Report (morning review)

**Status as of 2026-05-12 ~22:25 ET:** **4 of 6** build agents landed.
**Three at root-level layout** (execution, live, risk) — **one nested**
(foundation). Auto-merge halted; manual reconciliation needed in morning.
Reconciliation is simple — just relocate foundation's content from
`quanta_core/src/quanta_core/` → `src/quanta_core/`.

## Landed agent inventory

| # | Agent | Branch (tip) | Layout | Tests | Coverage | mypy | LOC src/test |
|---|---|---|---|---|---|---|---|
| 6 | Execution | `feat/v4-build-execution` (`d1620e1`) | ✓ root | 134 ✓ | 99.80% | clean | 1,075 / 1,662 |
| 3 | Live | `feat/v4-build-live` (`86e1b4e`) | ✓ root | 37 ✓ | 96% | clean | 1,516 / 1,182 |
| 5 | Risk | `feat/v4-build-risk` (`3926cbb`) | ✓ root | 113 ✓ | 98.25% | clean | 1,890 / 1,654 |
| 1 | Foundation | `feat/v4-build-foundation` (`cb87f3a`) | ✗ NESTED | 90 ✓ | 100% | clean | 892 / 1,095 |

**Vote tally: 3 root-level · 1 nested. Reconcile to root-level (doc #10 spec).**

Still in flight at 22:25 ET: agents #2 (exchanges), #4 (models).

## Highlights worth surfacing

### #6 Execution
- 2 P0 fixes from validator verified: cancel partial-fill race resolved · retry policy 4xx-terminates (no more wasted rate budget on auth errors)
- 85% port from `user_data/modules/execution_engine.py`; intentional drops: threading.Lock monitor (moves to live), dry-run path (replaced by adapter `paper=True`), SDK munging (per-venue ExchangeAdapter)

### #3 Live
- Structured anyio task group: consumer + reconciler + heartbeat
- SIGINT/SIGTERM → `request_stop` (no auto-close on shutdown, per DESIGN-LOCK)
- Per-hook 30s budget via `anyio.fail_after` (matches debate budget)
- Late-tick counter (drops, never back-applies — preserves backtest determinism)

### #5 Risk
- 100% port of `risk_governor.py` — dedup fix + runmode-aware anchor preserved verbatim
- 100% port of `subsystem_ownership.py` — anchor path generalized to `~/.quanta/state/owned_symbols-{subsystem}.json`
- NEW `asset_class_gate.py` — pure function distilled from today's Shark/Wheel leak fix
- NEW Monte Carlo engine — Bates (Heston SV + Merton jumps), antithetic + GBM control variate, fail-closed on stale calibration. CuPy optional (lazy import); CPU fallback at 10k×60 Bates+jumps: median 121ms, p99 129ms

### #1 Foundation
- 100% coverage on greenfield types + Strategy ABC + config + structlog
- 90 tests across 5 files
- Notable design: Strategy ABC is sync (not async per doc #6) — operator-locked from DESIGN-LOCK §5; preserves backtest determinism
- Decimal import at runtime (not TYPE_CHECKING) — required by Pydantic v2 forward-ref resolution

## Tomorrow morning recipe

```bash
cd /home/saijayanthai/Documents/trading-bot
git checkout feat/v4-build

# 1. Adopt execution's root layout as canonical
git merge --no-ff feat/v4-build-execution

# 2. Layer in live (matching root layout — should be clean after resolving
#    overlap on the sibling ABC files like strategy/base.py, util/types.py)
git merge --no-ff feat/v4-build-live
# (conflict on strategy/base.py + util/types.py — pick the more complete version per file)

# 3. Layer in risk
git merge --no-ff feat/v4-build-risk
# (likely conflict-free — risk only touches src/quanta_core/risk/)

# 4. Foundation reconciliation — relocate its files
git checkout feat/v4-build-foundation -- quanta_core/
# move quanta_core/src/quanta_core/* → src/quanta_core/*
# move quanta_core/pyproject.toml → merge dependency lists into root pyproject.toml
# rm -rf quanta_core/
git add -A && git commit -m "merge: foundation (relocated to root layout)"

# 5. Run combined test suite — expected ~370 tests
pytest src/quanta_core/ tests/
```

## Conditions for resuming auto-merge

- Foundation relocated to root layout
- Combined test suite green on `feat/v4-build`
- THEN auto-merge can resume for the 2 in-flight agents (exchanges, models)

— claude (auto-paused 2026-05-12 22:25 ET)
