# HANDOFF — `fix/shark-wheel-isolation`

**Date:** 2026-05-12
**Branch:** `fix/shark-wheel-isolation`
**Status:** NOT pushed. All commits local on this branch.
**Test suite:** 114 tests passing across the affected modules (10 new + 21 new + 83 pre-existing).

---

## What broke (one paragraph)

Today (2026-05-12) Shark's midday phase emitted five SELL rows in
`stocks/memory/TRADE-LOG.md` against long-put options (SOFI / PLTR /
NVDA / MARA / HOOD, all `260522` expiry). Catalyst: *"Midday cut: -7%
rule triggered"* — the legacy hard-stop loop in
`stocks/shark/phases/midday.py:134`. Those options were opened by the
**Wheel** subsystem (CSP/CC strategy) and should never have been
touched by Shark. `stocks/CLAUDE.md` is unambiguous on this:

* line 4: "Goal: Beat S&P 500. Stocks ONLY — no options, no crypto, no ETFs"
* line 29: "NO OPTIONS. EVER. Stocks only."
* line 51: "Buy-Side Gate rule 6: Instrument is a stock (not an option, ETF, or crypto)"

Root cause: every Shark loop that iterates Alpaca positions trusted the
list verbatim — there was no asset-class gate, and no per-subsystem
ownership concept.

---

## Two layered fixes — overview

| Fix     | Scope                                   | Detection mechanism           | Files                                                                                                                                                |
|---------|-----------------------------------------|-------------------------------|------------------------------------------------------------------------------------------------------------------------------------------------------|
| **Fix 1** | Asset-class gate                       | `asset_class != "us_equity"` | `data/alpaca_data.py`, `execution/exit_manager.py`, `execution/stops.py`, `execution/guardrails.py`, `phases/midday.py`, `phases/pre_market.py` |
| **Fix 3** | Per-subsystem ownership tagging        | `symbol not in shark_owned`  | new `shared/subsystem_ownership.py` + wiring in `phases/market_open.py`, `phases/midday.py`, `execution/exit_manager.py`, `execution/stops.py`, `wheel/runner.py` |

Fix 2 (changing the -7% threshold) was rejected by the operator and is
not touched here.

---

## Fix 1 — asset-class gate

Pattern applied at every site:

```python
asset_class = pos.get("asset_class", "us_equity")
if asset_class != "us_equity":
    logger.warning(
        "[shark.<site>] skipping non-equity position %s (asset_class=%s) — "
        "managed by wheel or other subsystem, NOT Shark's concern",
        pos.get("symbol", "?"), asset_class,
    )
    continue
```

### Files gated (one atomic commit per file)

| Commit    | File                                       | Site                                           | Lines |
|-----------|--------------------------------------------|------------------------------------------------|-------|
| `e72298b` | `stocks/shark/data/alpaca_data.py`         | `get_positions()` now returns `asset_class`    | +13 -1 |
| `b28f235` | `stocks/shark/execution/exit_manager.py`   | `evaluate_exits()` top of loop                 | +17   |
| `7d8f058` | `stocks/shark/phases/midday.py`            | Phases 2 (-7% hard stop), 4 (vol exp), 5 (thesis break) | +41   |
| `b5f6417` | `stocks/shark/phases/pre_market.py`        | `at_risk` premarket pager loop                 | +23 -1 |
| `1362ffc` | `stocks/shark/execution/stops.py`          | `manage_stops()` top of loop                   | +15   |
| `1bc8082` | `stocks/shark/execution/guardrails.py`     | `check_max_positions` + sector concentration   | +17 -3 |

The foundational commit `e72298b` adds `asset_class` to every dict
returned by `get_positions()` — without that field every downstream gate
would default to `us_equity` and pass everything through.

### What I deliberately did NOT gate

* `stocks/shark/phases/pre_execute.py` — does not iterate Alpaca positions
  for management; pulls bars by symbol only.
* `stocks/shark/phases/market_open.py` `existing_symbols` set — used only
  for "already holding this symbol" duplicate-trade detection; including
  options here is correct (don't open a Shark equity in a name Wheel
  already has option exposure on).
