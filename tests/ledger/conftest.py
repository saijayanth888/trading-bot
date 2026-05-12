"""Shared fixtures for ledger tests.

Strategy:

* If ``QUANTA_TEST_POSTGRES_DSN`` is set, use it as a real Postgres
  endpoint (CI / developer-with-docker case). The fixture spins up a fresh
  schema by applying every migration on entry and drops every quanta table
  on exit.
* Otherwise, mark the suite ``skip``. The ledger code is **also** exercised
  by ``test_postgres_mock.py``, which uses a hand-rolled fake psycopg
  protocol — that file gives us the >=95% coverage even when no DB is
  available.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio

from quanta_core.ledger import PostgresLedger

_DSN_ENV = "QUANTA_TEST_POSTGRES_DSN"


def _have_postgres() -> bool:
    return bool(os.environ.get(_DSN_ENV, "").strip())


postgres_required = pytest.mark.skipif(
    not _have_postgres(),
    reason=f"{_DSN_ENV} not set — skipping real-Postgres ledger tests",
)


@pytest_asyncio.fixture()
async def pg_ledger() -> AsyncIterator[PostgresLedger]:
    """Return a connected :class:`PostgresLedger` with a fresh schema."""
    dsn = os.environ.get(_DSN_ENV, "").strip()
    if not dsn:
        pytest.skip(f"{_DSN_ENV} not set")
    ledger = PostgresLedger(
        dsn=dsn,
        application_name=f"quanta_core_test_{uuid.uuid4().hex[:8]}",
    )
    await ledger.connect()
    try:
        # Drop any existing quanta tables, then re-apply migrations.
        await _drop_quanta_tables(ledger)
        await ledger.migrate()
        yield ledger
    finally:
        await _drop_quanta_tables(ledger)
        await ledger.close()


async def _drop_quanta_tables(ledger: PostgresLedger) -> None:
    sql = """
        DROP TABLE IF EXISTS fills CASCADE;
        DROP TABLE IF EXISTS orders CASCADE;
        DROP TABLE IF EXISTS reservations CASCADE;
        DROP TABLE IF EXISTS proposals CASCADE;
        DROP TABLE IF EXISTS decisions CASCADE;
        DROP TABLE IF EXISTS equity_snapshots CASCADE;
        DROP TABLE IF EXISTS quanta_schema_version CASCADE;
    """
    async with ledger._acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute(sql)
        await conn.commit()


@pytest.fixture(scope="session")
def event_loop_policy() -> asyncio.AbstractEventLoopPolicy:
    """Match pytest-asyncio's default; needed in pytest-asyncio>=0.23."""
    return asyncio.DefaultEventLoopPolicy()
