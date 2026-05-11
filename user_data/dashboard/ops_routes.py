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
import json
import logging
import os
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any

import httpx
from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from . import mcp_local, ops_db, ops_probes
from .data_sources import _ensure_jwt, _invalidate_jwt, fetch_freqtrade_candles, ft_authed_get
# NOTE: removed `from .stocks_sentiment import StocksSentimentFetcher` —
# the placeholder pipeline was redundant. Per-symbol sentiment is already
# produced by Shark's analyst_bull/analyst_bear/debate_orchestrator using
# the existing PERPLEXITY_API_KEY. The SharkBriefingLive card on /ops_spa
# (data-num 13c) surfaces those decisions; no separate sentiment scaffold
# needed. File stocks_sentiment.py kept on disk for git history but no
# longer imported.

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
# Auth dependency for mutating ops endpoints (P0-A through P0-E)
# --------------------------------------------------------------------------
#
# Same key the hermes-mcp server uses for its mutating tools — pause, resume,
# regime_config, rebalance, and the local MCP shim's mutating tools. Read
# endpoints stay open (the dashboard is on the LAN; read-only data is fine).
#
# Header format: ``Authorization: Bearer <HERMES_MCP_KEY>``.  Mirrors the
# hermes-mcp/server.py convention so a single key gates every mutation path.
#
# Behaviour:
#   - HERMES_MCP_KEY unset in the dashboard container env → 503 with a clear
#     "auth not configured" message. Refuse rather than silently allow.
#   - Header missing or doesn't match → 401.
#
# Tests / dev: set HERMES_MCP_KEY=<some-secret> in .env and pass
# ``Authorization: Bearer <some-secret>`` from the Ops UI's fetch calls.

_DASHBOARD_MCP_KEY = os.environ.get("HERMES_MCP_KEY", "").strip()


def _client_host_is_trusted(client_host: str) -> bool:
    """Trusted peer for the same-origin exemption.

    Loopback is the obvious case (process bound to 127.0.0.1, peer is 127.0.0.1).
    Docker port-forwarding is the non-obvious case: the host binds the
    dashboard to 127.0.0.1:8081 (P0-V), but inside the container the
    connection's peer is the docker bridge gateway — e.g. 172.19.0.1. The
    container only sees that traffic because P0-V's host-side bind already
    refused anything that wasn't 127.0.0.1 on the host. So in practice,
    "loopback OR private RFC1918" is the right trust boundary.

    NOT trusted: public addresses. If the dashboard ever sits behind a
    reverse proxy on a public interface, the proxy's connection-source
    won't match RFC1918 and we'll correctly require the Bearer token.
    """
    if not client_host:
        return False
    if client_host in ("127.0.0.1", "::1", "::ffff:127.0.0.1"):
        return True
    try:
        import ipaddress
        addr_str = client_host[7:] if client_host.startswith("::ffff:") else client_host
        addr = ipaddress.ip_address(addr_str)
        return addr.is_private and not addr.is_link_local
    except (ValueError, ImportError):
        return False


def require_mcp_key(
    request: Request,
    authorization: str | None = Header(default=None),
) -> None:
    """FastAPI dependency: gate mutating endpoints behind the shared MCP key.

    Same-origin exemption: browser POSTs from the dashboard's own UI carry an
    Origin/Referer matching the host. Since the dashboard binds 127.0.0.1 (P0-V),
    same-origin == local operator clicking the UI — allowed without bearer.
    External Hermes callers (different origin) must still send Authorization.

    Defense-in-depth: also require the TCP peer to be loopback OR a private
    RFC1918 address (covers docker bridge port-forwarding). Public peers
    must always present a Bearer token — that's the reverse-proxy guard.
    See docs/HERMES_GATEWAY_RUNBOOK.md.

    Raises 503 if the key isn't configured (refuse-by-default), 401 if the
    caller's Bearer token doesn't match. Returns None on success.
    """
    client_host = request.client.host if request.client else ""
    origin = request.headers.get("origin") or request.headers.get("referer") or ""
    host_header = request.headers.get("host") or ""
    if host_header and origin and _client_host_is_trusted(client_host):
        try:
            from urllib.parse import urlsplit
            origin_host = urlsplit(origin).netloc or origin
            if origin_host == host_header:
                return
        except Exception:
            pass

    if not _DASHBOARD_MCP_KEY:
        raise HTTPException(
            status_code=503,
            detail="MCP authentication not configured. "
                   "Set HERMES_MCP_KEY in .env to enable mutating ops endpoints.",
        )
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(
            status_code=401,
            detail="missing Authorization: Bearer <HERMES_MCP_KEY> header",
        )
    presented = authorization.split(" ", 1)[1].strip()
    import hmac
    if not hmac.compare_digest(presented, _DASHBOARD_MCP_KEY):
        raise HTTPException(status_code=401, detail="invalid MCP key")


# --------------------------------------------------------------------------
# /ops — HTML page
# --------------------------------------------------------------------------


# Mounted at app level so / and /ops live side-by-side; the router's prefix
# is ``/api/ops`` so this view function is registered separately.
def make_html_route(app):
    @app.get("/ops", response_class=HTMLResponse, name="ops_page")
    async def ops_page(request: Request) -> HTMLResponse:
        return templates.TemplateResponse(request, "ops_spa.html", {})
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
# /api/ops/uptime — real freqtrade + dashboard uptime for the SPA topbar
# --------------------------------------------------------------------------
# Replaces the hardcoded "14d 06:42:18" mock that previously sat in the
# topbar. Reads freqtrade's first "Bot heartbeat" log line for its actual
# start time; dashboard tracks its own start via _DASHBOARD_START_TS at
# module import. Both values are seconds since UTC epoch.

import time as _uptime_time
_DASHBOARD_START_TS = _uptime_time.time()


def _freqtrade_started_at() -> int | None:
    """Approximate freqtrade's startup time as the earliest heartbeat across
    rotated logs.

    The traceback flood (numpy.int64 serialization errors) pushes the real
    startup messages out of even the oldest rotated log. Using the EARLIEST
    "Bot heartbeat" line across freqtrade.log + freqtrade.log.1..N gives us
    a tight lower bound on actual startup (heartbeats fire every 60s after
    boot, so we under-report by at most ~60s — acceptable for a topbar pill).

    Returns Unix timestamp (seconds, UTC) or None if unreadable.
    """
    from datetime import datetime, timezone
    log_dir = Path("/freqtrade/user_data/logs")
    if not log_dir.exists():
        return None
    # Sort by numeric suffix DESC so oldest rotation is first (the .10 file
    # is older than .1; .log is newest).
    def _sort_key(name: str) -> int:
        suffix = name.split(".")[-1]
        return -int(suffix) if suffix.isdigit() else 0
    files = sorted(
        [f for f in os.listdir(log_dir) if f.startswith("freqtrade.log")],
        key=_sort_key,
    )
    for name in files:
        log = log_dir / name
        try:
            with log.open("r", errors="replace") as f:
                for _ in range(50000):
                    line = f.readline()
                    if not line:
                        break
                    if "Bot heartbeat" in line:
                        try:
                            ts_part = line.split(" - ")[0]
                            dt = datetime.strptime(ts_part, "%Y-%m-%d %H:%M:%S,%f")
                            return int(dt.replace(tzinfo=timezone.utc).timestamp())
                        except Exception:
                            continue
        except Exception as exc:
            logger.debug("uptime: log %s parse failed: %s", log, exc)
    return None