* `stocks/shark/phases/daily_summary.py` — reporting only, no management
  actions.
* `stocks/shark/signals/templates.py` — HTML formatting only.

---

## Fix 3 — per-subsystem ownership

### State files

```
stocks/shark/state/owned_symbols.json
stocks/wheel/state/owned_symbols.json
```

Schema:

```json
{
  "updated_at": "2026-05-12T19:30:00Z",
  "symbols": ["NVDA", "AAPL"],
  "schema_version": 1
}
```

Both are gitignored as runtime state.

### New module: `stocks/shared/subsystem_ownership.py`

Commit `e98c221`. Public API:

| Function                                  | Purpose                                       |
|-------------------------------------------|-----------------------------------------------|
| `load_owned(subsystem) -> set[str]`       | Read state file; empty set if missing/corrupt |
| `save_owned(subsystem, symbols)`          | Atomic write (`os.replace` over tempfile)     |
| `owns(owned_set, symbol) -> bool`         | Case-insensitive membership test              |
| `claim(subsystem, symbol)`                | Idempotent append + save                      |
| `release(subsystem, symbol)`              | Idempotent remove + save                      |

Atomic-write resilience verified by `test_atomic_no_partial_on_crash`
(test_subsystem_ownership.py:85): simulates a `os.replace` failure
mid-write and asserts the destination file is byte-for-byte unchanged
and no `.tmp` files leak.

### Wiring map

#### Shark
- `phases/market_open.py` `_execute()` after `place_bracket_order` → `claim("shark", symbol)`
- `phases/market_open.py` `_run_full()` after `place_bracket_order` → `claim("shark", symbol)`
- `phases/midday.py` Phase 1 `CLOSE_ALL` after `close_position` → `release("shark", symbol)`
- `phases/midday.py` Phase 2 hard-stop after `close_position` → `release("shark", symbol)`
- `phases/midday.py` Phase 5 thesis-break after `close_position` → `release("shark", symbol)`
- `phases/midday.py` Phase 1 `PARTIAL_SELL` → **no release** (runner remains)

Commit: `18aa172`.

#### Wheel
- `runner.py` `_sell_one_csp` after `add_position` → `claim("wheel", occ_ticker)` + `claim("wheel", underlying)`
- `runner.py` `_check_one_assignment` (assignment confirmed) → `claim("wheel", underlying)` (idempotent re-claim)
- `runner.py` `_check_one_assignment` (stale CSP removal) → `release("wheel", occ_ticker)` + conditional release of underlying
- `runner.py` `_check_csp_profit_take` (buy-to-close) → `release("wheel", occ_ticker)` + conditional release of underlying
- `runner.py` `sell_covered_calls` after `add_position` → `claim("wheel", occ_ticker)`

Commit: `52eb585`.

### Pre-action ownership check

Added in commit `2f31eb5` at every Shark management loop, alongside the
asset_class gate from Fix 1:

```python
if _ownership_active and symbol.upper() not in _shark_owned:
    logger.warning("[shark.<site>] skipping %s — equity but not in Shark's owned set")
    continue
```

`_ownership_active = bool(_shark_owned)` — when the owned set is empty
(cold-start, pre-bootstrap) the check is bypassed and asset_class alone
is the firewall. This is intentional fail-safe: an operator running
the upgrade without the bootstrap script still sees normal behaviour.

---

## Migration / bootstrap

### One-shot script: `stocks/shared/migrate_ownership_bootstrap.py`

Commit: `597adb1`.

```bash
cd stocks
python -m shared.migrate_ownership_bootstrap --dry-run    # inspect the plan
python -m shared.migrate_ownership_bootstrap              # write state files
```

Logic:
1. **Wheel set** = all OCC tickers in `wheel/state/positions.json`
   ∪ underlyings of any `long_shares` (assignment legs).
2. **Shark set** = every `us_equity` row on Alpaca whose symbol is NOT
   in the Wheel set.

Default behaviour aborts if either state file already exists; pass
`--force` to overwrite.

### Run order

