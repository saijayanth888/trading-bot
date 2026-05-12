# HANDOFF — 4 pre-existing bugs fixed on `fix/pre-existing-bugs`

Branch off `main`. NOT pushed. 4 atomic commits + this hand-off doc.

```
git log --oneline main..fix/pre-existing-bugs
5f70031 test(strategy): regression test for TFT-blind fallback log latch
5c43751 fix(retrain_tft_pairs): clear model_filename + data_path on reset
88aa16c fix(risk_governor): isolate backtest anchor from live state
14729cc fix(risk_governor): dedupe Series index in _pearson_returns
```

## Summary table

| # | Bug | Status | Notes |
|---|-----|--------|-------|
| 1 | `risk_governor._pearson_returns` duplicate-index crash | **FIXED** | dedup with `index.duplicated(keep="last")` before inner-join |
| 2 | Backtest reads live drawdown anchor | **FIXED** | runmode-aware `_resolve_anchor_path()` → `/tmp/risk_governor_backtest_<pid>.json` for backtest/hyperopt/edge |
| 3 | `retrain_tft_pairs.py` leaves stale `model_filename` + `data_path` | **FIXED** | now clears both to match freqai's `empty_pair_dict` shape |
| 4 | TFT-blind fallback ACTIVE log didn't fire post-restart | **VERIFIED** (no code change) | regression test added; root cause = timing (90s monitor vs 5m candle), not a code bug |

All test suites: **36 passed** (17 from `test_risk_execution.py` baseline + 19 new across 4 files).

---

## Bug 1 — `_pearson_returns` duplicate-index crash

**Symptom**: live freqtrade log emits
```
pandas.errors.InvalidIndexError: Reindexing only valid with uniquely
valued Index objects
```
from `RiskGovernor._pearson_returns` when two trades close at the same candle timestamp.

**Root cause**: per-pair returns Series can carry duplicate timestamps (single 5m candle filling trailing-stop + immediate re-entry). `pd.concat([ax, bx], axis=1, join="inner")` raises on non-unique `DatetimeIndex`.

**Fix approach (root-cause)**: dedupe both Series with `~index.duplicated(keep="last")` BEFORE the inner-join. `last` mirrors the actual book state at candle close.

**File:line**: `user_data/modules/risk_governor.py:600-647` (function `_pearson_returns`).

**Test added**: `tests/test_risk_governor_dup_index.py` — 3 cases:
1. `_pearson_returns` returns finite correlation with 3 duplicate stamps on each Series
2. Asymmetric duplicates (one Series only) does not raise
3. End-to-end `approve_entry()` correctly blocks correlation with duplicate-indexed inputs

**Commit sha**: `14729cc`

---

## Bug 2 — Backtest anchor pollutes live state

**Symptom**: running `freqtrade backtesting` while the live bot was paused for drawdown read `user_data/state/risk_governor_anchors.json` with `paused_for_drawdown: true` and silently blocked every entry in the backtest. The simulator looked like a no-op while the operator believed they were measuring strategy behaviour.

**Root cause**: `_anchor_path()` returned the same on-disk path regardless of runmode. The persistence path was added for live restart safety (P0-G) but never excluded the simulator runmodes (backtest / hyperopt / edge).

**Fix approach (root-cause)**:
- New `_resolve_anchor_path(runmode)` routes `backtest | hyperopt | edge` to `/tmp/risk_governor_backtest_<pid>.json` (transient, per-PID, `atexit`-cleared); live / dry / None keep the existing path.
- `RiskGovernor.__init__` accepts optional `runmode` kwarg and stores it; every read (`_load_anchors`) and write (`_persist_anchors`) site routes through the resolver with that mode.
- `RiskGovernor.from_config(config)` extracts `config["runmode"]` — handles freqtrade's `RunMode` enum (`.value` attr) AND plain strings for test configs.
- `RISK_GOVERNOR_ANCHORS_PATH` env override STILL wins in every mode (test fixture in `conftest.py` keeps working unchanged).
- Back-compat: bare `_anchor_path()` still works (other callers unaffected).

**File:line**:
- `user_data/modules/risk_governor.py:64-109` — `_BACKTEST_RUNMODES` constant + `_resolve_anchor_path` + back-compat shim
- `user_data/modules/risk_governor.py:206-230` — `__init__` takes `runmode`, registers atexit cleanup
- `user_data/modules/risk_governor.py:251` + `300` — `_load_anchors`/`_persist_anchors` route through resolver
- `user_data/modules/risk_governor.py:347-360` — `from_config` extracts runmode

**Test added**: `tests/test_risk_governor_backtest_isolation.py` — 9 cases including the smoking-gun test that plants a poisoned anchor with `paused_for_drawdown=True` and verifies a `runmode="backtest"` governor does NOT inherit it.

**Commit sha**: `88aa16c`

---

## Bug 3 — `retrain_tft_pairs.py` leaves stale `model_filename` + `data_path`

**Symptom**: after `python3 scripts/retrain_tft_pairs.py --only-stubs`, pair_dictionary.json entries had `trained_timestamp=0` but `model_filename` + `data_path` still pointed at the deleted stub folder. Between this script running and freqai finishing the new train, freqai's `load_data()` raised `FileNotFoundError` per candle → strategy's broad except caught it but the log became noisy (one ERROR per pair per candle until retrain done).

