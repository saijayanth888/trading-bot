"""In-process fake of the psycopg 3 async pool surface used by ``PostgresLedger``.

The fake implements only the operations the production code actually calls
and dispatches each ``execute(sql, params)`` to a handler matched by the
*shape* of the SQL string (not a parser). This keeps the file small and
deterministic — when ``PostgresLedger`` adds a new SQL statement we wire up
exactly one handler.

The state lives in module-level dictionaries (``_TABLES``) and is reset by
:func:`reset`. Each call to ``AsyncConnectionPool.connection()`` returns a
fresh ``_FakeConnection`` that shares the same underlying state — matching
the behaviour of a real pool against a single database.
"""

from __future__ import annotations

import itertools
import threading
from datetime import UTC, datetime
from decimal import Decimal
from types import TracebackType
from typing import Any, Self

from psycopg.errors import UniqueViolation

# --------------------------------------------------------------------------- state

_LOCK = threading.Lock()
_PROPOSALS: dict[str, dict[str, Any]] = {}
_ORDERS: dict[str, dict[str, Any]] = {}
_FILLS: list[dict[str, Any]] = []
_DECISIONS: list[dict[str, Any]] = []
_EQUITY: dict[datetime, dict[str, Any]] = {}
_RESERVATIONS: dict[str, dict[str, Any]] = {}
_SCHEMA_VERSIONS: set[int] = set()

_FILL_ID_SEQ = itertools.count(1)
_DECISION_ID_SEQ = itertools.count(1)


def reset() -> None:
    """Reset every in-memory table. Tests call this in setup + teardown."""
    global _FILL_ID_SEQ, _DECISION_ID_SEQ
    with _LOCK:
        _PROPOSALS.clear()
        _ORDERS.clear()
        _FILLS.clear()
        _DECISIONS.clear()
        _EQUITY.clear()
        _RESERVATIONS.clear()
        _SCHEMA_VERSIONS.clear()
        _FILL_ID_SEQ = itertools.count(1)
        _DECISION_ID_SEQ = itertools.count(1)


def state() -> dict[str, Any]:
    """Snapshot the in-memory state for assertions."""
    with _LOCK:
        return {
            "proposals": {k: dict(v) for k, v in _PROPOSALS.items()},
            "orders": {k: dict(v) for k, v in _ORDERS.items()},
            "fills": [dict(f) for f in _FILLS],
            "decisions": [dict(d) for d in _DECISIONS],
            "equity": {k: dict(v) for k, v in _EQUITY.items()},
            "reservations": {k: dict(v) for k, v in _RESERVATIONS.items()},
            "versions": sorted(_SCHEMA_VERSIONS),
        }


# --------------------------------------------------------------------------- API


class _FakeCursor:
    def __init__(self, connection: _FakeConnection) -> None:
        self._connection = connection
        self._rows: list[Any] = []
        self.rowcount: int = 0
        self.description: list[tuple[str, ...]] | None = None
        self._row_idx = 0

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        return None

    async def execute(self, sql: str, params: tuple[Any, ...] | None = None) -> None:
        self._rows = []
        self._row_idx = 0
        self.rowcount = 0
        params = params or ()
        handler = _route(sql)
        handler(self, params)

    async def fetchone(self) -> Any:
        if self._row_idx >= len(self._rows):
            return None
        row = self._rows[self._row_idx]
        self._row_idx += 1
        return row

    async def fetchall(self) -> list[Any]:
        rest = self._rows[self._row_idx :]
        self._row_idx = len(self._rows)
        return rest


class _FakeConnection:
    def __init__(self) -> None:
        self._row_factory_dict = False

    @property
    def row_factory(self) -> Any:  # pragma: no cover
        return None

    @row_factory.setter
    def row_factory(self, value: Any) -> None:
        # quanta_core only sets dict_row, so any truthy value flips us to dict mode.
        self._row_factory_dict = value is not None and getattr(value, "__name__", "") == "dict_row"

    def cursor(self) -> _FakeCursor:
        return _FakeCursor(self)

    async def commit(self) -> None:
        return None

    async def rollback(self) -> None:
        return None


class _FakeConnectionContext:
    def __init__(self) -> None:
        self._conn: _FakeConnection | None = None

    async def __aenter__(self) -> _FakeConnection:
        self._conn = _FakeConnection()
        return self._conn

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        return None


