"""
Data fetchers for the dashboard.

Three sources, each with graceful degradation:

  1. Freqtrade REST API (port 8080) — live candles + analyzed columns
     (regime_label, up/flat/down, meta_signal, tft_confidence). Requires
     `FREQTRADE_API_USER` / `FREQTRADE_API_PASS`.
  2. trade_journal in PostgreSQL — trade markers,
     daily P&L, recent trade history.
  3. evolution.json (`user_data/logs/evolution.json`) — current champion ID.

When the freqtrade API is unreachable the candle endpoint falls back to
public CCXT/Coinbase fetches so the chart still renders.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import httpx
import pandas as pd

try:
    import psycopg
    from psycopg.rows import dict_row
    _HAVE_PG = True
except Exception:
    psycopg = None
    dict_row = None
    _HAVE_PG = False

logger = logging.getLogger(__name__)

USER_DATA_ROOT = Path(os.environ.get(
    "USER_DATA_ROOT",
    str(Path(__file__).resolve().parent.parent),
))
EVOLUTION_LOG = USER_DATA_ROOT / "logs" / "evolution.json"


def _resolve_dsn() -> str:
    """Same DSN-build pattern as user_data/modules/db.py — URL-encodes the password."""
    from urllib.parse import quote_plus
    explicit = os.environ.get("DATABASE_URL", "").strip()
    if explicit:
        return explicit
    user = os.environ.get("POSTGRES_USER", "tradebot")
    password = os.environ.get("POSTGRES_PASSWORD", "tradebot-change-me")
    host = os.environ.get("POSTGRES_HOST", "postgres")
    port = os.environ.get("POSTGRES_PORT", "5432")
    db = os.environ.get("POSTGRES_DB", "tradebot")
    return (
        f"postgresql://{quote_plus(user)}:{quote_plus(password)}"
        f"@{host}:{port}/{db}"
    )


DATABASE_URL = _resolve_dsn()

FREQTRADE_API = os.environ.get("FREQTRADE_API_URL", "http://freqtrade:8080")
FREQTRADE_USER = os.environ.get("FREQTRADE_API_USER", "freqtrader")
FREQTRADE_PASS = os.environ.get("FREQTRADE_API_PASS", "")

# Public Coinbase Advanced Trade public-data endpoint, used only as a
# fallback when freqtrade is unreachable. Public, no auth needed.
COINBASE_PUBLIC = "https://api.exchange.coinbase.com"


# ---------------------------------------------------------------------------
# Freqtrade API
# ---------------------------------------------------------------------------


_jwt_lock = asyncio.Lock()
_jwt_token: str | None = None
_jwt_expires_at: datetime | None = None


async def _ensure_jwt(client: httpx.AsyncClient) -> str | None:
    """Login once, cache the JWT until ~9 minutes have passed."""
    global _jwt_token, _jwt_expires_at
    async with _jwt_lock:
        if _jwt_token and _jwt_expires_at and datetime.now(timezone.utc) < _jwt_expires_at:
            return _jwt_token
        if not FREQTRADE_PASS:
            return None
        try:
            resp = await client.post(
                f"{FREQTRADE_API}/api/v1/token/login",
                auth=(FREQTRADE_USER, FREQTRADE_PASS), timeout=5.0,
            )
            if resp.status_code != 200:
                logger.warning("freqtrade login failed status=%s", resp.status_code)
                return None
            payload = resp.json()
            _jwt_token = payload.get("access_token")
            _jwt_expires_at = datetime.now(timezone.utc) + timedelta(minutes=9)
            return _jwt_token
        except Exception as exc:
            logger.warning("freqtrade login exception: %s", exc)
            return None


async def fetch_freqtrade_candles(
    pair: str, timeframe: str = "5m", limit: int = 500,
) -> pd.DataFrame | None:
    """Pull pair_candles (candles + analyzed columns) from freqtrade."""
    async with httpx.AsyncClient() as client:
        token = await _ensure_jwt(client)
        if token is None:
            return None
        try:
            resp = await client.get(
                f"{FREQTRADE_API}/api/v1/pair_candles",
                params={"pair": pair, "timeframe": timeframe, "limit": limit},
                headers={"Authorization": f"Bearer {token}"},
                timeout=10.0,
            )
            if resp.status_code != 200:
                logger.warning(
                    "pair_candles %s status=%s body=%s",
                    pair, resp.status_code, resp.text[:200],
                )
                return None
            payload = resp.json()
        except Exception as exc:
            logger.warning("pair_candles fetch failed: %s", exc)
            return None

    cols = payload.get("columns") or []
    rows = payload.get("data") or []
    if not cols or not rows:
        return None
    df = pd.DataFrame(rows, columns=cols)
    if "date" in df.columns:
        df["date"] = pd.to_datetime(df["date"], utc=True, errors="coerce")
    return df


async def fetch_freqtrade_status() -> dict[str, Any]:
    """Open-trade summary + balance from freqtrade."""
    out: dict[str, Any] = {"open_trades": [], "balance": None}
    async with httpx.AsyncClient() as client:
        token = await _ensure_jwt(client)
        if token is None:
            return out
        try:
            r1 = await client.get(
                f"{FREQTRADE_API}/api/v1/status",
                headers={"Authorization": f"Bearer {token}"}, timeout=5.0,
            )
            if r1.status_code == 200:
                out["open_trades"] = r1.json() or []
            r2 = await client.get(
                f"{FREQTRADE_API}/api/v1/balance",
                headers={"Authorization": f"Bearer {token}"}, timeout=5.0,
            )
            if r2.status_code == 200:
                out["balance"] = r2.json()
        except Exception as exc:
            logger.debug("freqtrade status fetch failed: %s", exc)
    return out


# ---------------------------------------------------------------------------
# Coinbase public fallback
# ---------------------------------------------------------------------------


_TF_TO_GRAN = {
    "1m": 60, "5m": 300, "15m": 900, "1h": 3600, "6h": 21600, "1d": 86400,
}


async def fetch_coinbase_candles(
    pair: str, timeframe: str = "5m", limit: int = 300,
) -> pd.DataFrame | None:
    """
    Public Coinbase candles — used when freqtrade is unavailable.

    Coinbase Exchange's public `/products/{id}/candles` caps each request
    at 300 candles, regardless of the time window. If a caller asks for
    more, we cap silently rather than 400-ing.
    """
    product = pair.replace("/", "-").upper()
    gran = _TF_TO_GRAN.get(timeframe, 300)
    limit = min(int(limit), 300)
    end = datetime.now(timezone.utc)
    start = end - timedelta(seconds=gran * limit)
    try:
        async with httpx.AsyncClient() as client:
            r = await client.get(
                f"{COINBASE_PUBLIC}/products/{product}/candles",
                params={
                    "granularity": gran,
                    "start": start.isoformat(),
                    "end": end.isoformat(),
                },
                timeout=10.0,
            )
            if r.status_code != 200:
                return None
            rows = r.json()
    except Exception as exc:
        logger.debug("coinbase candle fetch failed: %s", exc)
        return None
    if not rows:
        return None
    # rows: [time, low, high, open, close, volume]
    df = pd.DataFrame(rows, columns=["time", "low", "high", "open", "close", "volume"])
    df["date"] = pd.to_datetime(df["time"], unit="s", utc=True)
    df = df.sort_values("date").reset_index(drop=True)
    return df[["date", "open", "high", "low", "close", "volume"]]


# ---------------------------------------------------------------------------
# Trade journal — markers + recent trades
# ---------------------------------------------------------------------------


def _journal_query(sql: str, params: tuple = ()) -> list[dict]:
    if not _HAVE_PG:
        return []
    try:
        with psycopg.connect(DATABASE_URL, connect_timeout=3) as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute(sql, params)
                return cur.fetchall()
    except Exception as exc:
        logger.debug("journal query failed: %s", exc)
        return []


def _to_unix_dt(v) -> int | None:
    if v is None:
        return None
    if isinstance(v, datetime):
        if v.tzinfo is None:
            v = v.replace(tzinfo=timezone.utc)
        return int(v.timestamp())
    return _to_unix(str(v))


def fetch_trade_markers(pair: str, since: datetime | None = None) -> list[dict]:
    """
    Return a list suitable for `series.setMarkers(...)`:
        [{"time": unix_seconds, "position": "belowBar"|"aboveBar",
          "color": "#26a69a"|"#ef5350", "shape": "arrowUp"|"arrowDown",
          "text": "+$12.3"}, ...]
    """
    sql = (
        "SELECT opened_at, closed_at, entry_price, exit_price, pnl, pair, "
        "exit_reason, confidence "
        "FROM trade_journal WHERE pair = %s "
    )
    params: list[Any] = [pair]
    if since is not None:
        sql += "AND opened_at >= %s "
        params.append(since.astimezone(timezone.utc))
    sql += "ORDER BY opened_at ASC"
    rows = _journal_query(sql, tuple(params))
    out: list[dict] = []
    for r in rows:
        opened = _to_unix_dt(r.get("opened_at"))
        if opened is not None and r.get("entry_price") is not None:
            out.append({
                "time": opened,
                "position": "belowBar",
                "color": "#22c55e",        # green entry
                "shape": "arrowUp",
                "text": f"BUY {float(r['entry_price']):.4f}",
            })
        closed = _to_unix_dt(r.get("closed_at"))
        if closed is not None and r.get("exit_price") is not None:
            pnl = float(r.get("pnl") or 0)
            out.append({
                "time": closed,
                "position": "aboveBar",
                "color": "#ef4444" if pnl < 0 else "#16a34a",
                "shape": "arrowDown",
                "text": f"SELL {float(r['exit_price']):.4f}  ({pnl:+.2f})",
            })
    out.sort(key=lambda m: m["time"])
    return out


def fetch_recent_trades(limit: int = 20) -> list[dict]:
    rows = _journal_query(
        "SELECT pair, opened_at, closed_at, entry_price, exit_price, pnl, "
        "pnl_pct, exit_reason, confidence, regime "
        "FROM trade_journal "
        "ORDER BY opened_at DESC LIMIT %s",
        (int(limit),),
    )
    # Normalise datetimes to ISO strings so the JSON encoder is happy.
    for r in rows:
        for k in ("opened_at", "closed_at"):
            v = r.get(k)
            if isinstance(v, datetime):
                r[k] = v.astimezone(timezone.utc).isoformat()
    return rows


def fetch_daily_pnl() -> dict[str, float]:
    """Returns {date: pnl_quote, ...} for the last 14 days."""
    rows = _journal_query(
        "SELECT to_char(closed_at AT TIME ZONE 'UTC', 'YYYY-MM-DD') AS day, "
        "       COALESCE(SUM(pnl), 0) AS pnl "
        "FROM trade_journal WHERE closed_at IS NOT NULL "
        "GROUP BY day ORDER BY day DESC LIMIT 14"
    )
    return {r["day"]: float(r["pnl"] or 0) for r in rows}


def _to_unix(iso: str | None) -> int | None:
    if not iso:
        return None
    try:
        s = iso.replace("Z", "+00:00") if iso.endswith("Z") else iso
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return int(dt.timestamp())
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Evolution snapshot — champion ID for the sidebar
# ---------------------------------------------------------------------------


def fetch_champion() -> dict[str, Any]:
    if not EVOLUTION_LOG.exists():
        return {}
    try:
        history = json.loads(EVOLUTION_LOG.read_text())
        if not history:
            return {}
        last = history[-1]
        champ_id = last.get("champion")
        runner_up_id = last.get("runner_up")
        alive = last.get("alive", []) or []
        champ = next((m for m in alive if m.get("member_id") == champ_id), None)
        return {
            "generation": last.get("generation"),
            "champion_id": champ_id,
            "runner_up_id": runner_up_id,
            "champion_fitness": (
                float(champ.get("fitness", 0.0)) if champ else None
            ),
        }
    except Exception:
        return {}


# ---------------------------------------------------------------------------
# Regime / sentiment / on-chain — derived from the freqtrade-analyzed columns
# ---------------------------------------------------------------------------


def regime_segments_from_df(df: pd.DataFrame, time_col: str = "date") -> list[dict]:
    """
    Compress the per-candle `regime_label` column into a sparse list of
    contiguous segments suitable for chart background shading:

        [{"start": unix_s, "end": unix_s, "label": "trending_up"}, ...]
    """
    if df is None or df.empty or "regime_label" not in df.columns:
        return []
    if time_col not in df.columns:
        return []
    s = df[[time_col, "regime_label"]].dropna().reset_index(drop=True)
    if s.empty:
        return []
    times = pd.to_datetime(s[time_col], utc=True).astype("int64") // 10**9
    labels = s["regime_label"].astype(str).tolist()
    out: list[dict] = []
    cur_label = labels[0]
    cur_start = int(times.iloc[0])
    for i in range(1, len(labels)):
        if labels[i] != cur_label:
            out.append({
                "start": cur_start,
                "end": int(times.iloc[i]),
                "label": cur_label,
            })
            cur_label = labels[i]
            cur_start = int(times.iloc[i])
    out.append({
        "start": cur_start,
        "end": int(times.iloc[-1]),
        "label": cur_label,
    })
    return out


def latest_state_from_df(df: pd.DataFrame, pair: str | None = None) -> dict[str, Any]:
    """Pull last-row regime / sentiment / on-chain / TFT signal for the sidebar.

    On-chain values are now read from the new free pipeline's DB tables
    (derivatives_features, macro_features) since freqtrade's pair_candles
    endpoint does not publish the FreqAI %- feature columns to clients.
    """
    out: dict[str, Any] = {}
    if df is None or df.empty:
        return out
    last = df.iloc[-1]
    for src, dst in (
        ("regime_label", "regime"),
        ("regime_confidence", "regime_confidence"),
        ("%-sentiment_score", "sentiment_score"),
        ("%-sentiment_confidence", "sentiment_confidence"),
        ("%-onchain_netflow_z", "onchain_netflow_z"),
        ("%-onchain_mvrv", "onchain_mvrv"),
        ("%-onchain_whale_count_1h", "onchain_whale_count"),
        ("up", "tft_up"),
        ("flat", "tft_flat"),
        ("down", "tft_down"),
        ("tft_confidence", "tft_confidence"),
        ("meta_signal", "meta_signal"),
        ("meta_confidence", "meta_confidence"),
    ):
        if src in df.columns:
            v = last.get(src, None)
            if v is None or (isinstance(v, float) and pd.isna(v)):
                continue
            try:
                out[dst] = float(v) if not isinstance(v, str) else str(v)
            except Exception:
                out[dst] = str(v)

    # Enrich with the new free on-chain pipeline (DB-direct).
    # We map:
    #   onchain_netflow_z   ← OKX funding rate × 10000  (basis points; readable)
    #   onchain_mvrv        ← BTC MVRV (only when pair=BTC/USD; else neutral 1.0)
    #   onchain_whale_count ← log1p(taker_buy_vol) over the last hour
    if pair and _HAVE_PG:
        try:
            with psycopg.connect(_resolve_dsn(), connect_timeout=2,
                                 row_factory=dict_row) as cn, cn.cursor() as cur:
                cur.execute(
                    "SELECT funding_rate, taker_buy_vol_usd, taker_sell_vol_usd "
                    "FROM derivatives_features "
                    "WHERE pair=%s ORDER BY ts DESC LIMIT 1",
                    (pair,),
                )
                deriv = cur.fetchone()
                cur.execute(
                    "SELECT btc_mvrv FROM macro_features ORDER BY ts DESC LIMIT 1"
                )
                macro = cur.fetchone()
        except Exception as exc:
            logger.debug("on-chain DB enrich failed: %s", exc)
            deriv = None
            macro = None
        if deriv:
            fr = deriv.get("funding_rate")
            buy = deriv.get("taker_buy_vol_usd") or 0.0
            sell = deriv.get("taker_sell_vol_usd") or 0.0
            if fr is not None:
                # Express funding as basis points × 100 for readability
                # (e.g. 0.0001 → 1.0). Fits the existing "Net-flow z" column
                # which the operator already reads as a positioning proxy.
                out["onchain_netflow_z"] = float(fr) * 10000.0
            if buy + sell > 0:
                import math
                out["onchain_whale_count"] = math.log1p(buy)
        if macro and pair.split("/")[0].upper() == "BTC":
            mvrv = macro.get("btc_mvrv")
            if mvrv is not None:
                out["onchain_mvrv"] = float(mvrv)
        elif "onchain_mvrv" not in out:
            out["onchain_mvrv"] = 1.0  # neutral for non-BTC pairs

    # Enrich sentiment from sentiment_log (same pattern — strategy doesn't
    # publish %-sentiment_* through freqtrade's pair_candles endpoint).
    if _HAVE_PG and "sentiment_score" not in out:
        try:
            with psycopg.connect(_resolve_dsn(), connect_timeout=2,
                                 row_factory=dict_row) as cn, cn.cursor() as cur:
                cur.execute(
                    "SELECT sentiment_score, confidence "
                    "FROM sentiment_log ORDER BY ts DESC LIMIT 1"
                )
                sent = cur.fetchone()
        except Exception as exc:
            logger.debug("sentiment DB enrich failed: %s", exc)
            sent = None
        if sent:
            if sent.get("sentiment_score") is not None:
                out["sentiment_score"] = float(sent["sentiment_score"])
            if sent.get("confidence") is not None:
                out["sentiment_confidence"] = float(sent["confidence"])

    return out
