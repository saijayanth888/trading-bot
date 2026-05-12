# Permanent train + backtest fixes — 2026-05-12

Branch `fix/permanent-train-and-backtest` off `main` (`9f05c65`). NOT pushed.
3 commits, ~312 net inserted lines across 6 files.

```
79bf524 config: tft_blind_fallback paper-mode default ON · dashboard banner update
95c85bd fix(strategy): coerce historic_predictions.date_pred dtype before freqai merge
9f05c65 (base = main)
```

```
 scripts/migrate_historic_predictions_dtype.py    | 174 +++++++++
 user_data/config.json                            |   5 ++-
 user_data/dashboard/static/js/ops_spa.js         |  11 +-
 user_data/dashboard/templates/dashboard_spa.html |   8 +-
 user_data/dashboard/templates/ops_spa.html       |   8 +-
 user_data/strategies/FreqAIMeanRevV1.py          | 119 ++++++
```

---

## Issue 1 — pandas dtype merge bug · ROOT-CAUSED & FIXED

**Symptom**: `--freqai-backtest-live-models` produced 0 trades across all 12
pairs (entire 2026-05-10 → 2026-05-12 window). Every per-candle
`populate_indicators` call raised

```
ValueError: You are trying to merge on datetime64[ms, UTC] and object
columns for key 'date'. If you wish to proceed you should use pd.concat
```

at `freqai_interface.py:949` (`start_backtesting_from_historic_predictions`,
`pd.merge(..., left_on="date", right_on="date_pred")`).

### Root cause

In freqai's `data_drawer.append_model_predictions` (called once per candle
in live inference):

```python
zeros_df = pd.DataFrame(np.zeros((1, len(columns))), index=index, columns=columns)
self.historic_predictions[pair] = pd.concat(
    [self.historic_predictions[pair], zeros_df], ignore_index=True, axis=0
)
```

`zeros_df` is all float64. When concatenated with the per-pair frame
(whose `date_pred` column was `datetime64[ns, UTC]` at first init via
`set_initial_historic_predictions`), pandas demotes the merged column to
`object` dtype because the cell types are now heterogenous (Timestamp +
0.0). Subsequent `df.iloc[-1, date_pred_loc] = Timestamp(...)` writes
leave the column as object-with-Timestamps forever.

That object dtype lands in the saved historic_predictions store on
disk. Live mode tolerates it (freqai re-applies `pd.to_datetime`
internally). Backtest merges the saved frame DIRECTLY against the
incoming candle dataframe (`datetime64[ms, UTC]` from feather) — pandas
refuses the merge.

Confirmed by inspecting the saved store directly:

```
BTC/USD: date_pred dtype=object, len=1085
  type=Timestamp val=Timestamp('2026-05-08 23:45:00+0000', tz='UTC')
```

All 12 pairs had `date_pred dtype=object` with `pd.Timestamp` cell values.

### Fix

NOT a monkey-patch of upstream `freqai_interface.py` / `data_drawer.py`
(that would break container updates). Instead, two layers:

**Strategy hook** — `FreqAIMeanRevV1._normalize_historic_predictions_dtype(pair)`
called from `_populate_indicators_inner` BEFORE `self.freqai.start(...)`.
Idempotent (per-process per-pair latch in `_hp_dtype_normalized: set`).
Coerces `historic_predictions[pair]["date_pred"]` to `datetime64[ms,
UTC]` in-memory so the backtest merge sees matching dtypes. O(1) on
all subsequent calls. Safe in live mode (`pd.to_datetime(..., utc=True)`
is an identity transform on Timestamp values).

**On-disk migration** — `scripts/migrate_historic_predictions_dtype.py`
rewrites the existing saved store with the same coercion. Atomic
(backup + temp + replace). `--dry-run` supported.

Why both: the strategy hook fixes the dtype permanently going forward
even if a future restart cold-starts from a fresh store. The migration
script cleans the EXISTING store so the very first `populate_indicators`
call after `freqai.dd.load_historic_predictions_from_disk` sees a clean
dtype (the hook fires BEFORE the merge but only AFTER the load — having
a clean on-disk store avoids one transient round-trip).

### Files

- `user_data/strategies/FreqAIMeanRevV1.py:443-449` (new latch)
- `user_data/strategies/FreqAIMeanRevV1.py:1081-1170` (helper) + line 1175 (call)
- `scripts/migrate_historic_predictions_dtype.py` (new, +174 lines)

### Verification

