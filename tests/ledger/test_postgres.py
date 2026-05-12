"""Behavioural tests for ``PostgresLedger`` against an in-memory psycopg fake.

The fake (``_fake_pg``) implements only the SQL shapes ``PostgresLedger``
actually emits. Every public method is exercised end-to-end here without a
real Postgres instance.

When ``QUANTA_TEST_POSTGRES_DSN`` is set the fixtures in ``conftest.py`` ALSO
run a parallel roundtrip against a real database; the fake tests are
authoritative for CI coverage.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
import pytest_asyncio

from quanta_core.ledger import (
    Decision,
    Fill,
    PostgresLedger,
    Proposal,
    ReservationConflictError,
    UnknownOrderError,
)

from . import _fake_pg


@pytest_asyncio.fixture()
async def ledger(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[PostgresLedger]:
    _fake_pg.install(monkeypatch)
    pg = PostgresLedger(dsn="postgresql://fake/test")
    await pg.connect()
    try:
        yield pg
    finally:
        await pg.close()
        _fake_pg.reset()


# --------------------------------------------------------------------------- lifecycle


async def test_connect_is_idempotent(ledger: PostgresLedger) -> None:
    # Already connected from the fixture — calling again must be a no-op.
    await ledger.connect()
    assert await ledger.ping() is True


async def test_close_is_idempotent(monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_pg.install(monkeypatch)
    pg = PostgresLedger(dsn="postgresql://fake/test")
    await pg.close()  # never connected
    await pg.connect()
    await pg.close()
    await pg.close()
    _fake_pg.reset()


async def test_constructor_rejects_empty_dsn() -> None:
    with pytest.raises(ValueError, match="non-empty dsn"):
        PostgresLedger(dsn="")


async def test_methods_require_connect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _fake_pg.install(monkeypatch)
    pg = PostgresLedger(dsn="postgresql://fake/test")
    with pytest.raises(RuntimeError, match="connect"):
        await pg.ping()
    _fake_pg.reset()


async def test_async_context_manager(monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_pg.install(monkeypatch)
    async with PostgresLedger(dsn="postgresql://fake/test") as pg:
        assert await pg.ping() is True
    _fake_pg.reset()


# --------------------------------------------------------------------------- migrations


async def test_migrate_applies_pending_versions(
    ledger: PostgresLedger,
) -> None:
    applied = await ledger.migrate()
    assert applied == [1, 2]
    # Re-running is a no-op.
    assert await ledger.migrate() == []
    versions = await ledger.applied_migrations()
    assert versions == [1, 2]


async def test_pending_migrations_rejects_bad_filename(
    ledger: PostgresLedger,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bad_dir = tmp_path / "migrations"
    bad_dir.mkdir()
    (bad_dir / "not_a_migration.sql").write_text("SELECT 1;")
    import quanta_core.ledger.postgres as ledger_mod

    monkeypatch.setattr(ledger_mod, "_MIGRATIONS_DIR", bad_dir)
    with pytest.raises(RuntimeError, match="does not match"):
        await ledger.migrate()


async def test_pending_migrations_empty_dir(
    ledger: PostgresLedger,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import quanta_core.ledger.postgres as ledger_mod

    monkeypatch.setattr(ledger_mod, "_MIGRATIONS_DIR", tmp_path / "missing")
    assert await ledger.migrate() == []


# --------------------------------------------------------------------------- reservations


async def test_reserve_records_unique_client_order_id(
    ledger: PostgresLedger,
) -> None:
    await ledger.migrate()
    await ledger.reserve("client-1", {"side": "BUY", "qty": "1"})
    state = _fake_pg.state()
    assert "client-1" in state["reservations"]
    assert state["reservations"]["client-1"]["intent"] == {
        "side": "BUY",
        "qty": "1",
    }


async def test_reserve_conflict_raises_typed_error(
    ledger: PostgresLedger,
) -> None:
    await ledger.migrate()
    await ledger.reserve("client-1", {})
    with pytest.raises(ReservationConflictError) as exc_info:
        await ledger.reserve("client-1", {})
    assert exc_info.value.client_order_id == "client-1"


async def test_find_existing_returns_none_when_unknown(
    ledger: PostgresLedger,
) -> None:
    await ledger.migrate()
    result = await ledger.find_existing("never-seen")
    assert result is None


async def test_find_existing_returns_reservation_and_proposal(
    ledger: PostgresLedger,
) -> None:
    await ledger.migrate()
    await ledger.reserve("c1", {"intent": "X"})
    proposal = _build_proposal("c1")
    await ledger.record_proposal(proposal)
    record = await ledger.find_existing("c1")
    assert record is not None
    assert record["reserved"] is True
    assert record["proposal"] is not None
    assert record["order"] is not None


# --------------------------------------------------------------------------- proposals + ack


async def test_record_proposal_is_idempotent(
    ledger: PostgresLedger,
) -> None:
    await ledger.migrate()
    p = _build_proposal("c1")
    await ledger.record_proposal(p)
    await ledger.record_proposal(p)
    state = _fake_pg.state()
    assert len(state["proposals"]) == 1
    assert state["orders"]["c1"]["status"] == "PROPOSED"


async def test_record_ack_flips_status_and_keeps_exchange_id(
    ledger: PostgresLedger,
) -> None:
    await ledger.migrate()
    await ledger.record_proposal(_build_proposal("c1"))
    await ledger.record_ack("c1", "exchange-AAA")
    state = _fake_pg.state()
    assert state["orders"]["c1"]["status"] == "ACKED"
    assert state["orders"]["c1"]["exchange_order_id"] == "exchange-AAA"
    # Second ack must not overwrite the first exchange id.
    await ledger.record_ack("c1", "exchange-BBB")
    state2 = _fake_pg.state()
    assert state2["orders"]["c1"]["exchange_order_id"] == "exchange-AAA"


async def test_record_ack_unknown_order_raises(
    ledger: PostgresLedger,
) -> None:
    await ledger.migrate()
    with pytest.raises(UnknownOrderError):
        await ledger.record_ack("ghost", "exchange-AAA")


# --------------------------------------------------------------------------- fills


async def test_record_fill_returns_id_and_updates_status(
    ledger: PostgresLedger,
) -> None:
    await ledger.migrate()
    await ledger.record_proposal(_build_proposal("c1"))
    fill_id = await ledger.record_fill(_build_fill("c1"))
    assert isinstance(fill_id, int)
    state = _fake_pg.state()
    assert state["orders"]["c1"]["status"] == "PARTIAL"
    assert len(state["fills"]) == 1
    assert state["fills"][0]["id"] == fill_id


async def test_record_fill_without_proposal_raises(
    ledger: PostgresLedger,
) -> None:
    await ledger.migrate()
    with pytest.raises(UnknownOrderError):
        await ledger.record_fill(_build_fill("ghost"))


# --------------------------------------------------------------------------- cancel


async def test_record_cancel_marks_order_cancelled(
    ledger: PostgresLedger,
) -> None:
    await ledger.migrate()
    await ledger.record_proposal(_build_proposal("c1"))
    await ledger.record_cancel("c1", "client_request")
    state = _fake_pg.state()
    assert state["orders"]["c1"]["status"] == "CANCELLED"
    assert state["orders"]["c1"]["cancel_reason"] == "client_request"


async def test_record_cancel_unknown_order_raises(
    ledger: PostgresLedger,
) -> None:
    await ledger.migrate()
    with pytest.raises(UnknownOrderError):
        await ledger.record_cancel("ghost", "x")


async def test_record_cancel_no_op_when_filled(
    ledger: PostgresLedger,
) -> None:
    await ledger.migrate()
    await ledger.record_proposal(_build_proposal("c1"))
    # Manually flip status to FILLED to test the no-op branch.
    state = _fake_pg.state()
    state["orders"]["c1"]["status"] = "FILLED"
    _fake_pg._ORDERS["c1"]["status"] = "FILLED"
    # Should not raise even though SQL UPDATE matched zero rows.
    await ledger.record_cancel("c1", "noop")
    assert _fake_pg.state()["orders"]["c1"]["status"] == "FILLED"


# --------------------------------------------------------------------------- decisions


async def test_record_decision_returns_id(
    ledger: PostgresLedger,
) -> None:
    await ledger.migrate()
    d = Decision(
        debate={"bull": "x", "bear": "y"},
        outcome="BUY",
        symbol="BTC/USD",
        strategy="mean_rev_tft",
        rationale="oversold + sentiment positive",
    )
    decision_id = await ledger.record_decision(d)
    assert isinstance(decision_id, int)
    assert len(_fake_pg.state()["decisions"]) == 1


# --------------------------------------------------------------------------- equity


async def test_record_equity_snapshot_upserts(
    ledger: PostgresLedger,
) -> None:
    await ledger.migrate()
    ts = datetime(2026, 5, 12, tzinfo=UTC)
    await ledger.record_equity_snapshot(
        ts=ts,
        equity=Decimal("100000"),
        unrealized=Decimal("0"),
        drawdown_pct=Decimal("0.01"),
        cash=Decimal("50000"),
    )
    await ledger.record_equity_snapshot(
        ts=ts,
        equity=Decimal("99500"),
        unrealized=Decimal("0"),
        drawdown_pct=Decimal("0.005"),
        cash=None,
    )
    state = _fake_pg.state()
    assert len(state["equity"]) == 1
    assert state["equity"][ts]["equity"] == Decimal("99500")


async def test_get_equity_curve_filters_by_window(
    ledger: PostgresLedger,
) -> None:
    await ledger.migrate()
    t0 = datetime(2026, 5, 1, tzinfo=UTC)
    for i in range(5):
        await ledger.record_equity_snapshot(
            ts=t0 + timedelta(days=i),
            equity=Decimal(100000 + i * 1000),
            unrealized=Decimal("0"),
            drawdown_pct=Decimal("0"),
        )
    curve = await ledger.get_equity_curve(
        start=t0 + timedelta(days=1),
        end=t0 + timedelta(days=4),
    )
    assert len(curve) == 3
    assert curve[0]["equity"] == Decimal("101000")


async def test_get_equity_curve_rejects_naive_dates(
    ledger: PostgresLedger,
) -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        await ledger.get_equity_curve(
            start=datetime(2026, 5, 1),
            end=datetime(2026, 5, 7, tzinfo=UTC),
        )


# --------------------------------------------------------------------------- trades-of-week


async def test_get_trades_for_week_aggregates_fills(
    ledger: PostgresLedger,
) -> None:
    await ledger.migrate()
    # Week 19 of 2026 = 2026-05-04 .. 2026-05-10 (Mon..Sun).
    monday = datetime(2026, 5, 4, 9, 30, tzinfo=UTC)
    p = Proposal(
        client_order_id="cw1",
        venue="alpaca",
        symbol="AAPL",
        side="BUY",
        qty=Decimal("100"),
        strategy="swing",
        created_at=monday,
    )
    await ledger.record_proposal(p)
    await ledger.record_fill(
        Fill(
            client_order_id="cw1",
            qty=Decimal("50"),
            price=Decimal("150"),
            side="BUY",
            ts=monday + timedelta(minutes=10),
            fee=Decimal("0.5"),
        )
    )
    await ledger.record_fill(
        Fill(
            client_order_id="cw1",
            qty=Decimal("50"),
            price=Decimal("151"),
            side="BUY",
            ts=monday + timedelta(minutes=20),
            fee=Decimal("0.5"),
        )
    )
    # Add another trade OUTSIDE the window.
    next_week = datetime(2026, 5, 12, tzinfo=UTC)
    await ledger.record_proposal(
        Proposal(
            client_order_id="cw2",
            venue="alpaca",
            symbol="AAPL",
            side="SELL",
            qty=Decimal("100"),
            strategy="swing",
            created_at=next_week,
        )
    )
    rows = await ledger.get_trades_for_week("2026-W19")
    assert len(rows) == 1
    row = rows[0]
    assert row["client_order_id"] == "cw1"
    assert row["qty"] == Decimal("100")
    # Volume-weighted average price.
    assert row["avg_price"] == Decimal("150.5")
    assert row["fees"] == Decimal("1.0")


@pytest.mark.parametrize(
    "week_iso",
    ["2026-W00", "2026-W54", "2026W19", "abc"],
)
async def test_get_trades_for_week_validates_iso(ledger: PostgresLedger, week_iso: str) -> None:
    await ledger.migrate()
    with pytest.raises(ValueError):
        await ledger.get_trades_for_week(week_iso)


# --------------------------------------------------------------------------- json + ping


async def test_apply_schema_sql_does_not_raise(
    ledger: PostgresLedger,
) -> None:
    # The fake's "CREATE TABLE IF NOT EXISTS QUANTA_SCHEMA_VERSION" route
    # accepts the full schema.sql file as a no-op.
    await ledger.apply_schema_sql()


async def test_ping_returns_true(ledger: PostgresLedger) -> None:
    assert await ledger.ping() is True


def test_json_default_encoder_for_decimal_and_datetime() -> None:
    from quanta_core.ledger.postgres import _json_default

    assert _json_default(Decimal("3.14")) == "3.14"
    ts = datetime(2026, 5, 12, tzinfo=UTC)
    assert _json_default(ts) == ts.isoformat()
    with pytest.raises(TypeError):
        _json_default(object())


async def test_proposal_intent_round_trips_decimal_and_datetime(
    ledger: PostgresLedger,
) -> None:
    await ledger.migrate()
    proposal = Proposal(
        client_order_id="json-1",
        venue="alpaca",
        symbol="AAPL",
        side="BUY",
        qty=Decimal("100"),
        strategy="x",
        intent={
            "limit_price": Decimal("150.50"),
            "submitted_at": datetime(2026, 5, 12, tzinfo=UTC),
        },
    )
    await ledger.record_proposal(proposal)
    state = _fake_pg.state()
    assert state["proposals"]["json-1"]["intent"]["limit_price"] == "150.50"


# --------------------------------------------------------------------------- helpers


def _build_proposal(client_order_id: str) -> Proposal:
    return Proposal(
        client_order_id=client_order_id,
        venue="alpaca",
        symbol="AAPL",
        side="BUY",
        qty=Decimal("100"),
        strategy="swing",
        intent={"reason": "breakout"},
        limit_price=Decimal("150.00"),
        created_at=datetime(2026, 5, 12, 14, 0, tzinfo=UTC),
    )


def _build_fill(client_order_id: str) -> Fill:
    return Fill(
        client_order_id=client_order_id,
        qty=Decimal("100"),
        price=Decimal("150.0"),
        side="BUY",
        ts=datetime(2026, 5, 12, 14, 1, tzinfo=UTC),
        fee=Decimal("0.50"),
        fee_currency="USD",
        venue_fill_id="venue-1",
    )
