"""
test_wheel_preconditions — guards the P1-S4 + P1-S5 entry gates added
2026-05-11 as part of the wheel pilot pre-condition audit.

Covers:
  - P1-S4 total_collateral_usd cap blocks a CSP entry when the new
    contract would push pilot-wide open collateral past the ceiling.
  - P1-S5a earnings blackout reads state/earnings.json and skips the
    entry when next-earnings is within cfg.earnings_blackout_days.
  - P1-S5b kill_loss_per_cycle_usd flips the per-ticker kill flag when
    rolling-30d realized P&L is below the loss floor.

These tests work without the alpaca SDK by patching the broker hook.
"""
from __future__ import annotations

import json
from datetime import date, timedelta
from unittest.mock import MagicMock, patch

import pytest
from wheel.config import WheelConfig
from wheel.state import Position


@pytest.fixture
def isolated_state(tmp_path, monkeypatch):
    """Redirect wheel.state file paths into a tmp dir so tests don't
    clobber the real journal."""
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    monkeypatch.setattr("wheel.state._STATE_DIR", state_dir)
    monkeypatch.setattr("wheel.state._POSITIONS_FILE", state_dir / "positions.json")
    monkeypatch.setattr("wheel.state._TRADES_FILE", state_dir / "trades.jsonl")
    monkeypatch.setattr("wheel.state._KILL_FLAGS_FILE", state_dir / "kill_flags.json")
    # Runner caches the earnings.json path at import time — redirect it too.
    monkeypatch.setattr("wheel.runner._EARNINGS_FILE", state_dir / "earnings.json")
    # filters.py has its own _EARNINGS_FILE reference — redirect it as well.
    monkeypatch.setattr("wheel.filters._EARNINGS_FILE", state_dir / "earnings.json")
    # Disable IVR filter for all precondition tests: avoids live yfinance
    # calls and keeps these tests fast and network-free.
    monkeypatch.setenv("WHEEL_IVR_FILTER_ENABLED", "false")
    return state_dir


def _build_mock_broker():
    """A broker mock that returns a benign account + a single put candidate."""
    pytest.importorskip("alpaca")
    from wheel.broker import AccountSnapshot
    from wheel.strategy import OptionContract

    broker = MagicMock()
    broker.get_account.return_value = AccountSnapshot(
        cash=100_000.0, buying_power=100_000.0,
        portfolio_value=100_000.0, paper=True,
        options_buying_power=100_000.0,
    )
    # One sane put: SOFI strike 15 delta 0.30, $0.40 mid → collateral $1500.
    contract = OptionContract(
        symbol="SOFI260516P00015000",
        underlying="SOFI",
        strike=15.0,
        expiry=date.today() + timedelta(days=8),
        contract_type="put",
        delta=-0.30,
        bid=0.40,
        ask=0.42,
        open_interest=1500,
    )
    broker.list_put_contracts.return_value = [contract]
    # sell_to_open must return an object with an .id attribute (not a plain
    # dict) so _try_sell_csp's getattr(order, "id", None) finds "test-order-1"
    # and the 30s fill-poll loop can call broker.trading.get_order_by_id.
    sell_resp = MagicMock()
    sell_resp.id = "test-order-1"
    sell_resp.get = lambda key, default=None: {"id": "test-order-1"}.get(key, default)
    broker.sell_to_open.return_value = sell_resp
    # Stub the fill-poll: status.value == "filled" exits the loop immediately.
    fill_resp = MagicMock()
    fill_resp.status.value = "filled"
    fill_resp.filled_avg_price = 0.40
    broker.trading.get_order_by_id.return_value = fill_resp
    return broker, contract


def test_total_collateral_cap_blocks_new_csp(isolated_state, monkeypatch):
    """P1-S4: when the journal already has $4500 of open CSP collateral,
    a $1500 candidate that would push the total past $5000 must be
    skipped — not silently executed."""
    pytest.importorskip("alpaca")
    from wheel import runner, state

    # Pin the collateral ceiling to $5000 so this test is independent of any
    # WHEEL_MAX_TOTAL_COLLATERAL value set in .env (which may be much larger).
    monkeypatch.setenv("WHEEL_MAX_TOTAL_COLLATERAL", "5000")

    # Pre-seed: a SOFI short_put at strike 45 (so collateral = $4500).
    state.add_position(Position(
        underlying="ABNB", contract_symbol="ABNB260516P00045000",
        kind="short_put", qty=1, strike=45.0,
        expiry=(date.today() + timedelta(days=8)).isoformat(),
        entry_credit=100.0, opened_at="2026-05-10T11:00:00Z",
    ))
    broker, _ = _build_mock_broker()

    with patch.object(runner, "_shark_kill_active", return_value=False), \
         patch.object(runner, "_fetch_spy_regime", return_value="mean_reverting"), \
         patch.object(runner, "from_env", return_value=broker):
        result = runner.sell_csps(symbols_override=["SOFI"])

    assert any("total collateral" in s for s in result["skipped"]), (
        f"expected total_collateral skip, got skipped={result['skipped']}"
    )
    assert result["actions"] == []
    broker.sell_to_open.assert_not_called()


