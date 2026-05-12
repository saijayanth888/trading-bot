"""Shared pytest fixtures for ``quanta_core.risk`` tests.

Mirrors the isolation pattern from the legacy tests/conftest.py:
every test gets a fresh, temporary anchor path so the on-disk state
files for one test cannot bleed into another.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def isolated_risk_anchor(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[Path]:
    """Point ``RISK_GOVERNOR_ANCHORS_PATH`` at a per-test tmp file."""
    anchor = tmp_path / "risk_governor_anchors.json"
    monkeypatch.setenv("RISK_GOVERNOR_ANCHORS_PATH", str(anchor))
    return anchor


@pytest.fixture(autouse=True)
def isolated_quanta_state_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[Path]:
    """Redirect ``QUANTA_STATE_DIR`` so ownership state files don't leak."""
    state_dir = tmp_path / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("QUANTA_STATE_DIR", str(state_dir))
    return state_dir
