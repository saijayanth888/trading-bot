"""Exchange ABC + shared value types + live-stream contract.

This module hosts TWO related but separable contracts:

1. **Adapter contract** (``Exchange`` ABC) — the full broker API surface.
   Every concrete venue (Alpaca, Coinbase, Paper) implements this. The
   execution engine consumes the rich connect / submit / cancel / stream_*
   methods. Vendor-specific responses are normalised to the dataclasses
   defined here so the strategy + ledger layers never branch on venue.

2. **Live-engine streaming contract** (``ExchangeStream`` ABC +
   ``StreamEvent`` dataclass) — a narrower facade used by
   ``quanta_core.live.engine``. It exposes a single async iterator of
   normalised tick/fill events. Concrete adapters MAY implement
   ``Exchange.open()`` to wrap their own stream_ticks/stream_fills into
   one of these streams (the in-process FakeExchange used by live tests
   does exactly that).

Design notes
------------
* All I/O is async on the anyio runtime. Sync-only SDK calls are wrapped
  in ``anyio.to_thread.run_sync`` inside the concrete adapters.
* ``connect()`` / ``disconnect()`` are idempotent and reentrant — calling
  ``connect`` twice is fine, the second call is a no-op.
* The streams (``stream_ticks``, ``stream_fills``, ``stream_orderbook``)
  are bare async iterators so callers can compose them with task groups
  and ``anyio.create_memory_object_stream`` backpressure-aware queues.
* ``OrderProposal`` carries the ``client_order_id`` already — the
  idempotency layer above is responsible for generating it. Adapters do
  not generate IDs (would break replay-safety).
* The ``Tick`` / ``Fill`` dataclasses defined here are the **adapter**
  view — venue-rich, with ``raw`` payloads. The live engine consumes the
  narrower ``quanta_core.util.types.Tick`` / ``.Fill`` shapes via
  ``StreamEvent``; concrete adapters convert at the boundary.
"""

from __future__ import annotations

import abc
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:
    from quanta_core.util.types import Fill as LiveFill
    from quanta_core.util.types import Tick as LiveTick

# ---------------------------------------------------------------------------
# Value types (normalised across venues)
# ---------------------------------------------------------------------------

Side = Literal["BUY", "SELL"]
OrderType = Literal["market", "limit", "stop", "stop_limit"]
TimeInForce = Literal["day", "gtc", "ioc", "fok", "gtd"]
AssetClass = Literal["stock", "option", "crypto"]
OrderStatus = Literal["open", "filled", "partially_filled", "canceled", "rejected", "expired"]


@dataclass(frozen=True, slots=True)
class OrderProposal:
    """An order ready to be sent to a venue.

    The strategy layer hands one of these to the execution engine, which
    enriches it with a ``client_order_id`` (see :mod:`quanta_core.exchanges.idempotency`)
    and then dispatches to the right adapter.
    """

    symbol: str
    side: Side
    qty: Decimal
    order_type: OrderType
    client_order_id: str
    limit_price: Decimal | None = None
    stop_price: Decimal | None = None
    time_in_force: TimeInForce = "day"
    extended_hours: bool = False
    asset_class: AssetClass = "stock"
    strategy_id: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class OrderAck:
    """Broker acknowledgement for a newly-submitted order."""

    venue: str
    client_order_id: str
    venue_order_id: str
    status: OrderStatus
    symbol: str
    side: Side
    qty: Decimal
    filled_qty: Decimal
    asset_class: AssetClass
    submitted_at: datetime
    raw: dict[str, Any]


@dataclass(frozen=True, slots=True)
class Fill:
    """A single execution event. Crypto venues fire one per partial; Alpaca
    fires partial_fill / fill on the trade_updates WebSocket."""

    venue: str
    asset_class: AssetClass
    symbol: str
    venue_order_id: str
    client_order_id: str
    side: Side
    qty: Decimal
    price: Decimal
    fee: Decimal
    fee_accrued_later: bool  # True for Alpaca regulatory fees, False for Coinbase
    ts: datetime
    raw: dict[str, Any]


@dataclass(frozen=True, slots=True)
class PositionSnapshot:
    """A held position. ``asset_class`` MUST be populated — surfacing it
    closed today's option-vs-stock confusion bug."""

    symbol: str
    qty: Decimal
    avg_entry_price: Decimal
    market_value: Decimal
    asset_class: AssetClass
    unrealized_pl: Decimal
    venue: str
    raw: dict[str, Any]


