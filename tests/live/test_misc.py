"""Misc coverage tests: error types + Strategy ABC defaults."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from quanta_core.observability.notifier import NullNotifier
from quanta_core.strategy.base import Strategy
from quanta_core.util.errors import (
    LateTickError,
    QuantaError,
    ReconciliationDriftError,
    StaleFeedError,
)
from quanta_core.util.types import (
    Bar,
    ClientOrderId,
    Fill,
    OrderProposal,
    Symbol,
    Tick,
    Timeframe,
    VenueOrderId,
)


def test_error_hierarchy() -> None:
    assert issubclass(StaleFeedError, QuantaError)
    assert issubclass(LateTickError, QuantaError)
    assert issubclass(ReconciliationDriftError, QuantaError)
    err = StaleFeedError("stale!")
    assert isinstance(err, QuantaError)
    assert isinstance(err, Exception)
    assert str(err) == "stale!"


class _MinimalStrategy(Strategy):
    name = "minimal"
    symbols = [Symbol("AAPL")]
    timeframes: list[Timeframe] = ["1m"]

    async def on_candle(self, bar: Bar, ctx: object) -> list[OrderProposal]:
        return []


@pytest.mark.anyio
async def test_strategy_default_hooks_are_noops() -> None:
    """The ABC defaults must return ``[]`` / ``None`` without raising."""

    s = _MinimalStrategy()
    bar = Bar(
        symbol=Symbol("AAPL"),
        timeframe="1m",
        open_ts=datetime(2026, 5, 12, 12, 0, 0, tzinfo=UTC),
        close_ts=datetime(2026, 5, 12, 12, 1, 0, tzinfo=UTC),
        open=Decimal("100"),
        high=Decimal("101"),
        low=Decimal("99"),
        close=Decimal("100"),
        volume=Decimal("10"),
        vwap=Decimal("100"),
        trades=3,
    )
    tick = Tick(
        symbol=Symbol("AAPL"),
        ts=bar.close_ts,
        price=Decimal("100"),
        size=Decimal("1"),
    )
    fill = Fill(
        symbol=Symbol("AAPL"),
        side="SELL",
        qty=Decimal("1"),
        price=Decimal("100"),
        ts=bar.close_ts,
        client_order_id=ClientOrderId("coid"),
        venue_order_id=VenueOrderId("vid"),
        venue="paper",
        fee=Decimal("0"),
    )
    # on_candle is implemented; the rest are inherited defaults.
    assert await s.on_candle(bar, None) == []
    assert await s.on_tick(tick, None) == []
    assert await s.on_fill(fill, None) == []
    # The default on_start / on_stop return None; calling them must not raise.
    await s.on_start(None)
    await s.on_stop(None)


@pytest.mark.anyio
async def test_null_notifier_swallows_alerts() -> None:
    n = NullNotifier()
    # Both calls must return without raising.
    await n.warning("subj", "body")
    await n.info("subj", "body")


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"
