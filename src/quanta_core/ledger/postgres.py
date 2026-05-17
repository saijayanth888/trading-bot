"""Postgres-backed ledger — the single source of truth for trades.

``PostgresLedger`` is the only module in the codebase that imports
:mod:`psycopg`. Every other module talks to the ledger through this class.

The class is **async** (psycopg 3 ``AsyncConnectionPool``), every write is
parameterised, and every public method has a NumPy-style docstring (CODE
PATTERNS doc 10 §1.11).

Idempotency is enforced two ways:

1. ``reserve()`` inserts into ``reservations`` whose ``client_order_id`` is a
   PRIMARY KEY — a second reserve with the same id raises
   :class:`~quanta_core.ledger.errors.ReservationConflictError`.
2. ``record_proposal()`` uses ``INSERT ... ON CONFLICT DO NOTHING`` on the
   ``proposals`` primary key, so retries are safe.

The migration runner reads ``migrations/*.sql`` in lexical order and applies
every file whose numeric prefix is greater than the highest version recorded
in ``quanta_schema_version``. The runner is idempotent — re-running it after
a successful migrate is a no-op.
"""

from __future__ import annotations

import contextlib
import json
import re
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from types import TracebackType
from typing import Any, Self, cast

from psycopg import AsyncConnection, errors
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from quanta_core.ledger.errors import (
    ReservationConflictError,
    UnknownOrderError,
)
from quanta_core.ledger.types import Decision, Fill, Proposal

_MIGRATIONS_DIR: Path = Path(__file__).parent / "migrations"
_MIGRATION_RE = re.compile(r"^(\d+)_.+\.sql$")


def _json_default(value: Any) -> Any:
    """JSON encoder fallback for :class:`Decimal` and :class:`datetime`.

    Postgres ``NUMERIC`` arrives as :class:`Decimal` and timestamps as
    :class:`datetime`; both are not JSON-serialisable. We coerce both to
    string so the payload survives a round-trip through Jsonb.
    """
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    raise TypeError(f"object of type {type(value).__name__} is not JSON-serialisable")


def _to_jsonb(payload: dict[str, Any]) -> Jsonb:
    """Wrap an application dict for safe binding into a JSONB column."""
    # Round-trip via json.dumps so the Decimal / datetime fallbacks fire
    # before psycopg sees the value. Loading back gives us a plain dict.
    encoded = json.dumps(payload, default=_json_default)
    return Jsonb(json.loads(encoded))


