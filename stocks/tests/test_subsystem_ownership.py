"""Tests for shared.subsystem_ownership — Shark/Wheel position isolation."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from shared import subsystem_ownership as so


@pytest.fixture
def isolated_state(monkeypatch, tmp_path):
    """Redirect both state files into a tmp dir so each test starts clean."""
    shark_dir = tmp_path / "shark" / "state"
    wheel_dir = tmp_path / "wheel" / "state"
    shark_dir.mkdir(parents=True)
    wheel_dir.mkdir(parents=True)

    def fake_state_path(subsystem: str) -> Path:
        if subsystem == "shark":
            return shark_dir / "owned_symbols.json"
        if subsystem == "wheel":
            return wheel_dir / "owned_symbols.json"
        raise ValueError(subsystem)

    monkeypatch.setattr(so, "_state_path", fake_state_path)
    return tmp_path


class TestLoadOwned:
    def test_missing_file_returns_empty(self, isolated_state):
        assert so.load_owned("shark") == set()
        assert so.load_owned("wheel") == set()

    def test_loads_saved_symbols(self, isolated_state):
        so.save_owned("shark", ["NVDA", "AAPL"])
        assert so.load_owned("shark") == {"NVDA", "AAPL"}

    def test_uppercases_input(self, isolated_state):
        so.save_owned("shark", ["nvda", "aapl"])
        assert so.load_owned("shark") == {"NVDA", "AAPL"}

    def test_corrupt_file_returns_empty(self, isolated_state):
        path = so._state_path("shark")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{not json")
        assert so.load_owned("shark") == set()

    def test_unknown_subsystem_raises(self):
        with pytest.raises(ValueError):
            so._state_path("crypto")


class TestSaveOwned:
    def test_atomic_write_creates_file(self, isolated_state):
        so.save_owned("shark", ["NVDA"])
        path = so._state_path("shark")
        assert path.exists()
        data = json.loads(path.read_text())
        assert data["symbols"] == ["NVDA"]
        assert data["schema_version"] == so.SCHEMA_VERSION
        assert "updated_at" in data

    def test_dedupes_input(self, isolated_state):
        so.save_owned("shark", ["NVDA", "nvda", "NVDA", "AAPL"])
        assert so.load_owned("shark") == {"NVDA", "AAPL"}

    def test_writes_sorted_for_diff_stability(self, isolated_state):
        so.save_owned("shark", ["NVDA", "AAPL", "MSFT"])
        data = json.loads(so._state_path("shark").read_text())
        assert data["symbols"] == ["AAPL", "MSFT", "NVDA"]

    def test_empty_iterable_is_valid(self, isolated_state):
        so.save_owned("shark", [])
        assert so.load_owned("shark") == set()

    def test_atomic_no_partial_on_crash(self, isolated_state, monkeypatch):
        """Simulate fsync failure — destination must not contain partial bytes."""
        so.save_owned("shark", ["AAPL"])  # establish a baseline
        path = so._state_path("shark")
        original_bytes = path.read_bytes()

        # Inject a failure after the temp file is written but before replace
        real_replace = os.replace

        def boom(src, dst):
            # Simulate kill mid-write: do nothing, raise as if interrupted
            raise OSError("simulated mid-write kill")

        monkeypatch.setattr(os, "replace", boom)
        with pytest.raises(OSError):
            so.save_owned("shark", ["NVDA"])
        monkeypatch.setattr(os, "replace", real_replace)

        # Original file must be untouched (no half-written state)
        assert path.read_bytes() == original_bytes

        # And no temp file should remain in the directory
        leftover = [p for p in path.parent.iterdir() if p.name.startswith(f".{path.name}.")]
        assert not leftover, f"temp file leak: {leftover}"


class TestOwns:
    def test_basic_membership(self):
        assert so.owns({"NVDA", "AAPL"}, "NVDA")
        assert not so.owns({"NVDA"}, "AAPL")

    def test_case_insensitive(self):
        assert so.owns({"NVDA"}, "nvda")
        assert so.owns({"nvda"}, "NVDA")

    def test_empty_symbol_returns_false(self):
        assert not so.owns({"NVDA"}, "")


class TestClaim:
    def test_adds_symbol(self, isolated_state):
        so.claim("shark", "NVDA")
        assert so.load_owned("shark") == {"NVDA"}

    def test_idempotent(self, isolated_state):
        so.claim("shark", "NVDA")
        so.claim("shark", "NVDA")
        so.claim("shark", "nvda")
        assert so.load_owned("shark") == {"NVDA"}

    def test_subsystems_are_isolated(self, isolated_state):
        so.claim("shark", "NVDA")
        so.claim("wheel", "AAPL")
        assert so.load_owned("shark") == {"NVDA"}
        assert so.load_owned("wheel") == {"AAPL"}

    def test_empty_symbol_is_noop(self, isolated_state):
        so.claim("shark", "")
        assert so.load_owned("shark") == set()


class TestRelease:
    def test_removes_symbol(self, isolated_state):
        so.save_owned("shark", ["NVDA", "AAPL"])
        so.release("shark", "NVDA")
        assert so.load_owned("shark") == {"AAPL"}

    def test_idempotent_on_missing(self, isolated_state):
        so.save_owned("shark", ["AAPL"])
        so.release("shark", "NVDA")  # not present
        assert so.load_owned("shark") == {"AAPL"}

    def test_case_insensitive(self, isolated_state):
        so.save_owned("shark", ["NVDA"])
        so.release("shark", "nvda")
        assert so.load_owned("shark") == set()

    def test_empty_symbol_is_noop(self, isolated_state):
        so.save_owned("shark", ["NVDA"])
        so.release("shark", "")
        assert so.load_owned("shark") == {"NVDA"}
