"""
FastAPI sub-router for the Ops tab.

Mounted from app.py via ``app.include_router(ops_routes.router)``.

Endpoint contract (every endpoint returns this envelope):

    {
        "status":     "ok" | "degraded" | "down",
        "data":       {...} | [...],
        "error":      None | "human-readable string",
        "checked_at": ISO-8601 UTC timestamp
    }

Hard 2 s timeout per endpoint via inner ``asyncio.wait_for``. The router
itself does not enforce HTTP-level timeouts; the dashboard frontend has its
own 3 s fetch timeout.
"""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from . import ops_db, ops_probes
from .data_sources import _ensure_jwt

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/ops", tags=["ops"])

HERE = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(HERE / "templates"))

ENDPOINT_TIMEOUT_S = float(os.environ.get("OPS_ENDPOINT_TIMEOUT_S", "3.5"))
FREQTRADE_API_URL = os.environ.get("FREQTRADE_API_URL", "http://freqtrade:8080")


def _envelope(status: str, data: Any = None, error: str | None = None) -> dict[str, Any]:
    return {
        "status": status,
        "data": data,
        "error": error,
        "checked_at": datetime.now(timezone.utc).isoformat(),
    }


async def _bounded(coro, fallback_fn, *fallback_args):
    """Run ``coro`` with the endpoint timeout; fall back gracefully on TimeoutError."""
    try:
        return await asyncio.wait_for(coro, timeout=ENDPOINT_TIMEOUT_S)
    except asyncio.TimeoutError:
        return fallback_fn(*fallback_args)


# --------------------------------------------------------------------------
# /ops — HTML page
# --------------------------------------------------------------------------


# Mounted at app level so / and /ops live side-by-side; the router's prefix
# is ``/api/ops`` so this view function is registered separately.
def make_html_route(app):
    @app.get("/ops", response_class=HTMLResponse, name="ops_page")
    async def ops_page(request: Request) -> HTMLResponse:
        return templates.TemplateResponse(request, "ops.html", {})
    return ops_page


# --------------------------------------------------------------------------
# /api/ops/services
# --------------------------------------------------------------------------


@router.get("/services")
async def services():
    try:
        results = await asyncio.wait_for(ops_probes.services_summary(), timeout=ENDPOINT_TIMEOUT_S)
    except asyncio.TimeoutError:
        return _envelope("down", error="services_summary timed out")
    except Exception as exc:
        logger.exception("services_summary failed")
        return _envelope("down", error=str(exc))

    down = [k for k, v in results.items() if not v.get("up")]
    if not down:
        return _envelope("ok", data=results)
    if len(down) == len(results):
        return _envelope("down", data=results, error="all probes failed")
    return _envelope("degraded", data=results, error=f"down: {','.join(down)}")


# --------------------------------------------------------------------------
# /api/ops/training
# --------------------------------------------------------------------------


@router.get("/training")
async def training():
    try:
        # training_state is sync; offload so it doesn't block the loop on slow IO
        loop = asyncio.get_running_loop()
        result = await asyncio.wait_for(
            loop.run_in_executor(None, ops_probes.training_state),
            timeout=ENDPOINT_TIMEOUT_S,
        )
    except asyncio.TimeoutError:
        return _envelope("down", error="training_state timed out")
    except Exception as exc:
        logger.exception("training_state failed")
        return _envelope("down", error=str(exc))

    has_any = any(result.get(k) for k in ("tft", "drl", "ept"))
    return _envelope("ok" if has_any else "degraded", data=result,
                     error=None if has_any else "no training signals available yet")


# --------------------------------------------------------------------------
# /api/ops/regime
# --------------------------------------------------------------------------