class PostgresLedger:
    """Async psycopg 3 wrapper for the quanta_core ledger.

    Use as an async context manager or call ``connect()`` / ``close()``
    explicitly. The pool is created lazily on first ``connect()``.

    Parameters
    ----------
    dsn:
        Postgres connection string (``postgresql://user:pw@host:port/db``).
    min_size:
        Minimum number of pooled connections (default ``1``).
    max_size:
        Maximum number of pooled connections (default ``10``).
    timeout:
        Acquire-timeout in seconds (default ``30.0``).
    application_name:
        Reported to Postgres for connection auditing.

    Examples
    --------
    >>> async def demo() -> None:
    ...     async with PostgresLedger(dsn="postgresql://...") as ledger:
    ...         await ledger.migrate()
    """

    def __init__(
        self,
        dsn: str,
        *,
        min_size: int = 1,
        max_size: int = 10,
        timeout: float = 30.0,
        application_name: str = "quanta_core",
        schema: str = "quanta_schema",
    ) -> None:
        if not dsn:
            raise ValueError("PostgresLedger requires a non-empty dsn")
        self._dsn = dsn
        self._min_size = min_size
        self._max_size = max_size
        self._timeout = timeout
        self._application_name = application_name
        # Server-side search_path so every unqualified table name in this
        # class (reservations / proposals / orders / fills / decisions /
        # equity_snapshots / run_state) resolves into the application
        # schema. Without this, the default ``"$user", public`` order sends
        # bare CREATE/INSERT/SELECT to `public` where the tables don't
        # exist — caught by the 2026-05-16 DB audit (Phase-3 live order
        # placement would have thrown `relation "fills" does not exist` on
        # the first call).
        self._schema = schema
        self._pool: AsyncConnectionPool | None = None

    # ------------------------------------------------------------------ lifecycle

    async def connect(self) -> None:
        """Open the connection pool. No-op if already open."""
        if self._pool is not None:
            return
        kwargs: dict[str, Any] = {
            "application_name": self._application_name,
            # psycopg passes connection options through as libpq parameters.
            # `-c search_path=...` runs at session start so every pooled
            # connection — including ones recycled after server-side resets —
            # gets the schema-qualified path. `public` stays in the chain so
            # public.* objects (trade_journal, regime_log, …) remain
            # reachable for cross-schema queries.
            "options": f"-c search_path={self._schema},public",
        }
        pool = AsyncConnectionPool(
            conninfo=self._dsn,
            min_size=self._min_size,
            max_size=self._max_size,
            timeout=self._timeout,
            open=False,
            kwargs=kwargs,
        )
        await pool.open()
        self._pool = pool

    async def close(self) -> None:
        """Close the connection pool. No-op if already closed."""
        if self._pool is None:
            return
        await self._pool.close()
        self._pool = None

    async def __aenter__(self) -> Self:
        await self.connect()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.close()

    @contextlib.asynccontextmanager
    async def _acquire(self) -> AsyncIterator[AsyncConnection]:
        """Acquire a pooled connection inside an async context."""
        if self._pool is None:
            raise RuntimeError("PostgresLedger.connect() must be called before any operation")
        async with self._pool.connection() as conn:
            yield conn

    # ------------------------------------------------------------------ migrations

    async def migrate(self) -> list[int]:
        """Apply every pending migration in lexical order.

        Returns
        -------
        list[int]
            Migration version numbers that were applied during this call
            (empty if the schema was already current).

        Raises
        ------
        RuntimeError
            If a migration file has a malformed name or no version prefix.
        """
        async with self._acquire() as conn:
            await self._ensure_version_table(conn)
            applied = await self._applied_versions(conn)
            pending = self._pending_migrations(applied)
            for version, path, description in pending:
                sql = path.read_text(encoding="utf-8")
                async with conn.cursor() as cur:
                    await cur.execute(sql)
                    await cur.execute(
                        """
                        INSERT INTO quanta_schema_version
                            (version, description)
                        VALUES (%s, %s)
                        ON CONFLICT (version) DO NOTHING
                        """,
                        (version, description),
                    )
                await conn.commit()
            return [v for v, _, _ in pending]

    async def applied_migrations(self) -> list[int]:
        """Return the versions already recorded in ``quanta_schema_version``."""
        async with self._acquire() as conn:
            await self._ensure_version_table(conn)
            return sorted(await self._applied_versions(conn))

    @staticmethod
    async def _ensure_version_table(conn: AsyncConnection) -> None:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                CREATE TABLE IF NOT EXISTS quanta_schema_version (
                    version     INTEGER PRIMARY KEY,
                    applied_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    description TEXT NOT NULL
                )
                """
            )
        await conn.commit()

    @staticmethod
    async def _applied_versions(conn: AsyncConnection) -> set[int]:
        async with conn.cursor() as cur:
            await cur.execute("SELECT version FROM quanta_schema_version")
            rows = await cur.fetchall()
        return {int(r[0]) for r in rows}

    def _pending_migrations(self, applied: set[int]) -> list[tuple[int, Path, str]]:
        out: list[tuple[int, Path, str]] = []
        if not _MIGRATIONS_DIR.is_dir():
            return out
        for path in sorted(_MIGRATIONS_DIR.glob("*.sql")):
            match = _MIGRATION_RE.match(path.name)
            if match is None:
                raise RuntimeError(
                    f"migration filename {path.name!r} does not match "
                    "the expected ``NNN_description.sql`` pattern"
                )
            version = int(match.group(1))
            if version in applied:
                continue
            description = path.stem
            out.append((version, path, description))
        return out

    # ------------------------------------------------------------------ idempotency

    async def reserve(self, client_order_id: str, intent: dict[str, Any]) -> None:
        """Reserve a slot for ``client_order_id``.

        Idempotency primitive: callers reserve BEFORE attempting any external
        side-effect. A second reservation with the same id raises
        :class:`ReservationConflictError`, which callers should treat as
        "already in flight, nothing to do".

        Parameters
        ----------
        client_order_id:
            UUID5 string.
        intent:
            Application-level intent payload (JSON-serialisable).

        Raises
        ------
        ReservationConflictError
            If ``client_order_id`` already exists in ``reservations``.
        """
        async with self._acquire() as conn, conn.cursor() as cur:
            try:
                await cur.execute(
                    """
                    INSERT INTO reservations (client_order_id, intent)
                    VALUES (%s, %s)
                    """,
                    (client_order_id, _to_jsonb(intent)),
                )
            except errors.UniqueViolation as exc:
                await conn.rollback()
                raise ReservationConflictError(client_order_id) from exc
            await conn.commit()

    async def find_existing(self, client_order_id: str) -> dict[str, Any] | None:
        """Look up an existing reservation/proposal/order row.

        Returns
        -------
        dict | None
            A merged view across ``reservations``, ``proposals`` and
            ``orders`` for the supplied id, or ``None`` if nothing matches.
            The shape is ``{"reserved": bool, "proposal": dict | None,
            "order": dict | None}``.
        """
        async with self._acquire() as conn:
            conn.row_factory = dict_row
            async with conn.cursor() as cur:
                await cur.execute(
                    "SELECT client_order_id, intent, reserved_at "
                    "FROM reservations WHERE client_order_id = %s",
                    (client_order_id,),
                )
                reservation = await cur.fetchone()
                await cur.execute(
                    "SELECT * FROM proposals WHERE client_order_id = %s",
                    (client_order_id,),
                )
                proposal = await cur.fetchone()
                await cur.execute(
                    "SELECT * FROM orders WHERE client_order_id = %s",
                    (client_order_id,),
                )
                order = await cur.fetchone()
        if reservation is None and proposal is None and order is None:
            return None
        return {
            "reserved": reservation is not None,
            "reservation": reservation,
            "proposal": proposal,
            "order": order,
        }

    # ------------------------------------------------------------------ writers

    async def record_proposal(self, proposal: Proposal) -> None:
        """Insert a proposal + matching ``orders`` row in ``PROPOSED`` state.

        Idempotent: a second call with the same ``client_order_id`` is a
        no-op (``ON CONFLICT DO NOTHING``).

        Parameters
        ----------
        proposal:
            Validated :class:`Proposal` payload.
        """
        async with self._acquire() as conn, conn.cursor() as cur:
            await cur.execute(
                """
                INSERT INTO proposals
                    (client_order_id, venue, symbol, side, qty,
                     limit_price, strategy, intent, created_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s,
                        COALESCE(%s, NOW()))
                ON CONFLICT (client_order_id) DO NOTHING
                """,
                (
                    proposal.client_order_id,
                    proposal.venue,
                    proposal.symbol,
                    proposal.side,
                    proposal.qty,
                    proposal.limit_price,
                    proposal.strategy,
                    _to_jsonb(proposal.intent),
                    proposal.created_at,
                ),
            )
            await cur.execute(
                """
                INSERT INTO orders (client_order_id, status, last_update)
                VALUES (%s, 'PROPOSED', NOW())
                ON CONFLICT (client_order_id) DO NOTHING
                """,
                (proposal.client_order_id,),
            )
            await conn.commit()

    async def record_ack(self, client_order_id: str, exchange_order_id: str) -> None:
        """Move ``orders.status`` from ``PROPOSED`` to ``ACKED``.

        Idempotent: if the row is already ``ACKED`` (or further along), the
        ``exchange_order_id`` is preserved and the call is a no-op.

        Raises
        ------
        UnknownOrderError
            If no row exists for ``client_order_id``.
        """
        async with self._acquire() as conn, conn.cursor() as cur:
            await cur.execute(
                """
                UPDATE orders
                SET exchange_order_id = COALESCE(exchange_order_id, %s),
                    status            = CASE
                        WHEN status = 'PROPOSED' THEN 'ACKED'
                        ELSE status
                    END,
                    last_update       = NOW()
                WHERE client_order_id = %s
                """,
                (exchange_order_id, client_order_id),
            )
            updated = cur.rowcount
            await conn.commit()
        if updated == 0:
            raise UnknownOrderError(client_order_id)

    async def record_fill(self, fill: Fill) -> int:
        """Insert a fill row. Returns the auto-assigned ``id``.

        Updates the parent order's ``status`` to ``PARTIAL`` (always; the
        engine flips it to ``FILLED`` once cumulative qty == ordered qty).

        Raises
        ------
        UnknownOrderError
            If no proposal exists for ``fill.client_order_id``.
        """
        async with self._acquire() as conn, conn.cursor() as cur:
            await cur.execute(
                "SELECT 1 FROM proposals WHERE client_order_id = %s",
                (fill.client_order_id,),
            )
            if await cur.fetchone() is None:
                await conn.rollback()
                raise UnknownOrderError(fill.client_order_id)
            await cur.execute(
                """
                INSERT INTO fills
                    (client_order_id, venue_fill_id, qty, price, fee,
                     fee_currency, side, ts)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING id
                """,
                (
                    fill.client_order_id,
                    fill.venue_fill_id,
                    fill.qty,
                    fill.price,
                    fill.fee,
                    fill.fee_currency,
                    fill.side,
                    fill.ts,
                ),
            )
            row = await cur.fetchone()
            if row is None:  # pragma: no cover — defensive; INSERT RETURNING always yields
                await conn.rollback()
                raise RuntimeError("INSERT ... RETURNING id returned no row — unreachable")
            fill_id = int(row[0])
            await cur.execute(
                """
                UPDATE orders
                SET status      = CASE
                    WHEN status IN ('PROPOSED', 'ACKED') THEN 'PARTIAL'
                    ELSE status
                END,
                last_update = NOW()
                WHERE client_order_id = %s
                """,
                (fill.client_order_id,),
            )
            await conn.commit()
        return fill_id

    async def record_cancel(self, client_order_id: str, reason: str) -> None:
        """Mark an order ``CANCELLED`` with the supplied reason.

        Idempotent: cancelling an already-cancelled order is a no-op.

        Raises
        ------
        UnknownOrderError
            If no row exists for ``client_order_id``.
        """
        async with self._acquire() as conn, conn.cursor() as cur:
            await cur.execute(
                """
                UPDATE orders
                SET status       = 'CANCELLED',
                    cancel_reason = COALESCE(cancel_reason, %s),
                    last_update  = NOW()
                WHERE client_order_id = %s
                  AND status <> 'FILLED'
                """,
                (reason, client_order_id),
            )
            updated = cur.rowcount
            if updated == 0:
                await cur.execute(
                    "SELECT 1 FROM orders WHERE client_order_id = %s",
                    (client_order_id,),
                )
                exists = await cur.fetchone()
                await conn.commit()
                if exists is None:
                    raise UnknownOrderError(client_order_id)
                # Otherwise: already terminal (FILLED). No-op.
                return
            await conn.commit()

    async def record_decision(self, decision: Decision) -> int:
        """Insert a decision row and return its primary-key ``id``."""
        async with self._acquire() as conn, conn.cursor() as cur:
            await cur.execute(
                """
                INSERT INTO decisions
                    (ts, symbol, strategy, debate, outcome, rationale)
                VALUES (COALESCE(%s, NOW()), %s, %s, %s, %s, %s)
                RETURNING id
                """,
                (
                    decision.ts,
                    decision.symbol,
                    decision.strategy,
                    _to_jsonb(decision.debate),
                    decision.outcome,
                    decision.rationale,
                ),
            )
            row = await cur.fetchone()
            await conn.commit()
        if row is None:  # pragma: no cover — defensive
            raise RuntimeError("INSERT INTO decisions RETURNING id returned no row")
        return int(row[0])

    async def record_equity_snapshot(
        self,
        *,
        ts: datetime,
        equity: Decimal,
        unrealized: Decimal = Decimal("0"),
        drawdown_pct: Decimal = Decimal("0"),
        cash: Decimal | None = None,
    ) -> None:
        """Record a single point on the equity curve.

        Idempotent on ``ts`` — repeating the same timestamp updates the
        snapshot in place (``ON CONFLICT DO UPDATE``).
        """
        async with self._acquire() as conn, conn.cursor() as cur:
            await cur.execute(
                """
                INSERT INTO equity_snapshots
                    (ts, equity, unrealized, drawdown_pct, cash)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (ts) DO UPDATE
                SET equity       = EXCLUDED.equity,
                    unrealized   = EXCLUDED.unrealized,
                    drawdown_pct = EXCLUDED.drawdown_pct,
                    cash         = EXCLUDED.cash
                """,
                (ts, equity, unrealized, drawdown_pct, cash),
            )
            await conn.commit()

    # ------------------------------------------------------------------ readers

    async def get_trades_for_week(self, week_iso: str) -> list[dict[str, Any]]:
        """Return per-trade rows (one row per ``client_order_id``) for one ISO week.

        The week is identified by an ISO 8601 string of the form ``YYYY-Www``
        (e.g. ``2026-W19``). The query computes the Monday-Sunday window in
        UTC and returns fills aggregated by ``client_order_id``.

        Output schema:
            ``client_order_id``, ``symbol``, ``side``, ``strategy``,
            ``qty``, ``avg_price``, ``fees``, ``first_fill``, ``last_fill``,
            ``proposed_at``.
        """
        start, end = _iso_week_window(week_iso)
        async with self._acquire() as conn:
            conn.row_factory = dict_row
            async with conn.cursor() as cur:
                await cur.execute(
                    """
                    SELECT
                        p.client_order_id,
                        p.symbol,
                        p.side,
                        p.strategy,
                        COALESCE(SUM(f.qty), 0)              AS qty,
                        CASE WHEN COALESCE(SUM(f.qty), 0) = 0
                             THEN NULL
                             ELSE SUM(f.qty * f.price) / SUM(f.qty)
                        END                                  AS avg_price,
                        COALESCE(SUM(f.fee), 0)              AS fees,
                        MIN(f.ts)                            AS first_fill,
                        MAX(f.ts)                            AS last_fill,
                        p.created_at                         AS proposed_at
                    FROM proposals p
                    LEFT JOIN fills f
                           ON f.client_order_id = p.client_order_id
                    WHERE p.created_at >= %s
                      AND p.created_at <  %s
                    GROUP BY p.client_order_id, p.symbol, p.side,
                             p.strategy, p.created_at
                    ORDER BY p.created_at
                    """,
                    (start, end),
                )
                rows = await cur.fetchall()
        return [dict(r) for r in rows]

    async def get_equity_curve(self, start: datetime, end: datetime) -> list[dict[str, Any]]:
        """Return equity snapshots in ``[start, end)`` ordered by ``ts``."""
        if start.tzinfo is None or end.tzinfo is None:
            raise ValueError("get_equity_curve requires timezone-aware start/end datetimes")
        async with self._acquire() as conn:
            conn.row_factory = dict_row
            async with conn.cursor() as cur:
                await cur.execute(
                    """
                    SELECT ts, equity, unrealized, drawdown_pct, cash
                    FROM equity_snapshots
                    WHERE ts >= %s AND ts < %s
                    ORDER BY ts
                    """,
                    (start, end),
                )
                rows = await cur.fetchall()
        return [dict(r) for r in rows]

    async def ping(self) -> bool:
        """Round-trip ``SELECT 1`` — returns ``True`` if the pool is healthy."""
        async with self._acquire() as conn, conn.cursor() as cur:
            await cur.execute("SELECT 1")
            row = await cur.fetchone()
        return row is not None and row[0] == 1

    # ------------------------------------------------------------------ helpers

    async def apply_schema_sql(self) -> None:
        """One-shot bootstrap: execute the consolidated ``schema.sql``.

        Use ONLY for fresh test containers that do not need the versioned
        migration log. Production deployments should call :meth:`migrate`.
        """
        sql_path = Path(__file__).parent / "schema.sql"
        sql = sql_path.read_text(encoding="utf-8")
        async with self._acquire() as conn, conn.cursor() as cur:
            await cur.execute(cast(Any, sql))
            await conn.commit()


_ISO_WEEK_RE = re.compile(r"^(\d{4})-W(\d{2})$")


def _iso_week_window(week_iso: str) -> tuple[datetime, datetime]:
    """Convert an ISO week ``YYYY-Www`` to a half-open UTC datetime window.

    Returns ``(monday_00:00:00Z, next_monday_00:00:00Z)``.
    """
    match = _ISO_WEEK_RE.match(week_iso)
    if match is None:
        raise ValueError(f"week_iso must look like 'YYYY-Www', got {week_iso!r}")
    year = int(match.group(1))
    week = int(match.group(2))
    if not 1 <= week <= 53:
        raise ValueError(f"week_iso week number {week} out of range (1..53)")
    # ``%G-W%V-%u`` parses ISO-week dates. Day 1 = Monday.
    from datetime import timedelta

    start = datetime.strptime(f"{year}-W{week:02d}-1", "%G-W%V-%u").replace(tzinfo=UTC)
    end = start + timedelta(days=7)
    return start, end
