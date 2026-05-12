"""Tests for ``quanta_core.hermes._ledger``."""

from __future__ import annotations

from datetime import datetime, timezone

from quanta_core.hermes._ledger import LedgerClient, TradeRow, _as_dt, _as_float, _as_str_or_none


def test_ledger_unavailable_returns_empty():
    client = LedgerClient(dsn=None)
    assert client.available is False
    assert list(client.closed_trades_for_day(datetime(2026, 5, 12).date())) == []
    assert list(client.closed_trades_for_range(
        datetime(2026, 5, 11).date(), datetime(2026, 5, 12).date()
    )) == []
    assert list(client.open_positions()) == []
    assert client.ping() is False


def test_row_to_trade_handles_full_row():
    row = {
        "trade_id": "abc",
        "pair": "BTC/USD",
        "side": "long",
        "entry_price": 100.5,
        "exit_price": 110.5,
        "entry_ts": datetime(2026, 5, 12, tzinfo=timezone.utc),
        "exit_ts": datetime(2026, 5, 13, tzinfo=timezone.utc),
        "pnl": 10.0,
        "pnl_pct": 9.9,
        "strategy": "mean_rev",
        "regime": "trending_up",
    }
    t = LedgerClient._row_to_trade(row)
    assert isinstance(t, TradeRow)
    assert t.trade_id == "abc"
    assert t.pair == "BTC/USD"
    assert t.entry_price == 100.5
    assert t.strategy == "mean_rev"


def test_row_to_trade_handles_partial_row():
    t = LedgerClient._row_to_trade({"trade_id": "x", "pair": "ETH/USD", "side": "short"})
    assert t.trade_id == "x"
    assert t.pair == "ETH/USD"
    assert t.entry_price is None
    assert t.strategy is None
    assert t.regime is None


def test_as_float_handles_none():
    assert _as_float(None) is None
    assert _as_float("12.5") == 12.5
    assert _as_float("bad") is None
    assert _as_float(7) == 7.0


def test_as_dt_passthrough():
    now = datetime.now(timezone.utc)
    assert _as_dt(now) is now
    assert _as_dt("not a dt") is None
    assert _as_dt(None) is None


def test_as_str_or_none():
    assert _as_str_or_none(None) is None
    assert _as_str_or_none(42) == "42"
    assert _as_str_or_none("abc") == "abc"
