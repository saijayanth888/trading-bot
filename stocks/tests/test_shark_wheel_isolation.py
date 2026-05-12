"""Regression tests for the 2026-05-12 Shark/Wheel leak.

Two protections being verified:

1. Fix 1 — asset-class gate. Given an Alpaca positions list with both
   equity and option rows, every Shark management loop must touch only
   the equity rows.

2. Fix 3 — per-subsystem ownership. Given an equity row in Shark's
   owned set and a second equity row in Wheel's owned set, only the
   Shark-owned row gets managed.

These run as unit tests against the deterministic in-process helpers —
no Alpaca, no Perplexity, no LLM. The midday phase itself has too many
I/O integrations to exercise end-to-end here; we cover the choke points
(evaluate_exits, manage_stops) which are where every Shark action
funnels through.
"""

from __future__ import annotations

import importlib
from unittest.mock import MagicMock, patch

import pytest

from shared import subsystem_ownership as so


@pytest.fixture
def isolated_ownership(monkeypatch, tmp_path):
    """Redirect ownership state files into tmp_path."""
    shark_dir = tmp_path / "shark" / "state"
    wheel_dir = tmp_path / "wheel" / "state"
    shark_dir.mkdir(parents=True)
    wheel_dir.mkdir(parents=True)

    def fake_state_path(subsystem: str):
        if subsystem == "shark":
            return shark_dir / "owned_symbols.json"
        if subsystem == "wheel":
            return wheel_dir / "owned_symbols.json"
        raise ValueError(subsystem)

    monkeypatch.setattr(so, "_state_path", fake_state_path)
    return tmp_path


def _equity_pos(symbol: str, plpc: float = -0.10, qty: int = 30) -> dict:
    """A Shark-style equity position at -10% (below -7% hard stop)."""
    return {
        "symbol": symbol,
        "qty": qty,
        "unrealized_plpc": plpc,
        "current_price": 100.0,
        "avg_entry_price": 110.0,
        "asset_class": "us_equity",
    }


def _option_pos(symbol: str, plpc: float = -0.10, qty: int = 1) -> dict:
    """A Wheel-style long-put option position at -10%."""
    return {
        "symbol": symbol,
        "qty": qty,
        "unrealized_plpc": plpc,
        "current_price": 1.50,
        "avg_entry_price": 2.00,
        "asset_class": "us_option",
    }


# ---------------------------------------------------------------------------
# Fix 1 — asset_class gate
# ---------------------------------------------------------------------------


class TestFix1AssetClassGate:
    def test_exit_manager_ignores_options(self):
        """Mixed positions: only the equities produce exit actions."""
        from shark.execution import exit_manager
        importlib.reload(exit_manager)

        positions = [
            _option_pos("SOFI260522P00012000", plpc=-0.10),
            _option_pos("PLTR260522P00120000", plpc=-0.20),
            _equity_pos("NVDA", plpc=-0.08),
            _equity_pos("AAPL", plpc=-0.09),
        ]
        actions = exit_manager.evaluate_exits(positions)

        action_symbols = {a["symbol"] for a in actions}
        assert action_symbols == {"NVDA", "AAPL"}, (
            f"options leaked into exit actions: {action_symbols}"
        )
        # The five SOFI/PLTR-style option symbols (the 2026-05-12 leak)
        # must be wholly absent from the action list.
        assert all("260522P" not in s for s in action_symbols)

    def test_evaluate_exits_zero_options_no_close_calls(self):
        """The exact 2026-05-12 scenario: 5 options + 0 equities → 0 actions."""
        from shark.execution import exit_manager
        importlib.reload(exit_manager)

        # The five option OCC tickers that appeared as SELL rows on 2026-05-12
        positions = [
            _option_pos("SOFI260522P00012000", plpc=-0.12),
            _option_pos("PLTR260522P00120000", plpc=-0.15),
            _option_pos("NVDA260522P00130000", plpc=-0.08),
            _option_pos("MARA260522P00012000", plpc=-0.20),
            _option_pos("HOOD260522P00045000", plpc=-0.10),
        ]
        actions = exit_manager.evaluate_exits(positions)
        assert actions == [], (
            f"Shark would have closed {len(actions)} option positions — "
            "Fix 1 asset-class gate failed"
        )

    def test_evaluate_exits_with_2_equities_plus_1_option(self):
        """Plan-spec scenario: 1 option + 2 equities → ZERO option close calls."""
        from shark.execution import exit_manager
        importlib.reload(exit_manager)

        positions = [
            _option_pos("SOFI260522P00012000", plpc=-0.20),  # would trigger -7% if leaked
            _equity_pos("NVDA", plpc=-0.08),
            _equity_pos("AAPL", plpc=-0.05),  # under threshold — no action
        ]
        actions = exit_manager.evaluate_exits(positions)
        close_all_actions = [a for a in actions if a["action"] == "CLOSE_ALL"]
        # Only NVDA at -8% should produce a CLOSE_ALL hard-stop action
        assert len(close_all_actions) == 1
        assert close_all_actions[0]["symbol"] == "NVDA"


# ---------------------------------------------------------------------------
# Fix 3 — ownership pre-action check
# ---------------------------------------------------------------------------


