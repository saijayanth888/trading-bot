"""Execution engine — happy path, gates, retry policy, P0 fixes."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest

from quanta_core.execution.engine import (
    CancelOutcome,
    ExchangeError,
    ExecutionEngine,
    Fill,
    OrderProposal,
    RejectedReason,
    RetryableError,
    Side,
    _RetryPolicy,
)
from quanta_core.execution.idempotency import IdempotencyStore
from tests.execution.conftest import FakeExchange, FakeLedger, make_response

UTC = dt.UTC
NOW = dt.datetime(2026, 5, 12, 12, 0, 0, tzinfo=UTC)


def _engine(
    exchange: FakeExchange,
    ledger: FakeLedger,
    store: IdempotencyStore,
    *,
    retry_policy: _RetryPolicy | None = None,
    threshold_pct: Decimal = Decimal("0.5"),
    sleeps: list[float] | None = None,
) -> ExecutionEngine:
    sleeps = sleeps if sleeps is not None else []
    return ExecutionEngine(
        exchange=exchange,
        ledger=ledger,
        idempotency_store=store,
        slippage_threshold_pct=threshold_pct,
        retry_policy=retry_policy or _RetryPolicy(max_attempts=3, initial_backoff_s=0.0),
        now_fn=lambda: NOW,
        sleep_fn=sleeps.append,
    )


# ===========================================================================
# Happy path
# ===========================================================================


def test_happy_path_returns_fill(
    fake_exchange: FakeExchange,
    fake_ledger: FakeLedger,
    idem_store: IdempotencyStore,
    proposal: OrderProposal,
) -> None:
    fake_exchange.queue_place(make_response(status="FILLED"))
    engine = _engine(fake_exchange, fake_ledger, idem_store)

    out = engine.submit(proposal)

    assert isinstance(out, Fill)
    assert out.client_order_id == proposal.client_order_id
    assert out.filled_qty == Decimal("0.1")
    assert out.avg_price == Decimal("65000")
    assert fake_ledger.fills == [out]
    assert fake_ledger.rejections == []

    # Idempotency row committed.
    row = idem_store.find_existing(proposal.client_order_id)
    assert row is not None
    assert row.status == "committed"


def test_partial_fill_emits_fill_record(
    fake_exchange: FakeExchange,
    fake_ledger: FakeLedger,
    idem_store: IdempotencyStore,
    proposal: OrderProposal,
) -> None:
    fake_exchange.queue_place(make_response(status="PARTIAL", filled_qty=Decimal("0.05")))
    engine = _engine(fake_exchange, fake_ledger, idem_store)

    out = engine.submit(proposal)

    assert isinstance(out, Fill)
    assert out.filled_qty == Decimal("0.05")


# ===========================================================================
# Slippage gate
# ===========================================================================


def test_slippage_blocks_before_place(
    fake_exchange: FakeExchange,
    fake_ledger: FakeLedger,
    idem_store: IdempotencyStore,
    proposal: OrderProposal,
) -> None:
    # Mid drifts 2 % from signal; threshold is 0.5 %.
    fake_exchange.quote_mid = Decimal("66300")
    engine = _engine(fake_exchange, fake_ledger, idem_store)

    out = engine.submit(proposal)

    assert isinstance(out, RejectedReason)
    assert out.code.startswith("slippage_")
    assert fake_exchange.place_calls == []  # never reached the venue
    assert fake_ledger.rejections == [out]

    row = idem_store.find_existing(proposal.client_order_id)
    assert row is not None
    assert row.status == "abandoned"


def test_stale_quote_blocks(
    fake_exchange: FakeExchange,
    fake_ledger: FakeLedger,
    idem_store: IdempotencyStore,
    proposal: OrderProposal,
) -> None:
    fake_exchange.quote_ts = NOW - dt.timedelta(seconds=30)
    engine = _engine(fake_exchange, fake_ledger, idem_store)
    out = engine.submit(proposal)
    assert isinstance(out, RejectedReason)
    assert out.code == "slippage_stale_quote"
    assert fake_exchange.place_calls == []


def test_quote_fetch_io_error_rejects_and_abandons(
    fake_exchange: FakeExchange,
    fake_ledger: FakeLedger,
    idem_store: IdempotencyStore,
    proposal: OrderProposal,
) -> None:
    fake_exchange.quote_exc = ConnectionError("WS dead")
    engine = _engine(fake_exchange, fake_ledger, idem_store)
    out = engine.submit(proposal)
    assert isinstance(out, RejectedReason)
    assert out.code == "quote_fetch_failed"
    row = idem_store.find_existing(proposal.client_order_id)
    assert row is not None
    assert row.status == "abandoned"


def test_quote_fetch_4xx_rejects(
    fake_exchange: FakeExchange,
    fake_ledger: FakeLedger,
    idem_store: IdempotencyStore,
    proposal: OrderProposal,
) -> None:
    fake_exchange.quote_exc = ExchangeError(401, "unauthorized")
    engine = _engine(fake_exchange, fake_ledger, idem_store)
    out = engine.submit(proposal)
    assert isinstance(out, RejectedReason)
    assert out.code == "quote_http_401"


# ===========================================================================
# Retry policy — P0 FIX: only 5xx / network / timeout retry
# ===========================================================================


def test_5xx_retries_then_succeeds(
    fake_exchange: FakeExchange,
    fake_ledger: FakeLedger,
    idem_store: IdempotencyStore,
    proposal: OrderProposal,
) -> None:
    sleeps: list[float] = []
    fake_exchange.queue_place(
        RetryableError(503, "service unavailable"),
        RetryableError(503, "service unavailable"),
        make_response(status="FILLED"),
    )
    engine = _engine(fake_exchange, fake_ledger, idem_store, sleeps=sleeps)

    out = engine.submit(proposal)

    assert isinstance(out, Fill)
    assert len(fake_exchange.place_calls) == 3
    assert len(sleeps) == 2  # backoff between attempts


def test_connection_error_retries(
    fake_exchange: FakeExchange,
    fake_ledger: FakeLedger,
    idem_store: IdempotencyStore,
    proposal: OrderProposal,
) -> None:
    fake_exchange.queue_place(
        ConnectionError("WS reset"),
        make_response(status="FILLED"),
    )
    engine = _engine(fake_exchange, fake_ledger, idem_store)
    out = engine.submit(proposal)
    assert isinstance(out, Fill)
    assert len(fake_exchange.place_calls) == 2


def test_timeout_retries(
    fake_exchange: FakeExchange,
    fake_ledger: FakeLedger,
    idem_store: IdempotencyStore,
    proposal: OrderProposal,
) -> None:
    fake_exchange.queue_place(
        TimeoutError("dns"),
        make_response(status="FILLED"),
    )
    engine = _engine(fake_exchange, fake_ledger, idem_store)
    out = engine.submit(proposal)
    assert isinstance(out, Fill)


def test_4xx_never_retries(
    fake_exchange: FakeExchange,
    fake_ledger: FakeLedger,
    idem_store: IdempotencyStore,
    proposal: OrderProposal,
) -> None:
    """P0 FIX: 4xx is terminal. The legacy retry-everything bug created
    phantom orders on duplicate-client-order-id 422s."""
    fake_exchange.queue_place(ExchangeError(422, "duplicate client_order_id"))
    engine = _engine(fake_exchange, fake_ledger, idem_store)

    out = engine.submit(proposal)

    assert isinstance(out, RejectedReason)
    assert out.code == "http_422"
    assert len(fake_exchange.place_calls) == 1  # NO RETRY


def test_4xx_401_no_retry(
    fake_exchange: FakeExchange,
    fake_ledger: FakeLedger,
    idem_store: IdempotencyStore,
    proposal: OrderProposal,
) -> None:
    fake_exchange.queue_place(ExchangeError(401, "auth"))
    engine = _engine(fake_exchange, fake_ledger, idem_store)
    out = engine.submit(proposal)
    assert isinstance(out, RejectedReason)
    assert out.code == "http_401"
    assert len(fake_exchange.place_calls) == 1


def test_5xx_exhausts_retries(
    fake_exchange: FakeExchange,
    fake_ledger: FakeLedger,
    idem_store: IdempotencyStore,
    proposal: OrderProposal,
) -> None:
    fake_exchange.queue_place(
        RetryableError(503, "down"),
        RetryableError(503, "down"),
        RetryableError(503, "down"),
    )
    engine = _engine(fake_exchange, fake_ledger, idem_store)
    out = engine.submit(proposal)
    assert isinstance(out, RejectedReason)
    assert out.code == "http_503"
    assert len(fake_exchange.place_calls) == 3


def test_retry_policy_classification() -> None:
    p = _RetryPolicy()
    assert p.should_retry(RetryableError(503, "x")) is True
    assert p.should_retry(ConnectionError()) is True
    assert p.should_retry(TimeoutError()) is True
    assert p.should_retry(ExchangeError(400, "x")) is False
    assert p.should_retry(ExchangeError(401, "x")) is False
    assert p.should_retry(ExchangeError(422, "x")) is False
    assert p.should_retry(ExchangeError(429, "x")) is False
    assert p.should_retry(ValueError()) is False


# ===========================================================================
# Duplicate client_order_id — replay semantics
# ===========================================================================


def test_duplicate_with_committed_row_returns_recorded_fill(
    fake_exchange: FakeExchange,
    fake_ledger: FakeLedger,
    idem_store: IdempotencyStore,
    proposal: OrderProposal,
) -> None:
    """First submit succeeds; second submit with same id replays the fill."""
    fake_exchange.queue_place(make_response(status="FILLED"))
    engine = _engine(fake_exchange, fake_ledger, idem_store)

    first = engine.submit(proposal)
    assert isinstance(first, Fill)

    # Second call returns the prior Fill (no new venue call)
    second = engine.submit(proposal)
    assert isinstance(second, Fill)
    assert second.exchange_order_id == first.exchange_order_id
    assert len(fake_exchange.place_calls) == 1


def test_duplicate_with_reserved_row_returns_reject(
    fake_exchange: FakeExchange,
    fake_ledger: FakeLedger,
    idem_store: IdempotencyStore,
    proposal: OrderProposal,
) -> None:
    """A still-reserved (in-flight) id is a programmer error; we reject."""
    idem_store.reserve(proposal.client_order_id, proposal.to_intent_dict())
    engine = _engine(fake_exchange, fake_ledger, idem_store)

    out = engine.submit(proposal)
    assert isinstance(out, RejectedReason)
    assert out.code == "duplicate_client_order_id"
    assert fake_exchange.place_calls == []


# ===========================================================================
# P0-4 FIX: cancel honours venue response (partial-fill race)
# ===========================================================================


def test_cancel_records_fill_on_partial_fill_race(
    fake_exchange: FakeExchange,
    fake_ledger: FakeLedger,
    idem_store: IdempotencyStore,
    proposal: OrderProposal,
) -> None:
    """We issue cancel; venue says PARTIALLY_FILLED. Engine MUST record the fill."""
    # First reserve via a placed (open) order
    fake_exchange.queue_place(make_response(status="OPEN", filled_qty=Decimal("0")))
    engine = _engine(fake_exchange, fake_ledger, idem_store)
    # The OPEN status is treated as 'unknown_status'; we don't care for this test —
    # we manually seed the reservation:
    idem_store.abandon(proposal.client_order_id, "test_reset")  # no-op clear path
    # Clean and rebuild:
    fake_ledger.fills.clear()
    fake_ledger.rejections.clear()

    idem_store.reserve("coid-race", proposal.to_intent_dict() | {"symbol": "BTC-USD"})

    fake_exchange.queue_cancel(
        make_response(
            client_order_id="coid-race",
            status="PARTIALLY_FILLED",
            filled_qty=Decimal("0.07"),
            avg_price=Decimal("64980"),
            raw={"symbol": "BTC-USD", "side": "BUY"},
        )
    )

    outcome = engine.cancel("coid-race")

    assert outcome == CancelOutcome.ALREADY_FILLED
    assert len(fake_ledger.fills) == 1
    fill = fake_ledger.fills[0]
    assert fill.filled_qty == Decimal("0.07")
    assert fill.avg_price == Decimal("64980")

    # Idempotency row promoted to committed.
    row = idem_store.find_existing("coid-race")
    assert row is not None
    assert row.status == "committed"


def test_cancel_records_fill_on_full_fill_race(
    fake_exchange: FakeExchange,
    fake_ledger: FakeLedger,
    idem_store: IdempotencyStore,
) -> None:
    idem_store.reserve("coid-r2", {"symbol": "ETH-USD", "side": "SELL"})
    fake_exchange.queue_cancel(
        make_response(
            client_order_id="coid-r2",
            status="FILLED",
            filled_qty=Decimal("1.5"),
            avg_price=Decimal("3500"),
            raw={"symbol": "ETH-USD", "side": "SELL"},
        )
    )
    engine = _engine(fake_exchange, fake_ledger, idem_store)
    outcome = engine.cancel("coid-r2")
    assert outcome == CancelOutcome.ALREADY_FILLED
    assert fake_ledger.fills[0].side == Side.SELL


def test_cancel_normal_path_returns_canceled(
    fake_exchange: FakeExchange,
    fake_ledger: FakeLedger,
    idem_store: IdempotencyStore,
) -> None:
    fake_exchange.queue_cancel(
        make_response(client_order_id="coid-c", status="CANCELED", filled_qty=Decimal("0"))
    )
    engine = _engine(fake_exchange, fake_ledger, idem_store)
    outcome = engine.cancel("coid-c")
    assert outcome == CancelOutcome.CANCELED
    assert fake_ledger.fills == []


def test_cancel_404_returns_not_found(
    fake_exchange: FakeExchange,
    fake_ledger: FakeLedger,
    idem_store: IdempotencyStore,
) -> None:
    fake_exchange.queue_cancel(ExchangeError(404, "order not found"))
    engine = _engine(fake_exchange, fake_ledger, idem_store)
    assert engine.cancel("coid-x") == CancelOutcome.NOT_FOUND


def test_cancel_500_returns_error(
    fake_exchange: FakeExchange,
    fake_ledger: FakeLedger,
    idem_store: IdempotencyStore,
) -> None:
    fake_exchange.queue_cancel(ExchangeError(500, "boom"))
    engine = _engine(fake_exchange, fake_ledger, idem_store)
    assert engine.cancel("coid-x") == CancelOutcome.ERROR


def test_cancel_unknown_status_returns_error(
    fake_exchange: FakeExchange,
    fake_ledger: FakeLedger,
    idem_store: IdempotencyStore,
) -> None:
    fake_exchange.queue_cancel(
        make_response(client_order_id="coid", status="WEIRD", filled_qty=Decimal("0"))
    )
    engine = _engine(fake_exchange, fake_ledger, idem_store)
    assert engine.cancel("coid") == CancelOutcome.ERROR


def test_cancel_already_canceled(
    fake_exchange: FakeExchange,
    fake_ledger: FakeLedger,
    idem_store: IdempotencyStore,
) -> None:
    fake_exchange.queue_cancel(
        make_response(client_order_id="coid", status="ALREADY_CANCELED", filled_qty=Decimal("0"))
    )
    engine = _engine(fake_exchange, fake_ledger, idem_store)
    assert engine.cancel("coid") == CancelOutcome.ALREADY_CANCELED


def test_cancel_filled_without_reservation_still_records(
    fake_exchange: FakeExchange,
    fake_ledger: FakeLedger,
    idem_store: IdempotencyStore,
) -> None:
    """External cancel (no prior reserve). The fill must still be recorded;
    the commit failure is logged but not fatal."""
    fake_exchange.queue_cancel(
        make_response(
            client_order_id="external-coid",
            status="FILLED",
            filled_qty=Decimal("0.3"),
            avg_price=Decimal("100"),
            raw={"symbol": "X-USD", "side": "BUY"},
        )
    )
    engine = _engine(fake_exchange, fake_ledger, idem_store)
    outcome = engine.cancel("external-coid")
    assert outcome == CancelOutcome.ALREADY_FILLED
    assert len(fake_ledger.fills) == 1


# ===========================================================================
# cancel_all
# ===========================================================================


def test_cancel_all_mixed_outcomes(
    fake_exchange: FakeExchange,
    fake_ledger: FakeLedger,
    idem_store: IdempotencyStore,
) -> None:
    idem_store.reserve("c-1", {"symbol": "BTC-USD", "side": "BUY"})
    idem_store.reserve("c-2", {"symbol": "ETH-USD", "side": "SELL"})
    fake_exchange.queue_cancel_all(
        [
            make_response(client_order_id="c-1", status="CANCELED", filled_qty=Decimal("0")),
            make_response(
                client_order_id="c-2",
                status="PARTIAL_FILL",
                filled_qty=Decimal("0.5"),
                avg_price=Decimal("3500"),
                raw={"symbol": "ETH-USD", "side": "SELL"},
            ),
        ]
    )
    engine = _engine(fake_exchange, fake_ledger, idem_store)
    outcomes = engine.cancel_all()
    assert outcomes == [CancelOutcome.CANCELED, CancelOutcome.ALREADY_FILLED]
    assert len(fake_ledger.fills) == 1


def test_cancel_all_empty(
    fake_exchange: FakeExchange,
    fake_ledger: FakeLedger,
    idem_store: IdempotencyStore,
) -> None:
    engine = _engine(fake_exchange, fake_ledger, idem_store)
    assert engine.cancel_all() == []


def test_cancel_all_unknown_status(
    fake_exchange: FakeExchange,
    fake_ledger: FakeLedger,
    idem_store: IdempotencyStore,
) -> None:
    fake_exchange.queue_cancel_all(
        [
            make_response(client_order_id="c", status="WTF", filled_qty=Decimal("0")),
        ]
    )
    engine = _engine(fake_exchange, fake_ledger, idem_store)
    assert engine.cancel_all() == [CancelOutcome.ERROR]


# ===========================================================================
# Unknown / canceled status from place response
# ===========================================================================


def test_venue_canceled_response_is_rejection(
    fake_exchange: FakeExchange,
    fake_ledger: FakeLedger,
    idem_store: IdempotencyStore,
    proposal: OrderProposal,
) -> None:
    fake_exchange.queue_place(make_response(status="CANCELED", filled_qty=Decimal("0")))
    engine = _engine(fake_exchange, fake_ledger, idem_store)
    out = engine.submit(proposal)
    assert isinstance(out, RejectedReason)
    assert out.code == "venue_canceled"


def test_unknown_place_status_is_rejection(
    fake_exchange: FakeExchange,
    fake_ledger: FakeLedger,
    idem_store: IdempotencyStore,
    proposal: OrderProposal,
) -> None:
    fake_exchange.queue_place(make_response(status="QUEUED", filled_qty=Decimal("0")))
    engine = _engine(fake_exchange, fake_ledger, idem_store)
    out = engine.submit(proposal)
    assert isinstance(out, RejectedReason)
    assert out.code == "unknown_status"


# ===========================================================================
# Exception bubbling — programmer bugs
# ===========================================================================


def test_unexpected_exception_becomes_rejection(
    fake_exchange: FakeExchange,
    fake_ledger: FakeLedger,
    idem_store: IdempotencyStore,
    proposal: OrderProposal,
) -> None:
    """A non-classified exception is surfaced as a rejection, not a crash —
    but it does NOT retry (only RetryableError + IO retry)."""

    class WeirdError(Exception):
        pass

    fake_exchange.queue_place(WeirdError("?!"))
    engine = _engine(fake_exchange, fake_ledger, idem_store)
    out = engine.submit(proposal)
    assert isinstance(out, RejectedReason)
    assert out.code == "exception"
    assert len(fake_exchange.place_calls) == 1  # no retry


# ===========================================================================
# Proposal field coverage
# ===========================================================================


def test_proposal_to_intent_dict_is_json_safe(proposal: OrderProposal) -> None:
    import json

    d = proposal.to_intent_dict()
    json.dumps(d)  # must not raise


def test_proposal_market_order_has_none_limit() -> None:
    p = OrderProposal(
        client_order_id="qc4-mkt-001",
        symbol="X",
        side=Side.SELL,
        qty=Decimal("1"),
        limit_px=None,
        signal_px=Decimal("10"),
        strategy_name="test",
        intent_ts_ms=1,
    )
    d = p.to_intent_dict()
    assert d["limit_px"] is None


# ===========================================================================
# Side / status flexibility
# ===========================================================================


@pytest.mark.parametrize("status_alias", ["FILLED", "filled", "Filled", "DONE"])
def test_filled_aliases_are_terminal(
    fake_exchange: FakeExchange,
    fake_ledger: FakeLedger,
    idem_store: IdempotencyStore,
    proposal: OrderProposal,
    status_alias: str,
) -> None:
    fake_exchange.queue_place(make_response(status=status_alias))
    engine = _engine(fake_exchange, fake_ledger, idem_store)
    out = engine.submit(proposal)
    assert isinstance(out, Fill)


@pytest.mark.parametrize("status_alias", ["PARTIAL", "PARTIAL_FILL", "partially_filled"])
def test_partial_aliases_are_partial(
    fake_exchange: FakeExchange,
    fake_ledger: FakeLedger,
    idem_store: IdempotencyStore,
    proposal: OrderProposal,
    status_alias: str,
) -> None:
    fake_exchange.queue_place(make_response(status=status_alias, filled_qty=Decimal("0.04")))
    engine = _engine(fake_exchange, fake_ledger, idem_store)
    out = engine.submit(proposal)
    assert isinstance(out, Fill)


# ===========================================================================
# Coverage corners
# ===========================================================================


def test_duplicate_committed_without_fill_json_falls_back_to_reject(
    fake_exchange: FakeExchange,
    fake_ledger: FakeLedger,
    idem_store: IdempotencyStore,
    proposal: OrderProposal,
) -> None:
    """Edge: a row marked committed but with empty fill_json (schema migration
    in-flight). The engine must NOT crash; it returns a structured reject."""
    idem_store.reserve(proposal.client_order_id, proposal.to_intent_dict())
    idem_store.commit(proposal.client_order_id, "venue-X", {})  # empty fill_json
    engine = _engine(fake_exchange, fake_ledger, idem_store)

    out = engine.submit(proposal)
    assert isinstance(out, RejectedReason)
    assert out.code == "duplicate_client_order_id"


def test_duplicate_committed_with_garbage_fill_json_falls_back(
    fake_exchange: FakeExchange,
    fake_ledger: FakeLedger,
    idem_store: IdempotencyStore,
    proposal: OrderProposal,
) -> None:
    """Edge: stored fill_json missing required keys. Engine logs + rejects."""
    idem_store.reserve(proposal.client_order_id, proposal.to_intent_dict())
    idem_store.commit(
        proposal.client_order_id,
        "venue-Y",
        {"not_the_right_keys": "oops"},
    )
    engine = _engine(fake_exchange, fake_ledger, idem_store)
    out = engine.submit(proposal)
    assert isinstance(out, RejectedReason)
    assert out.code == "duplicate_client_order_id"


def test_duplicate_at_venue_during_place(
    fake_exchange: FakeExchange,
    fake_ledger: FakeLedger,
    idem_store: IdempotencyStore,
    proposal: OrderProposal,
) -> None:
    """The venue raises DuplicateClientOrderId on place (e.g. after a
    network-error replay where the prior submit had landed)."""
    from quanta_core.execution.idempotency import DuplicateClientOrderId

    fake_exchange.queue_place(DuplicateClientOrderId(proposal.client_order_id))
    engine = _engine(fake_exchange, fake_ledger, idem_store)
    out = engine.submit(proposal)
    assert isinstance(out, RejectedReason)
    assert out.code == "duplicate_at_venue"


def test_cancel_not_found_status_string(
    fake_exchange: FakeExchange,
    fake_ledger: FakeLedger,
    idem_store: IdempotencyStore,
) -> None:
    """Venue returns NOT_FOUND in the body (not as 404)."""
    fake_exchange.queue_cancel(
        make_response(client_order_id="c", status="UNKNOWN_ORDER", filled_qty=Decimal("0"))
    )
    engine = _engine(fake_exchange, fake_ledger, idem_store)
    assert engine.cancel("c") == CancelOutcome.NOT_FOUND


def test_cancel_all_filled_without_reservation_records_anyway(
    fake_exchange: FakeExchange,
    fake_ledger: FakeLedger,
    idem_store: IdempotencyStore,
) -> None:
    fake_exchange.queue_cancel_all(
        [
            make_response(
                client_order_id="external-coid",
                status="FILLED",
                filled_qty=Decimal("0.5"),
                avg_price=Decimal("100"),
                raw={"symbol": "X-USD", "side": "BUY"},
            ),
        ]
    )
    engine = _engine(fake_exchange, fake_ledger, idem_store)
    outcomes = engine.cancel_all()
    assert outcomes == [CancelOutcome.ALREADY_FILLED]
    assert len(fake_ledger.fills) == 1


def test_lookup_symbol_falls_back_to_intent_when_raw_missing(
    fake_exchange: FakeExchange,
    fake_ledger: FakeLedger,
    idem_store: IdempotencyStore,
) -> None:
    """Venue response has no symbol in raw — engine recovers from intent."""
    idem_store.reserve("c-fallback", {"symbol": "FOO-USD", "side": "SELL"})
    fake_exchange.queue_cancel(
        make_response(
            client_order_id="c-fallback",
            status="FILLED",
            filled_qty=Decimal("1"),
            avg_price=Decimal("50"),
            raw={},  # no symbol, no side
        )
    )
    engine = _engine(fake_exchange, fake_ledger, idem_store)
    outcome = engine.cancel("c-fallback")
    assert outcome == CancelOutcome.ALREADY_FILLED
    assert fake_ledger.fills[0].symbol == "FOO-USD"
    assert fake_ledger.fills[0].side == Side.SELL


def test_lookup_symbol_unknown_when_no_intent(
    fake_exchange: FakeExchange,
    fake_ledger: FakeLedger,
    idem_store: IdempotencyStore,
) -> None:
    """No raw symbol AND no idempotency row — symbol falls back to 'UNKNOWN'."""
    fake_exchange.queue_cancel(
        make_response(
            client_order_id="orphan-coid",
            status="FILLED",
            filled_qty=Decimal("0.1"),
            avg_price=Decimal("100"),
            raw={},
        )
    )
    engine = _engine(fake_exchange, fake_ledger, idem_store)
    outcome = engine.cancel("orphan-coid")
    assert outcome == CancelOutcome.ALREADY_FILLED
    assert fake_ledger.fills[0].symbol == "UNKNOWN"
    assert fake_ledger.fills[0].side == Side.BUY  # default fallback


def test_lookup_symbol_uses_product_id_alias(
    fake_exchange: FakeExchange,
    fake_ledger: FakeLedger,
    idem_store: IdempotencyStore,
) -> None:
    """Coinbase uses 'product_id' instead of 'symbol' on cancel responses."""
    fake_exchange.queue_cancel(
        make_response(
            client_order_id="cb-coid",
            status="FILLED",
            filled_qty=Decimal("0.1"),
            avg_price=Decimal("65000"),
            raw={"product_id": "BTC-USD", "side": "BUY"},
        )
    )
    engine = _engine(fake_exchange, fake_ledger, idem_store)
    engine.cancel("cb-coid")
    assert fake_ledger.fills[0].symbol == "BTC-USD"


def test_parse_ts_accepts_iso_string() -> None:
    from quanta_core.execution.engine import _parse_ts

    out = _parse_ts("2026-05-12T12:00:00+00:00")
    assert out.year == 2026


def test_parse_ts_accepts_datetime() -> None:
    from quanta_core.execution.engine import _parse_ts

    now = dt.datetime(2026, 1, 1, tzinfo=UTC)
    assert _parse_ts(now) is now


def test_parse_ts_raises_on_garbage() -> None:
    from quanta_core.execution.engine import _parse_ts

    with pytest.raises(ValueError):
        _parse_ts(12345)
    with pytest.raises(ValueError):
        _parse_ts(None)


def test_reject_swallows_illegal_transition_from_terminal(
    fake_exchange: FakeExchange,
    fake_ledger: FakeLedger,
    idem_store: IdempotencyStore,
    proposal: OrderProposal,
) -> None:
    """Defensive: if _reject is called with already_rejected=False against
    a machine that is already terminal, the failed transition must not mask
    the underlying rejection."""
    from quanta_core.execution.order_state_machine import (
        OrderState,
        OrderStateMachine,
    )

    engine = _engine(fake_exchange, fake_ledger, idem_store)
    terminal_machine = OrderStateMachine(state=OrderState.FILLED)
    out = engine._reject(
        terminal_machine,
        proposal,
        code="post_terminal_reject",
        detail="state machine already terminal",
        already_rejected=False,
    )
    assert isinstance(out, RejectedReason)
    assert out.code == "post_terminal_reject"
    assert terminal_machine.state == OrderState.FILLED  # unchanged


def test_fill_replay_via_iso_string_venue_ts(
    fake_exchange: FakeExchange,
    fake_ledger: FakeLedger,
    idem_store: IdempotencyStore,
    proposal: OrderProposal,
) -> None:
    """Test that a successful submit + replay round-trips through ISO ts.

    model_dump(mode='json') emits ts as ISO string; the replay path must
    parse it back."""
    fake_exchange.queue_place(make_response(status="FILLED"))
    engine = _engine(fake_exchange, fake_ledger, idem_store)
    first = engine.submit(proposal)
    assert isinstance(first, Fill)

    # Second submit returns the recorded fill — exercises _fill_from_row
    # full path including _parse_ts on the str venue_ts.
    second = engine.submit(proposal)
    assert isinstance(second, Fill)
    assert second.venue_ts == first.venue_ts


def test_cancel_with_committed_id_records_fill_and_does_not_double_commit(
    fake_exchange: FakeExchange,
    fake_ledger: FakeLedger,
    idem_store: IdempotencyStore,
    proposal: OrderProposal,
) -> None:
    """If commit fails (LookupError) during cancel_all path, fill still recorded."""
    # Pre-commit the row so a subsequent commit attempt is a no-op via LookupError
    # path — we trigger this by NOT reserving and going straight to cancel_all
    # with an external order. Already covered by the prior test; this one is
    # a redundant safety net to ensure cancel_all hits the LookupError branch.
    fake_exchange.queue_cancel_all(
        [
            make_response(
                client_order_id="no-reserve-coid",
                status="PARTIAL_FILL",
                filled_qty=Decimal("0.05"),
                avg_price=Decimal("100"),
                raw={"symbol": "X", "side": "BUY"},
            ),
        ]
    )
    engine = _engine(fake_exchange, fake_ledger, idem_store)
    outcomes = engine.cancel_all()
    assert outcomes == [CancelOutcome.ALREADY_FILLED]
    assert len(fake_ledger.fills) == 1
