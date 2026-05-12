"""Quanta Core ledger — Postgres-backed single source of truth.

Public surface:

* ``PostgresLedger`` — the async connection pool wrapper. The ONLY module in
  the codebase that imports :mod:`psycopg`.
* ``Proposal``, ``Fill``, ``Decision`` — typed dataclasses for the
  application-layer payloads accepted by the ledger writers.
* ``LedgerError``, ``ReservationConflictError``, ``UnknownOrderError`` — the
  typed exception tree raised by ledger operations. Callers either re-raise
  or convert to a structured ``Result`` (see ``docs/quanta-core-v4/10-CODE_PATTERNS.md``
  §1.4).

Migrations live under ``migrations/`` and are applied in lexical order by
``PostgresLedger.migrate``.
"""

from __future__ import annotations

from quanta_core.ledger.errors import (
    LedgerError,
    ReservationConflictError,
    UnknownOrderError,
)
from quanta_core.ledger.postgres import PostgresLedger
from quanta_core.ledger.types import Decision, Fill, Proposal

__all__ = [
    "Decision",
    "Fill",
    "LedgerError",
    "PostgresLedger",
    "Proposal",
    "ReservationConflictError",
    "UnknownOrderError",
]