@router.get("/uptime")
async def uptime():
    """Per-service start timestamps + computed uptime seconds.

    Used by the SPA topbar's "BOT UP" / "DASH UP" pills so we stop showing
    page-load time (which restarts every refresh) and instead show real
    freqtrade process age.
    """
    now = int(_uptime_time.time())
    out: dict[str, Any] = {
        "now": now,
        "dashboard": {
            "started_at": int(_DASHBOARD_START_TS),
            "uptime_s": now - int(_DASHBOARD_START_TS),
        },
        "freqtrade": {"started_at": None, "uptime_s": None},
    }
    ft_started = _freqtrade_started_at()
    if ft_started:
        out["freqtrade"] = {
            "started_at": ft_started,
            "uptime_s": now - ft_started,
        }
    return _envelope("ok", data=out)


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

    # Stale check: regime model writes hourly → use 90min as the stale
    # threshold so the card stays green across normal hour transitions.
    ts = latest.get("ts")
    age_s = None
    if ts:
        age_s = (datetime.now(timezone.utc) - ts).total_seconds() if ts.tzinfo else None
    stale = (age_s is not None) and age_s > 90 * 60

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

    # Pull per-model scores so the operator can see WHY the aggregate is 0
    # — disagreement between fast (llama) and deep (claude) zeroes the
    # final score by design. Surface both raw values for diagnostics.
    claude_score = latest.get("claude_score")
    llama_score = latest.get("llama_score")
    key_events = latest.get("key_events") or []

    return _envelope(
        "degraded" if stale else "ok",
        data={
            "score": float(latest.get("sentiment_score") or 0),
            "confidence": float(latest.get("confidence") or 0),
            "agreement": bool(latest.get("agreement")),
            "n_headlines": int(latest.get("n_headlines") or 0),
            "ts": ts.isoformat() if ts else None,
            "age_s": int(age_s) if age_s is not None else None,
            # Per-model breakdown — explains the aggregate
            "deep_score": float(claude_score) if claude_score is not None else None,
            "deep_impact": latest.get("claude_impact"),
            "fast_score": float(llama_score) if llama_score is not None else None,
            "fast_impact": latest.get("llama_impact"),
            # Side-channel signals
            "fear_greed": latest.get("fear_greed_value"),
            "fear_greed_label": latest.get("fear_greed_classification"),
            "community_score": float(latest["community_score_avg"]) if latest.get("community_score_avg") is not None else None,
            "key_events": list(key_events)[:5] if isinstance(key_events, list) else [],
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
# /api/ops/stocks_sentiment — REMOVED 2026-05-11
# --------------------------------------------------------------------------
# This endpoint shipped a placeholder Perplexity scaffold (commit d957e5c).
# Operator clarified: PERPLEXITY_API_KEY is already wired into Shark's
# analyst_bull / analyst_bear / debate_orchestrator pipeline. Per-symbol
# sentiment is produced there and surfaced via /api/ops/shark_briefing
# (commit 023e907). Two endpoints for the same thing was confusing.
# Endpoint deleted; SharkBriefingLive card on /ops_spa is the source of truth.


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
                # ft_authed_get handles 401 → re-login → retry-once so a
                # freqtrade restart no longer floods the dashboard with 401s
                # until the cached JWT's 9-min TTL expires.
                r = await ft_authed_get(client, "/api/v1/status", timeout=ENDPOINT_TIMEOUT_S)
                if r is None:
                    return {"status": None, "open_trades": [], "error": "freqtrade auth failed"}
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
    """POST to a freqtrade endpoint with auto re-auth on 401.

    Pause/resume calls used to spend the rest of a 9-min TTL returning 401s
    after a freqtrade restart — now we invalidate-and-retry-once exactly like
    ft_authed_get does for the read side.
    """
    async with httpx.AsyncClient(timeout=ENDPOINT_TIMEOUT_S) as client:
        token = await _ensure_jwt(client)
        if token is None:
            return 401, None, "freqtrade auth failed"
        url = f"{FREQTRADE_API_URL}{endpoint}"
        headers = {"Authorization": f"Bearer {token}"}
        r = await client.post(url, headers=headers)
        if r.status_code == 401:
            logger.info("freqtrade POST %s 401 with cached JWT — refreshing token", endpoint)
            _invalidate_jwt()
            token = await _ensure_jwt(client, force_refresh=True)
            if token is None:
                return 401, None, "freqtrade auth failed (post-refresh)"
            headers = {"Authorization": f"Bearer {token}"}
            r = await client.post(url, headers=headers)
        try:
            return r.status_code, r.json(), None
        except ValueError:
            return r.status_code, None, "non-JSON response"


@router.post("/pause", dependencies=[Depends(require_mcp_key)])
async def pause(request: Request):
    body = await request.json() if request.headers.get("content-length") else {}
    note = body.get("reason", "ops-tab manual pause")
    code, payload, err = await _freqtrade_post("/api/v1/stop")
    if err or code >= 400:
        raise HTTPException(status_code=code if code >= 400 else 502,
                            detail=err or f"freqtrade {code}: {payload}")
    return _envelope("ok", data={"freqtrade_response": payload, "reason": note})


# --------------------------------------------------------------------------
# /api/ops/sparklines — per-pair last-N close prices for tiny inline charts
# --------------------------------------------------------------------------

DEFAULT_PAIRS = [p.strip() for p in os.environ.get(
    "DASHBOARD_PAIRS", "BTC/USD,ETH/USD,SOL/USD,ADA/USD",
).split(",") if p.strip()]


@router.get("/sparklines")
async def sparklines(timeframe: str = "5m", limit: int = 60):
    """Per-pair compact close-price arrays + 24h % change.

    Used by the Ops trades panel to render small inline price sparklines.
    Reuses freqtrade's /api/v1/pair_candles so we don't add a second data
    pipe. ``limit`` capped at 200 so payloads stay small.
    """
    limit = max(10, min(200, int(limit)))
    if timeframe not in ("1m", "5m", "15m", "1h", "6h"):
        timeframe = "5m"

    async def _one(pair: str):
        df = await fetch_freqtrade_candles(pair, timeframe=timeframe, limit=limit)
        if df is None or df.empty:
            return pair, {"closes": [], "current": None, "pct_24h": None}
        closes = [float(x) for x in df["close"].tolist()]
        current = closes[-1]
        # 24h % change: walk back enough candles for 24h
        per_day = {"1m": 1440, "5m": 288, "15m": 96, "1h": 24, "6h": 4}.get(timeframe, 288)
        ref_idx = max(0, len(closes) - per_day - 1)
        ref = closes[ref_idx] if ref_idx < len(closes) else closes[0]
        pct = ((current - ref) / ref * 100.0) if ref else None
        return pair, {"closes": closes, "current": current, "pct_24h": pct}

    try:
        results = await asyncio.wait_for(
            asyncio.gather(*[_one(p) for p in DEFAULT_PAIRS], return_exceptions=False),
            timeout=ENDPOINT_TIMEOUT_S,
        )
    except asyncio.TimeoutError:
        return _envelope("down", error="sparklines fetch timed out")
    except Exception as exc:
        logger.exception("sparklines failed")
        return _envelope("down", error=str(exc))

    data = {pair: payload for pair, payload in results}

    # Improvement 3 — also surface 24h regime band + sentiment line. These
    # are shared across pairs (the HMM + Perplexity-fetched sentiment are
    # global, not per-pair), so one DB roundtrip serves all the cards.
    timeline_24h = {"regimes": [], "sentiment": []}
    try:
        loop = asyncio.get_running_loop()

        def _read_timeline():
            if not ops_db._HAVE_PG:
                return [], []
            with ops_db._connect() as conn, conn.cursor() as cur:
                cur.execute(
                    "SELECT ts, regime, probability FROM regime_log "
                    "WHERE ts > NOW() - INTERVAL '24 hours' ORDER BY ts"
                )
                regs = [
                    {"ts": r["ts"].isoformat(), "regime": r["regime"],
                     "probability": float(r["probability"] or 0)}
                    for r in cur.fetchall()
                ]
                cur.execute(
                    "SELECT ts, sentiment_score, confidence FROM sentiment_log "
                    "WHERE ts > NOW() - INTERVAL '24 hours' ORDER BY ts"
                )
                sents = [
                    {"ts": r["ts"].isoformat(),
                     "score": float(r["sentiment_score"] or 0),
                     "confidence": float(r["confidence"] or 0)}
                    for r in cur.fetchall()
                ]
                return regs, sents

        regs, sents = await asyncio.wait_for(
            loop.run_in_executor(None, _read_timeline), timeout=ENDPOINT_TIMEOUT_S,
        )
        timeline_24h["regimes"] = regs
        timeline_24h["sentiment"] = sents
    except Exception:
        logger.exception("sparklines timeline read failed")

    has_any = any(p.get("closes") for p in data.values())
    return _envelope(
        "ok" if has_any else "degraded",
        data={
            "pairs": data,
            "timeframe": timeframe,
            "limit": limit,
            "timeline_24h": timeline_24h,
        },
        error=None if has_any else "freqtrade returned no candle data",
    )


# --------------------------------------------------------------------------
# Regime params editor: GET / POST /api/ops/regime_config
# --------------------------------------------------------------------------

# Param-name → (min, max) sanity range. Anything outside is rejected.
# Conservative ranges; tune by editing config.json directly if you need wider.
_REGIMES = ("trending_up", "trending_down", "mean_reverting", "high_volatility", "unknown")
_DELTA_RANGE = (-0.5, 0.5)   # entry_delta / exit_delta per regime
_RANGES = {
    "high_vol_stake_factor":      (0.0, 1.0),
    "high_vol_min_confidence":    (0.0, 1.0),
    "mean_rev_take_profit":       (0.0, 0.10),
    "trending_up_trail_trigger":  (0.0, 0.10),
    "trending_up_trail_distance": (-0.10, 0.0),
    "tft_min_confidence":         (0.0, 1.0),
    "meta_min_confidence":        (0.0, 1.0),
}

CONFIG_PATH = Path(os.environ.get(
    "FREQTRADE_CONFIG_PATH",
    "/freqtrade/user_data/config.json",
))

# Same root the strategy uses; we drop config-backup-*.json snapshots here.
USER_DATA_ROOT_FOR_BACKUPS = Path(os.environ.get(
    "USER_DATA_ROOT",
    "/freqtrade/user_data",
))


@router.get("/regime_config")
def regime_config_get():
    """Return the current regime_gating block + the schema (ranges) for the UI."""
    try:
        import json
        cfg = json.loads(CONFIG_PATH.read_text())
        rg = cfg.get("regime_gating") or {}
    except Exception as exc:
        return _envelope("down", error=str(exc))

    return _envelope("ok", data={
        "regime_gating": {k: v for k, v in rg.items() if not k.startswith("_")},
        "schema": {
            "regimes": list(_REGIMES),
            "delta_range": list(_DELTA_RANGE),
            "scalar_ranges": _RANGES,
        },
        "config_path": str(CONFIG_PATH),
    })


@router.post("/regime_config", dependencies=[Depends(require_mcp_key)])
async def regime_config_post(request: Request):
    """Validate + atomically write the new regime_gating block.

    Body must be ``{"regime_gating": {...}}`` matching the existing shape.
    We:
      1. Accept only known keys; reject extras.
      2. Validate each value against its sanity range.
      3. Snapshot the old config to ``user_data/data/config-backup-<ts>.json``.
      4. Atomic-write the new config (tmp + rename).
      5. Best-effort POST freqtrade ``/api/v1/reload_config`` so it picks up.

    Returns the diff in the envelope so the frontend can confirm.
    """
    import json
    body = await request.json() if request.headers.get("content-length") else {}
    new_rg = body.get("regime_gating")
    if not isinstance(new_rg, dict):
        raise HTTPException(status_code=400, detail="body.regime_gating must be a dict")

    # Load current config + the existing regime_gating
    try:
        cfg_text = CONFIG_PATH.read_text()
        cfg = json.loads(cfg_text)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"could not read {CONFIG_PATH}: {exc}")
    current = cfg.get("regime_gating") or {}

    # Validate every submitted key/value against the known shape.
    diffs: list[str] = []
    for key, value in new_rg.items():
        if key.startswith("_"):
            continue  # never overwrite documentation keys
        if key in ("entry_delta", "exit_delta"):
            if not isinstance(value, dict):
                raise HTTPException(status_code=400, detail=f"{key} must be a dict")
            for r, v in value.items():
                if r not in _REGIMES:
                    raise HTTPException(status_code=400, detail=f"{key}.{r}: unknown regime")
                if v is None:
                    continue  # null = hard-block long entries; allowed
                if not isinstance(v, (int, float)):
                    raise HTTPException(status_code=400, detail=f"{key}.{r}: must be a number or null")
                lo, hi = _DELTA_RANGE
                if not (lo <= v <= hi):
                    raise HTTPException(status_code=400, detail=f"{key}.{r}={v} outside allowed range [{lo}, {hi}]")
                old = (current.get(key) or {}).get(r)
                if old != v:
                    diffs.append(f"{key}.{r}: {old} → {v}")
        elif key in _RANGES:
            if not isinstance(value, (int, float)):
                raise HTTPException(status_code=400, detail=f"{key} must be a number")
            lo, hi = _RANGES[key]
            if not (lo <= value <= hi):
                raise HTTPException(status_code=400, detail=f"{key}={value} outside allowed range [{lo}, {hi}]")
            old = current.get(key)
            if old != value:
                diffs.append(f"{key}: {old} → {value}")
        else:
            raise HTTPException(status_code=400, detail=f"unknown param: {key}")

    if not diffs:
        return _envelope("ok", data={"changes": [], "note": "no-op (values unchanged)"})

    # Build the new config (preserve "_doc" and any extras we didn't touch)
    merged = dict(current)
    for k, v in new_rg.items():
        if k in ("entry_delta", "exit_delta"):
            base = dict(current.get(k) or {})
            base.update(v)
            merged[k] = base
        else:
            merged[k] = v
    cfg["regime_gating"] = merged

    # Snapshot the previous config so the operator can roll back.
    try:
        from datetime import datetime as _dt
        backup_dir = USER_DATA_ROOT_FOR_BACKUPS / "data"
        backup_dir.mkdir(parents=True, exist_ok=True)
        stamp = _dt.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%SZ")
        backup_path = backup_dir / f"config-backup-{stamp}.json"
        backup_path.write_text(cfg_text)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"could not snapshot existing config: {exc}")

    # Atomic write: tmp file + rename (same fs guaranteed since it's the same dir).
    try:
        tmp = CONFIG_PATH.with_suffix(CONFIG_PATH.suffix + ".tmp")
        tmp.write_text(json.dumps(cfg, indent=4))
        tmp.replace(CONFIG_PATH)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"atomic write failed: {exc}")

    # Best-effort freqtrade reload.
    reload_status = None
    try:
        async with httpx.AsyncClient(timeout=ENDPOINT_TIMEOUT_S) as client:
            token = await _ensure_jwt(client)
            if token:
                r = await client.post(f"{FREQTRADE_API_URL}/api/v1/reload_config",
                                      headers={"Authorization": f"Bearer {token}"})
                reload_status = r.status_code
    except Exception as exc:
        reload_status = f"error: {exc}"

    return _envelope("ok", data={
        "changes": diffs,
        "backup": str(backup_path),
        "freqtrade_reload": reload_status,
        "note": "Some params (entry/exit deltas) take effect on the next candle. "
                "Trail distance / take-profit affect new positions only.",
    })


# --------------------------------------------------------------------------
# Pause / Resume (continued)
# --------------------------------------------------------------------------


@router.post("/resume", dependencies=[Depends(require_mcp_key)])
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

    # P0-H: clear the risk-governor drawdown-pause flag in the persisted
    # anchor file so the strategy picks up the manual resume next tick. The
    # in-memory flag in the freqtrade container will reset to the persisted
    # value via _load_anchors on the next governor restart; for a hot resume
    # we patch the on-disk state directly here.
    anchor_cleared = False
    try:
        anchor_path = Path(os.environ.get(
            "RISK_GOVERNOR_ANCHORS_PATH",
            str(USER_DATA_ROOT_FOR_BACKUPS / "state" / "risk_governor_anchors.json"),
        ))
        if anchor_path.exists():
            data = json.loads(anchor_path.read_text())
            if data.get("paused_for_drawdown"):
                data["paused_for_drawdown"] = False
                data["manual_resume_at"] = datetime.now(timezone.utc).isoformat()
                tmp = anchor_path.with_suffix(anchor_path.suffix + ".tmp")
                tmp.write_text(json.dumps(data, indent=2))
                tmp.replace(anchor_path)
                anchor_cleared = True
    except Exception as exc:
        logger.warning("resume: could not clear risk_governor anchor: %s", exc)

    return _envelope("ok", data={
        "freqtrade_response": payload,
        "reason": body.get("reason", "ops-tab manual resume"),
        "drawdown_pause_cleared": anchor_cleared,
    })


# --------------------------------------------------------------------------
# /api/ops/config — surface live config + relevant env vars to the dashboard
# --------------------------------------------------------------------------
#
# One place to read every knob the bot is currently using. Helpful for new
# operators who want to know "what's actually configured right now?" without
# poking around config.json or env files. Sensitive values are redacted.


# Env vars worth surfacing — operator visibility, not "everything in os.environ".
_VISIBLE_ENV_VARS = (
    # Sentiment / news pipeline
    "SENTIMENT_POLL_INTERVAL_S", "SENTIMENT_HISTORY_DAYS", "SENTIMENT_MAX_HEADLINES_TO_LLM",
    "OLLAMA_MODEL_FAST", "OLLAMA_MODEL_DEEP", "OLLAMA_HOST",
    "PERPLEXITY_MODEL", "PERPLEXITY_RECENCY",
    # Dashboard
    "DASHBOARD_PAIRS", "DASHBOARD_TIMEFRAME", "DASHBOARD_WS_INTERVAL_SEC",
    # Freqtrade
    "FREQTRADE_API_URL", "FREQTRADE_API_USER",
    # MCP
    "HERMES_MCP_KEY", "HERMES_MCP_PORT", "HERMES_MCP_TRANSPORT",
    # Postgres
    "POSTGRES_HOST", "POSTGRES_PORT", "POSTGRES_USER", "POSTGRES_DB",
    # Slack / Telegram (presence-only — never echo the value)
    "SLACK_WEBHOOK_URL", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID",
)

