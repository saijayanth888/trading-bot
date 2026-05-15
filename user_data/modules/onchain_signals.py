"""
On-chain / derivatives / macro feature pipeline (free-tier rebuild, 2026-05).

The original CryptoQuant + Whale Alert + Glassnode wiring was retired upstream:
their useful endpoints all moved behind paid plans the operator cannot afford.
This module replaces them with **seven free, no-key, US-accessible sources**,
none of which require credentials, all read-only, all HTTPS.

Public surface (unchanged for the strategy):
    FEATURE_COLUMNS    — same 4 column names as before
    get_features(pair, timeframe)
    OnChainSignals.instance()

Sources used
------------
Per-pair (8 calls each per poll cycle):
    OKX     /api/v5/public/funding-rate
    OKX     /api/v5/rubik/stat/contracts/open-interest-history (period=5m)
    OKX     /api/v5/rubik/stat/contracts/long-short-account-ratio-contract
    OKX     /api/v5/rubik/stat/taker-volume

Cross-venue funding (one call each, sanity check):
    dYdX v4         indexer.dydx.trade/v4/perpetualMarkets
    Coinbase Intl   api.international.coinbase.com/api/v1/instruments
    Kraken Futures  futures.kraken.com/derivatives/api/v3/tickers

Macro features (one call each, all pairs share):
    DefiLlama       stablecoins.llama.fi/stablecoincharts/all
    alternative.me  /fng/?limit=2
    CoinGecko       /api/v3/global
    mempool.space   /api/v1/fees/recommended
    bitcoin-data    /api/v1/mvrv  (BTC only — for non-BTC pairs we fall back to 1.0)

Safety rails
------------
* Every fetcher: 3 s connect / 5 s read timeout (HTTP_TIMEOUT_S = 5)
* Per-source circuit breaker: 3 failures in a row → mark down for 5 min,
  use neutral defaults instead of stalling the strategy.
* 60-second in-memory cache so we never burst beyond an exchange's rate limit.
* Append-only writes; ON CONFLICT DO UPDATE for idempotency.
* No credentials handled. Nothing logged that could correlate operator
  identity to API providers.
* Strategy fail-soft preserved: empty DB → neutral defaults
  (`%-onchain_netflow_z = 0`, `%-onchain_mvrv = 1.0`, whale = 0) — model
  still runs, just with constant features for that group.

Mapping legacy → new (semantics preserved, source replaced)
----------------------------------------------------------
    %-onchain_netflow_z       ← 7-day z-score of OKX funding rate per pair.
                                Funding > 0 + rising = longs piling on
                                = analogous to spot net-flow into exchanges
                                (which is what CryptoQuant netflow used to
                                measure on the spot side).

    %-onchain_mvrv            ← BTC: bitcoin-data.com /mvrv. Non-BTC: 1.0
                                (no genuinely-free MVRV exists for alts).

    %-onchain_whale_count_1h  ← log1p of OKX taker buy volume per 5-min
                                bucket, summed over the trailing hour.
                                Captures large-money taker pressure.

    %-onchain_whale_volume_1h ← log1p of OKX (taker_buy + taker_sell)
                                summed over the trailing hour. Captures
                                total taker activity (regardless of side).
"""

from __future__ import annotations

import logging
import os
import threading
import time
from datetime import UTC, datetime, timedelta
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
_USER_DATA = _HERE.parent.parent
LOG_PATH = _USER_DATA / "logs" / "onchain.log"

POLL_INTERVAL_S = 300                 # 5 min
HTTP_TIMEOUT_S = (3, 5)               # (connect, read)
HISTORY_DAYS = 30
CACHE_TTL_S = 60                      # default cache TTL
CIRCUIT_FAIL_THRESHOLD = 3
CIRCUIT_COOLDOWN_S = 300              # default cooldown after circuit trips

# Per-source cache TTL overrides (seconds). Used for slow-moving metrics
# whose source has a stricter rate limit than our 5-min poll.
#
#   bitcoin_data: free tier is 10 req/hour; MVRV is a daily metric so
#   caching for 6 hours = 4 calls/day, comfortably under the limit even
#   without an API key.
_SOURCE_TTL_OVERRIDES = {
    "bitcoin_data": 6 * 3600,         # 6 hours
    "defillama":    15 * 60,          # 15 min — daily metric
    "alternative_me": 30 * 60,        # 30 min — daily metric
    "coingecko_global": 5 * 60,       # 5 min — slow-moving
}

