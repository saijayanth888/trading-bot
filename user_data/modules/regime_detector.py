"""
Regime detector — Gaussian HMM over BTC/USD 1h features.

Fits a 4-state Gaussian HMM on a rolling 90-day window of BTC 1h candles
and maps the latent states to four labels:
    - trending_up
    - trending_down
    - mean_reverting
    - high_volatility

Features (all derived from BTC/USD on Coinbase public OHLCV):
    - log_return       : log(close / close.shift(1))
    - realized_vol_30d : annualised rolling std of log returns over 30d
    - volume_ratio     : volume / SMA20(volume)
    - rsi_14           : 14-period Wilder RSI
    - funding_rate     : Binance perp BTCUSDT 8h funding (optional, ffilled)

A daemon thread refits the HMM every 24h and re-predicts every 5 minutes,
appending one row per prediction to `regime_log` in the on-chain SQLite DB.
The fitted model is JSON-serialised to ``user_data/data/regime_hmm.json`` so
container restarts are warm.

Public API:
    get_regime_features(pair="BTC/USD") -> pd.DataFrame  # FreqAI-friendly
"""

from __future__ import annotations

import json
import logging
import math
import threading
import time
from datetime import datetime, timedelta, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import requests

from . import db

# ---------------------------------------------------------------------------
# Paths & constants
# ---------------------------------------------------------------------------

_HERE = Path(__file__).resolve()
_USER_DATA = _HERE.parent.parent
LOG_PATH = _USER_DATA / "logs" / "regime.log"
MODEL_PATH = _USER_DATA / "data" / "regime_hmm.json"
LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
MODEL_PATH.parent.mkdir(parents=True, exist_ok=True)

REFIT_INTERVAL_S = 24 * 3600                     # 24h
PREDICT_INTERVAL_S = 5 * 60                      # 5 min
TRAIN_WINDOW_DAYS = 90
FEATURE_HISTORY_DAYS = 30                        # rolling-vol lookback

N_REGIMES = 4
REGIME_LABELS: tuple[str, ...] = (
    "trending_up", "trending_down", "mean_reverting", "high_volatility",
)

COINBASE_BROKERAGE_BASE = "https://api.coinbase.com"
COINBASE_EXCHANGE_BASE = "https://api.exchange.coinbase.com"
KRAKEN_BASE = "https://api.kraken.com"
BINANCE_FUTURES_BASE = "https://fapi.binance.com"
HTTP_TIMEOUT = 20

# ---------------------------------------------------------------------------
# Logger
# ---------------------------------------------------------------------------

logger = logging.getLogger("regime")
if not logger.handlers:
    h = RotatingFileHandler(
        LOG_PATH, maxBytes=2 * 1024 * 1024, backupCount=3, encoding="utf-8",
    )
    h.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
    logger.addHandler(h)
    logger.setLevel(logging.INFO)
    logger.propagate = False

# ---------------------------------------------------------------------------
# Database — schema lives in user_data/data/schema.sql
# ---------------------------------------------------------------------------


def _ts_to_dt(ts: int | float) -> datetime:
    return datetime.fromtimestamp(int(ts), tz=timezone.utc)

# ---------------------------------------------------------------------------
# HTTP with retry
# ---------------------------------------------------------------------------


def _http_get(url: str, params: dict | None = None, max_retries: int = 4
              ) -> requests.Response | None:
    delay = 1.0
    for attempt in range(1, max_retries + 1):
        try:
            r = requests.get(
                url, params=params, timeout=HTTP_TIMEOUT,
                headers={"User-Agent": "freqtrade-regime/0.1"},
            )
        except requests.RequestException as exc:
            logger.warning("[%s] err (try %d): %s", url, attempt, exc)
            time.sleep(delay)
            delay = min(delay * 2, 30.0)
            continue
        if r.status_code == 429 or 500 <= r.status_code < 600:
            retry_after = float(r.headers.get("Retry-After", delay))
            logger.warning("[%s] HTTP %d (try %d) backoff %.1fs",
                           url, r.status_code, attempt, retry_after)
            time.sleep(retry_after)
            delay = min(delay * 2, 30.0)
            continue
        return r
    return None


# ---------------------------------------------------------------------------
# Coinbase public OHLCV (paginated, max 300 candles per call)
# ---------------------------------------------------------------------------


_CANDLE_COLS = ["time", "low", "high", "open", "close", "volume"]