@dataclass(frozen=True, slots=True)
class AccountSnapshot:
    """Account state — equity + buying power + restrictions."""

    venue: str
    equity: Decimal
    buying_power: Decimal
    cash: Decimal
    portfolio_value: Decimal
    currency: str
    pattern_day_trader: bool
    trading_blocked: bool
    raw: dict[str, Any]


@dataclass(frozen=True, slots=True)
class Tick:
    """A normalised trade tick (last-trade event)."""

    venue: str
    symbol: str
    price: Decimal
    size: Decimal
    ts: datetime
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class OrderbookLevel:
    price: Decimal
    size: Decimal


@dataclass(frozen=True, slots=True)
class OrderbookSnapshot:
    """A best-bid/ask + N-level book snapshot."""

    venue: str
    symbol: str
    bids: Sequence[OrderbookLevel]
    asks: Sequence[OrderbookLevel]
    ts: datetime
    sequence_num: int | None = None  # Coinbase populates; Alpaca leaves None


# ---------------------------------------------------------------------------
# Exception hierarchy
# ---------------------------------------------------------------------------


class ExchangeError(Exception):
    """Base class for every adapter-raised error."""


class OrderRejected(ExchangeError):
    """Broker rejected the order (insufficient BP, PDT, duplicate coid, ...)."""

    def __init__(
        self,
        message: str,
        *,
        client_order_id: str | None = None,
        venue: str | None = None,
        raw: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.client_order_id = client_order_id
        self.venue = venue
        self.raw = raw or {}


class RateLimited(ExchangeError):
    """HTTP 429 (or Retry-After header surfaced) — caller should back off."""

    def __init__(self, message: str, *, retry_after_s: float | None = None) -> None:
        super().__init__(message)
        self.retry_after_s = retry_after_s


class SequenceGap(ExchangeError):
    """Coinbase WebSocket dropped frames — caller must reconcile via REST."""

    def __init__(self, channel: str, product_id: str, expected: int, got: int) -> None:
        super().__init__(f"sequence gap on {channel}/{product_id}: expected {expected}, got {got}")
        self.channel = channel
        self.product_id = product_id
        self.expected = expected
        self.got = got
        self.gap = got - expected


# ---------------------------------------------------------------------------
# Live-engine streaming facade
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class StreamEvent:
    """A single normalised event off an exchange stream — live-engine shape.

    Exactly one of ``tick`` / ``fill`` is populated. The discriminant is the
    field that is not ``None``; consumers branch on identity rather than
    introspecting a ``kind`` string (smaller mistake surface).

    Note: the ``tick`` and ``fill`` payloads here are the narrower
    ``quanta_core.util.types`` dataclasses (used by the live engine), not the
    adapter-layer ``Tick`` / ``Fill`` defined above. Concrete adapters convert
    at the boundary when wrapping their ``stream_*`` iterators into an
    ``ExchangeStream``.
    """

    tick: LiveTick | None = None
    fill: LiveFill | None = None


class ExchangeStream(abc.ABC):
    """Async stream of market + user events from one venue — live-engine view."""

    @abc.abstractmethod
    def __aiter__(self) -> AsyncIterator[StreamEvent]:
        """Return an async iterator over StreamEvents.

        The iterator MUST yield until ``aclose()`` is called or the
        underlying connection is permanently closed by the consumer.
        Transient disconnects are handled by the implementation
        (reconnect + replay subscriptions); they are invisible to the
        consumer.
        """

    @abc.abstractmethod
    async def aclose(self) -> None:
        """Cleanly shut down the stream and release resources."""


# ---------------------------------------------------------------------------
# Exchange ABC (canonical adapter contract — full broker API)
# ---------------------------------------------------------------------------


class Exchange(abc.ABC):
    """Abstract async exchange adapter — the live-engine streaming view.

    This ABC is intentionally **narrow** (open/list_positions/close + a
    ``name``) so the live engine and its tests can substitute a synthetic
    in-process exchange without implementing the full broker API.

    The richer **broker-adapter** contract — connect/get_account/submit_order/
    cancel_order/stream_ticks/stream_fills/etc. — lives on
    :class:`BrokerExchange` below. Concrete Alpaca / Coinbase adapters
    implement both via multiple inheritance or by subclassing
    :class:`BrokerExchange` and overriding ``open()``.
    """

    name: str

    @abc.abstractmethod
    async def open(self) -> ExchangeStream:
        """Open the data + user stream. Returns once authenticated."""

    @abc.abstractmethod
    async def list_positions(self) -> list[Any]:
        """REST snapshot of open positions.

        Returns ``list[quanta_core.util.types.Position]`` for live-engine
        callers, or ``list[PositionSnapshot]`` for broker-adapter callers
        — the live engine and reconciler use ``util.types.Position``.
        """

    @abc.abstractmethod
    async def close(self) -> None:
        """Close REST + WS clients. Safe to call multiple times."""


class BrokerExchange(Exchange):
    """Full broker-adapter contract on top of :class:`Exchange`.

    Concrete implementations:
        * :class:`quanta_core.exchanges.alpaca.AlpacaExchange`
        * :class:`quanta_core.exchanges.coinbase.CoinbaseExchange`

    All methods are coroutines. Lifecycle is ``connect()`` → use → ``disconnect()``.
    Streams (``stream_*``) MUST be cancellable via the surrounding
    ``anyio.create_task_group`` — they yield until cancelled.

    The live-engine ``open()`` method default-raises ``NotImplementedError``;
    adapters that wish to back the live engine override it to wrap their
    ``stream_ticks`` + ``stream_fills`` into an :class:`ExchangeStream`.
    """

    venue: str

    @abc.abstractmethod
    async def connect(self) -> None:
        """Open underlying sessions / WS connections. Idempotent."""

    @abc.abstractmethod
    async def disconnect(self) -> None:
        """Close everything. Safe to call without a prior ``connect``."""

    # -- account / portfolio --------------------------------------------

    @abc.abstractmethod
    async def get_account(self) -> AccountSnapshot:
        """One-shot REST snapshot of the account."""

    @abc.abstractmethod
    async def get_positions(self) -> list[PositionSnapshot]:
        """Current positions. ``asset_class`` MUST be populated per position."""

    # -- orders ----------------------------------------------------------

    @abc.abstractmethod
    async def submit_order(self, proposal: OrderProposal) -> OrderAck:
        """Submit a new order. Idempotent by ``proposal.client_order_id`` —
        re-submitting the same id returns the existing order (after a
        successful look-up) rather than firing a fresh one."""

    @abc.abstractmethod
    async def cancel_order(self, client_order_id: str) -> None:
        """Cancel by our id. No-op if already filled / canceled."""

    @abc.abstractmethod
    async def get_orders(self, status: OrderStatus | None = None) -> list[OrderAck]:
        """List orders, optionally filtered."""

    # -- streams ---------------------------------------------------------

    @abc.abstractmethod
    def stream_ticks(self, symbols: Sequence[str]) -> AsyncIterator[Tick]:
        """Subscribe to trades / last-prices for the given symbols."""

    @abc.abstractmethod
    def stream_fills(self) -> AsyncIterator[Fill]:
        """Subscribe to our account's fill stream."""

    @abc.abstractmethod
    def stream_orderbook(
        self,
        symbols: Sequence[str] | None = None,
        depth: int = 10,
    ) -> AsyncIterator[OrderbookSnapshot]:
        """Subscribe to L2 book updates."""

    # -- live-engine facade (default impls bridge to ``disconnect`` /
    #    ``get_positions``; adapters override ``open`` to provide a stream)

    async def open(self) -> ExchangeStream:
        raise NotImplementedError(
            f"{type(self).__name__} does not implement the live-engine ExchangeStream facade; "
            "override ``open`` to bridge stream_ticks + stream_fills into a StreamEvent iterator.",
        )

    async def list_positions(self) -> list[Any]:  # type: ignore[override]
        return await self.get_positions()

    async def close(self) -> None:
        await self.disconnect()


# ---------------------------------------------------------------------------
# Helpers used by concrete adapters
# ---------------------------------------------------------------------------


def _utc(dt: datetime | None = None) -> datetime:
    """Coerce to timezone-aware UTC. ``None`` yields ``utcnow``."""
    if dt is None:
        return datetime.now(UTC)
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def _to_decimal(value: Any) -> Decimal:
    """Coerce SDK-returned numbers to Decimal preserving string precision
    where possible."""
    if isinstance(value, Decimal):
        return value
    if value is None:
        return Decimal("0")
    return Decimal(str(value))


__all__ = [
    "AccountSnapshot",
    "AssetClass",
    "BrokerExchange",
    "Exchange",
    "ExchangeError",
    "ExchangeStream",
    "Fill",
    "OrderAck",
    "OrderProposal",
    "OrderRejected",
    "OrderStatus",
    "OrderType",
    "OrderbookLevel",
    "OrderbookSnapshot",
    "PositionSnapshot",
    "RateLimited",
    "SequenceGap",
    "Side",
    "StreamEvent",
    "Tick",
    "TimeInForce",
]
