"""SQLAlchemy-backed idempotency store for ``client_order_id``.

Pattern (per docs/quanta-core-v4/04-RESEARCH-EXCHANGE_CONNECTIVITY.md §6.2):

1. **Reserve** the ``client_order_id`` in Postgres BEFORE the venue call.
   A unique index on ``client_order_id`` makes a double-reserve impossible.
   The reservation row stores the originating proposal as JSON, so a
   "lookup-on-network-error" replay can prove the intent matches.
2. **Commit** after the venue acknowledges, attaching the venue's order id
   and the fill data.
3. **Find existing** for the network-error replay path. Returns the row if
   it exists, ``None`` otherwise.

The schema is defined inline (SQLAlchemy 2.x declarative). The store
auto-creates the table on first use; production deploys should run
``IdempotencyStore.create_all`` from a migration step instead.

Cleanup: a 7-day TTL helper deletes committed rows older than the cutoff.
This is deliberately separated from the hot path — it runs from a Hermes
nightly cron, not on every reserve.
"""

from __future__ import annotations

import datetime as dt
import logging
from collections.abc import Mapping
from typing import Any

from pydantic import BaseModel, ConfigDict
from sqlalchemy import (
    JSON,
    DateTime,
    Engine,
    Index,
    String,
    delete,
    func,
    select,
)
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column

__all__ = [
    "TTL_DEFAULT",
    "Base",
    "DuplicateClientOrderId",
    "IdempotencyRow",
    "IdempotencyStore",
    "ReservationResult",
]


logger = logging.getLogger(__name__)


TTL_DEFAULT: dt.timedelta = dt.timedelta(days=7)


# ---------------------------------------------------------------------------
# ORM
# ---------------------------------------------------------------------------


class Base(DeclarativeBase):
    """Declarative base for the execution-module ORM tables.

    Kept local so that ``import quanta_core.execution.idempotency`` does not
    transitively bind every ORM table in the project. The ledger module
    will own its own declarative base.
    """


class IdempotencyRow(Base):
    """One row per ``client_order_id``.

    ``status`` tracks the lifecycle:
        ``reserved`` — INSERT succeeded; venue call not yet attempted.
        ``committed`` — venue acknowledged; ``exchange_order_id`` is set.
        ``abandoned`` — local-side rejection; safe to re-reserve a new id.

    The unique index on ``client_order_id`` is the load-bearing constraint;
    everything else is for forensics + the 7-day cleanup.
    """

    __tablename__ = "execution_idempotency"

    client_order_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    intent_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="reserved")
    exchange_order_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    fill_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    reserved_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    committed_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        Index("ix_execution_idempotency_committed_at", "committed_at"),
        Index("ix_execution_idempotency_status", "status"),
    )


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------


class ReservationResult(BaseModel):
    """Outcome of :meth:`IdempotencyStore.reserve`."""

    model_config = ConfigDict(frozen=True)

    client_order_id: str
    kind: str  # "new" | "duplicate" — duplicate is raised separately, kept for symmetry
    reserved_at: dt.datetime


class DuplicateClientOrderId(Exception):
    """Raised by :meth:`IdempotencyStore.reserve` when the id is already taken.

    The caller's correct response is to either (a) re-derive the id with a
    fresh intent timestamp if this is a genuine retry-after-success, or
    (b) call :meth:`IdempotencyStore.find_existing` to perform the
    network-error replay (look up what the venue did with the prior call).
    """


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------


