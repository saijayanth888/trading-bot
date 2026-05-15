#!/usr/bin/env python3
"""
refit_regime_hmm.py — host-side daily refit of the BTC regime HMM.

Why: the V4 quanta-core runner (`scripts/run_v4_shadow.py`) loads
`/app/regime_hmm.json` (baked into the image) at startup and uses its
Gaussian parameters to classify every BTC bar into one of four regimes
(trending_up, trending_down, mean_reverting, high_volatility). The
freqtrade-side daemon that used to refit the model every 24h
(`user_data/modules/regime_detector.py::RegimeDetector._run`) is no longer
running — freqtrade was stopped during the V4 cutover. Without a daily
refit the HMM's z-score baseline (`feature_mean_` / `feature_std_`)
drifts away from the current market and the regime labels become
noise.

This script runs daily from Hermes cron and:
  1. Pulls 30d of BTC/USD 1h candles from Coinbase Exchange public REST
     (same source pattern as `run_v4_shadow.py:fetch_coinbase_candles`).
  2. Builds the 4 features the existing JSON model expects:
     [log_return, realized_vol_30d, volume_ratio, rsi_14].
  3. Fits a fresh GaussianHMM (n=4, diag covariance) and maps states →
     regime labels using the same heuristic as `regime_detector._label_states`.
  4. Atomically overwrites `user_data/data/regime_hmm.json` (tempfile + rename).
  5. Posts a one-line Slack notification on success/failure.

IMPORTANT (host vs container mismatch): the model file is BAKED INTO the
quanta-core Docker image at /app/regime_hmm.json and user_data/ is NOT
bind-mounted. Refitting the host file does NOT propagate to the running
container — a rebuild is required for the new fit to take effect. See
the cron output / operator runbook for the cadence.

Exits 0 on success, 1 on failure (any uncaught exception, no data, etc).
"""
from __future__ import annotations

import json
import logging
import math
import os
import sys
import time
import urllib.request
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
import pandas as pd
import requests

# ---------------------------------------------------------------------------
# Paths & constants
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parent.parent
MODEL_PATH = REPO_ROOT / "user_data" / "data" / "regime_hmm.json"
LOG_PATH = REPO_ROOT / "user_data" / "logs" / "regime_refit.log"
LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
MODEL_PATH.parent.mkdir(parents=True, exist_ok=True)

COINBASE_EXCHANGE_BASE = "https://api.exchange.coinbase.com"
HTTP_TIMEOUT = 20

# Feature window. The freqtrade-era daemon used 90d for training; for daily
# refits 30d is what we ingest (matches the runner's regime-compute window
# and is enough for the 30-day rolling vol). 30d × 24h = 720 bars is the
# minimum for the realized-vol window itself.
INGEST_DAYS = 30
N_REGIMES = 4

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_PATH),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger("regime_refit")


# ---------------------------------------------------------------------------
# HTTP with retry (mirrors user_data/modules/regime_detector.py::_http_get)
# ---------------------------------------------------------------------------

def _http_get(url: str, params: dict | None = None, max_retries: int = 4
              ) -> requests.Response | None:
    delay = 1.0
    for attempt in range(1, max_retries + 1):
        try:
            r = requests.get(
                url, params=params, timeout=HTTP_TIMEOUT,
                headers={"User-Agent": "trading-bot-regime-refit/1.0"},
            )
        except requests.RequestException as exc:
            log.warning("[%s] error (try %d): %s", url, attempt, exc)
            time.sleep(delay)
            delay = min(delay * 2, 30.0)
            continue
        if r.status_code == 429 or 500 <= r.status_code < 600:
            retry_after = float(r.headers.get("Retry-After", delay))
            log.warning("[%s] HTTP %d (try %d) backoff %.1fs",
                        url, r.status_code, attempt, retry_after)
            time.sleep(retry_after)
            delay = min(delay * 2, 30.0)
            continue
        return r
    return None


# ---------------------------------------------------------------------------
# Coinbase public OHLCV (1h, paginated, max 300 candles per call)
# ---------------------------------------------------------------------------

_CANDLE_COLS = ["time", "low", "high", "open", "close", "volume"]


def fetch_btc_1h_candles(days: int = INGEST_DAYS) -> pd.DataFrame:
    """30d × 24h = 720 bars at most. Returns DataFrame sorted oldest-first."""
    granularity = 3600
    end_ts = int(time.time())
    start_ts = end_ts - days * 86400
    rows: list[list[float]] = []
    cur_end = end_ts
    url = f"{COINBASE_EXCHANGE_BASE}/products/BTC-USD/candles"
    while cur_end > start_ts:
        cur_start = max(start_ts, cur_end - 300 * granularity)
        params = {
            "granularity": granularity,
            "start": datetime.fromtimestamp(cur_start, tz=UTC).isoformat(),
            "end":   datetime.fromtimestamp(cur_end,   tz=UTC).isoformat(),
        }
        r = _http_get(url, params=params)
        if r is None or not r.ok:
            log.warning("coinbase candles fetch failed at cur_end=%d", cur_end)
            break
        try:
            chunk = r.json()
        except json.JSONDecodeError:
            log.warning("coinbase candles JSON decode failed")
            break
        if not chunk:
            break
        rows.extend(chunk)
        oldest = min(c[0] for c in chunk)
        if oldest <= start_ts:
            break
        cur_end = oldest - granularity
        time.sleep(0.3)
    if not rows:
        return pd.DataFrame(columns=_CANDLE_COLS)
    return (
        pd.DataFrame(rows, columns=_CANDLE_COLS)
        .drop_duplicates(subset="time")
        .sort_values("time")
        .reset_index(drop=True)
    )


