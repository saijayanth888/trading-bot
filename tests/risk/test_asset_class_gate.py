"""Tests for ``quanta_core.risk.asset_class_gate``.

Derived from the 2026-05-12 Shark/Wheel leak fix (the "Bug #74" in the
task spec). The pure-function gate must:

* refuse non-equity rows when subsystem == "shark" (the actual leak);
* allow non-equity rows for subsystem == "wheel";
* for equity rows, consult the per-subsystem ownership ledger.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from quanta_core.risk import ownership
from quanta_core.risk.asset_class_gate import Position, is_quanta_managed


@pytest.fixture
def seeded_state(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    state = tmp_path / "seeded-state"
    state.mkdir()
    monkeypatch.setenv("QUANTA_STATE_DIR", str(state))
    # Pre-seed: Shark owns NVDA; Wheel owns AAPL (the assigned-share case).
    ownership.save_owned("shark", ["NVDA"])
    ownership.save_owned("wheel", ["AAPL"])
    return state


class TestAssetClassGate:
    def test_shark_blocked_from_options(self, seeded_state: Path) -> None:
        opt = Position(
            symbol="AAPL250620P00150000",
            asset_class="us_option",
            venue="alpaca",
        )
        # This was the leak: midday hard-stop walking option rows.
        assert is_quanta_managed(opt, "shark") is False

    def test_wheel_owns_options(self, seeded_state: Path) -> None:
        opt = Position(symbol="AAPL250620P00150000", asset_class="us_option")
        assert is_quanta_managed(opt, "wheel") is True

    def test_shark_owns_listed_equity(self, seeded_state: Path) -> None:
        nvda = Position(symbol="NVDA", asset_class="us_equity")
        assert is_quanta_managed(nvda, "shark") is True

    def test_shark_does_not_own_other_equity(self, seeded_state: Path) -> None:
        # AAPL is Wheel-owned (assigned shares); Shark must not touch.
        aapl = Position(symbol="AAPL", asset_class="us_equity")
        assert is_quanta_managed(aapl, "shark") is False

    def test_wheel_owns_assigned_shares(self, seeded_state: Path) -> None:
        aapl = Position(symbol="AAPL", asset_class="us_equity")
        assert is_quanta_managed(aapl, "wheel") is True

    def test_unowned_equity_fails_safe(self, seeded_state: Path) -> None:
        # MSFT is in neither ledger → both subsystems refuse.
        msft = Position(symbol="MSFT", asset_class="us_equity")
        assert is_quanta_managed(msft, "shark") is False
        assert is_quanta_managed(msft, "wheel") is False

    def test_empty_symbol_returns_false(self, seeded_state: Path) -> None:
        assert is_quanta_managed(Position(symbol=""), "shark") is False

    def test_asset_class_case_insensitive(self, seeded_state: Path) -> None:
        opt = Position(symbol="X", asset_class="US_OPTION")
        # Upper-case "US_OPTION" still classifies as non-equity → Shark blocked.
        assert is_quanta_managed(opt, "shark") is False

    def test_default_asset_class_is_equity(self, seeded_state: Path) -> None:
        # Position constructed without asset_class defaults to us_equity.
        nvda = Position(symbol="NVDA")
        assert is_quanta_managed(nvda, "shark") is True

    def test_position_is_frozen(self, seeded_state: Path) -> None:
        nvda = Position(symbol="NVDA")
        with pytest.raises(Exception):  # FrozenInstanceError, but subclass of AttributeError
            nvda.symbol = "MSFT"  # type: ignore[misc]

    def test_no_ledger_returns_false_for_equity(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # State dir exists but is empty (no save_owned calls).
        empty = tmp_path / "no-ledger"
        empty.mkdir()
        monkeypatch.setenv("QUANTA_STATE_DIR", str(empty))
        nvda = Position(symbol="NVDA", asset_class="us_equity")
        assert is_quanta_managed(nvda, "shark") is False
