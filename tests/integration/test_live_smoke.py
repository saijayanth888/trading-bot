"""Live-engine smoke — replays a fixed tick stream through ``LiveEngine``.

Scenario
--------
1. Build a :class:`FakeLiveExchange` that yields N ticks and a handful of
   fills as ``StreamEvent``s.
2. Construct a :class:`LiveEngine` with the strategy + execution sink
   already used by the backtest smoke. The wiring under test:

       FakeLiveExchange (stream) ──► LiveEngine
                                        │
                            ┌───────────┴──────────┐
                            ▼                      ▼
                   TickAggregator(1m)        Reconciler (mocked)
                            │
                            ▼ (on bar close)
                  StrategyDispatcher.dispatch_candle
                            │
                            ▼
                    Strategy.on_candle  ──► OrderProposal
                                              │
                                              ▼
                              RiskGatedExecutionSink.submit
                                              │
                                              ▼
                                   ExecutionEngine.submit
                                              │
                                              ▼
                                       Ledger.record_fill
3. Run the engine until the stream drains, then ``request_stop``.
4. Assert:
       * ``on_candle`` fired exactly N times where N is the number of bars
         the aggregator closed.
       * ``on_fill`` fired exactly M times where M is the number of fill
         events in the stream.
       * The ledger has K rows where K == proposals approved by the risk
         engine (== proposals_emitted in the no-rejection happy path).

Heartbeat / reconciler
----------------------
The heartbeat coroutine + reconciler coroutine both run as expected; the
heartbeat must NOT fire a stale-feed warning because we always emit at
least one event per heartbeat window. The reconciler must run at least
one sweep when its cadence is set to the engine's lifetime.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING

import anyio
import pytest

from quanta_core.exchanges.base import StreamEvent
from quanta_core.live.engine import EngineConfig, LiveEngine
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

from .conftest import (
    DEFAULT_START_TS,
    FakeLiveExchange,
    InMemoryLedger,
    RiskGatedExecutionSink,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    pass


SYMBOL = Symbol("BTC-USD")


class _RecordingNotifier:
    """Records alerts. Implements the ``Notifier`` Protocol structurally."""

    def __init__(self) -> None:
        self.warnings: list[tuple[str, str]] = []
        self.infos: list[tuple[str, str]] = []

    async def info(self, subject: str, body: str) -> None:
        self.infos.append((subject, body))

    async def warning(self, subject: str, body: str) -> None:
        self.warnings.append((subject, body))


class CountingStrategy(Strategy):
    """Strategy that records every callback + emits one proposal per 5 ticks worth of bars."""

    name = "live-counter"
    symbols: list[Symbol] = [SYMBOL]
    timeframes: list[Timeframe] = ["1m"]
    wants_ticks = False

    def __init__(self, *, emit_every_n_bars: int = 1) -> None:
        self._every = emit_every_n_bars
        self.candles: list[Bar] = []
        self.ticks: list[Tick] = []
        self.fills: list[Fill] = []

    async def on_candle(self, bar: Bar, ctx: object) -> list[OrderProposal]:
        self.candles.append(bar)
        if len(self.candles) % self._every != 0:
            return []
        return [
            OrderProposal(
                strategy_name=self.name,
                symbol=bar.symbol,
                venue="paper",
                side="BUY",
                qty=Decimal("0.01"),
                order_type="limit",
                limit_price=bar.close,
                intent_timestamp_ms=int(bar.close_ts.timestamp() * 1000),
                metadata={
                    "client_order_id": (f"qc4-live-{len(self.candles):04d}"),
                    "signal_px": str(bar.close),
                },
            )
        ]

    async def on_tick(self, tick: Tick, ctx: object) -> list[OrderProposal]:
        self.ticks.append(tick)
        return []

    async def on_fill(self, fill: Fill, ctx: object) -> list[OrderProposal]:
        self.fills.append(fill)
        return []


def _build_stream(*, n_bars_to_close: int = 5, fills: int = 3) -> list[StreamEvent]:
    """Build a stream that closes exactly ``n_bars_to_close`` 1m bars.

    The TickAggregator emits a Bar on the *first tick past the boundary*.
    So to close ``n`` bars we need ``n + 1`` boundaries' worth of ticks.

    Each tick is 65 seconds apart so it always crosses one boundary.
    """
    price = Decimal("65000")
    tick_events: list[StreamEvent] = [
        StreamEvent(
            tick=Tick(
                symbol=SYMBOL,
                ts=DEFAULT_START_TS + dt.timedelta(seconds=65 * i),
                price=price + Decimal(i),
                size=Decimal("0.01"),
                side=None,
            )
        )
        for i in range(n_bars_to_close + 1)
    ]
    fill_events: list[StreamEvent] = [
        StreamEvent(
            fill=Fill(
                symbol=SYMBOL,
                side="BUY",
                qty=Decimal("0.01"),
                price=Decimal("65000") + Decimal(i),
                ts=DEFAULT_START_TS + dt.timedelta(seconds=120 + i),
                client_order_id=ClientOrderId(f"qc4-fill-stream-{i}"),
                venue_order_id=VenueOrderId(f"venue-stream-{i}"),
                venue="paper",
                fee=Decimal("0"),
            )
        )
        for i in range(fills)
    ]
    return [*tick_events, *fill_events]


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_live_engine_dispatches_candles_and_fills(
    risk_sink: RiskGatedExecutionSink,
    ledger: InMemoryLedger,
    tmp_path: Path,
) -> None:
    """The full LiveEngine loop runs: ticks → bars → proposals → fills → ledger.

    Assertions:
        * The strategy's ``on_candle`` fires for each closed bar.
        * ``on_fill`` fires once per fill event in the stream.
        * Every proposal goes through the risk-gated sink AND lands in the
          ledger (no risk rejections in this scenario).
        * ``engine.metrics.events_processed`` counts every StreamEvent.
        * The exchange's ``open`` + ``close`` lifecycle hooks ran.
    """
    n_bars = 5
    n_fills_in_stream = 3
    events = _build_stream(n_bars_to_close=n_bars, fills=n_fills_in_stream)
    exchange = FakeLiveExchange(events)
    strategy = CountingStrategy(emit_every_n_bars=1)
    notifier = _RecordingNotifier()

    config = EngineConfig(
        symbols=[SYMBOL],
        timeframes=["1m"],
        heartbeat_seconds=120.0,
        reconciler_interval_seconds=120.0,
        anomaly_path=tmp_path / "anomalies.jsonl",
    )
    engine = LiveEngine(
        exchange=exchange,
        config=config,
        sink=risk_sink,
        notifier=notifier,
    )
    engine.register([strategy])

    # Run the engine for a bounded window. The stream drains on its own
    # after the events are exhausted (our _ReplayStream returns); we then
    # explicitly request_stop so the engine exits its task group.
    async def _drain_then_stop() -> None:
        # Give the engine task group enough time to consume every scripted
        # event and run a few aggregator checkpoints. The stream is
        # in-memory so 200ms is generous; the test fails fast if the loop
        # actually hung.
        await anyio.sleep(0.2)
        engine.request_stop()

    async with anyio.create_task_group() as tg:
        tg.start_soon(engine.run)
        tg.start_soon(_drain_then_stop)

    # Exchange lifecycle ran.
    assert exchange.opened is True
    assert exchange.closed is True

    # Every stream event was processed.
    assert engine.metrics.events_processed == len(events)

    # The strategy's ``on_candle`` fired for each closed bar (we sent
    # n_bars + 1 ticks so the aggregator emits ``n_bars`` closed bars).
    assert len(strategy.candles) == n_bars

    # ``on_fill`` fired exactly once per stream fill.
    assert len(strategy.fills) == n_fills_in_stream

    # Every candle produced a proposal (emit_every_n_bars=1), so the
    # risk-gated sink should have N rows in ``adapted`` and N fills in
    # the ledger (paper exchange fills everything).
    assert len(risk_sink.adapted) == n_bars
    assert len(risk_sink.outcomes) == n_bars
    assert len(ledger.fills) == n_bars
    assert len(ledger.rejections) == 0

    # Heartbeat watchdog did not warn — we emitted events faster than the
    # 120s budget.
    assert notifier.warnings == [], f"unexpected stale-feed warnings: {notifier.warnings}"


@pytest.mark.anyio
async def test_live_engine_handles_empty_stream(
    risk_sink: RiskGatedExecutionSink,
    ledger: InMemoryLedger,
    tmp_path: Path,
) -> None:
    """An empty stream is not a failure — the engine just produces nothing.

    The engine still completes its ``open`` + ``close`` lifecycle and the
    metrics counters end at zero. No proposals, no fills, no ledger rows.
    """
    exchange = FakeLiveExchange(events=[])
    strategy = CountingStrategy()
    config = EngineConfig(
        symbols=[SYMBOL],
        timeframes=["1m"],
        heartbeat_seconds=120.0,
        reconciler_interval_seconds=120.0,
        anomaly_path=tmp_path / "anomalies.jsonl",
    )
    engine = LiveEngine(exchange=exchange, config=config, sink=risk_sink)
    engine.register([strategy])

    async with anyio.create_task_group() as tg:
        tg.start_soon(engine.run)
        await anyio.sleep(0.05)
        engine.request_stop()

    assert engine.metrics.events_processed == 0
    assert strategy.candles == []
    assert strategy.fills == []
    assert risk_sink.adapted == []
    assert ledger.fills == []


@pytest.mark.anyio
async def test_live_engine_fill_updates_position_state(
    risk_sink: RiskGatedExecutionSink,
    tmp_path: Path,
) -> None:
    """A BUY fill increments the in-memory PositionState; a SELL decrements.

    The reconciler reads this state on its REST sweep. We assert the
    engine applies the signed delta correctly.
    """
    events: list[StreamEvent] = [
        StreamEvent(
            fill=Fill(
                symbol=SYMBOL,
                side="BUY",
                qty=Decimal("0.5"),
                price=Decimal("65000"),
                ts=DEFAULT_START_TS,
                client_order_id=ClientOrderId("qc4-pos-1"),
                venue_order_id=VenueOrderId("venue-1"),
                venue="paper",
                fee=Decimal("0"),
            )
        ),
        StreamEvent(
            fill=Fill(
                symbol=SYMBOL,
                side="SELL",
                qty=Decimal("0.2"),
                price=Decimal("65100"),
                ts=DEFAULT_START_TS + dt.timedelta(seconds=10),
                client_order_id=ClientOrderId("qc4-pos-2"),
                venue_order_id=VenueOrderId("venue-2"),
                venue="paper",
                fee=Decimal("0"),
            )
        ),
    ]
    exchange = FakeLiveExchange(events)
    strategy = CountingStrategy()
    config = EngineConfig(
        symbols=[SYMBOL],
        timeframes=["1m"],
        heartbeat_seconds=120.0,
        reconciler_interval_seconds=120.0,
        anomaly_path=tmp_path / "anomalies.jsonl",
    )
    engine = LiveEngine(exchange=exchange, config=config, sink=risk_sink)
    engine.register([strategy])

    async def _drain_then_stop() -> None:
        await anyio.sleep(0.2)
        engine.request_stop()

    async with anyio.create_task_group() as tg:
        tg.start_soon(engine.run)
        tg.start_soon(_drain_then_stop)

    snap = engine.position_state.snapshot()
    # BUY 0.5 - SELL 0.2 = 0.3 net long.
    assert snap[str(SYMBOL)] == Decimal("0.3")


@pytest.mark.anyio
async def test_live_engine_unsubscribed_symbol_does_not_crash(
    risk_sink: RiskGatedExecutionSink,
    ledger: InMemoryLedger,
    tmp_path: Path,
) -> None:
    """Ticks for a symbol we never subscribed to are logged + dropped, not raised.

    This is the guard that keeps a noisy public feed from crashing the
    engine. The strategy must never see the orphan event.
    """
    rogue_symbol = Symbol("DOGE-USD")
    events: list[StreamEvent] = [
        StreamEvent(
            tick=Tick(
                symbol=rogue_symbol,
                ts=DEFAULT_START_TS,
                price=Decimal("0.5"),
                size=Decimal("1000"),
                side=None,
            )
        ),
    ]
    exchange = FakeLiveExchange(events)
    strategy = CountingStrategy()
    config = EngineConfig(
        symbols=[SYMBOL],
        timeframes=["1m"],
        heartbeat_seconds=120.0,
        reconciler_interval_seconds=120.0,
        anomaly_path=tmp_path / "anomalies.jsonl",
    )
    engine = LiveEngine(exchange=exchange, config=config, sink=risk_sink)
    engine.register([strategy])

    async with anyio.create_task_group() as tg:
        tg.start_soon(engine.run)
        await anyio.sleep(0.05)
        engine.request_stop()

    assert engine.metrics.events_processed == 1
    assert strategy.candles == []
    assert strategy.ticks == []
    assert risk_sink.adapted == []
    assert ledger.fills == []
