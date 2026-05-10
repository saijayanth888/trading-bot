"""
Stocks ML pipeline — TFT predictor + DRL ensemble + EPT evolution.

⚠️  ALPHA — built 2026-05-10, paper-pilot training tonight. Not validated
on live capital. Predictions FROM these models log a `[STOCKS_ML_ALPHA]`
warning and are gated by STOCKS_ML_ENABLED env var (default: 0 = compute
only, do NOT influence trade decisions).

Architecture mirrors crypto's stack but adapted for daily-cadence
equities:
  features_stock.py    cross-sectional feature engineering
  dataset_stock.py     walk-forward Dataset (no look-ahead leaks)
  tft_stock.py         TFT model + train + inference
  drl_ensemble_stocks.py PPO + A2C + DQN voting on TFT-augmented obs
  ept_evolution_stocks.py weekly generation tracking
  cli.py               python -m shark.ml.cli {train_tft, train_drl, infer, backtest}

Operator: enable ML influence on trades only after one full backtest +
one week of paper-trading data attribution (see docs/ML_VALIDATION.md).
"""

__version__ = "0.1.0-alpha"