1. Merge this branch into `main` (or cherry-pick the 12 commits).
2. Deploy.
3. SSH to the host, `cd stocks`, run the bootstrap script once.
4. From this point on Shark/Wheel claim/release at every open/close.

---

## Verification

### Tests added

```bash
cd stocks
python -m pytest tests/test_subsystem_ownership.py    # 21 passed
python -m pytest tests/test_shark_wheel_isolation.py  # 10 passed
```

The 2026-05-12 leak is covered specifically by
`TestFix1AssetClassGate::test_evaluate_exits_zero_options_no_close_calls` —
it feeds the five 260522 OCC tickers into `evaluate_exits()` and asserts
ZERO actions are emitted.

Combined run across affected modules: **114 tests passing.**

```bash
python -m pytest tests/test_exit_manager.py \
                 tests/test_stops.py \
                 tests/test_subsystem_ownership.py \
                 tests/test_shark_wheel_isolation.py \
                 tests/test_guardrails.py \
                 tests/test_alpaca_data.py -q
# 114 passed in 0.45s
```

`test_wheel_preconditions.py` fails on this dev host with
`ModuleNotFoundError: No module named 'alpaca'` — pre-existing
(reproduces on `git stash` baseline), unrelated to this branch.

### Operator checks (after merge + dashboard hard-refresh)

1. Tomorrow's midday phase runs: zero options in TRADE-LOG SELL rows.
2. New Wheel CSP openings populate `wheel/state/owned_symbols.json`
   and Shark's trade-log stays clean.
3. `cat stocks/shark/state/owned_symbols.json` shows current
   Shark-managed equity tickers.
4. `cat stocks/wheel/state/owned_symbols.json` shows current
   Wheel-owned OCC tickers + assigned underlyings.

---

## Known limits

* **No cross-process file lock on owned_symbols.json.** Atomic writes
  via `os.replace` prevent torn writes but cannot serialize concurrent
  read-modify-write cycles. The operator's cron schedule already
  serializes Shark phases and Wheel routines (no two of them run
  simultaneously), so this is acceptable today. If we ever go multi-
  worker, swap in `shark.memory.atomic.file_lock` around `claim/release`.

* **Cold-start fall-through.** Until `migrate_ownership_bootstrap` runs,
  Shark uses asset_class alone (the Fix 1 firewall is still up). This
  is intentional — the alternative would brick the operator the first
  time the upgrade lands.

* **Wheel's release of the underlying** in `_check_one_assignment`
  (stale CSP path) and `_check_csp_profit_take` consults
  `load_positions()` after `remove_position()`. If a future refactor
  changes the order, the conditional `if not still_held` could
  prematurely release the underlying while shares remain. Tests cover
  the current code path.

* **Test gap.** I didn't write an end-to-end midday phase integration
  test because that path has 8+ external dependencies (Perplexity,
  Alpaca, kill_switch, market_regime, knowledge_base, etc.). The unit
  tests cover every funnel point Shark actually uses to act on a
  position.

---

## Commits (12 total)

```
fe9720d test: regression tests for the 2026-05-12 Shark/Wheel leak
52eb585 feat(wheel): wire ownership claim/release into runner sites (Fix 3)
2f31eb5 feat(shark): ownership pre-action check at every Shark management loop (Fix 3)
18aa172 feat(shark): wire ownership claim/release into BUY and SELL paths (Fix 3)
597adb1 feat(shared): one-shot ownership bootstrap script (Fix 3)
e98c221 feat(shared): subsystem_ownership module — per-subsystem position tagging (Fix 3)
1bc8082 fix(shark): gate guardrails position-count + sector concentration to us_equity (Fix 1)
1362ffc fix(shark): gate stops.manage_stops() to us_equity only (Fix 1)
b5f6417 fix(shark): gate pre_market at_risk premarket loop to us_equity only (Fix 1)
7d8f058 fix(shark): gate midday Phases 2/4/5 to us_equity only (Fix 1)
b28f235 fix(shark): gate exit_manager.evaluate_exits() to us_equity only (Fix 1)
e72298b fix(shark): get_positions() returns asset_class — foundation for Shark/Wheel isolation
```

```
14 files changed, 1126 insertions(+), 5 deletions(-)
```
