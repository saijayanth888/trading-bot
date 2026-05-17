"""Verify the on-disk migration files: presence, naming, idempotency."""

from __future__ import annotations

import re
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
import pytest_asyncio

from quanta_core.ledger import PostgresLedger
from quanta_core.ledger.postgres import _MIGRATIONS_DIR

from . import _fake_pg

_VERSION_RE = re.compile(r"^(\d{3})_[a-z0-9_]+\.sql$")


def test_migrations_dir_exists() -> None:
    assert _MIGRATIONS_DIR.is_dir(), f"missing {_MIGRATIONS_DIR}"
    files = sorted(_MIGRATIONS_DIR.glob("*.sql"))
    assert files, "no migration files found"


def test_migration_filenames_are_versioned() -> None:
    for path in _MIGRATIONS_DIR.glob("*.sql"):
        match = _VERSION_RE.match(path.name)
        assert match, f"bad migration name: {path.name}"


def test_migration_versions_are_sequential_from_001() -> None:
    from itertools import pairwise

    versions = sorted(
        int(_VERSION_RE.match(p.name).group(1))  # type: ignore[union-attr]
        for p in _MIGRATIONS_DIR.glob("*.sql")
    )
    assert versions[0] == 1
    for prev, nxt in pairwise(versions):
        assert nxt == prev + 1, f"gap between {prev} and {nxt}"


def test_migration_001_creates_required_tables() -> None:
    body = (Path(_MIGRATIONS_DIR) / "001_initial.sql").read_text()
    for required in (
        "reservations",
        "proposals",
        "orders",
        "fills",
        "decisions",
        "equity_snapshots",
        "quanta_schema_version",
    ):
        assert (
            re.search(
                rf"CREATE TABLE IF NOT EXISTS\s+{required}\b",
                body,
                re.IGNORECASE,
            )
            is not None
        ), f"migration 001 must create {required!r}"


def test_migration_002_adds_indices() -> None:
    body = (Path(_MIGRATIONS_DIR) / "002_add_indices.sql").read_text()
    assert "idx_proposals_symbol_created" in body
    assert "idx_fills_ts" in body
    assert "idx_decisions_ts" in body


@pytest_asyncio.fixture()
async def ledger(
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[PostgresLedger]:
    _fake_pg.install(monkeypatch)
    pg = PostgresLedger(dsn="postgresql://fake/test")
    await pg.connect()
    try:
        yield pg
    finally:
        await pg.close()
        _fake_pg.reset()


async def test_migrate_then_re_migrate_is_idempotent(
    ledger: PostgresLedger,
) -> None:
    first = await ledger.migrate()
    # Don't hardcode the migration count — the test should pass regardless
    # of how many .sql files live in migrations/. What matters: the FIRST
    # run applies *something*, the SECOND run applies nothing, and the
    # applied list matches what was returned.
    assert first, "expected first migrate() to apply at least one migration"
    assert first == sorted(first)
    second = await ledger.migrate()
    assert second == []
    assert await ledger.applied_migrations() == first
