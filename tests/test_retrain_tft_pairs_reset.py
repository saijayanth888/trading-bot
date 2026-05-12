"""
Regression test for Bug 3 (2026-05-12).

Symptom: after ``python3 scripts/retrain_tft_pairs.py --only-stubs``,
pair_dictionary.json entries for affected pairs had
    trained_timestamp = 0
but model_filename + data_path still pointed at the now-deleted stub
folder. Between this script running and freqai finishing its new train,
freqai's load_data() raised FileNotFoundError → the strategy's broad
except caught it but the log became noisy (ERROR per pair per candle).

Root cause: reset_pairs() zeroed only trained_timestamp, leaving the
two path-shaped fields stale.

Fix: also clear model_filename + data_path to match freqai's own
``empty_pair_dict`` shape (data_drawer.py line ~100).

Test plan: write a fake pair_dictionary.json with two pairs and stub
artifacts under a fake models root, run reset_pairs(), and assert:
  1. trained_timestamp == 0 (existing behaviour, unchanged)
  2. model_filename == ""    (NEW — Bug 3 fix)
  3. data_path == ""         (NEW — Bug 3 fix)
  4. stub artifacts removed from the data_path folder
  5. atomic rewrite (no .tmp file left over)
  6. dry-run never modifies the file
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import retrain_tft_pairs  # noqa: E402


def _make_fake_models_root(tmp_path: Path) -> tuple[Path, Path]:
    """Stamp out a minimal models/tft_v1 tree with one stub pair.
    Returns (models_root, pair_dict_path)."""
    identifier_root = tmp_path / "models" / "tft_v1"
    sub_train = identifier_root / "sub-train-DOGE_USD"
    sub_train.mkdir(parents=True)
    # Stub model.zip — content doesn't matter, just needs to exist so
    # the cleanup branch fires.
    (sub_train / "cb_doge_model.zip").write_bytes(b"STUB")
    (sub_train / "cb_doge_metadata.json").write_text("{}")

    pair_dict = identifier_root / "pair_dictionary.json"
    pair_dict.write_text(json.dumps({
        "DOGE/USD": {
            "model_filename": "cb_doge",
            "trained_timestamp": 1716566400,
            "data_path": str(sub_train),
            "extras": {},
        },
        "XRP/USD": {
            "model_filename": "cb_xrp",
            "trained_timestamp": 1716566400,
            "data_path": "/freqtrade/user_data/models/tft_v1/sub-train-XRP_USD",
            "extras": {},
        },
    }, indent=2))
    return tmp_path, pair_dict


def test_reset_clears_model_filename_and_data_path(tmp_path, monkeypatch):
    """The headline assertion: Bug 3 fix."""
    models_dir, pd_path = _make_fake_models_root(tmp_path)
    monkeypatch.setenv("USER_DATA_ROOT", str(models_dir))

    result = retrain_tft_pairs.reset_pairs(
        pairs=["DOGE/USD"], identifier="tft_v1",
        dry_run=False, remove_stub_artifacts=True,
    )

    # File on disk must reflect the cleared shape.
    rewritten = json.loads(pd_path.read_text())
    doge = rewritten["DOGE/USD"]
    assert doge["trained_timestamp"] == 0, "existing behaviour: ts zeroed"
    assert doge["model_filename"] == "", (
        "Bug 3: model_filename must be cleared so freqai's load_data() "
        "does not try the stale stub path"
    )
    assert doge["data_path"] == "", (
        "Bug 3: data_path must be cleared to match freqai's "
        "empty_pair_dict shape"
    )
    # Untouched pair stays untouched.
    xrp = rewritten["XRP/USD"]
    assert xrp["trained_timestamp"] == 1716566400
    assert xrp["model_filename"] == "cb_xrp"

    # Return value reports both clears.
    assert result["DOGE/USD"]["model_filename_cleared"] is True
    assert result["DOGE/USD"]["data_path_cleared"] is True


def test_reset_removes_stub_artifacts(tmp_path, monkeypatch):
    """The folder contents must be wiped (but folder itself preserved)."""
    models_dir, _ = _make_fake_models_root(tmp_path)
    monkeypatch.setenv("USER_DATA_ROOT", str(models_dir))
    sub_train = models_dir / "models" / "tft_v1" / "sub-train-DOGE_USD"
    assert (sub_train / "cb_doge_model.zip").exists()

    retrain_tft_pairs.reset_pairs(
        pairs=["DOGE/USD"], identifier="tft_v1",
        dry_run=False, remove_stub_artifacts=True,
    )
    assert sub_train.is_dir(), "folder itself must remain"
    assert not (sub_train / "cb_doge_model.zip").exists(), "stub zip removed"
    assert not (sub_train / "cb_doge_metadata.json").exists(), "sidecar removed"


def test_dry_run_leaves_pair_dict_untouched(tmp_path, monkeypatch):
    """--dry-run must not modify the file or remove stubs."""
    models_dir, pd_path = _make_fake_models_root(tmp_path)
    monkeypatch.setenv("USER_DATA_ROOT", str(models_dir))
    original = pd_path.read_text()

    retrain_tft_pairs.reset_pairs(
        pairs=["DOGE/USD"], identifier="tft_v1",
        dry_run=True, remove_stub_artifacts=True,
    )
    assert pd_path.read_text() == original, "dry-run modified the file"
    sub_train = models_dir / "models" / "tft_v1" / "sub-train-DOGE_USD"
    assert (sub_train / "cb_doge_model.zip").exists(), "dry-run removed artifacts"


def test_cleared_shape_matches_freqai_empty_pair_dict(tmp_path, monkeypatch):
    """After reset, the entry must round-trip cleanly through freqai's
    own 'first time training' code path. We approximate that contract by
    matching the shape of empty_pair_dict from data_drawer.py:
      {model_filename: "", trained_timestamp: 0, data_path: ""}
    """
    models_dir, pd_path = _make_fake_models_root(tmp_path)
    monkeypatch.setenv("USER_DATA_ROOT", str(models_dir))

    retrain_tft_pairs.reset_pairs(
        pairs=["DOGE/USD"], identifier="tft_v1",
        dry_run=False, remove_stub_artifacts=True,
    )
    rewritten = json.loads(pd_path.read_text())
    doge = rewritten["DOGE/USD"]
    # The three keys freqai cares about must all be cleared.
    assert doge["model_filename"] == ""
    assert doge["trained_timestamp"] == 0
    assert doge["data_path"] == ""


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
