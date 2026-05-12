# V4 Build Wave 1 — Path Conflict Report (morning review)

**Status as of 2026-05-12 ~22:00 ET:** 2 of 6 build agents landed with
**structural conflict** on project layout. Auto-merge halted — manual
reconciliation needed before morning.

## The conflict

Two agents both wrote project scaffolding (`pyproject.toml`, `src/quanta_core/__init__.py`)
but to **different paths**:

| Agent | Branch | Layout | Conformance to doc #10 |
|---|---|---|---|
| #6 execution | `feat/v4-build-execution` (tip `d1620e1`) | `pyproject.toml` + `src/quanta_core/...` at **REPO ROOT** | ✓ matches `src/ layout: src/quanta_core/` |
| #1 foundation | `feat/v4-build-foundation` (tip `cb87f3a`) | `quanta_core/pyproject.toml` + `quanta_core/src/quanta_core/...` (**nested**) | ✗ adds an extra `quanta_core/` wrapper dir |

The other 4 build agents (exchanges, live, models, risk) are still in flight —
**don't merge any of them automatically until this layout is reconciled**, because
they'll inherit whichever layout lands first and bake it in.

## Recommendation for morning review

**Pick the execution agent's layout** (root-level `pyproject.toml` + `src/quanta_core/`)
because:

1. It matches doc #10 §1 spec verbatim.
2. The nested `quanta_core/quanta_core/` pattern is awkward for installs
   (`pip install ./quanta_core` requires the extra `quanta_core/` prefix).
3. Most modern Python projects use root-level `pyproject.toml` + `src/<pkg>/`.

Action: re-run the foundation agent with the corrected path, OR manually
relocate its output to match the execution layout (cleaner — most of its
content is identical, just paths differ).

## Per-agent quality (both excellent)

| Metric | #6 Execution | #1 Foundation |
|---|---|---|
| Tests | 134 passed | 90 passed |
| Coverage | 99.80% | 100.00% |
| Coverage gate | 95% ✓ | 85% ✓ |
| mypy --strict | clean | clean |
| ruff | clean | clean |
| LOC source / test | 1,075 / 1,662 | 892 / 1,095 |
| P0 fixes verified | 2 (cancel race · retry 4xx) | n/a |
| Port % | 85% from execution_engine.py | n/a (greenfield) |

Both are merge-quality on their own. The only issue is the structural overlap.

## Other 4 agents (in flight at fire time)

- `feat/v4-build-exchanges` — alpaca-py + coinbase-advanced-py wrappers
- `feat/v4-build-live` — WS consumer + tick aggregator + dispatcher
- `feat/v4-build-models` — registry + TFT port (drop legacy serialize, use safetensors)
- `feat/v4-build-risk` — risk_governor port + CuPy Monte Carlo

When they land, I'll record their completion and quality metrics in this file
WITHOUT merging.

## Tomorrow morning recipe

```bash
# 1. Inspect both candidate layouts
git checkout feat/v4-build-execution -- pyproject.toml src/quanta_core/
git checkout feat/v4-build-foundation -- quanta_core/

# 2. Pick execution's layout (recommendation above)

# 3. Cherry-pick foundation's modules into the execution layout
mkdir -p src/quanta_core/strategy
git show feat/v4-build-foundation:quanta_core/src/quanta_core/types.py > src/quanta_core/types.py
git show feat/v4-build-foundation:quanta_core/src/quanta_core/strategy/base.py > src/quanta_core/strategy/base.py
# ... etc for config.py, logging_setup.py

# 4. Reconcile pyproject.toml (foundation has more deps; merge dependency lists)

# 5. Commit on feat/v4-build as a single reconciliation commit

# 6. Resume auto-merge for the other 4 branches now that layout is settled
```

— claude (auto-paused 2026-05-12 22:00 ET)