@router.get("/regime")
async def regime():
    try:
        loop = asyncio.get_running_loop()
        latest = await asyncio.wait_for(loop.run_in_executor(None, ops_db.regime_latest),
                                        timeout=ENDPOINT_TIMEOUT_S)
        transitions = await asyncio.wait_for(loop.run_in_executor(None, ops_db.regime_transitions_24h),
                                             timeout=ENDPOINT_TIMEOUT_S)
    except asyncio.TimeoutError:
        return _envelope("down", error="regime query timed out")
    except Exception as exc:
        logger.exception("regime query failed")
        return _envelope("down", error=str(exc))

    if not latest:
        return _envelope("degraded", data={"current": None}, error="regime_log empty")

    # Stale check: > 15 min old → degraded
    ts = latest.get("ts")
    age_s = None
    if ts:
        age_s = (datetime.now(timezone.utc) - ts).total_seconds() if ts.tzinfo else None
    stale = (age_s is not None) and age_s > 15 * 60

    return _envelope(
        "degraded" if stale else "ok",
        data={
            "current": latest.get("regime"),
            "probability": float(latest.get("probability") or 0),
            "duration_hours": float(latest.get("regime_duration_hours") or 0),
            "ts": ts.isoformat() if ts else None,
            "age_s": int(age_s) if age_s is not None else None,
            "transitions_24h": [
                {"ts": r["ts"].isoformat() if r.get("ts") else None,
                 "regime": r.get("regime"),
                 "duration_h": float(r.get("regime_duration_hours") or 0)}
                for r in transitions
            ],
        },
        error="regime row > 15 min old" if stale else None,
    )


# --------------------------------------------------------------------------
# /api/ops/sentiment
# --------------------------------------------------------------------------


@router.get("/sentiment")
async def sentiment():
    try:
        loop = asyncio.get_running_loop()
        latest = await asyncio.wait_for(loop.run_in_executor(None, ops_db.sentiment_latest),
                                        timeout=ENDPOINT_TIMEOUT_S)
        hourly = await asyncio.wait_for(loop.run_in_executor(None, ops_db.sentiment_hourly_24h),
                                        timeout=ENDPOINT_TIMEOUT_S)
    except asyncio.TimeoutError:
        return _envelope("down", error="sentiment query timed out")
    except Exception as exc:
        logger.exception("sentiment query failed")
        return _envelope("down", error=str(exc))

    if not latest:
        return _envelope("degraded", data={"score": None}, error="sentiment_log empty")

    ts = latest.get("ts")
    age_s = (datetime.now(timezone.utc) - ts).total_seconds() if ts and ts.tzinfo else None
    stale = (age_s is not None) and age_s > 30 * 60

    return _envelope(
        "degraded" if stale else "ok",
        data={
            "score": float(latest.get("sentiment_score") or 0),
            "confidence": float(latest.get("confidence") or 0),
            "agreement": bool(latest.get("agreement")),
            "n_headlines": int(latest.get("n_headlines") or 0),
            "ts": ts.isoformat() if ts else None,
            "age_s": int(age_s) if age_s is not None else None,
            "hourly_24h": [
                {"hour": r["hour"].isoformat() if hasattr(r["hour"], "isoformat") else str(r["hour"]),
                 "score": float(r["score"]),
                 "n": int(r["n"])}
                for r in hourly
            ],
        },
        error="sentiment row > 30 min old" if stale else None,
    )


# --------------------------------------------------------------------------
# /api/ops/mcp
# --------------------------------------------------------------------------


@router.get("/mcp")
async def mcp():
    try:
        result = await asyncio.wait_for(ops_probes.mcp_state(), timeout=ENDPOINT_TIMEOUT_S)
    except asyncio.TimeoutError:
        return _envelope("down", error="mcp probe timed out")
    except Exception as exc:
        logger.exception("mcp_state failed")
        return _envelope("down", error=str(exc))

    if not result["probe"].get("ok_for_streamable_http"):
        return _envelope("down", data=result, error="mcp endpoint not reachable")
    return _envelope("ok", data=result)


# --------------------------------------------------------------------------
# /api/ops/trades_risk
# --------------------------------------------------------------------------


