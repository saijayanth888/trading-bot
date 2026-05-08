"""
On-chain data integration for Freqtrade FreqAI strategies.

Sources (all free-tier, all need an API key from the provider):
- CryptoQuant : BTC / ETH exchange net-flow.
- Whale Alert : large transactions (>= $1M).
- Glassnode   : MVRV ratio.

Keys are read from environment variables. A missing key disables that
source only — the others continue to work.

    CRYPTOQUANT_API_KEY
    WHALE_ALERT_API_KEY
    GLASSNODE_API_KEY

Data is cached in PostgreSQL (TimescaleDB hypertables on `ts`).  A daemon thread
refreshes every five minutes and is started lazily on the first call to
``get_features``.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from datetime import datetime, timedelta, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path

import numpy as np
import pandas as pd
import requests

from . import db

# ---------------------------------------------------------------------------
# Paths and constants
# ---------------------------------------------------------------------------

_HERE = Path(__file__).resolve()
_USER_DATA = _HERE.parent.parent              # .../user_data
LOG_PATH = _USER_DATA / "logs" / "onchain.log"

POLL_INTERVAL_S = 300                          # 5 minutes
HTTP_TIMEOUT_S = 15
WHALE_MIN_USD = 1_000_000
HISTORY_DAYS = 30                              # rows returned by get_features

LOG_PATH.parent.mkdir(parents=True, exist_ok=True)

# ---------------------------------------------------------------------------
# Logger (rotating, no stdout — Freqtrade owns stdout)
# ---------------------------------------------------------------------------

logger = logging.getLogger("onchain")
if not logger.handlers:
    handler = RotatingFileHandler(
        LOG_PATH, maxBytes=2 * 1024 * 1024, backupCount=3, encoding="utf-8"
    )
    handler.setFormatter(
        logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
    )
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False

# ---------------------------------------------------------------------------
# API keys (env)
# ---------------------------------------------------------------------------

CRYPTOQUANT_API_KEY = os.getenv("CRYPTOQUANT_API_KEY", "").strip()
WHALE_ALERT_API_KEY = os.getenv("WHALE_ALERT_API_KEY", "").strip()
GLASSNODE_API_KEY = os.getenv("GLASSNODE_API_KEY", "").strip()

# ---------------------------------------------------------------------------
# Database (schema in user_data/data/schema.sql, run by db.ensure_schema())
# ---------------------------------------------------------------------------


def _ts_to_dt(ts: int) -> datetime:
    return datetime.fromtimestamp(int(ts), tz=timezone.utc)

# ---------------------------------------------------------------------------
# HTTP with exponential backoff
# ---------------------------------------------------------------------------


def _request_with_backoff(
    method: str,
    url: str,
    *,
    params: dict | None = None,
    headers: dict | None = None,
    max_retries: int = 5,
) -> requests.Response | None:
    delay = 1.0
    for attempt in range(1, max_retries + 1):
        try:
            resp = requests.request(
                method, url,
                params=params, headers=headers,
                timeout=HTTP_TIMEOUT_S,
            )
        except requests.RequestException as exc:
            logger.warning("[%s] network error (try %d/%d): %s",
                           url, attempt, max_retries, exc)
            time.sleep(delay)
            delay = min(delay * 2, 60.0)
            continue

        if resp.status_code == 429 or 500 <= resp.status_code < 600:
            retry_after = float(resp.headers.get("Retry-After", delay))
            logger.warning(
                "[%s] HTTP %d (try %d/%d), backing off %.1fs",
                url, resp.status_code, attempt, max_retries, retry_after,
            )
            time.sleep(retry_after)
            delay = min(delay * 2, 60.0)
            continue

        return resp

    logger.error("[%s] gave up after %d attempts", url, max_retries)
    return None


# ---------------------------------------------------------------------------
# Source-specific fetchers — each returns a list of rows ready for executemany
# ---------------------------------------------------------------------------


def _fetch_cryptoquant_netflow(asset: str) -> list[tuple[str, int, float]]:
    """Hourly exchange net-flow (inflow - outflow) for a chain."""
    if not CRYPTOQUANT_API_KEY:
        logger.info("CRYPTOQUANT_API_KEY missing — skipping netflow %s", asset)
        return []

    url = f"https://api.cryptoquant.com/v1/{asset.lower()}/exchange-flows/netflow"
    params = {"window": "hour", "limit": 48}
    headers = {"Authorization": f"Bearer {CRYPTOQUANT_API_KEY}"}

    resp = _request_with_backoff("GET", url, params=params, headers=headers)
    if resp is None or not resp.ok:
        logger.warning("cryptoquant netflow %s failed: %s",
                       asset, resp and resp.status_code)
        return []

    rows: list[tuple[str, int, float]] = []
    try:
        payload = resp.json()
        # Free-tier response shape: {"status": ..., "result": {"data": [...]}}
        records = (payload.get("result") or {}).get("data") or []
        for item in records:
            ts_raw = item.get("start_time") or item.get("date") or item.get("timestamp")
            net = item.get("netflow_total")
            if net is None:
                net = (item.get("inflow_total") or 0.0) - (item.get("outflow_total") or 0.0)
            if ts_raw is None:
                continue
            ts = int(pd.Timestamp(ts_raw).timestamp())
            rows.append((asset.upper(), ts, float(net)))
    except (KeyError, ValueError, TypeError) as exc:
        logger.warning("cryptoquant netflow %s parse error: %s", asset, exc)
        return []

    logger.info("cryptoquant netflow %s: %d rows", asset.upper(), len(rows))
    return rows


def _fetch_whale_alerts() -> list[tuple]:
    """Whale Alert transactions >= WHALE_MIN_USD over the past hour."""
    if not WHALE_ALERT_API_KEY:
        logger.info("WHALE_ALERT_API_KEY missing — skipping whale alerts")
        return []

    start = int(time.time()) - 3600                # free tier: 1h max lookback
    url = "https://api.whale-alert.io/v1/transactions"
    params = {
        "api_key": WHALE_ALERT_API_KEY,
        "min_value": WHALE_MIN_USD,
        "start": start,
    }

    resp = _request_with_backoff("GET", url, params=params)
    if resp is None or not resp.ok:
        logger.warning("whale alert failed: %s", resp and resp.status_code)
        return []

    rows: list[tuple] = []
    try:
        for tx in resp.json().get("transactions", []) or []:
            rows.append((
                str(tx.get("hash") or f"{tx['blockchain']}:{tx['timestamp']}"),
                int(tx["timestamp"]),
                str(tx.get("symbol", "")).upper(),
                float(tx.get("amount_usd", 0.0)),
                (tx.get("from") or {}).get("owner_type"),
                (tx.get("to") or {}).get("owner_type"),
            ))
    except (KeyError, ValueError, TypeError) as exc:
        logger.warning("whale alert parse error: %s", exc)
        return []

    logger.info("whale alert: %d txs >= $%d", len(rows), WHALE_MIN_USD)
    return rows


def _fetch_glassnode_mvrv(asset: str) -> list[tuple[str, int, float]]:
    """
    MVRV ratio. Some accounts have this on tier-2 only — if it 401/403s
    we log a warning and skip rather than crash.
    """
    if not GLASSNODE_API_KEY:
        logger.info("GLASSNODE_API_KEY missing — skipping MVRV %s", asset)
        return []

    url = "https://api.glassnode.com/v1/metrics/market/mvrv"
    params = {"a": asset.upper(), "i": "24h", "api_key": GLASSNODE_API_KEY}

    resp = _request_with_backoff("GET", url, params=params)
    if resp is None or not resp.ok:
        logger.warning("glassnode mvrv %s failed: %s",
                       asset, resp and resp.status_code)
        return []

    rows: list[tuple[str, int, float]] = []
    try:
        for item in resp.json() or []:
            rows.append((asset.upper(), int(item["t"]), float(item["v"])))
    except (KeyError, ValueError, TypeError) as exc:
        logger.warning("glassnode mvrv %s parse error: %s", asset, exc)
        return []

    logger.info("glassnode mvrv %s: %d rows", asset.upper(), len(rows))
    return rows


# ---------------------------------------------------------------------------
# Background poller — singleton
# ---------------------------------------------------------------------------


class OnChainSignals:
    _instance: "OnChainSignals | None" = None
    _instance_lock = threading.Lock()

    def __init__(self) -> None:
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.last_poll_ts: float = 0.0

    @classmethod
    def instance(cls) -> "OnChainSignals":
        with cls._instance_lock:
            if cls._instance is None:
                cls._instance = cls()
            return cls._instance

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, name="onchain-poller", daemon=True,
        )
        self._thread.start()
        logger.info("onchain poller started (interval=%ds)", POLL_INTERVAL_S)

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.poll_once()
            except Exception:
                logger.exception("poll cycle crashed")
            self._stop.wait(POLL_INTERVAL_S)

    def poll_once(self) -> None:
        logger.info("poll cycle start")
        netflow_rows: list = []
        for asset in ("btc", "eth"):
            netflow_rows.extend(_fetch_cryptoquant_netflow(asset))

        whales = _fetch_whale_alerts()

        mvrv_rows: list = []
        for asset in ("BTC", "ETH"):
            mvrv_rows.extend(_fetch_glassnode_mvrv(asset))

        try:
            with db.cursor() as cur:
                if netflow_rows:
                    cur.executemany(
                        "INSERT INTO exchange_netflow (asset, ts, netflow) "
                        "VALUES (%s, %s, %s) "
                        "ON CONFLICT (asset, ts) DO UPDATE SET netflow = EXCLUDED.netflow",
                        [(a, _ts_to_dt(t), float(v)) for (a, t, v) in netflow_rows],
                    )
                if whales:
                    cur.executemany(
                        "INSERT INTO whale_transactions "
                        "(id, ts, symbol, amount_usd, from_owner_type, to_owner_type) "
                        "VALUES (%s, %s, %s, %s, %s, %s) "
                        "ON CONFLICT (id, ts) DO UPDATE SET "
                        "  symbol = EXCLUDED.symbol, "
                        "  amount_usd = EXCLUDED.amount_usd, "
                        "  from_owner_type = EXCLUDED.from_owner_type, "
                        "  to_owner_type = EXCLUDED.to_owner_type",
                        [(wid, _ts_to_dt(t), sym, float(amt), fo, to_)
                         for (wid, t, sym, amt, fo, to_) in whales],
                    )
                if mvrv_rows:
                    cur.executemany(
                        "INSERT INTO mvrv_ratio (asset, ts, value) "
                        "VALUES (%s, %s, %s) "
                        "ON CONFLICT (asset, ts) DO UPDATE SET value = EXCLUDED.value",
                        [(a, _ts_to_dt(t), float(v)) for (a, t, v) in mvrv_rows],
                    )
        except Exception as exc:
            logger.warning("postgres write failed (will retry next poll): %s", exc)
        self.last_poll_ts = time.time()
        logger.info(
            "poll cycle done: netflow=%d whale=%d mvrv=%d",
            len(netflow_rows), len(whales), len(mvrv_rows),
        )


# ---------------------------------------------------------------------------
# Public feature accessor
# ---------------------------------------------------------------------------

FEATURE_COLUMNS: tuple[str, ...] = (
    "%-onchain_netflow_z",
    "%-onchain_mvrv",
    "%-onchain_whale_count_1h",
    "%-onchain_whale_volume_1h",
)


def _empty_features() -> pd.DataFrame:
    return pd.DataFrame(columns=list(FEATURE_COLUMNS))


def get_features(pair: str, timeframe: str) -> pd.DataFrame:
    """
    Return on-chain features for ``pair`` aligned to a 1h grid.

    The result is a DataFrame indexed by UTC timestamp with the columns
    in :data:`FEATURE_COLUMNS`. Callers should ``pd.merge_asof`` the
    result onto their candle dataframe (left_on='date', direction='backward').

    Empty DataFrame is returned if no data has been collected yet — the
    caller should fall back to neutral values.
    """
    OnChainSignals.instance().start()                 # lazy start

    asset = pair.split("/")[0].upper()
    cutoff = datetime.now(timezone.utc) - timedelta(days=HISTORY_DAYS)

    try:
        netflow_rows = db.fetch_all(
            "SELECT ts, netflow FROM exchange_netflow "
            "WHERE asset=%s AND ts>=%s ORDER BY ts",
            (asset, cutoff),
        )
        mvrv_rows = db.fetch_all(
            "SELECT ts, value FROM mvrv_ratio "
            "WHERE asset=%s AND ts>=%s ORDER BY ts",
            (asset, cutoff),
        )
        whale_rows = db.fetch_all(
            "SELECT ts, amount_usd FROM whale_transactions "
            "WHERE symbol=%s AND ts>=%s ORDER BY ts",
            (asset, cutoff),
        )
    except Exception as exc:
        logger.warning("get_features db error: %s", exc)
        return _empty_features()

    netflow = pd.DataFrame(netflow_rows)
    mvrv = pd.DataFrame(mvrv_rows)
    whales = pd.DataFrame(whale_rows)

    if netflow.empty and mvrv.empty and whales.empty:
        return _empty_features()

    bounds = [
        pd.to_datetime(df["ts"], utc=True)
        for df in (netflow, mvrv, whales) if not df.empty
    ]
    min_ts = min(b.min() for b in bounds)
    max_ts = max(b.max() for b in bounds)
    grid = pd.date_range(
        pd.Timestamp(min_ts).floor("1h"),
        pd.Timestamp(max_ts).ceil("1h"),
        freq="1h",
        tz="UTC",
    )
    out = pd.DataFrame(index=grid)
    out.index.name = "date"

    # ---- exchange netflow z-score over a rolling 7d window ----
    if not netflow.empty:
        s = (netflow.assign(date=pd.to_datetime(netflow["ts"], utc=True))
                    .set_index("date")["netflow"]
                    .astype(float)
                    .reindex(grid, method="ffill"))
        roll_mean = s.rolling("7D", min_periods=12).mean()
        roll_std = s.rolling("7D", min_periods=12).std().replace(0, np.nan)
        out["%-onchain_netflow_z"] = ((s - roll_mean) / roll_std).fillna(0.0)
    else:
        out["%-onchain_netflow_z"] = 0.0

    # ---- MVRV ratio (centred around 1.0) ----
    if not mvrv.empty:
        s = (mvrv.assign(date=pd.to_datetime(mvrv["ts"], utc=True))
                  .set_index("date")["value"]
                  .astype(float)
                  .reindex(grid, method="ffill"))
        out["%-onchain_mvrv"] = s.fillna(1.0)
    else:
        out["%-onchain_mvrv"] = 1.0

    # ---- whale activity per 1h bucket ----
    if not whales.empty:
        whales = whales.copy()
        whales["bucket"] = (
            pd.to_datetime(whales["ts"], utc=True).dt.floor("1h")
        )
        agg = whales.groupby("bucket").agg(
            count=("amount_usd", "size"),
            volume=("amount_usd", "sum"),
        )
        out["%-onchain_whale_count_1h"] = (
            np.log1p(agg["count"].reindex(grid).fillna(0))
        )
        out["%-onchain_whale_volume_1h"] = (
            np.log1p(agg["volume"].reindex(grid).fillna(0))
        )
    else:
        out["%-onchain_whale_count_1h"] = 0.0
        out["%-onchain_whale_volume_1h"] = 0.0

    return out[list(FEATURE_COLUMNS)]