class FakeAsyncConnectionPool:
    """Drop-in replacement for ``psycopg_pool.AsyncConnectionPool``."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self._opened = False

    async def open(self) -> None:
        self._opened = True

    async def close(self) -> None:
        self._opened = False

    def connection(self) -> _FakeConnectionContext:
        if not self._opened:
            raise RuntimeError("FakeAsyncConnectionPool not opened")
        return _FakeConnectionContext()


# --------------------------------------------------------------------------- dispatch

# Each handler receives (cursor, params) and mutates the cursor.
_HANDLERS: list[tuple[str, Any]] = []


def _route(sql: str) -> Any:
    needle = _normalise(sql)
    for marker, handler in _HANDLERS:
        if marker in needle:
            return handler
    raise NotImplementedError(f"fake psycopg: no handler matched SQL fragment\n----\n{sql}\n----")


def _normalise(sql: str) -> str:
    return " ".join(sql.split()).strip().upper()


def _emit(cursor: _FakeCursor, row: dict[str, Any] | None) -> None:
    if row is None:
        return
    if cursor._connection._row_factory_dict:
        cursor._rows.append(dict(row))
    else:
        cursor._rows.append(tuple(row.values()))
    cursor.description = [(k,) for k in (row or {}).keys()]


# --------------------------------------------------------------------------- handlers


def _h_select_1(cursor: _FakeCursor, params: tuple[Any, ...]) -> None:
    _emit(cursor, {"?column?": 1})
    cursor.rowcount = 1


def _h_create_schema_version(cursor: _FakeCursor, params: tuple[Any, ...]) -> None:
    cursor.rowcount = 0


def _h_select_schema_versions(cursor: _FakeCursor, params: tuple[Any, ...]) -> None:
    with _LOCK:
        for v in sorted(_SCHEMA_VERSIONS):
            _emit(cursor, {"version": v})


def _h_insert_schema_version(cursor: _FakeCursor, params: tuple[Any, ...]) -> None:
    # Two callers reach this handler:
    # 1. The migration runner's separate INSERT (params=(version, description))
    # 2. The literal INSERT inside each *.sql migration file (params=())
    # For (2) the version is encoded inline and we can no-op — the runner's
    # own INSERT will follow and capture the version explicitly.
    if not params:
        cursor.rowcount = 0
        return
    version, _desc = params
    with _LOCK:
        _SCHEMA_VERSIONS.add(int(version))
    cursor.rowcount = 1


def _h_migration_initial(cursor: _FakeCursor, params: tuple[Any, ...]) -> None:
    """Apply the initial schema. The fake state is dictionary-shaped so the
    DDL doesn't need to do anything — we just need to honour the call."""
    cursor.rowcount = 0


def _h_migration_indices(cursor: _FakeCursor, params: tuple[Any, ...]) -> None:
    cursor.rowcount = 0


def _h_insert_reservation(cursor: _FakeCursor, params: tuple[Any, ...]) -> None:
    cid, intent = params
    with _LOCK:
        if cid in _RESERVATIONS:
            raise UniqueViolation(
                f"duplicate key value violates unique constraint reservations_pkey: {cid}"
            )
        _RESERVATIONS[cid] = {
            "client_order_id": cid,
            "intent": _coerce_json(intent),
            "reserved_at": datetime.now(UTC),
        }
    cursor.rowcount = 1


def _h_select_reservation(cursor: _FakeCursor, params: tuple[Any, ...]) -> None:
    cid = params[0]
    with _LOCK:
        row = _RESERVATIONS.get(cid)
    if row is not None:
        _emit(cursor, row)


def _h_select_proposal(cursor: _FakeCursor, params: tuple[Any, ...]) -> None:
    cid = params[0]
    with _LOCK:
        row = _PROPOSALS.get(cid)
    if row is not None:
        _emit(cursor, row)


def _h_select_proposal_exists(cursor: _FakeCursor, params: tuple[Any, ...]) -> None:
    cid = params[0]
    with _LOCK:
        if cid in _PROPOSALS:
            _emit(cursor, {"?column?": 1})


def _h_select_order(cursor: _FakeCursor, params: tuple[Any, ...]) -> None:
    cid = params[0]
    with _LOCK:
        row = _ORDERS.get(cid)
    if row is not None:
        _emit(cursor, row)


def _h_select_order_exists(cursor: _FakeCursor, params: tuple[Any, ...]) -> None:
    cid = params[0]
    with _LOCK:
        if cid in _ORDERS:
            _emit(cursor, {"?column?": 1})


def _h_insert_proposal(cursor: _FakeCursor, params: tuple[Any, ...]) -> None:
    (
        cid,
        venue,
        symbol,
        side,
        qty,
        limit_price,
        strategy,
        intent,
        created_at,
    ) = params
    with _LOCK:
        if cid in _PROPOSALS:
            cursor.rowcount = 0
            return
        _PROPOSALS[cid] = {
            "client_order_id": cid,
            "venue": venue,
            "symbol": symbol,
            "side": side,
            "qty": qty,
            "limit_price": limit_price,
            "strategy": strategy,
            "intent": _coerce_json(intent),
            "created_at": created_at or datetime.now(UTC),
        }
    cursor.rowcount = 1


