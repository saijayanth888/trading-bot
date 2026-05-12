"""Backtest module — the parity oracle for V4.

Public surface:

* :class:`quanta_core.backtest.engine.BacktestEngine` — replays historical
  candles through the SAME ``Strategy`` ABC the live engine uses. Backtest
  output is identical to live output for the same candle inputs (the parity
  invariant; see ``tests/backtest/test_live_backtest_parity.py``).
* :class:`quanta_core.backtest.walk_forward.WalkForwardRunner` — rolling
  train/test split for evaluating ML and rule-based strategies on a single
  contiguous history.
* :class:`quanta_core.backtest.result.BacktestResult` — Pydantic v2 model
  carrying trades, fills, proposals, equity curve, and summary metrics.
* :class:`quanta_core.backtest.candle_source.CandleSource` plus the
  ``FeatherCandleSource`` and ``SyntheticCandleSource`` implementations.
"""

from quanta_core.backtest.candle_source import (
    CandleSource,
    FeatherCandleSource,
    SyntheticCandleSource,
)
from quanta_core.backtest.engine import (
    BacktestConfig,
    BacktestEngine,
    FixedBpsSlippageModel,
    NoSlippageModel,
    SlippageModel,
)
from quanta_core.backtest.result import (
    BacktestResult,
    EquityPoint,
    SummaryMetrics,
    TradeRecord,
)
from quanta_core.backtest.walk_forward import (
    WalkForwardFold,
    WalkForwardReport,
    WalkForwardRunner,
)

__all__ = [
    "BacktestConfig",
    "BacktestEngine",
    "BacktestResult",
    "CandleSource",
    "EquityPoint",
    "FeatherCandleSource",
    "FixedBpsSlippageModel",
    "NoSlippageModel",
    "SlippageModel",
    "SummaryMetrics",
    "SyntheticCandleSource",
    "TradeRecord",
    "WalkForwardFold",
    "WalkForwardReport",
    "WalkForwardRunner",
]
