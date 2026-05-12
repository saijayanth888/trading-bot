"""StrategyDispatcher — routes events to strategies, never crashes the loop.

The dispatcher is the only place that calls into user-supplied strategy
code. Three guarantees:

1. Exceptions raised inside a strategy hook are caught, logged with a
   correlation id, and swallowed. They DO NOT propagate to the caller.
2. The 30-second deliberate-debate budget is enforced at the per-call
   level: a strategy that spends more than the budget on a single hook has
   its result discarded and a metric counter incremented.
3. OrderProposals returned by hooks are forwarded to the execution layer
   via a protocol (``OrderSink``) — the live module never imports
   ``execution.engine`` directly.

The execution layer is owned by a sibling agent; we only assume the
interface declared in ``OrderSink``.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Protocol, runtime_checkable

import anyio

if TYPE_CHECKING:
    from quanta_core.strategy.base import Strategy
    from quanta_core.util.types import Bar, Fill, OrderProposal, Tick

_log = logging.getLogger(__name__)


DEFAULT_BUDGET_SECONDS: float = 30.0
"""Hard upper bound on a single strategy hook invocation.

The deliberate-debate workflow (bull / bear / arbiter) is locked at 30s in
``docs/quanta-core-v4-rev2/DESIGN-LOCK.md``. Hooks that exceed this budget
have their result dropped — we never let a slow agent block the loop.
"""


@runtime_checkable
class OrderSink(Protocol):
    """Where the dispatcher forwards approved OrderProposals.

    The concrete sink is the execution engine in production, or a recording
    fake in tests. We never import ``execution.engine`` here.
    """

    async def submit(self, proposal: OrderProposal) -> None:
        """Forward an order proposal for risk + execution."""


@dataclass
class DispatcherMetrics:
    """Counters for observability — read by the dashboard."""

    candles_dispatched: int = 0
    ticks_dispatched: int = 0
    fills_dispatched: int = 0
    hook_exceptions: int = 0
    budget_exceeded: int = 0
    proposals_forwarded: int = 0


@dataclass
class StrategyDispatcher:
    """Routes Bar / Tick / Fill events to registered strategies.

    Parameters
    ----------
    sink
        Where to forward OrderProposals returned by hooks.
    budget_seconds
        Per-hook timeout. Defaults to ``DEFAULT_BUDGET_SECONDS``.
    """

    sink: OrderSink
    budget_seconds: float = DEFAULT_BUDGET_SECONDS
    metrics: DispatcherMetrics = field(default_factory=DispatcherMetrics)
    _strategies: list[Strategy] = field(default_factory=list)

    def register(self, strategy: Strategy) -> None:
        """Add a strategy. Idempotent on ``strategy.name``."""

        for existing in self._strategies:
            if existing.name == strategy.name:
                return
        self._strategies.append(strategy)
        _log.info(
            "dispatcher.register",
            extra={
                "strategy": strategy.name,
                "symbols": [str(s) for s in strategy.symbols],
                "timeframes": list(strategy.timeframes),
            },
        )

    def unregister(self, name: str) -> None:
        """Drop a strategy by name. No-op if not registered."""

        self._strategies = [s for s in self._strategies if s.name != name]

    @property
    def strategies(self) -> tuple[Strategy, ...]:
        """Snapshot of currently registered strategies."""

        return tuple(self._strategies)

    async def dispatch_candle(self, bar: Bar, ctx: object = None) -> None:
        """Call ``on_candle`` on every strategy that subscribes."""

        self.metrics.candles_dispatched += 1
        for strategy in self._strategies:
            if bar.symbol not in strategy.symbols:
                continue
            if bar.timeframe not in strategy.timeframes:
                continue
            await self._invoke(strategy, "on_candle", bar, ctx)

    async def dispatch_tick(self, tick: Tick, ctx: object = None) -> None:
        """Call ``on_tick`` on every strategy that opted in via ``wants_ticks``."""

        self.metrics.ticks_dispatched += 1
        for strategy in self._strategies:
            if not strategy.wants_ticks:
                continue
            if tick.symbol not in strategy.symbols:
                continue
            await self._invoke(strategy, "on_tick", tick, ctx)

    async def dispatch_fill(self, fill: Fill, ctx: object = None) -> None:
        """Call ``on_fill`` on every strategy holding the symbol."""

        self.metrics.fills_dispatched += 1
        for strategy in self._strategies:
            if fill.symbol not in strategy.symbols:
                continue
            await self._invoke(strategy, "on_fill", fill, ctx)

    # ----- private -----

    async def _invoke(
        self,
        strategy: Strategy,
        hook: str,
        event: object,
        ctx: object,
    ) -> None:
        """Run one hook with timeout + exception isolation."""

        correlation_id = uuid.uuid4().hex
        method = getattr(strategy, hook)
        try:
            with anyio.fail_after(self.budget_seconds):
                proposals: Iterable[OrderProposal] = await method(event, ctx)
        except TimeoutError:
            self.metrics.budget_exceeded += 1
            _log.warning(
                "dispatcher.budget_exceeded",
                extra={
                    "strategy": strategy.name,
                    "hook": hook,
                    "budget_seconds": self.budget_seconds,
                    "correlation_id": correlation_id,
                },
            )
            return
        except Exception:
            self.metrics.hook_exceptions += 1
            _log.exception(
                "dispatcher.hook_exception",
                extra={
                    "strategy": strategy.name,
                    "hook": hook,
                    "correlation_id": correlation_id,
                },
            )
            return

        await self._forward(proposals, strategy.name, correlation_id)

    async def _forward(
        self,
        proposals: Iterable[OrderProposal],
        strategy_name: str,
        correlation_id: str,
    ) -> None:
        """Push proposals into the execution sink, one by one.

        A failure in the sink is logged but does NOT propagate — the loop
        keeps running. Production sinks raise only on programming errors
        (interface drift); transient broker errors are absorbed by the
        sink's own retry layer.
        """

        for proposal in proposals or []:
            try:
                await self.sink.submit(proposal)
                self.metrics.proposals_forwarded += 1
            except Exception:
                _log.exception(
                    "dispatcher.sink_exception",
                    extra={
                        "strategy": strategy_name,
                        "correlation_id": correlation_id,
                    },
                )


__all__ = [
    "DEFAULT_BUDGET_SECONDS",
    "DispatcherMetrics",
    "OrderSink",
    "StrategyDispatcher",
]