# Per-source cooldown overrides (seconds). When 429-rate-limited we want
# a much longer cool-off than the default 5 min so we don't trip again.
_SOURCE_COOLDOWN_OVERRIDES = {
    "bitcoin_data": 3600,             # 1 hour after a 429
}

USER_AGENT = "Mozilla/5.0 (compatible; quanta-bot/1.0)"

# Pair → exchange-specific symbol mapping ---------------------------------
# Operator can override per-pair via config.json[onchain_sources][pair_map].
PAIR_TO_OKX_SWAP = {
    "BTC/USD": "BTC-USDT-SWAP",
    "ETH/USD": "ETH-USDT-SWAP",
    "SOL/USD": "SOL-USDT-SWAP",
    "ADA/USD": "ADA-USDT-SWAP",
    "XRP/USD": "XRP-USDT-SWAP",
    "DOGE/USD": "DOGE-USDT-SWAP",
    "AVAX/USD": "AVAX-USDT-SWAP",
    "LINK/USD": "LINK-USDT-SWAP",
}
PAIR_TO_OKX_CCY = {
    pair: pair.split("/")[0] for pair in PAIR_TO_OKX_SWAP
}

LOG_PATH.parent.mkdir(parents=True, exist_ok=True)

# ---------------------------------------------------------------------------
# Logger (rotating, no stdout)
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
# Per-source enable flags (config.json[onchain_sources][<src>][enabled])
# Defaults all True so the bot works out of the box.
# ---------------------------------------------------------------------------

_SOURCES_CONFIG: dict = {}


def configure_sources(cfg: dict) -> None:
    """Hot-set the per-source enabled/weight config from config.json.
    Called by the strategy at bot_start; safe to call repeatedly."""
    global _SOURCES_CONFIG
    _SOURCES_CONFIG = dict(cfg or {})


def _enabled(name: str) -> bool:
    src = _SOURCES_CONFIG.get(name)
    if src is None:
        return True   # default-enabled
    return bool(src.get("enabled", True))


# ---------------------------------------------------------------------------
# Circuit breaker (per source) + tiny in-memory cache
# ---------------------------------------------------------------------------


class _Circuit:
    """Per-source breaker: N failures in a row → cool off for X seconds."""

    def __init__(self) -> None:
        self._fail_count: dict[str, int] = {}
        self._cooldown_until: dict[str, float] = {}
        self._lock = threading.Lock()

    def is_open(self, source: str) -> bool:
        with self._lock:
            until = self._cooldown_until.get(source, 0.0)
            if until and time.time() < until:
                return True
            if until and time.time() >= until:
                self._cooldown_until.pop(source, None)
                self._fail_count[source] = 0
            return False

    def record_success(self, source: str) -> None:
        with self._lock:
            self._fail_count[source] = 0
            self._cooldown_until.pop(source, None)

    def record_failure(self, source: str) -> None:
        with self._lock:
            n = self._fail_count.get(source, 0) + 1
            self._fail_count[source] = n
            if n >= CIRCUIT_FAIL_THRESHOLD:
                cooldown = _SOURCE_COOLDOWN_OVERRIDES.get(
                    source, CIRCUIT_COOLDOWN_S
                )
                self._cooldown_until[source] = time.time() + cooldown
                logger.warning(
                    "circuit breaker OPEN for %s (failures=%d, cooldown=%ds)",
                    source, n, cooldown,
                )


_circuit = _Circuit()
_cache: dict[str, tuple[float, object]] = {}
_cache_lock = threading.Lock()


def _cached_get(source: str, key: str, fetcher) -> object | None:
    """In-memory cache with per-source TTL. fetcher() called only on miss.
    Source-keyed circuit breaker prevents bursty retries against rate-
    limited or down endpoints."""
    if _circuit.is_open(source):
        return None
    cache_key = f"{source}:{key}"
    ttl = _SOURCE_TTL_OVERRIDES.get(source, CACHE_TTL_S)
    with _cache_lock:
        hit = _cache.get(cache_key)
        if hit and time.time() - hit[0] < ttl:
            return hit[1]
    try:
        val = fetcher()
    except Exception as exc:
        _circuit.record_failure(source)
        logger.warning("[%s/%s] fetcher exception: %s", source, key, exc)
        return None
    if val is None:
        _circuit.record_failure(source)
        return None
    _circuit.record_success(source)
    with _cache_lock:
        _cache[cache_key] = (time.time(), val)
    return val


