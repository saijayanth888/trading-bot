"""Exchange connectivity: vendor-neutral ABCs + alpaca-py / coinbase-advanced-py wrappers.

Public surface:

* :class:`Exchange` — narrow async ABC (open/list_positions/close) used by
  the live engine and its in-process test fixtures.
* :class:`BrokerExchange` — full broker API ABC (connect/get_account/
  submit_order/cancel_order/stream_*); concrete adapters subclass this.
* :class:`AlpacaExchange` — alpaca-py wrapper (stocks · options · crypto).
* :class:`CoinbaseExchange` — coinbase-advanced-py wrapper (spot crypto).
* :class:`ExchangeStream` / :class:`StreamEvent` — live-engine streaming
  facade (single async iterator of normalised tick/fill events).
* :func:`make_client_order_id` — deterministic UUID7-flavoured idempotency key.

The strategy layer must NOT import this module directly — it talks to a
``Context`` that mediates. Execution engine + live engine are the two
legitimate consumers.
"""

from quanta_core.exchanges.base import (
    AccountSnapshot,
    BrokerExchange,
    Exchange,
    ExchangeError,
    ExchangeStream,
    Fill,
    OrderAck,
    OrderbookLevel,
    OrderbookSnapshot,
    OrderProposal,
    OrderRejected,
    PositionSnapshot,
    RateLimited,
    StreamEvent,
    Tick,
)
from quanta_core.exchanges.idempotency import IntentKey, make_client_order_id

__all__ = [
    "AccountSnapshot",
    "BrokerExchange",
    "Exchange",
    "ExchangeError",
    "ExchangeStream",
    "Fill",
    "IntentKey",
    "OrderAck",
    "OrderProposal",
    "OrderRejected",
    "OrderbookLevel",
    "OrderbookSnapshot",
    "PositionSnapshot",
    "RateLimited",
    "StreamEvent",
    "Tick",
    "make_client_order_id",
]
