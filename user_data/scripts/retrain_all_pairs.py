#!/usr/bin/env python3
"""
Force-retrain all FreqAI/TFT pairs.

WHAT IT DOES
------------
Sets ``trained_timestamp = 0`` for every pair in
``user_data/models/<identifier>/pair_dictionary.json`` so that on the next time
freqtrade's freqai retrain-scan thread reads the drawer from disk, every pair
fails ``check_if_new_training_required`` (elapsed_time > live_retrain_hours)
and FreqAI schedules a retrain.

WHEN THE RETRAIN ACTUALLY FIRES
-------------------------------
``pair_dictionary.json`` is loaded into ``self.dd.pair_dict`` *once* at
freqtrade startup (``data_drawer.py::load_drawer_from_disk``). Mutating the
file from outside the process therefore takes effect at the next freqtrade
reload/restart, not instantly. This is intentional: the script is part of a
"prepare for restart" pipeline where the coordinator restarts freqtrade after
merging configuration changes, and the bumped ``n_epochs`` only takes effect
on that restart anyway.

If you do want an in-process trigger, hit the freqtrade REST API:
``POST /api/v1/reload_config`` — this re-instantiates FreqtradeBot in the same
worker loop without a container restart. The freshly-reconfigured bot then
reads pair_dictionary.json (with timestamps zeroed by this script) and starts
the retrain cycle. The current trading-bot operator preference is to leave
that call to the coordinator.

USAGE
-----
Run on the HOST (path resolved relative to repo root) or inside the freqtrade
container (path is symmetric via the bind-mount):

    # Host:
    python3 user_data/scripts/retrain_all_pairs.py

    # Container:
    docker exec freqtrade python /freqtrade/user_data/scripts/retrain_all_pairs.py

The script is idempotent and safe to run multiple times.
"""
from __future__ import annotations

import json
import os
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path


# ---- path resolution -------------------------------------------------------
def _resolve_identifier_dir() -> Path:
    """Find ``user_data/models/<identifier>/`` from either host or container.

    The identifier subdir (``tft_v1`` by default, overridable via
    ``$FREQAI_IDENTIFIER``) must exist — bare ``user_data/models`` empty
    placeholders (e.g. in git-worktree checkouts) are skipped.

    Resolution order:
      1. ``$FREQAI_MODELS_DIR`` override (absolute path to models dir)
      2. ``/freqtrade/user_data/models`` (inside container)
      3. ``<repo>/user_data/models`` two levels up from this file
      4. ``$HOME/Documents/trading-bot/user_data/models`` (operator default)

    AUDIT 2026-05-12 Critical #1: the third entry previously hardcoded
    one operator's home path; replaced with a $HOME-relative resolver.
    """
    ident = os.environ.get("FREQAI_IDENTIFIER", "tft_v1")

    candidates: list[Path] = []
    if env_path := os.environ.get("FREQAI_MODELS_DIR"):
        candidates.append(Path(env_path))
    candidates.extend([
        Path("/freqtrade/user_data/models"),
        Path(__file__).resolve().parents[2] / "user_data" / "models",
        Path(os.environ.get("HOME", "/root")) / "Documents" / "trading-bot" / "user_data" / "models",
    ])

    tried: list[Path] = []
    for c in candidates:
        target = c / ident
        tried.append(target)
        if target.is_dir():
            return target
    raise SystemExit(
        f"identifier dir {ident!r} not found in any candidate. tried: {tried}"
    )


# ---- core ------------------------------------------------------------------
def zero_pair_timestamps(pair_dict_path: Path) -> tuple[dict, dict]:
    """Read pair_dictionary.json, return (original, updated) with ts=0 for all pairs."""
    with pair_dict_path.open("r") as fp:
        original = json.load(fp)

    updated = json.loads(json.dumps(original))  # deep copy
    for pair, info in updated.items():
        if isinstance(info, dict) and "trained_timestamp" in info:
            info["trained_timestamp"] = 0

    return original, updated


def main() -> int:
    identifier_dir = _resolve_identifier_dir()
    pair_dict_path = identifier_dir / "pair_dictionary.json"

    if not pair_dict_path.is_file():
        print(f"ERROR: {pair_dict_path} not found", file=sys.stderr)
        return 1

    original, updated = zero_pair_timestamps(pair_dict_path)

    # back up the original (timestamped) so we can audit / roll back
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup_path = pair_dict_path.with_suffix(f".json.bak-{ts}")
    shutil.copy2(pair_dict_path, backup_path)

    # atomic write
    tmp_path = pair_dict_path.with_suffix(".json.tmp")
    with tmp_path.open("w") as fp:
        json.dump(updated, fp, indent=None, separators=(",", ":"))
    os.replace(tmp_path, pair_dict_path)

    pairs = list(updated.keys())
    print(f"[retrain_all_pairs] zeroed trained_timestamp for {len(pairs)} pairs:")
    for p in pairs:
        orig_ts = original.get(p, {}).get("trained_timestamp")
        print(f"  {p:<12s}  trained_timestamp: {orig_ts} -> 0")
    print(f"[retrain_all_pairs] backup written: {backup_path}")
    print(
        "[retrain_all_pairs] retrain will fire on next freqtrade reload/restart. "
        "POST /api/v1/reload_config inside the container to trigger without "
        "container restart, or wait for coordinator-driven restart."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
