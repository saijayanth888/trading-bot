"""Exchange connectivity: vendor-neutral ABC + alpaca-py / coinbase-advanced-py wrappers.

Public surface:

* :class:`Exchange` — async ABC every concrete adapter implements.
* :class:`AlpacaExchange` — alpaca-py wrapper (stocks · options · crypto).
* :class:`CoinbaseExchange` — coinbase-advanced-py wrapper (spot crypto).
* :func:`make_client_order_id` — deterministic UUID7-flavoured idempotency key.

The strategy layer must NOT import this module directly — it talks to a
``Context`` that mediates. Execution engine is the one legitimate consumer.
"""

from quanta_core.exchanges.base import (
    AccountSnapshot,
    Exchange,
    ExchangeError,
    Fill,
    OrderAck,
    OrderbookLevel,
    OrderbookSnapshot,
    OrderProposal,
    OrderRejected,
    PositionSnapshot,
    RateLimited,
    Tick,
)
from quanta_core.exchanges.idempotency import IntentKey, make_client_order_id

__all__ = [
    "AccountSnapshot",
    "Exchange",
    "ExchangeError",
    "Fill",
    "IntentKey",
    "OrderAck",
    "OrderProposal",
    "OrderRejected",
    "OrderbookLevel",
    "OrderbookSnapshot",
    "PositionSnapshot",
    "RateLimited",
    "Tick",
    "make_client_order_id",
]