@router.get("/trades_risk")
async def trades_risk():
    """Combine freqtrade live status + Postgres-derived risk numbers."""
    try:
        # Run DB query and freqtrade probe concurrently
        loop = asyncio.get_running_loop()
        db_task = loop.run_in_executor(None, ops_db.trades_risk_summary)

        async def _ft():
            async with httpx.AsyncClient(timeout=ENDPOINT_TIMEOUT_S) as client:
                token = await _ensure_jwt(client)
                if token is None:
                    return {"status": None, "open_trades": [], "error": "freqtrade auth failed"}
                headers = {"Authorization": f"Bearer {token}"}
                r = await client.get(f"{FREQTRADE_API_URL}/api/v1/status", headers=headers)
                return {"status": r.status_code, "open_trades": r.json() if r.status_code == 200 else []}

        ft_data, db_data = await asyncio.wait_for(
            asyncio.gather(_ft(), db_task), timeout=ENDPOINT_TIMEOUT_S * 2,
        )
    except asyncio.TimeoutError:
        return _envelope("down", error="trades_risk timed out")
    except Exception as exc:
        logger.exception("trades_risk failed")
        return _envelope("down", error=str(exc))

    open_trades = ft_data.get("open_trades") or []
    open_count = len(open_trades) if isinstance(open_trades, list) else 0

    return _envelope(
        "ok" if ft_data.get("status") == 200 else "degraded",
        data={
            "open_count": open_count,
            "max_open": int(os.environ.get("OPS_MAX_OPEN_TRADES", "6")),
            "open_trades": open_trades if isinstance(open_trades, list) else [],
            "daily_pnl_usd": db_data.get("daily_pnl_usd"),
            "daily_pnl_pct": db_data.get("daily_pnl_pct"),
            "closed_today": db_data.get("closed_today"),
            "drawdown_pct_30d": db_data.get("drawdown_pct_30d"),
            "circuit_breaker": db_data.get("circuit_breaker"),
            "live_tape": [
                {"pair": r.get("pair"), "side": r.get("side"),
                 "exit_time": r["exit_time"].isoformat() if r.get("exit_time") else None,
                 "pnl_pct": float(r.get("pnl_pct") or 0),
                 "pnl_abs": float(r.get("pnl_abs") or 0),
                 "regime_at_entry": r.get("regime_at_entry")}
                for r in (db_data.get("live_tape") or [])
            ],
        },
        error=None if ft_data.get("status") == 200 else f"freqtrade status={ft_data.get('status')}",
    )


# --------------------------------------------------------------------------
# Mutating: /api/ops/pause + /api/ops/resume
# --------------------------------------------------------------------------


async def _freqtrade_post(endpoint: str) -> tuple[int, dict | None, str | None]:
    async with httpx.AsyncClient(timeout=ENDPOINT_TIMEOUT_S) as client:
        token = await _ensure_jwt(client)
        if token is None:
            return 401, None, "freqtrade auth failed"
        headers = {"Authorization": f"Bearer {token}"}
        r = await client.post(f"{FREQTRADE_API_URL}{endpoint}", headers=headers)
        try:
            return r.status_code, r.json(), None
        except ValueError:
            return r.status_code, None, "non-JSON response"


@router.post("/pause")
async def pause(request: Request):
    body = await request.json() if request.headers.get("content-length") else {}
    note = body.get("reason", "ops-tab manual pause")
    code, payload, err = await _freqtrade_post("/api/v1/stop")
    if err or code >= 400:
        raise HTTPException(status_code=code if code >= 400 else 502,
                            detail=err or f"freqtrade {code}: {payload}")
    return _envelope("ok", data={"freqtrade_response": payload, "reason": note})


@router.post("/resume")
async def resume(request: Request):
    body = await request.json() if request.headers.get("content-length") else {}
    if not body.get("confirm"):
        raise HTTPException(status_code=400, detail="confirm=true required")

    # Pre-flight: refuse if drawdown > 6% or circuit breaker active.
    loop = asyncio.get_running_loop()
    risk = await loop.run_in_executor(None, ops_db.trades_risk_summary)
    dd = risk.get("drawdown_pct_30d") or 0
    if dd < -6.0:
        raise HTTPException(status_code=409, detail=f"resume refused: 30d max drawdown {dd:.1f}% (limit -6%)")
    if risk.get("circuit_breaker", {}).get("active"):
        raise HTTPException(status_code=409, detail="resume refused: circuit breaker active")

    code, payload, err = await _freqtrade_post("/api/v1/start")
    if err or code >= 400:
        raise HTTPException(status_code=code if code >= 400 else 502,
                            detail=err or f"freqtrade {code}: {payload}")
    return _envelope("ok", data={"freqtrade_response": payload, "reason": body.get("reason", "ops-tab manual resume")})