def _h_insert_order(cursor: _FakeCursor, params: tuple[Any, ...]) -> None:
    cid = params[0]
    with _LOCK:
        if cid in _ORDERS:
            cursor.rowcount = 0
            return
        _ORDERS[cid] = {
            "client_order_id": cid,
            "exchange_order_id": None,
            "status": "PROPOSED",
            "cancel_reason": None,
            "last_update": datetime.now(UTC),
        }
    cursor.rowcount = 1


def _h_update_ack(cursor: _FakeCursor, params: tuple[Any, ...]) -> None:
    exchange_id, cid = params
    with _LOCK:
        row = _ORDERS.get(cid)
        if row is None:
            cursor.rowcount = 0
            return
        if row["exchange_order_id"] is None:
            row["exchange_order_id"] = exchange_id
        if row["status"] == "PROPOSED":
            row["status"] = "ACKED"
        row["last_update"] = datetime.now(UTC)
    cursor.rowcount = 1


def _h_insert_fill(cursor: _FakeCursor, params: tuple[Any, ...]) -> None:
    (
        cid,
        venue_fill_id,
        qty,
        price,
        fee,
        fee_currency,
        side,
        ts,
    ) = params
    with _LOCK:
        fill_id = next(_FILL_ID_SEQ)
        _FILLS.append(
            {
                "id": fill_id,
                "client_order_id": cid,
                "venue_fill_id": venue_fill_id,
                "qty": qty,
                "price": price,
                "fee": fee,
                "fee_currency": fee_currency,
                "side": side,
                "ts": ts,
            }
        )
    _emit(cursor, {"id": fill_id})
    cursor.rowcount = 1


def _h_update_fill_status(cursor: _FakeCursor, params: tuple[Any, ...]) -> None:
    cid = params[0]
    with _LOCK:
        row = _ORDERS.get(cid)
        if row is None:
            cursor.rowcount = 0
            return
        if row["status"] in {"PROPOSED", "ACKED"}:
            row["status"] = "PARTIAL"
        row["last_update"] = datetime.now(UTC)
    cursor.rowcount = 1


def _h_update_cancel(cursor: _FakeCursor, params: tuple[Any, ...]) -> None:
    reason, cid = params
    with _LOCK:
        row = _ORDERS.get(cid)
        if row is None or row["status"] == "FILLED":
            cursor.rowcount = 0
            return
        row["status"] = "CANCELLED"
        if row["cancel_reason"] is None:
            row["cancel_reason"] = reason
        row["last_update"] = datetime.now(UTC)
    cursor.rowcount = 1


def _h_insert_decision(cursor: _FakeCursor, params: tuple[Any, ...]) -> None:
    ts, symbol, strategy, debate, outcome, rationale = params
    with _LOCK:
        decision_id = next(_DECISION_ID_SEQ)
        _DECISIONS.append(
            {
                "id": decision_id,
                "ts": ts or datetime.now(UTC),
                "symbol": symbol,
                "strategy": strategy,
                "debate": _coerce_json(debate),
                "outcome": outcome,
                "rationale": rationale,
            }
        )
    _emit(cursor, {"id": decision_id})
    cursor.rowcount = 1


def _h_insert_equity(cursor: _FakeCursor, params: tuple[Any, ...]) -> None:
    ts, equity, unrealized, drawdown_pct, cash = params
    with _LOCK:
        _EQUITY[ts] = {
            "ts": ts,
            "equity": equity,
            "unrealized": unrealized,
            "drawdown_pct": drawdown_pct,
            "cash": cash,
        }
    cursor.rowcount = 1


def _h_select_trades_for_week(cursor: _FakeCursor, params: tuple[Any, ...]) -> None:
    start, end = params
    rows: list[dict[str, Any]] = []
    with _LOCK:
        for p in _PROPOSALS.values():
            if not (start <= p["created_at"] < end):
                continue
            # Mirror the production INNER JOIN orders … AND status='FILLED'
            # added in audit 2026-05-16 (G7): proposals that never reached
            # a FILLED order are excluded from the weekly trades view.
            order = _ORDERS.get(p["client_order_id"])
            if not order or order.get("status") != "FILLED":
                continue
            related = [f for f in _FILLS if f["client_order_id"] == p["client_order_id"]]
            qty = sum((f["qty"] for f in related), Decimal("0"))
            if qty != 0:
                avg_price = sum(f["qty"] * f["price"] for f in related) / qty
            else:
                avg_price = None
            fees = sum((f["fee"] for f in related), Decimal("0"))
            first_ts = min((f["ts"] for f in related), default=None)
            last_ts = max((f["ts"] for f in related), default=None)
            rows.append(
                {
                    "client_order_id": p["client_order_id"],
                    "symbol": p["symbol"],
                    "side": p["side"],
                    "strategy": p["strategy"],
                    "qty": qty,
                    "avg_price": avg_price,
                    "fees": fees,
                    "first_fill": first_ts,
                    "last_fill": last_ts,
                    "proposed_at": p["created_at"],
                }
            )
    rows.sort(key=lambda r: r["proposed_at"])
    for row in rows:
        _emit(cursor, row)


