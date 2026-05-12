"""Tests for ``quanta_core.live.dispatcher``."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import anyio
import pytest

from quanta_core.live.dispatcher import StrategyDispatcher
from quanta_core.strategy.base import Strategy
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

# ----- helpers -----


class _RecordingSink:
    """Captures forwarded proposals so tests can assert routing."""

    def __init__(self) -> None:
        self.forwarded: list[OrderProposal] = []
        self.raise_on_submit: BaseException | None = None

    async def submit(self, proposal: OrderProposal) -> None:
        if self.raise_on_submit is not None:
            raise self.raise_on_submit
        self.forwarded.append(proposal)


class _GoodStrategy(Strategy):
    name = "good"
    symbols = [Symbol("AAPL")]
    timeframes: list[Timeframe] = ["1m"]
    wants_ticks = True

    def __init__(self) -> None:
        self.candles_seen: list[Bar] = []
        self.ticks_seen: list[Tick] = []
        self.fills_seen: list[Fill] = []

    async def on_candle(self, bar: Bar, ctx: object) -> list[OrderProposal]:
        self.candles_seen.append(bar)
        return [_make_proposal("good", bar.symbol)]

    async def on_tick(self, tick: Tick, ctx: object) -> list[OrderProposal]:
        self.ticks_seen.append(tick)
        return []

    async def on_fill(self, fill: Fill, ctx: object) -> list[OrderProposal]:
        self.fills_seen.append(fill)
        return []


class _BadStrategy(Strategy):
    name = "bad"
    symbols = [Symbol("AAPL")]
    timeframes: list[Timeframe] = ["1m"]

    async def on_candle(self, bar: Bar, ctx: object) -> list[OrderProposal]:
        raise RuntimeError("kaboom")


class _SlowStrategy(Strategy):
    name = "slow"
    symbols = [Symbol("AAPL")]
    timeframes: list[Timeframe] = ["1m"]

    def __init__(self, sleep_seconds: float) -> None:
        self.sleep_seconds = sleep_seconds
        self.completed = False

    async def on_candle(self, bar: Bar, ctx: object) -> list[OrderProposal]:
        await anyio.sleep(self.sleep_seconds)
        self.completed = True
        return [_make_proposal("slow", bar.symbol)]


def _make_proposal(strategy_name: str, symbol: Symbol) -> OrderProposal:
    return OrderProposal(
        strategy_name=strategy_name,
        symbol=symbol,
        venue="paper",
        side="BUY",
        qty=Decimal("1"),
        order_type="market",
    )


def _bar(symbol: str, tf: Timeframe = "1m") -> Bar:
    ts = datetime(2026, 5, 12, 12, 0, 0, tzinfo=UTC)
    return Bar(
        symbol=Symbol(symbol),
        timeframe=tf,
        open_ts=ts,
        close_ts=ts,
        open=Decimal("100"),
        high=Decimal("101"),
        low=Decimal("99"),
        close=Decimal("100"),
        volume=Decimal("10"),
        vwap=Decimal("100"),
        trades=3,
    )


def _tick(symbol: str) -> Tick:
    return Tick(
        symbol=Symbol(symbol),
        ts=datetime(2026, 5, 12, 12, 0, 0, tzinfo=UTC),
        price=Decimal("100"),
        size=Decimal("1"),
    )


def _fill(symbol: str) -> Fill:
    return Fill(
        symbol=Symbol(symbol),
        side="BUY",
        qty=Decimal("1"),
        price=Decimal("100"),
        ts=datetime(2026, 5, 12, 12, 0, 0, tzinfo=UTC),
        client_order_id=ClientOrderId("coid-1"),
        venue_order_id=VenueOrderId("vid-1"),
        venue="paper",
        fee=Decimal("0"),
    )


# ----- tests -----


@pytest.mark.anyio
async def test_register_is_idempotent_on_name() -> None:
    sink = _RecordingSink()
    dispatcher = StrategyDispatcher(sink=sink)
    s = _GoodStrategy()
    dispatcher.register(s)
    dispatcher.register(s)
    assert len(dispatcher.strategies) == 1


@pytest.mark.anyio
async def test_dispatch_candle_only_to_matching_symbol_and_tf() -> None:
    sink = _RecordingSink()
    dispatcher = StrategyDispatcher(sink=sink)
    s = _GoodStrategy()
    dispatcher.register(s)
    await dispatcher.dispatch_candle(_bar("AAPL", "1m"))
    await dispatcher.dispatch_candle(_bar("MSFT", "1m"))  # wrong symbol
    await dispatcher.dispatch_candle(_bar("AAPL", "5m"))  # wrong tf
    assert len(s.candles_seen) == 1
    assert s.candles_seen[0].symbol == Symbol("AAPL")
    assert len(sink.forwarded) == 1
    assert sink.forwarded[0].strategy_name == "good"


@pytest.mark.anyio
async def test_dispatch_tick_respects_wants_ticks_flag() -> None:
    class _NoTicks(_GoodStrategy):
        name = "no_ticks"
        wants_ticks = False

    sink = _RecordingSink()
    dispatcher = StrategyDispatcher(sink=sink)
    s = _NoTicks()
    dispatcher.register(s)
    await dispatcher.dispatch_tick(_tick("AAPL"))
    assert s.ticks_seen == []


@pytest.mark.anyio
async def test_dispatch_fill_invokes_on_fill() -> None:
    sink = _RecordingSink()
    dispatcher = StrategyDispatcher(sink=sink)
    s = _GoodStrategy()
    dispatcher.register(s)
    await dispatcher.dispatch_fill(_fill("AAPL"))
    assert len(s.fills_seen) == 1


@pytest.mark.anyio
async def test_hook_exception_does_not_crash_loop() -> None:
    sink = _RecordingSink()
    dispatcher = StrategyDispatcher(sink=sink)
    bad = _BadStrategy()
    good = _GoodStrategy()
    dispatcher.register(bad)
    dispatcher.register(good)
    # Bad raises; loop must still call good after.
    await dispatcher.dispatch_candle(_bar("AAPL"))
    assert dispatcher.metrics.hook_exceptions == 1
    assert len(good.candles_seen) == 1
    assert len(sink.forwarded) == 1


@pytest.mark.anyio
async def test_budget_exceeded_drops_proposals() -> None:
    sink = _RecordingSink()
    dispatcher = StrategyDispatcher(sink=sink, budget_seconds=0.05)
    slow = _SlowStrategy(sleep_seconds=0.5)
    dispatcher.register(slow)
    await dispatcher.dispatch_candle(_bar("AAPL"))
    assert dispatcher.metrics.budget_exceeded == 1
    assert sink.forwarded == []


@pytest.mark.anyio
async def test_sink_exception_does_not_crash_dispatcher() -> None:
    sink = _RecordingSink()
    sink.raise_on_submit = RuntimeError("sink broken")
    dispatcher = StrategyDispatcher(sink=sink)
    dispatcher.register(_GoodStrategy())
    # Should NOT raise.
    await dispatcher.dispatch_candle(_bar("AAPL"))
    assert sink.forwarded == []


@pytest.mark.anyio
async def test_unregister_removes_strategy() -> None:
    sink = _RecordingSink()
    dispatcher = StrategyDispatcher(sink=sink)
    s = _GoodStrategy()
    dispatcher.register(s)
    dispatcher.unregister("good")
    assert dispatcher.strategies == ()
    await dispatcher.dispatch_candle(_bar("AAPL"))
    assert s.candles_seen == []


@pytest.mark.anyio
async def test_metrics_count_proposals_forwarded() -> None:
    sink = _RecordingSink()
    dispatcher = StrategyDispatcher(sink=sink)
    dispatcher.register(_GoodStrategy())
    await dispatcher.dispatch_candle(_bar("AAPL"))
    await dispatcher.dispatch_candle(_bar("AAPL"))
    assert dispatcher.metrics.proposals_forwarded == 2
    assert dispatcher.metrics.candles_dispatched == 2


# ----- anyio configuration -----


@pytest.fixture
def anyio_backend() -> str:
    """Run tests on the asyncio backend (no trio dep)."""

    return "asyncio"
