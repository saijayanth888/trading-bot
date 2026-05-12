"""Shared fixtures for the execution test suite.

Uses an in-memory SQLite engine for the idempotency store. SQLite honours
the unique constraint we depend on (``IntegrityError``) identically to
Postgres for our hot-path semantics. Postgres-only behaviour (advisory
locks, partial indexes) is not used here.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterator
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import Engine, create_engine

from quanta_core.execution.engine import (
    CancelOutcome,
    Exchange,
    ExchangeError,
    Fill,
    Ledger,
    OrderProposal,
    OrderResponse,
    Quote,
    RejectedReason,
    RetryableError,
    Side,
)
from quanta_core.execution.idempotency import Base, IdempotencyStore

UTC = dt.UTC


@pytest.fixture
def fixed_now() -> dt.datetime:
    return dt.datetime(2026, 5, 12, 12, 0, 0, tzinfo=UTC)


@pytest.fixture
def sqlite_engine() -> Iterator[Engine]:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    yield engine
    engine.dispose()


@pytest.fixture
def idem_store(sqlite_engine: Engine, fixed_now: dt.datetime) -> IdempotencyStore:
    return IdempotencyStore(sqlite_engine, now_fn=lambda: fixed_now)


@pytest.fixture
def proposal() -> OrderProposal:
    return OrderProposal(
        client_order_id="qc4-test-0001",
        symbol="BTC-USD",
        side=Side.BUY,
        qty=Decimal("0.1"),
        limit_px=Decimal("65000"),
        signal_px=Decimal("65000"),
        strategy_name="test",
        intent_ts_ms=1_715_000_000_000,
    )


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class FakeLedger:
    """Test-only ledger that records calls."""

    def __init__(self) -> None:
        self.fills: list[Fill] = []
        self.rejections: list[RejectedReason] = []

    def record_fill(self, fill: Fill) -> None:
        self.fills.append(fill)

    def record_rejection(self, reason: RejectedReason) -> None:
        self.rejections.append(reason)


class FakeExchange:
    """Test-only exchange. Use ``script_*`` attributes to drive behaviour.

    The fake is intentionally explicit (no clever auto-fill responses) so
    tests fail loudly when the engine wires it up wrong.
    """

    def __init__(self, *, quote_mid: Decimal, quote_ts: dt.datetime) -> None:
        self.quote_mid = quote_mid
        self.quote_ts = quote_ts
        self.place_calls: list[OrderProposal] = []
        self.cancel_calls: list[str] = []
        self.cancel_all_calls: list[str | None] = []
        self.place_responses: list[OrderResponse | BaseException] = []
        self.cancel_responses: list[OrderResponse | BaseException] = []
        self.cancel_all_responses: list[list[OrderResponse]] = []
        self.quote_exc: BaseException | None = None

    # --- script helpers ----------------------------------------------------

    def queue_place(self, *items: OrderResponse | BaseException) -> None:
        self.place_responses.extend(items)

    def queue_cancel(self, *items: OrderResponse | BaseException) -> None:
        self.cancel_responses.extend(items)

    def queue_cancel_all(self, items: list[OrderResponse]) -> None:
        self.cancel_all_responses.append(items)

    # --- Exchange protocol -------------------------------------------------

    def place(self, proposal: OrderProposal) -> OrderResponse:
        self.place_calls.append(proposal)
        if not self.place_responses:
            raise AssertionError("place called with empty script")
        head = self.place_responses.pop(0)
        if isinstance(head, BaseException):
            raise head
        return head

    def cancel(self, client_order_id: str) -> OrderResponse:
        self.cancel_calls.append(client_order_id)
        if not self.cancel_responses:
            raise AssertionError("cancel called with empty script")
        head = self.cancel_responses.pop(0)
        if isinstance(head, BaseException):
            raise head
        return head

    def get_quote(self, symbol: str) -> Quote:
        if self.quote_exc is not None:
            raise self.quote_exc
        return Quote(symbol=symbol, mid=self.quote_mid, ts=self.quote_ts)

    def cancel_all(self, symbol: str | None = None) -> list[OrderResponse]:
        self.cancel_all_calls.append(symbol)
        if not self.cancel_all_responses:
            return []
        return self.cancel_all_responses.pop(0)


@pytest.fixture
def fake_ledger() -> FakeLedger:
    return FakeLedger()


@pytest.fixture
def fake_exchange(fixed_now: dt.datetime) -> FakeExchange:
    return FakeExchange(quote_mid=Decimal("65000"), quote_ts=fixed_now)


def make_response(
    *,
    client_order_id: str = "qc4-test-0001",
    exchange_order_id: str = "venue-1",
    status: str = "FILLED",
    filled_qty: Decimal = Decimal("0.1"),
    avg_price: Decimal | None = Decimal("65000"),
    venue_ts: dt.datetime | None = None,
    raw: dict[str, Any] | None = None,
) -> OrderResponse:
    return OrderResponse(
        client_order_id=client_order_id,
        exchange_order_id=exchange_order_id,
        status=status,
        filled_qty=filled_qty,
        avg_price=avg_price,
        venue_ts=venue_ts or dt.datetime(2026, 5, 12, 12, 0, 0, tzinfo=UTC),
        raw=raw or {},
    )


__all__ = [
    "CancelOutcome",
    "Exchange",
    "ExchangeError",
    "FakeExchange",
    "FakeLedger",
    "Fill",
    "Ledger",
    "OrderProposal",
    "OrderResponse",
    "Quote",
    "RejectedReason",
    "RetryableError",
    "Side",
    "make_response",
]
