"""Tests for ``quanta_core.risk.ownership`` (port of stocks/tests/test_subsystem_ownership.py)."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from quanta_core.risk import ownership as so

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def isolated_state(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Redirect both state files into tmp_path so each test starts clean."""
    state_dir = tmp_path / "ownership-state"
    state_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("QUANTA_STATE_DIR", str(state_dir))
    return state_dir


# ---------------------------------------------------------------------------
# load_owned
# ---------------------------------------------------------------------------


class TestLoadOwned:
    def test_missing_file_returns_empty(self, isolated_state: Path) -> None:
        assert so.load_owned("shark") == set()
        assert so.load_owned("wheel") == set()

    def test_loads_saved_symbols(self, isolated_state: Path) -> None:
        so.save_owned("shark", ["NVDA", "AAPL"])
        assert so.load_owned("shark") == {"NVDA", "AAPL"}

    def test_uppercases_input(self, isolated_state: Path) -> None:
        so.save_owned("shark", ["nvda", "aapl"])
        assert so.load_owned("shark") == {"NVDA", "AAPL"}

    def test_corrupt_file_returns_empty(self, isolated_state: Path) -> None:
        path = so._state_path("shark")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{not json")
        assert so.load_owned("shark") == set()

    def test_unknown_subsystem_raises(self) -> None:
        with pytest.raises(ValueError):
            so._state_path("crypto")  # type: ignore[arg-type]

    def test_non_dict_json_returns_empty(self, isolated_state: Path) -> None:
        path = so._state_path("shark")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("[1, 2, 3]")  # valid JSON but not a dict
        assert so.load_owned("shark") == set()

    def test_non_string_symbols_are_filtered(self, isolated_state: Path) -> None:
        path = so._state_path("shark")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "symbols": ["AAPL", 42, "", None, "NVDA"],
                    "schema_version": 1,
                }
            )
        )
        assert so.load_owned("shark") == {"AAPL", "NVDA"}


# ---------------------------------------------------------------------------
# save_owned
# ---------------------------------------------------------------------------


class TestSaveOwned:
    def test_atomic_write_creates_file(self, isolated_state: Path) -> None:
        so.save_owned("shark", ["NVDA"])
        path = so._state_path("shark")
        assert path.exists()
        data = json.loads(path.read_text())
        assert data["symbols"] == ["NVDA"]
        assert data["schema_version"] == so.SCHEMA_VERSION
        assert "updated_at" in data

    def test_dedupes_input(self, isolated_state: Path) -> None:
        so.save_owned("shark", ["NVDA", "nvda", "NVDA", "AAPL"])
        assert so.load_owned("shark") == {"NVDA", "AAPL"}

    def test_writes_sorted_for_diff_stability(self, isolated_state: Path) -> None:
        so.save_owned("shark", ["NVDA", "AAPL", "MSFT"])
        data = json.loads(so._state_path("shark").read_text())
        assert data["symbols"] == ["AAPL", "MSFT", "NVDA"]

    def test_empty_iterable_is_valid(self, isolated_state: Path) -> None:
        so.save_owned("shark", [])
        assert so.load_owned("shark") == set()

    def test_atomic_no_partial_on_crash(
        self,
        isolated_state: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Simulate fsync failure — destination must not contain partial bytes."""
        so.save_owned("shark", ["AAPL"])
        path = so._state_path("shark")
        original_bytes = path.read_bytes()

        def boom(src: str, dst: str) -> None:
            raise OSError("simulated mid-write kill")

        monkeypatch.setattr(os, "replace", boom)
        with pytest.raises(OSError):
            so.save_owned("shark", ["NVDA"])

        # Original file must be untouched (no half-written state).
        assert path.read_bytes() == original_bytes

        # And no temp file should remain in the directory.
        leftover = [p for p in path.parent.iterdir() if p.name.startswith(f".{path.name}.")]
        assert not leftover, f"temp file leak: {leftover}"


# ---------------------------------------------------------------------------
# owns
# ---------------------------------------------------------------------------


class TestOwns:
    def test_basic_membership(self) -> None:
        assert so.owns({"NVDA", "AAPL"}, "NVDA")
        assert not so.owns({"NVDA"}, "AAPL")

    def test_case_insensitive(self) -> None:
        assert so.owns({"NVDA"}, "nvda")
        assert so.owns({"nvda"}, "NVDA")

    def test_empty_symbol_returns_false(self) -> None:
        assert not so.owns({"NVDA"}, "")


# ---------------------------------------------------------------------------
# claim
# ---------------------------------------------------------------------------


class TestClaim:
    def test_adds_symbol(self, isolated_state: Path) -> None:
        so.claim("shark", "NVDA")
        assert so.load_owned("shark") == {"NVDA"}

    def test_idempotent(self, isolated_state: Path) -> None:
        so.claim("shark", "NVDA")
        so.claim("shark", "NVDA")
        so.claim("shark", "nvda")
        assert so.load_owned("shark") == {"NVDA"}

    def test_subsystems_are_isolated(self, isolated_state: Path) -> None:
        so.claim("shark", "NVDA")
        so.claim("wheel", "AAPL")
        assert so.load_owned("shark") == {"NVDA"}
        assert so.load_owned("wheel") == {"AAPL"}

    def test_empty_symbol_is_noop(self, isolated_state: Path) -> None:
        so.claim("shark", "")
        assert so.load_owned("shark") == set()


# ---------------------------------------------------------------------------
# release
# ---------------------------------------------------------------------------


class TestRelease:
    def test_removes_symbol(self, isolated_state: Path) -> None:
        so.save_owned("shark", ["NVDA", "AAPL"])
        so.release("shark", "NVDA")
        assert so.load_owned("shark") == {"AAPL"}

    def test_idempotent_on_missing(self, isolated_state: Path) -> None:
        so.save_owned("shark", ["AAPL"])
        so.release("shark", "NVDA")
        assert so.load_owned("shark") == {"AAPL"}

    def test_case_insensitive(self, isolated_state: Path) -> None:
        so.save_owned("shark", ["NVDA"])
        so.release("shark", "nvda")
        assert so.load_owned("shark") == set()

    def test_empty_symbol_is_noop(self, isolated_state: Path) -> None:
        so.save_owned("shark", ["NVDA"])
        so.release("shark", "")
        assert so.load_owned("shark") == {"NVDA"}


# ---------------------------------------------------------------------------
# State-dir overrides
# ---------------------------------------------------------------------------


class TestStateDir:
    def test_default_dir_is_under_home(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("QUANTA_STATE_DIR", raising=False)
        assert so._state_dir().parts[-2:] == (".quanta", "state")

    def test_env_override_wins(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        monkeypatch.setenv("QUANTA_STATE_DIR", str(tmp_path))
        assert so._state_dir() == tmp_path
        assert so._state_path("shark") == tmp_path / "owned_symbols-shark.json"
