"""Tests for :mod:`quanta_core.backtest.result`."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from quanta_core.backtest.result import (
    BacktestResult,
    EquityPoint,
    SimFill,
    SummaryMetrics,
    TradeRecord,
)
from quanta_core.types import ClientOrderId, OrderProposal, Symbol

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _summary(
    starting: Decimal = Decimal("10000"),
    ending: Decimal = Decimal("10500"),
) -> SummaryMetrics:
    return SummaryMetrics(
        n_trades=2,
        n_proposals=3,
        n_fills=4,
        starting_equity=starting,
        ending_equity=ending,
        total_return_pct=Decimal("0.05"),
        win_rate=0.5,
        sharpe=1.5,
        max_drawdown_pct=0.02,
        total_fees=Decimal("1.5"),
    )


def _make_result(*, symbol: Symbol) -> BacktestResult:
    start = datetime(2026, 1, 1, tzinfo=UTC)
    return BacktestResult(
        strategy_name="demo",
        symbol=symbol,
        start=start,
        end=datetime(2026, 1, 2, tzinfo=UTC),
        bars_processed=10,
        proposals=(
            OrderProposal(
                symbol=symbol,
                side="BUY",
                qty=Decimal("1"),
                order_type="market",
                client_order_id=ClientOrderId("co-1"),
                rationale="demo",
                asset_class="crypto",
            ),
        ),
        fills=(
            SimFill(
                symbol=symbol,
                side="BUY",
                qty=Decimal("1"),
                price=Decimal("100"),
                fee=Decimal("0.1"),
                timestamp_utc=start,
                client_order_id="co-1",
            ),
        ),
        trades=(
            TradeRecord(
                symbol=symbol,
                side="BUY",
                entry_price=Decimal("100"),
                exit_price=Decimal("105"),
                qty=Decimal("1"),
                entry_ts=start,
                exit_ts=datetime(2026, 1, 1, 12, tzinfo=UTC),
                pnl=Decimal("4.9"),
                fee_total=Decimal("0.2"),
                bars_held=6,
            ),
        ),
        equity_curve=(
            EquityPoint(
                timestamp_utc=start,
                equity=Decimal("10000"),
                cash=Decimal("9900"),
                holdings_value=Decimal("100"),
            ),
        ),
        summary=_summary(),
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestEquityPoint:
    def test_naive_ts_rejected(self):
        with pytest.raises(ValueError, match="timezone-aware"):
            EquityPoint(
                timestamp_utc=datetime(2026, 1, 1),  # naive
                equity=Decimal("100"),
                cash=Decimal("50"),
                holdings_value=Decimal("50"),
            )

    def test_frozen(self):
        pt = EquityPoint(
            timestamp_utc=datetime(2026, 1, 1, tzinfo=UTC),
            equity=Decimal("100"),
            cash=Decimal("50"),
            holdings_value=Decimal("50"),
        )
        with pytest.raises(ValueError):
            pt.equity = Decimal("200")  # type: ignore[misc]


class TestSimFill:
    def test_naive_ts_rejected(self, btc_symbol):
        with pytest.raises(ValueError, match="timezone-aware"):
            SimFill(
                symbol=btc_symbol,
                side="BUY",
                qty=Decimal("1"),
                price=Decimal("100"),
                fee=Decimal("0"),
                timestamp_utc=datetime(2026, 1, 1),
                client_order_id="co-1",
            )

    def test_negative_qty_rejected(self, btc_symbol):
        with pytest.raises(ValueError):
            SimFill(
                symbol=btc_symbol,
                side="BUY",
                qty=Decimal("0"),
                price=Decimal("100"),
                fee=Decimal("0"),
                timestamp_utc=datetime(2026, 1, 1, tzinfo=UTC),
                client_order_id="co-1",
            )


class TestTradeRecord:
    def test_naive_ts_rejected(self, btc_symbol):
        with pytest.raises(ValueError, match="timezone-aware"):
            TradeRecord(
                symbol=btc_symbol,
                side="BUY",
                entry_price=Decimal("100"),
                exit_price=Decimal("110"),
                qty=Decimal("1"),
                entry_ts=datetime(2026, 1, 1),  # naive
                exit_ts=datetime(2026, 1, 1, 12, tzinfo=UTC),
                pnl=Decimal("10"),
                fee_total=Decimal("0"),
                bars_held=3,
            )


class TestSummaryMetrics:
    def test_win_rate_bounds(self):
        with pytest.raises(ValueError):
            SummaryMetrics(
                n_trades=0,
                n_proposals=0,
                n_fills=0,
                starting_equity=Decimal("100"),
                ending_equity=Decimal("100"),
                total_return_pct=Decimal("0"),
                win_rate=1.5,  # out of bounds
                sharpe=0.0,
                max_drawdown_pct=0.0,
                total_fees=Decimal("0"),
            )


class TestBacktestResult:
    def test_summary_table_renders(self, btc_symbol):
        r = _make_result(symbol=btc_symbol)
        table = r.summary_table()
        assert "BacktestResult" in table
        assert "demo" in table
        assert "BTC/USD" in table
        assert "win_rate" in table

    def test_to_jsonl_roundtrip(self, tmp_path: Path, btc_symbol):
        r = _make_result(symbol=btc_symbol)
        path = tmp_path / "results.jsonl"
        r.to_jsonl(path)
        r.to_jsonl(path)  # append a second line
        loaded = BacktestResult.from_jsonl(path)
        assert len(loaded) == 2
        assert loaded[0].strategy_name == "demo"
        assert loaded[0].trades[0].pnl == Decimal("4.9")
        # Idempotent: dump → load → dump produces same shape
        again = tmp_path / "again.jsonl"
        loaded[0].to_jsonl(again)
        round_tripped = BacktestResult.from_jsonl(again)
        assert round_tripped[0] == loaded[0]

    def test_from_jsonl_skips_blank_lines(self, tmp_path: Path, btc_symbol):
        r = _make_result(symbol=btc_symbol)
        path = tmp_path / "results.jsonl"
        r.to_jsonl(path)
        path.write_text(path.read_text() + "\n\n")
        loaded = BacktestResult.from_jsonl(path)
        assert len(loaded) == 1

    def test_to_jsonl_creates_parent_dir(self, tmp_path: Path, btc_symbol):
        r = _make_result(symbol=btc_symbol)
        nested = tmp_path / "nested" / "dir" / "results.jsonl"
        r.to_jsonl(nested)
        assert nested.is_file()

    def test_naive_start_rejected(self, btc_symbol):
        r = _make_result(symbol=btc_symbol)
        with pytest.raises(ValueError, match="timezone-aware"):
            BacktestResult(
                strategy_name=r.strategy_name,
                symbol=r.symbol,
                start=datetime(2026, 1, 1),  # naive
                end=r.end,
                bars_processed=r.bars_processed,
                proposals=r.proposals,
                fills=r.fills,
                trades=r.trades,
                equity_curve=r.equity_curve,
                summary=r.summary,
            )

    def test_json_default_rejects_unknown(self):
        from quanta_core.backtest.result import _json_default

        class Weird:
            """Unknown type."""

        with pytest.raises(TypeError, match="not JSON serializable"):
            _json_default(Weird())

    def test_json_default_serialises_decimal_and_datetime(self):
        from quanta_core.backtest.result import _json_default

        assert _json_default(Decimal("1.5")) == "1.5"
        ts = datetime(2026, 5, 12, tzinfo=UTC)
        assert _json_default(ts) == "2026-05-12T00:00:00+00:00"
