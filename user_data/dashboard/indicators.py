"""
Pure-pandas indicators used by the dashboard. Kept self-contained so the
dashboard container doesn't need TA-Lib or talib-binary.
"""

from __future__ import annotations

import numpy as np
import pandas as pd


def rsi(close: pd.Series, period: int = 14) -> pd.Series:
    """Wilder's RSI — same recursive smoothing TA-Lib uses."""
    delta = close.diff()
    gain = delta.clip(lower=0).fillna(0)
    loss = (-delta.clip(upper=0)).fillna(0)
    # Wilder smoothing (RMA). EWMA with alpha = 1/period.
    avg_gain = gain.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    avg_loss = loss.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    out = 100 - (100 / (1 + rs))
    return out.fillna(50)


def bollinger_bands(
    close: pd.Series, period: int = 20, k: float = 2.0,
) -> tuple[pd.Series, pd.Series, pd.Series]:
    """Returns (upper, middle, lower)."""
    mid = close.rolling(period, min_periods=period).mean()
    std = close.rolling(period, min_periods=period).std(ddof=0)
    upper = mid + k * std
    lower = mid - k * std
    return upper, mid, lower


def macd(
    close: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9,
) -> tuple[pd.Series, pd.Series, pd.Series]:
    """Returns (macd_line, signal_line, histogram)."""
    ema_fast = close.ewm(span=fast, adjust=False).mean()
    ema_slow = close.ewm(span=slow, adjust=False).mean()
    macd_line = ema_fast - ema_slow
    signal_line = macd_line.ewm(span=signal, adjust=False).mean()
    hist = macd_line - signal_line
    return macd_line, signal_line, hist


def ema(close: pd.Series, period: int) -> pd.Series:
    """Standard exponential moving average."""
    return close.ewm(span=period, adjust=False, min_periods=period).mean()


def vwap_session(df: pd.DataFrame) -> pd.Series:
    """
    Rolling 24h VWAP (typical price · volume / volume), reset every UTC day.
    Best-effort if `volume` is missing — falls back to NaN-filled series.
    """
    if "volume" not in df.columns or "high" not in df.columns or "low" not in df.columns:
        return pd.Series(index=df.index, dtype=float)
    typical = (df["high"] + df["low"] + df["close"]) / 3.0
    pv = (typical * df["volume"]).cumsum()
    vv = df["volume"].cumsum().replace(0, pd.NA)
    return (pv / vv).astype(float)


def attach_all(df: pd.DataFrame) -> pd.DataFrame:
    """Add RSI / BB / MACD / EMA(20,50) / VWAP columns onto a candles dataframe."""
    if "close" not in df.columns:
        return df
    df = df.copy()
    df["rsi"] = rsi(df["close"])
    upper, mid, lower = bollinger_bands(df["close"])
    df["bb_upper"] = upper
    df["bb_mid"] = mid
    df["bb_lower"] = lower
    macd_line, signal_line, hist = macd(df["close"])
    df["macd"] = macd_line
    df["macd_signal"] = signal_line
    df["macd_hist"] = hist
    df["ema20"] = ema(df["close"], 20)
    df["ema50"] = ema(df["close"], 50)
    df["vwap"] = vwap_session(df)
    return df
