"""
FastAPI dashboard for the trading bot.

Endpoints:
    GET  /                              — single-page UI
    GET  /api/pairs                     — list of pairs the dashboard knows about
    GET  /api/candles/{base}/{quote}    — candles + indicators + regime + state
    GET  /api/trades/{base}/{quote}     — markers (entry/exit) for that pair
    GET  /api/state                     — sidebar payload (positions, P&L, champion)
    WS   /ws                            — pushes the same `state` payload every 30s

Run from the repo root:
    uvicorn user_data.dashboard.app:app --host 0.0.0.0 --port 8081
"""

from __future__ import annotations

import asyncio
import logging
import math
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
import pandas as pd
from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .data_sources import (
    fetch_champion,
    fetch_coinbase_candles,
    fetch_daily_pnl,
    fetch_freqtrade_candles,
    fetch_freqtrade_status,
    fetch_recent_trades,
    fetch_trade_markers,
    latest_state_from_df,
    regime_segments_from_df,
)
from .indicators import attach_all
from . import ops_routes

logger = logging.getLogger(__name__)
logging.basicConfig(
    level=os.environ.get("DASHBOARD_LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)

HERE = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(HERE / "templates"))

DEFAULT_PAIRS = os.environ.get(
    "DASHBOARD_PAIRS", "BTC/USD,ETH/USD,SOL/USD",
).split(",")
DEFAULT_TIMEFRAME = os.environ.get("DASHBOARD_TIMEFRAME", "5m")
# Stocks the chart-page dropdown will offer (Alpaca paper). Reads
# WHEEL_SYMBOLS first so it tracks whatever the wheel cron pre-fetches.
DEFAULT_STOCK_SYMBOLS = [
    s.strip().upper() for s in os.environ.get(
        "DASHBOARD_STOCK_SYMBOLS",
        os.environ.get("WHEEL_SYMBOLS", "SOFI"),
    ).split(",") if s.strip()
]
WS_PUSH_INTERVAL = float(os.environ.get("DASHBOARD_WS_INTERVAL_SEC", "30"))

app = FastAPI(title="Trading bot dashboard", docs_url="/api/docs")
app.mount("/static", StaticFiles(directory=str(HERE / "static")), name="static")

# Ops tab — registers /ops (HTML) + /api/ops/* (REST envelope endpoints)
ops_routes.make_html_route(app)
app.include_router(ops_routes.router)


# ---------------------------------------------------------------------------
# UI
# ---------------------------------------------------------------------------


@app.get("/", response_class=HTMLResponse)
async def index(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "index.html",
        {
            "pairs": [p.strip() for p in DEFAULT_PAIRS if p.strip()],
            "stock_symbols": DEFAULT_STOCK_SYMBOLS,
            "default_timeframe": DEFAULT_TIMEFRAME,
            "ws_push_interval": int(WS_PUSH_INTERVAL),
        },
    )


# ---------------------------------------------------------------------------
# Pairs
# ---------------------------------------------------------------------------


@app.get("/api/pairs")
async def api_pairs() -> dict:
    return {"pairs": DEFAULT_PAIRS, "timeframe": DEFAULT_TIMEFRAME}


@app.get("/api/mode")
async def api_mode() -> dict[str, Any]:
    """Return the freqtrade run mode (paper / live / paused) for the topbar badge."""
    out = {"mode": "unknown", "state": "unknown", "dry_run": None}
    async with httpx.AsyncClient() as client:
        from .data_sources import _ensure_jwt
        token = await _ensure_jwt(client)
        if token is None:
            return out
        try:
            r = await client.get(
                f"{os.environ.get('FREQTRADE_API_URL', 'http://freqtrade:8080')}/api/v1/show_config",
                headers={"Authorization": f"Bearer {token}"},
                timeout=3.0,
            )
            if r.status_code == 200:
                cfg = r.json()
                state = str(cfg.get("state", "unknown")).lower()
                dry = bool(cfg.get("dry_run", True))
                out["state"] = state
                out["dry_run"] = dry
                if state in ("paused", "stopped"):
                    out["mode"] = "paused"
                elif dry:
                    out["mode"] = "paper"
                else:
                    out["mode"] = "live"
        except Exception as exc:
            logger.debug("mode probe failed: %s", exc)
    return out


# ---------------------------------------------------------------------------
# Candles + indicators + regime + per-pair state
# ---------------------------------------------------------------------------


@app.get("/api/candles/{base}/{quote}")
async def api_candles(
    base: str, quote: str,
    timeframe: str = DEFAULT_TIMEFRAME, limit: int = 500,
) -> dict[str, Any]:
    pair = f"{base.upper()}/{quote.upper()}"
    df = await fetch_freqtrade_candles(pair, timeframe=timeframe, limit=limit)
    source = "freqtrade"
    if df is None or df.empty:
        df = await fetch_coinbase_candles(pair, timeframe=timeframe, limit=limit)
        source = "coinbase"
    if df is None or df.empty:
        raise HTTPException(503, f"no candle source available for {pair}")

    df = attach_all(df)
    candles, volume = _candles_to_chart(df)
    rsi_pts = _line_series(df, "rsi")
    bb_upper = _line_series(df, "bb_upper")
    bb_mid = _line_series(df, "bb_mid")
    bb_lower = _line_series(df, "bb_lower")
    macd_line = _line_series(df, "macd")
    macd_signal = _line_series(df, "macd_signal")
    macd_hist = _hist_series(df, "macd_hist")
    regime = regime_segments_from_df(df)
    state = latest_state_from_df(df, pair)
    last_close = float(df["close"].iloc[-1]) if "close" in df.columns else None
    last_time = (
        int(pd.to_datetime(df["date"].iloc[-1], utc=True).timestamp())
        if "date" in df.columns else None
    )
    return {
        "pair": pair,
        "timeframe": timeframe,
        "source": source,
        "candles": candles,
        "volume": volume,
        "indicators": {
            "rsi": rsi_pts,
            "bb_upper": bb_upper,
            "bb_mid": bb_mid,
            "bb_lower": bb_lower,
            "macd": macd_line,
            "macd_signal": macd_signal,
            "macd_hist": macd_hist,
            "ema20": _line_series(df, "ema20"),
            "ema50": _line_series(df, "ema50"),
            "vwap": _line_series(df, "vwap"),
        },
        "regime_segments": regime,
        "pair_state": state,
        "last_close": last_close,
        "last_time": last_time,
    }


@app.get("/api/trades/{base}/{quote}")
async def api_trades(base: str, quote: str) -> dict[str, Any]:
    pair = f"{base.upper()}/{quote.upper()}"
    return {"pair": pair, "markers": fetch_trade_markers(pair)}


# ---------------------------------------------------------------------------
# Sidebar state
# ---------------------------------------------------------------------------


async def _build_state_payload() -> dict[str, Any]:
    pair = (DEFAULT_PAIRS[0] if DEFAULT_PAIRS else "BTC/USD").strip()

    # Run the slow pieces in parallel.
    candles_task = asyncio.create_task(
        fetch_freqtrade_candles(pair, timeframe=DEFAULT_TIMEFRAME, limit=200),
    )
    status_task = asyncio.create_task(fetch_freqtrade_status())

    df = await candles_task
    status = await status_task

    pair_state = latest_state_from_df(df, pair) if df is not None else {}
    daily_pnl = fetch_daily_pnl()
    recent = fetch_recent_trades(limit=10)
    champion = fetch_champion()

    open_trades = status.get("open_trades") or []
    positions = [{
        "pair": t.get("pair"),
        "open_rate": t.get("open_rate"),
        "stake_amount": t.get("stake_amount"),
        "current_profit": t.get("profit_pct") or t.get("profit_ratio"),
        "open_date": t.get("open_date") or t.get("open_date_hum"),
        "trade_id": t.get("trade_id"),
    } for t in open_trades]

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    today_pnl = float(daily_pnl.get(today, 0.0) or 0.0)

    return {
        "ts": datetime.now(timezone.utc).isoformat(),
        "pair": pair,
        "regime": pair_state.get("regime"),
        "regime_confidence": pair_state.get("regime_confidence"),
        "sentiment_score": pair_state.get("sentiment_score"),
        "sentiment_confidence": pair_state.get("sentiment_confidence"),
        "onchain": {
            "netflow_z": pair_state.get("onchain_netflow_z"),
            "mvrv": pair_state.get("onchain_mvrv"),
            "whale_count_1h": pair_state.get("onchain_whale_count"),
        },
        "tft": {
            "up": pair_state.get("tft_up"),
            "flat": pair_state.get("tft_flat"),
            "down": pair_state.get("tft_down"),
            "confidence": pair_state.get("tft_confidence"),
        },
        "meta_signal": pair_state.get("meta_signal"),
        "meta_confidence": pair_state.get("meta_confidence"),
        "positions": positions,
        "daily_pnl": today_pnl,
        "daily_pnl_history": daily_pnl,
        "recent_trades": recent,
        "champion": champion,
    }


@app.get("/api/state")
async def api_state() -> dict[str, Any]:
    return await _build_state_payload()


# ---------------------------------------------------------------------------
# WebSocket — push state every WS_PUSH_INTERVAL seconds
# ---------------------------------------------------------------------------


@app.websocket("/ws")
async def ws_state(ws: WebSocket) -> None:
    await ws.accept()
    try:
        while True:
            try:
                payload = await _build_state_payload()
            except Exception as exc:
                logger.warning("ws state build failed: %s", exc)
                payload = {"error": str(exc)}
            try:
                await ws.send_json(payload)
            except Exception:
                break
            try:
                await asyncio.wait_for(ws.receive_text(), timeout=WS_PUSH_INTERVAL)
            except asyncio.TimeoutError:
                pass
            except WebSocketDisconnect:
                break
    except WebSocketDisconnect:
        return
    except Exception as exc:
        logger.warning("ws error: %s", exc)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


_EPOCH_UTC = pd.Timestamp("1970-01-01", tz="UTC")


def _to_unix_seconds(series: pd.Series) -> pd.Series:
    """
    Convert a tz-aware datetime Series into int64 unix seconds.
    pandas 3.x broke `series.astype("int64") // 10**9` on tz-aware datetimes
    (returns 1 for every row); epoch-arithmetic is the version-portable fix.
    """
    s = pd.to_datetime(series, utc=True)
    return ((s - _EPOCH_UTC) // pd.Timedelta(seconds=1)).astype("int64")


def _candles_to_chart(df: pd.DataFrame) -> tuple[list[dict], list[dict]]:
    candles: list[dict] = []
    volume: list[dict] = []
    if "date" not in df.columns:
        return candles, volume
    times = _to_unix_seconds(df["date"])
    for i, t in enumerate(times):
        try:
            o = float(df["open"].iat[i]); h = float(df["high"].iat[i])
            l = float(df["low"].iat[i]);  c = float(df["close"].iat[i])
            v = float(df["volume"].iat[i])
        except Exception:
            continue
        if any(math.isnan(x) for x in (o, h, l, c, v)):
            continue
        candles.append({"time": int(t), "open": o, "high": h, "low": l, "close": c})
        # Colour volume by candle direction
        color = "rgba(34,197,94,0.45)" if c >= o else "rgba(239,68,68,0.45)"
        volume.append({"time": int(t), "value": v, "color": color})
    return candles, volume


def _line_series(df: pd.DataFrame, col: str) -> list[dict]:
    if "date" not in df.columns or col not in df.columns:
        return []
    out: list[dict] = []
    times = _to_unix_seconds(df["date"])
    series = df[col]
    for i in range(len(df)):
        v = series.iat[i]
        if v is None or (isinstance(v, float) and (math.isnan(v) or math.isinf(v))):
            continue
        out.append({"time": int(times.iat[i]), "value": float(v)})
    return out


def _hist_series(df: pd.DataFrame, col: str) -> list[dict]:
    if "date" not in df.columns or col not in df.columns:
        return []
    out: list[dict] = []
    times = _to_unix_seconds(df["date"])
    series = df[col]
    for i in range(len(df)):
        v = series.iat[i]
        if v is None or (isinstance(v, float) and (math.isnan(v) or math.isinf(v))):
            continue
        color = "rgba(34,197,94,0.7)" if v >= 0 else "rgba(239,68,68,0.7)"
        out.append({"time": int(times.iat[i]), "value": float(v), "color": color})
    return out