def _fetch_btc_1h_coinbase_brokerage(days: int) -> pd.DataFrame:
    """Coinbase Brokerage public candles. Up to 350 candles per call."""
    end_ts = int(time.time())
    start_ts = end_ts - days * 86400
    rows: list[list[float]] = []
    cur_end = end_ts
    chunk_seconds = 350 * 3600
    url = (f"{COINBASE_BROKERAGE_BASE}"
           f"/api/v3/brokerage/market/products/BTC-USD/candles")

    while cur_end > start_ts:
        cur_start = max(start_ts, cur_end - chunk_seconds)
        params = {
            "granularity": "ONE_HOUR",
            "start": str(cur_start),
            "end": str(cur_end),
        }
        r = _http_get(url, params=params)
        if r is None or not r.ok:
            return pd.DataFrame()
        try:
            payload = r.json()
        except json.JSONDecodeError:
            return pd.DataFrame()
        chunk = payload.get("candles") or []
        if not chunk:
            break
        for c in chunk:
            rows.append([
                int(c["start"]),
                float(c["low"]),
                float(c["high"]),
                float(c["open"]),
                float(c["close"]),
                float(c["volume"]),
            ])
        oldest = min(int(c["start"]) for c in chunk)
        if oldest <= start_ts:
            break
        cur_end = oldest - 3600
        time.sleep(0.25)

    if not rows:
        return pd.DataFrame()
    return (
        pd.DataFrame(rows, columns=_CANDLE_COLS)
        .drop_duplicates(subset="time")
        .sort_values("time")
        .reset_index(drop=True)
    )


def _fetch_btc_1h_coinbase_exchange(days: int) -> pd.DataFrame:
    granularity = 3600
    end_ts = int(time.time())
    start_ts = end_ts - days * 86400
    rows: list[list[float]] = []
    cur_end = end_ts
    while cur_end > start_ts:
        cur_start = max(start_ts, cur_end - 300 * granularity)
        params = {
            "granularity": granularity,
            "start": datetime.fromtimestamp(cur_start, tz=timezone.utc).isoformat(),
            "end":   datetime.fromtimestamp(cur_end,   tz=timezone.utc).isoformat(),
        }
        r = _http_get(
            f"{COINBASE_EXCHANGE_BASE}/products/BTC-USD/candles", params=params,
        )
        if r is None or not r.ok:
            return pd.DataFrame()
        try:
            chunk = r.json()
        except json.JSONDecodeError:
            return pd.DataFrame()
        if not chunk:
            break
        rows.extend(chunk)
        oldest = min(c[0] for c in chunk)
        if oldest <= start_ts:
            break
        cur_end = oldest - granularity
        time.sleep(0.3)
    if not rows:
        return pd.DataFrame()
    return (
        pd.DataFrame(rows, columns=_CANDLE_COLS)
        .drop_duplicates(subset="time")
        .sort_values("time")
        .reset_index(drop=True)
    )


def _fetch_btc_1h_kraken(days: int) -> pd.DataFrame:
    """
    Kraken public OHLC. The endpoint always returns the last 720 hours
    regardless of `since`; we walk it back when more history is needed.
    """
    rows: list[list[float]] = []
    seen_ts: set[int] = set()
    end_ts = int(time.time())
    start_ts = end_ts - days * 86400
    since = max(0, start_ts - 3600)
    url = f"{KRAKEN_BASE}/0/public/OHLC"

    for _ in range(20):                         # safety cap on pagination loops
        params = {"pair": "XBTUSD", "interval": 60, "since": since}
        r = _http_get(url, params=params)
        if r is None or not r.ok:
            break
        try:
            payload = r.json()
        except json.JSONDecodeError:
            break
        if payload.get("error"):
            logger.warning("kraken error: %s", payload["error"])
            break
        result = payload.get("result") or {}
        # Kraken returns the BTC pair under various keys (XXBTZUSD / XBTUSD)
        candle_key = next((k for k in result if k != "last"), None)
        if not candle_key:
            break
        chunk = result[candle_key] or []
        if not chunk:
            break
        added = 0
        for c in chunk:
            t = int(c[0])
            if t in seen_ts or t > end_ts:
                continue
            seen_ts.add(t)
            rows.append([
                t,
                float(c[3]),               # low
                float(c[2]),               # high
                float(c[1]),               # open
                float(c[4]),               # close
                float(c[6]),               # volume (5 = vwap)
            ])
            added += 1
        if added == 0:
            break
        last_cursor = int(result.get("last") or chunk[-1][0])
        if last_cursor <= since or last_cursor >= end_ts:
            break
        since = last_cursor
        time.sleep(0.4)

    if not rows:
        return pd.DataFrame()
    df = (
        pd.DataFrame(rows, columns=_CANDLE_COLS)
        .drop_duplicates(subset="time")
        .sort_values("time")
        .reset_index(drop=True)
    )
    return df[df["time"] >= start_ts]


