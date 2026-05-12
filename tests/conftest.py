"""
Shared pytest fixtures and isolation hooks.

Two things this file does:

1. Provides the ``tmp_user_data`` fixture that ``test_dashboard.py``
   expected to exist but never had. The fixture stamps a minimal
   user_data tree under pytest's tmp_path so tests can swap in
   ``USER_DATA_ROOT`` without touching the operator's live tree.

2. Auto-isolates the ``RiskGovernor`` anchor file for every test by
   pointing ``RISK_GOVERNOR_ANCHORS_PATH`` at a per-test tmp path.
   Without this, the governor restores the operator's live drawdown-
   pause flag and every test in ``test_risk_execution.py`` fails with
   ``max_drawdown_paused`` regardless of its own setup. See AUDIT
   2026-05-12 High #9.
"""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
USER_DATA_SRC = REPO_ROOT / "user_data"


@pytest.fixture
def tmp_user_data(tmp_path: Path) -> Path:
    """Create a minimal user_data tree under tmp_path.

    Copies just the small immutable files the dashboard tests need
    (config.json, modules/, dashboard/) and creates empty
    ``data/``, ``logs/``, ``state/`` dirs. Skipped if the source tree
    is missing.
    """
    if not USER_DATA_SRC.is_dir():
        pytest.skip("user_data/ not available in this checkout")
    dst = tmp_path / "user_data"
    dst.mkdir()
    # Symlink the static parts the dashboard imports rather than copy
    # — keeps the fixture fast and avoids stale duplicates.
    for sub in ("dashboard", "modules", "strategies"):
        src = USER_DATA_SRC / sub
        if src.is_dir():
            os.symlink(src, dst / sub)
    # Real-copy config.json so tests can mutate it without affecting
    # the operator's live config.
    cfg_src = USER_DATA_SRC / "config.json"
    if cfg_src.is_file():
        shutil.copy2(cfg_src, dst / "config.json")
    # Stub the runtime dirs the dashboard creates lazily.
    for sub in ("data", "logs", "state", "models", "snapshots"):
        (dst / sub).mkdir(exist_ok=True)
    # Minimal evolution log with the synthetic champion that
    # ``test_dashboard.test_http_endpoints`` asserts on. Schema mirrors
    # what ``_make_temp_user_data`` in test_dashboard.py constructed
    # before this fixture replaced it.
    (dst / "logs" / "evolution.json").write_text(json.dumps([{
        "generation": 4,
        "champion": "gen4-c00",
        "champion_id": "gen4-c00",
        "runner_up": "gen4-c01",
        "alive": [{"member_id": "gen4-c00", "fitness": 1.42}],
    }]))
    return dst


@pytest.fixture(autouse=True)
def _isolate_risk_governor_state(tmp_path_factory, monkeypatch):
    """Auto-isolate the RiskGovernor anchor file per test.

    The default anchor path is ``user_data/state/risk_governor_anchors.json``
    which is the operator's LIVE drawdown-pause state. Without this hook,
    every test that constructs a RiskGovernor inherits "paused_for_drawdown:
    True" from the running bot and fails with ``max_drawdown_paused``.

    Tests that need to test the load-from-disk path should override
    ``RISK_GOVERNOR_ANCHORS_PATH`` themselves.
    """
    iso_root = tmp_path_factory.mktemp("risk_governor_state")
    anchor_file = iso_root / "anchors.json"
    monkeypatch.setenv("RISK_GOVERNOR_ANCHORS_PATH", str(anchor_file))
    yield
