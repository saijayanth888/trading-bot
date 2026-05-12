"""Typed payload dataclasses for the ledger writer methods.

These are deliberately thin: they exist to give the ``PostgresLedger`` an
input shape that ``mypy --strict`` can verify, NOT to carry business logic.
The strategy + execution layers convert their richer domain objects into
these payloads before crossing the ledger boundary.

All ``ts`` fields require a timezone-aware ``datetime``. The application
layer is UTC-only (``docs/quanta-core-v4/10-CODE_PATTERNS.md`` §1) and a
naive datetime is treated as a programmer error.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Final, Literal

Side = Literal["BUY", "SELL"]

_VALID_SIDES: Final[frozenset[str]] = frozenset({"BUY", "SELL"})


def _ensure_utc_aware(ts: datetime, field_name: str) -> datetime:
    """Reject naive datetimes; normalise tz-aware values to UTC.

    Parameters
    ----------
    ts:
        Candidate timestamp.
    field_name:
        Name of the dataclass field for the error message.

    Returns
    -------
    datetime
        ``ts`` converted to UTC if it carried a non-UTC tzinfo.

    Raises
    ------
    ValueError
        If ``ts`` is naive (``tzinfo is None``).
    """
    if ts.tzinfo is None:
        raise ValueError(f"{field_name} must be a timezone-aware datetime (got naive)")
    return ts.astimezone(UTC)


@dataclass(frozen=True, slots=True)
class Proposal:
    """An order intent captured before submission to a venue.

    The ``client_order_id`` is the application-supplied idempotency key
    (SHA256 → UUID5 per ``DESIGN-LOCK.md``). Repeated inserts with the same
    id are rejected by the ``proposals`` table's PRIMARY KEY.

    Attributes
    ----------
    client_order_id:
        UUID5 string. Must already be canonicalised.
    venue:
        Lower-case venue identifier (``"alpaca"`` | ``"coinbase"`` | ``"paper"``).
    symbol:
        Canonical symbol (``"BTC/USD"``, ``"AAPL"``, OCC option string).
    side:
        ``"BUY"`` or ``"SELL"``.
    qty:
        Order quantity. ``Decimal`` to avoid float rounding into Postgres
        ``NUMERIC``.
    limit_price:
        ``None`` for market orders; ``Decimal`` for limit / stop-limit.
    strategy:
        Strategy name that produced the proposal.
    intent:
        Free-form JSON-serialisable payload (the full ``OrderRequest``).
    created_at:
        Optional override; defaults to ``NOW()`` at the DB.
    """

    client_order_id: str
    venue: str
    symbol: str
    side: Side
    qty: Decimal
    strategy: str
    intent: dict[str, Any] = field(default_factory=dict)
    limit_price: Decimal | None = None
    created_at: datetime | None = None

    def __post_init__(self) -> None:
        if self.side not in _VALID_SIDES:
            raise ValueError(f"Proposal.side must be one of {_VALID_SIDES}, got {self.side!r}")
        if self.qty <= 0:
            raise ValueError(f"Proposal.qty must be > 0, got {self.qty!r}")
        if self.limit_price is not None and self.limit_price <= 0:
            raise ValueError(f"Proposal.limit_price must be > 0 when set, got {self.limit_price!r}")
        if self.created_at is not None:
            object.__setattr__(
                self,
                "created_at",
                _ensure_utc_aware(self.created_at, "Proposal.created_at"),
            )


@dataclass(frozen=True, slots=True)
class Fill:
    """A single execution event from a venue.

    Multiple fills may be attached to one proposal (partial fills); the
    application aggregates by ``client_order_id`` when computing position
    state.
    """

    client_order_id: str
    qty: Decimal
    price: Decimal
    side: Side
    ts: datetime
    fee: Decimal = Decimal("0")
    fee_currency: str | None = None
    venue_fill_id: str | None = None

    def __post_init__(self) -> None:
        if self.side not in _VALID_SIDES:
            raise ValueError(f"Fill.side must be one of {_VALID_SIDES}, got {self.side!r}")
        if self.qty <= 0:
            raise ValueError(f"Fill.qty must be > 0, got {self.qty!r}")
        if self.price <= 0:
            raise ValueError(f"Fill.price must be > 0, got {self.price!r}")
        if self.fee < 0:
            raise ValueError(f"Fill.fee must be >= 0, got {self.fee!r}")
        object.__setattr__(self, "ts", _ensure_utc_aware(self.ts, "Fill.ts"))


@dataclass(frozen=True, slots=True)
class Decision:
    """A debate / arbiter outcome row.

    The full bull/bear/arbiter transcript lives in ``debate`` as a JSON blob;
    ``outcome`` is the machine-readable verdict (``"BUY"`` / ``"SELL"`` /
    ``"NO_TRADE"`` / ``"BLOCKED"``); ``rationale`` is the human-readable
    one-liner the dashboard and weekly publisher quote.
    """

    debate: dict[str, Any]
    outcome: str
    symbol: str | None = None
    strategy: str | None = None
    rationale: str | None = None
    ts: datetime | None = None

    def __post_init__(self) -> None:
        if not self.outcome:
            raise ValueError("Decision.outcome must not be empty")
        if self.ts is not None:
            object.__setattr__(self, "ts", _ensure_utc_aware(self.ts, "Decision.ts"))