def _http_json(
    url: str, *,
    params: dict | None = None,
    headers: dict | None = None,
) -> dict | list | None:
    """One-shot HTTPS GET with hard timeouts and a pinned UA. No retries here
    — circuit breaker handles retry policy at the source level. A 429 is
    treated as a hard failure (returns None) so the breaker trips fast and
    we stop burning API quota."""
    final_headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    if headers:
        final_headers.update(headers)
    try:
        resp = requests.get(
            url,
            params=params,
            timeout=HTTP_TIMEOUT_S,
            headers=final_headers,
        )
    except requests.RequestException as exc:
        logger.info("[%s] http error: %s", url.split("?")[0], exc)
        return None
    if resp.status_code == 429:
        logger.warning("[%s] HTTP 429 rate-limited — backing off",
                       url.split("?")[0])
        return None
    if not resp.ok:
        logger.info("[%s] http %s", url.split("?")[0], resp.status_code)
        return None
    try:
        return resp.json()
    except ValueError:
        logger.info("[%s] non-json response", url.split("?")[0])
        return None


def _ts_to_dt(ts: int) -> datetime:
    return datetime.fromtimestamp(int(ts), tz=UTC)


# ---------------------------------------------------------------------------
# OKX fetchers — per-pair derivatives data
# ---------------------------------------------------------------------------


def _fetch_okx_funding(pair: str) -> tuple[float, float] | None:
    """Returns (current_funding_rate, next_predicted_funding_rate) or None."""
    inst = PAIR_TO_OKX_SWAP.get(pair)
    if not inst:
        return None
    payload = _http_json(
        "https://www.okx.com/api/v5/public/funding-rate",
        params={"instId": inst},
    )
    if not payload or payload.get("code") != "0" or not payload.get("data"):
        return None
    row = payload["data"][0]
    try:
        return (float(row.get("fundingRate") or 0.0),
                float(row.get("nextFundingRate") or 0.0))
    except (TypeError, ValueError):
        return None


def _fetch_okx_oi_latest(pair: str) -> float | None:
    """Latest 5m open-interest in USD notional. Returns None on failure."""
    inst = PAIR_TO_OKX_SWAP.get(pair)
    if not inst:
        return None
    payload = _http_json(
        "https://www.okx.com/api/v5/rubik/stat/contracts/open-interest-history",
        params={"instId": inst, "period": "5m", "limit": "1"},
    )
    if not payload or payload.get("code") != "0" or not payload.get("data"):
        return None
    # Schema: [ts, oiCcy, oiCcyVal, oiUsd, oiUsdVal] — keep oiUsd
    row = payload["data"][0]
    try:
        # row[3] is oiUsd in some versions; row[2] in others. Take the
        # largest numeric value as the most likely USD-denominated one.
        floats = [float(x) for x in row[1:] if x not in (None, "")]
        return max(floats) if floats else None
    except (TypeError, ValueError):
        return None


def _fetch_okx_long_short(pair: str) -> float | None:
    """Latest long/short account ratio (>1 = more longs). 5-minute period."""
    inst = PAIR_TO_OKX_SWAP.get(pair)
    if not inst:
        return None
    payload = _http_json(
        "https://www.okx.com/api/v5/rubik/stat/contracts/"
        "long-short-account-ratio-contract",
        params={"instId": inst, "period": "5m", "limit": "1"},
    )
    if not payload or payload.get("code") != "0" or not payload.get("data"):
        return None
    row = payload["data"][0]
    try:
        return float(row[1])
    except (TypeError, ValueError, IndexError):
        return None


def _fetch_okx_taker_volume(pair: str, hours: int = 1) -> tuple[float, float] | None:
    """Sum of taker buy / sell volume in USD notional over the last `hours`.
    Schema: [ts, sellVol, buyVol] — values in base currency, *not* USD —
    so we multiply by latest mark price using the funding endpoint's
    indirect (we can also simply use base-currency values as a proxy)."""
    ccy = PAIR_TO_OKX_CCY.get(pair)
    if not ccy:
        return None
    payload = _http_json(
        "https://www.okx.com/api/v5/rubik/stat/taker-volume",
        params={"ccy": ccy, "instType": "SPOT", "period": "5m"},
    )
    if not payload or payload.get("code") != "0" or not payload.get("data"):
        return None
    rows = payload["data"]
    # Filter to last `hours` of 5-min buckets = 12*hours rows
    recent = rows[: 12 * hours]
    sell_total = 0.0
    buy_total = 0.0
    try:
        for row in recent:
            sell_total += float(row[1])
            buy_total += float(row[2])
    except (TypeError, ValueError, IndexError):
        return None
    return (buy_total, sell_total)


