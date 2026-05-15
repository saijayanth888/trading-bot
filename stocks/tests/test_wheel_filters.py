"""
Unit tests for wheel.filters — earnings blackout and IV-rank filters.

Pure-function tests: no Alpaca, no live network.  yfinance calls are
monkey-patched.  Fast (< 200 ms).

Run from stocks/:
    source venv/bin/activate && pytest tests/test_wheel_filters.py -v
"""

from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from wheel.filters import earnings_blackout, iv_rank_filter


# ── earnings_blackout ──────────────────────────────────────────────────────


class TestEarningsBlackout:
    """Test earnings_blackout() with mocked yfinance and static file."""

    def _mock_yf_no_earnings(self):
        """yfinance returns no earnings dates."""
        ticker = MagicMock()
        ticker.earnings_dates = None
        return ticker

    def _mock_yf_earnings_on(self, d: date):
        """yfinance returns a single future earnings date."""
        import pandas as pd
        ticker = MagicMock()
        idx = pd.DatetimeIndex([str(d)])
        df = pd.DataFrame({"Reported EPS": [float("nan")]}, index=idx)
        ticker.earnings_dates = df
        return ticker

    # ── Basic calendar proximity gates ─────────────────────────────────────

    def test_blocked_when_earnings_5_days_away_threshold_7(self):
        """Earnings 5 days away, 7-day blackout → blocked."""
        today = date(2026, 5, 15)
        earnings = today + timedelta(days=5)
        with patch("wheel.filters.yf") as mock_yf:
            mock_yf.Ticker.return_value = self._mock_yf_earnings_on(earnings)
            blocked, reason = earnings_blackout("NVDA", target_dte=10, today=today, blackout_days=7)
        assert blocked is True
        assert "earnings blackout" in reason
        assert "NVDA" in reason

    def test_not_blocked_when_earnings_30_days_away_threshold_7(self):
        """Earnings 30 days away, 7-day blackout, 10-DTE option → not blocked."""
        today = date(2026, 5, 15)
        earnings = today + timedelta(days=30)
        with patch("wheel.filters.yf") as mock_yf:
            mock_yf.Ticker.return_value = self._mock_yf_earnings_on(earnings)
            blocked, reason = earnings_blackout("SOFI", target_dte=10, today=today, blackout_days=7)
        assert blocked is False

    def test_blocked_when_earnings_exactly_on_boundary(self):
        """Earnings exactly 7 days away → blocked (boundary inclusive)."""
        today = date(2026, 5, 15)
        earnings = today + timedelta(days=7)
        with patch("wheel.filters.yf") as mock_yf:
            mock_yf.Ticker.return_value = self._mock_yf_earnings_on(earnings)
            blocked, reason = earnings_blackout("AMD", target_dte=5, today=today, blackout_days=7)
        assert blocked is True

    def test_not_blocked_when_earnings_8_days_away_threshold_7(self):
        """Earnings 8 days away, 7-day blackout, 5-DTE option → not blocked."""
        today = date(2026, 5, 15)
        earnings = today + timedelta(days=8)
        with patch("wheel.filters.yf") as mock_yf:
            mock_yf.Ticker.return_value = self._mock_yf_earnings_on(earnings)
            blocked, reason = earnings_blackout("AMD", target_dte=5, today=today, blackout_days=7)
        assert blocked is False

    # ── DTE span gate: blocked if option expires after earnings ───────────

    def test_blocked_when_option_expires_after_earnings(self):
        """Earnings 12 days away but 15-DTE option spans it → blocked."""
        today = date(2026, 5, 15)
        earnings = today + timedelta(days=12)
        with patch("wheel.filters.yf") as mock_yf:
            mock_yf.Ticker.return_value = self._mock_yf_earnings_on(earnings)
            blocked, reason = earnings_blackout("TSLA", target_dte=15, today=today, blackout_days=7)
        assert blocked is True
        assert "DTE window" in reason or "within" in reason

    def test_not_blocked_when_option_expires_before_earnings(self):
        """Earnings 12 days away, 10-DTE option expires before it → not blocked."""
        today = date(2026, 5, 15)
        earnings = today + timedelta(days=12)
        with patch("wheel.filters.yf") as mock_yf:
            mock_yf.Ticker.return_value = self._mock_yf_earnings_on(earnings)
            blocked, reason = earnings_blackout("TSLA", target_dte=10, today=today, blackout_days=7)
        assert blocked is False

    # ── yfinance failure / fallback paths ──────────────────────────────────

    def test_falls_back_to_static_file_when_yfinance_fails(self, tmp_path):
        """yfinance raises → reads earnings.json → blocks correctly."""
        # Write a temporary earnings.json
        earnings_date = date.today() + timedelta(days=3)
        earnings_json = tmp_path / "earnings.json"
        earnings_json.write_text(f'{{"PLTR": "{earnings_date.isoformat()}"}}')

        with patch("wheel.filters.yf") as mock_yf:
            mock_yf.Ticker.side_effect = Exception("network error")
            # Patch the static file path to our temp file
            with patch("wheel.filters._EARNINGS_FILE", earnings_json):
                blocked, reason = earnings_blackout(
                    "PLTR", target_dte=10, today=date.today(), blackout_days=7
                )
        assert blocked is True
        assert "earnings.json" in reason

    def test_not_blocked_when_no_earnings_on_file(self):
        """yfinance returns nothing, static file has no entry → not blocked."""
        with patch("wheel.filters.yf") as mock_yf:
            mock_yf.Ticker.return_value = self._mock_yf_no_earnings()
            with patch("wheel.filters._EARNINGS_FILE", Path("/nonexistent/path/earnings.json")):
                blocked, reason = earnings_blackout(
                    "COIN", target_dte=10, today=date.today(), blackout_days=7
                )
        assert blocked is False

    def test_past_earnings_not_blocked(self, tmp_path):
        """Earnings date in the past → not blocked (static file)."""
        yesterday = date.today() - timedelta(days=1)
        earnings_json = tmp_path / "earnings.json"
        earnings_json.write_text(f'{{"MSTR": "{yesterday.isoformat()}"}}')

        with patch("wheel.filters.yf") as mock_yf:
            mock_yf.Ticker.return_value = self._mock_yf_no_earnings()
            with patch("wheel.filters._EARNINGS_FILE", earnings_json):
                blocked, reason = earnings_blackout(
                    "MSTR", target_dte=10, today=date.today(), blackout_days=7
                )
        assert blocked is False