def test_total_collateral_cap_allows_when_room_remains(isolated_state):
    """Inverse of the previous test: with the journal empty, the same
    SOFI candidate must go through (cap not breached)."""
    pytest.importorskip("alpaca")
    from wheel import runner

    broker, _ = _build_mock_broker()
    with patch.object(runner, "_shark_kill_active", return_value=False), \
         patch.object(runner, "_fetch_spy_regime", return_value="mean_reverting"), \
         patch.object(runner, "from_env", return_value=broker):
        result = runner.sell_csps(symbols_override=["SOFI"])

    assert len(result["actions"]) == 1
    assert result["actions"][0]["action"] == "sell_to_open_put"
    broker.sell_to_open.assert_called_once()


def test_earnings_blackout_skips_csp(isolated_state):
    """P1-S5a: next-earnings within cfg.earnings_blackout_days → skip."""
    pytest.importorskip("alpaca")
    from wheel import filters, runner

    # Pin earnings 2 days out → inside the default 7-day blackout.
    (isolated_state / "earnings.json").write_text(
        json.dumps({"SOFI": (date.today() + timedelta(days=2)).isoformat()})
    )

    broker, _ = _build_mock_broker()
    # Silence yfinance so the test falls back to the static earnings.json.
    with patch.object(filters, "yf", None), \
         patch.object(runner, "_shark_kill_active", return_value=False), \
         patch.object(runner, "_fetch_spy_regime", return_value="mean_reverting"), \
         patch.object(runner, "from_env", return_value=broker):
        result = runner.sell_csps(symbols_override=["SOFI"])

    assert any("earnings blackout" in s for s in result["skipped"]), (
        f"expected earnings_blackout skip, got skipped={result['skipped']}"
    )
    broker.sell_to_open.assert_not_called()


def test_earnings_blackout_far_out_passes(isolated_state):
    """Earnings 30 days out is outside the 7-day blackout and option DTE → no skip."""
    pytest.importorskip("alpaca")
    from wheel import filters, runner

    (isolated_state / "earnings.json").write_text(
        json.dumps({"SOFI": (date.today() + timedelta(days=30)).isoformat()})
    )

    broker, _ = _build_mock_broker()
    # Silence yfinance so the test falls back to the static earnings.json.
    with patch.object(filters, "yf", None), \
         patch.object(runner, "_shark_kill_active", return_value=False), \
         patch.object(runner, "_fetch_spy_regime", return_value="mean_reverting"), \
         patch.object(runner, "from_env", return_value=broker):
        result = runner.sell_csps(symbols_override=["SOFI"])

    assert len(result["actions"]) == 1
    broker.sell_to_open.assert_called_once()


def test_kill_loss_per_cycle_flips_kill_flag(isolated_state):
    """P1-S5b: cycle P&L below -kill_loss_per_cycle_usd sets the kill flag
    and skips. Subsequent calls find the kill flag and skip immediately."""
    pytest.importorskip("alpaca")
    from wheel import runner, state

    # Seed the trade log with a -$600 loss inside the rolling 30-day window
    # (default kill_loss_per_cycle = $500 → -$600 trips the gate).
    state.append_trade(state.TradeRecord(
        timestamp=(date.today() - timedelta(days=5)).isoformat() + "T11:00:00Z",
        underlying="SOFI",
        cycle="csp_close",
        pnl=-600.0,
        notes="paper-mode stop-loss closed bad CSP",
    ))

    broker, _ = _build_mock_broker()
    with patch.object(runner, "_shark_kill_active", return_value=False), \
         patch.object(runner, "_fetch_spy_regime", return_value="mean_reverting"), \
         patch.object(runner, "from_env", return_value=broker):
        result = runner.sell_csps(symbols_override=["SOFI"])

    # Cycle-loss skip should appear AND the per-ticker kill flag should be set.
    assert any("cycle P&L" in s for s in result["skipped"])
    assert state.is_killed("SOFI"), "kill flag must be set after cycle-loss trip"
    broker.sell_to_open.assert_not_called()


def test_kill_loss_per_cycle_outside_window_does_not_trip(isolated_state):
    """A -$600 loss 45 days ago is outside the 30-day rolling window —
    the kill gate must NOT trip."""
    pytest.importorskip("alpaca")
    from wheel import runner, state

    state.append_trade(state.TradeRecord(
        timestamp=(date.today() - timedelta(days=45)).isoformat() + "T11:00:00Z",
        underlying="SOFI",
        cycle="csp_close",
        pnl=-600.0,
        notes="ancient history outside rolling window",
    ))

    broker, _ = _build_mock_broker()
    with patch.object(runner, "_shark_kill_active", return_value=False), \
         patch.object(runner, "_fetch_spy_regime", return_value="mean_reverting"), \
         patch.object(runner, "from_env", return_value=broker):
        result = runner.sell_csps(symbols_override=["SOFI"])

    assert len(result["actions"]) == 1
    assert not state.is_killed("SOFI")