def fetch_btc_1h_candles(
    days: int = TRAIN_WINDOW_DAYS + FEATURE_HISTORY_DAYS + 5,
) -> pd.DataFrame:
    """Try Coinbase Brokerage → Coinbase Exchange → Kraken. Returns first hit."""
    for name, fn in (
        ("coinbase-brokerage", _fetch_btc_1h_coinbase_brokerage),
        ("coinbase-exchange", _fetch_btc_1h_coinbase_exchange),
        ("kraken", _fetch_btc_1h_kraken),
    ):
        try:
            df = fn(days)
        except Exception:
            logger.exception("%s candle fetch crashed", name)
            continue
        if not df.empty:
            logger.info("BTC 1h candles (%s): %d rows", name, len(df))
            return df
        logger.warning("%s returned no data, trying next source", name)

    logger.error("all candle sources failed")
    return pd.DataFrame(columns=_CANDLE_COLS)


def fetch_funding_rate(symbol: str = "BTCUSDT", days: int = 120
                        ) -> pd.DataFrame | None:
    try:
        end_ms = int(time.time() * 1000)
        start_ms = end_ms - days * 86400 * 1000
        rows: list[dict[str, Any]] = []
        cur_end = end_ms
        while cur_end > start_ms:
            params = {"symbol": symbol, "limit": 1000, "endTime": cur_end}
            r = _http_get(
                f"{BINANCE_FUTURES_BASE}/fapi/v1/fundingRate", params=params,
            )
            if r is None or not r.ok:
                return None
            chunk = r.json()
            if not chunk:
                break
            rows.extend(chunk)
            oldest = min(int(c["fundingTime"]) for c in chunk)
            if oldest <= start_ms:
                break
            cur_end = oldest - 1
            time.sleep(0.2)
        if not rows:
            return None
        df = pd.DataFrame(rows)
        df["fundingTime"] = pd.to_datetime(df["fundingTime"], unit="ms", utc=True)
        df["fundingRate"] = df["fundingRate"].astype(float)
        df = (
            df[["fundingTime", "fundingRate"]]
            .drop_duplicates()
            .sort_values("fundingTime")
        )
        logger.info("funding rate: %d rows", len(df))
        return df
    except Exception as exc:
        logger.warning("funding rate fetch failed: %s", exc)
        return None


# ---------------------------------------------------------------------------
# Feature engineering
# ---------------------------------------------------------------------------


def _rsi(close: pd.Series, period: int = 14) -> pd.Series:
    delta = close.diff()
    gain = delta.clip(lower=0.0)
    loss = -delta.clip(upper=0.0)
    avg_gain = gain.ewm(alpha=1.0 / period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1.0 / period, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0.0, np.nan)
    return 100.0 - 100.0 / (1.0 + rs)


def build_features(
    candles: pd.DataFrame,
    funding: pd.DataFrame | None = None,
) -> pd.DataFrame:
    if candles.empty:
        return pd.DataFrame()
    df = candles.copy()
    df["date"] = pd.to_datetime(df["time"], unit="s", utc=True)
    df = df.set_index("date").sort_index()
    df["close"] = df["close"].astype(float)
    df["volume"] = df["volume"].astype(float)

    df["log_return"] = np.log(df["close"] / df["close"].shift(1))
    annualisation = math.sqrt(24 * 365)
    df["realized_vol_30d"] = (
        df["log_return"].rolling(30 * 24, min_periods=24 * 7).std() * annualisation
    )
    df["volume_ratio"] = df["volume"] / df["volume"].rolling(20).mean()
    df["rsi_14"] = _rsi(df["close"], 14)

    feats = ["log_return", "realized_vol_30d", "volume_ratio", "rsi_14"]

    if funding is not None and not funding.empty:
        f = funding.set_index("fundingTime").sort_index()["fundingRate"]
        df["funding_rate"] = f.reindex(df.index, method="ffill")
        if df["funding_rate"].notna().sum() > 100:
            feats.append("funding_rate")
        else:
            df = df.drop(columns=["funding_rate"])

    out = df[feats].dropna()
    return out


