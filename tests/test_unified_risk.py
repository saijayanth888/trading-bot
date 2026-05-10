"""Unit tests for user_data.modules.unified_risk — combined drawdown governor.

Run from the repo root:
    pytest tests/test_unified_risk.py -v
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

# Add repo root to path so `import user_data.modules.unified_risk` works
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from user_data.modules import unified_risk
from user_data.modules.unified_risk import (
    _dd,
    _save_peak,
    _load_peaks,
    get_combined_risk_status,
    COMBINED_DD_THRESHOLD_PCT,
    STOCKS_STALE_SECONDS,
)


# ---------------------------------------------------------------------------
# Drawdown formula — pinned independent of the rest of the module
# ---------------------------------------------------------------------------


class TestDrawdownFormula:
    def test_no_drop(self):
        assert _dd(100.0, 100.0) == 0.0

    def test_10pct_drop(self):
        assert abs(_dd(90.0, 100.0) - 0.10) < 1e-9

    def test_50pct_drop(self):
        assert abs(_dd(50.0, 100.0) - 0.50) < 1e-9

    def test_zero_peak_returns_zero(self):
        # Avoid division by zero on first observation
        assert _dd(100.0, 0.0) == 0.0

    def test_negative_peak_returns_zero(self):
        assert _dd(100.0, -10.0) == 0.0

    def test_above_peak_floors_at_zero(self):
        assert _dd(110.0, 100.0) == 0.0

    def test_negative_equity_exceeds_one(self):
        # Liquidation event: equity went negative — drawdown > 100%
        assert _dd(-10.0, 100.0) > 1.0


# ---------------------------------------------------------------------------
# Peak persistence — multi-component (combined / crypto / stocks)
# ---------------------------------------------------------------------------


class TestPeakTracking:
    def test_save_then_load_roundtrip(self, tmp_path, monkeypatch):
        peak_file = tmp_path / "peak.json"
        monkeypatch.setattr(unified_risk, "_PEAK_FILE", peak_file)

        _save_peak(40000.0, 19000.0, 21000.0, {"crypto": 19000, "stocks": 21000})
        c, cr, st = _load_peaks()
        assert c == 40000.0
        assert cr == 19000.0
        assert st == 21000.0

    def test_load_returns_none_tuple_on_missing_file(self, tmp_path, monkeypatch):
        monkeypatch.setattr(unified_risk, "_PEAK_FILE", tmp_path / "nope.json")
        c, cr, st = _load_peaks()
        assert c is None and cr is None and st is None

    def test_corrupt_file_returns_none_tuple(self, tmp_path, monkeypatch):
        peak_file = tmp_path / "peak.json"
        peak_file.write_text("not valid json {")
        monkeypatch.setattr(unified_risk, "_PEAK_FILE", peak_file)
        c, cr, st = _load_peaks()
        assert c is None and cr is None and st is None

    def test_save_writes_components_for_forensics(self, tmp_path, monkeypatch):
        peak_file = tmp_path / "peak.json"
        monkeypatch.setattr(unified_risk, "_PEAK_FILE", peak_file)
        _save_peak(40000.0, 19000.0, 21000.0,
                   {"crypto": 19000, "stocks": 21000, "note": "test"})
        payload = json.loads(peak_file.read_text())
        assert payload["combined_peak_equity"] == 40000.0
        assert payload["components"]["note"] == "test"
        assert "updated_at" in payload


# ---------------------------------------------------------------------------
# Drawdown formula composition — happy path + breaker logic
# ---------------------------------------------------------------------------


def _seed_mocks(mock_open, mock_start, mock_unr, mock_rea, mock_stocks, *,
                stocks_pv=21000.0, ts: str | None = None,
                crypto_realised=0.0, crypto_unrealised=0.0):
    """Common mock seed for happy-path scenarios."""
    if ts is None:
        ts = datetime.now(timezone.utc).isoformat()
    mock_open.return_value = 0
    mock_start.return_value = 19000.0
    mock_rea.return_value = crypto_realised
    mock_unr.return_value = crypto_unrealised
    mock_stocks.return_value = {
        "portfolio_value": stocks_pv, "cash": 5000.0, "buying_power": 5000.0,
        "wheel_cumulative_pnl": 0.0, "open_positions": 0,
        "snapshot_ts": ts, "paper": True,
    }


class TestCircuitBreakerLogic:
    @patch("user_data.modules.unified_risk._stocks_state")
    @patch("user_data.modules.unified_risk._crypto_realised_pnl_usd")
    @patch("user_data.modules.unified_risk._crypto_unrealised_pnl_usd")
    @patch("user_data.modules.unified_risk._crypto_starting_equity")
    @patch("user_data.modules.unified_risk._crypto_open_count")
    def test_no_drawdown_no_breaker(
        self, mock_open, mock_start, mock_unr, mock_rea, mock_stocks,
        tmp_path, monkeypatch,
    ):
        monkeypatch.setattr(unified_risk, "_PEAK_FILE", tmp_path / "peak.json")
        _seed_mocks(mock_open, mock_start, mock_unr, mock_rea, mock_stocks)

        s = get_combined_risk_status()
        assert s["total_equity"] == 40000.0
        assert s["combined_drawdown_pct"] == 0.0
        assert not s["circuit_breaker_active"]

    @patch("user_data.modules.unified_risk._is_nyse_open_now", return_value=True)
    @patch("user_data.modules.unified_risk._stocks_state")
    @patch("user_data.modules.unified_risk._crypto_realised_pnl_usd")
    @patch("user_data.modules.unified_risk._crypto_unrealised_pnl_usd")
    @patch("user_data.modules.unified_risk._crypto_starting_equity")
    @patch("user_data.modules.unified_risk._crypto_open_count")
    def test_combined_dd_over_threshold_trips(
        self, mock_open, mock_start, mock_unr, mock_rea, mock_stocks, _mock_market,
        tmp_path, monkeypatch,
    ):
        """Establish peak, then drop hard enough to cross the threshold."""
        monkeypatch.setattr(unified_risk, "_PEAK_FILE", tmp_path / "peak.json")

        # First call seeds peak at $40K
        _seed_mocks(mock_open, mock_start, mock_unr, mock_rea, mock_stocks,
                    stocks_pv=21000.0)
        get_combined_risk_status()

        # Second call: 12% drawdown across the combined portfolio
        _seed_mocks(mock_open, mock_start, mock_unr, mock_rea, mock_stocks,
                    stocks_pv=18900.0,
                    crypto_realised=-2300.0)
        s = get_combined_risk_status()
        assert s["combined_drawdown_pct"] > 10.0
        assert s["circuit_breaker_active"]

    @patch("user_data.modules.unified_risk._stocks_state")
    @patch("user_data.modules.unified_risk._crypto_realised_pnl_usd")
    @patch("user_data.modules.unified_risk._crypto_unrealised_pnl_usd")
    @patch("user_data.modules.unified_risk._crypto_starting_equity")
    @patch("user_data.modules.unified_risk._crypto_open_count")
    def test_threshold_value_matches_env(
        self, mock_open, mock_start, mock_unr, mock_rea, mock_stocks,
        tmp_path, monkeypatch,
    ):
        monkeypatch.setattr(unified_risk, "_PEAK_FILE", tmp_path / "peak.json")
        _seed_mocks(mock_open, mock_start, mock_unr, mock_rea, mock_stocks)
        s = get_combined_risk_status()
        # threshold_pct is the float-percentage form (e.g. 10.0 for 0.10)
        assert s["threshold_pct"] == round(COMBINED_DD_THRESHOLD_PCT * 100, 1)


class TestStaleDataFailSafe:
    @patch("user_data.modules.unified_risk._is_nyse_open_now", return_value=True)
    @patch("user_data.modules.unified_risk._stocks_state")
    @patch("user_data.modules.unified_risk._crypto_realised_pnl_usd")
    @patch("user_data.modules.unified_risk._crypto_unrealised_pnl_usd")
    @patch("user_data.modules.unified_risk._crypto_starting_equity")
    @patch("user_data.modules.unified_risk._crypto_open_count")
    def test_stale_snapshot_during_market_trips_breaker(
        self, mock_open, mock_start, mock_unr, mock_rea, mock_stocks, _mock_market,
        tmp_path, monkeypatch,
    ):
        """Stocks snapshot >10min old DURING market hours should trip
        the breaker as fail-safe — we can't trust the equity number."""
        monkeypatch.setattr(unified_risk, "_PEAK_FILE", tmp_path / "peak.json")

        old_ts = (datetime.now(timezone.utc) - timedelta(minutes=30)).isoformat()
        _seed_mocks(mock_open, mock_start, mock_unr, mock_rea, mock_stocks,
                    ts=old_ts)
        s = get_combined_risk_status()
        assert s["snapshot_age_seconds"] > STOCKS_STALE_SECONDS
        assert s["stocks_data_stale"] is True
        assert s["circuit_breaker_active"] is True

    @patch("user_data.modules.unified_risk._is_nyse_open_now", return_value=False)
    @patch("user_data.modules.unified_risk._stocks_state")
    @patch("user_data.modules.unified_risk._crypto_realised_pnl_usd")
    @patch("user_data.modules.unified_risk._crypto_unrealised_pnl_usd")
    @patch("user_data.modules.unified_risk._crypto_starting_equity")
    @patch("user_data.modules.unified_risk._crypto_open_count")
    def test_stale_snapshot_off_hours_does_not_trip(
        self, mock_open, mock_start, mock_unr, mock_rea, mock_stocks, _mock_market,
        tmp_path, monkeypatch,
    ):
        """Off-hours stale snapshot is normal (cron not firing) — should
        flag but NOT trip the breaker."""
        monkeypatch.setattr(unified_risk, "_PEAK_FILE", tmp_path / "peak.json")

        old_ts = (datetime.now(timezone.utc) - timedelta(hours=12)).isoformat()
        _seed_mocks(mock_open, mock_start, mock_unr, mock_rea, mock_stocks,
                    ts=old_ts)
        s = get_combined_risk_status()
        assert s["stocks_data_stale"] is True
        assert s["circuit_breaker_active"] is False