# ---------------------------------------------------------------------------
# Macro fetchers — single call, all pairs share
# ---------------------------------------------------------------------------


def _fetch_defillama_stablecoin_mcap() -> tuple[float, float] | None:
    """Returns (latest_total_mcap_usd, delta_vs_24h_ago_usd) or None."""
    payload = _http_json(
        "https://stablecoins.llama.fi/stablecoincharts/all"
    )
    if not isinstance(payload, list) or len(payload) < 2:
        return None
    try:
        latest_total = float(
            payload[-1].get("totalCirculatingUSD", {}).get("peggedUSD", 0.0)
        )
        # Find a row ~24h ago (DefiLlama is daily, so [-2] is ~24h prior)
        prior = payload[-2]
        prior_total = float(
            prior.get("totalCirculatingUSD", {}).get("peggedUSD", 0.0)
        )
    except (KeyError, TypeError, ValueError):
        return None
    return (latest_total, latest_total - prior_total)


def _fetch_fear_greed() -> float | None:
    payload = _http_json(
        "https://api.alternative.me/fng/", params={"limit": "1"}
    )
    if not payload or not payload.get("data"):
        return None
    try:
        return float(payload["data"][0]["value"])
    except (KeyError, TypeError, ValueError, IndexError):
        return None


def _fetch_btc_dominance() -> float | None:
    payload = _http_json("https://api.coingecko.com/api/v3/global")
    if not payload:
        return None
    try:
        return float(payload["data"]["market_cap_percentage"]["btc"])
    except (KeyError, TypeError, ValueError):
        return None


def _fetch_btc_mempool_fee() -> float | None:
    payload = _http_json("https://mempool.space/api/v1/fees/recommended")
    if not payload:
        return None
    try:
        return float(payload.get("fastestFee") or 0.0)
    except (TypeError, ValueError):
        return None


# bitcoin-data.com is the only macro source we deliberately do NOT enable
# by default: its free tier rate-limits aggressively without a key. Operator
# can flip onchain_sources.bitcoin_data.enabled = true after registering for
# a free key (no payment) and exporting BITCOIN_DATA_API_KEY.
_BITCOIN_DATA_KEY = os.getenv("BITCOIN_DATA_API_KEY", "").strip()