# These keys, if present in env, get reported as "<set>" / "<unset>" — never the value.
_SECRET_ENV_VARS = {
    "HERMES_MCP_KEY", "SLACK_WEBHOOK_URL", "TELEGRAM_BOT_TOKEN",
    "FREQTRADE_API_PASS", "PERPLEXITY_API_KEY", "POSTGRES_PASSWORD",
    "COINBASE_API_KEY", "COINBASE_API_SECRET",
}


@router.get("/config")
async def config_overview():
    """Live config + env-var presence map. Sensitive values are redacted."""
    try:
        cfg = json.loads(CONFIG_PATH.read_text())
    except Exception as exc:
        return _envelope("down", error=f"could not read config.json: {exc}")

    # Pluck the most-asked-about blocks for prominence
    summary = {
        "trading": {
            "dry_run":                 cfg.get("dry_run"),
            "dry_run_wallet":          cfg.get("dry_run_wallet"),
            "tradable_balance_ratio":  cfg.get("tradable_balance_ratio"),
            "max_open_trades":         cfg.get("max_open_trades"),
            "stake_currency":          cfg.get("stake_currency"),
            "timeframe":               cfg.get("timeframe"),
            "minimal_roi":             cfg.get("minimal_roi"),
            "stoploss":                cfg.get("stoploss"),
        },
        "pairs": {
            "whitelist":  ((cfg.get("exchange") or {}).get("pair_whitelist") or []),
            "blacklist":  ((cfg.get("exchange") or {}).get("pair_blacklist") or []),
        },
        "capital_allocation": cfg.get("capital_allocation"),
        "regime_gating":      cfg.get("regime_gating"),
        "risk_management":    cfg.get("risk_management"),
        "ept_evolution":      cfg.get("ept_evolution"),
        "sentiment_sources":  cfg.get("sentiment_sources"),
        "sentiment_pipeline": cfg.get("sentiment_pipeline"),
        "news_sources_config": cfg.get("news_sources_config"),
    }

    # Env presence map (no secret values leak)
    env_view: dict[str, Any] = {}
    for key in _VISIBLE_ENV_VARS:
        raw = os.environ.get(key, "")
        if not raw:
            env_view[key] = None
            continue
        if key in _SECRET_ENV_VARS:
            env_view[key] = "<set>"
        else:
            env_view[key] = raw

    return _envelope("ok", data={
        "config": summary,
        "env": env_view,
        "config_path": str(CONFIG_PATH),
        "_doc": "Live view of config.json + selected env vars. Edit config "
                "via the regime-params editor or by committing config.json. "
                "Secret env vars are reported as '<set>' / null only.",
    })


# --------------------------------------------------------------------------
# /api/ops/readiness — go-live validation gate (UI button on Ops dashboard)
# --------------------------------------------------------------------------
#
# Mirrors scripts/validate_readiness.py — same checks, same thresholds. Lets
# the operator click a button instead of shelling out, and surfaces the
# pass/fail-per-criterion result in the same Quick-Actions result card.

# Mode → (sharpe_min, dd_max, pf_min, wr_min, trades_min, window_days)
_READINESS_MODES: dict[str, tuple[float, float, float, float, int, int | None]] = {
    "standard":   (1.5, 0.12, 1.4, 0.55, 200, None),
    "fast_track": (1.2, 0.08, 1.5, 0.55,  80, 7),
}


def _evaluate_readiness_inline(mode: str = "standard") -> dict:
    """Compute the readiness report from trade_journal.

    Mirrors scripts/validate_readiness.py's ``evaluate_readiness`` so the UI
    button gives the same answer as the CLI. Returns a dict ready for the
    Quick-Actions card.
    """
    if mode not in _READINESS_MODES:
        return {"error": f"unknown mode: {mode}"}
    sharpe_min, dd_max, pf_min, wr_min, trades_min, window_days = _READINESS_MODES[mode]

    if not ops_db._HAVE_PG:
        return {"error": "psycopg not installed"}

    import math
    where = "WHERE closed_at IS NOT NULL"
    if window_days:
        where += f" AND closed_at > NOW() - INTERVAL '{int(window_days)} days'"

    with ops_db._connect() as conn, conn.cursor() as cur:
        cur.execute(
            f"SELECT closed_at, pnl, pnl_pct, stake FROM trade_journal {where} "
            "ORDER BY closed_at"
        )
        rows = cur.fetchall()

    n = len(rows)
    if n == 0:
        return {
            "mode": mode,
            "ready": False,
            "n_trades": 0,
            "checks": [],
            "diagnostics": {"reason": "no closed trades in window"},
            "thresholds": {
                "sharpe_min": sharpe_min, "dd_max": dd_max,
                "profit_factor_min": pf_min, "win_rate_min": wr_min,
                "trades_min": trades_min, "window_days": window_days,
            },
        }

    pnls = [float(r["pnl"] or 0) for r in rows]
    pnl_pcts = [float(r["pnl_pct"] or 0) for r in rows]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    wr = len(wins) / n
    pf = (sum(wins) / abs(sum(losses))) if losses else float("inf")

    # Daily P&L pct buckets → annualised Sharpe (× √365)
    daily: dict[str, float] = {}
    for r in rows:
        day = r["closed_at"].astimezone(timezone.utc).strftime("%Y-%m-%d")
        daily[day] = daily.get(day, 0.0) + float(r["pnl_pct"] or 0)
    daily_pcts = list(daily.values())
    if len(daily_pcts) >= 2:
        mean = sum(daily_pcts) / len(daily_pcts)
        var = sum((x - mean) ** 2 for x in daily_pcts) / (len(daily_pcts) - 1)
        sd = math.sqrt(var)
        sharpe = (mean / sd * math.sqrt(365)) if sd > 0 else 0.0
    else:
        sharpe = 0.0

    # Max drawdown on cumulative quote PnL
    cum, peak, max_dd_quote = 0.0, 0.0, 0.0
    avg_stake = sum(float(r.get("stake") or 0.0) for r in rows) / n
    for p in pnls:
        cum += p
        peak = max(peak, cum)
        dd = (peak - cum) / max(peak, avg_stake or 1.0)
        max_dd_quote = max(max_dd_quote, dd)

    checks = [
        {"name": "sharpe",        "value": round(sharpe, 4),       "threshold": sharpe_min,  "op": ">",  "passed": sharpe > sharpe_min},
        {"name": "max_drawdown",  "value": round(max_dd_quote, 4), "threshold": dd_max,      "op": "<",  "passed": max_dd_quote < dd_max},
        {"name": "profit_factor", "value": round(pf, 4) if pf != float("inf") else None, "threshold": pf_min, "op": ">", "passed": pf > pf_min},
        {"name": "win_rate",      "value": round(wr, 4),           "threshold": wr_min,      "op": ">",  "passed": wr > wr_min},
        {"name": "total_trades",  "value": n,                      "threshold": trades_min,  "op": ">=", "passed": n >= trades_min},
    ]
    return {
        "mode": mode,
        "ready": all(c["passed"] for c in checks),
        "n_trades": n,
        "checks": checks,
        "diagnostics": {
            "daily_buckets": len(daily_pcts),
            "starting_equity_proxy": round(avg_stake, 2),
            "window_days": window_days,
        },
        "thresholds": {
            "sharpe_min": sharpe_min, "dd_max": dd_max,
            "profit_factor_min": pf_min, "win_rate_min": wr_min,
            "trades_min": trades_min, "window_days": window_days,
        },
    }


@router.get("/readiness")
async def readiness(fast_track: bool = False):
    mode = "fast_track" if fast_track else "standard"
    try:
        loop = asyncio.get_running_loop()
        report = await asyncio.wait_for(
            loop.run_in_executor(None, _evaluate_readiness_inline, mode),
            timeout=ENDPOINT_TIMEOUT_S * 3,
        )
    except asyncio.TimeoutError:
        return _envelope("down", error="readiness query timed out")
    except Exception as exc:
        logger.exception("readiness failed")
        return _envelope("down", error=str(exc))
    if "error" in report:
        return _envelope("degraded", data=report, error=report["error"])
    return _envelope("ok", data=report)


# --------------------------------------------------------------------------
# /api/ops/rebalance — capital-allocation rebalance (UI button on Ops)
# --------------------------------------------------------------------------
#
# Two-step UX:
#   GET  /api/ops/rebalance                  → dry-run preview (default)
#   POST /api/ops/rebalance with {confirm:true} → atomic-write the new weights


def _compute_rebalance(
    *,
    window_days: int = 14,
    max_weight: float = 0.50,
    min_weight: float = 0.05,
) -> dict:
    """Pure-function rebalance computation. Mirrors scripts/rebalance_capital.py.

    Returns a dict with current/proposed weights + the delta. Doesn't write
    anything — caller decides whether to commit.
    """
    import math

    cfg_text = CONFIG_PATH.read_text()
    cfg = json.loads(cfg_text)
    alloc = cfg.get("capital_allocation") or {}
    current = dict(alloc.get("pair_weights") or {})
    if not current:
        return {"error": "no capital_allocation.pair_weights in config.json"}
    floor = float(alloc.get("min_sharpe_for_trading", 0.0))

    # Compute rolling-Sharpe per pair (live, last `window_days` days)
    if not ops_db._HAVE_PG:
        return {"error": "psycopg not installed"}
    with ops_db._connect() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT pair, closed_at, pnl_pct FROM trade_journal "
            "WHERE closed_at IS NOT NULL AND closed_at > NOW() - (%s || ' days')::interval "
            "ORDER BY pair, closed_at",
            (str(window_days),),
        )
        rows = cur.fetchall()

    daily: dict[str, dict[str, float]] = {}
    for r in rows:
        pair = r["pair"]
        day = r["closed_at"].strftime("%Y-%m-%d")
        d = daily.setdefault(pair, {})
        d[day] = d.get(day, 0.0) + float(r["pnl_pct"] or 0.0)

    sharpes: dict[str, float] = {}
    for pair, dmap in daily.items():
        pcts = list(dmap.values())
        if len(pcts) < 2:
            continue
        mean = sum(pcts) / len(pcts)
        var = sum((x - mean) ** 2 for x in pcts) / (len(pcts) - 1)
        sd = math.sqrt(var)
        if sd > 0:
            sharpes[pair] = (mean / sd) * math.sqrt(365)

    eligible = {p: max(0.0, s) for p, s in sharpes.items()
                if s >= floor and p in current}

    if not eligible:
        # No live data → keep existing allocation untouched
        new = dict(current)
    else:
        total = sum(eligible.values()) or 1.0
        new = {p: (eligible[p] / total) if p in eligible else 0.0 for p in current}
        # Per-pair max cap
        capped = {p: min(w, max_weight) for p, w in new.items()}
        overflow = 1.0 - sum(capped.values())
        if overflow > 0.001:
            uncapped = {p: w for p, w in capped.items() if w < max_weight and p in eligible}
            uc_total = sum(uncapped.values())
            for p in uncapped:
                bonus = overflow * (uncapped[p] / uc_total) if uc_total > 0 else 0
                capped[p] = min(max_weight, capped[p] + bonus)
        # Per-pair min floor for tradeable pairs
        for p in eligible:
            if 0 < capped[p] < min_weight:
                capped[p] = min_weight
        # Final normalise so total ≤ 1.0
        total2 = sum(capped.values())
        if total2 > 1.0:
            capped = {p: w / total2 for p, w in capped.items()}
        new = {p: round(w, 4) for p, w in capped.items()}

    diffs = []
    for p in sorted(set(current) | set(new)):
        old = current.get(p, 0.0)
        n = new.get(p, 0.0)
        if abs(old - n) > 1e-4:
            diffs.append({
                "pair": p,
                "from": round(old, 4),
                "to": round(n, 4),
                "sharpe": round(sharpes.get(p), 4) if sharpes.get(p) is not None else None,
            })

    return {
        "window_days": window_days,
        "min_sharpe_for_trading": floor,
        "max_weight": max_weight,
        "min_weight": min_weight,
        "sharpes": {p: round(s, 4) for p, s in sharpes.items()},
        "current_weights": current,
        "proposed_weights": new,
        "changes": diffs,
        "n_changes": len(diffs),
    }


@router.get("/rebalance")
async def rebalance_dryrun(window: int = 14):
    """Dry-run: compute proposed new pair_weights but don't write."""
    try:
        loop = asyncio.get_running_loop()
        result = await asyncio.wait_for(
            loop.run_in_executor(None, lambda: _compute_rebalance(window_days=window)),
            timeout=ENDPOINT_TIMEOUT_S * 3,
        )
    except asyncio.TimeoutError:
        return _envelope("down", error="rebalance preview timed out")
    except Exception as exc:
        logger.exception("rebalance preview failed")
        return _envelope("down", error=str(exc))
    if "error" in result:
        return _envelope("degraded", data=result, error=result["error"])
    return _envelope("ok", data=result)


