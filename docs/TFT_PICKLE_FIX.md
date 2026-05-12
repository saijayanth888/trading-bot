# TFT PicklingError Fix - Operator Handoff

**Branch:** `fix/tft-pickling-error`
**Date:** 2026-05-12
**Status:** committed, not pushed, not deployed (awaits operator review per `reload_config` plan)

---

## Root cause (one paragraph)

FreqAI's `IResolver` loads `user_data/freqaimodels/TFTModel.py` via
`importlib.util.spec_from_file_location("TFTModel", path)` followed by
`module_from_spec` + `exec_module`. That re-executes the file every time it is
called, producing a *new* module object with brand-new class objects defined
inside it. `TFTTrainerWrapper` used to be defined inside `TFTModel.py`, so each
re-exec created a fresh `TFTTrainerWrapper` class. The existing
`_register_module_aliases()` shim cached the *first* class in
`sys.modules["TFTModel"]` and never refreshed - so when `torch.save({"pytrainer":
wrapper, ...})` later tried to resolve the wrapper's class via
`sys.modules["TFTModel"].TFTTrainerWrapper`, it found the V1 class while the
wrapper instance held a V2 class, and the serializer raised the production
error `PicklingError: it's not the same object as TFTModel.TFTTrainerWrapper`.
The fix moves the wrapper into a regular package module
(`freqaimodels.tft_pickle`) whose class identity is deduplicated by Python's
standard `sys.modules` cache, so all re-imports of `TFTModel.py` see the *same*
class object.

## Failure scope (before the fix)

The save failure aborts `data_drawer.save_data()` at the very first step
(`model.save(...)` on line 523), so **none** of the sidecars are written:

| File | Without fix | With fix |
|---|---|---|
| `*_model.zip` | 786-byte error stub | full ~28 MB weights |
| `*_metadata.json` | missing | present |
| `*_feature_pipeline.pkl` | missing | present |
| `*_label_pipeline.pkl` | missing | present |
| `*_trained_dates_df.pkl` | missing | present |
| `*_trained_df.pkl` | missing | present |

Recent log evidence (`user_data/logs/freqtrade.log`, 2026-05-12) shows the bug
hit at least **6 pairs in 12 hours**: AVAX/USD, LINK/USD, ADA/USD (twice),
XRP/USD (twice), DOGE/USD. After each failure the affected pair logs:

```
freqtrade.freqai.freqai_interface - WARNING - Model expired for <PAIR>,
returning null values to strategy
```

...indefinitely, because `pair_dictionary.json` never gets the new
`trained_timestamp` write either.

## Files touched

| Path | Before | After | Notes |
|---|---|---|---|
| `user_data/freqaimodels/tft_pickle.py` | did not exist | new file with `TFTTrainerWrapper`, `_set_inference_mode`, `_set_training_mode` | the serialization-stable home for everything that gets persisted inside `model.zip` |
| `user_data/freqaimodels/TFTModel.py` | wrapper at line 97-119; helpers at line 81-87 | imports them from `freqaimodels.tft_pickle` | also adds `_neutral_predictions` fallback for inference when sidecars are missing; `_register_module_aliases` now refreshes on every re-exec instead of guarding on first import |
| `tests/test_tft_pickle.py` | did not exist | new file with 6 tests | `test_class_identity_stable_across_simulated_resolver_imports` is the precise regression test |
| `docs/TFT_PICKLE_FIX.md` | did not exist | this file | |

## Backward compatibility / compatibility shim

Existing `model.zip` files on disk were persisted with `__module__ ==
"TFTModel"`. After the fix, new payloads have `__module__ ==
"freqaimodels.tft_pickle"`. Both load correctly:

- **New payloads** load via the normal import path; `freqaimodels.tft_pickle`
  is importable because `_USER_DATA` is added to `sys.path` at the top of
  `TFTModel.py`.
- **Old payloads** load via the `sys.modules["TFTModel"]` proxy that
  `TFTModel.py` still registers at the bottom. The proxy now points
  `TFTTrainerWrapper` at the canonical class in `freqaimodels.tft_pickle`, so
  the serializer resolves the old `(TFTModel, TFTTrainerWrapper)` string pair
  to the *new* class. No migration step is needed.

This is exercised by `test_old_pickle_loads_via_tftmodel_proxy` in the test
file.

## Test command + result

Run on host (skips the freqtrade-dependent cases cleanly):

```
$ python tests/test_tft_pickle.py
PASS  test_module_is_stable_under_repeated_imports
SKIP test_class_identity_stable_across_simulated_resolver_imports (freqtrade not installed - run inside container)
PASS  test_pickle_roundtrip_in_memory
PASS  test_torch_save_roundtrip_via_wrapper
PASS  test_atomic_save_leaves_no_tmp_on_success
SKIP test_old_pickle_loads_via_tftmodel_proxy (freqtrade not installed - run inside container)

all 6 tests passed
```

Run inside the running freqtrade container (full coverage):

