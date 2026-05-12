"""Idempotency store — reserve/commit, duplicate raise, find_existing, TTL."""

from __future__ import annotations

import datetime as dt

import pytest
from sqlalchemy import Engine

from quanta_core.execution.idempotency import (
    TTL_DEFAULT,
    DuplicateClientOrderId,
    IdempotencyRow,
    IdempotencyStore,
)

UTC = dt.UTC
NOW = dt.datetime(2026, 5, 12, 12, 0, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# Reserve
# ---------------------------------------------------------------------------


def test_reserve_new_id_succeeds(idem_store: IdempotencyStore) -> None:
    result = idem_store.reserve("coid-1", {"symbol": "BTC-USD", "qty": "0.1"})
    assert result.client_order_id == "coid-1"
    assert result.kind == "new"


def test_reserve_duplicate_raises(idem_store: IdempotencyStore) -> None:
    idem_store.reserve("coid-1", {"symbol": "BTC-USD"})
    with pytest.raises(DuplicateClientOrderId) as exc_info:
        idem_store.reserve("coid-1", {"symbol": "BTC-USD"})
    assert "coid-1" in str(exc_info.value)


def test_reserve_different_ids_independent(idem_store: IdempotencyStore) -> None:
    idem_store.reserve("coid-1", {"symbol": "BTC-USD"})
    idem_store.reserve("coid-2", {"symbol": "ETH-USD"})
    assert idem_store.count() == 2


# ---------------------------------------------------------------------------
# Commit
# ---------------------------------------------------------------------------


def test_commit_updates_row(idem_store: IdempotencyStore) -> None:
    idem_store.reserve("coid-1", {"symbol": "BTC-USD"})
    idem_store.commit("coid-1", "venue-99", {"filled_qty": "0.1", "avg_price": "65000"})

    row = idem_store.find_existing("coid-1")
    assert row is not None
    assert row.status == "committed"
    assert row.exchange_order_id == "venue-99"
    assert row.fill_json is not None
    assert row.fill_json["filled_qty"] == "0.1"
    assert row.committed_at is not None


def test_commit_without_reserve_raises(idem_store: IdempotencyStore) -> None:
    with pytest.raises(LookupError):
        idem_store.commit("never-reserved", "venue-1", {})


# ---------------------------------------------------------------------------
# Abandon
# ---------------------------------------------------------------------------


def test_abandon_marks_row(idem_store: IdempotencyStore) -> None:
    idem_store.reserve("coid-1", {"symbol": "BTC-USD"})
    idem_store.abandon("coid-1", "slippage_drift")

    row = idem_store.find_existing("coid-1")
    assert row is not None
    assert row.status == "abandoned"
    assert row.fill_json is not None
    assert row.fill_json["abandon_reason"] == "slippage_drift"


def test_abandon_missing_row_is_noop(idem_store: IdempotencyStore) -> None:
    idem_store.abandon("never-reserved", "stale_quote")
    assert idem_store.find_existing("never-reserved") is None


# ---------------------------------------------------------------------------
# find_existing — network-error replay pattern
# ---------------------------------------------------------------------------


def test_find_existing_returns_none_for_unknown(idem_store: IdempotencyStore) -> None:
    assert idem_store.find_existing("unknown") is None


def test_find_existing_returns_reserved_row(idem_store: IdempotencyStore) -> None:
    idem_store.reserve("coid-1", {"symbol": "BTC-USD", "qty": "0.1"})
    row = idem_store.find_existing("coid-1")
    assert row is not None
    assert row.client_order_id == "coid-1"
    assert row.status == "reserved"
    assert row.intent_json["qty"] == "0.1"


def test_find_existing_returns_detached_row(idem_store: IdempotencyStore) -> None:
    """Caller can inspect fields after the session closes."""
    idem_store.reserve("coid-1", {"symbol": "BTC-USD"})
    row = idem_store.find_existing("coid-1")
    assert row is not None
    # Detached: accessing fields must not fail with DetachedInstanceError.
    assert row.intent_json == {"symbol": "BTC-USD"}


# ---------------------------------------------------------------------------
# Cleanup / TTL
# ---------------------------------------------------------------------------


def test_cleanup_removes_old_committed_rows(sqlite_engine: Engine) -> None:
    base = dt.datetime(2026, 5, 1, tzinfo=UTC)
    clock = {"now": base}
    store = IdempotencyStore(sqlite_engine, now_fn=lambda: clock["now"])

    # Reserve + commit at t=0
    store.reserve("old-1", {"x": 1})
    store.commit("old-1", "v-1", {"client_order_id": "old-1"})

    # Advance time past TTL
    clock["now"] = base + dt.timedelta(days=8)
    store.reserve("new-1", {"x": 2})
    store.commit("new-1", "v-2", {"client_order_id": "new-1"})

    # Cleanup at t=8d should drop old-1 only (committed at t=0).
    deleted = store.cleanup(ttl=TTL_DEFAULT)
    assert deleted == 1
    assert store.find_existing("old-1") is None
    assert store.find_existing("new-1") is not None


def test_cleanup_preserves_reserved_rows(sqlite_engine: Engine) -> None:
    """Reserved (uncommitted) rows are NEVER auto-deleted — they represent
    in-flight orders that need operator triage."""
    base = dt.datetime(2026, 5, 1, tzinfo=UTC)
    clock = {"now": base}
    store = IdempotencyStore(sqlite_engine, now_fn=lambda: clock["now"])

    store.reserve("stuck-1", {"x": 1})
    # Note: status="reserved", committed_at IS NULL — cleanup must skip.
    clock["now"] = base + dt.timedelta(days=30)

    deleted = store.cleanup(ttl=TTL_DEFAULT)
    assert deleted == 0
    assert store.find_existing("stuck-1") is not None


def test_cleanup_removes_abandoned_rows(sqlite_engine: Engine) -> None:
    base = dt.datetime(2026, 5, 1, tzinfo=UTC)
    clock = {"now": base}
    store = IdempotencyStore(sqlite_engine, now_fn=lambda: clock["now"])
    store.reserve("aban-1", {"x": 1})
    store.abandon("aban-1", "slippage")
    clock["now"] = base + dt.timedelta(days=8)
    deleted = store.cleanup(ttl=TTL_DEFAULT)
    assert deleted == 1


def test_cleanup_returns_zero_when_nothing_old(idem_store: IdempotencyStore) -> None:
    idem_store.reserve("coid-1", {"x": 1})
    idem_store.commit("coid-1", "v", {"client_order_id": "coid-1"})
    deleted = idem_store.cleanup(ttl=TTL_DEFAULT)
    assert deleted == 0


# ---------------------------------------------------------------------------
# create_all idempotency
# ---------------------------------------------------------------------------


def test_create_all_is_idempotent(sqlite_engine: Engine) -> None:
    store = IdempotencyStore(sqlite_engine)
    store.create_all()
    store.create_all()  # second call must not raise
    store.reserve("coid-1", {"x": 1})


# ---------------------------------------------------------------------------
# count
# ---------------------------------------------------------------------------


def test_count(idem_store: IdempotencyStore) -> None:
    assert idem_store.count() == 0
    idem_store.reserve("a", {})
    idem_store.reserve("b", {})
    assert idem_store.count() == 2


# ---------------------------------------------------------------------------
# Row shape
# ---------------------------------------------------------------------------


def test_row_carries_full_intent(idem_store: IdempotencyStore) -> None:
    intent = {
        "symbol": "BTC-USD",
        "side": "BUY",
        "qty": "0.5",
        "limit_px": "65000",
        "strategy_name": "wheel",
        "intent_ts_ms": 1_715_000_000_000,
    }
    idem_store.reserve("coid-x", intent)
    row = idem_store.find_existing("coid-x")
    assert row is not None
    assert row.intent_json == intent


def test_idempotency_row_table_name() -> None:
    assert IdempotencyRow.__tablename__ == "execution_idempotency"
