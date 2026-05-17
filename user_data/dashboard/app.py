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
import json
import logging
import math
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pandas as pd
from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import ops_routes
from .data_sources import (
    fetch_champion,
    fetch_coinbase_candles,
    fetch_daily_pnl,
    fetch_recent_trades,
    fetch_trade_markers,
    latest_state_from_df,
    regime_segments_from_df,
)
from .indicators import attach_all

logger = logging.getLogger(__name__)
logging.basicConfig(
    level=os.environ.get("DASHBOARD_LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
# Silence httpx per-request INFO logs (kept the level pinned post-freqtrade
# cutover so coinbase REST + ModelForge calls don't flood the dashboard log).
logging.getLogger("httpx").setLevel(logging.WARNING)

HERE = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(HERE / "templates"))

DEFAULT_PAIRS = os.environ.get(
    "DASHBOARD_PAIRS", "BTC/USD,ETH/USD,SOL/USD",
).split(",")
# Trading dashboards prioritise data over animation. Set DASHBOARD_EFFECTS=0
# in .env to drop the decorative effects.js layer (cursor trails, particle
# backgrounds, etc.) — this leaves all functional UI untouched.
EFFECTS_ENABLED = os.environ.get("DASHBOARD_EFFECTS", "1").strip() not in {"0", "false", "False", ""}
SHARK_LLM_PROVIDER = os.environ.get("SHARK_LLM_PROVIDER", "ollama")
DEFAULT_TIMEFRAME = os.environ.get("DASHBOARD_TIMEFRAME", "5m")
# Stocks the chart-page dropdown will offer (Alpaca paper). Reads
# WHEEL_SYMBOLS first so it tracks whatever the wheel cron pre-fetches.
DEFAULT_STOCK_SYMBOLS = [
    s.strip().upper() for s in os.environ.get(
        "DASHBOARD_STOCK_SYMBOLS",
        # Operator's full watchlist — SOFI is the only one currently traded
        # by the wheel, but the others are chart-able + used by Shark TFT.
        # Without these in the dropdown, /?venue=stocks&pair=SPY silently
        # falls back to SOFI because the URL param can't match an option.
        os.environ.get("WHEEL_SYMBOLS", "SOFI,PLTR,NVDA,AMD,SPY"),
    ).split(",") if s.strip()
]
WS_PUSH_INTERVAL = float(os.environ.get("DASHBOARD_WS_INTERVAL_SEC", "30"))

app = FastAPI(title="Trading bot dashboard", docs_url="/api/docs")
app.mount("/static", StaticFiles(directory=str(HERE / "static")), name="static")

# Ops tab — registers /ops (HTML) + /api/ops/* (REST envelope endpoints)
ops_routes.make_html_route(app)
app.include_router(ops_routes.router)

# 2026-05-16 cleanup: removed v4_routes.mount, the v5 router, and the
# legacy_proxy middleware. /api/v4/* was the wave-2 debate/Monte Carlo
# SPA surface that the operator never adopted. /api/v5/* was the redesign
# attempt that the operator rejected. /ops + /api/ops/* are the surviving
# operator surfaces. Producers in user_data/modules/producers/ stay — the
# /ops SPA can consume them through new /api/ops/* endpoints as needed.
# Risk-governor at-entry enforcement (single_name_cap) stays — it's wired
# into src/quanta_core/live/dispatcher.py, not into the dashboard layer.


# ---------------------------------------------------------------------------
# UI surfaces
# ---------------------------------------------------------------------------
#   /                  → Pair Dashboard SPA (per-pair candles / decisions / fills)
#   /ops               → Operator Console SPA (the 24-card overview)
#   /ops/preview       → cloud-Claude redesign mockup, Direction A (static)
#   /ops/design-canvas → pan/zoom canvas, both Operator + Telemetry directions
#   /legacy            → alias of `/` (kept for back-compat)
#   /docs              → quanta documentation


@app.get("/", response_class=HTMLResponse)
async def index(request: Request) -> HTMLResponse:
    """The Pair Dashboard SPA — drill-down on a single pair (candles, decisions,
    fills, sentiment). Title: 'Quanta — Pair dashboard (SPA)'. This is a
    SEPARATE surface from /ops (the operator console). Both live, both used."""
    return templates.TemplateResponse(request, "dashboard_spa.html", {})


@app.get("/legacy", response_class=HTMLResponse)
async def legacy_dashboard(request: Request) -> HTMLResponse:
    """Alias of `/` — kept for any link still pointing here."""
    return templates.TemplateResponse(request, "dashboard_spa.html", {})


# ---------------------------------------------------------------------------
# /ops-design static assets — serves the cloud-Claude design bundle
# (tokens.css + shared.jsx + operator.jsx + telemetry.jsx + data.js).
# Used by /ops (Direction A only) and /ops/design-canvas (both directions).
# ---------------------------------------------------------------------------
_OPS_DESIGN = HERE.parents[1] / "ops-design"
if _OPS_DESIGN.is_dir():
    app.mount("/ops-design", StaticFiles(directory=str(_OPS_DESIGN)), name="ops_design")
    logger.info("ops-design: assets mounted at /ops-design from %s", _OPS_DESIGN)
else:
    logger.warning(
        "ops-design/ not present — /ops will fall back to the legacy SPA. "
        "Copy the cloud-Claude design bundle to %s.", _OPS_DESIGN,
    )


@app.get("/docs", response_class=HTMLResponse, name="docs_page")
async def docs_page(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(request, "docs.html", {})


# ---------------------------------------------------------------------------
# Pairs
# ---------------------------------------------------------------------------


@app.get("/api/pairs")
async def api_pairs() -> dict:
    return {"pairs": DEFAULT_PAIRS, "timeframe": DEFAULT_TIMEFRAME}


@app.get("/api/universe")
async def api_universe() -> dict[str, Any]:
    """Single source of truth for all symbols the bot tracks.

    Reads user_data/universe.json. Frontend SPAs hit this on mount so
    the hero strip + dropdowns reflect whatever's currently configured
    without hardcoded fallback lists drifting out of sync.

    Path resolution: tries USER_DATA_ROOT env first (default
    /app/user_data inside the container, mounted from host's
    user_data/), then falls back to repo-relative for host-side dev.
    """
    candidates = [
        Path(os.environ.get("USER_DATA_ROOT", "/app/user_data")) / "universe.json",
        HERE.parent.parent / "user_data" / "universe.json",  # repo-relative
        HERE.parent / "universe.json",  # legacy
    ]
    for path in candidates:
        if path.exists():
            try:
                return json.loads(path.read_text())
            except Exception as exc:
                logger.warning("api_universe parse failed for %s: %s", path, exc)
    return {"error": "universe.json not found in any candidate path",
            "candidates": [str(p) for p in candidates],
            "crypto": {"pairs": []},
            "stocks": {"wheel_universe": [], "dashboard_basket": []}}


@app.get("/api/mode")
async def api_mode() -> dict[str, Any]:
    """Active trading engine + mode for the topbar badge.

    Post-2026-05-14 (freqtrade decommissioned):
      1. If LIVE_ENGINE_MODE env is "live" or "shadow" → V4 (quanta_core)
         is the active engine. Mode is "paper" (paper-fill simulator) or
         "shadow" (no orders). State is "running" when there's a recent
         decision row in quanta_schema.decisions.
      2. Otherwise mode=unknown (no fallback engine to probe).
    """
    out: dict[str, Any] = {
        "mode": "unknown", "state": "unknown", "dry_run": None,
        "engine": None,
    }

    # ---- V4 branch (post-cutover) ----
    v4_mode = (os.environ.get("LIVE_ENGINE_MODE") or "").lower()
    if v4_mode in ("live", "shadow"):
        out["engine"] = "quanta_core"
        out["state"] = "running"
        out["dry_run"] = True  # V4 paper-fill simulator (no real exchange)
        out["mode"] = "paper" if v4_mode == "live" else "shadow"
        # Try to confirm quanta-core is alive — best-effort, don't block.
        try:
            async with httpx.AsyncClient(timeout=2.0) as client:
                # quanta-core has no HTTP surface yet; we infer liveness from
                # the most recent decision row instead. That query is cheap.
                from .ops_db import _HAVE_PG, _connect  # local import
                if _HAVE_PG:
                    with _connect() as conn, conn.cursor() as cur:
                        cur.execute(
                            "SELECT EXTRACT(EPOCH FROM (NOW() - MAX(ts)))::int "
                            "FROM quanta_schema.decisions"
                        )
                        row = cur.fetchone()
                        age = row[0] if row else None
                        if age is not None and age < 600:  # decision in last 10 min
                            out["state"] = "running"
                        else:
                            out["state"] = "stale"
        except Exception as exc:
            logger.debug("v4 liveness probe failed: %s", exc)
        return out

    # ---- No engine identified ----
    # Post-2026-05-14 freqtrade decommissioning: when LIVE_ENGINE_MODE is
    # unset there is no fallback engine to probe. Defaults stand.
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
    # Post-2026-05-14 (freqtrade decommissioned): coinbase public REST is
    # the only candle source. FreqAI columns are no longer in the frame;
    # _v4_state_fallback() merges regime/sentiment/onchain below.
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
    # Post-cutover: Coinbase-sourced df has no FreqAI columns. Merge the
    # canonical regime + sentiment + on-chain from the V4-era sources so
    # the pair table doesn't render "regime unknown" / "onchain —" rows.
    if not state.get("regime"):
        state.update(_v4_state_fallback(pair))
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


def _v4_state_fallback(pair: str | None = None) -> dict[str, Any]:
    """Post-2026-05-14 freqtrade decommissioning — derive every dashboard
    sidebar field from canonical V4-era sources (TimescaleDB, not the
    freqtrade dataframe).

    Sources:
      - regime / regime_confidence       ← ops_db.regime_latest()
      - sentiment_score / confidence     ← ops_db.sentiment_latest()
      - onchain_netflow_z / mvrv / whale ← ops_db.onchain_latest(pair)
                                           (derivatives_features +
                                            macro_features hypertables)

    Returns {} on any failure (caller already handles that path).
    Wave A of the post-freqtrade rebuild (2026-05-14).
    """
    out: dict[str, Any] = {}
    try:
        from .ops_db import (
            classifier_latest,
            meta_signal_latest,
            onchain_latest,
            regime_latest,
            sentiment_latest,
        )
    except Exception:
        return out
    try:
        rl = regime_latest() or {}
        if rl.get("regime"):
            out["regime"] = rl["regime"]
            out["regime_confidence"] = float(rl.get("probability") or 0.0)
            # Surface regime_duration_hours so the pair-dashboard RegimeGuide
            # can render "Xh in regime" without a second roundtrip. Field
            # was previously kept server-side only — the regime_log writer
            # already populates it on every cycle.
            if rl.get("regime_duration_hours") is not None:
                out["regime_duration_hours"] = float(rl["regime_duration_hours"])
    except Exception:
        pass
    try:
        sl = sentiment_latest() or {}
        if sl.get("sentiment_score") is not None:
            out["sentiment_score"] = float(sl["sentiment_score"])
            out["sentiment_confidence"] = float(sl.get("confidence") or 0.0)
    except Exception:
        pass
    try:
        oc = onchain_latest(pair) or {}
        if oc.get("netflow_z") is not None:
            out["onchain_netflow_z"] = oc["netflow_z"]
        if oc.get("mvrv") is not None:
            out["onchain_mvrv"] = oc["mvrv"]
        if oc.get("whale_count_1h") is not None:
            out["onchain_whale_count"] = oc["whale_count_1h"]
    except Exception:
        pass
    try:
        ms = meta_signal_latest(pair)
        if ms is not None:
            out["meta_signal"] = ms["signal"]
            out["meta_confidence"] = ms["confidence"]
            out["meta_strategies"] = ms.get("strategies") or {}
            out["meta_reasoning"] = ms.get("reasoning")
    except Exception:
        pass
    try:
        cl = classifier_latest(pair)
        if cl is not None:
            # Wave D: card 02 TFT block reads these. UI labels this as
            # "MOMENTUM CLASSIFIER" (not "TFT") — honest naming.
            out["tft_up"] = cl["p_up"]
            out["tft_flat"] = cl["p_flat"]
            out["tft_down"] = cl["p_down"]
            out["tft_confidence"] = cl["confidence"]
            out["classifier_name"] = cl.get("classifier")
            out["classifier_features"] = cl.get("features") or {}
    except Exception:
        pass
    return out


def _quanta_open_positions() -> list[dict[str, Any]]:
    """Post-2026-05-14 — open positions come from trade_journal (mirrored
    by quanta-core's run_v4_shadow.py on every paper-fill), NOT from
    freqtrade's /api/v1/status endpoint.
    """
    try:
        from .ops_db import open_positions as _op
        return _op(limit=50)
    except Exception:
        logger.debug("open_positions fetch failed", exc_info=True)
        return []


async def _build_state_payload() -> dict[str, Any]:
    pair = (DEFAULT_PAIRS[0] if DEFAULT_PAIRS else "BTC/USD").strip()

    # Post-2026-05-14 (freqtrade decommissioned): coinbase candles for
    # price/sparklines, V4 fallback for regime/sentiment/onchain/TFT.
    df = await fetch_coinbase_candles(pair, timeframe=DEFAULT_TIMEFRAME, limit=200)
    pair_state = latest_state_from_df(df, pair) if df is not None else {}
    if not pair_state.get("regime"):
        pair_state.update(_v4_state_fallback(pair))
    daily_pnl = fetch_daily_pnl()
    recent = fetch_recent_trades(limit=10)
    champion = fetch_champion()

    # Open positions from trade_journal (quanta-core writes here on every
    # paper-fill). Replaces the old freqtrade /api/v1/status path.
    positions = _quanta_open_positions()

    today = datetime.now(UTC).strftime("%Y-%m-%d")
    today_pnl = float(daily_pnl.get(today, 0.0) or 0.0)

    return {
        "ts": datetime.now(UTC).isoformat(),
        "pair": pair,
        "regime": pair_state.get("regime"),
        "regime_confidence": pair_state.get("regime_confidence"),
        "regime_duration_hours": pair_state.get("regime_duration_hours"),
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
            "classifier": pair_state.get("classifier_name"),
            "features": pair_state.get("classifier_features"),
        },
        "meta_signal": pair_state.get("meta_signal"),
        "meta_confidence": pair_state.get("meta_confidence"),
        "meta_strategies": pair_state.get("meta_strategies"),
        "meta_reasoning": pair_state.get("meta_reasoning"),
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