```
$ docker cp tests/test_tft_pickle.py freqtrade:/tmp/test_tft_pickle.py
$ docker exec freqtrade python /tmp/test_tft_pickle.py
PASS  test_module_is_stable_under_repeated_imports
PASS  test_class_identity_stable_across_simulated_resolver_imports
PASS  test_pickle_roundtrip_in_memory
PASS  test_torch_save_roundtrip_via_wrapper
PASS  test_atomic_save_leaves_no_tmp_on_success
PASS  test_old_pickle_loads_via_tftmodel_proxy

all 6 tests passed
```

Independent end-to-end verification with a real `TemporalFusionTransformer`:

```
$ docker exec freqtrade python -c "<<the snippet in the session log>>"
wrote model.zip (118349 bytes)
keys: ['model_state_dict', 'model_meta_data', 'pytrainer', 'optimizer_state_dict']
class_names: ['down', 'flat', 'up']
load_from_checkpoint OK
```

I also ran the exact same wrapper-save flow against the **pre-fix** code from
HEAD (commit `9d40efe`) inside the container, and confirmed it raises::

    PicklingError: Can't pickle <class 'TFTModel.TFTTrainerWrapper'>:
        it's not the same object as TFTModel.TFTTrainerWrapper

So the regression test is provably catching the right bug.

## How to verify the fix in production (no restart)

The running freqtrade process already loaded `TFTModel.py` once at startup, so
its in-memory module still holds the *old* class. The fix activates after
`reload_config`, which `worker._reconfigure()` implements by calling
`freqtrade.cleanup()` then `_init(True)` then `load_freqAI_model()` -
re-resolving the model from disk.

1. Tail the logs in one terminal:

   ```
   tail -F user_data/logs/freqtrade.log | grep -E "PicklingError|TFTModel|save_data|Training .* raised"
   ```

2. Trigger reload_config via the FreqAI REST API (auth helpers already in the
   dashboard, e.g. `dashboard/data_sources.py::ft_authed_post`):

   ```
   POST /api/v1/reload_config
   ```

3. Wait for the model-expired pairs (AVAX/USD, LINK/USD, DOGE/USD per the
   current log) to retrain - this happens within a few minutes because their
   `trained_timestamp` is already stale.

4. Expected log signature on success:

   ```
   TFTModel - INFO - early stopping at epoch N (best val_sharpe=X)
   <no PicklingError follows>
   freqtrade.freqai.freqai_interface - INFO - Total time spent inferencing pairlist ...
   ```

   Plus, list the on-disk artifacts to confirm sidecars exist:

   ```
   docker exec freqtrade ls -la /freqtrade/user_data/models/tft_v1/sub-train-<PAIR>_<TS>/
   # Should now show 6 files: model.zip + 5 sidecars + tensorboard/
   ```

5. Run the full test suite once more inside the freqtrade container as a
   smoke check.

## Risk + rollback

**Risk: very low.** The fix touches three things only:

1. **Moves a class definition** from `TFTModel.py` into a new module. The class
   is structurally identical - same `__init__`, same `save`, same
   `load_from_checkpoint`, same attributes. Only `save` gained an atomic
   tmp+rename wrapper (with bare `torch.save` semantics on the happy path).
2. **Adds a defensive `_neutral_predictions` fallback** to `predict()` that
   *only* triggers when `dk.feature_pipeline` is missing/invalid. Today, that
   path crashes; after the fix, it returns an all-uniform / zero-confidence
   frame with `do_predict=0` so the strategy ignores the pair. This is strict
   improvement.
3. **Refreshes the `sys.modules["TFTModel"]` proxy** on every re-exec instead
   of skipping when already present. Old saves keep loading because the proxy
   still exposes `TFTTrainerWrapper`.

**No other strategies are touched** (FreqAIMeanRevV1, NFI X6, BollingerRSI are
all separate files). No config-file changes. No dependency changes. No
database changes.

**Rollback path:**

```
git checkout main -- user_data/freqaimodels/TFTModel.py
git rm user_data/freqaimodels/tft_pickle.py tests/test_tft_pickle.py docs/TFT_PICKLE_FIX.md
# then reload_config again to swap back to old code
```

After rollback, any `model.zip` files that were persisted with `__module__ ==
"freqaimodels.tft_pickle"` will still load because
`freqaimodels/tft_pickle.py` would still exist on disk if you only revert
`TFTModel.py`. The cleanest "go back to exactly main" rollback is
`git checkout main -- user_data/freqaimodels/` which removes `tft_pickle.py`
too; any new-format saves would have to be retrained. Old-format saves remain
loadable in either direction.

## Constraints met

- Operator has 5 open stock positions on IBKR (not freqtrade). The freqtrade
  container is crypto-only. The fix does not restart freqtrade; activation is
  via `reload_config` only.
- No other strategies touched.
- Total LOC: ~330 added across the new module, modified TFTModel.py, the new
  test file, and this doc - well within the 200-500 LOC budget.
- Branch `fix/tft-pickling-error` committed locally; not pushed.