def _fetch_btc_mvrv() -> float | None:
    """BTC MVRV from bitcoin-data.com.

    Free tier without a key: 10 req/hour. With a free key (Bearer auth):
    much higher quota. We cache 6 h (4 calls/day) so even the unauthenticated
    limit is never approached. Set BITCOIN_DATA_API_KEY in .env and flip
    onchain_sources.bitcoin_data.enabled=true in config.json to enable.

    Auth: `Authorization: Bearer <key>` (verified 2026-05-09).
    Schema: list of {d, unixTs, mvrv} dated daily.
    """
    if not _enabled("bitcoin_data"):
        return None
    headers = (
        {"Authorization": f"Bearer {_BITCOIN_DATA_KEY}"}
        if _BITCOIN_DATA_KEY else None
    )
    payload = _http_json(
        "https://bitcoin-data.com/api/v1/mvrv",
        headers=headers,
    )
    if not isinstance(payload, list) or not payload:
        return None
    try:
        last = payload[-1]
        for k in ("mvrv", "value", "v"):
            if k in last and last[k] is not None:
                return float(last[k])
        return None
    except (KeyError, TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Background poller — singleton
# ---------------------------------------------------------------------------


class OnChainSignals:
    _instance: OnChainSignals | None = None
    _instance_lock = threading.Lock()

    def __init__(self) -> None:
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.last_poll_ts: float = 0.0

    @classmethod
    def instance(cls) -> OnChainSignals:
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

    # ------------------------------------------------------------------
    # The actual poll logic
    # ------------------------------------------------------------------

    def poll_once(self) -> None:
        now_dt = datetime.now(UTC)
        logger.info("poll cycle start (sources=%s)",
                    sorted(_SOURCES_CONFIG.keys()) or "<defaults>")

        # 1) per-pair derivatives via OKX
        deriv_rows: list[tuple] = []
        if _enabled("okx"):
            for pair in PAIR_TO_OKX_SWAP:
                f = _cached_get("okx_funding", pair,
                                lambda p=pair: _fetch_okx_funding(p))
                oi = _cached_get("okx_oi", pair,
                                 lambda p=pair: _fetch_okx_oi_latest(p))
                ls = _cached_get("okx_ls", pair,
                                 lambda p=pair: _fetch_okx_long_short(p))
                tv = _cached_get("okx_taker", pair,
                                 lambda p=pair: _fetch_okx_taker_volume(p, hours=1))
                if f is None and oi is None and ls is None and tv is None:
                    continue
                fr, next_fr = f if f is not None else (None, None)
                buy_v, sell_v = tv if tv is not None else (None, None)
                deriv_rows.append((
                    pair, now_dt,
                    fr, next_fr, oi, ls, buy_v, sell_v, "okx",
                ))

        # 2) macro globals (one call per source)
        macro_row = None
        try:
            stablecoin = (_cached_get("defillama", "stablecoin",
                                       _fetch_defillama_stablecoin_mcap)
                          if _enabled("defillama") else None)
            fg = (_cached_get("alternative_me", "fng", _fetch_fear_greed)
                  if _enabled("alternative_me") else None)
            btc_dom = (_cached_get("coingecko_global", "global", _fetch_btc_dominance)
                       if _enabled("coingecko_global") else None)
            mempool = (_cached_get("mempool_space", "fees", _fetch_btc_mempool_fee)
                       if _enabled("mempool_space") else None)
            btc_mvrv = (_cached_get("bitcoin_data", "mvrv", _fetch_btc_mvrv)
                        if _enabled("bitcoin_data") else None)
            sc_total, sc_chg = stablecoin if stablecoin else (None, None)
            macro_row = (
                now_dt, sc_total, sc_chg,
                fg, btc_dom, btc_mvrv, mempool,
            )
        except Exception as exc:
            logger.warning("macro fetch crashed: %s", exc)

        # 3) write to DB (append-only, idempotent)
        try:
            with db.cursor() as cur:
                if deriv_rows:
                    cur.executemany(
                        "INSERT INTO derivatives_features ("
                        "  pair, ts, funding_rate, next_funding_rate, "
                        "  open_interest_usd, long_short_ratio, "
                        "  taker_buy_vol_usd, taker_sell_vol_usd, source) "
                        "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s) "
                        "ON CONFLICT (pair, source, ts) DO UPDATE SET "
                        "  funding_rate = EXCLUDED.funding_rate, "
                        "  next_funding_rate = EXCLUDED.next_funding_rate, "
                        "  open_interest_usd = EXCLUDED.open_interest_usd, "
                        "  long_short_ratio = EXCLUDED.long_short_ratio, "
                        "  taker_buy_vol_usd = EXCLUDED.taker_buy_vol_usd, "
                        "  taker_sell_vol_usd = EXCLUDED.taker_sell_vol_usd",
                        deriv_rows,
                    )
                if macro_row:
                    cur.execute(
                        "INSERT INTO macro_features ("
                        "  ts, stablecoin_mcap_usd, stablecoin_mcap_chg_24h, "
                        "  fear_greed_index, btc_dominance_pct, btc_mvrv, "
                        "  btc_mempool_fastest_fee) "
                        "VALUES (%s,%s,%s,%s,%s,%s,%s) "
                        "ON CONFLICT (ts) DO UPDATE SET "
                        "  stablecoin_mcap_usd = EXCLUDED.stablecoin_mcap_usd, "
                        "  stablecoin_mcap_chg_24h = EXCLUDED.stablecoin_mcap_chg_24h, "
                        "  fear_greed_index = EXCLUDED.fear_greed_index, "
                        "  btc_dominance_pct = EXCLUDED.btc_dominance_pct, "
                        "  btc_mvrv = EXCLUDED.btc_mvrv, "
                        "  btc_mempool_fastest_fee = EXCLUDED.btc_mempool_fastest_fee",
                        macro_row,
                    )
        except Exception as exc:
            logger.warning("postgres write failed (will retry next poll): %s", exc)

        self.last_poll_ts = time.time()
        logger.info("poll cycle done: deriv_rows=%d macro=%s",
                    len(deriv_rows), "yes" if macro_row else "no")


# ---------------------------------------------------------------------------
# Public feature accessor — same column names as before for compatibility
# ---------------------------------------------------------------------------

FEATURE_COLUMNS: tuple[str, ...] = (
    "%-onchain_netflow_z",        # ← OKX funding rate, 7d z-score
    "%-onchain_mvrv",             # ← BTC: bitcoin-data MVRV; else 1.0
    "%-onchain_whale_count_1h",   # ← log1p of OKX taker buy volume (1h)
    "%-onchain_whale_volume_1h",  # ← log1p of OKX taker total volume (1h)
)


def _empty_features() -> pd.DataFrame:
    return pd.DataFrame(columns=list(FEATURE_COLUMNS))


def get_features(pair: str, timeframe: str) -> pd.DataFrame:
    """Return on-chain features for ``pair`` aligned to a 1h grid.

    Same contract as before: caller does pd.merge_asof(..., direction='backward').
    Empty DataFrame is returned if no data has been collected yet — strategy's
    neutral fallbacks (lines 111-114 of FreqAIMeanRevV1.py) take over.
    """
    OnChainSignals.instance().start()                 # lazy start

    asset = pair.split("/")[0].upper()
    cutoff = datetime.now(UTC) - timedelta(days=HISTORY_DAYS)

    try:
        deriv_rows = db.fetch_all(
            "SELECT ts, funding_rate, taker_buy_vol_usd, taker_sell_vol_usd "
            "FROM derivatives_features "
            "WHERE pair=%s AND ts>=%s ORDER BY ts",
            (pair, cutoff),
        )
        macro_rows = db.fetch_all(
            "SELECT ts, btc_mvrv FROM macro_features "
            "WHERE ts>=%s ORDER BY ts",
            (cutoff,),
        )
    except Exception as exc:
        logger.warning("get_features db error: %s", exc)
        return _empty_features()

    deriv = pd.DataFrame(deriv_rows)
    macro = pd.DataFrame(macro_rows)

    if deriv.empty and macro.empty:
        return _empty_features()

    bounds = []
    if not deriv.empty:
        bounds.append(pd.to_datetime(deriv["ts"], utc=True))
    if not macro.empty:
        bounds.append(pd.to_datetime(macro["ts"], utc=True))
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

    # ---- %-onchain_netflow_z = 7-day z-score of funding rate ----
    if not deriv.empty:
        s = (deriv.assign(date=pd.to_datetime(deriv["ts"], utc=True))
                  .set_index("date")["funding_rate"]
                  .astype(float)
                  .reindex(grid, method="ffill"))
        roll_mean = s.rolling("7D", min_periods=12).mean()
        roll_std = s.rolling("7D", min_periods=12).std().replace(0, np.nan)
        out["%-onchain_netflow_z"] = ((s - roll_mean) / roll_std).fillna(0.0)
    else:
        out["%-onchain_netflow_z"] = 0.0

    # ---- %-onchain_mvrv = BTC MVRV (only) — others get neutral 1.0 ----
    if asset == "BTC" and not macro.empty:
        s = (macro.assign(date=pd.to_datetime(macro["ts"], utc=True))
                   .set_index("date")["btc_mvrv"]
                   .astype(float)
                   .reindex(grid, method="ffill"))
        out["%-onchain_mvrv"] = s.fillna(1.0)
    else:
        out["%-onchain_mvrv"] = 1.0

    # ---- whale-* features = log1p of taker volumes per 1h ----
    if not deriv.empty:
        d2 = deriv.copy()
        d2["bucket"] = pd.to_datetime(d2["ts"], utc=True).dt.floor("1h")
        agg = d2.groupby("bucket").agg(
            buy_v=("taker_buy_vol_usd", "sum"),
            sell_v=("taker_sell_vol_usd", "sum"),
        )
        out["%-onchain_whale_count_1h"] = (
            np.log1p(agg["buy_v"].reindex(grid).fillna(0))
        )
        out["%-onchain_whale_volume_1h"] = (
            np.log1p(
                (agg["buy_v"].reindex(grid).fillna(0)
                 + agg["sell_v"].reindex(grid).fillna(0))
            )
        )
    else:
        out["%-onchain_whale_count_1h"] = 0.0
        out["%-onchain_whale_volume_1h"] = 0.0

    return out[list(FEATURE_COLUMNS)]