@router.post("/rebalance", dependencies=[Depends(require_mcp_key)])
async def rebalance_apply(request: Request):
    body: dict = {}
    if request.headers.get("content-length"):
        try:
            body = await request.json()
        except Exception:
            body = {}
    if not body.get("confirm"):
        raise HTTPException(status_code=400, detail="confirm=true required to apply")
    window = int(body.get("window", 14))

    loop = asyncio.get_running_loop()
    try:
        result = await loop.run_in_executor(None, lambda: _compute_rebalance(window_days=window))
    except Exception as exc:
        logger.exception("rebalance compute failed")
        raise HTTPException(status_code=500, detail=str(exc))
    if "error" in result:
        raise HTTPException(status_code=400, detail=result["error"])
    if result["n_changes"] == 0:
        return _envelope("ok", data={**result, "applied": False, "note": "no-op (current weights stable)"})

    # Snapshot + atomic-write
    cfg_text = CONFIG_PATH.read_text()
    cfg = json.loads(cfg_text)
    backup_dir = USER_DATA_ROOT_FOR_BACKUPS / "data"
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%SZ")
    backup_path = backup_dir / f"config-backup-{stamp}-rebalance-ui.json"
    backup_path.write_text(cfg_text)
    cfg["capital_allocation"]["pair_weights"] = result["proposed_weights"]
    tmp = CONFIG_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(cfg, indent=4))
    tmp.replace(CONFIG_PATH)

    return _envelope("ok", data={**result, "applied": True, "backup": str(backup_path),
                                 "note": "freqtrade picks up new weights within 1h via "
                                         "bot_loop_start config re-read; no restart needed"})


# --------------------------------------------------------------------------
# /api/mcp/tools and /api/mcp/{tool_name} — surface MCP-tool calls to the UI
# --------------------------------------------------------------------------
#
# The dashboard container can't reach the host's hermes-mcp on :8089 (the
# host firewall blocks docker-bridge → host:8089). So instead of an HTTP
# proxy, this is a local shim that runs the same tool implementations
# in-process. Same audit log path, same result shapes.


@router.get("/tools", include_in_schema=True)
@router.get("/mcp/tools", include_in_schema=False)  # alias under /api/mcp/* for nicer paths
async def mcp_tools():
    return _envelope("ok", data={"tools": mcp_local.schema()})


@router.post("/mcp/{tool_name}")
async def mcp_call(
    tool_name: str,
    request: Request,
    authorization: str | None = Header(default=None),
):
    body: dict = {}
    if request.headers.get("content-length"):
        try:
            body = await request.json()
        except Exception:
            body = {}
    # P0-E: gate mutating MCP tools at the HTTP layer too, not just inside
    # the tool body (which currently returns a 200 with {"error": ...}).
    # Read-only tools (mutating=False) stay open for the dashboard's pull
    # paths. Unknown tool names fall through to dispatch's error handling.
    tool_meta = mcp_local.TOOLS.get(tool_name)
    if tool_meta and tool_meta.get("mutating"):
        # Reuse the dependency body — raises 401/503 on missing/invalid key.
        require_mcp_key(request=request, authorization=authorization)
    try:
        result = await asyncio.wait_for(
            mcp_local.dispatch(tool_name, body or {}),
            timeout=ENDPOINT_TIMEOUT_S * 5,  # MCP tools can be slow (DB scans, freqtrade RTT)
        )
    except asyncio.TimeoutError:
        return _envelope("down", error=f"{tool_name} timed out")
    except Exception as exc:
        logger.exception("MCP tool %s failed", tool_name)
        return _envelope("down", error=str(exc))
    # Tools return either {"error": "..."} or the actual payload. Surface
    # them through the envelope so the frontend has a stable contract.
    if isinstance(result, dict) and "error" in result and len(result) == 1:
        return _envelope("degraded", data=result, error=result["error"])
    return _envelope("ok", data=result)


# --------------------------------------------------------------------------
# /api/explainability/{pair} — last N decisions for that pair (entered + blocked)
# --------------------------------------------------------------------------


@router.get("/explainability/{base}/{quote}")
async def explainability(base: str, quote: str, limit: int = 5):
    """Decision records for the most recent candle ticks.

    Two sources joined into one timeline:
      - trade_journal rows (full context: TFT/DRL/sentiment/regime + reasoning)
      - parsed lines from freqtrade.log of the form
        ``[strategy] risk-blocked PAIR: REASON (constraint=NAME)``
        for entries the risk governor refused.
    """
    pair = f"{base.upper()}/{quote.upper()}"
    limit = max(1, min(50, int(limit)))

    decisions: list[dict] = []

    # 1) Last `limit` trade-journal entries (entered)
    if ops_db._HAVE_PG:
        try:
            with ops_db._connect() as conn, conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT trade_id, pair, direction, opened_at, closed_at,
                           entry_price, exit_price, pnl, pnl_pct, stake,
                           confidence, tft_probs, drl_votes, sentiment_score,
                           sentiment_conf AS sentiment_confidence,
                           regime, exit_reason, reasoning
                    FROM trade_journal
                    WHERE pair = %s
                    ORDER BY opened_at DESC LIMIT %s
                    """,
                    (pair, limit),
                )
                for r in cur.fetchall():
                    decisions.append({
                        "kind": "entered",
                        "ts": r["opened_at"].isoformat() if r.get("opened_at") else None,
                        "pair": r["pair"],
                        "side": r["direction"],
                        "entry_price": float(r["entry_price"] or 0),
                        "stake": float(r["stake"] or 0),
                        "confidence": float(r["confidence"] or 0),
                        "tft_probs": r.get("tft_probs"),
                        "drl_votes": r.get("drl_votes"),
                        "sentiment_score": float(r["sentiment_score"] or 0) if r.get("sentiment_score") is not None else None,
                        "sentiment_confidence": float(r["sentiment_confidence"] or 0) if r.get("sentiment_confidence") is not None else None,
                        "regime": r.get("regime"),
                        "reasoning": r.get("reasoning"),
                        "closed_at": r["closed_at"].isoformat() if r.get("closed_at") else None,
                        "exit_price": float(r["exit_price"] or 0) if r.get("exit_price") is not None else None,
                        "pnl_pct": float(r["pnl_pct"] or 0) if r.get("pnl_pct") is not None else None,
                        "exit_reason": r.get("exit_reason"),
                    })
        except Exception as exc:
            logger.exception("explainability DB read failed")

    # 2) Last `limit` blocked-entry log lines (didn't fire)
    log_path = USER_DATA_ROOT_FOR_BACKUPS / "logs" / "freqtrade.log"
    if log_path.exists():
        try:
            with log_path.open("rb") as f:
                f.seek(0, 2)
                size = f.tell()
                f.seek(max(0, size - 500_000))
                tail = f.read().decode("utf-8", errors="replace").splitlines()
            import re as _re
            blocked_pat = _re.compile(
                r"(\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}).*"
                r"risk-blocked\s+" + _re.escape(pair) + r":\s*(.+?)\s*\(constraint=([^)]+)\)"
            )
            blocked: list[dict] = []
            for line in reversed(tail):
                m = blocked_pat.search(line)
                if m:
                    blocked.append({
                        "kind": "blocked",
                        "ts": m.group(1),
                        "pair": pair,
                        "reason": m.group(2).strip(),
                        "constraint": m.group(3).strip(),
                    })
                    if len(blocked) >= limit:
                        break
            decisions.extend(blocked)
        except OSError:
            pass

    decisions.sort(key=lambda d: d.get("ts") or "", reverse=True)
    decisions = decisions[:limit]
    return _envelope(
        "ok" if decisions else "degraded",
        data={"pair": pair, "decisions": decisions},
        error=None if decisions else "no entries or blocked-decisions in window",
    )


# --------------------------------------------------------------------------
# /api/timeline/{pair}?hours=24 — candles + regime band + sentiment line
# --------------------------------------------------------------------------


@router.get("/timeline/{base}/{quote}")
async def timeline(base: str, quote: str, hours: int = 24, timeframe: str = "5m"):
    pair = f"{base.upper()}/{quote.upper()}"
    hours = max(1, min(168, int(hours)))
    if timeframe not in ("1m", "5m", "15m", "1h", "6h"):
        timeframe = "5m"

    # ── Candles (close-only is enough for the sparkline) ──
    per_hour = {"1m": 60, "5m": 12, "15m": 4, "1h": 1, "6h": 1}.get(timeframe, 12)
    limit = min(500, hours * per_hour + 5)
    df = await fetch_freqtrade_candles(pair, timeframe=timeframe, limit=limit)
    candles: list[dict] = []
    if df is not None and not df.empty:
        for _, row in df.iterrows():
            ts = row.get("date")
            candles.append({
                "ts": ts.isoformat() if hasattr(ts, "isoformat") else str(ts),
                "close": float(row.get("close", 0) or 0),
            })

    # ── Regime segments + sentiment series ──
    regimes: list[dict] = []
    sentiment: list[dict] = []
    if ops_db._HAVE_PG:
        try:
            with ops_db._connect() as conn, conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT ts, regime, probability
                    FROM regime_log
                    WHERE ts >= NOW() - (%s || ' hours')::interval
                    ORDER BY ts
                    """,
                    (str(hours),),
                )
                regimes = [
                    {"ts": r["ts"].isoformat(), "regime": r["regime"],
                     "probability": float(r["probability"] or 0)}
                    for r in cur.fetchall()
                ]
                cur.execute(
                    """
                    SELECT ts, sentiment_score, market_impact, confidence
                    FROM sentiment_log
                    WHERE ts >= NOW() - (%s || ' hours')::interval
                    ORDER BY ts
                    """,
                    (str(hours),),
                )
                sentiment = [
                    {"ts": r["ts"].isoformat(),
                     "score": float(r["sentiment_score"] or 0),
                     "market_impact": r.get("market_impact"),
                     "confidence": float(r["confidence"] or 0)}
                    for r in cur.fetchall()
                ]
        except Exception:
            logger.exception("timeline DB read failed")

    has_data = bool(candles) and (bool(regimes) or bool(sentiment))
    return _envelope(
        "ok" if has_data else "degraded",
        data={
            "pair": pair, "timeframe": timeframe, "hours": hours,
            "candles": candles, "regimes": regimes, "sentiment": sentiment,
        },
        error=None if has_data else "candles missing or regime/sentiment empty in window",
    )


# --------------------------------------------------------------------------
# /api/slack-preview — what the next daily-P&L Slack report will look like
# --------------------------------------------------------------------------


@router.get("/slack_preview")
async def slack_preview():
    """Live render of today's daily P&L (the same payload the cron will Slack
    at 00:00 UTC). Frontend renders this in a Slack-styled card.
    """
    loop = asyncio.get_running_loop()
    try:
        # Today's stats from trade_journal
        if not ops_db._HAVE_PG:
            return _envelope("degraded", data={}, error="postgres unavailable")
        with ops_db._connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    COALESCE(SUM(pnl), 0)                               AS pnl_usd,
                    COALESCE(SUM(pnl_pct), 0)                           AS pnl_pct,
                    COUNT(*)                                             AS trades,
                    SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END)             AS wins,
                    SUM(CASE WHEN pnl < 0 THEN 1 ELSE 0 END)             AS losses
                FROM trade_journal
                WHERE closed_at IS NOT NULL
                  AND closed_at >= date_trunc('day', NOW() AT TIME ZONE 'UTC')
                """
            )
            today = cur.fetchone() or {}

            # Per-pair P&L for top/bottom
            cur.execute(
                """
                SELECT pair, COUNT(*) AS n, COALESCE(SUM(pnl), 0) AS pnl
                FROM trade_journal
                WHERE closed_at IS NOT NULL
                  AND closed_at >= date_trunc('day', NOW() AT TIME ZONE 'UTC')
                GROUP BY pair ORDER BY pnl DESC
                """
            )
            per_pair = [dict(r) for r in cur.fetchall()]

            # Regime distribution (last 24h)
            cur.execute(
                """
                SELECT regime, COUNT(*) AS n
                FROM regime_log
                WHERE ts > NOW() - INTERVAL '24 hours'
                GROUP BY regime ORDER BY n DESC
                """
            )
            regime_dist = [dict(r) for r in cur.fetchall()]
    except Exception as exc:
        logger.exception("slack_preview db failed")
        return _envelope("down", error=str(exc))

    perf = await loop.run_in_executor(None, mcp_local.get_performance_metrics)

    n = int(today.get("trades") or 0)
    pnl_usd = float(today.get("pnl_usd") or 0)
    # trade_journal.pnl_pct is fractional (-0.0123 = -1.23%); the Slack-styled
    # card renders this verbatim as a percent, so multiply once here.
    pnl_pct = float(today.get("pnl_pct") or 0) * 100
    wins = int(today.get("wins") or 0)
    losses = int(today.get("losses") or 0)
    win_rate = (wins / n * 100) if n else 0.0

    best = per_pair[0] if per_pair else None
    worst = per_pair[-1] if per_pair and len(per_pair) > 1 else None

    return _envelope("ok", data={
        "date_utc": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "pnl_usd": pnl_usd,
        "pnl_pct": pnl_pct,
        "trades": n,
        "wins": wins,
        "losses": losses,
        "win_rate_pct": round(win_rate, 1),
        "sharpe_trailing": perf.get("sharpe", 0.0),
        "max_dd_trailing": perf.get("max_dd", 0.0),
        "best": best, "worst": worst,
        "regime_distribution": regime_dist,
    })


# --------------------------------------------------------------------------
# /api/ops/stocks — unified shark + wheel state
# --------------------------------------------------------------------------

STOCKS_ROOT = Path(os.environ.get("STOCKS_ROOT", "/freqtrade/stocks"))


def _read_json(path: Path) -> dict | None:
    try:
        if not path.is_file():
            return None
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("stocks: failed to read %s: %s", path, exc)
        return None


def _next_sun_23_et_iso() -> str:
    """Return the next Sunday-23:00-ET firing of the stocks_ml_train cron
    as a human-readable string + ISO datetime. Used by /api/ops/stocks_ml
    so the operator sees "next Sun, May 17, 11 PM ET" instead of an
    ambiguous cron expression that was the same string regardless of
    whether tonight's run had already completed."""
    try:
        from zoneinfo import ZoneInfo
    except ImportError:
        return "0 23 * * 0  (Sun 11 PM ET)"
    et = ZoneInfo("America/New_York")
    now_et = datetime.now(et)
    # Sunday is weekday 6 in Python's isoweekday (Mon=1..Sun=7)
    days_ahead = (6 - now_et.weekday()) % 7
    candidate = now_et.replace(hour=23, minute=0, second=0, microsecond=0)
    candidate += timedelta(days=days_ahead)
    if candidate <= now_et:
        candidate += timedelta(days=7)
    return candidate.strftime("%a %b %d · 11:00 PM ET")


