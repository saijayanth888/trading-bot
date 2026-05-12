"""Tests for :mod:`quanta_core.backtest.candle_source`."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pandas as pd
import pytest

from quanta_core.backtest.candle_source import (
    CandleSourceError,
    FeatherCandleSource,
    InMemoryCandleSource,
    SyntheticCandleSource,
    timeframe_to_timedelta,
)
from quanta_core.types import Bar, Symbol

# ---------------------------------------------------------------------------
# timeframe_to_timedelta
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("tf", "expected_seconds"),
    [
        ("1m", 60),
        ("5m", 300),
        ("15m", 900),
        ("1h", 3600),
        ("4h", 14400),
        ("1d", 86400),
    ],
)
def test_timeframe_to_timedelta(tf, expected_seconds):
    assert timeframe_to_timedelta(tf).total_seconds() == expected_seconds


# ---------------------------------------------------------------------------
# SyntheticCandleSource
# ---------------------------------------------------------------------------


class TestSyntheticCandleSource:
    def test_emits_requested_count(self, btc_symbol):
        src = SyntheticCandleSource(
            symbol=btc_symbol,
            timeframe="1m",
            start=datetime(2026, 1, 1, tzinfo=UTC),
            n_bars=25,
            seed=42,
        )
        bars = list(src)
        assert len(bars) == 25

    def test_deterministic_with_seed(self, btc_symbol, fixed_start):
        src_a = SyntheticCandleSource(
            symbol=btc_symbol, timeframe="1m", start=fixed_start, n_bars=10, seed=1
        )
        src_b = SyntheticCandleSource(
            symbol=btc_symbol, timeframe="1m", start=fixed_start, n_bars=10, seed=1
        )
        bars_a = list(src_a)
        bars_b = list(src_b)
        assert [b.close for b in bars_a] == [b.close for b in bars_b]

    def test_different_seeds_diverge(self, btc_symbol, fixed_start):
        src_a = SyntheticCandleSource(
            symbol=btc_symbol, timeframe="1m", start=fixed_start, n_bars=10, seed=1
        )
        src_b = SyntheticCandleSource(
            symbol=btc_symbol, timeframe="1m", start=fixed_start, n_bars=10, seed=2
        )
        assert list(src_a)[-1].close != list(src_b)[-1].close

    def test_chronological(self, synthetic_source):
        bars = list(synthetic_source)
        timestamps = [b.timestamp_utc for b in bars]
        assert timestamps == sorted(timestamps)
        # Spacing is exactly one timeframe.
        for prev, nxt in zip(timestamps[:-1], timestamps[1:], strict=True):
            assert nxt - prev == timedelta(minutes=1)

    def test_ohlcv_validity(self, synthetic_source):
        for bar in synthetic_source:
            assert bar.high >= bar.low
            assert bar.low <= bar.open <= bar.high
            assert bar.low <= bar.close <= bar.high
            assert bar.volume > 0

    def test_naive_start_rejected(self, btc_symbol):
        with pytest.raises(CandleSourceError, match="timezone-aware"):
            SyntheticCandleSource(
                symbol=btc_symbol,
                timeframe="1m",
                start=datetime(2026, 1, 1),  # naive
                n_bars=10,
            )

    def test_zero_bars_rejected(self, btc_symbol, fixed_start):
        with pytest.raises(ValueError, match="positive"):
            SyntheticCandleSource(symbol=btc_symbol, timeframe="1m", start=fixed_start, n_bars=0)

    def test_negative_volatility_rejected(self, btc_symbol, fixed_start):
        with pytest.raises(ValueError, match="non-negative"):
            SyntheticCandleSource(
                symbol=btc_symbol,
                timeframe="1m",
                start=fixed_start,
                n_bars=10,
                volatility=Decimal("-1"),
            )

    def test_slice_includes_only_window(self, btc_symbol, fixed_start):
        src = SyntheticCandleSource(symbol=btc_symbol, timeframe="1m", start=fixed_start, n_bars=20)
        lo = fixed_start + timedelta(minutes=5)
        hi = fixed_start + timedelta(minutes=15)
        clipped = list(src.slice(lo, hi))
        # 10 bars in [05, 15) — bar at minute 5 is included; bar at minute 15 is excluded.
        assert len(clipped) == 10
        assert clipped[0].timestamp_utc == lo
        assert clipped[-1].timestamp_utc == fixed_start + timedelta(minutes=14)

    def test_slice_naive_bounds_rejected(self, synthetic_source):
        with pytest.raises(CandleSourceError, match="timezone-aware"):
            list(synthetic_source.slice(datetime(2026, 1, 1), datetime(2026, 1, 2)))

    def test_slice_inverted_window_rejected(self, synthetic_source, fixed_start):
        with pytest.raises(CandleSourceError, match="after start"):
            list(synthetic_source.slice(fixed_start + timedelta(minutes=5), fixed_start))


# ---------------------------------------------------------------------------
# FeatherCandleSource
# ---------------------------------------------------------------------------


def _write_feather(
    path: Path,
    *,
    n: int = 20,
    start: datetime,
    tf_minutes: int = 1,
) -> None:
    """Write a small OHLCV feather file at ``path``."""
    rows = []
    base = float(100)
    for i in range(n):
        ts = start + timedelta(minutes=tf_minutes * i)
        o = base + i * 0.1
        c = o + 0.05
        rows.append(
            {
                "date": ts,
                "open": o,
                "high": o + 0.5,
                "low": o - 0.2,
                "close": c,
                "volume": 100.0 + i,
            }
        )
    df = pd.DataFrame(rows)
    df.to_feather(path)


class TestFeatherCandleSource:
    def test_reads_feather(self, tmp_path: Path, btc_symbol, fixed_start):
        root = tmp_path / "coinbase"
        root.mkdir()
        _write_feather(root / "BTC_USD-1m.feather", n=10, start=fixed_start)
        src = FeatherCandleSource(symbol=btc_symbol, timeframe="1m", root=root)
        bars = list(src)
        assert len(bars) == 10
        assert all(isinstance(b, Bar) for b in bars)
        assert bars[0].timestamp_utc == fixed_start
        assert bars[-1].timestamp_utc == fixed_start + timedelta(minutes=9)

    def test_reads_parquet_fallback(self, tmp_path: Path, btc_symbol, fixed_start):
        root = tmp_path / "coinbase"
        root.mkdir()
        # Build a small DataFrame and write parquet only.
        rows = [
            {
                "date": fixed_start + timedelta(minutes=i),
                "open": 50.0 + i,
                "high": 51.0 + i,
                "low": 49.0 + i,
                "close": 50.5 + i,
                "volume": 10.0,
            }
            for i in range(5)
        ]
        pd.DataFrame(rows).to_parquet(root / "BTC_USD-1m.parquet")
        src = FeatherCandleSource(symbol=btc_symbol, timeframe="1m", root=root)
        bars = list(src)
        assert len(bars) == 5

    def test_missing_root_raises(self, tmp_path: Path, btc_symbol):
        with pytest.raises(CandleSourceError, match="does not exist"):
            FeatherCandleSource(symbol=btc_symbol, timeframe="1m", root=tmp_path / "missing")

    def test_missing_file_raises(self, tmp_path: Path, btc_symbol):
        root = tmp_path / "coinbase"
        root.mkdir()
        with pytest.raises(CandleSourceError, match="no OHLCV file"):
            FeatherCandleSource(symbol=btc_symbol, timeframe="1m", root=root)

    def test_missing_columns_raises(self, tmp_path: Path, btc_symbol, fixed_start):
        root = tmp_path / "coinbase"
        root.mkdir()
        # Write a feather file without the 'volume' column.
        df = pd.DataFrame(
            [
                {
                    "date": fixed_start,
                    "open": 1.0,
                    "high": 2.0,
                    "low": 0.5,
                    "close": 1.5,
                }
            ]
        )
        df.to_feather(root / "BTC_USD-1m.feather")
        src = FeatherCandleSource(symbol=btc_symbol, timeframe="1m", root=root)
        with pytest.raises(CandleSourceError, match="missing columns"):
            list(src)

    def test_duplicate_timestamps_deduped(self, tmp_path: Path, btc_symbol, fixed_start):
        root = tmp_path / "coinbase"
        root.mkdir()
        rows = [
            {
                "date": fixed_start,
                "open": 1.0,
                "high": 2.0,
                "low": 0.5,
                "close": 1.5,
                "volume": 10.0,
            },
            {
                "date": fixed_start,  # duplicate
                "open": 1.1,
                "high": 2.1,
                "low": 0.6,
                "close": 1.6,
                "volume": 11.0,
            },
            {
                "date": fixed_start + timedelta(minutes=1),
                "open": 1.2,
                "high": 2.2,
                "low": 0.7,
                "close": 1.7,
                "volume": 12.0,
            },
        ]
        pd.DataFrame(rows).to_feather(root / "BTC_USD-1m.feather")
        src = FeatherCandleSource(symbol=btc_symbol, timeframe="1m", root=root)
        bars = list(src)
        assert len(bars) == 2
        # Dedup keeps the *last* of the duplicate group; that's the open=1.1 row.
        assert bars[0].open == Decimal("1.1")

    def test_dashed_symbol_layout(self, tmp_path: Path, fixed_start):
        root = tmp_path / "alpaca"
        root.mkdir()
        _write_feather(root / "AAPL-1d.feather", n=3, start=fixed_start, tf_minutes=1440)
        src = FeatherCandleSource(symbol=Symbol("AAPL"), timeframe="1d", root=root)
        assert len(list(src)) == 3

    def test_slice_clips_window(self, tmp_path: Path, btc_symbol, fixed_start):
        root = tmp_path / "coinbase"
        root.mkdir()
        _write_feather(root / "BTC_USD-1m.feather", n=20, start=fixed_start)
        src = FeatherCandleSource(symbol=btc_symbol, timeframe="1m", root=root)
        clipped = list(
            src.slice(
                fixed_start + timedelta(minutes=3),
                fixed_start + timedelta(minutes=8),
            )
        )
        assert len(clipped) == 5
        assert clipped[0].timestamp_utc == fixed_start + timedelta(minutes=3)

    def test_handles_numpy_datetime64_via_coerce_utc(self):
        """`_coerce_utc` handles ``numpy.datetime64`` instances (non-datetime)."""
        import numpy as np

        from quanta_core.backtest.candle_source import _coerce_utc

        v = np.datetime64("2026-05-12T12:00:00")
        ts = _coerce_utc(v)
        assert ts.tzinfo is UTC
        assert ts.year == 2026
        assert ts.month == 5
        assert ts.hour == 12

    def test_handles_tz_aware_datetime_via_coerce_utc(self):
        from datetime import timezone

        from quanta_core.backtest.candle_source import _coerce_utc

        # Naive ET-equivalent — represented as UTC-5 fixed offset.
        v = datetime(2026, 5, 12, 9, 0, tzinfo=timezone(timedelta(hours=-5)))
        ts = _coerce_utc(v)
        assert ts.tzinfo is UTC
        assert ts.hour == 14  # 9 EST -> 14 UTC


# ---------------------------------------------------------------------------
# InMemoryCandleSource
# ---------------------------------------------------------------------------


class TestInMemoryCandleSource:
    def test_iterates_in_order(self, btc_symbol, make_bar, fixed_start):
        bars = [
            make_bar(symbol=btc_symbol, ts=fixed_start + timedelta(minutes=i)) for i in range(3)
        ]
        src = InMemoryCandleSource(bars)
        assert list(src) == bars
        assert src.bars == tuple(bars)
        assert src.symbol == btc_symbol
        assert src.timeframe == "1m"

    def test_empty_rejected(self):
        with pytest.raises(CandleSourceError, match="at least one"):
            InMemoryCandleSource([])

    def test_mixed_symbols_rejected(self, btc_symbol, eth_symbol, make_bar, fixed_start):
        bars = [
            make_bar(symbol=btc_symbol, ts=fixed_start),
            make_bar(symbol=eth_symbol, ts=fixed_start + timedelta(minutes=1)),
        ]
        with pytest.raises(CandleSourceError, match="share one symbol"):
            InMemoryCandleSource(bars)

    def test_mixed_timeframes_rejected(self, btc_symbol, make_bar, fixed_start):
        bars = [
            make_bar(symbol=btc_symbol, ts=fixed_start, timeframe="1m"),
            make_bar(
                symbol=btc_symbol,
                ts=fixed_start + timedelta(hours=1),
                timeframe="1h",
            ),
        ]
        with pytest.raises(CandleSourceError, match="share one timeframe"):
            InMemoryCandleSource(bars)

    def test_unsorted_rejected(self, btc_symbol, make_bar, fixed_start):
        bars = [
            make_bar(symbol=btc_symbol, ts=fixed_start + timedelta(minutes=2)),
            make_bar(symbol=btc_symbol, ts=fixed_start + timedelta(minutes=1)),
        ]
        with pytest.raises(CandleSourceError, match="strictly chronological"):
            InMemoryCandleSource(bars)

    def test_slice_via_base(self, btc_symbol, make_bar, fixed_start):
        bars = [
            make_bar(symbol=btc_symbol, ts=fixed_start + timedelta(minutes=i)) for i in range(6)
        ]
        src = InMemoryCandleSource(bars)
        clipped = list(
            src.slice(fixed_start + timedelta(minutes=2), fixed_start + timedelta(minutes=5))
        )
        assert [b.timestamp_utc for b in clipped] == [
            fixed_start + timedelta(minutes=2),
            fixed_start + timedelta(minutes=3),
            fixed_start + timedelta(minutes=4),
        ]
