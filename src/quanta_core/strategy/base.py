"""Strategy ABC — the contract every concrete strategy implements.

The live engine dispatches Bar / Tick / Fill events to subclasses through
this surface. ``on_candle`` is mandatory; the other hooks default to no-op.
The full design rationale lives in
``docs/quanta-core-v4/06-ARCHITECTURE.md §5``.

The live module owns the *scheduling* of hooks (serialised per strategy)
and the routing of returned proposals; the strategy itself is pure logic.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from quanta_core.util.types import (
        Bar,
        Fill,
        OrderProposal,
        Symbol,
        Tick,
        Timeframe,
    )


class Strategy(ABC):
    """Abstract base every strategy implements.

    Class attributes
    ----------------
    name
        Unique strategy identifier; used in OrderProposal.strategy_name.
    symbols
        Symbols this instance trades.
    timeframes
        Timeframes the strategy wants closed bars for. The engine will only
        invoke ``on_candle`` for bars whose timeframe is in this list.
    wants_ticks
        Opt-in to ``on_tick`` — costs CPU; default False.
    """

    name: str = "unnamed"
    symbols: list[Symbol] = []
    timeframes: list[Timeframe] = []
    wants_ticks: bool = False

    @abstractmethod
    async def on_candle(self, bar: Bar, ctx: object) -> list[OrderProposal]:
        """Process a closed bar; return zero or more proposed orders."""

    async def on_tick(self, tick: Tick, ctx: object) -> list[OrderProposal]:
        """Optional. Default no-op. Only called when ``wants_ticks`` is True."""
        return []

    async def on_fill(self, fill: Fill, ctx: object) -> list[OrderProposal]:
        """Optional. Default no-op. Called once per confirmed fill."""
        return []

    async def on_start(self, ctx: object) -> None:
        """Optional. Default no-op. One-time warm-up before the first event."""
        return

    async def on_stop(self, ctx: object) -> None:
        """Optional. Default no-op. Called on graceful shutdown."""
        return


__all__ = ["Strategy"]