def _file_age_seconds(path: Path) -> int | None:
    try:
        if not path.is_file():
            return None
        return int((datetime.now(timezone.utc).timestamp() - path.stat().st_mtime))
    except OSError:
        return None


def _wheel_cumulative_pnl(trades_file: Path) -> tuple[float, str | None]:
    """Sum pnl across the JSONL ledger; also return the latest trade ts."""
    if not trades_file.is_file():
        return 0.0, None
    total = 0.0
    last_ts: str | None = None
    try:
        for line in trades_file.read_text().splitlines():
            if not line.strip():
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            total += float(rec.get("pnl", 0.0) or 0.0)
            ts = rec.get("timestamp")
            if ts and (last_ts is None or ts > last_ts):
                last_ts = ts
    except OSError as exc:
        logger.warning("stocks: failed to scan trades.jsonl: %s", exc)
    return round(total, 2), last_ts


@router.get("/stocks")
async def stocks_status():
    """Unified shark + wheel + Alpaca state for the /ops Stocks card.

    Reads only from disk (stocks/ is bind-mounted read-only). Live Alpaca
    data comes from `wheel/state/account_snapshot.json`, which the
    `python -m wheel.cli snapshot` cron writes every few minutes — `age_seconds`
    surfaces freshness so a stale snapshot is visible in the UI.
    """
    if not STOCKS_ROOT.is_dir():
        return _envelope(
            "down",
            error=f"stocks root not mounted: {STOCKS_ROOT}",
        )

    # ── Alpaca account snapshot (written by wheel.cli snapshot cron) ─────
    snap_file = STOCKS_ROOT / "wheel" / "state" / "account_snapshot.json"
    snap = _read_json(snap_file) or {}
    alpaca = {
        "cash": snap.get("cash"),
        "buying_power": snap.get("buying_power"),
        "portfolio_value": snap.get("portfolio_value"),
        "paper": snap.get("paper", True),
        "ts": snap.get("ts"),
        "age_seconds": _file_age_seconds(snap_file),
    }

    # ── Wheel positions + cumulative P&L ─────────────────────────────────
    pos_file = STOCKS_ROOT / "wheel" / "state" / "positions.json"
    raw_positions = _read_json(pos_file) or []
    if not isinstance(raw_positions, list):
        raw_positions = []
    wheel_positions = [
        {
            "underlying": p.get("underlying"),
            "kind": p.get("kind"),
            "qty": p.get("qty"),
            "strike": p.get("strike"),
            "expiry": p.get("expiry"),
            "entry_credit": round(float(p.get("entry_credit") or 0.0), 2),
            "contract": p.get("contract_symbol"),
            "opened_at": p.get("opened_at"),
        }
        for p in raw_positions
    ]
    trades_file = STOCKS_ROOT / "wheel" / "state" / "trades.jsonl"
    cumulative_pnl, last_trade_ts = _wheel_cumulative_pnl(trades_file)
    wheel = {
        "open_positions": wheel_positions,
        "cumulative_pnl_usd": cumulative_pnl,
        "last_trade_ts": last_trade_ts,
    }

    # ── Shark momentum bot state (from generated dashboard json) ─────────
    shark_data_file = STOCKS_ROOT / "docs" / "dashboard" / "data.json"
    shark_raw = _read_json(shark_data_file) or {}
    state = shark_raw.get("state") or {}
    stats = shark_raw.get("stats") or {}
    kill_switch = shark_raw.get("kill_switch") or {}
    open_trades_obj = shark_raw.get("open_trades") or {}
    if isinstance(open_trades_obj, dict):
        open_trades = list(open_trades_obj.values())
    elif isinstance(open_trades_obj, list):
        open_trades = open_trades_obj
    else:
        open_trades = []
    shark = {
        "mode": state.get("current_mode"),
        "peak_equity": state.get("peak_equity"),
        "circuit_breaker": bool(state.get("circuit_breaker_triggered", False)),
        "weekly_trade_count": state.get("weekly_trade_count"),
        "kill_switch_active": bool(kill_switch.get("active", False)),
        "kill_switch_reason": kill_switch.get("reason"),
        "open_trades": open_trades,
        "stats": {
            "total_trades": stats.get("total_trades", 0),
            "wins": stats.get("wins", 0),
            "losses": stats.get("losses", 0),
            "win_rate": stats.get("win_rate", 0.0),
            "total_pnl": stats.get("total_pnl", 0.0),
            "current_drawdown_pct": stats.get("current_drawdown_pct", 0.0),
        },
        "generated_at": shark_raw.get("generated_at"),
        "age_seconds": _file_age_seconds(shark_data_file),
    }

    # ── Status: degraded if any source is stale or missing ───────────────
    degraded_reasons: list[str] = []
    if alpaca["age_seconds"] is None:
        degraded_reasons.append("alpaca snapshot missing — run `python -m wheel.cli snapshot`")
    elif alpaca["age_seconds"] > 86400:
        degraded_reasons.append(f"alpaca snapshot stale ({alpaca['age_seconds']}s old)")
    if shark["age_seconds"] is None:
        degraded_reasons.append("shark dashboard data missing")
    if shark["circuit_breaker"]:
        degraded_reasons.append("shark circuit breaker tripped")
    if shark["kill_switch_active"]:
        degraded_reasons.append(f"shark kill switch: {shark['kill_switch_reason'] or 'active'}")

    payload = {"alpaca": alpaca, "wheel": wheel, "shark": shark}
    if not degraded_reasons:
        return _envelope("ok", data=payload)
    return _envelope("degraded", data=payload, error="; ".join(degraded_reasons))


# Whitelist of stock symbols the dashboard chart page is allowed to query.
# Keeps `/api/ops/stock_candles/{sym}` from being a generic Alpaca proxy.
_STOCK_SYMBOL_WHITELIST = {"SOFI", "AAPL", "TSLA", "NVDA", "META", "MSFT", "GOOGL", "AMZN", "MARA", "F", "PLTR", "AMD", "SPY"}


@router.get("/stock_candles/{symbol}")
async def stock_candles(symbol: str, timeframe: str = "5Min"):
    """Serve OHLC candles for a stock from the cron-fed JSON cache.

    The dashboard never calls Alpaca directly. The `wheel_candles` Hermes
    cron writes `stocks/wheel/state/candles_{SYM}_{tf}.json` every 5 min
    during market hours; this endpoint streams the cached file as a
    Lightweight-Charts-compatible payload.
    """
    sym = symbol.upper()
    if sym not in _STOCK_SYMBOL_WHITELIST:
        raise HTTPException(status_code=404, detail=f"symbol not whitelisted: {sym}")
    if timeframe not in {"1Min", "5Min", "15Min", "1Hour", "1Day"}:
        raise HTTPException(status_code=400, detail=f"unsupported timeframe: {timeframe}")

    candles_file = STOCKS_ROOT / "wheel" / "state" / f"candles_{sym}_{timeframe}.json"
    raw = _read_json(candles_file)
    if not raw:
        return _envelope(
            "down",
            error=f"no cached candles for {sym} {timeframe} — run `python -m wheel.cli candles {sym} --timeframe {timeframe}`",
        )

    bars = raw.get("bars") or []
    age = _file_age_seconds(candles_file)
    payload = {
        "symbol": sym,
        "timeframe": timeframe,
        "ts": raw.get("ts"),
        "age_seconds": age,
        "bars": bars,
    }

    # Stale = no refresh in 24h
    if age is not None and age > 86400:
        return _envelope("degraded", data=payload, error=f"candles stale ({age}s old)")
    return _envelope("ok", data=payload)