# ---------------------------------------------------------------------------
# HMM fit + state→label mapping
# ---------------------------------------------------------------------------


def fit_hmm(features: pd.DataFrame, n_states: int = N_REGIMES, seed: int = 42):
    """
    Fit a Gaussian HMM with diagonal covariance. Returns (model, state_to_label).
    The model carries `feature_mean_`, `feature_std_`, `feature_names_` so
    `predict_regime` can normalise consistently.
    """
    from hmmlearn.hmm import GaussianHMM

    X = features.values.astype(np.float64)
    if len(X) < 100:
        raise ValueError(f"insufficient samples for HMM fit: {len(X)}")

    mean = X.mean(axis=0)
    std = X.std(axis=0)
    std = np.where(std == 0.0, 1.0, std)
    Xn = (X - mean) / std

    model = GaussianHMM(
        n_components=n_states,
        covariance_type="diag",
        n_iter=200,
        tol=1e-3,
        random_state=seed,
        init_params="stmc",
    )
    model.fit(Xn)
    model.feature_mean_ = mean
    model.feature_std_ = std
    model.feature_names_ = list(features.columns)
    state_to_label = _label_states(model)
    return model, state_to_label


def _label_states(model) -> dict[int, str]:
    """
    Heuristic mapping HMM states → regime labels:
      - state with highest realised_vol  → high_volatility
      - of remaining: highest log_return → trending_up
                      lowest  log_return → trending_down
                      rest               → mean_reverting
    """
    fnames = list(model.feature_names_)
    means_n = model.means_                       # standardised
    means = means_n * model.feature_std_ + model.feature_mean_
    df = pd.DataFrame(means, columns=fnames)
    if "realized_vol_30d" not in df or "log_return" not in df:
        raise RuntimeError("required feature names missing from model")

    available = list(df.index)
    mapping: dict[int, str] = {}

    s_hv = int(df.loc[available, "realized_vol_30d"].idxmax())
    mapping[s_hv] = "high_volatility"
    available.remove(s_hv)

    if len(available) >= 2:
        ranked = df.loc[available, "log_return"].sort_values()
        s_dn = int(ranked.index[0])
        s_up = int(ranked.index[-1])
        mapping[s_dn] = "trending_down"
        mapping[s_up] = "trending_up"
        available = [s for s in available if s not in (s_up, s_dn)]
    elif len(available) == 1:
        mapping[int(available[0])] = "mean_reverting"
        available = []

    for s in available:
        mapping[int(s)] = "mean_reverting"

    return mapping


def predict_regime(model, features: pd.DataFrame, state_to_label: dict[int, str]
                   ) -> pd.DataFrame:
    X = features.values.astype(np.float64)
    Xn = (X - model.feature_mean_) / model.feature_std_
    states = model.predict(Xn)
    probs = model.predict_proba(Xn)
    out = pd.DataFrame(
        {"state": states,
         "regime": [state_to_label[int(s)] for s in states]},
        index=features.index,
    )
    for i in range(model.n_components):
        out[f"prob_state_{i}"] = probs[:, i]
    out["regime_probability"] = probs[np.arange(len(states)), states]
    return out


# ---------------------------------------------------------------------------
# JSON model persistence (intentionally avoids pickle)
# ---------------------------------------------------------------------------


def _serialise_model(model, state_to_label: dict[int, str], fitted_at: int) -> dict:
    return {
        "fitted_at": fitted_at,
        "n_components": int(model.n_components),
        "covariance_type": model.covariance_type,
        "feature_names": list(model.feature_names_),
        "feature_mean": model.feature_mean_.tolist(),
        "feature_std": model.feature_std_.tolist(),
        "startprob": model.startprob_.tolist(),
        "transmat": model.transmat_.tolist(),
        "means": model.means_.tolist(),
        "covars": model.covars_.tolist(),
        "state_to_label": {str(k): v for k, v in state_to_label.items()},
    }


