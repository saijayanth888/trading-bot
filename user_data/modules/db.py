"""
Centralised PostgreSQL access for every module that previously used SQLite.

Connection string from `DATABASE_URL` env var, defaulting to the compose
network's postgres service. Schema is loaded once from
`user_data/data/schema.sql` on first connection — every CREATE / SELECT
block in there is idempotent.

Usage:

    from modules import db

    # sync, dict-rows
    with db.cursor() as cur:
        cur.execute("SELECT 1")
        rows = cur.fetchall()

    db.execute_one("INSERT INTO ... VALUES (%s, %s)", (a, b))
    rows = db.fetch_all("SELECT * FROM trade_journal WHERE pair = %s", (pair,))

The pool is lazy — the first call opens it; if Postgres isn't reachable
the call raises and the *caller* decides whether to swallow it (on-chain
modules degrade gracefully) or propagate (trade journal must succeed).
"""

from __future__ import annotations

import logging
import os
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Sequence
from urllib.parse import quote_plus

import psycopg
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

logger = logging.getLogger(__name__)

SCHEMA_FILE = Path(__file__).resolve().parent.parent / "data" / "schema.sql"

_pool: ConnectionPool | None = None
_pool_lock = threading.Lock()
_schema_loaded = False
_schema_lock = threading.Lock()


def dsn() -> str:
    """
    Resolve the Postgres DSN.

    Order of precedence:
      1. DATABASE_URL (explicit override; caller is responsible for any
         URL-encoding in the password segment).
      2. POSTGRES_* parts assembled with `urllib.parse.quote_plus` on the
         password — this is what makes special chars like '@' work.

    Inside docker-compose the network host is `postgres` on 5432; from the
    Spark host it's `localhost` on 5434.
    """
    explicit = os.environ.get("DATABASE_URL", "").strip()
    if explicit:
        return explicit
    user = os.environ.get("POSTGRES_USER", "tradebot")
    password = os.environ.get("POSTGRES_PASSWORD", "tradebot-change-me")
    host = os.environ.get("POSTGRES_HOST", "postgres")
    port = os.environ.get("POSTGRES_PORT", "5432")
    db = os.environ.get("POSTGRES_DB", "tradebot")
    return (
        f"postgresql://{quote_plus(user)}:{quote_plus(password)}"
        f"@{host}:{port}/{db}"
    )


def pool() -> ConnectionPool:
    """Lazy global pool. Safe to call from multiple threads."""
    global _pool
    if _pool is not None:
        return _pool
    with _pool_lock:
        if _pool is None:
            _pool = ConnectionPool(
                dsn(),
                min_size=1,
                max_size=10,
                max_idle=300,
                kwargs={"row_factory": dict_row, "autocommit": True},
                # Don't block module import if Postgres is briefly unreachable
                # — let the caller see the error on first use.
                open=False,
            )
            _pool.open(wait=False)
    return _pool


def close() -> None:
    """For test teardown."""
    global _pool, _schema_loaded
    if _pool is not None:
        try:
            _pool.close()
        except Exception:
            pass
        _pool = None
    _schema_loaded = False


@contextmanager
def connection() -> Iterator[psycopg.Connection]:
    """One pooled connection. Auto-returns on exit."""
    p = pool()
    with p.connection() as c:
        yield c


@contextmanager
def cursor(*, row_factory=None) -> Iterator[psycopg.Cursor]:
    """One cursor on a pooled connection. Schema is loaded on first use."""
    ensure_schema()
    with connection() as c:
        with c.cursor(row_factory=row_factory or dict_row) as cur:
            yield cur


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------


def ensure_schema() -> None:
    """Run schema.sql once per process. Idempotent on repeat invocations."""
    global _schema_loaded
    if _schema_loaded:
        return
    with _schema_lock:
        if _schema_loaded:
            return
        if not SCHEMA_FILE.exists():
            raise RuntimeError(f"schema file missing: {SCHEMA_FILE}")
        sql = SCHEMA_FILE.read_text()
        with connection() as c:
            with c.cursor() as cur:
                cur.execute(sql)
        _schema_loaded = True
        logger.info("[db] schema initialised against %s", _redacted_dsn())


def _redacted_dsn() -> str:
    """DSN with the password masked, for log lines."""
    raw = dsn()
    try:
        # postgresql://user:pass@host:port/db
        if "://" in raw and "@" in raw:
            scheme, rest = raw.split("://", 1)
            creds, host_part = rest.split("@", 1)
            if ":" in creds:
                user, _ = creds.split(":", 1)
                creds = f"{user}:***"
            return f"{scheme}://{creds}@{host_part}"
    except Exception:
        pass
    return raw


# ---------------------------------------------------------------------------
# Convenience execute / fetch wrappers
# ---------------------------------------------------------------------------


def execute_one(sql: str, params: Sequence[Any] = ()) -> int:
    """Run an INSERT/UPDATE/DELETE. Returns affected row count."""
    with cursor() as cur:
        cur.execute(sql, params)
        return cur.rowcount


def execute_returning(sql: str, params: Sequence[Any] = ()) -> Any:
    """Run an INSERT ... RETURNING and return the first row."""
    with cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchone()


def fetch_all(sql: str, params: Sequence[Any] = ()) -> list[dict]:
    with cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchall()


def fetch_one(sql: str, params: Sequence[Any] = ()) -> dict | None:
    with cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchone()


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------


def is_reachable(timeout: float = 2.0) -> bool:
    """Used by callers that want to fail-soft when Postgres is down."""
    try:
        with psycopg.connect(dsn(), connect_timeout=int(timeout)) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT 1")
                cur.fetchone()
        return True
    except Exception as exc:
        logger.debug("[db] not reachable: %s", exc)
        return False