@router.get("/gates")
async def gates():
    """Per-pair entry-gate matrix: which gate is blocking each pair right now.

    Walks every pair in DASHBOARD_PAIRS, pulls the latest row from
    freqtrade's pair_candles, and evaluates each entry gate the strategy
    enforces in populate_entry_trend(). Returns a grid the dashboard
    renders so the operator can see exactly *why* a pair isn't trading.

    Gates evaluated (in strategy order):
      1. capital_allocation     pair weight > 0
      2. do_predict             FreqAI has live predictions
      3. volume                 last bar has volume > 0
      4. regime                 != trending_down (hard block)
      5. up_prob_threshold      up >= base + regime delta
      6. tft_confidence         >= TFT_MIN_CONFIDENCE (0.40)
      7. high_vol_confidence    not (regime=high_vol AND up < HIGH_VOL_MIN)
      8. meta_signal_up         == +1 when DRL active
      9. meta_confidence        >= META_MIN_CONFIDENCE (0.40) when DRL active
     10. account_capacity       open_count < max_open AND breaker clear
    """
    from .data_sources import fetch_freqtrade_candles, latest_state_from_df

    # Strategy constants — read live from config so dashboard mirrors the
    # actual gate the strategy enforces. Falls back to the strategy defaults
    # (FreqAIMeanRevV1._DEFAULT_REGIME_GATING) when keys are missing.
    _rg_live = {}
    _expiration_h = 26.0
    _identifier = "tft_v1"
    try:
        import json as _json
        _cfg = _json.loads(CONFIG_PATH.read_text())
        _rg_live = (_cfg.get("regime_gating") or {})
        _freqai = (_cfg.get("freqai") or {})
        _expiration_h = float(_freqai.get("expiration_hours") or 26.0)
        _identifier = str(_freqai.get("identifier") or "tft_v1")
    except Exception:
        pass
    TFT_MIN = float(_rg_live.get("tft_min_confidence", 0.40))
    META_MIN = float(_rg_live.get("meta_min_confidence", 0.40))
    HIGH_VOL_MIN = float(_rg_live.get("high_vol_min_confidence", 0.75))
    BASE_ENTRY = 0.62
    REGIME_DELTA = {
        "trending_up":      -0.05,
        "trending_down":    None,   # None = hard block
        "mean_reverting":   +0.10,
        "high_volatility":  +0.05,
        "unknown":          0.0,
    }

    # FreqAI authoritative model registry — used by the `model_freshness` gate
    # to surface MODEL EXPIRED before the strategy-level do_predict gate.
    _pair_dict: dict = {}
    try:
        import json as _json2, time as _time
        _pd_path = Path(f"/freqtrade/user_data/models/{_identifier}/pair_dictionary.json")
        if _pd_path.exists():
            _pair_dict = _json2.loads(_pd_path.read_text()) or {}
    except Exception as _exc:
        logger.warning("gates: pair_dictionary read failed: %s", _exc)
    _now_ts = int(__import__("time").time())

    pairs = [p.strip() for p in os.environ.get("DASHBOARD_PAIRS", "BTC/USD,ETH/USD,SOL/USD").split(",") if p.strip()]
    timeframe = os.environ.get("DASHBOARD_TIMEFRAME", "5m")

    # Account-level inputs from existing endpoints
    open_count = 0
    max_open = 6
    breaker_active = False
    try:
        async with httpx.AsyncClient(timeout=ENDPOINT_TIMEOUT_S) as client:
            tok = await _ensure_jwt(client)
            if tok:
                r = await client.get(f"{FREQTRADE_API_URL}/api/v1/status", headers={"Authorization": f"Bearer {tok}"})
                if r.status_code == 200:
                    open_count = len(r.json() or [])
                r2 = await client.get(f"{FREQTRADE_API_URL}/api/v1/show_config", headers={"Authorization": f"Bearer {tok}"})
                if r2.status_code == 200:
                    cfg = r2.json() or {}
                    max_open = int(cfg.get("max_open_trades") or 6)
    except Exception as exc:
        logger.warning("gates: account-level fetch failed: %s", exc)

    # Per-pair gate evaluation
    rows = []
    for pair in pairs:
        try:
            df = await fetch_freqtrade_candles(pair, timeframe, limit=5)
            state = latest_state_from_df(df, pair) if df is not None else {}
        except Exception as exc:
            logger.warning("gates: pair_candles failed for %s: %s", pair, exc)
            state = {"_error": str(exc)}

        regime = state.get("regime") or "unknown"
        delta = REGIME_DELTA.get(regime, 0.0)
        threshold = (BASE_ENTRY + delta) if delta is not None else None
        up = state.get("tft_up")
        tft_conf = state.get("tft_confidence")
        meta_sig = state.get("meta_signal")
        meta_conf = state.get("meta_confidence")
        volume = state.get("volume")
        do_predict = state.get("do_predict")

        gate_results = []

        # 1. Pair capital allocation — we don't have per-pair weight here
        #    without parsing config.json; assume allowed (most operators set this once).
        gate_results.append({
            "gate": "capital_allocation",
            "pass": True,
            "detail": "weight > 0 (assumed)",
        })

        # 2. model_freshness — FreqAI's pair_dictionary.json is the authoritative
        # source of "is there a usable model right now?". When the registered
        # trained_timestamp is older than freqai.expiration_hours, FreqAI returns
        # do_predict=2 / prediction=0 to the strategy and the bot CANNOT trade
        # this pair. Surface that explicitly here so the operator sees the real
        # reason on the gates panel instead of a misleading "no signal" null.
        _pd_entry = _pair_dict.get(pair) or {}
        _trained_ts = _pd_entry.get("trained_timestamp")
        if not _trained_ts:
            gate_results.append({
                "gate": "model_freshness",
                "pass": False,
                "detail": "no model registered in pair_dictionary",
            })
            _age_h = None
        else:
            _age_h = (_now_ts - int(_trained_ts)) / 3600.0
            _is_fresh = _age_h < _expiration_h
            gate_results.append({
                "gate": "model_freshness",
                "pass": _is_fresh,
                "detail": (
                    f"model {_age_h:.1f}h old (limit {_expiration_h:.0f}h)"
                    if _is_fresh
                    else f"MODEL EXPIRED · {_age_h:.1f}h old > {_expiration_h:.0f}h"
                ),
            })

        # 3. do_predict — FreqAI has live predictions on the latest bar
        gate_results.append({
            "gate": "freqai_predict",
            "pass": do_predict in (1, True, "1") if do_predict is not None else None,
            "detail": f"do_predict={do_predict}",
        })

        # 3. volume
        vol_pass = (volume or 0) > 0 if volume is not None else None
        gate_results.append({
            "gate": "volume",
            "pass": vol_pass,
            "detail": f"volume={volume}",
        })

        # 4. regime — trending_down is a hard block
        regime_pass = regime != "trending_down"
        gate_results.append({
            "gate": "regime",
            "pass": regime_pass,
            "detail": f"regime={regime}" + (" [HARD BLOCK]" if regime == "trending_down" else ""),
        })

        # 5. up_prob_threshold (only meaningful when regime != trending_down)
        if threshold is None:
            gate_results.append({
                "gate": "up_prob_threshold",
                "pass": False,
                "detail": "regime=trending_down → hard block",
            })
        elif up is None:
            gate_results.append({
                "gate": "up_prob_threshold",
                "pass": None,
                "detail": "no up signal yet",
            })
        else:
            gate_results.append({
                "gate": "up_prob_threshold",
                "pass": up >= threshold,
                "detail": f"up={up:.3f} vs threshold={threshold:.2f} (base {BASE_ENTRY:+.2f}{delta:+.2f})",
            })

        # 6. tft_confidence
        if tft_conf is None:
            gate_results.append({
                "gate": "tft_confidence",
                "pass": None,
                "detail": "tft_confidence missing",
            })
        else:
            gate_results.append({
                "gate": "tft_confidence",
                "pass": tft_conf >= TFT_MIN,
                "detail": f"{tft_conf:.3f} ≥ {TFT_MIN:.2f}",
            })

        # 7. high_vol_confidence
        if regime == "high_volatility":
            if up is None:
                gate_results.append({"gate": "high_vol_confidence", "pass": None, "detail": "up missing"})
            else:
                gate_results.append({
                    "gate": "high_vol_confidence",
                    "pass": up >= HIGH_VOL_MIN,
                    "detail": f"high_vol regime: up={up:.3f} ≥ {HIGH_VOL_MIN}",
                })
        else:
            gate_results.append({
                "gate": "high_vol_confidence",
                "pass": True,
                "detail": "n/a (not high_vol regime)",
            })

        # 8. meta_signal — only enforced when DRL active (meta_confidence > 0)
        meta_active = meta_conf is not None and meta_conf > 0
        if meta_active:
            gate_results.append({
                "gate": "meta_signal_up",
                "pass": meta_sig == 1,
                "detail": f"meta_signal={meta_sig} (need +1)",
            })
            gate_results.append({
                "gate": "meta_confidence",
                "pass": meta_conf >= META_MIN,
                "detail": f"{meta_conf:.3f} ≥ {META_MIN:.2f}",
            })
        else:
            gate_results.append({
                "gate": "meta_signal_up",
                "pass": True,
                "detail": "DRL ensemble inactive — gate disabled",
            })
            gate_results.append({
                "gate": "meta_confidence",
                "pass": True,
                "detail": "DRL ensemble inactive — gate disabled",
            })

        # 10. account_capacity (account-wide, but we render per row)
        gate_results.append({
            "gate": "account_capacity",
            "pass": open_count < max_open and not breaker_active,
            "detail": f"{open_count}/{max_open} open" + (" · BREAKER" if breaker_active else ""),
        })

        # Summary
        blocking = [g for g in gate_results if g["pass"] is False]
        rows.append({
            "pair": pair,
            "regime": regime,
            "n_gates": len(gate_results),
            "n_blocking": len(blocking),
            "first_blocker": blocking[0]["gate"] if blocking else None,
            "gates": gate_results,
            "snapshot": {
                "up": up,
                "tft_confidence": tft_conf,
                "meta_signal": meta_sig,
                "meta_confidence": meta_conf,
                "volume": volume,
                "threshold": threshold,
                "model_age_h": _age_h,
                "model_expiration_h": _expiration_h,
            },
        })

    # ── Stocks gates (wheel CSP rules per ticker) ──────────────────────
    stock_rows = []
    pos_file = STOCKS_ROOT / "wheel" / "state" / "positions.json"
    snap_file = STOCKS_ROOT / "wheel" / "state" / "account_snapshot.json"
    kill_file = STOCKS_ROOT / "wheel" / "state" / "kill_flags.json"
    raw_positions = _read_json(pos_file) or []
    snap = _read_json(snap_file) or {}
    kill_flags = _read_json(kill_file) or {}
    shark_kill = (STOCKS_ROOT / "memory" / "KILL.flag").exists() if STOCKS_ROOT.is_dir() else False

    # Stocks-regime feed (SPY simple classifier)
    stock_regime = None
    spy_file = STOCKS_ROOT / "wheel" / "state" / "candles_SPY_1Day.json"
    spy_raw = _read_json(spy_file)
    if spy_raw and len(spy_raw.get("bars") or []) >= 200:
        closes = [float(b["close"]) for b in spy_raw["bars"]]
        ma50 = sum(closes[-50:]) / 50
        ma200 = sum(closes[-200:]) / 200
        spot = closes[-1]
        if ma50 < ma200 and spot < ma200:
            stock_regime = "trending_down"
        elif ma50 > ma200 and spot > ma200:
            stock_regime = "trending_up"
        else:
            stock_regime = "mean_reverting"

    wheel_symbols = [
        s.strip().upper() for s in os.environ.get("WHEEL_SYMBOLS", "SOFI").split(",") if s.strip()
    ]
    cash = snap.get("cash") or 0
    bp = snap.get("buying_power") or 0
    paper = snap.get("paper", True)

    for sym in wheel_symbols:
        gate_results = []

        # 1. Shark global kill switch
        gate_results.append({
            "gate": "kill_switch",
            "pass": not shark_kill,
            "detail": "KILL.flag present" if shark_kill else "no kill flag",
        })

        # 2. Per-ticker 90-day kill flag
        kill_until = kill_flags.get(sym)
        gate_results.append({
            "gate": "ticker_kill_flag",
            "pass": kill_until is None,
            "detail": f"killed until {kill_until}" if kill_until else "clear",
        })

        # 3. SPY regime not trending_down (the safety rail we want to add)
        if stock_regime is None:
            gate_results.append({"gate": "spy_regime", "pass": None, "detail": "SPY data missing"})
        else:
            gate_results.append({
                "gate": "spy_regime",
                "pass": stock_regime != "trending_down",
                "detail": f"SPY regime={stock_regime}",
            })

        # 4. Existing CSP / shares — already-positioned skip
        has_csp = any(p.get("kind") == "short_put" and p.get("underlying") == sym for p in raw_positions)
        has_shares = sum(p.get("qty", 0) for p in raw_positions if p.get("kind") == "long_shares" and p.get("underlying") == sym) >= 100
        gate_results.append({
            "gate": "no_existing_csp",
            "pass": not has_csp,
            "detail": "open CSP exists" if has_csp else "no open CSP",
        })
        gate_results.append({
            "gate": "no_assignment",
            "pass": not has_shares,
            "detail": "holds ≥100 shares (CC leg)" if has_shares else "no assigned shares",
        })

        # 5. Buying power / collateral budget — assume strike $15 → $1500 collateral
        max_risk = float(os.environ.get("WHEEL_MAX_RISK_PER_TICKER", "1700"))
        gate_results.append({
            "gate": "buying_power",
            "pass": bp >= max_risk,
            "detail": f"BP ${bp:,.0f} ≥ collateral ~${max_risk:,.0f}",
        })

        # 6. Account snapshot fresh
        snap_age = _file_age_seconds(snap_file)
        gate_results.append({
            "gate": "snapshot_fresh",
            "pass": snap_age is not None and snap_age < 86400,
            "detail": f"snapshot {snap_age}s old" if snap_age is not None else "no snapshot",
        })

        # 7. Calendar / cron schedule (today is Friday for sell-csps)
        from datetime import datetime, timezone, timedelta
        weekday = datetime.now(timezone.utc).weekday()
        gate_results.append({
            "gate": "schedule",
            "pass": True,  # always passes; schedule-driven
            "detail": "next sell-csps cron: Friday 15:00 UTC",
        })

        blocking = [g for g in gate_results if g["pass"] is False]
        stock_rows.append({
            "pair": sym,
            "regime": stock_regime or "—",
            "n_gates": len(gate_results),
            "n_blocking": len(blocking),
            "first_blocker": blocking[0]["gate"] if blocking else None,
            "gates": gate_results,
            "snapshot": {
                "cash": cash,
                "buying_power": bp,
                "paper": paper,
            },
        })

    return _envelope("ok", data={
        "crypto": rows,
        "stocks": stock_rows,
        "constants": {
            "tft_min": TFT_MIN,
            "meta_min": META_MIN,
            "high_vol_min": HIGH_VOL_MIN,
            "base_entry": BASE_ENTRY,
            "regime_delta": REGIME_DELTA,
        },
        "account": {
            "open_count": open_count,
            "max_open": max_open,
            "breaker_active": breaker_active,
            "paper": paper,
        },
    })


