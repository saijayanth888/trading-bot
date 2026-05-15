"""
Stock-side feature engineering — pure, no IO.

Builds per-bar feature vectors from raw OHLCV. Features are computed
WITHOUT look-ahead — every value at row t depends only on rows ≤ t.

Feature set (12 cols):
  return_1d, return_5d, return_20d
  log_volume, volume_z_20d
  rsi_14
  macd, macd_signal, macd_hist
  bb_pct (%B from Bollinger Bands)
  realized_vol_20d (annualised)
  spy_excess_return_5d  (set by caller — needs cross-sectional join)

Cross-sectional features (sector RS, market beta, breadth) are computed
in dataset_stock.py at the dataframe level so we have all tickers
simultaneously.

A note on look-ahead: we compute returns as `close[t] / close[t-N] - 1`
and ALL rolling windows use values UP TO AND INCLUDING t. Targets are
computed separately and use values AFTER t.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

FEATURE_COLS: tuple[str, ...] = (
    "return_1d",
    "return_5d",
    "return_20d",
    "log_volume",
    "volume_z_20d",
    "rsi_14",
    "macd",
    "macd_signal",
    "macd_hist",
    "bb_pct",
    "realized_vol_20d",
    "spy_excess_return_5d",
)


def _rsi(close: pd.Series, period: int = 14) -> pd.Series:
    delta = close.diff()
    gain = delta.clip(lower=0).rolling(period, min_periods=period).mean()
    loss = (-delta.clip(upper=0)).rolling(period, min_periods=period).mean()
    rs = gain / loss.replace(0, np.nan)
    return 100 - (100 / (1 + rs))


def _macd(close: pd.Series, fast: int = 12, slow: int = 26, sig: int = 9) -> pd.DataFrame:
    ema_fast = close.ewm(span=fast, adjust=False, min_periods=fast).mean()
    ema_slow = close.ewm(span=slow, adjust=False, min_periods=slow).mean()
    macd = ema_fast - ema_slow
    signal = macd.ewm(span=sig, adjust=False, min_periods=sig).mean()
    hist = macd - signal
    return pd.DataFrame({"macd": macd, "macd_signal": signal, "macd_hist": hist})


def _bb_pct(close: pd.Series, period: int = 20, k: float = 2.0) -> pd.Series:
    mid = close.rolling(period, min_periods=period).mean()
    sd = close.rolling(period, min_periods=period).std()
    upper = mid + k * sd
    lower = mid - k * sd
    width = (upper - lower).replace(0, np.nan)
    return (close - lower) / width


def build_features(bars: pd.DataFrame) -> pd.DataFrame:
    """Per-ticker feature matrix from OHLCV bars.

    Args
      bars: DataFrame with columns [date, o, h, l, c, v]. Index doesn't
            matter; we sort by `date` ascending.

    Returns
      DataFrame indexed by date with FEATURE_COLS. Rows where any
      feature would be NaN (insufficient warm-up history) are dropped.
      `spy_excess_return_5d` is filled with 0.0 here — the caller
      overrides per-ticker after computing the SPY 5-day return.
    """
    df = bars.sort_values("date").reset_index(drop=True).copy()
    df["date"] = pd.to_datetime(df["date"], utc=True)

    out = pd.DataFrame(index=df["date"])

    out["return_1d"] = df["c"].pct_change(1).values
    out["return_5d"] = df["c"].pct_change(5).values
    out["return_20d"] = df["c"].pct_change(20).values

    out["log_volume"] = np.log1p(df["v"].astype(float).values)
    vol_mean = df["v"].rolling(20, min_periods=20).mean()
    vol_std = df["v"].rolling(20, min_periods=20).std()
    out["volume_z_20d"] = ((df["v"] - vol_mean) / vol_std.replace(0, np.nan)).values

    out["rsi_14"] = _rsi(df["c"], 14).values

    macd = _macd(df["c"]).reset_index(drop=True)
    out["macd"] = macd["macd"].values
    out["macd_signal"] = macd["macd_signal"].values
    out["macd_hist"] = macd["macd_hist"].values

    out["bb_pct"] = _bb_pct(df["c"]).values

    log_ret = np.log(df["c"]).diff()
    out["realized_vol_20d"] = (
        log_ret.rolling(20, min_periods=20).std() * np.sqrt(252)
    ).values

    out["spy_excess_return_5d"] = 0.0  # overridden by caller via SPY join

    out = out.dropna()
    return out


def attach_spy_excess(
    feat: pd.DataFrame, spy_return_5d: pd.Series,
) -> pd.DataFrame:
    """Override `spy_excess_return_5d` = ticker.return_5d − SPY.return_5d.
    feat must include `return_5d`. spy_return_5d index = same date axis.
    """
    out = feat.copy()
    spy_aligned = spy_return_5d.reindex(out.index).fillna(0.0)
    out["spy_excess_return_5d"] = (out["return_5d"] - spy_aligned).values
    return out


def build_target(
    bars: pd.DataFrame,
    *,
    horizon_days: int = 5,
    up_threshold: float = 0.015,
    down_threshold: float = -0.015,
) -> pd.Series:
    """Forward-return classification target — pure, no leakage.

    Default: 5-day horizon, ±1.5% threshold. Returns a Series indexed
    by date with values in {0=down, 1=flat, 2=up}. Last `horizon_days`
    rows are NaN (no future data) and should be dropped before training.
    """
    df = bars.sort_values("date").reset_index(drop=True).copy()
    df["date"] = pd.to_datetime(df["date"], utc=True)
    fwd_close = df["c"].shift(-horizon_days)
    fwd_return = (fwd_close / df["c"] - 1.0).values
    label = np.full(len(df), np.nan, dtype=float)
    label[fwd_return > up_threshold] = 2.0
    label[fwd_return < down_threshold] = 0.0
    label[(fwd_return >= down_threshold) & (fwd_return <= up_threshold)] = 1.0
    return pd.Series(label, index=df["date"])