**Migration run** (one-time, on disk):
```
python3 scripts/migrate_historic_predictions_dtype.py
INFO loaded 12 pair(s): ADA/USD, ATOM/USD, ..., XRP/USD
INFO   ADA/USD: coerced (object -> datetime64[ms, UTC])
... (12 pairs all coerced)
INFO backed up original to historic_predictions.pkl.bak-1778610603
INFO rewrote historic_predictions.pkl with normalized date_pred dtype
```

**Backtest, post-fix** (`docker exec freqtrade freqtrade backtesting --config
/freqtrade/user_data/config.backtest_blind.json --strategy FreqAIMeanRevV1
--freqaimodel TFTModel --freqai-backtest-live-models`):

- `grep -c "ValueError\|merge on datetime" /tmp/bt_after2.log` → **0** (was: hundreds)
- BACKTESTING REPORT:

```
  XRP/USD  0 trades  (quarantined - missing model)
  DOGE/USD 0 trades  (quarantined - missing model)
  DOT/USD  0 trades  (no MR-signal candles in window)
  ATOM/USD 0 trades
  LTC/USD  0 trades
  BCH/USD  0 trades
  ETH/USD  2 trades  -0.46%   -5.85 USD
  AVAX/USD 1 trade   -1.30%   -9.73 USD   (TFT-blind path)
  ADA/USD  1 trade   -1.80%  -15.89 USD
  LINK/USD 2 trades  -1.52%  -17.18 USD   (TFT-blind path)
  SOL/USD  4 trades  -0.56%  -35.63 USD
  BTC/USD  3 trades  -1.25%  -68.56 USD
  TOTAL   13 trades  -1.00% -152.84 USD   0% win, -0.80% drawdown
```

ENTER TAG STATS:
- `bb_oversold_revert` (TFT-present BB path): 6 trades, -0.84%
- `meta_up_regime` (TFT meta-agent path): 7 trades, -1.14%

The merge error is **GONE** and the strategy is generating + executing
entries. All-losses result is expected: 2.7-day backtest window covers
only the tail of available historic predictions, and the BollingerRSI
mean-reversion thesis takes weeks of data to play out. The fix is the
*ability to backtest at all*, not the P&L itself.

### Caveats found during verification

1. **Risk governor state pollution** — the live bot's
   `state/risk_governor_anchors.json` had `paused_for_drawdown: true`
   from the 18:18 live session (-171 USD daily loss vs 19k starting).
   First backtest run reused that anchor (drawdown ≥ 8% block on every
   pair, 0 trades). Workaround for verification:
   `RISK_GOVERNOR_ANCHORS_PATH=/tmp/risk_anchors_backtest.json` was set
   as an env var in the backtest container exec, giving the risk gov a
   fresh state. **Not part of this branch** — pre-existing issue. The
   operator may want a future fix (e.g. backtest config sets a different
   anchors path automatically).

2. **Pre-existing `risk_governor._pearson_returns` reindex bug** —
   `cannot reindex on an axis with duplicate labels` is logged on
   `confirm_trade_entry` for some pairs in the LIVE bot. Strategy
   gracefully fails-closed (blocks entry). Independent issue. Not on
   this branch.

---

## Issue 2 — 4 broken model.zip artifacts · RETRAIN TRIGGERED, IN PROGRESS

**Pairs**: DOGE/USD, XRP/USD, AVAX/USD, LINK/USD.

### Pre-state

```
DOGE/USD: trained_timestamp=0, model_filename=cb_doge_1778508850
XRP/USD : trained_timestamp=0, model_filename=cb_xrp_1778504376
AVAX/USD: trained_timestamp=0, model_filename=cb_avax_1778511060
LINK/USD: trained_timestamp=0, model_filename=cb_link_1778513855
```

All 4 had `trained_timestamp=0` already (set earlier today by a prior
agent / operator run). Stub `model.zip` files (789 bytes each, vs the
~92 MB / ~8k tensor_blobs of a healthy one like ATOM) still existed in
some sub-train folders.

### Command executed

```
docker exec freqtrade python3 /freqtrade/scripts/retrain_tft_pairs.py \
  --pairs DOGE/USD,XRP/USD,AVAX/USD,LINK/USD
```

Output:
```
INFO removed stub artifacts in /freqtrade/user_data/models/tft_v1/sub-train-AVAX_1778511060
INFO removed stub artifacts in /freqtrade/user_data/models/tft_v1/sub-train-LINK_1778513855
INFO backed up pair_dictionary.json -> pair_dictionary.json.bak-1778610838
INFO rewrote pair_dictionary.json with trained_timestamp=0 for 4 pair(s)
```

(DOGE/XRP older folders already had their stubs cleared; only AVAX/LINK
still had a 789-byte stub to remove.)

### GPU state at trigger

`nvidia-smi --query-compute-apps=process_name,pid,used_memory --format=csv`:

