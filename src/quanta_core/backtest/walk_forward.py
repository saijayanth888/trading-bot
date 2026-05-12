"""Walk-forward driver — rolling train/test split for backtests.

A :class:`WalkForwardRunner` slices a contiguous candle history into a
sequence of ``(train_window, test_window)`` folds, calls
``strategy.train_hook`` on the train slice, and replays the test slice
through a fresh :class:`BacktestEngine` instance. Per-fold metrics are
aggregated into a :class:`WalkForwardReport`.

The strategy class is constructed once per fold so any internal state (TFT
checkpoints, indicator warm-up, debate caches) is honest to a real
re-deployment.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ConfigDict, Field

from quanta_core.backtest.candle_source import (
    CandleSource,
    InMemoryCandleSource,
    timeframe_to_timedelta,
)
from quanta_core.backtest.engine import BacktestConfig, BacktestEngine, SlippageModel
from quanta_core.backtest.result import BacktestResult, SummaryMetrics
from quanta_core.types import Bar, Symbol, Timeframe

if TYPE_CHECKING:  # pragma: no cover — typing only
    from collections.abc import Sequence

    from quanta_core.strategy.base import Strategy


_ZERO = Decimal("0")


# ---------------------------------------------------------------------------
# Fold + report data models
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class WalkForwardFold:
    """One ``(train, test)`` slice within a walk-forward run."""

    index: int
    train_start: datetime
    train_end: datetime
    test_start: datetime
    test_end: datetime
    train_bars: int
    test_bars: int


class FoldOutcome(BaseModel):
    """Per-fold result paired with the fold definition."""

    model_config = ConfigDict(frozen=True, extra="forbid", arbitrary_types_allowed=True)

    fold: WalkForwardFold
    result: BacktestResult


class WalkForwardReport(BaseModel):
    """Aggregate report across every fold in a walk-forward run."""

    model_config = ConfigDict(frozen=True, extra="forbid", arbitrary_types_allowed=True)

    strategy_name: str = Field(min_length=1)
    symbol: Symbol
    timeframe: Timeframe
    folds: tuple[FoldOutcome, ...]
    aggregated: SummaryMetrics

    def fold_pnl(self) -> tuple[Decimal, ...]:
        """Per-fold absolute PnL in chronological order."""
        return tuple(
            f.result.summary.ending_equity - f.result.summary.starting_equity for f in self.folds
        )

    def per_fold_sharpe(self) -> tuple[float, ...]:
        """Per-fold annualised Sharpe ratio."""
        return tuple(f.result.summary.sharpe for f in self.folds)

    def summary_table(self) -> str:
        """Render a human-readable fold-by-fold breakdown."""
        if not self.folds:
            return f"WalkForwardReport :: {self.strategy_name} (0 folds)"
        rows: list[str] = []
        header = "fold | test_window                                | trades | win% | sharpe |  pnl"
        sep = "-" * len(header)
        rows.append(sep)
        rows.append(f"WalkForwardReport :: {self.strategy_name} @ {self.symbol} / {self.timeframe}")
        rows.append(sep)
        rows.append(header)
        rows.append(sep)
        for f in self.folds:
            s = f.result.summary
            pnl = s.ending_equity - s.starting_equity
            window = f"{f.fold.test_start.isoformat()} → {f.fold.test_end.isoformat()}"
            rows.append(
                f"{f.fold.index:4d} | {window} | {s.n_trades:6d} | "
                f"{s.win_rate * 100:4.1f} | {s.sharpe:6.2f} | {pnl}"
            )
        rows.append(sep)
        a = self.aggregated
        rows.append(
            f"AGG  | total: trades={a.n_trades} win%={a.win_rate * 100:.1f} "
            f"sharpe={a.sharpe:.2f} dd={a.max_drawdown_pct:.2%} "
            f"pnl={a.ending_equity - a.starting_equity}"
        )
        rows.append(sep)
        return "\n".join(rows)


# ---------------------------------------------------------------------------
# WalkForwardRunner
# ---------------------------------------------------------------------------


class WalkForwardRunner:
    """Run rolling train/test folds against one Strategy class.

    Parameters
    ----------
    strategy_class
        Subclass of :class:`quanta_core.strategy.base.Strategy`.
    config
        :class:`BacktestConfig` shared by every fold. ``starting_equity`` is
        the equity each fold starts with — this is deliberate (out-of-sample
        evaluation must not reuse in-sample profits).
    candle_source
        Source covering the full window. The runner snapshots it once into
        memory so each fold can slice cheaply.
    full_range
        ``(start, end)`` UTC tuple for the entire walk.
    train_window_days
        Length of each fold's training slice (days).
    test_window_days
        Length of each fold's testing slice (days).
    step_days
        Slide between consecutive train windows (defaults to test_window_days).
    strategy_config
        Optional config dict passed to every per-fold strategy constructor.
    slippage_model
        Optional slippage model applied to every fold.
    """

    def __init__(
        self,
        *,
        strategy_class: type[Strategy],
        config: BacktestConfig,
        candle_source: CandleSource,
        full_range: tuple[datetime, datetime],
        train_window_days: int,
        test_window_days: int,
        step_days: int | None = None,
        strategy_config: dict[str, Any] | None = None,
        slippage_model: SlippageModel | None = None,
    ) -> None:
        """Snapshot config and pre-bucket the candle source into folds."""
        start, end = full_range
        _require_utc("full_range[0]", start)
        _require_utc("full_range[1]", end)
        if end <= start:
            msg = f"full_range end ({end}) must be strictly after start ({start})"
            raise ValueError(msg)
        if train_window_days <= 0 or test_window_days <= 0:
            msg = (
                f"train/test window days must be positive; got "
                f"train={train_window_days} test={test_window_days}"
            )
            raise ValueError(msg)
        self._strategy_class = strategy_class
        self._config = config
        self._candle_source = candle_source
        self._full_range = (start, end)
        self._train_window = timedelta(days=train_window_days)
        self._test_window = timedelta(days=test_window_days)
        self._step = timedelta(days=step_days if step_days is not None else test_window_days)
        self._strategy_config: dict[str, Any] = dict(strategy_config or {})
        self._slippage_model = slippage_model
        # Snapshot all bars once so each fold can slice cheaply.
        self._all_bars: tuple[Bar, ...] = tuple(self._candle_source.slice(start, end))
        # Smoke-check timeframe consistency with config.
        if self._all_bars and self._all_bars[0].timeframe != config.timeframe:
            msg = (
                f"candle source emits timeframe={self._all_bars[0].timeframe} but "
                f"config.timeframe={config.timeframe}"
            )
            raise ValueError(msg)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def folds(self) -> Sequence[WalkForwardFold]:
        """Compute the fold boundary list without running the strategy.

        Useful for dashboards that want to preview the planned schedule
        before kicking off a long replay.
        """
        plan: list[WalkForwardFold] = []
        start, end = self._full_range
        # Walk train_start forward by step until the test window would
        # overrun the full range.
        train_start = start
        idx = 0
        while True:
            train_end = train_start + self._train_window
            test_start = train_end
            test_end = test_start + self._test_window
            if test_end > end:
                break
            train_bars = sum(
                1 for b in self._all_bars if train_start <= b.timestamp_utc < train_end
            )
            test_bars = sum(1 for b in self._all_bars if test_start <= b.timestamp_utc < test_end)
            plan.append(
                WalkForwardFold(
                    index=idx,
                    train_start=train_start,
                    train_end=train_end,
                    test_start=test_start,
                    test_end=test_end,
                    train_bars=train_bars,
                    test_bars=test_bars,
                )
            )
            train_start = train_start + self._step
            idx += 1
        return tuple(plan)

    def run(self) -> WalkForwardReport:
        """Execute every fold and return the aggregated report."""
        outcomes: list[FoldOutcome] = []
        for fold in self.folds():
            outcome = self._run_fold(fold)
            outcomes.append(FoldOutcome(fold=fold, result=outcome))
        agg = self._aggregate(outcomes)
        return WalkForwardReport(
            strategy_name=self._strategy_class.name,
            symbol=self._config.symbol,
            timeframe=self._config.timeframe,
            folds=tuple(outcomes),
            aggregated=agg,
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _run_fold(self, fold: WalkForwardFold) -> BacktestResult:
        """Construct a fresh engine, run train_hook, replay test slice."""
        train_bars = tuple(
            b for b in self._all_bars if fold.train_start <= b.timestamp_utc < fold.train_end
        )
        test_bars = tuple(
            b for b in self._all_bars if fold.test_start <= b.timestamp_utc < fold.test_end
        )

        # Edge case: no test bars — produce an empty BacktestResult so the
        # aggregate doesn't crash. This mirrors the live engine returning
        # without action when no candles arrive.
        if not test_bars:
            return _empty_result(
                strategy_name=self._strategy_class.name,
                symbol=self._config.symbol,
                start=fold.test_start,
                end=fold.test_end,
                starting_equity=self._config.starting_equity,
            )

        test_source = InMemoryCandleSource(test_bars)
        engine = BacktestEngine(
            strategy_class=self._strategy_class,
            config=self._config,
            candle_source=test_source,
            strategy_config=self._strategy_config,
            slippage_model=self._slippage_model,
        )
        # Construct strategy via the engine so the Context binding is in place.
        engine._ensure_strategy()
        # Pass the training slice (Bars) to the strategy's train_hook.
        # Strategies that don't override train_hook get a no-op.
        engine._strategy.train_hook(list(train_bars))  # type: ignore[union-attr]
        return engine.run()

    def _aggregate(self, outcomes: Sequence[FoldOutcome]) -> SummaryMetrics:
        """Roll-up summary metrics across folds.

        The aggregated equity treats fold returns as compounding: the next
        fold's PnL is scaled to a re-based starting_equity. We instead
        report aggregated PnL = sum of per-fold ending - starting (i.e.
        each fold starts flat); the parity invariant doesn't depend on
        compounding semantics, but the operator's UI does want a single
        number. We chose additive PnL because compounding requires deciding
        what to do with drawdown floors — out of scope for wave 2.
        """
        if not outcomes:
            return SummaryMetrics(
                n_trades=0,
                n_proposals=0,
                n_fills=0,
                starting_equity=self._config.starting_equity,
                ending_equity=self._config.starting_equity,
                total_return_pct=_ZERO,
                win_rate=0.0,
                sharpe=0.0,
                max_drawdown_pct=0.0,
                total_fees=_ZERO,
            )
        n_trades = sum(o.result.summary.n_trades for o in outcomes)
        n_proposals = sum(o.result.summary.n_proposals for o in outcomes)
        n_fills = sum(o.result.summary.n_fills for o in outcomes)
        starting = self._config.starting_equity
        total_pnl = sum(
            (o.result.summary.ending_equity - o.result.summary.starting_equity for o in outcomes),
            _ZERO,
        )
        ending = starting + total_pnl
        total_return_pct = ((ending / starting) - Decimal("1")) if starting != _ZERO else _ZERO
        wins = 0
        for o in outcomes:
            wins += sum(1 for t in o.result.trades if t.pnl > 0)
        win_rate = wins / n_trades if n_trades > 0 else 0.0
        # Average per-fold Sharpe — a conservative proxy across regimes;
        # Sharpe of the concatenated return stream is more honest but needs
        # equity-curve stitching that we defer to a future revision.
        n = len(outcomes)
        avg_sharpe = sum(o.result.summary.sharpe for o in outcomes) / n
        max_dd = max((o.result.summary.max_drawdown_pct for o in outcomes), default=0.0)
        total_fees = sum((o.result.summary.total_fees for o in outcomes), _ZERO)
        return SummaryMetrics(
            n_trades=n_trades,
            n_proposals=n_proposals,
            n_fills=n_fills,
            starting_equity=starting,
            ending_equity=ending,
            total_return_pct=total_return_pct,
            win_rate=win_rate,
            sharpe=avg_sharpe,
            max_drawdown_pct=max_dd,
            total_fees=total_fees,
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _require_utc(name: str, v: datetime) -> None:
    """Raise if ``v`` is not timezone-aware."""
    if v.tzinfo is None:
        msg = f"{name} must be timezone-aware (UTC)"
        raise ValueError(msg)


def _empty_result(
    *,
    strategy_name: str,
    symbol: Symbol,
    start: datetime,
    end: datetime,
    starting_equity: Decimal,
) -> BacktestResult:
    """Construct a no-op :class:`BacktestResult` for an empty fold."""
    return BacktestResult(
        strategy_name=strategy_name,
        symbol=symbol,
        start=start.astimezone(UTC),
        end=end.astimezone(UTC),
        bars_processed=0,
        proposals=(),
        fills=(),
        trades=(),
        equity_curve=(),
        summary=SummaryMetrics(
            n_trades=0,
            n_proposals=0,
            n_fills=0,
            starting_equity=starting_equity,
            ending_equity=starting_equity,
            total_return_pct=_ZERO,
            win_rate=0.0,
            sharpe=0.0,
            max_drawdown_pct=0.0,
            total_fees=_ZERO,
        ),
    )


# Re-export so the engine helper names are easy to import from one place.
__all__ = [
    "FoldOutcome",
    "WalkForwardFold",
    "WalkForwardReport",
    "WalkForwardRunner",
    "timeframe_to_timedelta",
]