def _deserialise_model(blob: dict):
    """Rebuild a `GaussianHMM` from JSON-only fields. No code execution."""
    from hmmlearn.hmm import GaussianHMM

    cov_type = blob["covariance_type"]
    model = GaussianHMM(
        n_components=int(blob["n_components"]),
        covariance_type=cov_type,
        init_params="",
        params="stmc",
    )
    model.startprob_ = np.asarray(blob["startprob"], dtype=np.float64)
    model.transmat_ = np.asarray(blob["transmat"], dtype=np.float64)
    model.means_ = np.asarray(blob["means"], dtype=np.float64)
    covars_arr = np.asarray(blob["covars"], dtype=np.float64)
    if cov_type == "diag":
        # hmmlearn quirk: `model.covars_` (property) returns 3D
        # full-matrix diag form (n_components × n_features × n_features),
        # but the internal `_covars_` storage that predict() consumes
        # MUST be the 2D diag-only form (n_components × n_features). If
        # we feed back the 3D form `.covars_.tolist()` produced at
        # serialise time, predict() crashes with a broadcast-shape error
        # — silently, because the regime-detector thread catches all
        # exceptions. So extract the diagonal here on load.
        if covars_arr.ndim == 3:
            covars_arr = np.array([
                np.diag(covars_arr[i]) for i in range(covars_arr.shape[0])
            ], dtype=np.float64)
        model._covars_ = covars_arr
    else:
        model._covars_ = covars_arr
    model.feature_mean_ = np.asarray(blob["feature_mean"], dtype=np.float64)
    model.feature_std_ = np.asarray(blob["feature_std"], dtype=np.float64)
    model.feature_names_ = list(blob["feature_names"])
    state_to_label = {int(k): v for k, v in blob["state_to_label"].items()}
    return model, state_to_label, int(blob.get("fitted_at") or 0)


# ---------------------------------------------------------------------------
# Background detector
# ---------------------------------------------------------------------------


