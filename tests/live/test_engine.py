"""Tests for ``quanta_core.live.engine``.

These tests use a synthetic in-process Exchange to drive the engine without
any network I/O. The exchange emits a scripted sequence of StreamEvents,
then awaits closure.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import anyio
import anyio.lowlevel
import pytest

from quanta_core.exchanges.base import Exchange, ExchangeStream, StreamEvent
from quanta_core.live.engine import EngineConfig, LiveEngine
from quanta_core.strategy.base import Strategy
from quanta_core.util.types import (
    Bar,
    ClientOrderId,
    Fill,
    OrderProposal,
    Position,
    Symbol,
    Tick,
    Timeframe,
    VenueOrderId,
)

# ----- test doubles -----


class _FakeStream(ExchangeStream):
    def __init__(self, events: list[StreamEvent]) -> None:
        self._events = events
        self._close_event = anyio.Event()
        self.closed = False

    def __aiter__(self) -> AsyncIterator[StreamEvent]:
        return self._iter()

    async def _iter(self) -> AsyncIterator[StreamEvent]:
        for evt in self._events:
            if self._close_event.is_set():
                return
            yield evt
            await anyio.lowlevel.checkpoint()
        # Stay idle until ``aclose`` is called or the surrounding task group
        # cancels us — whichever comes first.
        await self._close_event.wait()

    async def aclose(self) -> None:
        self.closed = True
        self._close_event.set()


class _FakeExchange(Exchange):
    name = "paper"

    def __init__(
        self,
        events: list[StreamEvent],
        positions: list[Position] | None = None,
    ) -> None:
        self._events = events
        self._positions = positions or []
        self.opened = False
        self.closed = False
        self._stream: _FakeStream | None = None

    async def open(self) -> ExchangeStream:
        self.opened = True
        self._stream = _FakeStream(self._events)
        return self._stream

    async def list_positions(self) -> list[Position]:
        return list(self._positions)

    async def close(self) -> None:
        self.closed = True


class _RecordingSink:
    def __init__(self) -> None:
        self.forwarded: list[OrderProposal] = []

    async def submit(self, proposal: OrderProposal) -> None:
        self.forwarded.append(proposal)


class _RecordingNotifier:
    def __init__(self) -> None:
        self.warnings: list[tuple[str, str]] = []

    async def warning(self, subject: str, body: str) -> None:
        self.warnings.append((subject, body))

    async def info(self, subject: str, body: str) -> None:
        pass


class _BarRecordingStrategy(Strategy):
    name = "test_strat"
    symbols = [Symbol("AAPL")]
    timeframes: list[Timeframe] = ["1m"]

    def __init__(self) -> None:
        self.bars: list[Bar] = []
        self.fills: list[Fill] = []

    async def on_candle(self, bar: Bar, ctx: object) -> list[OrderProposal]:
        self.bars.append(bar)
        return [
            OrderProposal(
                strategy_name=self.name,
                symbol=bar.symbol,
                venue="paper",
                side="BUY",
                qty=Decimal("1"),
                order_type="market",
            ),
        ]

    async def on_fill(self, fill: Fill, ctx: object) -> list[OrderProposal]:
        self.fills.append(fill)
        return []


def _tick(symbol: str, ts: datetime, price: str = "100", size: str = "1") -> Tick:
    return Tick(
        symbol=Symbol(symbol),
        ts=ts,
        price=Decimal(price),
        size=Decimal(size),
    )


def _fill(symbol: str) -> Fill:
    return Fill(
        symbol=Symbol(symbol),
        side="BUY",
        qty=Decimal("2"),
        price=Decimal("100"),
        ts=datetime(2026, 5, 12, 12, 5, 0, tzinfo=UTC),
        client_order_id=ClientOrderId("coid-1"),
        venue_order_id=VenueOrderId("vid-1"),
        venue="paper",
        fee=Decimal("0.01"),
    )


# ----- tests -----


@pytest.mark.anyio
async def test_engine_routes_ticks_into_bars_into_strategy(tmp_path: Path) -> None:
    base = datetime(2026, 5, 12, 12, 0, 0, tzinfo=UTC)
    events = [
        StreamEvent(tick=_tick("AAPL", base + timedelta(seconds=5), "100", "10")),
        StreamEvent(tick=_tick("AAPL", base + timedelta(seconds=30), "101", "5")),
        # Crosses 1m boundary, closes the first bar.
        StreamEvent(tick=_tick("AAPL", base + timedelta(minutes=1, seconds=2), "102", "1")),
    ]
    exchange = _FakeExchange(events)
    sink = _RecordingSink()
    cfg = EngineConfig(
        symbols=[Symbol("AAPL")],
        timeframes=["1m"],
        anomaly_path=tmp_path / "anomalies.jsonl",
        reconciler_interval_seconds=10.0,
        heartbeat_seconds=10.0,
    )
    engine = LiveEngine(exchange, cfg, sink=sink, notifier=_RecordingNotifier())
    strategy = _BarRecordingStrategy()
    engine.register([strategy])

    async def _run_then_stop() -> None:
        await anyio.sleep(0.2)
        engine.request_stop()

    async with anyio.create_task_group() as tg:
        tg.start_soon(engine.run)
        tg.start_soon(_run_then_stop)

    assert len(strategy.bars) == 1
    bar = strategy.bars[0]
    assert bar.open == Decimal("100")
    assert bar.high == Decimal("101")
    assert bar.low == Decimal("100")
    assert bar.close == Decimal("101")
    assert len(sink.forwarded) == 1
    assert exchange.opened
    assert exchange.closed


@pytest.mark.anyio
async def test_engine_applies_fill_delta_and_dispatches_on_fill(tmp_path: Path) -> None:
    events = [
        StreamEvent(fill=_fill("AAPL")),
    ]
    exchange = _FakeExchange(events)
    sink = _RecordingSink()
    cfg = EngineConfig(
        symbols=[Symbol("AAPL")],
        timeframes=["1m"],
        anomaly_path=tmp_path / "anomalies.jsonl",
        reconciler_interval_seconds=10.0,
        heartbeat_seconds=10.0,
    )
    engine = LiveEngine(exchange, cfg, sink=sink, notifier=_RecordingNotifier())
    strategy = _BarRecordingStrategy()
    engine.register([strategy])

    async def _stop() -> None:
        await anyio.sleep(0.15)
        engine.request_stop()

    async with anyio.create_task_group() as tg:
        tg.start_soon(engine.run)
        tg.start_soon(_stop)

    assert len(strategy.fills) == 1
    assert engine.position_state.snapshot() == {"AAPL": Decimal("2")}


@pytest.mark.anyio
async def test_engine_stale_feed_alert_fires(tmp_path: Path) -> None:
    """No events flow; heartbeat must trigger a stale-feed warning."""

    exchange = _FakeExchange(events=[])
    sink = _RecordingSink()
    notifier = _RecordingNotifier()
    cfg = EngineConfig(
        symbols=[Symbol("AAPL")],
        timeframes=["1m"],
        anomaly_path=tmp_path / "anomalies.jsonl",
        reconciler_interval_seconds=10.0,
        heartbeat_seconds=0.05,
    )
    engine = LiveEngine(exchange, cfg, sink=sink, notifier=notifier)

    async def _stop() -> None:
        await anyio.sleep(0.25)
        engine.request_stop()

    async with anyio.create_task_group() as tg:
        tg.start_soon(engine.run)
        tg.start_soon(_stop)

    assert engine.metrics.stale_feed_alerts >= 1
    assert any(":warning:" in w[0] for w in notifier.warnings)


@pytest.mark.anyio
async def test_engine_drops_tick_for_unsubscribed_symbol(tmp_path: Path) -> None:
    base = datetime(2026, 5, 12, 12, 0, 0, tzinfo=UTC)
    events = [
        StreamEvent(tick=_tick("MSFT", base, "200", "1")),
    ]
    exchange = _FakeExchange(events)
    cfg = EngineConfig(
        symbols=[Symbol("AAPL")],
        timeframes=["1m"],
        anomaly_path=tmp_path / "anomalies.jsonl",
        reconciler_interval_seconds=10.0,
        heartbeat_seconds=10.0,
    )
    engine = LiveEngine(exchange, cfg, sink=_RecordingSink(), notifier=_RecordingNotifier())

    async def _stop() -> None:
        await anyio.sleep(0.1)
        engine.request_stop()

    async with anyio.create_task_group() as tg:
        tg.start_soon(engine.run)
        tg.start_soon(_stop)

    # MSFT is unsubscribed but the engine should not crash.
    assert engine.metrics.events_processed == 1


@pytest.mark.anyio
async def test_engine_sell_fill_applies_negative_delta(tmp_path: Path) -> None:
    sell = Fill(
        symbol=Symbol("AAPL"),
        side="SELL",
        qty=Decimal("3"),
        price=Decimal("100"),
        ts=datetime(2026, 5, 12, 12, 5, 0, tzinfo=UTC),
        client_order_id=ClientOrderId("coid-2"),
        venue_order_id=VenueOrderId("vid-2"),
        venue="paper",
        fee=Decimal("0"),
    )
    exchange = _FakeExchange([StreamEvent(fill=sell)])
    cfg = EngineConfig(
        symbols=[Symbol("AAPL")],
        timeframes=["1m"],
        anomaly_path=tmp_path / "anomalies.jsonl",
        reconciler_interval_seconds=10.0,
        heartbeat_seconds=10.0,
    )
    engine = LiveEngine(exchange, cfg, sink=_RecordingSink(), notifier=_RecordingNotifier())

    async def _stop() -> None:
        await anyio.sleep(0.1)
        engine.request_stop()

    async with anyio.create_task_group() as tg:
        tg.start_soon(engine.run)
        tg.start_soon(_stop)

    assert engine.position_state.snapshot() == {"AAPL": Decimal("-3")}


@pytest.mark.anyio
async def test_engine_swallows_stream_close_exception(tmp_path: Path) -> None:
    """A failing ``aclose`` must not propagate out of ``run``."""

    class _CloseRaisingStream(_FakeStream):
        async def aclose(self) -> None:
            raise RuntimeError("aclose blew up")

    class _CloseRaisingExchange(_FakeExchange):
        async def open(self) -> ExchangeStream:
            self.opened = True
            self._stream = _CloseRaisingStream([])
            return self._stream

        async def close(self) -> None:
            raise RuntimeError("close blew up")

    exchange = _CloseRaisingExchange([])
    cfg = EngineConfig(
        symbols=[Symbol("AAPL")],
        timeframes=["1m"],
        anomaly_path=tmp_path / "anomalies.jsonl",
        reconciler_interval_seconds=10.0,
        heartbeat_seconds=10.0,
    )
    engine = LiveEngine(exchange, cfg, sink=_RecordingSink(), notifier=_RecordingNotifier())

    async def _stop() -> None:
        await anyio.sleep(0.05)
        engine.request_stop()

    async with anyio.create_task_group() as tg:
        tg.start_soon(engine.run)
        tg.start_soon(_stop)
    # No exception escaped.


@pytest.mark.anyio
async def test_engine_does_not_close_positions_on_shutdown(tmp_path: Path) -> None:
    """Per design lock: graceful shutdown stops new orders, never auto-closes."""

    fill = _fill("AAPL")
    events = [StreamEvent(fill=fill)]
    exchange = _FakeExchange(events)
    sink = _RecordingSink()
    cfg = EngineConfig(
        symbols=[Symbol("AAPL")],
        timeframes=["1m"],
        anomaly_path=tmp_path / "anomalies.jsonl",
        reconciler_interval_seconds=10.0,
        heartbeat_seconds=10.0,
    )
    engine = LiveEngine(exchange, cfg, sink=sink, notifier=_RecordingNotifier())

    async def _stop() -> None:
        await anyio.sleep(0.1)
        engine.request_stop()

    async with anyio.create_task_group() as tg:
        tg.start_soon(engine.run)
        tg.start_soon(_stop)

    # After shutdown: position state remains; no "close-out" orders submitted.
    assert engine.position_state.snapshot() == {"AAPL": Decimal("2")}
    assert sink.forwarded == []


# ----- anyio configuration -----


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"