```
/usr/local/bin/python3.14 [freqtrade]   12763 MiB   <- TFT training (XRP)
/usr/local/bin/ollama (hermes3:8b)      5153 MiB
/usr/local/bin/ollama (hermes3:8b)     40600 MiB
```

Total ~58 GB of 128 GB unified — plenty of headroom for TFT's 38 GB cap.
`gpu_yield_now.sh` was NOT needed.

### Live freqtrade training queue (snapshot from log)

```
deque(['XRP/USD', 'DOGE/USD', 'AVAX/USD', 'LINK/USD', 'BCH/USD',
       'LTC/USD', 'ATOM/USD', 'DOT/USD', 'BTC/USD', 'ETH/USD',
       'SOL/USD', 'ADA/USD'])
```

XRP/USD is currently training. Latest log line:

```
2026-05-12 18:32:40 - TFTModel - INFO - epoch 4/50 loss=1.0615
  (ce=0.9992 q=0.2078) val_sharpe=0.629 lr=9.98e-04 step=2624
2026-05-12 18:16:02 - TFTModel - INFO - [XRP/USD] resuming from epoch 2
  (saved 0.1h ago, best_val_sharpe=0.322)
```

≈ 8 min/epoch on the Spark with batch=256/AMP. Historic early-stopping
on this strategy fires around epoch 6-10, so each pair should complete
in ~50-80 min. Estimated total wall-clock to clear all 4 pairs: **3–5 h**.
(Operator's original estimate of 30-60 min was per-pair, not total —
4 pairs sequential = 2-4 h normally; XRP appears to be lingering longer
because it's resumed from a checkpoint and hasn't hit the val_sharpe
plateau yet.)

### Verification commands (run AFTER training finishes)

```bash
# 1. Zip-size guard (must all be > 1 MB; healthy ones are 50-200 MB)
cd /home/saijayanthai/Documents/trading-bot
for p in DOGE XRP AVAX LINK; do
  latest=$(find user_data/models/tft_v1/sub-train-${p}_* -name "*model.zip" -printf "%T@ %p\n" 2>/dev/null \
           | sort -n | tail -1 | cut -d' ' -f2-)
  size=$(stat -c %s "$latest" 2>/dev/null || echo 0)
  echo "$p: $latest = $size bytes"
done

# 2. pair_dictionary timestamps (must all be > 0)
python3 -c "
import json
d = json.load(open('user_data/models/tft_v1/pair_dictionary.json'))
for p in ['DOGE/USD','XRP/USD','AVAX/USD','LINK/USD']:
    print(f'{p}: trained_timestamp={d[p][\"trained_timestamp\"]}')
"

# 3. Dashboard health (counts.missing should drop from 4 to 0)
curl -s http://localhost:8081/api/ops/training_health | python3 -m json.tool | head -20
```

### Risk: a retrain might produce ANOTHER stub

The 6-fix prior session installed a validation gate (`validate_model_zip`)
in `freqaimodels/tft_pickle.py` that **rejects** any new model.zip that:
- is < 1 MB, OR
- has no `data.pkl` member, OR
- has zero tensor blobs (`tensor*.bin`).

If a retrain produces a stub, that gate raises and freqai logs the
PicklingError loudly. Look for `[tft-pickle]` ERROR lines in
`user_data/logs/freqtrade.log`. The pair will stay quarantined and the
strategy will route it through the TFT-blind path (now default ON, see
Fix 3).

If you see ANY new stub after retraining: the bug is NOT in this branch
— it's a deeper issue in the validate gate or in IResolver re-exec
behaviour. Surface it loudly in the next HANDOFF — do not mark Issue 2
complete.

---

## Issue 3 — default flip + dashboard banner · DONE

### config.json diff

```json
"strategy_overrides": {
-   "_doc": "OPTIONAL strategy-level overrides. Default behaviour is unchanged ... Operator must opt in by flipping enabled to true.",
+   "_doc": "OPTIONAL strategy-level overrides. tft_blind_fallback: when TFT prediction columns are missing ...",
+   "_paper_default_doc": "PAPER MODE DEFAULT 2026-05-12+: tft_blind_fallback.enabled defaults to TRUE so quarantined pairs (DOGE/XRP/AVAX/LINK on 2026-05-12) keep trading on pure BollingerRSI MR signal at 50% size while their TFT models retrain. Before switching to LIVE: (1) backtest validates well? (2) risk acceptable on real money? (3) if performance is good, consider bumping position_size_multiplier toward 1.0 OR flip enabled to false to gate behind TFT only.",
    "tft_blind_fallback": {
-       "enabled": false,
+       "enabled": true,
        "position_size_multiplier": 0.5,
        "log_per_pair_once": true
    }
}
```