class TestFix3OwnershipGate:
    def test_exit_manager_skips_unowned_equity(self, isolated_ownership):
        """Shark owns NVDA; Wheel owns ASSIGNED. Only NVDA generates exits."""
        from shark.execution import exit_manager
        importlib.reload(exit_manager)

        so.claim("shark", "NVDA")
        so.claim("wheel", "ASSIGNED")  # e.g. assigned-share underlying

        positions = [
            _equity_pos("NVDA", plpc=-0.10),
            _equity_pos("ASSIGNED", plpc=-0.10),  # Wheel-owned share at -10%
        ]
        actions = exit_manager.evaluate_exits(positions)

        action_symbols = {a["symbol"] for a in actions}
        assert action_symbols == {"NVDA"}, (
            f"Wheel-owned ASSIGNED leaked into Shark's exit actions: {action_symbols}"
        )

    def test_exit_manager_cold_start_falls_through(self, isolated_ownership):
        """No bootstrap yet → ownership gate is bypassed (backwards-compat).

        Cold-start operators haven't run migrate_ownership_bootstrap. We
        must not strand them with zero exit actions. asset_class is still
        the firewall in this mode.
        """
        from shark.execution import exit_manager
        importlib.reload(exit_manager)

        # No claims made → load_owned("shark") returns set()
        positions = [
            _equity_pos("NVDA", plpc=-0.10),
            _option_pos("SOFI260522P00012000", plpc=-0.20),
        ]
        actions = exit_manager.evaluate_exits(positions)
        # NVDA still gets managed (cold-start falls through), option still blocked
        action_symbols = {a["symbol"] for a in actions}
        assert action_symbols == {"NVDA"}

    def test_exit_manager_post_bootstrap_blocks_unknown_equity(self, isolated_ownership):
        """Once bootstrapped, an un-claimed equity is blocked even if asset_class is us_equity."""
        from shark.execution import exit_manager
        importlib.reload(exit_manager)

        so.claim("shark", "NVDA")  # only NVDA is Shark's

        positions = [
            _equity_pos("NVDA", plpc=-0.10),
            _equity_pos("MYSTERY", plpc=-0.20),  # equity, but Shark doesn't own
        ]
        actions = exit_manager.evaluate_exits(positions)
        action_symbols = {a["symbol"] for a in actions}
        assert action_symbols == {"NVDA"}


# ---------------------------------------------------------------------------
# stops.manage_stops — same defenses
# ---------------------------------------------------------------------------


class TestStopsManagement:
    def test_manage_stops_skips_options(self, isolated_ownership):
        """manage_stops must not bolt trailing stops onto option contracts."""
        from shark.execution import stops
        importlib.reload(stops)

        # so far so good — also need to mock the broker client lookup
        fake_api = MagicMock()
        fake_api.get_orders.return_value = []
        with patch.object(stops, "_get_client", return_value=fake_api), \
             patch.object(stops, "place_trailing_stop") as ts, \
             patch.object(stops, "cancel_order"):
            actions = stops.manage_stops([
                _option_pos("SOFI260522P00012000", plpc=0.25, qty=1),
                _equity_pos("NVDA", plpc=0.25, qty=30),
            ])
            # place_trailing_stop must only be called for NVDA, never the option
            called_symbols = [c.args[0] for c in ts.call_args_list]
            assert "SOFI260522P00012000" not in called_symbols
            # Bug catch: a leaked option here would have appeared on the list

    def test_manage_stops_skips_unowned_equity(self, isolated_ownership):
        """manage_stops must skip equities Shark didn't open."""
        from shark.execution import stops
        importlib.reload(stops)

        so.claim("shark", "NVDA")

        fake_api = MagicMock()
        fake_api.get_orders.return_value = []
        with patch.object(stops, "_get_client", return_value=fake_api), \
             patch.object(stops, "place_trailing_stop") as ts, \
             patch.object(stops, "cancel_order"):
            stops.manage_stops([
                _equity_pos("NVDA", plpc=0.25, qty=30),
                _equity_pos("WHEEL_ASSIGNED", plpc=0.25, qty=100),
            ])
            called_symbols = [c.args[0] for c in ts.call_args_list]
            assert "WHEEL_ASSIGNED" not in called_symbols


# ---------------------------------------------------------------------------
# alpaca_data.get_positions — asset_class is surfaced
# ---------------------------------------------------------------------------


class TestGetPositionsAssetClass:
    def test_asset_class_default(self):
        """Defensive default: missing asset_class → us_equity (legacy behaviour)."""
        from types import SimpleNamespace
        from shark.data import alpaca_data

        fake_pos = SimpleNamespace(
            symbol="AAPL", qty=10, avg_entry_price=100.0,
            current_price=105.0, unrealized_pl=50.0,
            unrealized_plpc=0.05, market_value=1050.0, side="long",
            # no asset_class attribute — simulates legacy SDK or paper edge case
        )
        fake_client = MagicMock()
        fake_client.get_all_positions.return_value = [fake_pos]
        with patch.object(alpaca_data, "_get_trading_client", return_value=fake_client):
            positions = alpaca_data.get_positions()
        assert positions[0]["asset_class"] == "us_equity"

    def test_asset_class_pass_through(self):
        """Alpaca returns an option → asset_class flows through unchanged."""
        from types import SimpleNamespace
        from shark.data import alpaca_data

        fake_pos = SimpleNamespace(
            symbol="SOFI260522P00012000", qty=1, avg_entry_price=2.00,
            current_price=1.50, unrealized_pl=-50.0,
            unrealized_plpc=-0.25, market_value=150.0, side="long",
            asset_class="us_option",
        )
        fake_client = MagicMock()
        fake_client.get_all_positions.return_value = [fake_pos]
        with patch.object(alpaca_data, "_get_trading_client", return_value=fake_client):
            positions = alpaca_data.get_positions()
        assert positions[0]["asset_class"] == "us_option"
