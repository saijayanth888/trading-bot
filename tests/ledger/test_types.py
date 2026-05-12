"""Unit tests for the typed payload dataclasses."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from quanta_core.ledger.types import Decision, Fill, Proposal


def test_proposal_rejects_bad_side() -> None:
    with pytest.raises(ValueError, match="side must be one of"):
        Proposal(
            client_order_id="abc",
            venue="alpaca",
            symbol="AAPL",
            side="HOLD",  # type: ignore[arg-type]
            qty=Decimal("1"),
            strategy="x",
        )


def test_proposal_rejects_non_positive_qty() -> None:
    with pytest.raises(ValueError, match="qty must be > 0"):
        Proposal(
            client_order_id="abc",
            venue="alpaca",
            symbol="AAPL",
            side="BUY",
            qty=Decimal("0"),
            strategy="x",
        )


def test_proposal_rejects_non_positive_limit_price() -> None:
    with pytest.raises(ValueError, match="limit_price must be > 0"):
        Proposal(
            client_order_id="abc",
            venue="alpaca",
            symbol="AAPL",
            side="BUY",
            qty=Decimal("1"),
            strategy="x",
            limit_price=Decimal("0"),
        )


def test_proposal_normalises_tz_aware_created_at() -> None:
    plus_one = timezone(timedelta(hours=1))
    proposal = Proposal(
        client_order_id="abc",
        venue="alpaca",
        symbol="AAPL",
        side="BUY",
        qty=Decimal("1"),
        strategy="x",
        created_at=datetime(2026, 5, 12, 10, 0, tzinfo=plus_one),
    )
    assert proposal.created_at is not None
    assert proposal.created_at.tzinfo == UTC


def test_proposal_rejects_naive_created_at() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        Proposal(
            client_order_id="abc",
            venue="alpaca",
            symbol="AAPL",
            side="BUY",
            qty=Decimal("1"),
            strategy="x",
            created_at=datetime(2026, 5, 12, 10, 0),
        )


def test_proposal_accepts_minimal_fields() -> None:
    proposal = Proposal(
        client_order_id="abc",
        venue="alpaca",
        symbol="AAPL",
        side="BUY",
        qty=Decimal("1"),
        strategy="x",
    )
    assert proposal.created_at is None
    assert proposal.limit_price is None
    assert proposal.intent == {}


def test_fill_validates_fields() -> None:
    ts = datetime(2026, 5, 12, tzinfo=UTC)
    fill = Fill(
        client_order_id="abc",
        qty=Decimal("0.5"),
        price=Decimal("100"),
        side="BUY",
        ts=ts,
    )
    assert fill.ts == ts


def test_fill_rejects_naive_ts() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        Fill(
            client_order_id="abc",
            qty=Decimal("1"),
            price=Decimal("1"),
            side="BUY",
            ts=datetime(2026, 5, 12),
        )


def test_fill_rejects_negative_fee() -> None:
    ts = datetime(2026, 5, 12, tzinfo=UTC)
    with pytest.raises(ValueError, match="fee must be >= 0"):
        Fill(
            client_order_id="abc",
            qty=Decimal("1"),
            price=Decimal("1"),
            side="BUY",
            ts=ts,
            fee=Decimal("-0.1"),
        )


def test_fill_rejects_bad_side() -> None:
    ts = datetime(2026, 5, 12, tzinfo=UTC)
    with pytest.raises(ValueError, match="side must be one of"):
        Fill(
            client_order_id="abc",
            qty=Decimal("1"),
            price=Decimal("1"),
            side="HOLD",  # type: ignore[arg-type]
            ts=ts,
        )


def test_fill_rejects_non_positive_qty_and_price() -> None:
    ts = datetime(2026, 5, 12, tzinfo=UTC)
    with pytest.raises(ValueError, match="qty must be > 0"):
        Fill(
            client_order_id="abc",
            qty=Decimal("0"),
            price=Decimal("1"),
            side="BUY",
            ts=ts,
        )
    with pytest.raises(ValueError, match="price must be > 0"):
        Fill(
            client_order_id="abc",
            qty=Decimal("1"),
            price=Decimal("0"),
            side="BUY",
            ts=ts,
        )


def test_decision_requires_outcome() -> None:
    with pytest.raises(ValueError, match="outcome must not be empty"):
        Decision(debate={}, outcome="")


def test_decision_normalises_ts() -> None:
    ts = datetime(2026, 5, 12, 12, tzinfo=timezone(timedelta(hours=-5)))
    d = Decision(debate={"x": 1}, outcome="BUY", ts=ts)
    assert d.ts is not None
    assert d.ts.tzinfo == UTC


def test_decision_rejects_naive_ts() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        Decision(debate={}, outcome="BUY", ts=datetime(2026, 5, 12))


def test_decision_defaults() -> None:
    d = Decision(debate={"x": 1}, outcome="NO_TRADE")
    assert d.symbol is None
    assert d.strategy is None
    assert d.rationale is None
    assert d.ts is None