### Strategy header comment

Added a top-of-file block to `FreqAIMeanRevV1.py` explaining the
paper-mode-default and the pre-LIVE review checklist. JSON doesn't take
comments — the operator is supposed to grep here first.

### Dashboard banner copy

`user_data/dashboard/static/js/ops_spa.js`:

```diff
- "tft-blind fallback ON · " + active + " pair(s) trading on BollingerRSI MR at " + mult + "% size · auto-disables when TFT retrains clean"
+ "tft-blind fallback ON · " + active + " pair(s) trading on BollingerRSI MR at " + mult + "% size"
```

(The "auto-disables when TFT retrains clean" wording was misleading — the
flag is configurable, not auto-managed. Trimmed.)

```diff
- "tft-blind fallback OFF · " + eligible + " eligible pair(s) DARK · set strategy_overrides.tft_blind_fallback.enabled=true to trade them at " + mult + "% size"
+ "tft-blind fallback OFF · " + eligible + " eligible pair(s) DARK until next TFT retrain · flip strategy_overrides.tft_blind_fallback.enabled=true to trade them at " + mult + "% size"
```

Dark-chip tooltip also updated to reflect that the new default is `true`.

### Cache-bust

`?v=20260512-tft-blind-fallback` → `?v=20260512-permanent-fixes` across
`ops_spa.html` + `dashboard_spa.html` (all 8 css/js refs).

---

## Live impact — what to expect after merge + restart

After operator does **(a)** `git merge fix/permanent-train-and-backtest`
on main and **(b)** restarts freqtrade + dashboard:

1. **Backtest works.** `--freqai-backtest-live-models` no longer raises
   `ValueError: merge on datetime64...`. Strategy generates entries.
2. **DOGE/XRP/AVAX/LINK go from DARK → BLIND-trading.** The 4
   quarantined pairs are now eligible for BollingerRSI MR entries at
   50% size. The dashboard banner will read:
   *"tft-blind fallback ON · 4 pair(s) trading on BollingerRSI MR at 50% size"*
3. **Once retrains complete** (3–5 h after operator restart, see Issue 2
   above), each pair flips from `tft_blind_active=true` to
   `tft_blind_active=false` and the full TFT signal takes over.
   Dashboard `training_health.counts.missing` drops from 4 to 0.
4. **No new merge errors** — the historic_predictions saved store is
   permanently clean (migrated), and the strategy hook keeps it that
   way forever.
5. **Risk governor unchanged.** Live drawdown anchors still apply.
   Backtest needs `RISK_GOVERNOR_ANCHORS_PATH=/tmp/...` to get a fresh
   state (see Issue 1 caveats).

---

## NOT done / out of scope

- `risk_governor._pearson_returns` `cannot reindex on an axis with
  duplicate labels` bug — pre-existing, observed in live log. Strategy
  fail-closes on it (no entry), so it's not a trading-blocker.
- `risk_governor_anchors.json` pollution between backtest and live —
  pre-existing, worked around with env var.
- Auto-clearing the older sub-train folder references in pair_dictionary
  after retrain_tft_pairs.py runs — the script zeros the timestamp but
  leaves `model_filename` / `data_path` pointing at old (now-empty)
  folders. Freqai's `load_data` hits FileNotFoundError until the new
  training cycle writes fresh artifacts. The strategy's neutral-frame
  catch handles it gracefully, but it's noisy in the log. A future
  cleanup could clear those fields too, or set `model_filename = ""`
  so freqai's early-return short-circuits load_data.

---

## Operator checklist after restart

```bash
# 1. Confirm the config is applied
docker exec freqtrade python3 -c "
import json
c = json.load(open('/freqtrade/user_data/config.json'))
tbf = c['strategy_overrides']['tft_blind_fallback']
print('tft_blind_fallback.enabled =', tbf['enabled'])
print('position_size_multiplier  =', tbf['position_size_multiplier'])
"
# expected: enabled = True, multiplier = 0.5

# 2. Watch retrain progress
tail -F /home/saijayanthai/Documents/trading-bot/user_data/logs/freqtrade.log \
  | grep -E "epoch|early stopping|trained"

# 3. After retrain completes, re-run the verification block from Issue 2.

# 4. Re-run the backtest to confirm everything still works end-to-end:
docker exec -e RISK_GOVERNOR_ANCHORS_PATH=/tmp/risk_bt.json freqtrade \
  freqtrade backtesting \
  --config /freqtrade/user_data/config.backtest_blind.json \
  --strategy FreqAIMeanRevV1 --freqaimodel TFTModel \
  --freqai-backtest-live-models
# expected: no merge error, > 0 trades
```