class IdempotencyStore:
    """Reserve-then-commit on a Postgres unique index.

    Construct one per engine instance; pass the same SQLAlchemy ``Engine``
    used by the rest of the ledger.
    """

    def __init__(self, engine: Engine, *, now_fn: Any | None = None) -> None:
        """
        Parameters
        ----------
        engine
            SQLAlchemy 2.x ``Engine`` (typically ``create_engine(dsn, ...)``).
            Sync engine; the execution path is synchronous-by-design.
        now_fn
            Optional callable returning a tz-aware ``datetime``. Injected for
            tests. Defaults to ``datetime.now(tz=UTC)``.
        """
        self._engine = engine
        self._now_fn = now_fn or (lambda: dt.datetime.now(tz=dt.UTC))

    # ------------------------------------------------------------------
    # Schema management
    # ------------------------------------------------------------------

    def create_all(self) -> None:
        """Create the idempotency table if it does not exist.

        Idempotent. Safe to call on every process start; production deploys
        should prefer alembic / numbered SQL migrations.
        """
        # ``__table__`` is a ``Table`` at runtime; the typing stub returns
        # ``FromClause`` so we annotate inline.
        from sqlalchemy import Table as _Table  # local import keeps top tidy

        table: _Table = IdempotencyRow.__table__  # type: ignore[assignment]
        Base.metadata.create_all(self._engine, tables=[table])

    # ------------------------------------------------------------------
    # Hot path
    # ------------------------------------------------------------------

    def reserve(
        self,
        client_order_id: str,
        intent: Mapping[str, Any],
    ) -> ReservationResult:
        """Insert a reservation row; raise :class:`DuplicateClientOrderId` if taken.

        Parameters
        ----------
        client_order_id
            Deterministic id derived from the order intent. Caller owns the
            derivation (see ``execution.engine``).
        intent
            JSON-serialisable snapshot of the proposal. Stored verbatim so
            the network-error replay path can prove "the prior call's intent
            matches this call's intent" before deciding to reuse the id.

        Returns
        -------
        ReservationResult
            ``kind="new"`` on success.
        """
        now = self._now_fn()
        row = IdempotencyRow(
            client_order_id=client_order_id,
            intent_json=dict(intent),
            status="reserved",
            reserved_at=now,
        )
        try:
            with Session(self._engine) as session:
                session.add(row)
                session.commit()
        except IntegrityError as exc:
            logger.warning(
                "idempotency_duplicate",
                extra={"client_order_id": client_order_id},
            )
            raise DuplicateClientOrderId(client_order_id) from exc

        return ReservationResult(
            client_order_id=client_order_id,
            kind="new",
            reserved_at=now,
        )

    def commit(
        self,
        client_order_id: str,
        exchange_order_id: str,
        fill_data: Mapping[str, Any],
    ) -> None:
        """Update the reservation row with the venue id + fill payload.

        Raises ``LookupError`` if no reservation exists (programmer bug).
        """
        with Session(self._engine) as session:
            row = session.get(IdempotencyRow, client_order_id)
            if row is None:
                raise LookupError(f"commit called for unknown client_order_id={client_order_id!r}")
            row.exchange_order_id = exchange_order_id
            row.fill_json = dict(fill_data)
            row.status = "committed"
            row.committed_at = self._now_fn()
            session.commit()

    def abandon(self, client_order_id: str, reason: str) -> None:
        """Mark a reservation as abandoned (local-side reject, e.g. slippage).

        Safe no-op if the row does not exist — abandoning a never-reserved
        id has no destructive side effects.
        """
        with Session(self._engine) as session:
            row = session.get(IdempotencyRow, client_order_id)
            if row is None:
                return
            row.status = "abandoned"
            existing = dict(row.fill_json or {})
            existing["abandon_reason"] = reason
            row.fill_json = existing
            row.committed_at = self._now_fn()
            session.commit()

    def find_existing(self, client_order_id: str) -> IdempotencyRow | None:
        """Lookup-on-network-error: return the row if any, else ``None``.

        Detached from the session so callers may inspect it freely.
        """
        with Session(self._engine) as session:
            row = session.get(IdempotencyRow, client_order_id)
            if row is None:
                return None
            session.expunge(row)
            return row

    # ------------------------------------------------------------------
    # Cleanup
    # ------------------------------------------------------------------

    def cleanup(self, ttl: dt.timedelta = TTL_DEFAULT) -> int:
        """Delete committed/abandoned rows older than ``ttl``. Returns count.

        Reserved-status rows are NEVER deleted by this helper — those
        represent in-flight orders whose venue ack we never recorded, and
        losing them silently is precisely the kind of bug idempotency is
        meant to prevent. Operator must triage manually.
        """
        cutoff = self._now_fn() - ttl
        with Session(self._engine) as session:
            stmt = delete(IdempotencyRow).where(
                IdempotencyRow.committed_at.is_not(None),
                IdempotencyRow.committed_at < cutoff,
                IdempotencyRow.status.in_(("committed", "abandoned")),
            )
            result = session.execute(stmt)
            session.commit()
            # ``CursorResult.rowcount`` is the standard sync return; the
            # generic ``Result`` typing stub doesn't expose it.
            return int(getattr(result, "rowcount", 0) or 0)

    def count(self) -> int:
        """Return total row count. Used by tests + ops snapshots."""
        with Session(self._engine) as session:
            result = session.scalar(select(func.count()).select_from(IdempotencyRow))
            return int(result or 0)
