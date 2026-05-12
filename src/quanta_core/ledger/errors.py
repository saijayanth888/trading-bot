"""Typed exceptions raised by the ledger layer.

Per ``docs/quanta-core-v4/10-CODE_PATTERNS.md`` §1.4 every failure mode gets
its own typed subclass so callers can ``except`` precisely instead of fishing
strings out of ``RuntimeError``.
"""

from __future__ import annotations


class LedgerError(Exception):
    """Base class for every error raised by :mod:`quanta_core.ledger`."""


class ReservationConflictError(LedgerError):
    """A ``reserve(client_order_id, ...)`` collided with an existing reservation.

    This is the canonical idempotency signal: callers should treat it as
    "already done, nothing to do" — not as an error worth retrying.
    """

    def __init__(self, client_order_id: str) -> None:
        super().__init__(f"client_order_id already reserved: {client_order_id!r}")
        self.client_order_id = client_order_id


class UnknownOrderError(LedgerError):
    """An ``UPDATE`` against orders/proposals matched zero rows.

    Raised by ``record_ack`` and ``record_cancel`` when the supplied
    ``client_order_id`` does not exist in the ledger. Indicates a programmer
    error or a corrupted state file — callers should crash loud, not retry.
    """

    def __init__(self, client_order_id: str) -> None:
        super().__init__(f"no proposal/order row exists for client_order_id {client_order_id!r}")
        self.client_order_id = client_order_id