@router.get("/market_hours")
async def market_hours():
    """NYSE / US-equities session state for the dashboard.

    Markets are M-F 09:30-16:00 America/New_York. Crypto charts run 24/7;
    stocks are paused 17/24 of each weekday plus all weekends. The dashboard
    uses this to show "🔒 NYSE closed · reopens Monday 09:30 ET" instead of
    a stale chart that looks broken.

    No external dependency on `pandas_market_calendars` (which would add
    holiday awareness). Returns:
        is_open:           bool
        is_extended:       bool   (premarket 04:00-09:30 or after-hours 16:00-20:00)
        last_close_utc:    ISO    (UTC)
        next_open_utc:     ISO    (UTC)
        next_close_utc:    ISO    (UTC)
        now_et:            string in ET for display
        holiday_note:      None for now (TODO: NYSE holiday feed)
    """
    from datetime import datetime, time, timedelta, timezone
    try:
        # Python 3.9+ stdlib
        from zoneinfo import ZoneInfo
    except ImportError:  # pragma: no cover
        from backports.zoneinfo import ZoneInfo

    ET = ZoneInfo("America/New_York")
    now_utc = datetime.now(timezone.utc)
    now_et = now_utc.astimezone(ET)

    REG_OPEN = time(9, 30)
    REG_CLOSE = time(16, 0)
    EXT_OPEN = time(4, 0)
    EXT_CLOSE = time(20, 0)

    weekday = now_et.weekday()  # 0=Mon..6=Sun
    is_weekday = weekday < 5
    cur_t = now_et.time()
    is_open = is_weekday and (REG_OPEN <= cur_t < REG_CLOSE)
    is_extended = is_weekday and (
        (EXT_OPEN <= cur_t < REG_OPEN) or (REG_CLOSE <= cur_t < EXT_CLOSE)
    ) and not is_open

    # Compute last-close: most recent weekday at 16:00 ET that has already passed
    def _last_weekday_at(target: datetime, t: time) -> datetime:
        candidate = target.replace(hour=t.hour, minute=t.minute, second=0, microsecond=0)
        if candidate > target:
            candidate -= timedelta(days=1)
        while candidate.weekday() >= 5:  # roll back through weekend
            candidate -= timedelta(days=1)
        return candidate

    last_close_et = _last_weekday_at(now_et, REG_CLOSE)

    # Compute next open/close
    def _next_weekday_at(target: datetime, t: time) -> datetime:
        candidate = target.replace(hour=t.hour, minute=t.minute, second=0, microsecond=0)
        if candidate <= target:
            candidate += timedelta(days=1)
        while candidate.weekday() >= 5:
            candidate += timedelta(days=1)
        return candidate

    if is_open:
        next_close_et = now_et.replace(hour=16, minute=0, second=0, microsecond=0)
        if next_close_et <= now_et:
            next_close_et += timedelta(days=1)
        # Next open after that day's close
        next_open_et = _next_weekday_at(next_close_et, REG_OPEN)
    else:
        next_open_et = _next_weekday_at(now_et, REG_OPEN)
        next_close_et = next_open_et.replace(hour=16, minute=0, second=0, microsecond=0)

    return _envelope("ok", data={
        "is_open": is_open,
        "is_extended": is_extended,
        "last_close_utc": last_close_et.astimezone(timezone.utc).isoformat(),
        "next_open_utc": next_open_et.astimezone(timezone.utc).isoformat(),
        "next_close_utc": next_close_et.astimezone(timezone.utc).isoformat(),
        "now_et": now_et.isoformat(timespec="seconds"),
        "weekday": ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"][weekday],
        "holiday_note": None,
    })


@router.get("/live_trades")
async def live_trades():
    """Aggregate every active position across crypto + wheel + shark for
    the top hero strip. One source of truth so the operator sees ALL
    trading activity at a glance, not split across two pages.
    """
    out: list[dict] = []

    # ── Crypto open trades from freqtrade ────────────────────────────
    try:
        async with httpx.AsyncClient(timeout=ENDPOINT_TIMEOUT_S) as client:
            token = await _ensure_jwt(client)
            if token:
                r = await client.get(
                    f"{FREQTRADE_API_URL}/api/v1/status",
                    headers={"Authorization": f"Bearer {token}"},
                )
                if r.status_code == 200:
                    for t in (r.json() or []):
                        out.append({
                            "kind": "crypto",
                            "subkind": "long" if not t.get("is_short") else "short",
                            "label": t.get("pair") or "?",
                            "entry": t.get("open_rate"),
                            "current": t.get("current_rate"),
                            "qty": t.get("amount"),
                            "pnl_pct": (t.get("profit_ratio") or 0) * 100,
                            "pnl_usd": t.get("profit_abs"),
                            "duration_s": t.get("trade_duration_s"),
                            "opened_at": t.get("open_date"),
                            "extra": f"regime@entry={t.get('regime', '—')}",
                        })
    except Exception as exc:
        logger.warning("live_trades: freqtrade fetch failed: %s", exc)

    # ── Wheel positions (puts/calls/shares) ─────────────────────────
    pos_file = STOCKS_ROOT / "wheel" / "state" / "positions.json"
    snap_file = STOCKS_ROOT / "wheel" / "state" / "account_snapshot.json"
    raw_positions = _read_json(pos_file) or []
    if isinstance(raw_positions, list):
        for p in raw_positions:
            kind = p.get("kind", "")
            label_kind = (
                "short_put" if kind == "short_put"
                else "short_call" if kind == "short_call"
                else "long_shares" if kind == "long_shares"
                else kind
            )
            entry_credit = float(p.get("entry_credit") or 0.0)
            out.append({
                "kind": "wheel",
                "subkind": label_kind,
                "label": p.get("underlying") or "?",
                "entry": p.get("strike"),
                "current": None,
                "qty": p.get("qty"),
                "pnl_pct": None,
                "pnl_usd": entry_credit if "short_" in kind else None,
                "duration_s": None,
                "opened_at": p.get("opened_at"),
                "extra": f"expiry={p.get('expiry') or '—'} contract={p.get('contract_symbol') or '—'}",
            })

    # ── Aggregate health ─────────────────────────────────────────────
    snap = _read_json(snap_file) or {}
    summary = {
        "total_active": len(out),
        "crypto_active": sum(1 for t in out if t["kind"] == "crypto"),
        "wheel_active": sum(1 for t in out if t["kind"] == "wheel"),
        "shark_active": 0,  # shark trades are crypto-tracked separately for now
        "alpaca_paper": snap.get("paper", True),
    }
    return _envelope("ok", data={"trades": out, "summary": summary})


@router.get("/ollama_health")
async def ollama_health():
    """Latest Ollama health probe — read from /tmp/ollama-health.json that
    the cron-driven `python -m user_data.modules.ollama_health` writes."""
    status_file = Path(os.environ.get("OLLAMA_HEALTH_STATUS_FILE", "/tmp/ollama-health.json"))
    raw = _read_json(status_file)
    if not raw:
        return _envelope(
            "down",
            error="No health data yet — wait for the next ollama_health cron tick "
                  "(or run `python -m user_data.modules.ollama_health` manually).",
        )
    age = _file_age_seconds(status_file)
    raw["status_age_seconds"] = age
    if age is not None and age > 300:
        return _envelope("degraded", data=raw, error=f"health probe {age}s stale")
    return _envelope("ok" if raw.get("healthy") else "degraded", data=raw,
                     error=raw.get("error"))


@router.get("/circuit_breakers")
async def circuit_breakers():
    """Status of all LLM circuit breakers (Ollama + Anthropic, fast + deep).

    The shark process owns the breaker registry; the dashboard reads disk
    state via discover_from_disk() so it doesn't need to share memory.
    """
    try:
        import sys as _sys
        stocks_root = STOCKS_ROOT
        if str(stocks_root) not in _sys.path:
            _sys.path.insert(0, str(stocks_root))
        from shark.llm.circuit_breaker import discover_from_disk
    except ImportError as exc:
        return _envelope("down", error=f"circuit_breaker module unavailable: {exc}",
                         data={"breakers": []})

    breakers = discover_from_disk()
    # Build a top-level summary so the UI can show "any breaker open" at-a-glance.
    open_count = sum(1 for b in breakers if b.get("state") == "open")
    half_open_count = sum(1 for b in breakers if b.get("state") == "half_open")
    summary = {
        "total": len(breakers),
        "open": open_count,
        "half_open": half_open_count,
        "any_failover_active": open_count > 0 or half_open_count > 0,
    }
    return _envelope(
        "degraded" if open_count > 0 else "ok",
        data={"summary": summary, "breakers": breakers},
        error=f"{open_count} breaker(s) OPEN — Anthropic fallback active" if open_count else None,
    )


@router.get("/llm_stats")
async def llm_stats():
    """LLM inference monitor — latency, model routing, counterfactual cost.

    Reads two sources:
      1. stocks/memory/llm-calls.jsonl  (shark agents, written by
         shark.llm.tracker on every chat_json call)
      2. sentiment_log table             (crypto sentiment engine, latency
         derived from prompt_eval + eval counts that the engine logs)

    Returns a single envelope so one card on /ops can render everything.
    """
    import json as _json

    # ── Shark side: rolling 24h window of the JSONL ─────────────────
    shark_log = STOCKS_ROOT / "memory" / "llm-calls.jsonl"
    shark_summary: dict = {
        "total_calls": 0, "total_latency_seconds": 0.0,
        "avg_latency_seconds": 0.0, "total_api_cost_saved_usd": 0.0,
        "by_model": {}, "by_agent": {}, "by_tier": {"fast": 0, "deep": 0},
    }
    cutoff = datetime.now(timezone.utc).timestamp() - 86400
    if shark_log.is_file():
        try:
            calls = []
            with shark_log.open("r", encoding="utf-8") as fh:
                for line in fh:
                    if not line.strip():
                        continue
                    try:
                        rec = _json.loads(line)
                    except _json.JSONDecodeError:
                        continue
                    ts = rec.get("timestamp") or ""
                    try:
                        when = datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
                    except (ValueError, TypeError):
                        continue
                    if when < cutoff:
                        continue
                    calls.append(rec)
            if calls:
                p_toks = sum(int(c.get("prompt_tokens") or 0) for c in calls)
                c_toks = sum(int(c.get("completion_tokens") or 0) for c in calls)
                # Counterfactual cost: $3/M prompt + $15/M completion (Sonnet 4.6)
                saved = (p_toks * 3.0 + c_toks * 15.0) / 1_000_000
                total_lat = sum(float(c.get("latency_seconds") or 0) for c in calls)

                # By agent
                by_agent: dict = {}
                for c in calls:
                    agent = c.get("agent", "unknown")
                    rec = by_agent.setdefault(agent, {"calls": 0, "latencies": [], "models": set()})
                    rec["calls"] += 1
                    rec["latencies"].append(float(c.get("latency_seconds") or 0))
                    rec["models"].add(c.get("model", "?"))
                for agent, rec in by_agent.items():
                    lats = rec["latencies"]
                    rec["avg_latency"] = round(sum(lats) / len(lats), 2)
                    rec["max_latency"] = round(max(lats), 2)
                    rec["models"] = sorted(rec["models"])
                    del rec["latencies"]

                # By model + tier
                by_model: dict = {}
                by_tier = {"fast": 0, "deep": 0}
                for c in calls:
                    m = c.get("model", "?")
                    by_model[m] = by_model.get(m, 0) + 1
                    t = c.get("tier", "deep")
                    if t in by_tier:
                        by_tier[t] += 1

                shark_summary = {
                    "total_calls": len(calls),
                    "total_latency_seconds": round(total_lat, 1),
                    "avg_latency_seconds": round(total_lat / len(calls), 2),
                    "total_prompt_tokens": p_toks,
                    "total_completion_tokens": c_toks,
                    "total_api_cost_saved_usd": round(saved, 4),
                    "by_model": dict(sorted(by_model.items(), key=lambda kv: -kv[1])),
                    "by_agent": by_agent,
                    "by_tier": by_tier,
                    "log_path": str(shark_log),
                    "log_size_bytes": shark_log.stat().st_size,
                }
        except Exception as exc:
            logger.warning("llm_stats: shark log read failed: %s", exc)

    # ── Crypto side: sentiment-engine call counts from sentiment_log ────
    crypto_summary: dict = {"calls_24h": 0, "avg_latency_seconds": None}
    if ops_db._HAVE_PG:
        try:
            with ops_db._connect() as conn, conn.cursor() as cur:
                cur.execute(
                    "SELECT COUNT(*) AS n, "
                    "       SUM(COALESCE(claude_score,0) * 0 + 1) AS x "  # sentinel
                    "FROM sentiment_log "
                    "WHERE ts > NOW() - INTERVAL '24 hours'"
                )
                row = cur.fetchone() or {}
                crypto_summary["calls_24h"] = int(row.get("n") or 0)
                # Sentiment engine doesn't currently persist latency. Note that.
                crypto_summary["latency_note"] = (
                    "sentiment_log doesn't carry per-call latency yet; row count is the proxy."
                )
        except Exception as exc:
            logger.warning("llm_stats: sentiment query failed: %s", exc)

    provider = os.environ.get("SHARK_LLM_PROVIDER", "ollama")
    return _envelope("ok", data={
        "provider": provider,
        "is_local": provider == "ollama",
        "shark": shark_summary,
        "crypto": crypto_summary,
    })


