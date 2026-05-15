#!/usr/bin/env python3
"""
One-time on-disk migration for ``historic_predictions.pkl`` to fix the
freqai-backtest-live-models dtype-merge bug.

SECURITY NOTE
=============
This script loads ``historic_predictions.pkl`` via cloudpickle. The pkl is
TRUSTED — it is produced by our own freqtrade container (data_drawer.py)
and lives inside our own ``user_data/models/`` tree. It is never sourced
from outside the project. cloudpickle is the canonical (and only) loader
for this file format because freqai itself uses cloudpickle to write it
(see freqai/data_drawer.py::save_historic_predictions_to_disk). JSON is
not an option — the payload is a dict of DataFrames with Timestamp values.

ROOT CAUSE
==========
freqai's ``data_drawer.append_model_predictions`` builds each per-candle
append row from ``np.zeros((1, len(columns)))`` then concats. The zeros
frame is all float64; when concatenated with the per-pair frame whose
``date_pred`` column was originally ``datetime64[ns, UTC]`` (set by
``set_initial_historic_predictions``), pandas demotes the merged column to
``object`` dtype because the values are now heterogenous (Timestamp + 0.0).
Subsequent ``df.iloc[-1, date_pred_loc] = strat_df.iloc[-1, date_loc]``
writes leave the column as object-dtype-with-Timestamps forever.

That object dtype lands in ``historic_predictions.pkl``. Live mode (which
uses ``start_inferencing``) tolerates it — freqai funnels date_pred through
``pd.to_datetime`` again inside ``set_initial_historic_predictions``. But
``--freqai-backtest-live-models`` merges the saved frame DIRECTLY against the
incoming candle dataframe (datetime64[ms, UTC] from feather) and pandas
raises:

    ValueError: You are trying to merge on datetime64[ms, UTC] and object
    columns for key 'date'. If you wish to proceed you should use pd.concat

Result: every per-candle ``populate_indicators`` call raises; the strategy's
fail-neutral handler emits zero entries; the backtest produces 0 trades for
every pair in the entire window.

THIS SCRIPT
===========
Rewrites the on-disk pkl with ``date_pred`` coerced to ``datetime64[ms,
UTC]`` for every pair. Idempotent — safe to run repeatedly. Atomic — backs
up the original, writes a temp file, replaces. The strategy hook
(``FreqAIMeanRevV1._normalize_historic_predictions_dtype``) does the same
fix in-memory at runtime, but having a clean on-disk pkl avoids one bad
merge attempt on the first ``populate_indicators`` call of every backtest
process (the hook fires before the merge, but only after the load).

USAGE
=====
::

    python3 scripts/migrate_historic_predictions_dtype.py
    python3 scripts/migrate_historic_predictions_dtype.py --dry-run
    python3 scripts/migrate_historic_predictions_dtype.py \
        --pkl user_data/models/tft_v1/historic_predictions.pkl

Honours ``USER_DATA_ROOT`` so it can run from inside or outside the
freqtrade container.
"""

from __future__ import annotations

import argparse
import logging
import os
import shutil
import sys
import time
from pathlib import Path

import cloudpickle
import pandas as pd

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("migrate_hp_dtype")


def _default_pkl_path() -> Path:
    env = os.environ.get("USER_DATA_ROOT")
    if env:
        return Path(env) / "models" / "tft_v1" / "historic_predictions.pkl"
    repo = Path(__file__).resolve().parent.parent
    return repo / "user_data" / "models" / "tft_v1" / "historic_predictions.pkl"


TARGET_DTYPE = "datetime64[ms, UTC]"


def coerce_in_place(data: dict) -> dict:
    """Return a {pair: action} dict describing the change applied."""
    actions: dict[str, dict] = {}
    for pair, df in data.items():
        info: dict = {"pair": pair}
        if "date_pred" not in df.columns:
            info["status"] = "no_date_pred_column"
            actions[pair] = info
            continue
        col = df["date_pred"]
        prev_dtype = str(col.dtype)
        info["prev_dtype"] = prev_dtype
        if prev_dtype == TARGET_DTYPE:
            info["status"] = "already_ok"
            actions[pair] = info
            continue
        try:
            coerced = pd.to_datetime(col, utc=True, errors="coerce")
            df["date_pred"] = coerced.astype(TARGET_DTYPE)
            info["status"] = "coerced"
            info["new_dtype"] = TARGET_DTYPE
        except Exception as exc:  # noqa: BLE001
            info["status"] = "error"
            info["error"] = str(exc)
        actions[pair] = info
    return actions


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    ap.add_argument("--pkl", help="path to historic_predictions.pkl")
    ap.add_argument(
        "--dry-run", action="store_true",
        help="report intended changes without writing",
    )
    args = ap.parse_args()

    pkl = Path(args.pkl) if args.pkl else _default_pkl_path()
    if not pkl.exists():
        log.error("pkl not found: %s", pkl)
        return 2

    log.info("loading %s", pkl)
    # SECURITY: trusted internal file — see module docstring.
    with pkl.open("rb") as fp:
        data = cloudpickle.load(fp)
    if not isinstance(data, dict):
        log.error("unexpected pkl payload type: %s (expected dict)", type(data))
        return 2
    log.info("loaded %d pair(s): %s", len(data), ", ".join(sorted(data.keys())))

    actions = coerce_in_place(data)
    needs_write = any(a.get("status") == "coerced" for a in actions.values())
    for pair, info in sorted(actions.items()):
        log.info("  %s: %s%s", pair, info.get("status"),
                 (f" ({info['prev_dtype']} -> {info.get('new_dtype', '-')})"
                  if info.get("status") == "coerced" else ""))

    if args.dry_run:
        log.info("[dry-run] would %s pkl on disk", "REWRITE" if needs_write else "SKIP")
        return 0
    if not needs_write:
        log.info("nothing to write — pkl already clean.")
        return 0

    ts = int(time.time())
    backup = pkl.with_suffix(f".pkl.bak-{ts}")
    shutil.copy2(pkl, backup)
    log.info("backed up original to %s", backup.name)

    tmp = pkl.with_suffix(".pkl.tmp")
    with tmp.open("wb") as fp:
        cloudpickle.dump(data, fp, protocol=cloudpickle.DEFAULT_PROTOCOL)
    tmp.replace(pkl)
    log.info("rewrote %s with normalized date_pred dtype", pkl.name)
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
