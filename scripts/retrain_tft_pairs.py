#!/usr/bin/env python3
"""
Selectively re-trigger TFT retraining for specific pairs.

How freqai's retrain trigger works (see freqai/data_kitchen.py
check_if_new_training_required): when ``trained_timestamp == 0`` in
pair_dictionary.json, freqai schedules a fresh retrain on the next
inference cycle for that pair. So the cleanest way to force a per-pair
retrain is to zero out the timestamp in pair_dictionary.json AND remove
the stale (likely stub) model.zip + its sidecar files so the next train
writes a clean folder rather than mixing artifacts.

Usage::

    python3 scripts/retrain_tft_pairs.py --pairs DOGE/USD,XRP/USD,AVAX/USD,LINK/USD
    python3 scripts/retrain_tft_pairs.py --only-stubs       # auto-target validated stubs
    python3 scripts/retrain_tft_pairs.py --only-stubs --dry-run

Constraints:
  - DOES NOT restart freqtrade. The operator restarts the container when ready;
    freqai picks up trained_timestamp == 0 on its next live_retrain_hours
    sweep (typically <= 5 minutes after a restart since the first inference
    cycle triggers the check).
  - Backs up the original pair_dictionary.json to .bak-{ts} before
    rewriting so a mistake is reversible.
  - Refuses to operate on pairs not present in pair_dictionary.json.

This script is the future "Retrain" dashboard button's backend. The
dashboard endpoint can POST {"pairs": [...]} to a thin wrapper that
shells out to this script under the mcp_key auth gate.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import shutil
import sys
import time
from pathlib import Path
from typing import Iterable

# Add user_data to sys.path so we can import the quarantine scanner.
_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("retrain_tft_pairs")


def _resolve_models_root() -> Path:
    """Return the host-side path to user_data/models. Honour USER_DATA_ROOT
    so the same script works from inside the freqtrade container or from
    the host."""
    env = os.environ.get("USER_DATA_ROOT")
    if env:
        return Path(env) / "models"
    return _REPO_ROOT / "user_data" / "models"


def _backup_pair_dict(pd_path: Path) -> Path:
    ts = int(time.time())
    bak = pd_path.with_suffix(f".json.bak-{ts}")
    shutil.copy2(pd_path, bak)
    log.info("backed up pair_dictionary.json -> %s", bak.name)
    return bak


def _container_to_host(path_str: str, models_root: Path) -> Path:
    """data_path in pair_dictionary uses the in-container layout
    (/freqtrade/user_data/models/...). Re-anchor to the host-side root."""
    prefix = "/freqtrade/user_data/models/"
    if path_str.startswith(prefix):
        return models_root / path_str[len(prefix):]
    return Path(path_str)


def collect_stub_pairs(identifier: str = "tft_v1") -> list[str]:
    """Use the quarantine scanner to find pairs that need a retrain."""
    # Lazy import so this script stays usable even before user_data/ is on
    # the path under cron. The scanner module lives next to TFTModel.py.
    sys.path.insert(0, str(_REPO_ROOT / "user_data"))
    from freqaimodels import tft_pickle as _tft_save  # noqa: N813

    scan = _tft_save.scan_pair_dictionary_for_quarantine(identifier)
    return sorted(p for p, info in scan.items() if info["status"] != "ok")


def reset_pairs(
    pairs: Iterable[str],
    identifier: str = "tft_v1",
    dry_run: bool = False,
    remove_stub_artifacts: bool = True,
) -> dict[str, dict]:
    """Zero out trained_timestamp for each pair and remove the stub artifact
    directory contents (preserving the folder so freqai can write into it)."""
    models_root = _resolve_models_root()
    pd_path = models_root / identifier / "pair_dictionary.json"
    if not pd_path.exists():
        raise SystemExit(f"pair_dictionary.json not found at {pd_path}")

    with pd_path.open("r") as fp:
        entries = json.load(fp)

    pairs = [p.strip() for p in pairs if p.strip()]
    unknown = [p for p in pairs if p not in entries]
    if unknown:
        raise SystemExit(
            f"unknown pair(s): {unknown}. Known: {sorted(entries.keys())}"
        )

    result: dict[str, dict] = {}
    for pair in pairs:
        entry = entries[pair]
        prior_ts = int(entry.get("trained_timestamp", 0) or 0)
        data_path = _container_to_host(str(entry.get("data_path", "")), models_root)
        model_filename = entry.get("model_filename", "")
        zip_path = data_path / f"{model_filename}_model.zip" if model_filename else None

        action: dict = {
            "prior_trained_timestamp": prior_ts,
            "data_path": str(data_path),
            "zip_path": str(zip_path) if zip_path else None,
            "stub_removed": False,
        }

        if dry_run:
            action["new_trained_timestamp"] = 0
            action["dry_run"] = True
            result[pair] = action
            log.info("[dry-run] would reset %s: ts=%d -> 0", pair, prior_ts)
            continue

        # Reset the in-memory entry. We write the whole file back at the
        # end so a single atomic rename promotes all changes.
        #
        # Bug 3 (2026-05-12): zeroing only ``trained_timestamp`` left
        # ``model_filename`` and ``data_path`` pointing at the now-deleted
        # stub folder. Between this script running and freqai finishing
        # the new train cycle, freqai's load_data() tries to read the
        # stale path → FileNotFoundError → the strategy's broad except
        # in populate_entry_trend masks it but the log becomes noisy
        # (one ERROR per pair per candle until retrain completes).
        #
        # Root-cause fix: also clear model_filename + data_path to match
        # freqai's own ``empty_pair_dict`` shape from data_drawer.py:
        #   {"model_filename": "", "trained_timestamp": 0,
        #    "data_path": "", "extras": {}}
        # freqai's get_pair_dict_info() treats this as "first ever train"
        # and skips the load path entirely.
        entry["trained_timestamp"] = 0
        entry["model_filename"] = ""
        entry["data_path"] = ""

        # Remove the stub zip + sidecars so the next training cycle writes
        # into a clean folder. We resolved data_path / zip_path BEFORE
        # clearing the entry above so this cleanup still works.
        if remove_stub_artifacts and zip_path and zip_path.exists():
            try:
                # Only remove sub-train-* folder contents, never the folder itself.
                # freqai writes new files into the same folder on retrain.
                for f in data_path.iterdir():
                    if f.is_file():
                        f.unlink()
                action["stub_removed"] = True
                log.info("removed stub artifacts in %s", data_path)
            except Exception as exc:  # noqa: BLE001
                log.warning("could not clean %s: %s", data_path, exc)

        action["new_trained_timestamp"] = 0
        action["model_filename_cleared"] = True
        action["data_path_cleared"] = True
        result[pair] = action

    if not dry_run:
        _backup_pair_dict(pd_path)
        tmp = pd_path.with_suffix(".json.tmp")
        with tmp.open("w") as fp:
            json.dump(entries, fp)
        tmp.replace(pd_path)
        log.info("rewrote %s with trained_timestamp=0 for %d pair(s)",
                 pd_path.name, len(pairs))

    return result


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Force a per-pair TFT retrain by zeroing trained_timestamp."
    )
    ap.add_argument(
        "--pairs",
        help="comma-separated pair list (e.g. DOGE/USD,XRP/USD). "
        "Mutually exclusive with --only-stubs.",
    )
    ap.add_argument(
        "--only-stubs",
        action="store_true",
        help="auto-target every pair currently flagged by the quarantine scanner.",
    )
    ap.add_argument(
        "--identifier",
        default="tft_v1",
        help="freqai identifier (default: tft_v1)",
    )
    ap.add_argument(
        "--dry-run",
        action="store_true",
        help="print intended changes without modifying anything",
    )
    ap.add_argument(
        "--keep-artifacts",
        action="store_true",
        help="don't delete the existing model.zip + sidecars in the sub-train folder",
    )
    args = ap.parse_args()

    if not args.pairs and not args.only_stubs:
        ap.error("supply --pairs or --only-stubs")
    if args.pairs and args.only_stubs:
        ap.error("--pairs and --only-stubs are mutually exclusive")

    if args.only_stubs:
        pairs = collect_stub_pairs(args.identifier)
        if not pairs:
            log.info("no quarantined pairs found — nothing to do.")
            return 0
        log.info("targeting %d quarantined pair(s): %s", len(pairs), ", ".join(pairs))
    else:
        pairs = [p.strip() for p in args.pairs.split(",") if p.strip()]

    result = reset_pairs(
        pairs,
        identifier=args.identifier,
        dry_run=args.dry_run,
        remove_stub_artifacts=not args.keep_artifacts,
    )

    print(json.dumps(result, indent=2, sort_keys=True))
    log.info(
        "DONE — freqtrade will pick up the retrain on its next live_retrain_hours "
        "sweep. If freqtrade is currently running, you may want to restart it "
        "so the retrain starts within minutes instead of hours."
    )
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
