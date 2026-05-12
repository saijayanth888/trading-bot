"""Tests for ``quanta_core.live.tick_aggregator``."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from quanta_core.live.tick_aggregator import TickAggregator
from quanta_core.util.types import Symbol, Tick


def _tick(
    symbol: str,
    ts: datetime,
    price: str,
    size: str = "1",
) -> Tick:
    return Tick(
        symbol=Symbol(symbol),
        ts=ts,
        price=Decimal(price),
        size=Decimal(size),
    )


def test_rejects_unsupported_timeframe() -> None:
    with pytest.raises(ValueError, match="unsupported timeframe"):
        TickAggregator(symbol=Symbol("BTC/USD"), timeframes=["3m"])  # type: ignore[list-item]


def test_rejects_naive_timestamp() -> None:
    agg = TickAggregator(symbol=Symbol("BTC/USD"), timeframes=["1m"])
    naive_ts = datetime(2026, 5, 12, 12, 0, 0)
    bad_tick = Tick(
        symbol=Symbol("BTC/USD"),
        ts=naive_ts,
        price=Decimal("100"),
        size=Decimal("1"),
    )
    with pytest.raises(ValueError, match="tz-aware"):
        agg.on_tick(bad_tick)


def test_rejects_symbol_mismatch() -> None:
    agg = TickAggregator(symbol=Symbol("BTC/USD"), timeframes=["1m"])
    ts = datetime(2026, 5, 12, 12, 0, 0, tzinfo=UTC)
    with pytest.raises(ValueError, match="aggregator"):
        agg.on_tick(_tick("ETH/USD", ts, "100"))


def test_first_tick_opens_bar_but_emits_nothing() -> None:
    agg = TickAggregator(symbol=Symbol("AAPL"), timeframes=["1m"])
    ts = datetime(2026, 5, 12, 12, 0, 30, tzinfo=UTC)
    bars = agg.on_tick(_tick("AAPL", ts, "100", "10"))
    assert bars == []


def test_boundary_crossing_emits_one_bar() -> None:
    agg = TickAggregator(symbol=Symbol("AAPL"), timeframes=["1m"])
    open_ts = datetime(2026, 5, 12, 12, 0, 0, tzinfo=UTC)
    # Three ticks inside [12:00, 12:01) — open, mid, late
    bars = agg.on_tick(_tick("AAPL", open_ts + timedelta(seconds=5), "100", "10"))
    assert bars == []
    bars = agg.on_tick(_tick("AAPL", open_ts + timedelta(seconds=30), "105", "20"))
    assert bars == []
    bars = agg.on_tick(_tick("AAPL", open_ts + timedelta(seconds=55), "102", "5"))
    assert bars == []
    # Tick at 12:01:02 crosses the boundary and closes the bar.
    boundary_cross = open_ts + timedelta(minutes=1, seconds=2)
    bars = agg.on_tick(_tick("AAPL", boundary_cross, "110", "1"))
    assert len(bars) == 1
    bar = bars[0]
    assert bar.symbol == Symbol("AAPL")
    assert bar.timeframe == "1m"
    assert bar.open_ts == open_ts
    assert bar.close_ts == open_ts + timedelta(minutes=1)
    assert bar.open == Decimal("100")
    assert bar.high == Decimal("105")
    assert bar.low == Decimal("100")
    # close is the last price observed *inside* the closed window.
    assert bar.close == Decimal("102")
    assert bar.volume == Decimal("35")
    # VWAP = (100*10 + 105*20 + 102*5) / 35 = 3610/35
    expected_vwap = Decimal("3610") / Decimal("35")
    assert bar.vwap == expected_vwap
    assert bar.trades == 3


def test_multiple_timeframes_align_to_their_own_boundary() -> None:
    agg = TickAggregator(symbol=Symbol("BTC/USD"), timeframes=["1m", "5m"])
    base = datetime(2026, 5, 12, 12, 0, 0, tzinfo=UTC)
    # Tick inside the first 1m window.
    agg.on_tick(_tick("BTC/USD", base + timedelta(seconds=10), "30000", "1"))
    # Crosses the first 1m boundary but not the 5m.
    bars = agg.on_tick(_tick("BTC/USD", base + timedelta(minutes=1, seconds=5), "30100", "1"))
    timeframes = {b.timeframe for b in bars}
    assert timeframes == {"1m"}
    # Feed one tick inside each of the next three 1m windows.
    for minute in (2, 3, 4):
        agg.on_tick(
            _tick("BTC/USD", base + timedelta(minutes=minute, seconds=10), "30100", "1"),
        )
    # A tick at 12:05:10 crosses BOTH boundaries: the 4-5m 1m bar and the
    # 0-5m 5m bar.
    bars = agg.on_tick(_tick("BTC/USD", base + timedelta(minutes=5, seconds=10), "30200", "1"))
    timeframes = {b.timeframe for b in bars}
    assert timeframes == {"1m", "5m"}


def test_late_tick_is_counted_and_dropped() -> None:
    agg = TickAggregator(symbol=Symbol("AAPL"), timeframes=["1m"])
    open_ts = datetime(2026, 5, 12, 12, 1, 30, tzinfo=UTC)
    agg.on_tick(_tick("AAPL", open_ts, "100", "1"))
    # Tick stamped 5 minutes earlier — late, must be dropped.
    late_ts = open_ts - timedelta(minutes=5)
    bars = agg.on_tick(_tick("AAPL", late_ts, "99", "1"))
    assert bars == []
    assert agg.late_tick_count == 1


def test_vwap_falls_back_to_last_price_when_volume_zero() -> None:
    """Zero-volume ticks (size=0) should yield VWAP == last price."""

    agg = TickAggregator(symbol=Symbol("AAPL"), timeframes=["1m"])
    open_ts = datetime(2026, 5, 12, 12, 0, 0, tzinfo=UTC)
    agg.on_tick(_tick("AAPL", open_ts + timedelta(seconds=10), "100", "0"))
    agg.on_tick(_tick("AAPL", open_ts + timedelta(seconds=30), "101", "0"))
    bars = agg.on_tick(_tick("AAPL", open_ts + timedelta(minutes=1, seconds=2), "200", "0"))
    assert len(bars) == 1
    bar = bars[0]
    assert bar.volume == Decimal("0")
    assert bar.vwap == bar.close == Decimal("101")


def test_idle_period_skipping_boundaries_still_emits_at_most_one_bar() -> None:
    """The aggregator emits ONE bar even if the next tick is hours away.

    This is intentional: we do not extrapolate. A long idle period closes
    the in-progress bar at its actual boundary and the new tick opens a
    fresh bar at its own (much later) boundary.
    """

    agg = TickAggregator(symbol=Symbol("BTC/USD"), timeframes=["1m"])
    open_ts = datetime(2026, 5, 12, 12, 0, 0, tzinfo=UTC)
    agg.on_tick(_tick("BTC/USD", open_ts, "30000", "1"))
    # Next tick four hours later
    bars = agg.on_tick(
        _tick("BTC/USD", open_ts + timedelta(hours=4), "30500", "1"),
    )
    assert len(bars) == 1
    closed = bars[0]
    assert closed.open_ts == open_ts
    assert closed.close_ts == open_ts + timedelta(minutes=1)


def test_low_tracks_minimum_within_window() -> None:
    """A tick below the current low must update the bar's ``low`` field."""

    agg = TickAggregator(symbol=Symbol("AAPL"), timeframes=["1m"])
    open_ts = datetime(2026, 5, 12, 12, 0, 0, tzinfo=UTC)
    agg.on_tick(_tick("AAPL", open_ts + timedelta(seconds=5), "100", "1"))
    agg.on_tick(_tick("AAPL", open_ts + timedelta(seconds=10), "95", "1"))
    bars = agg.on_tick(
        _tick("AAPL", open_ts + timedelta(minutes=1, seconds=2), "98", "1"),
    )
    assert len(bars) == 1
    assert bars[0].low == Decimal("95")
    assert bars[0].high == Decimal("100")


def test_boundary_alignment_to_utc_epoch() -> None:
    """Boundaries align to the UTC epoch, not to the first tick."""

    agg = TickAggregator(symbol=Symbol("BTC/USD"), timeframes=["5m"])
    # Tick at 12:03:17 should belong to the [12:00, 12:05) window.
    ts = datetime(2026, 5, 12, 12, 3, 17, tzinfo=UTC)
    agg.on_tick(_tick("BTC/USD", ts, "30000", "1"))
    # Crossing tick at 12:05:00 closes the bar.
    bars = agg.on_tick(
        _tick(
            "BTC/USD",
            datetime(2026, 5, 12, 12, 5, 0, tzinfo=UTC),
            "30100",
            "1",
        ),
    )
    assert len(bars) == 1
    bar = bars[0]
    assert bar.open_ts == datetime(2026, 5, 12, 12, 0, 0, tzinfo=UTC)
    assert bar.close_ts == datetime(2026, 5, 12, 12, 5, 0, tzinfo=UTC)
