"""Shared type aliases and domain dataclasses.

The live module is the first consumer of the runtime type vocabulary. We
keep the surface minimal here so it can be vendored by sibling agents
(exchanges, strategy, execution) without circular imports.

Conventions
-----------
- All timestamps are ``datetime`` instances in **UTC**. We never store naive
  times. The aggregator rejects naive timestamps at the boundary.
- All monetary / quantity values are ``Decimal``. We never carry ``float``.
- Symbols are opaque strings via ``NewType``. The exchanges layer owns
  symbology translation.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any, Literal, NewType

Symbol = NewType("Symbol", str)
"""Canonical symbol, e.g. ``"BTC/USD"`` or ``"AAPL"``."""

Venue = Literal["alpaca", "coinbase", "paper"]
Side = Literal["BUY", "SELL"]
Timeframe = Literal["1m", "5m", "15m", "1h", "4h", "1d"]
ClientOrderId = NewType("ClientOrderId", str)
VenueOrderId = NewType("VenueOrderId", str)


@dataclass(frozen=True)
class Tick:
    """A single trade print (not a quote).

    Parameters
    ----------
    symbol
        Canonical symbol.
    ts
        UTC timestamp of the print at the exchange.
    price
        Trade price.
    size
        Trade size in base units.
    side
        Aggressor side if disclosed by the venue, otherwise ``None``.
    """

    symbol: Symbol
    ts: datetime
    price: Decimal
    size: Decimal
    side: Side | None = None


@dataclass(frozen=True)
class Bar:
    """A closed OHLCV bar.

    The aggregator emits one ``Bar`` per closed boundary. ``close_ts`` is the
    *exclusive upper bound* of the bar window — a 1m bar covering
    ``[09:00, 09:01)`` has ``open_ts=09:00`` and ``close_ts=09:01``.

    The ``vwap`` field is computed from the ticks observed within the
    boundary (Sum(price * size) / Sum(size)). When ``volume == 0`` the
    aggregator falls back to ``close``.
    """

    symbol: Symbol
    timeframe: Timeframe
    open_ts: datetime
    close_ts: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal
    vwap: Decimal
    trades: int


@dataclass(frozen=True)
class Fill:
    """A confirmed fill from the venue."""

    symbol: Symbol
    side: Side
    qty: Decimal
    price: Decimal
    ts: datetime
    client_order_id: ClientOrderId
    venue_order_id: VenueOrderId
    venue: Venue
    fee: Decimal


@dataclass(frozen=True)
class Position:
    """A net position snapshot at a point in time.

    Used by the reconciler when diffing in-memory state against REST
    polling. ``qty`` is signed (positive long, negative short).
    """

    symbol: Symbol
    qty: Decimal
    avg_price: Decimal
    venue: Venue


@dataclass(frozen=True)
class OrderProposal:
    """A typed order intent returned by a Strategy hook.

    The live engine forwards proposals to the execution layer (owned by the
    sibling exec agent). This module never constructs proposals; it only
    routes them.
    """

    strategy_name: str
    symbol: Symbol
    venue: Venue
    side: Side
    qty: Decimal
    order_type: str
    limit_price: Decimal | None = None
    stop_price: Decimal | None = None
    time_in_force: str = "day"
    intent_timestamp_ms: int = 0
    extended_hours: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)


__all__ = [
    "Bar",
    "ClientOrderId",
    "Fill",
    "OrderProposal",
    "Position",
    "Side",
    "Symbol",
    "Tick",
    "Timeframe",
    "Venue",
    "VenueOrderId",
]