def _h_select_equity(cursor: _FakeCursor, params: tuple[Any, ...]) -> None:
    start, end = params
    with _LOCK:
        rows = [
            {
                "ts": r["ts"],
                "equity": r["equity"],
                "unrealized": r["unrealized"],
                "drawdown_pct": r["drawdown_pct"],
                "cash": r["cash"],
            }
            for r in _EQUITY.values()
            if start <= r["ts"] < end
        ]
    rows.sort(key=lambda r: r["ts"])
    for row in rows:
        _emit(cursor, row)


def _coerce_json(value: Any) -> Any:
    """Strip a psycopg ``Jsonb`` wrapper if one is present."""
    if hasattr(value, "obj"):
        return value.obj
    return value


# Order matters — most specific markers first.
_HANDLERS.extend(
    [
        ("SELECT 1 FROM PROPOSALS WHERE CLIENT_ORDER_ID =", _h_select_proposal_exists),
        ("SELECT 1 FROM ORDERS WHERE CLIENT_ORDER_ID =", _h_select_order_exists),
        ("SELECT VERSION FROM QUANTA_SCHEMA_VERSION", _h_select_schema_versions),
        ("CREATE TABLE IF NOT EXISTS QUANTA_SCHEMA_VERSION", _h_create_schema_version),
        ("INSERT INTO QUANTA_SCHEMA_VERSION", _h_insert_schema_version),
        ("DROP TABLE IF EXISTS", _h_migration_indices),
        # Migrations: the .sql files contain DDL — match by a distinctive token.
        ("CREATE TABLE IF NOT EXISTS RESERVATIONS", _h_migration_initial),
        ("CREATE INDEX IF NOT EXISTS IDX_PROPOSALS_SYMBOL_CREATED", _h_migration_indices),
        # Reservations
        ("INSERT INTO RESERVATIONS (CLIENT_ORDER_ID, INTENT)", _h_insert_reservation),
        (
            "SELECT CLIENT_ORDER_ID, INTENT, RESERVED_AT FROM RESERVATIONS",
            _h_select_reservation,
        ),
        # Proposals
        ("INSERT INTO PROPOSALS", _h_insert_proposal),
        ("SELECT * FROM PROPOSALS WHERE CLIENT_ORDER_ID =", _h_select_proposal),
        # Orders
        ("INSERT INTO ORDERS (CLIENT_ORDER_ID, STATUS, LAST_UPDATE)", _h_insert_order),
        ("SELECT * FROM ORDERS WHERE CLIENT_ORDER_ID =", _h_select_order),
        ("UPDATE ORDERS SET EXCHANGE_ORDER_ID", _h_update_ack),
        ("UPDATE ORDERS SET STATUS = 'CANCELLED'", _h_update_cancel),
        (
            "UPDATE ORDERS SET STATUS = CASE WHEN STATUS IN ('PROPOSED', 'ACKED') THEN 'PARTIAL'",
            _h_update_fill_status,
        ),
        # Fills
        (
            "INSERT INTO FILLS (CLIENT_ORDER_ID, VENUE_FILL_ID",
            _h_insert_fill,
        ),
        # Decisions
        ("INSERT INTO DECISIONS (TS, SYMBOL", _h_insert_decision),
        # Equity snapshots
        ("INSERT INTO EQUITY_SNAPSHOTS", _h_insert_equity),
        (
            "SELECT TS, EQUITY, UNREALIZED, DRAWDOWN_PCT, CASH FROM EQUITY_SNAPSHOTS",
            _h_select_equity,
        ),
        # Trades-for-week join: production query now INNER-JOINs orders
        # on status='FILLED' before LEFT-JOINing fills (G7 audit fix).
        # Match the new fragment "JOIN ORDERS O" so the fake routes
        # correctly under both shapes (old + new).
        ("FROM PROPOSALS P JOIN ORDERS O", _h_select_trades_for_week),
        ("FROM PROPOSALS P LEFT JOIN FILLS F", _h_select_trades_for_week),
        # SELECT 1
        ("SELECT 1", _h_select_1),
    ]
)


def install(monkeypatch: Any) -> None:
    """Patch :mod:`quanta_core.ledger.postgres` to use this fake pool."""
    import quanta_core.ledger.postgres as ledger_mod

    monkeypatch.setattr(ledger_mod, "AsyncConnectionPool", FakeAsyncConnectionPool)
    reset()