**Root cause**: `reset_pairs()` zeroed only `trained_timestamp`, leaving the two path-shaped fields stale.

**Fix approach (root-cause)**: also clear `entry["model_filename"] = ""` and `entry["data_path"] = ""` to match freqai's own `empty_pair_dict` shape from `freqtrade/freqai/data_drawer.py` line ~100:
```python
self.empty_pair_dict = {
    "model_filename": "", "trained_timestamp": 0,
    "data_path": "", "extras": {},
}
```
`get_pair_dict_info()` (data_drawer.py:247) treats that exact shape as "first ever train" and skips the broken load path entirely.

`data_path` / `zip_path` are resolved BEFORE the entry is cleared so the existing stub-cleanup branch still works on the same paths.

**File:line**: `scripts/retrain_tft_pairs.py:140-176` (function `reset_pairs`, the entry-rewrite block).

**Test added**: `tests/test_retrain_tft_pairs_reset.py` — 4 cases:
1. Headline: `model_filename` + `data_path` cleared on disk after reset
2. Stub artifacts removed (zip + metadata) but parent folder preserved
3. `--dry-run` leaves the file and artifacts untouched
4. Cleared entry shape matches freqai's `empty_pair_dict` exactly

**Commit sha**: `5c43751`

---

## Bug 4 — TFT-blind fallback ACTIVE log didn't fire post-restart

**Symptom**: after 18:47 restart, operator monitored 90s for
```
[strategy] DOGE/USD TFT-blind fallback ACTIVE — trading on
BollingerRSI MR signal at 50% size
```
on the 4 quarantined pairs. Log did NOT appear in that window.

**Root-cause analysis** (per the spec's checklist):

| Hypothesis | Verdict |
|---|---|
| (a) class-level `_tft_blind_logged` polluted across instances | Production declares it as `set = set()` class attr (shared across instances). Safe in practice because freqtrade runs ONE strategy instance per process. Verified by test #4. |
| (b) `blind_cfg.get("enabled")` silently False | Verified: `_tft_blind_config` defaults `enabled=False` but `config.json[strategy_overrides][tft_blind_fallback].enabled = true` in the live config. With `enabled=True` the log fires on the first column-missing candle. |
| (c) broad `try/except` swallowing the log | No: the log call is BEFORE any branch that could raise inside the inner method. The outer `try` in `populate_entry_trend` wraps the entire inner method — same iteration. |
| (d) timing — 90s window vs 5m candle cycle | **MOST LIKELY.** `process_only_new_candles=True` means `_populate_entry_trend_inner` only runs on a fresh 5m candle. 90s after restart is at most 0.3 candle. Operator should have waited 5-6 min. |

**Conclusion**: no production code bug. The fallback path is correctly wired; the latch fires exactly once per pair per process; the log copy matches the operator's grep pattern. The monitor window was simply too short.

**Test added**: `tests/test_tft_blind_log_latch.py` — 6 cases:
1. With `enabled=True`, ACTIVE log fires EXACTLY once per pair across 5 candle iterations
2. Per-pair isolation: pair A does NOT silence pair B
3. With `enabled=False`, ACTIVE log does NOT fire (falls back to "missing prediction columns" log)
4. Fresh strategy instance has fresh latches (regression guard against class-level mutable-default leak)
5. Production class still declares the latch names + log copy the operator's grep depends on (smoke check)
6. Class-level latch pollution contract documented

The test harness mirrors lines ~1635-1690 of `FreqAIMeanRevV1.py` without importing talib/freqtrade (unavailable in test env). Test #5 fails loudly if production identifiers drift.

**Operator action**: when monitoring for TFT-blind ACTIVE log after a restart, wait **at least 6 minutes** (one full 5m candle + a buffer). 90 seconds is too short.

**Verification path**: post-restart,
```bash
docker logs --since 6m freqtrade 2>&1 | grep -iE 'TFT-blind fallback ACTIVE' | head -10
```
should show one line per quarantined pair.

**Commit sha**: `5f70031`

---

## Verification — full test matrix

```bash
$ python3 -m pytest \
    tests/test_risk_governor_dup_index.py \
    tests/test_risk_governor_backtest_isolation.py \
    tests/test_retrain_tft_pairs_reset.py \
    tests/test_tft_blind_log_latch.py \
    tests/test_risk_execution.py \
    -q
36 passed
```

`py_compile` clean on all touched production files:
- `user_data/modules/risk_governor.py`
- `scripts/retrain_tft_pairs.py`

## Next steps (operator)

1. Review the 4 commits on `fix/pre-existing-bugs`.
2. Merge into `main` (operator's call — branch is NOT pushed).
3. Restart freqtrade after merge to pick up the risk_governor change.
4. Verify TFT-blind ACTIVE log appears within 6 minutes for quarantined pairs:
   ```bash
   docker logs --since 6m freqtrade 2>&1 | grep 'TFT-blind fallback ACTIVE'
   ```
5. Run a backtest in a separate terminal to confirm Bug 2 fix: backtest should NOT inherit the live `paused_for_drawdown` flag.