class RegimeDetector:
    _instance: "RegimeDetector | None" = None
    _instance_lock = threading.Lock()

    def __init__(self) -> None:
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._model = None
        self._state_to_label: dict[int, str] | None = None
        self._fitted_at: int = 0
        self._inner_lock = threading.RLock()
        self.last_predict_ts: float = 0.0
        self._load_persisted()

    @classmethod
    def instance(cls) -> "RegimeDetector":
        with cls._instance_lock:
            if cls._instance is None:
                cls._instance = cls()
            return cls._instance

    # ---------- persistence ----------

    def _load_persisted(self) -> None:
        if not MODEL_PATH.exists():
            return
        try:
            with open(MODEL_PATH, "r", encoding="utf-8") as f:
                blob = json.load(f)
            model, mapping, fitted_at = _deserialise_model(blob)
            self._model = model
            self._state_to_label = mapping
            self._fitted_at = fitted_at
            logger.info("loaded persisted model (fitted_at=%d)", self._fitted_at)
        except Exception as exc:
            logger.warning("failed to load persisted model: %s", exc)

    def _persist(self) -> None:
        tmp = MODEL_PATH.with_suffix(".json.tmp")
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(
                    _serialise_model(
                        self._model, self._state_to_label, self._fitted_at,
                    ),
                    f,
                )
            tmp.replace(MODEL_PATH)
        except Exception:
            logger.exception("failed to persist model")

    # ---------- thread control ----------

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, name="regime-detector", daemon=True,
        )
        self._thread.start()
        logger.info(
            "regime detector started (refit=%dh, predict=%dmin)",
            REFIT_INTERVAL_S // 3600, PREDICT_INTERVAL_S // 60,
        )

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=15)

    def _run(self) -> None:
        try:
            if self._model is None or (time.time() - self._fitted_at) > REFIT_INTERVAL_S:
                self.refit()
        except Exception:
            logger.exception("initial refit failed")

        while not self._stop.is_set():
            try:
                if (time.time() - self._fitted_at) > REFIT_INTERVAL_S:
                    self.refit()
                self.predict_now()
            except Exception:
                logger.exception("predict cycle crashed")
            for _ in range(PREDICT_INTERVAL_S):
                if self._stop.is_set():
                    return
                time.sleep(1)

    # ---------- core operations ----------

    def refit(self) -> None:
        candles = fetch_btc_1h_candles()
        if candles.empty:
            logger.warning("refit skipped — no candle data")
            return
        funding = fetch_funding_rate()
        feats = build_features(candles, funding)
        if feats.empty:
            logger.warning("refit skipped — empty features")
            return

        cutoff = pd.Timestamp.now(tz="UTC") - pd.Timedelta(days=TRAIN_WINDOW_DAYS)
        train = feats[feats.index >= cutoff]
        if len(train) < 200:
            logger.warning("refit skipped — only %d training samples", len(train))
            return

        try:
            model, mapping = fit_hmm(train)
        except Exception:
            logger.exception("HMM fit failed")
            return

        Xn = (train.values - model.feature_mean_) / model.feature_std_
        try:
            ll = float(model.score(Xn))
        except Exception:
            ll = float("nan")

        with self._inner_lock:
            self._model = model
            self._state_to_label = mapping
            self._fitted_at = int(time.time())
            self._persist()

        try:
            db.execute_one(
                "INSERT INTO regime_model_meta "
                "(fitted_at, n_samples, log_likelihood, state_to_label, feature_names) "
                "VALUES (%s, %s, %s, %s::jsonb, %s::jsonb)",
                (
                    _ts_to_dt(self._fitted_at), len(train), ll,
                    json.dumps({str(k): v for k, v in mapping.items()}),
                    json.dumps(list(train.columns)),
                ),
            )
        except Exception as exc:
            logger.warning("regime_model_meta write failed: %s", exc)

        preds = predict_regime(model, train, mapping)
        self._persist_predictions_bulk(preds, model, mapping)

        logger.info(
            "refit done: n=%d ll=%.2f mapping=%s features=%s",
            len(train), ll, mapping, list(train.columns),
        )

    def predict_now(self) -> dict | None:
        with self._inner_lock:
            if self._model is None or self._state_to_label is None:
                return None
            model = self._model
            mapping = dict(self._state_to_label)

        candles = fetch_btc_1h_candles(days=TRAIN_WINDOW_DAYS + FEATURE_HISTORY_DAYS + 5)
        if candles.empty:
            return None
        feats = build_features(candles, fetch_funding_rate())
        if feats.empty:
            return None

        try:
            feats = feats[model.feature_names_]
        except KeyError as exc:
            logger.warning("feature mismatch: %s — refitting", exc)
            self.refit()
            return None

        preds = predict_regime(model, feats, mapping)
        latest = preds.iloc[-1]
        ts = int(latest.name.timestamp())
        regime = latest["regime"]
        prob = float(latest["regime_probability"])
        state = int(latest["state"])

        arr = preds["regime"].values
        run = 0
        for v in arr[::-1]:
            if v == regime:
                run += 1
            else:
                break
        duration_h = float(run)

        state_means = model.means_ * model.feature_std_ + model.feature_mean_
        state_probs = {
            mapping[i]: float(latest.get(f"prob_state_{i}", 0.0))
            for i in range(model.n_components)
        }

        try:
            db.execute_one(
                """
                INSERT INTO regime_log
                    (ts, regime, probability, state, state_means, transition_matrix,
                     regime_duration_hours, state_probabilities)
                VALUES (%s, %s, %s, %s, %s::jsonb, %s::jsonb, %s, %s::jsonb)
                ON CONFLICT (ts) DO UPDATE SET
                    regime                = EXCLUDED.regime,
                    probability           = EXCLUDED.probability,
                    state                 = EXCLUDED.state,
                    state_means           = EXCLUDED.state_means,
                    transition_matrix     = EXCLUDED.transition_matrix,
                    regime_duration_hours = EXCLUDED.regime_duration_hours,
                    state_probabilities   = EXCLUDED.state_probabilities
                """,
                (
                    _ts_to_dt(ts), regime, prob, state,
                    json.dumps(state_means.tolist()),
                    json.dumps(model.transmat_.tolist()),
                    duration_h,
                    json.dumps(state_probs),
                ),
            )
        except Exception as exc:
            logger.warning("regime_log write failed: %s", exc)
        self.last_predict_ts = time.time()
        logger.info(
            "predict: regime=%s prob=%.2f duration=%.0fh", regime, prob, duration_h,
        )
        return {
            "ts": ts,
            "regime": regime,
            "probability": prob,
            "state": state,
            "regime_duration_hours": duration_h,
            "state_probabilities": state_probs,
            "transition_matrix": model.transmat_.tolist(),
        }

    def _persist_predictions_bulk(
        self,
        preds: pd.DataFrame,
        model,
        mapping: dict[int, str],
    ) -> None:
        regime_arr = preds["regime"].values
        duration = np.zeros(len(preds), dtype=float)
        cur = 0
        last = None
        for i, r in enumerate(regime_arr):
            if r != last:
                cur = 1
                last = r
            else:
                cur += 1
            duration[i] = cur

        state_means_json = json.dumps(
            (model.means_ * model.feature_std_ + model.feature_mean_).tolist()
        )
        transmat_json = json.dumps(model.transmat_.tolist())

        rows: list[tuple] = []
        for i, (idx, row) in enumerate(preds.iterrows()):
            state_probs = {
                mapping[j]: float(row.get(f"prob_state_{j}", 0.0))
                for j in range(model.n_components)
            }
            rows.append((
                _ts_to_dt(idx.timestamp()),
                str(row["regime"]),
                float(row["regime_probability"]),
                int(row["state"]),
                state_means_json,
                transmat_json,
                float(duration[i]),
                json.dumps(state_probs),
            ))
        try:
            with db.cursor() as cur:
                cur.executemany(
                    """
                    INSERT INTO regime_log
                        (ts, regime, probability, state, state_means, transition_matrix,
                         regime_duration_hours, state_probabilities)
                    VALUES (%s, %s, %s, %s, %s::jsonb, %s::jsonb, %s, %s::jsonb)
                    ON CONFLICT (ts) DO UPDATE SET
                        regime                = EXCLUDED.regime,
                        probability           = EXCLUDED.probability,
                        state                 = EXCLUDED.state,
                        state_means           = EXCLUDED.state_means,
                        transition_matrix     = EXCLUDED.transition_matrix,
                        regime_duration_hours = EXCLUDED.regime_duration_hours,
                        state_probabilities   = EXCLUDED.state_probabilities
                    """,
                    rows,
                )
            logger.info("persisted %d historical regime rows", len(rows))
        except Exception as exc:
            logger.warning("regime_log bulk write failed: %s", exc)