# ---------------------------------------------------------------------------
# Feature engineering (matches regime_detector.build_features for the
# 4 features the production JSON model uses; funding-rate optional 5th
# feature is intentionally omitted — the current production model
# was fit on 4 features and the V4 runner only computes 4).
# ---------------------------------------------------------------------------

def _rsi(close: pd.Series, period: int = 14) -> pd.Series:
    delta = close.diff()
    gain = delta.clip(lower=0.0)
    loss = -delta.clip(upper=0.0)
    avg_gain = gain.ewm(alpha=1.0 / period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1.0 / period, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0.0, np.nan)
    return 100.0 - 100.0 / (1.0 + rs)


def build_features(candles: pd.DataFrame) -> pd.DataFrame:
    if candles.empty:
        return pd.DataFrame()
    df = candles.copy()
    df["date"] = pd.to_datetime(df["time"], unit="s", utc=True)
    df = df.set_index("date").sort_index()
    df["close"] = df["close"].astype(float)
    df["volume"] = df["volume"].astype(float)

    df["log_return"] = np.log(df["close"] / df["close"].shift(1))
    annualisation = math.sqrt(24 * 365)
    # min_periods=24*7 mirrors the freqtrade module; with 30d ingest we'll
    # have enough samples for the full 30-day window once warmed.
    df["realized_vol_30d"] = (
        df["log_return"].rolling(30 * 24, min_periods=24 * 7).std() * annualisation
    )
    df["volume_ratio"] = df["volume"] / df["volume"].rolling(20).mean()
    df["rsi_14"] = _rsi(df["close"], 14)

    feats = ["log_return", "realized_vol_30d", "volume_ratio", "rsi_14"]
    return df[feats].dropna()


# ---------------------------------------------------------------------------
# HMM fit + state→label mapping (mirrors regime_detector.fit_hmm/_label_states)
# ---------------------------------------------------------------------------

def fit_hmm(features: pd.DataFrame, n_states: int = N_REGIMES, seed: int = 42):
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
    fnames = list(model.feature_names_)
    means_n = model.means_
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


# ---------------------------------------------------------------------------
# JSON serialisation — schema identical to regime_detector._serialise_model
# so the V4 runner (_load_regime_model) can consume it without changes.
# ---------------------------------------------------------------------------

def serialise_model(model, state_to_label: dict[int, str], fitted_at: int) -> dict:
    # hmmlearn's `.covars_` property is the 3D full-matrix diag form for
    # covariance_type="diag" (n_components × n_features × n_features). That
    # matches what the V4 runner's _gaussian_logpdf_diag indexes with
    # `covar_diag[i][i]`, so we preserve the 3D shape.
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


def atomic_write_json(blob: dict, path: Path) -> None:
    """Write to a sibling tempfile then rename — never truncate the live file."""
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(blob, f)
        f.flush()
        os.fsync(f.fileno())
    tmp.replace(path)


# ---------------------------------------------------------------------------
# Slack one-liner
# ---------------------------------------------------------------------------

def slack_post(message: str) -> None:
    url = (os.environ.get("SLACK_WEBHOOK_URL") or "").strip()
    if not url:
        log.info("no SLACK_WEBHOOK_URL; skipping slack post")
        return
    try:
        req = urllib.request.Request(
            url,
            data=json.dumps({"text": message}).encode(),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=10) as r:
            log.info("slack status=%s", r.status)
    except Exception as exc:
        log.warning("slack post failed: %s", exc)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    started = time.time()
    log.info("regime HMM refit started; target=%s", MODEL_PATH)

    # Capture prior fitted_at for the post-mortem message
    prior_fitted_at = None
    if MODEL_PATH.exists():
        try:
            prior = json.loads(MODEL_PATH.read_text())
            prior_fitted_at = int(prior.get("fitted_at") or 0)
        except Exception as exc:
            log.warning("could not read prior fitted_at: %s", exc)

    try:
        candles = fetch_btc_1h_candles(days=INGEST_DAYS)
        if candles.empty:
            slack_post(":rotating_light: *[hmm_refit]* FAILED — no candles from Coinbase")
            log.error("aborting: empty candles")
            return 1
        log.info("fetched %d BTC/USD 1h candles", len(candles))

        feats = build_features(candles)
        if len(feats) < 200:
            slack_post(
                f":rotating_light: *[hmm_refit]* FAILED — only {len(feats)} feature "
                f"rows (need ≥200)"
            )
            log.error("aborting: only %d feature rows", len(feats))
            return 1
        log.info("built features: %d rows, cols=%s", len(feats), list(feats.columns))

        model, mapping = fit_hmm(feats)
        # Optional health check — score the training set
        Xn = (feats.values - model.feature_mean_) / model.feature_std_
        try:
            ll = float(model.score(Xn))
        except Exception:
            ll = float("nan")
        log.info("fit complete: n=%d ll=%.2f mapping=%s", len(feats), ll, mapping)

        fitted_at = int(time.time())
        blob = serialise_model(model, mapping, fitted_at)
        atomic_write_json(blob, MODEL_PATH)
        log.info("wrote %s (fitted_at=%d)", MODEL_PATH, fitted_at)

        elapsed = time.time() - started
        prior_str = (
            datetime.fromtimestamp(prior_fitted_at, tz=UTC).isoformat()
            if prior_fitted_at else "n/a"
        )
        new_str = datetime.fromtimestamp(fitted_at, tz=UTC).isoformat()
        slack_post(
            f":white_check_mark: *[hmm_refit]* OK — n={len(feats)} ll={ll:.1f} "
            f"mapping={mapping} prior={prior_str} new={new_str} elapsed={elapsed:.1f}s"
        )
        return 0

    except Exception as exc:
        log.exception("refit failed: %s", exc)
        slack_post(f":rotating_light: *[hmm_refit]* FAILED — {type(exc).__name__}: {exc}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
