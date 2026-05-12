"""LiveEngine — composes exchange, aggregator, dispatcher, reconciler.

Lifecycle
---------
- ``__init__`` builds the components but opens no network connections.
- ``run`` opens the exchange, starts the reconciler, and consumes the event
  stream until ``stop`` is requested or the stream ends.
- ``stop`` flips an internal ``shutdown_requested`` flag. The engine drains
  in-flight tasks and exits the structured task group. We DO NOT auto-close
  positions on shutdown — per design lock (``DESIGN-LOCK.md``) we only stop
  placing NEW orders.

Signal handling
---------------
On Unix, ``run_with_signal_handlers`` installs SIGINT/SIGTERM handlers that
call ``stop`` once each. A second signal of the same kind raises through
anyio's signal handler to allow forceful exit. Tests use ``run`` directly
with a manual cancel event to keep them deterministic.

Heartbeat
---------
A watchdog task asserts that *some* event (tick OR fill) has been observed
within ``heartbeat_seconds`` (default 30). On expiry, the watchdog fires a
Slack warning and bumps a metric counter; trading does NOT pause — stale
feeds are a notify-only condition because intermittent quiet periods are
normal outside RTH.
"""

from __future__ import annotations

import logging
import signal
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

import anyio

from quanta_core.live.dispatcher import OrderSink, StrategyDispatcher
from quanta_core.live.reconciler import PositionState, Reconciler
from quanta_core.live.tick_aggregator import TickAggregator
from quanta_core.observability.notifier import Notifier, NullNotifier

if TYPE_CHECKING:
    from quanta_core.exchanges.base import Exchange
    from quanta_core.strategy.base import Strategy
    from quanta_core.util.types import Symbol

_log = logging.getLogger(__name__)


DEFAULT_HEARTBEAT_SECONDS: float = 30.0


@dataclass
class EngineConfig:
    """Static configuration for a single LiveEngine instance.

    Parameters
    ----------
    symbols
        Symbols the engine subscribes to (handed to TickAggregator).
    timeframes
        Timeframes to maintain per symbol.
    heartbeat_seconds
        Max idle time on the stream before the watchdog fires a stale-feed
        alert.
    reconciler_interval_seconds
        Cadence for the REST position sweep.
    anomaly_path
        Where the reconciler appends drift rows.
    """

    symbols: list[Symbol]
    timeframes: list[str]
    heartbeat_seconds: float = DEFAULT_HEARTBEAT_SECONDS
    reconciler_interval_seconds: float = 60.0
    anomaly_path: Path = field(
        default_factory=lambda: Path.home() / ".quanta" / "logs" / "anomalies.jsonl",
    )


@dataclass
class EngineMetrics:
    """Counters exposed for observability."""

    events_processed: int = 0
    stale_feed_alerts: int = 0
    started_at: datetime | None = None
    stopped_at: datetime | None = None