# ---------------------------------------------------------------------------
# Public sync accessor for FreqAI
# ---------------------------------------------------------------------------

FEATURE_COLUMNS: tuple[str, ...] = (
    *(f"%-regime_is_{lbl}" for lbl in REGIME_LABELS),
    *(f"%-regime_prob_{lbl}" for lbl in REGIME_LABELS),
    "%-regime_probability",
    "%-regime_duration_h",
)


def _empty_features() -> pd.DataFrame:
    cols = list(FEATURE_COLUMNS) + ["regime_label", "regime_confidence"]
    return pd.DataFrame(columns=cols)


def get_regime_features(pair: str = "BTC/USD") -> pd.DataFrame:
    """
    Return a DataFrame indexed by UTC datetime with regime features and the
    raw `regime_label` / `regime_confidence` columns used by the strategy
    for gating logic.

    `pair` is accepted for API symmetry with the other modules — regime is
    BTC-driven and broad-market, so the same series is returned for every pair.
    """
    RegimeDetector.instance().start()                      # lazy start

    cutoff = datetime.now(timezone.utc) - timedelta(days=TRAIN_WINDOW_DAYS)
    try:
        raw = db.fetch_all(
            "SELECT ts, regime, probability, regime_duration_hours, "
            "       state_probabilities "
            "FROM regime_log WHERE ts >= %s ORDER BY ts",
            (cutoff,),
        )
    except Exception as exc:
        logger.warning("get_regime_features db error: %s", exc)
        return _empty_features()

    if not raw:
        return _empty_features()

    rows = pd.DataFrame(raw)
    idx = pd.DatetimeIndex(
        pd.to_datetime(rows["ts"], utc=True), name="date",
    )
    out = pd.DataFrame(index=idx)
    regime_arr = rows["regime"].astype(str).values

    for label in REGIME_LABELS:
        out[f"%-regime_is_{label}"] = (regime_arr == label).astype(float)

    out["%-regime_probability"] = rows["probability"].astype(float).values
    out["%-regime_duration_h"] = rows["regime_duration_hours"].astype(float).fillna(0.0).values

    # JSONB columns come back as native dicts from psycopg
    def _as_dict(v):
        if isinstance(v, dict):
            return v
        if isinstance(v, str) and v:
            try:
                return json.loads(v)
            except Exception:
                return {}
        return {}

    state_probs_series = rows["state_probabilities"].apply(_as_dict)
    for label in REGIME_LABELS:
        out[f"%-regime_prob_{label}"] = state_probs_series.apply(
            lambda d, lbl=label: float(d.get(lbl, 0.0))
        ).values

    # Non-prefixed columns for strategy gating (custom_*, populate_entry/exit)
    out["regime_label"] = regime_arr
    out["regime_confidence"] = rows["probability"].astype(float).values

    return out
