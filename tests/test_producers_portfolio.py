"""Unit tests for `user_data.modules.producers.portfolio`.

Covers the **B1** day-PnL contract:
  - `stocks.day_pnl_usd = portfolio_value − last_equity` (NOT
    `portfolio_value − peak_equity`, which is drawdown)
  - When `last_equity` is missing, day_pnl_usd is 0.0 + a `_meta`
    flag (not a phantom number)
  - The producer never writes (spec §5.4 data-preservation)
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from user_data.modules.producers import portfolio as P


def _write_snapshot(dir_: Path, **fields) -> Path:
    p = dir_ / "account_snapshot.json"
    p.write_text(json.dumps({
        "ts": "2026-05-15T20:59:06.811497+00:00",
        "cash": 100831.17,
        "buying_power": 315896.74,
        "portfolio_value": 100241.17,
        "wheel_open_positions": 1,
        **fields,
    }))
    return p


def test_stocks_day_pnl_b1_truth_with_last_equity(monkeypatch, tmp_path):
    """B1 fix — day_pnl_usd = portfolio_value − last_equity."""
    snap = {
        "ts": "2026-05-15T20:59:06+00:00",
        "portfolio_value": 100_241.17,
        "last_equity": 100_528.31,   # yesterday's close
        "cash": 100_831.17,
        "buying_power": 315_896.74,
        "wheel_open_positions": 1,
    }
    out = P.stocks_day_pnl(snap)
    # The legacy `stocksMove = equity - peak_equity` would have produced
    # (100_241.17 - 100_528.31) = -287.14 BUT against the *peak*, not
    # last_equity. Here last_equity == 100528.31 happens to equal the
    # historical peak — so the number happens to match. The semantic
    # difference: with last_equity, this number is "today's move" and
    # correctly rolls to 0 at the next session boundary. With peak, it
    # would stay -287.14 forever.
    assert out["day_pnl_usd"] == pytest.approx(-287.14, abs=0.01)
    assert out["equity"] == pytest.approx(100_241.17, abs=0.01)
    assert out["last_equity"] == pytest.approx(100_528.31, abs=0.01)
    assert out["_last_equity_present"] is True


def test_stocks_day_pnl_b1_missing_last_equity_returns_zero():
    """When `last_equity` is missing, return 0.0 (NOT a phantom
    `portfolio_value − peak_equity`). The `_last_equity_present` flag
    lets the UI render `—` instead of a misleading zero."""
    snap = {
        "ts": "2026-05-15T20:59:06+00:00",
        "portfolio_value": 100_241.17,
        # NO last_equity
        "cash": 100_831.17,
        "wheel_open_positions": 0,
    }
    out = P.stocks_day_pnl(snap)
    assert out["day_pnl_usd"] == 0.0
    assert out["_last_equity_present"] is False


def test_stocks_day_pnl_flat_day():
    """Flat day → 0.00, not noise from float subtraction."""
    snap = {
        "ts": "2026-05-15T20:59:06+00:00",
        "portfolio_value": 100_000.00,
        "last_equity": 100_000.00,
    }
    out = P.stocks_day_pnl(snap)
    assert out["day_pnl_usd"] == 0.0
    assert out["day_pnl_pct"] == 0.0


def test_stocks_day_pnl_up_day():
    snap = {
        "portfolio_value": 101_000.00,
        "last_equity": 100_000.00,
    }
    out = P.stocks_day_pnl(snap)
    assert out["day_pnl_usd"] == pytest.approx(1000.0, abs=0.01)
    assert out["day_pnl_pct"] == pytest.approx(1.0, abs=0.01)


def test_stocks_day_pnl_handles_bad_strings_gracefully():
    """Non-numeric strings in snapshot must not crash the producer."""
    snap = {
        "portfolio_value": "not-a-number",
        "last_equity": None,
    }
    out = P.stocks_day_pnl(snap)
    assert out["day_pnl_usd"] == 0.0
    assert out["_last_equity_present"] is False


def test_read_wheel_snapshot_missing_file_returns_empty(monkeypatch, tmp_path):
    """Missing snapshot must not raise — return {}."""
    monkeypatch.setattr(P, "_WHEEL_SNAPSHOT_PATH", tmp_path / "nope.json")
    assert P._read_wheel_snapshot() == {}


def test_portfolio_snapshot_smoke_with_missing_db(monkeypatch, tmp_path):
    """End-to-end: even when crypto side (unified_risk / ops_db) is
    unavailable, the producer must still return a shaped dict with
    the stocks side populated from the snapshot file."""
    snap_path = tmp_path / "account_snapshot.json"
    snap_path.write_text(json.dumps({
        "ts": "2026-05-15T20:59:06+00:00",
        "portfolio_value": 100_241.17,
        "last_equity": 100_000.00,
        "cash": 100_831.17,
        "buying_power": 315_896.74,
        "wheel_open_positions": 1,
    }))
    monkeypatch.setattr(P, "_WHEEL_SNAPSHOT_PATH", snap_path)

    # Force unified_risk + ops_db to fail — simulates the dashboard
    # boot path where postgres isn't reachable yet.
    import sys
    fake_mod = type(sys)("user_data.modules.unified_risk")
    def _raise(*a, **kw):
        raise RuntimeError("DB unreachable")
    fake_mod.get_combined_risk_status = _raise  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "user_data.modules.unified_risk", fake_mod)

    out = P.portfolio_snapshot()
    assert "combined" in out
    assert "crypto" in out
    assert "stocks" in out
    assert "_meta" in out
    assert out["stocks"]["day_pnl_usd"] == pytest.approx(241.17, abs=0.01)
    # Crypto side gracefully zeroed
    assert out["crypto"]["equity"] == 0.0