class LiveEngine:
    """Top-level live trading event loop.

    The engine owns:
        - one ``Exchange`` (interface only — the exchanges agent supplies
          the concrete adapter)
        - one ``TickAggregator`` per symbol
        - one ``StrategyDispatcher`` (with the execution-side ``OrderSink``)
        - one ``Reconciler`` for REST-vs-memory position sweeps
        - one ``PositionState`` (in-memory view, mutated on every fill)

    Parameters
    ----------
    exchange
        Concrete venue connector implementing ``Exchange``.
    config
        Static configuration.
    sink
        Where the dispatcher forwards proposals. Owned by the execution agent.
    notifier
        Optional Slack/Telegram notifier. Defaults to ``NullNotifier``.
    """

    def __init__(
        self,
        exchange: Exchange,
        config: EngineConfig,
        sink: OrderSink,
        *,
        notifier: Notifier | None = None,
    ) -> None:
        self.exchange = exchange
        self.config = config
        self.notifier: Notifier = notifier if notifier is not None else NullNotifier()
        self.metrics = EngineMetrics()
        self.shutdown_requested = anyio.Event()

        self._aggregators: dict[str, TickAggregator] = {
            str(sym): TickAggregator(
                symbol=sym,
                timeframes=list(config.timeframes),  # type: ignore[arg-type]
            )
            for sym in config.symbols
        }
        self.position_state = PositionState()
        self.dispatcher = StrategyDispatcher(sink=sink)
        self.reconciler = Reconciler(
            exchange=exchange,
            state=self.position_state,
            notifier=self.notifier,
            anomaly_path=config.anomaly_path,
            interval_seconds=config.reconciler_interval_seconds,
        )
        self._last_event_at: float = 0.0

    # ----- public API -----

    def register(self, strategies: Iterable[Strategy]) -> None:
        """Register one or more strategies with the dispatcher."""

        for strategy in strategies:
            self.dispatcher.register(strategy)

    def request_stop(self) -> None:
        """Signal the engine to drain and exit. Idempotent."""

        if not self.shutdown_requested.is_set():
            _log.info("engine.stop_requested")
            self.shutdown_requested.set()

    async def run(self) -> None:
        """Open the exchange and run the event loop until ``request_stop``.

        The engine fans out three coroutines under a structured task group:

        - consumer: reads the stream and dispatches events
        - reconciler: 60s REST sweeps
        - heartbeat: watchdog for stale feeds

        Cancelling any one cancels them all (anyio level-cancellation).
        """

        self.metrics.started_at = datetime.now(UTC)
        self._touch_heartbeat()
        stream = await self.exchange.open()
        try:
            async with anyio.create_task_group() as tg:
                tg.start_soon(self._consume_stream, stream)
                tg.start_soon(self._run_reconciler)
                tg.start_soon(self._run_heartbeat)
                await self.shutdown_requested.wait()
                tg.cancel_scope.cancel()
        finally:
            await self._shutdown(stream)
            self.metrics.stopped_at = datetime.now(UTC)

    async def run_with_signal_handlers(self) -> None:
        """Convenience wrapper: install SIGINT/SIGTERM -> ``request_stop``.

        Intended for production entrypoints. Tests use ``run`` + a manual
        cancel for determinism.
        """

        async with anyio.create_task_group() as tg:
            tg.start_soon(self._signal_watcher)
            await self.run()
            tg.cancel_scope.cancel()

    # ----- private coroutines -----

    async def _consume_stream(self, stream: object) -> None:
        """Pull events off the exchange stream and dispatch them."""

        async for event in stream:  # type: ignore[attr-defined]
            self._touch_heartbeat()
            self.metrics.events_processed += 1
            if event.tick is not None:
                await self._handle_tick(event.tick)
            if event.fill is not None:
                await self._handle_fill(event.fill)
            if self.shutdown_requested.is_set():
                return

    async def _handle_tick(self, tick: object) -> None:
        aggregator = self._aggregators.get(str(tick.symbol))  # type: ignore[attr-defined]
        if aggregator is None:
            _log.warning(
                "engine.tick_unsubscribed_symbol",
                extra={"symbol": str(tick.symbol)},  # type: ignore[attr-defined]
            )
            return
        try:
            closed = aggregator.on_tick(tick)  # type: ignore[arg-type]
        except ValueError:
            _log.exception("engine.tick_aggregator_rejected")
            return
        await self.dispatcher.dispatch_tick(tick)  # type: ignore[arg-type]
        for bar in closed:
            await self.dispatcher.dispatch_candle(bar)

    async def _handle_fill(self, fill: object) -> None:
        # Apply signed delta to in-memory state. Positive for BUY, negative for SELL.
        delta = fill.qty if fill.side == "BUY" else -fill.qty  # type: ignore[attr-defined]
        self.position_state.apply_fill_delta(fill.symbol, delta)  # type: ignore[attr-defined]
        await self.dispatcher.dispatch_fill(fill)  # type: ignore[arg-type]

    async def _run_reconciler(self) -> None:
        await self.reconciler.run(cancel_event=self.shutdown_requested)

    async def _run_heartbeat(self) -> None:
        """Periodically check that we have seen an event recently.

        Uses ``anyio.current_time()`` so we move at the same clock as the
        rest of the loop (and so tests can mock with ``anyio.move_on_after``).
        """

        budget = self.config.heartbeat_seconds
        while not self.shutdown_requested.is_set():
            try:
                with anyio.fail_after(budget):
                    await self.shutdown_requested.wait()
                    return
            except TimeoutError:
                idle = anyio.current_time() - self._last_event_at
                if idle >= budget:
                    self.metrics.stale_feed_alerts += 1
                    _log.warning(
                        "engine.stale_feed",
                        extra={"idle_seconds": idle, "budget_seconds": budget},
                    )
                    try:
                        await self.notifier.warning(
                            ":warning: live engine: stale feed",
                            f"no events for {idle:.1f}s (budget={budget:.0f}s)",
                        )
                    except Exception:
                        _log.exception("engine.notifier_failed")

    async def _signal_watcher(self) -> None:
        """Translate SIGINT/SIGTERM into ``request_stop``."""

        with anyio.open_signal_receiver(signal.SIGINT, signal.SIGTERM) as signals:
            async for signum in signals:
                _log.info("engine.signal_received", extra={"signum": int(signum)})
                self.request_stop()
                return

    async def _shutdown(self, stream: object) -> None:
        """Close the stream + exchange. Never raises."""

        try:
            await stream.aclose()  # type: ignore[attr-defined]
        except Exception:
            _log.exception("engine.stream_close_failed")
        try:
            await self.exchange.close()
        except Exception:
            _log.exception("engine.exchange_close_failed")

    def _touch_heartbeat(self) -> None:
        self._last_event_at = anyio.current_time()


__all__ = [
    "DEFAULT_HEARTBEAT_SECONDS",
    "EngineConfig",
    "EngineMetrics",
    "LiveEngine",
]
