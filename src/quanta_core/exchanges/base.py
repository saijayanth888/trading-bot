"""Exchange ABC + shared value types.

Every concrete venue (Alpaca, Coinbase, Paper) implements this contract.
Vendor-specific responses are normalised on the way out so the strategy +
ledger layers never branch on venue.

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
"""

from __future__ import annotations

import abc
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Literal

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
# ABC
# ---------------------------------------------------------------------------


class Exchange(abc.ABC):
    """Abstract async exchange adapter.

    Concrete implementations:
        * :class:`quanta_core.exchanges.alpaca.AlpacaExchange`
        * :class:`quanta_core.exchanges.coinbase.CoinbaseExchange`

    All methods are coroutines. Lifecycle is ``connect()`` → use → ``disconnect()``.
    Streams (``stream_*``) MUST be cancellable via the surrounding
    ``anyio.create_task_group`` — they yield until cancelled.
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
        """Subscribe to trades / last-prices for the given symbols.

        Implementations MUST resubscribe on reconnect and SHOULD apply
        backoff with jitter. Cancellation propagates through the task group.
        """

    @abc.abstractmethod
    def stream_fills(self) -> AsyncIterator[Fill]:
        """Subscribe to our account's fill stream.

        Alpaca: ``trade_updates`` WebSocket (binary frames, handled by SDK).
        Coinbase: ``user`` channel on the WS feed.
        """

    @abc.abstractmethod
    def stream_orderbook(
        self,
        symbols: Sequence[str] | None = None,
        depth: int = 10,
    ) -> AsyncIterator[OrderbookSnapshot]:
        """Subscribe to L2 book updates.

        Coinbase: ``level2`` (snapshot + deltas, sequence-numbered).
        Alpaca: not supported for free-tier crypto/options — implementations
        may raise NotImplementedError per asset class.
        """


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
