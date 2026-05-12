"""Smoke tests for the typed exception classes."""

from __future__ import annotations

from quanta_core.ledger.errors import (
    LedgerError,
    ReservationConflictError,
    UnknownOrderError,
)


def test_reservation_conflict_error_carries_id() -> None:
    err = ReservationConflictError("abc")
    assert err.client_order_id == "abc"
    assert "abc" in str(err)
    assert isinstance(err, LedgerError)


def test_unknown_order_error_carries_id() -> None:
    err = UnknownOrderError("xyz")
    assert err.client_order_id == "xyz"
    assert "xyz" in str(err)
    assert isinstance(err, LedgerError)