# ── iv_rank_filter ─────────────────────────────────────────────────────────


class TestIVRankFilter:
    """Test iv_rank_filter() with mocked yfinance + numpy."""

    def _make_ticker_mock(self, spot, put_iv, call_iv, hv_series_values):
        """Build a complete yfinance Ticker mock.

        Args:
            spot:             Current stock price.
            put_iv:           Implied vol on the ATM put.
            call_iv:          Implied vol on the ATM call.
            hv_series_values: List of floats representing daily closing prices.
        """
        import pandas as pd
        import numpy as np

        ticker = MagicMock()
        ticker.options = ("2026-06-20",)  # one upcoming expiry

        # Build option chain DataFrames with one ATM strike
        strike = round(spot)
        puts_df = pd.DataFrame({
            "strike": [strike],
            "impliedVolatility": [put_iv],
        })
        calls_df = pd.DataFrame({
            "strike": [strike],
            "impliedVolatility": [call_iv],
        })
        chain = MagicMock()
        chain.puts = puts_df
        chain.calls = calls_df
        ticker.option_chain.return_value = chain

        # fast_info
        ticker.fast_info = {"lastPrice": spot}

        # Price history: enough days for HV calculation
        n = len(hv_series_values)
        idx = pd.date_range("2025-01-01", periods=n, freq="B")
        hist_df = pd.DataFrame({"Close": hv_series_values}, index=idx)
        ticker.history.return_value = hist_df

        return ticker

    def test_pass_when_ivr_above_threshold(self):
        """IVR clearly above 35 → filter passes."""
        import numpy as np

        # Create a HV series where current_iv is at 70% of its range
        # HV low ≈ 0.20, HV high ≈ 0.60, current straddle IV = 0.48
        # IVR = (0.48 - 0.20) / (0.60 - 0.20) * 100 = 70
        # To produce HV ≈ 0.20–0.60 range we need enough price history.
        # Shortcut: use a large dataset with varying volatility.
        np.random.seed(42)
        # Prices that produce 30-day rolling HV roughly in [0.20, 0.60]
        n = 300
        low_vol_returns = np.random.normal(0, 0.012, 200)  # ~19% annualised
        high_vol_returns = np.random.normal(0, 0.038, 100)  # ~60% annualised
        all_returns = np.concatenate([low_vol_returns, high_vol_returns])
        prices = [100.0]
        for r in all_returns:
            prices.append(prices[-1] * (1 + r))

        spot = prices[-1]
        put_iv = 0.48   # high — straddle says high IV
        call_iv = 0.46

        with patch("wheel.filters.yf") as mock_yf:
            mock_yf.Ticker.return_value = self._make_ticker_mock(
                spot, put_iv, call_iv, prices
            )
            passing, reason = iv_rank_filter("NVDA", threshold=35.0)

        assert passing is True
        assert "IVR" in reason

    def test_block_when_ivr_below_threshold(self):
        """IVR clearly below 35 → filter blocks."""
        import numpy as np

        # Prices that produce consistently high HV but current_iv is low
        # HV range: [0.30, 0.70]; current straddle IV = 0.25 → IVR < 0
        np.random.seed(7)
        high_vol_returns = np.random.normal(0, 0.038, 300)
        prices = [100.0]
        for r in high_vol_returns:
            prices.append(prices[-1] * (1 + r))

        spot = prices[-1]
        put_iv = 0.08   # very low — dead market
        call_iv = 0.08

        with patch("wheel.filters.yf") as mock_yf:
            mock_yf.Ticker.return_value = self._make_ticker_mock(
                spot, put_iv, call_iv, prices
            )
            passing, reason = iv_rank_filter("SPY", threshold=35.0)

        assert passing is False
        assert "IVR" in reason
        assert "threshold" in reason

    def test_skips_gracefully_on_network_error(self):
        """yfinance raises an exception → filter passes (fail-open)."""
        with patch("wheel.filters.yf") as mock_yf:
            mock_yf.Ticker.side_effect = Exception("connection refused")
            passing, reason = iv_rank_filter("SOFI")
        # Should PASS (fail open) rather than blocking all entries
        assert passing is True
        assert "skipped" in reason.lower()

    def test_skips_gracefully_when_no_options_chain(self):
        """Ticker has no options → filter passes (fail-open)."""
        with patch("wheel.filters.yf") as mock_yf:
            ticker = MagicMock()
            ticker.options = ()  # empty tuple
            mock_yf.Ticker.return_value = ticker
            passing, reason = iv_rank_filter("HOOD")
        assert passing is True
        assert "skipped" in reason.lower()

    def test_skips_when_insufficient_history(self):
        """Only 10 days of price history → not enough for HV → skip (pass)."""
        import pandas as pd

        spot = 50.0
        put_iv = 0.40
        call_iv = 0.38

        with patch("wheel.filters.yf") as mock_yf:
            ticker = MagicMock()
            ticker.options = ("2026-06-20",)
            strike = round(spot)
            puts_df = pd.DataFrame({
                "strike": [strike],
                "impliedVolatility": [put_iv],
            })
            calls_df = pd.DataFrame({
                "strike": [strike],
                "impliedVolatility": [call_iv],
            })
            chain = MagicMock()
            chain.puts = puts_df
            chain.calls = calls_df
            ticker.option_chain.return_value = chain
            ticker.fast_info = {"lastPrice": spot}
            # Only 10 days of history — fewer than min 30
            idx = pd.date_range("2026-05-01", periods=10, freq="B")
            ticker.history.return_value = pd.DataFrame(
                {"Close": [50.0 + i * 0.1 for i in range(10)]}, index=idx
            )
            mock_yf.Ticker.return_value = ticker
            passing, reason = iv_rank_filter("MARA")

        assert passing is True
        assert "skipped" in reason.lower()

    def test_custom_threshold_respected(self):
        """A threshold of 0 means every non-zero IVR passes."""
        import numpy as np

        np.random.seed(1)
        returns = np.random.normal(0, 0.025, 300)
        prices = [100.0]
        for r in returns:
            prices.append(prices[-1] * (1 + r))

        spot = prices[-1]
        put_iv = 0.25
        call_iv = 0.25

        with patch("wheel.filters.yf") as mock_yf:
            mock_yf.Ticker.return_value = self._make_ticker_mock(
                spot, put_iv, call_iv, prices
            )
            passing, _ = iv_rank_filter("QQQ", threshold=0.0)

        assert passing is True