@router.get("/combined_portfolio")
async def combined_portfolio():
    """Combined crypto + stocks portfolio + drawdown, for the unified card."""
    try:
        # The unified_risk module lives in the freqtrade-side path tree;
        # it's read-only and doesn't import freqtrade itself.
        import sys as _sys
        repo_root = STOCKS_ROOT.parent
        if str(repo_root) not in _sys.path:
            _sys.path.insert(0, str(repo_root))
        from user_data.modules.unified_risk import get_combined_risk_status
    except ImportError as exc:
        return _envelope("down", error=f"unified_risk import failed: {exc}")

    try:
        loop = asyncio.get_running_loop()
        status = await asyncio.wait_for(
            loop.run_in_executor(None, get_combined_risk_status),
            timeout=ENDPOINT_TIMEOUT_S,
        )
    except asyncio.TimeoutError:
        return _envelope("down", error="combined_risk query timed out")
    except Exception as exc:
        logger.exception("combined_portfolio: %s", exc)
        return _envelope("down", error=str(exc))

    # Enrich with day-P&L (closed trades today, UTC) so the hero card has a
    # real day number instead of "combined_peak − today" (which is drawdown).
    # daily_pnl_usd / daily_pnl_pct come from trade_journal via ops_db; pct
    # is fractional (e.g. -0.0123 = -1.23%).
    try:
        risk = await asyncio.wait_for(
            loop.run_in_executor(None, ops_db.trades_risk_summary),
            timeout=ENDPOINT_TIMEOUT_S,
        )
        day_pnl_usd = risk.get("daily_pnl_usd")
        day_pnl_pct_frac = risk.get("daily_pnl_pct")  # fractional
        status["day_pnl_usd"] = float(day_pnl_usd) if day_pnl_usd is not None else 0.0
        status["day_pnl_pct"] = float(day_pnl_pct_frac) * 100 if day_pnl_pct_frac is not None else 0.0
    except Exception as exc:  # noqa: BLE001
        logger.warning("combined_portfolio: day P&L enrichment failed: %s", exc)
        status.setdefault("day_pnl_usd", 0.0)
        status.setdefault("day_pnl_pct", 0.0)

    # Promote the breaker flag to the envelope status
    return _envelope(
        "degraded" if status.get("circuit_breaker_active") else "ok",
        data=status,
        error="combined breaker tripped" if status.get("circuit_breaker_active") else None,
    )


@router.get("/shark_briefing")
async def shark_briefing():
    """Parsed Shark daily handoff — today's regime, macro, candidates.

    Source of truth: `stocks/memory/DAILY-HANDOFF.md`, written by every Shark
    phase. Format (per phase block):
        ## pre-market | 09:14 EDT
        confirmed: NVDA
        skipped: GOOGL, AMD, CCJ, CRDO, XOM
        market: bullish=9 bearish=1 of 30
        regime: BEAR_VOLATILE
        macro: ELEVATED
        lessons: none

    This is what's behind "why no stocks trades fired today" — the operator
    couldn't see Shark's decision flow on the dashboard until now.
    """
    handoff_path = STOCKS_ROOT / "memory" / "DAILY-HANDOFF.md"
    if not handoff_path.exists():
        return _envelope("down", error=f"missing: {handoff_path}")

    try:
        raw = handoff_path.read_text(errors="replace")
    except Exception as exc:
        return _envelope("down", error=str(exc))

    import re
    # Find today's date header (operator wants TODAY only)
    today = datetime.now(timezone.utc).astimezone(
        timezone(timedelta(hours=-4))  # ET — Shark writes in EDT
    ).strftime("%Y-%m-%d")
    # Locate "# Daily Handoff — <date>" block
    day_match = re.search(r"# Daily Handoff — (\d{4}-\d{2}-\d{2})", raw)
    handoff_date = day_match.group(1) if day_match else None

    # Parse each "## phase | HH:MM TZ" sub-block
    phases = []
    phase_re = re.compile(
        r"##\s+(?P<phase>[\w-]+)\s+\|\s+(?P<time>\d{2}:\d{2})\s+(?P<tz>\w+)\s*\n"
        r"(?P<body>(?:(?!^##\s).*\n?)+)",
        re.MULTILINE,
    )
    for m in phase_re.finditer(raw):
        body = m.group("body")
        kv: dict[str, str] = {}
        for line in body.splitlines():
            line = line.strip()
            if ":" in line and not line.startswith("#"):
                k, _, v = line.partition(":")
                kv[k.strip()] = v.strip()
        # Parse "confirmed: NVDA" into list; same for skipped
        confirmed = [s.strip() for s in (kv.get("confirmed") or "").split(",") if s.strip()]
        skipped = [s.strip() for s in (kv.get("skipped") or "").split(",") if s.strip()]
        phases.append({
            "phase": m.group("phase"),
            "time": m.group("time"),
            "tz": m.group("tz"),
            "confirmed": confirmed,
            "skipped": skipped,
            "market_summary": kv.get("market"),
            "regime": kv.get("regime"),
            "macro": kv.get("macro"),
            "lessons": kv.get("lessons"),
        })

    age_s = int((datetime.now(timezone.utc).timestamp() - handoff_path.stat().st_mtime))
    return _envelope(
        "ok" if phases else "degraded",
        data={
            "handoff_date": handoff_date,
            "phases": phases,
            "n_phases": len(phases),
            "file_age_s": age_s,
            "trade_block_explanation": (
                "Shark trades when regime is BULL_QUIET or BULL_VOLATILE AND no "
                "CPI/FOMC/NFP today or tomorrow. PAPER-mode BEAR override allows "
                "1 trade/day at 0.5x size with confidence ≥ 0.85."
            ),
        },
        error=None if phases else "no phase blocks parsed",
    )


@router.get("/stocks_ml")
async def stocks_ml():
    """Stocks ML pipeline status — TFT model freshness, val_acc, generation log.

    ALPHA. Reads:
      stocks/kb/models/tft/stock_tft_v1_summary.json   (training summary)
      stocks/kb/models/evolution_log.json              (EPT generation history)
      stocks/memory/cron-stocks-ml-train.log           (last few lines)
    """
    summary_path = STOCKS_ROOT / "kb" / "models" / "tft" / "stock_tft_v1_summary.json"
    weights_path = STOCKS_ROOT / "kb" / "models" / "tft" / "stock_tft_v1.pt"
    evolution_path = STOCKS_ROOT / "kb" / "models" / "evolution_log.json"
    log_path = STOCKS_ROOT / "memory" / "cron-stocks-ml-train.log"
    status_path = STOCKS_ROOT / "memory" / "stocks-ml-status.json"

    summary = _read_json(summary_path) or {}
    evolution = _read_json(evolution_path) or []
    if not isinstance(evolution, list):
        evolution = []
    training_status = _read_json(status_path) or {}

    weights_age_seconds = _file_age_seconds(weights_path)
    weights_exists = weights_path.is_file()

    # Last 20 lines of the train log so the operator can see what happened
    log_tail: list[str] = []
    if log_path.is_file():
        try:
            with log_path.open(errors="replace") as f:
                lines = f.readlines()
            log_tail = [l.rstrip() for l in lines[-20:]]
        except OSError:
            pass

    # ── Live training progress ─────────────────────────────────────────
    # When a worker is mid-flight, parse the latest "epoch N/M loss=… val_acc=…"
    # line out of the log tail so the dashboard renders a progress card
    # instead of just "weights present: False".
    import re, os as _os
    live_state = "idle"
    live_pid = training_status.get("pid")
    if live_pid is not None:
        try:
            _os.kill(int(live_pid), 0)
            live_state = "running"
        except (OSError, ValueError):
            # PID stale (worker exited) — fall back to the recorded state
            live_state = training_status.get("state", "idle")
    else:
        live_state = training_status.get("state", "idle")

    current_epoch = None
    epochs_target = training_status.get("epochs_target")
    current_loss = None
    current_val_acc = None
    if live_state == "running":
        for line in reversed(log_tail):
            m = re.search(
                r"epoch\s+(\d+)/(\d+)\s+loss=([\d.]+).*?val_acc=([\d.]+)",
                line,
            )
            if m:
                current_epoch = int(m.group(1))
                if epochs_target is None:
                    epochs_target = int(m.group(2))
                current_loss = float(m.group(3))
                current_val_acc = float(m.group(4))
                break

    ml_enabled = _os.environ.get("STOCKS_ML_ENABLED", "0").strip() in {"1", "true", "True"}

    payload = {
        "ml_alpha": True,  # always — we're in alpha until validated
        "ml_enabled": ml_enabled,  # whether trades actually use the predictions
        "weights_present": weights_exists,
        "weights_age_seconds": weights_age_seconds,
        "best_val_acc": summary.get("best_val_acc"),
        "best_epoch": summary.get("best_epoch"),
        "n_train": summary.get("n_train"),
        "n_val": summary.get("n_val"),
        "n_tickers": summary.get("n_tickers"),
        "device": summary.get("device"),
        "history": (summary.get("history") or [])[-10:],
        "evolution": evolution[-5:],
        "log_tail": log_tail,
        # Compute the *actual* next firing of the Sunday-23:00-ET cron
        # relative to now. The cron expression alone (`0 23 * * 0`) was
        # ambiguous — operator saw "Sun 11 PM ET" on Monday morning after
        # the Sunday run had already completed, and read it as "still
        # waiting for tonight's run".
        "next_train_cron": _next_sun_23_et_iso(),
        "next_train_cron_expr": "0 23 * * 0  (Sun 11 PM ET)",
        # Live training progress — populated whenever a worker is running.
        "training_state": live_state,
        "training_pid": live_pid,
        "training_started_at": training_status.get("started_at"),
        "training_finished_at": training_status.get("finished_at"),
        "training_elapsed_seconds": training_status.get("elapsed_seconds"),
        "current_epoch": current_epoch,
        "epochs_target": epochs_target,
        "current_loss": current_loss,
        "current_val_acc": current_val_acc,
    }

    if live_state == "running":
        # Reflect "training in progress" prominently rather than hiding behind
        # the "no weights yet" degraded state.
        return _envelope("ok", data=payload)
    if not weights_exists:
        return _envelope(
            "degraded",
            data=payload,
            error="No trained model yet — first training fires Sunday 11 PM ET.",
        )
    if weights_age_seconds is not None and weights_age_seconds > 14 * 86400:
        return _envelope("degraded", data=payload,
                         error=f"Model is {weights_age_seconds // 86400}d old — retraining stale.")
    return _envelope("ok", data=payload)


@router.get("/stock_regime")
async def stock_regime():
    """SPY-driven simple regime classifier for the dashboard.

    Reads cached SPY 1Day bars (cron-fed every 5min during market hours),
    computes 50-day MA, 200-day MA, and 5-day return, then classifies into
    one of {trending_up, trending_down, mean_reverting, high_volatility}.

    No training required — pure rule-based, refreshes whenever the
    `wheel_candles` cron runs. Pairs visually with the BTC HMM regime card
    so the operator sees both markets at a glance.
    """
    f = STOCKS_ROOT / "wheel" / "state" / "candles_SPY_1Day.json"
    raw = _read_json(f)
    if not raw:
        return _envelope("down", error="SPY daily candles missing — run `python -m wheel.cli candles SPY --timeframe 1Day`")
    bars = raw.get("bars") or []
    if len(bars) < 200:
        return _envelope("degraded", data={"current": None}, error=f"only {len(bars)} SPY bars (need ≥200 for 200d MA)")

    closes = [float(b["close"]) for b in bars]
    spot = closes[-1]
    ma50 = sum(closes[-50:]) / 50
    ma200 = sum(closes[-200:]) / 200
    return_5d_pct = (closes[-1] / closes[-6] - 1.0) * 100 if len(closes) >= 6 else 0.0

    # Realized 21-day vol (annualized) for the high-vol branch
    import math
    rets = [(closes[i] / closes[i-1] - 1.0) for i in range(max(1, len(closes)-21), len(closes))]
    if rets:
        mean = sum(rets) / len(rets)
        var = sum((r - mean) ** 2 for r in rets) / len(rets)
        realized_vol_pct = math.sqrt(var) * math.sqrt(252) * 100
    else:
        realized_vol_pct = 0.0

    # Classification
    above_50 = spot > ma50
    above_200 = spot > ma200
    golden = ma50 > ma200  # bullish trend structure
    death = ma50 < ma200   # bearish trend structure

    if realized_vol_pct > 35:
        regime = "high_volatility"
        confidence = min(1.0, realized_vol_pct / 50)
    elif golden and above_200 and return_5d_pct > 0.5:
        regime = "trending_up"
        # confidence scales with how far above the structure is
        spread_pct = (ma50 / ma200 - 1.0) * 100
        confidence = min(1.0, 0.5 + spread_pct / 10)
    elif death and not above_200 and return_5d_pct < -0.5:
        regime = "trending_down"
        spread_pct = (ma200 / ma50 - 1.0) * 100
        confidence = min(1.0, 0.5 + spread_pct / 10)
    else:
        regime = "mean_reverting"
        # higher confidence when price is near both MAs
        dist_50 = abs(spot - ma50) / ma50 * 100
        confidence = max(0.3, 1.0 - dist_50 / 5)

    age = _file_age_seconds(f)
    payload = {
        "current": regime,
        "probability": round(confidence, 4),
        "spot": round(spot, 2),
        "ma_50": round(ma50, 2),
        "ma_200": round(ma200, 2),
        "return_5d_pct": round(return_5d_pct, 2),
        "realized_vol_21d_pct": round(realized_vol_pct, 2),
        "structure": "golden_cross" if golden else "death_cross",
        "above_200d": above_200,
        "data_age_seconds": age,
        "bars": len(bars),
        "model": "spy_simple_v1",
    }
    if age is not None and age > 86400:
        return _envelope("degraded", data=payload, error=f"SPY data {age}s old")
    return _envelope("ok", data=payload)
