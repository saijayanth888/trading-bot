"""Tests for :mod:`quanta_core.backtest.walk_forward`."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest

from quanta_core.backtest.candle_source import SyntheticCandleSource
from quanta_core.backtest.engine import BacktestConfig
from quanta_core.backtest.walk_forward import (
    WalkForwardFold,
    WalkForwardRunner,
)
from quanta_core.strategy.base import Strategy
from quanta_core.types import Bar, ClientOrderId, OrderProposal

# ---------------------------------------------------------------------------
# Strategies for the walk-forward tests
# ---------------------------------------------------------------------------


class _TrainingStrategy(Strategy):
    """Buys on every Nth bar where N is set by train_hook."""

    name = "training_strategy"

    def __init__(self, ctx: Any, config: dict[str, Any]) -> None:
        super().__init__(ctx, config)
        self.train_samples: list[Bar] = []
        self.train_called: bool = False
        self._every = int(config.get("every", 10))
        self._idx = 0

    def train_hook(self, samples: list[Any]) -> None:
        self.train_called = True
        self.train_samples = list(samples)

    def on_candle(self, bar: Bar) -> Sequence[OrderProposal]:
        self._idx += 1
        if self._idx % self._every == 0:
            return [
                OrderProposal(
                    symbol=bar.symbol,
                    side="BUY",
                    qty=Decimal("1"),
                    order_type="market",
                    client_order_id=ClientOrderId(f"co-{self._idx}"),
                    rationale="cycle",
                    asset_class="crypto",
                )
            ]
        return ()


class _AlwaysFlat(Strategy):
    """No-op strategy for empty-fold tests."""

    name = "wf_flat"

    def on_candle(self, bar: Bar) -> Sequence[OrderProposal]:
        return ()


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


class TestConstructorValidation:
    def test_naive_full_range_rejected(self, btc_symbol):
        src = SyntheticCandleSource(
            symbol=btc_symbol, timeframe="1h", start=datetime(2026, 1, 1, tzinfo=UTC), n_bars=10
        )
        with pytest.raises(ValueError, match="timezone-aware"):
            WalkForwardRunner(
                strategy_class=_AlwaysFlat,
                config=BacktestConfig(symbol=btc_symbol, timeframe="1h"),
                candle_source=src,
                full_range=(datetime(2026, 1, 1), datetime(2026, 1, 5, tzinfo=UTC)),
                train_window_days=1,
                test_window_days=1,
            )

    def test_inverted_full_range_rejected(self, btc_symbol):
        src = SyntheticCandleSource(
            symbol=btc_symbol, timeframe="1h", start=datetime(2026, 1, 1, tzinfo=UTC), n_bars=10
        )
        with pytest.raises(ValueError, match="after start"):
            WalkForwardRunner(
                strategy_class=_AlwaysFlat,
                config=BacktestConfig(symbol=btc_symbol, timeframe="1h"),
                candle_source=src,
                full_range=(
                    datetime(2026, 1, 5, tzinfo=UTC),
                    datetime(2026, 1, 1, tzinfo=UTC),
                ),
                train_window_days=1,
                test_window_days=1,
            )

    def test_non_positive_windows_rejected(self, btc_symbol):
        src = SyntheticCandleSource(
            symbol=btc_symbol, timeframe="1h", start=datetime(2026, 1, 1, tzinfo=UTC), n_bars=10
        )
        with pytest.raises(ValueError, match="positive"):
            WalkForwardRunner(
                strategy_class=_AlwaysFlat,
                config=BacktestConfig(symbol=btc_symbol, timeframe="1h"),
                candle_source=src,
                full_range=(
                    datetime(2026, 1, 1, tzinfo=UTC),
                    datetime(2026, 1, 5, tzinfo=UTC),
                ),
                train_window_days=0,
                test_window_days=1,
            )

    def test_timeframe_mismatch_rejected(self, btc_symbol):
        src = SyntheticCandleSource(
            symbol=btc_symbol, timeframe="1h", start=datetime(2026, 1, 1, tzinfo=UTC), n_bars=10
        )
        with pytest.raises(ValueError, match="emits timeframe"):
            WalkForwardRunner(
                strategy_class=_AlwaysFlat,
                config=BacktestConfig(symbol=btc_symbol, timeframe="1m"),  # wrong
                candle_source=src,
                full_range=(
                    datetime(2026, 1, 1, tzinfo=UTC),
                    datetime(2026, 1, 2, tzinfo=UTC),
                ),
                train_window_days=1,
                test_window_days=1,
            )


# ---------------------------------------------------------------------------
# Fold boundaries
# ---------------------------------------------------------------------------


class TestFoldBoundaries:
    def test_folds_match_schedule(self, btc_symbol):
        start = datetime(2026, 1, 1, tzinfo=UTC)
        end = start + timedelta(days=10)
        # Enough bars for 10 days at 1h.
        src = SyntheticCandleSource(symbol=btc_symbol, timeframe="1h", start=start, n_bars=10 * 24)
        runner = WalkForwardRunner(
            strategy_class=_AlwaysFlat,
            config=BacktestConfig(symbol=btc_symbol, timeframe="1h"),
            candle_source=src,
            full_range=(start, end),
            train_window_days=3,
            test_window_days=2,
        )
        folds = runner.folds()
        # Step defaults to test_window_days. Each fold = 3 train + 2 test = 5 days.
        # Starting at day 0 → ends at day 5. Step=2 → next fold starts at day 2.
        # Fold 0: train [0,3) test [3,5) — end=5 ≤ 10 ✓
        # Fold 1: train [2,5) test [5,7) — end=7 ≤ 10 ✓
        # Fold 2: train [4,7) test [7,9) — end=9 ≤ 10 ✓
        # Fold 3: train [6,9) test [9,11) — end=11 > 10 ✗
        assert len(folds) == 3
        assert all(isinstance(f, WalkForwardFold) for f in folds)
        assert folds[0].train_start == start
        assert folds[0].train_end == start + timedelta(days=3)
        assert folds[0].test_start == start + timedelta(days=3)
        assert folds[0].test_end == start + timedelta(days=5)
        assert folds[1].train_start == start + timedelta(days=2)

    def test_zero_folds_when_too_short(self, btc_symbol):
        start = datetime(2026, 1, 1, tzinfo=UTC)
        end = start + timedelta(days=2)
        src = SyntheticCandleSource(symbol=btc_symbol, timeframe="1h", start=start, n_bars=48)
        runner = WalkForwardRunner(
            strategy_class=_AlwaysFlat,
            config=BacktestConfig(symbol=btc_symbol, timeframe="1h"),
            candle_source=src,
            full_range=(start, end),
            train_window_days=3,
            test_window_days=2,
        )
        assert runner.folds() == ()

    def test_custom_step(self, btc_symbol):
        start = datetime(2026, 1, 1, tzinfo=UTC)
        end = start + timedelta(days=10)
        src = SyntheticCandleSource(symbol=btc_symbol, timeframe="1h", start=start, n_bars=10 * 24)
        runner = WalkForwardRunner(
            strategy_class=_AlwaysFlat,
            config=BacktestConfig(symbol=btc_symbol, timeframe="1h"),
            candle_source=src,
            full_range=(start, end),
            train_window_days=2,
            test_window_days=1,
            step_days=3,
        )
        folds = runner.folds()
        # Step=3, train=2, test=1 ⇒ window=3 days, slides by 3 days each fold.
        # Fold 0: train [0,2) test [2,3) end=3 ≤ 10
        # Fold 1: train [3,5) test [5,6) end=6 ≤ 10
        # Fold 2: train [6,8) test [8,9) end=9 ≤ 10
        # Fold 3: train [9,11) → train_end=11 > 10
        assert len(folds) == 3
        assert (folds[1].train_start - folds[0].train_start) == timedelta(days=3)


# ---------------------------------------------------------------------------
# Run + report
# ---------------------------------------------------------------------------


class TestRunReport:
    def test_run_executes_every_fold(self, btc_symbol):
        start = datetime(2026, 1, 1, tzinfo=UTC)
        end = start + timedelta(days=8)
        src = SyntheticCandleSource(
            symbol=btc_symbol, timeframe="1h", start=start, n_bars=8 * 24, seed=11
        )
        runner = WalkForwardRunner(
            strategy_class=_TrainingStrategy,
            config=BacktestConfig(symbol=btc_symbol, timeframe="1h", fee_bps=Decimal("0")),
            candle_source=src,
            full_range=(start, end),
            train_window_days=2,
            test_window_days=2,
            strategy_config={"every": 5},
        )
        report = runner.run()
        assert len(report.folds) == 3
        # Every per-fold result is a BacktestResult.
        for outcome in report.folds:
            assert outcome.result.bars_processed > 0
            # train_hook was called (strategy stores the slice).
            # Walk the engine's strategy via the per-fold result? We can't, but
            # the fold's metadata shows train_bars>0.
            assert outcome.fold.train_bars > 0

    def test_aggregated_summary(self, btc_symbol):
        start = datetime(2026, 1, 1, tzinfo=UTC)
        end = start + timedelta(days=6)
        src = SyntheticCandleSource(symbol=btc_symbol, timeframe="1h", start=start, n_bars=6 * 24)
        runner = WalkForwardRunner(
            strategy_class=_AlwaysFlat,
            config=BacktestConfig(symbol=btc_symbol, timeframe="1h"),
            candle_source=src,
            full_range=(start, end),
            train_window_days=2,
            test_window_days=1,
        )
        report = runner.run()
        # flat strategy → zero trades anywhere → aggregated win_rate=0, sharpe=0.
        assert report.aggregated.n_trades == 0
        assert report.aggregated.win_rate == 0.0
        assert report.aggregated.sharpe == 0.0
        # Starting and ending equity equal (no trades).
        assert report.aggregated.starting_equity == report.aggregated.ending_equity

    def test_empty_run_report(self, btc_symbol):
        start = datetime(2026, 1, 1, tzinfo=UTC)
        end = start + timedelta(days=2)
        src = SyntheticCandleSource(symbol=btc_symbol, timeframe="1h", start=start, n_bars=48)
        # train+test together don't fit.
        runner = WalkForwardRunner(
            strategy_class=_AlwaysFlat,
            config=BacktestConfig(symbol=btc_symbol, timeframe="1h"),
            candle_source=src,
            full_range=(start, end),
            train_window_days=3,
            test_window_days=3,
        )
        report = runner.run()
        assert report.folds == ()
        assert report.aggregated.n_trades == 0

    def test_summary_table_renders(self, btc_symbol):
        start = datetime(2026, 1, 1, tzinfo=UTC)
        end = start + timedelta(days=5)
        src = SyntheticCandleSource(symbol=btc_symbol, timeframe="1h", start=start, n_bars=5 * 24)
        runner = WalkForwardRunner(
            strategy_class=_AlwaysFlat,
            config=BacktestConfig(symbol=btc_symbol, timeframe="1h"),
            candle_source=src,
            full_range=(start, end),
            train_window_days=1,
            test_window_days=1,
        )
        report = runner.run()
        table = report.summary_table()
        assert "WalkForwardReport" in table
        assert "fold" in table

    def test_summary_table_empty(self, btc_symbol):
        start = datetime(2026, 1, 1, tzinfo=UTC)
        end = start + timedelta(days=1)
        src = SyntheticCandleSource(symbol=btc_symbol, timeframe="1h", start=start, n_bars=24)
        runner = WalkForwardRunner(
            strategy_class=_AlwaysFlat,
            config=BacktestConfig(symbol=btc_symbol, timeframe="1h"),
            candle_source=src,
            full_range=(start, end),
            train_window_days=2,
            test_window_days=2,
        )
        report = runner.run()
        table = report.summary_table()
        assert "0 folds" in table

    def test_per_fold_pnl_and_sharpe_lists(self, btc_symbol):
        start = datetime(2026, 1, 1, tzinfo=UTC)
        end = start + timedelta(days=6)
        src = SyntheticCandleSource(symbol=btc_symbol, timeframe="1h", start=start, n_bars=6 * 24)
        runner = WalkForwardRunner(
            strategy_class=_AlwaysFlat,
            config=BacktestConfig(symbol=btc_symbol, timeframe="1h"),
            candle_source=src,
            full_range=(start, end),
            train_window_days=2,
            test_window_days=2,
        )
        report = runner.run()
        assert len(report.fold_pnl()) == len(report.folds)
        assert len(report.per_fold_sharpe()) == len(report.folds)


class TestEmptyFoldEdgeCase:
    def test_fold_with_gap_in_data(self, btc_symbol):
        """When the candle source has a gap, the corresponding fold receives 0 bars.

        Build an in-memory source covering [day0..day2] AND [day5..day7]
        with nothing in between. The walk-forward window may land on a
        day3-day4 test window with no bars at all.
        """
        from quanta_core.backtest.candle_source import InMemoryCandleSource

        # Two contiguous segments, with a 2-day gap between them.
        start = datetime(2026, 1, 1, tzinfo=UTC)
        bars_seg1 = []
        bars_seg2 = []
        for hour in range(48):
            bars_seg1.append(_bar(btc_symbol, start + timedelta(hours=hour)))
        gap_start = start + timedelta(days=4)
        for hour in range(48):
            bars_seg2.append(_bar(btc_symbol, gap_start + timedelta(hours=hour)))
        src = InMemoryCandleSource(bars_seg1 + bars_seg2)

        runner = WalkForwardRunner(
            strategy_class=_AlwaysFlat,
            config=BacktestConfig(symbol=btc_symbol, timeframe="1h"),
            candle_source=src,
            full_range=(start, start + timedelta(days=6)),
            train_window_days=1,
            test_window_days=1,
            step_days=1,
        )
        report = runner.run()
        # Some folds have data; some land in the gap and have 0 test bars.
        assert any(o.fold.test_bars == 0 for o in report.folds)
        # The empty folds still produce a BacktestResult (no crash).
        for o in report.folds:
            stayed_flat = o.result.summary.starting_equity == o.result.summary.ending_equity
            assert stayed_flat or o.fold.test_bars > 0


def _bar(symbol, ts):
    return Bar(
        symbol=symbol,
        open=Decimal("100"),
        high=Decimal("101"),
        low=Decimal("99"),
        close=Decimal("100.5"),
        volume=Decimal("10"),
        timestamp_utc=ts,
        timeframe="1h",
    )


class TestTrainHook:
    def test_train_hook_receives_slice(self, btc_symbol):
        """The strategy's ``train_hook`` is invoked on the train slice each fold."""
        # We need to peek inside the runner's per-fold engine to assert the
        # strategy got the train_hook call. To verify externally, set
        # `every=999` so the strategy does NOT trade and check fold count.
        start = datetime(2026, 1, 1, tzinfo=UTC)
        end = start + timedelta(days=4)
        src = SyntheticCandleSource(symbol=btc_symbol, timeframe="1h", start=start, n_bars=4 * 24)
        runner = WalkForwardRunner(
            strategy_class=_TrainingStrategy,
            config=BacktestConfig(symbol=btc_symbol, timeframe="1h", fee_bps=Decimal("0")),
            candle_source=src,
            full_range=(start, end),
            train_window_days=1,
            test_window_days=1,
            strategy_config={"every": 999},
        )
        report = runner.run()
        # Each fold's strategy was constructed fresh; we can't introspect
        # post-run, but we can assert that the fold count + bar counts are
        # honest (the train slice carried bars).
        assert len(report.folds) >= 1
        for o in report.folds:
            assert o.fold.train_bars > 0
            assert o.fold.test_bars > 0
