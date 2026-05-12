"""Live module — WebSocket consumer, tick aggregator, dispatcher, reconciler.

Public surface
--------------
LiveEngine
    Top-level event loop owner. Composed of an Exchange, a TickAggregator,
    a StrategyDispatcher and a Reconciler.
TickAggregator
    Buffers ticks into closed Bars across one or more timeframes.
StrategyDispatcher
    Routes Bar / Tick / Fill events to registered Strategy instances; never
    crashes the loop on per-call exceptions.
Reconciler
    Periodic REST poll of venue positions; diffs against in-memory state.
"""

from __future__ import annotations

from quanta_core.live.dispatcher import StrategyDispatcher
from quanta_core.live.engine import LiveEngine
from quanta_core.live.reconciler import PositionState, Reconciler
from quanta_core.live.tick_aggregator import TickAggregator

__all__ = [
    "LiveEngine",
    "PositionState",
    "Reconciler",
    "StrategyDispatcher",
    "TickAggregator",
]
