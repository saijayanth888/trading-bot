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
from .data_sources import fetch_coinbase_candles
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

    Three trust tiers, all on private/non-routable address space:
      1. Loopback (127.0.0.1, ::1) — same machine
      2. RFC1918 private (10/8, 172.16/12, 192.168/16) — operator's LAN
         + docker bridge gateways. Operator binds 0.0.0.0:8081 so the
         home LAN can reach the dashboard; the LAN router is the auth
         perimeter here.
      3. Tailscale CGNAT (100.64.0.0/10, RFC6598) — operator's tailnet.
         Tailscale's WireGuard control plane is the auth perimeter; only
         devices the operator added to their tailnet can present these
         addresses to us.

    NOT trusted: public addresses. A reverse proxy on a public interface
    would NOT match any of the three ranges → bearer required, correctly.
    """
    if not client_host:
        return False
    if client_host in ("127.0.0.1", "::1", "::ffff:127.0.0.1"):
        return True
    try:
        import ipaddress
        addr_str = client_host[7:] if client_host.startswith("::ffff:") else client_host
        addr = ipaddress.ip_address(addr_str)
        # Tailscale CGNAT range — not RFC1918 so ipaddress.is_private returns
        # False, but trust-equivalent to RFC1918 for a private mesh network.
        if addr in ipaddress.ip_network("100.64.0.0/10"):
            return True
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
# /api/ops/uptime — dashboard uptime for the SPA topbar
# --------------------------------------------------------------------------
# Post-2026-05-14: freqtrade decommissioned, so the "BOT UP" pill that read
# the freqtrade Bot-heartbeat line from rotated logs is gone. Dashboard
# tracks its own start via _DASHBOARD_START_TS at module import. Quanta-core
# liveness is surfaced via _quanta_core_probe in /api/ops/services instead.

import time as _uptime_time
_DASHBOARD_START_TS = _uptime_time.time()


@router.get("/uptime")
async def uptime():
    """Dashboard start timestamp + computed uptime seconds for the topbar."""
    now = int(_uptime_time.time())
    out: dict[str, Any] = {
        "now": now,
        "dashboard": {
            "started_at": int(_DASHBOARD_START_TS),
            "uptime_s": now - int(_DASHBOARD_START_TS),
        },
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
# /api/ops/training_health — per-pair model.zip validation status
# --------------------------------------------------------------------------

TRAINING_HEALTH_STALE_HOURS = float(
    os.environ.get("OPS_TRAINING_HEALTH_STALE_HOURS", "72")
)


def _training_health_payload(identifier: str = "tft_v1") -> dict[str, Any]:
    # Post-Phase-4 cutover (2026-05-13): the FreqAI ``freqaimodels`` package
    # was retired and `user_data/models/<id>/pair_dictionary.json` is no
    # longer written by quanta-core. Until a quanta-core training-health
    # producer is wired (Wave D), return an empty-pairs envelope so the UI
    # renders a clean "NO PAIRS YET" state instead of "endpoint unavailable".
    scan: dict[str, dict[str, Any]] = {}

    # Read strategy_overrides.tft_blind_fallback so the dashboard can
    # show whether the operator has opted in. We surface BOTH the
    # per-pair eligibility (status != ok OR stale) AND the global
    # enabled flag so the chip in the UI can disambiguate:
    #   - eligible & enabled  → fallback path is RUNNING for that pair
    #   - eligible & disabled → pair is DARK (no signal)
    blind_enabled = False
    blind_multiplier = 0.5
    try:
        with open(CONFIG_PATH) as _fp:
            _cfg = json.load(_fp)
        _so = (_cfg.get("strategy_overrides", {}) or {})
        _block = (_so.get("tft_blind_fallback", {}) or {})
        blind_enabled = bool(_block.get("enabled", False))
        blind_multiplier = float(_block.get("position_size_multiplier", 0.5))
    except Exception:    # noqa: BLE001
        # Config-read failures are non-fatal — fall back to eligibility only.
        pass

    now = datetime.now(timezone.utc)
    rows: list[dict[str, Any]] = []
    for pair, info in sorted(scan.items()):
        zip_path_str = info.get("zip_path")
        zip_path = Path(zip_path_str) if zip_path_str else None
        validate_info = info.get("info") or {}

        trained_ts = int(info.get("trained_ts") or 0)
        file_mtime: int | None = None
        size_bytes: int = int(validate_info.get("size_bytes") or 0)
        if zip_path and zip_path.exists():
            try:
                st = zip_path.stat()
                file_mtime = int(st.st_mtime)
                if not size_bytes:
                    size_bytes = int(st.st_size)
            except OSError:
                pass

        last_train_ts = file_mtime or trained_ts or 0
        age_hours: float | None = None
        if last_train_ts:
            age_hours = round((now.timestamp() - last_train_ts) / 3600.0, 2)
        last_train_iso = (
            datetime.fromtimestamp(last_train_ts, timezone.utc).isoformat()
            if last_train_ts else None
        )

        status = info["status"]
        stale = bool(
            status == "ok"
            and age_hours is not None
            and age_hours > TRAINING_HEALTH_STALE_HOURS
        )
        if stale:
            status = "stale"

        # TFT-blind fallback eligibility — the pair would be running on
        # the BollingerRSI MR signal at degraded sizing right now IF the
        # operator has flipped strategy_overrides.tft_blind_fallback to
        # enabled=true. "Eligible" = the TFT path is unavailable for this
        # pair (quarantine status OR > 72h stale).
        blind_eligible = status in ("stub", "missing", "error", "stale")
        blind_active = bool(blind_eligible and blind_enabled)

        rows.append({
            "pair": pair,
            "status": status,
            "reason": info.get("reason"),
            "last_train_ts": last_train_ts or None,
            "last_train_iso": last_train_iso,
            "zip_size_bytes": size_bytes or None,
            "has_metadata_json": validate_info.get("has_data_pkl"),
            "has_data_pkl": validate_info.get("has_data_pkl"),
            "tensor_blobs": validate_info.get("tensor_blobs") or 0,
            "age_hours": age_hours,
            "stale": stale,
            # Fix 6: TFT-blind fallback indicators.
            "tft_blind_eligible": blind_eligible,
            "tft_blind_active": blind_active,
        })

    counts = {"ok": 0, "stub": 0, "missing": 0, "stale": 0, "error": 0}
    for r in rows:
        counts[r["status"]] = counts.get(r["status"], 0) + 1

    return {
        "identifier": identifier,
        "stale_hours_threshold": TRAINING_HEALTH_STALE_HOURS,
        "counts": counts,
        "pairs": rows,
        # Fix 6: surface the global fallback config so the UI can render
        # the correct chip text (ACTIVE vs DARK) for each eligible row
        # and emit a banner when fallback is enabled.
        "tft_blind_fallback": {
            "enabled": blind_enabled,
            "position_size_multiplier": blind_multiplier,
            "eligible_count": sum(1 for r in rows if r["tft_blind_eligible"]),
            "active_count": sum(1 for r in rows if r["tft_blind_active"]),
        },
    }


@router.get("/training_health")
async def training_health(identifier: str = "tft_v1"):
    try:
        loop = asyncio.get_running_loop()
        result = await asyncio.wait_for(
            loop.run_in_executor(None, _training_health_payload, identifier),
            timeout=ENDPOINT_TIMEOUT_S,
        )
    except asyncio.TimeoutError:
        return _envelope("down", error="training_health scan timed out")
    except Exception as exc:
        logger.exception("training_health payload build failed")
        return _envelope("down", error=str(exc))

    counts = result.get("counts", {})
    bad = counts.get("stub", 0) + counts.get("missing", 0) + counts.get("error", 0)
    stale = counts.get("stale", 0)
    if bad > 0:
        status = "degraded"
        err = (f"{bad} pair(s) quarantined "
               f"(stub={counts.get('stub',0)}, missing={counts.get('missing',0)})")
    elif stale > 0:
        status = "degraded"
        err = f"{stale} pair(s) > {TRAINING_HEALTH_STALE_HOURS:.0f}h stale"
    elif not result.get("pairs"):
        # Post-Phase-4 cutover: FreqAI's `pair_dictionary.json` is no
        # longer written. Return "ok" with empty pairs so the dashboard
        # card stops flashing red — the UI's empty-state copy explains
        # the retired-producer state.
        status = "ok"
        err = None
    else:
        status = "ok"
        err = None
    return _envelope(status, data=result, error=err)


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

    # Per-source breakdown — surface which sources contributed to this row
    # so the operator can see at a glance whether reddit/HN/StockTwits are
    # live or silently dark. `sources_ok` is jsonb (list of source names);
    # `sources_failed` is jsonb (list of [name, error] pairs).
    sources_ok_raw = latest.get("sources_ok") or []
    sources_failed_raw = latest.get("sources_failed") or []
    sources_ok: list[str] = [str(s) for s in sources_ok_raw] if isinstance(sources_ok_raw, list) else []
    sources_failed: list[str] = []
    if isinstance(sources_failed_raw, list):
        for entry in sources_failed_raw:
            if isinstance(entry, list) and entry:
                sources_failed.append(str(entry[0]))
            elif isinstance(entry, str):
                sources_failed.append(entry)

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
            # Per-source breakdown — answers "which sources are alive right now?"
            "sources_ok": sources_ok,
            "sources_failed": sources_failed,
            "n_reddit": int(latest.get("n_reddit") or 0),
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
    """Postgres-derived risk numbers + open positions from trade_journal.

    Post-2026-05-14: freqtrade /api/v1/status is gone; quanta-core writes
    every paper-fill into trade_journal, so open positions come from
    ops_db.open_positions() (used by _quanta_open_positions in app.py).
    """
    try:
        loop = asyncio.get_running_loop()
        db_data = await asyncio.wait_for(
            loop.run_in_executor(None, ops_db.trades_risk_summary),
            timeout=ENDPOINT_TIMEOUT_S,
        )
    except asyncio.TimeoutError:
        return _envelope("down", error="trades_risk timed out")
    except Exception as exc:
        logger.exception("trades_risk failed")
        return _envelope("down", error=str(exc))

    try:
        open_trades = ops_db.open_positions(limit=50)
    except Exception:
        logger.debug("open_positions fetch failed", exc_info=True)
        open_trades = []
    open_count = len(open_trades) if isinstance(open_trades, list) else 0

    return _envelope(
        "ok",
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
    )


# --------------------------------------------------------------------------
# Mutating: /api/ops/pause + /api/ops/resume
# --------------------------------------------------------------------------


def _run_state_set(*, paused: bool, reason: str | None, set_by: str) -> dict[str, Any]:
    """UPSERT quanta_schema.run_state. Single source of truth for the V4
    runner's kill switch — `run_v4_shadow.py` reads `paused` at the top
    of each cycle and short-circuits proposal/order generation when True.
    """
    from . import ops_db
    if not ops_db._HAVE_PG:
        raise RuntimeError("postgres unavailable")
    with ops_db._connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            UPDATE quanta_schema.run_state
               SET paused        = %s,
                   paused_reason = %s,
                   paused_at     = CASE WHEN %s THEN NOW() ELSE NULL END,
                   set_by        = %s,
                   updated_at    = NOW()
             WHERE id = 1
            RETURNING paused, paused_reason, paused_at, set_by, updated_at
            """,
            (paused, reason, paused, set_by),
        )
        row = cur.fetchone()
        conn.commit()
    if not row:
        raise RuntimeError("run_state row missing (migration 003 not applied?)")
    return {
        "paused": row["paused"],
        "paused_reason": row["paused_reason"],
        "paused_at": row["paused_at"].isoformat() if row["paused_at"] else None,
        "set_by": row["set_by"],
        "updated_at": row["updated_at"].isoformat() if row["updated_at"] else None,
    }


@router.post("/pause", dependencies=[Depends(require_mcp_key)])
async def pause(request: Request):
    """Pause V4 trading. Post-cutover: writes to quanta_schema.run_state
    instead of POSTing to dead freqtrade. The V4 runner reads run_state
    on every cycle; new BUY proposals are skipped while paused. Existing
    positions stay open (use the kill switch to flatten)."""
    body = await request.json() if request.headers.get("content-length") else {}
    note = body.get("reason", "ops-tab manual pause")
    set_by = body.get("set_by", "dashboard")
    try:
        state = _run_state_set(paused=True, reason=note, set_by=set_by)
    except Exception as exc:
        logger.exception("pause failed")
        raise HTTPException(status_code=502, detail=f"run_state write failed: {exc}")
    return _envelope("ok", data={"run_state": state, "reason": note})


# --------------------------------------------------------------------------
# /api/ops/sparklines — per-pair last-N close prices for tiny inline charts
# --------------------------------------------------------------------------

DEFAULT_PAIRS = [p.strip() for p in os.environ.get(
    "DASHBOARD_PAIRS", "BTC/USD,ETH/USD,SOL/USD,ADA/USD",
).split(",") if p.strip()]


@router.get("/sparklines")
async def sparklines(timeframe: str = "5m", limit: int = 288):
    """Per-pair compact close-price arrays + 24h % change.

    Used by the Ops trades panel to render small inline price sparklines.
    Default `limit=288` gives true 24h coverage at 5m (288 = 24 × 12);
    bump cap to 500 so 1h timeframe (24h = 24 candles) and 1m (24h = 1440)
    can be requested explicitly. Coinbase REST fallback covers up to 300
    candles per public call.
    """
    limit = max(10, min(500, int(limit)))
    if timeframe not in ("1m", "5m", "15m", "1h", "6h"):
        timeframe = "5m"

    async def _one(pair: str):
        # Post-2026-05-14: freqtrade decommissioned; coinbase public REST
        # is the only source.
        try:
            df = await fetch_coinbase_candles(pair, timeframe=timeframe, limit=limit)
        except Exception:
            df = None
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
        error=None if has_any else "no candle data returned by coinbase",
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
    # Hours a regime must persist before the strategy acts on it.
    # 0 = no gate; 24 = full-day cooldown. Strategy default is 2.0h.
    "regime_min_stable_hours":    (0.0, 24.0),
}

CONFIG_PATH = Path(os.environ.get(
    "FREQTRADE_CONFIG_PATH",
    "/app/user_data/config.json",
))

# Same root the strategy uses; we drop config-backup-*.json snapshots here.
USER_DATA_ROOT_FOR_BACKUPS = Path(os.environ.get(
    "USER_DATA_ROOT",
    "/app/user_data",
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
      5. Quanta-core / unified_risk re-reads on its next cycle (no reload call needed).

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

    # Post-2026-05-14: freqtrade /api/v1/reload_config is gone. Quanta-core
    # re-reads regime params at the top of each cycle from the same
    # config.json on disk; no service-side reload call is needed.
    return _envelope("ok", data={
        "changes": diffs,
        "backup": str(backup_path),
        "note": "Quanta-core re-reads regime params on its next cycle. "
                "Some params (entry/exit deltas) take effect on the next "
                "candle; trail distance / take-profit affect new positions only.",
    })


# --------------------------------------------------------------------------
# Risk gates editor: GET / POST /api/ops/risk_gates
# --------------------------------------------------------------------------
#
# Mirrors the regime_config pattern above. Defaults live in
# ``user_data/modules/unified_risk.py:_RISK_GATE_DEFAULTS`` and are also the
# baked-in fallback when the block is absent from config.json.
#
# The allowlist below is the operator-approved set (2026-05-11). The ranges
# bracket each key's defensible bounds — pick a tight band, the operator can
# loosen it later with a code change if a real use case appears.

_RISK_GATE_RANGES = {
    "daily_loss_halt_pct":      (0.0, 0.20),   # 0% – 20% daily DD halt
    "weekly_loss_size_cut_pct": (0.0, 0.30),   # 0% – 30% weekly DD trigger
    "weekly_loss_size_factor":  (0.0, 1.0),    # 0× (halt) – 1× (no cut)
    "single_name_cap_pct":      (0.0, 0.50),   # 0% – 50% of equity
    "correlation_cap":          (0.0, 1.0),    # corr is bounded [-1,1]; we
                                               # only block when too-similar
    "vix_high_multiplier":      (1.0, 5.0),    # 1× – 5× historical VIX
    "vix_high_min_size_factor": (0.0, 1.0),    # min size when VIX is hot
}


@router.get("/risk_gates")
def risk_gates_get():
    """Return the current risk_gates block + the schema (ranges) for the UI."""
    try:
        cfg = json.loads(CONFIG_PATH.read_text())
        rg = cfg.get("risk_gates") or {}
    except Exception as exc:
        return _envelope("down", error=str(exc))

    # Live-resolved values (defaults overlaid with whatever's in config.json).
    # Lets the UI distinguish between "operator set this" and "fallback default".
    try:
        from user_data.modules.unified_risk import _load_risk_gates, _RISK_GATE_DEFAULTS
        resolved = _load_risk_gates()
        defaults = dict(_RISK_GATE_DEFAULTS)
    except Exception as exc:
        logger.warning("risk_gates_get: unified_risk import failed: %s", exc)
        resolved = {k: v for k, v in rg.items() if not k.startswith("_")}
        defaults = {}

    return _envelope("ok", data={
        "risk_gates": {k: v for k, v in rg.items() if not k.startswith("_")},
        "resolved": resolved,
        "defaults": defaults,
        "schema": {
            "ranges": {k: list(v) for k, v in _RISK_GATE_RANGES.items()},
        },
        "config_path": str(CONFIG_PATH),
    })


@router.post("/risk_gates", dependencies=[Depends(require_mcp_key)])
async def risk_gates_post(request: Request):
    """Validate + atomically write the new risk_gates block.

    Body must be ``{"risk_gates": {...}}`` matching the existing shape.
    We:
      1. Accept only known keys; reject extras.
      2. Validate each value against its sanity range.
      3. Snapshot the old config to ``user_data/data/config-backup-<ts>.json``.
      4. Atomic-write the new config (tmp + rename) — rolls back to the
         snapshot if validation throws after the snapshot is taken.
      5. Quanta-core / unified_risk re-reads on its next cycle (no reload call needed).

    Returns the diff in the envelope so the frontend can confirm.
    """
    body = await request.json() if request.headers.get("content-length") else {}
    new_rg = body.get("risk_gates")
    if not isinstance(new_rg, dict):
        raise HTTPException(status_code=400, detail="body.risk_gates must be a dict")

    # Load current config + the existing risk_gates
    try:
        cfg_text = CONFIG_PATH.read_text()
        cfg = json.loads(cfg_text)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"could not read {CONFIG_PATH}: {exc}")
    current = cfg.get("risk_gates") or {}

    # Validate every submitted key/value against the allowlist.
    diffs: list[str] = []
    for key, value in new_rg.items():
        if key.startswith("_"):
            continue  # never overwrite documentation keys
        if key not in _RISK_GATE_RANGES:
            raise HTTPException(status_code=400, detail=f"unknown risk_gate: {key}")
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise HTTPException(status_code=400, detail=f"{key} must be a number")
        lo, hi = _RISK_GATE_RANGES[key]
        if not (lo <= value <= hi):
            raise HTTPException(
                status_code=400,
                detail=f"{key}={value} outside allowed range [{lo}, {hi}]",
            )
        old = current.get(key)
        if old != value:
            diffs.append(f"{key}: {old} → {value}")

    if not diffs:
        return _envelope("ok", data={"changes": [], "note": "no-op (values unchanged)"})

    # Build the new config (preserve "_doc" and any extras we didn't touch)
    merged = dict(current)
    for k, v in new_rg.items():
        if k.startswith("_"):
            continue
        merged[k] = v
    cfg["risk_gates"] = merged

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

    # Atomic write: tmp file + rename. If anything between snapshot and
    # final rename throws, restore the original cfg_text from memory so
    # config.json never ends up in a half-written state on disk.
    try:
        tmp = CONFIG_PATH.with_suffix(CONFIG_PATH.suffix + ".tmp")
        tmp.write_text(json.dumps(cfg, indent=4))
        tmp.replace(CONFIG_PATH)
    except Exception as exc:
        # Defensive rollback: re-write the pre-edit text we already had in memory.
        try:
            CONFIG_PATH.write_text(cfg_text)
        except Exception:
            pass
        raise HTTPException(status_code=500, detail=f"atomic write failed: {exc}")

    # Post-2026-05-14: freqtrade /api/v1/reload_config is gone. unified_risk
    # reads config.json on each evaluation, so the new values take effect
    # on the next trading-loop tick without any service-side reload call.
    return _envelope("ok", data={
        "changes": diffs,
        "backup": str(backup_path),
        "note": "Risk gates take effect on the next trading-loop tick — "
                "unified_risk.py reads config.json on each evaluation, "
                "no process restart required.",
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
    # ops_db.trades_risk_summary returns drawdown_pct_30d as a fraction
    # (e.g. -0.012 = -1.2%); convert to percent for the threshold check
    # and the human-readable error string.
    #
    # Post-cutover (paper mode): the historical drawdown metric is
    # dominated by legacy freqtrade fills with stale risk semantics.
    # Skip the drawdown gate for paper mode; circuit breaker still
    # applies. `force=true` in body bypasses both gates (operator escape).
    loop = asyncio.get_running_loop()
    risk = await loop.run_in_executor(None, ops_db.trades_risk_summary)
    dd_pct = (risk.get("drawdown_pct_30d") or 0) * 100
    force = bool(body.get("force"))
    is_paper = _v4_is_active_engine()  # paper-mode paper-fill simulator
    if not force and not is_paper and dd_pct < -6.0:
        raise HTTPException(status_code=409, detail=f"resume refused: 30d max drawdown {dd_pct:.1f}% (limit -6%)")
    if not force and risk.get("circuit_breaker", {}).get("active"):
        raise HTTPException(status_code=409, detail="resume refused: circuit breaker active")

    # Post-cutover: signal V4 runner via quanta_schema.run_state. The
    # legacy freqtrade /api/v1/start path is dead (container retired).
    try:
        rs_state = _run_state_set(
            paused=False, reason=None,
            set_by=body.get("set_by", "dashboard"),
        )
    except Exception as exc:
        logger.exception("resume failed")
        raise HTTPException(status_code=502, detail=f"run_state write failed: {exc}")
    payload = {"run_state": rs_state}

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
        "run_state_response": payload,
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
    # Engine
    "LIVE_ENGINE_MODE",
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

    # Daily P&L pct buckets → annualised Sharpe (× √365). pnl_pct is
    # fractional (-0.0123 = -1.23%); Sharpe = mean/std is scale-invariant
    # so the result is the same whether we feed fractions or percents.
    # Thresholds in _READINESS_MODES mirror scripts/validate_readiness.py.
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

    # Per-pair daily PnL buckets → annualised rolling Sharpe. pnl_pct is
    # fractional; Sharpe is scale-invariant in pnl_pct units, so the
    # config-level threshold (capital_allocation.min_sharpe_for_trading,
    # currently 0.7) compares apples-to-apples regardless of unit choice.
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
    # Post-2026-05-14: freqtrade decommissioned; coinbase is the only source.
    try:
        df = await fetch_coinbase_candles(pair, timeframe=timeframe, limit=limit)
    except Exception:
        df = None
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
            # H-1 audit fix: NEVER SUM(pnl_pct) — it's per-trade fractional
            # return on each trade's own stake. Summing 50 fills × ~5%
            # each yields "+250%" for a $14 day. The denominator must be
            # the day's starting portfolio equity (see ops_db.py:337-342).
            cur.execute(
                """
                SELECT
                    COALESCE(SUM(pnl), 0)                               AS pnl_usd,
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

            # H-1: day-start equity. Prefer an explicit snapshot from
            # quanta_schema.equity_snapshots if the table exists; fall
            # back to the algebraic identity used in combined_portfolio
            # (start = current_total - daily_pnl_usd).
            day_start_equity: float | None = None
            try:
                cur.execute(
                    """
                    SELECT equity
                    FROM quanta_schema.equity_snapshots
                    WHERE ts >= date_trunc('day', NOW() AT TIME ZONE 'UTC')
                    ORDER BY ts ASC
                    LIMIT 1
                    """
                )
                _row = cur.fetchone()
                if _row and _row.get("equity") is not None:
                    day_start_equity = float(_row["equity"])
            except Exception as _exc:
                # Table may not exist on this deployment — that's fine.
                logger.debug("slack_preview: equity_snapshots probe failed: %s", _exc)
    except Exception as exc:
        logger.exception("slack_preview db failed")
        return _envelope("down", error=str(exc))

    perf = await loop.run_in_executor(None, mcp_local.get_performance_metrics)

    n = int(today.get("trades") or 0)
    pnl_usd = float(today.get("pnl_usd") or 0)
    wins = int(today.get("wins") or 0)
    losses = int(today.get("losses") or 0)
    win_rate = (wins / n * 100) if n else 0.0

    # H-1: compute day P&L percent honestly.
    #   pnl_pct = pnl_usd / day_start_equity × 100
    # When we can't establish day_start_equity, surface None — the UI
    # then renders dollars-only. Better than a fictional number.
    pnl_pct: float | None = None
    if day_start_equity is None:
        # Fallback: combined_portfolio algebra (start = current − pnl).
        try:
            import sys as _sys
            repo_root = STOCKS_ROOT.parent
            if str(repo_root) not in _sys.path:
                _sys.path.insert(0, str(repo_root))
            from user_data.modules.unified_risk import get_combined_risk_status
            _status = await loop.run_in_executor(None, get_combined_risk_status)
            _total_equity = float((_status or {}).get("total_equity") or 0)
            if _total_equity > 0:
                day_start_equity = _total_equity - pnl_usd
        except Exception as _exc:
            logger.debug("slack_preview: combined_portfolio fallback failed: %s", _exc)

    if day_start_equity is not None and day_start_equity > 0:
        pnl_pct = (pnl_usd / day_start_equity) * 100.0

    best = per_pair[0] if per_pair else None
    worst = per_pair[-1] if per_pair and len(per_pair) > 1 else None

    return _envelope("ok", data={
        "date_utc": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        # Keep `pnl_usd` and the new explicit aliases. day_pnl_* mirror
        # combined_portfolio's contract so the Slack preview card and
        # the hero card render the same numbers.
        "pnl_usd": pnl_usd,
        "pnl_pct": pnl_pct,
        "day_pnl_usd": pnl_usd,
        "day_pnl_pct": pnl_pct,
        "day_start_equity": day_start_equity,
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

STOCKS_ROOT = Path(os.environ.get("STOCKS_ROOT", "/app/stocks"))


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


def _parse_shark_phase_decisions(handoff_file: Path) -> dict:
    """Parse `stocks/memory/DAILY-HANDOFF.md` into per-symbol shark decisions.

    The file is the source of truth for what every shark phase decided
    today — pre-market scan, pre-execute validation, market-open trades,
    midday/EOD reviews. Each phase emits a small set of comma-separated
    symbol lists keyed by status (``confirmed:``, ``skipped:``,
    ``validated:``, ``rejected:``, ``traded:``, ``cuts:``).

    Returns::

        {
            "briefing_date": "2026-05-14" | None,
            "missing":     bool,        # file not found
            "stale_date":  None | "YYYY-MM-DD",  # file is for a prior day
            "by_symbol": {
                "NVDA": {
                    "pass": True | False | None,
                    "detail": "confirmed by pre-market; validated by pre-execute",
                    "phases": ["confirmed@pre-market", "validated@pre-execute"],
                },
                ...
            },
        }

    Decision rules (most positive wins, then most negative, then neutral):
      * pass=True   → symbol appears in ``confirmed``, ``validated``,
                      or ``traded`` of any phase today.
      * pass=False  → only in ``skipped``, ``rejected``, or ``cuts``.
      * pass=None   → mentioned only in ambiguous context, or not at all.

    The function is defensive: any parse error returns a useful default
    instead of raising, so a malformed handoff doesn't 500 the endpoint.
    """
    import re
    today_iso = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    result: dict = {
        "briefing_date": None,
        "missing": True,
        "stale_date": None,
        "by_symbol": {},
    }
    try:
        if not handoff_file.is_file():
            return result
        text = handoff_file.read_text()
    except OSError as exc:
        logger.warning("shark decisions: read failed %s: %s", handoff_file, exc)
        return result
    result["missing"] = False

    # Title line: ``# Daily Handoff — 2026-05-14``
    title_match = re.search(r"#\s*Daily Handoff[^0-9]*(\d{4}-\d{2}-\d{2})", text)
    if title_match:
        result["briefing_date"] = title_match.group(1)
        if result["briefing_date"] != today_iso:
            result["stale_date"] = result["briefing_date"]
            # Still parse it — operator may want yesterday's decision as a hint
            # when today's phase hasn't run yet — but the gate detail will
            # flag the date so it's not mistaken for fresh info.

    # Split into phase sections — each starts with a "## <name> | HH:MM" line.
    phase_pattern = re.compile(r"^##\s+([\w\-]+)\b", re.MULTILINE)
    phase_starts = [(m.start(), m.group(1)) for m in phase_pattern.finditer(text)]
    if not phase_starts:
        return result
    phase_starts.append((len(text), None))

    # Status → polarity. Keys are case-insensitive.
    pos_keys = {"confirmed", "validated", "traded"}
    neg_keys = {"skipped", "rejected", "cuts"}
    all_keys = pos_keys | neg_keys

    # Aggregate per-symbol decisions.
    by_symbol: dict[str, dict] = {}
    for i in range(len(phase_starts) - 1):
        start, phase_name = phase_starts[i]
        end, _ = phase_starts[i + 1]
        chunk = text[start:end]
        for line in chunk.splitlines():
            line = line.strip()
            if ":" not in line:
                continue
            key, _, payload = line.partition(":")
            key = key.strip().lower()
            if key not in all_keys:
                continue
            payload = payload.strip()
            if not payload or payload.lower() in {"none", "n/a", "-", "—"}:
                continue
            # Symbols are comma-separated tickers. Drop tokens that don't
            # look like a stock symbol (1-5 alnum upper) to avoid picking
            # up free-form text after a colon.
            for token in payload.split(","):
                sym = token.strip().upper()
                if not re.fullmatch(r"[A-Z]{1,5}", sym):
                    continue
                entry = by_symbol.setdefault(sym, {"pos": [], "neg": []})
                tag = f"{key}@{phase_name}"
                if key in pos_keys:
                    entry["pos"].append(tag)
                else:
                    entry["neg"].append(tag)

    # Collapse pos/neg into a single decision per symbol.
    decided: dict[str, dict] = {}
    for sym, e in by_symbol.items():
        pos = e["pos"]
        neg = e["neg"]
        if pos and not neg:
            decided[sym] = {
                "pass": True,
                "detail": "; ".join(pos),
                "phases": pos,
            }
        elif neg and not pos:
            decided[sym] = {
                "pass": False,
                "detail": "; ".join(neg),
                "phases": neg,
            }
        elif pos and neg:
            # Most recent phase wins (phases appear in file order top→down).
            # The last tag in either list is from the latest phase that
            # mentioned the symbol.
            last_pos_idx = max(_idx_of(by_symbol[sym]["pos"][-1], phase_starts), -1)
            last_neg_idx = max(_idx_of(by_symbol[sym]["neg"][-1], phase_starts), -1)
            if last_pos_idx >= last_neg_idx:
                decided[sym] = {
                    "pass": True,
                    "detail": "; ".join(pos + neg),
                    "phases": pos + neg,
                }
            else:
                decided[sym] = {
                    "pass": False,
                    "detail": "; ".join(pos + neg),
                    "phases": pos + neg,
                }
    result["by_symbol"] = decided
    return result


def _idx_of(tag: str, phase_starts) -> int:
    """Return the file-order index of the phase referenced by ``tag``.

    Helper for `_parse_shark_phase_decisions`. ``tag`` is ``"<status>@<phase>"``;
    we want the position of ``<phase>`` in ``phase_starts``.
    """
    phase_name = tag.split("@", 1)[1] if "@" in tag else tag
    for idx, (_, name) in enumerate(phase_starts):
        if name == phase_name:
            return idx
    return -1


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

    # ── Wheel position roll-ups (FIX-I bug 3 — 2026-05-14) ──────────────
    # Earlier the wheel block only exposed `open_positions` (the per-row
    # list) and `cumulative_pnl_usd` (a sum across the ledger). Several
    # dashboard cards consume rolled-up KPIs (open CSP count, total
    # collateral parked, premium booked from open positions) and were
    # falling back to zeros because they had no field to read. These
    # roll-ups are derived purely from positions.json — same source of
    # truth as `open_positions` — so adding them here keeps the API
    # consistent rather than forcing every consumer to re-aggregate.
    open_csps = sum(1 for p in raw_positions if p.get("kind") == "short_put")
    open_ccs = sum(1 for p in raw_positions if p.get("kind") == "short_call")
    # Cash-secured-put collateral = strike × 100 × |qty|. Alpaca enforces
    # this against options_buying_power on CSP submits — exact same math
    # as wheel/runner.py's pre-flight collateral check.
    open_collateral_usd = round(sum(
        float(p.get("strike") or 0.0) * 100.0 * abs(int(p.get("qty") or 0))
        for p in raw_positions
        if p.get("kind") == "short_put"
    ), 2)
    # Premium collected on currently-open positions (NOT lifetime P&L —
    # that's `cumulative_pnl_usd` which sums the closed-trade ledger).
    premium_collected_usd = round(sum(
        float(p.get("entry_credit") or 0.0)
        for p in raw_positions
    ), 2)

    wheel = {
        "open_positions": wheel_positions,
        "open_csps": open_csps,
        "open_ccs": open_ccs,
        "open_collateral_usd": open_collateral_usd,
        "premium_collected_usd": premium_collected_usd,
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
    # Real age of the shark intraday content: prefer the JSON's own
    # `generated_at` field (truth of the producer) over mtime, which can
    # be moved forward by a touch/rewrite that didn't actually refresh
    # the data — exactly what we saw 2026-05-14 where mtime was fresh
    # but generated_at was still 2026-05-13 17:30 ET.
    shark_gen_iso = shark_raw.get("generated_at")
    shark_content_age: int | None = None
    if shark_gen_iso:
        try:
            # Tolerate naive (no tz) timestamps — fall back to UTC.
            _dt = datetime.fromisoformat(str(shark_gen_iso))
            if _dt.tzinfo is None:
                _dt = _dt.replace(tzinfo=timezone.utc)
            shark_content_age = int(datetime.now(timezone.utc).timestamp() - _dt.timestamp())
        except (ValueError, TypeError) as exc:
            logger.debug("stocks: failed to parse generated_at %r: %s", shark_gen_iso, exc)
    if shark_content_age is None:
        shark_content_age = _file_age_seconds(shark_data_file)

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
        "generated_at": shark_gen_iso,
        # Content age (from generated_at) — what the operator cares about.
        "age_seconds": shark_content_age,
        # File-mtime age, kept for backward compat / debugging.
        "file_age_seconds": _file_age_seconds(shark_data_file),
    }

    # ── Staleness thresholds (C-8 audit fix) ─────────────────────────────
    # The shark intraday content (regime, candidate counts, open trades)
    # refreshes every ~30 min when the shark cron is healthy; > 4 h means
    # something is wrong. Daily-summary stats (total_trades / win_rate /
    # current_drawdown_pct) only refresh once a day at 17:30 ET — those
    # tolerate 24 h staleness before degrading.
    INTRADAY_STALE_S = int(os.environ.get("STOCKS_INTRADAY_STALE_S", "14400"))   # 4 h
    DAILY_STALE_S    = int(os.environ.get("STOCKS_DAILY_STALE_S",    "86400"))   # 24 h

    # ── Status: degraded if any source is stale or missing ───────────────
    degraded_reasons: list[str] = []
    if alpaca["age_seconds"] is None:
        degraded_reasons.append("alpaca snapshot missing — run `python -m wheel.cli snapshot`")
    elif alpaca["age_seconds"] > DAILY_STALE_S:
        degraded_reasons.append(f"alpaca snapshot stale ({alpaca['age_seconds']}s old)")
    # NYSE-aware stale check: the shark intraday cache is refreshed by the
    # market_open / pre_execute crons that ONLY run during the regular
    # session (09:30-16:00 ET). After-hours / weekends / holidays the
    # cache is the last-session snapshot — flagging it "stale" misleads
    # the operator into chasing a non-issue. Only enforce the 4 h
    # threshold while NYSE is in the regular session.
    _nyse_open_now = _is_nyse_open_now()
    if shark["age_seconds"] is None:
        degraded_reasons.append("shark dashboard data missing")
    elif _nyse_open_now and shark["age_seconds"] > INTRADAY_STALE_S:
        degraded_reasons.append(
            f"shark intraday stale: generated_at={shark_gen_iso} "
            f"({shark['age_seconds']}s ago > {INTRADAY_STALE_S}s)"
        )
    if shark["circuit_breaker"]:
        degraded_reasons.append("shark circuit breaker tripped")
    if shark["kill_switch_active"]:
        degraded_reasons.append(f"shark kill switch: {shark['kill_switch_reason'] or 'active'}")

    # Top-level convenience fields so the UI can render an
    # "as-of HH:MM ET" badge without digging through shark.*.
    payload = {
        "alpaca": alpaca,
        "wheel": wheel,
        "shark": shark,
        "as_of_iso": shark_gen_iso,
        "age_seconds": shark_content_age,
        "intraday_stale_threshold_s": INTRADAY_STALE_S,
        "daily_stale_threshold_s": DAILY_STALE_S,
    }
    if not degraded_reasons:
        return _envelope("ok", data=payload)
    return _envelope("degraded", data=payload, error="; ".join(degraded_reasons))


# Whitelist of stock symbols the dashboard chart page is allowed to query.
# Keeps `/api/ops/stock_candles/{sym}` from being a generic Alpaca proxy.
_STOCK_SYMBOL_WHITELIST = {"SOFI", "AAPL", "TSLA", "NVDA", "META", "MSFT", "GOOGL", "AMZN", "MARA", "F", "PLTR", "AMD", "SPY", "MSTR", "COIN", "QQQ", "IWM", "HOOD"}

# Default basket for /api/ops/stocks_sparklines. Operator can override via the
# DASHBOARD_STOCK_SYMBOLS env var (comma-separated). Each symbol must also
# appear in _STOCK_SYMBOL_WHITELIST or it is filtered out at request time.
DEFAULT_STOCK_SYMBOLS = [
    s.strip().upper() for s in os.environ.get(
        # Operator-tuned default: 10 high-liquidity options-traded names so the
        # wheel/sparklines basket isn't fenced into one stock. Override via
        # DASHBOARD_STOCK_SYMBOLS in .env. Index: SPY. AI/tech: NVDA, AMD,
        # GOOGL, AAPL, TSLA. Fintech: SOFI, COIN. AI plays: PLTR, MSTR.
        "DASHBOARD_STOCK_SYMBOLS",
        "SOFI,PLTR,NVDA,AMD,SPY,TSLA,AAPL,GOOGL,MSTR,COIN,MARA,F,QQQ,IWM,HOOD",
    ).split(",") if s.strip()
]


def _stock_timeframe_minutes(tf: str) -> int:
    return {"1Min": 1, "5Min": 5, "15Min": 15, "1Hour": 60, "1Day": 1440}.get(tf, 5)


def _is_nyse_open_now() -> bool:
    """Cheap NYSE-open check (no holiday awareness). M-F 09:30-16:00 ET.

    Matches the same logic as /api/ops/market_hours but inlined here so the
    sparklines envelope doesn't pay an extra round-trip. Holiday awareness
    is a TODO across both call sites.
    """
    from datetime import datetime, time, timezone
    try:
        from zoneinfo import ZoneInfo
    except ImportError:  # pragma: no cover
        from backports.zoneinfo import ZoneInfo
    et = ZoneInfo("America/New_York")
    now_et = datetime.now(timezone.utc).astimezone(et)
    if now_et.weekday() >= 5:
        return False
    return time(9, 30) <= now_et.time() < time(16, 0)


def _stock_candles_inner(symbol: str, timeframe: str) -> dict:
    """Load the cached candles payload for one stock symbol.

    Single source of truth for the per-symbol `/stock_candles/{sym}` route
    and the basket `/stocks_sparklines` route. Reads from the JSON cache
    the `wheel_candles` Hermes cron writes every 5 min during market hours.

    Raises:
        HTTPException(404) if the symbol is not on the operator whitelist.
        HTTPException(400) if the timeframe is not one of the Alpaca codes.

    Returns a dict that carries an internal `_status` field of {"ok",
    "degraded", "down"} plus an optional `_error` so callers can wrap
    the payload into the right `_envelope(...)` reply without duplicating
    the cache-staleness rules.
    """
    sym = (symbol or "").upper()
    if sym not in _STOCK_SYMBOL_WHITELIST:
        raise HTTPException(status_code=404, detail=f"symbol not whitelisted: {sym}")
    if timeframe not in {"1Min", "5Min", "15Min", "1Hour", "1Day"}:
        raise HTTPException(status_code=400, detail=f"unsupported timeframe: {timeframe}")

    candles_file = STOCKS_ROOT / "wheel" / "state" / f"candles_{sym}_{timeframe}.json"
    raw = _read_json(candles_file)
    if not raw:
        return {
            "_status": "down",
            "_error": (
                f"no cached candles for {sym} {timeframe} — run "
                f"`python -m wheel.cli candles {sym} --timeframe {timeframe}`"
            ),
            "symbol": sym,
            "timeframe": timeframe,
            "ts": None,
            "age_seconds": None,
            "bars": [],
        }

    bars = raw.get("bars") or []
    age = _file_age_seconds(candles_file)
    status = "ok"
    error = None
    if age is not None and age > 86400:
        status = "degraded"
        error = f"candles stale ({age}s old)"
    return {
        "_status": status,
        "_error": error,
        "symbol": sym,
        "timeframe": timeframe,
        "ts": raw.get("ts"),
        "age_seconds": age,
        "bars": bars,
    }


@router.get("/stock_candles/{symbol}")
async def stock_candles(symbol: str, timeframe: str = "5Min"):
    """Serve OHLC candles for a stock from the cron-fed JSON cache.

    The dashboard never calls Alpaca directly. The `wheel_candles` Hermes
    cron writes `stocks/wheel/state/candles_{SYM}_{tf}.json` every 5 min
    during market hours; this endpoint streams the cached file as a
    Lightweight-Charts-compatible payload.
    """
    inner = _stock_candles_inner(symbol, timeframe)
    status = inner.pop("_status", "ok")
    error = inner.pop("_error", None)
    if status == "down":
        return _envelope("down", error=error)
    if status == "degraded":
        return _envelope("degraded", data=inner, error=error)
    return _envelope("ok", data=inner)


@router.get("/stocks_sparklines")
async def stocks_sparklines(timeframe: str = "5Min", limit: int = 78):
    """Per-symbol close-price arrays + %-change since first bar in window.

    Mirrors the crypto `/sparklines` envelope so the SPA can render the
    same PairTelemetry card pattern for the stocks basket.

    `timeframe` accepts Alpaca codes: `1Min`, `5Min`, `15Min`, `1Hour`,
    `1Day`. `limit=78` × 5Min ≈ 6.5h ≈ one US trading session.

    Returns `_envelope(status, data={symbols, timeframe, limit, basket,
    market_open})`. Each per-symbol payload carries `{closes, current,
    pct_session, bars_count, session_window_h}` or, on failure,
    `{closes: [], current: None, pct_session: None, error}`.

    GET, no `require_mcp_key` — matches the crypto `/sparklines` pattern.
    Read-only operator data.
    """
    if timeframe not in ("1Min", "5Min", "15Min", "1Hour", "1Day"):
        return _envelope("down", error=f"unsupported timeframe: {timeframe}")
    limit = max(2, min(390, int(limit)))  # 390×1min = full session

    basket = [s for s in DEFAULT_STOCK_SYMBOLS if s in _STOCK_SYMBOL_WHITELIST]
    if not basket:
        return _envelope(
            "down",
            error=(
                "DASHBOARD_STOCK_SYMBOLS yielded zero whitelisted symbols "
                f"(input={DEFAULT_STOCK_SYMBOLS})"
            ),
        )

    def _empty(reason: str | None = None) -> dict:
        out = {
            "closes": [],
            "current": None,
            "pct_session": None,
            "bars_count": 0,
            "session_window_h": (limit * _stock_timeframe_minutes(timeframe)) / 60.0,
        }
        if reason:
            out["error"] = reason[:160]
        return out

    async def _one(symbol: str) -> tuple[str, dict]:
        # `_stock_candles_inner` is synchronous (cheap JSON read); push it
        # to the default executor so a slow filesystem can't block the loop.
        try:
            loop = asyncio.get_running_loop()
            inner = await loop.run_in_executor(
                None, _stock_candles_inner, symbol, timeframe,
            )
        except HTTPException as exc:
            return symbol, _empty(reason=str(exc.detail))
        except Exception as exc:  # pragma: no cover — defensive
            logger.warning("stocks_sparklines %s: %s", symbol, exc)
            return symbol, _empty(reason=str(exc))

        if inner.get("_status") == "down":
            return symbol, _empty(reason=inner.get("_error"))

        bars = (inner.get("bars") or [])[-limit:]
        # The wheel candle cache uses long-key OHLC ("close"); accept the
        # crypto-style short keys too in case the cron schema ever flips.
        closes = []
        for b in bars:
            v = b.get("close") if "close" in b else b.get("c")
            if v is not None:
                try:
                    closes.append(float(v))
                except (TypeError, ValueError):
                    continue
        if len(closes) < 2:
            return symbol, _empty(reason="insufficient bars")
        ref = closes[0]
        current = closes[-1]
        pct = ((current - ref) / ref * 100.0) if ref else None
        return symbol, {
            "closes": closes,
            "current": current,
            "pct_session": pct,
            "bars_count": len(closes),
            "session_window_h": (limit * _stock_timeframe_minutes(timeframe)) / 60.0,
        }

    try:
        results = await asyncio.wait_for(
            # return_exceptions=True so one bad symbol doesn't poison the
            # whole basket (lesson from the crypto endpoint's P1 #2.5 bug).
            asyncio.gather(*(_one(s) for s in basket), return_exceptions=True),
            timeout=ENDPOINT_TIMEOUT_S,
        )
    except asyncio.TimeoutError:
        return _envelope("down", error="stocks_sparklines fetch timed out")
    except Exception as exc:
        logger.exception("stocks_sparklines failed")
        return _envelope("down", error=str(exc))

    data: dict[str, dict] = {}
    for entry, sym in zip(results, basket):
        if isinstance(entry, Exception):
            data[sym] = _empty(reason=str(entry))
        else:
            data[sym] = entry[1]

    has_any = any((p.get("closes") or []) for p in data.values())
    status = "ok" if has_any else "degraded"
    return _envelope(
        status,
        data={
            "symbols": data,
            "timeframe": timeframe,
            "limit": limit,
            "basket": basket,
            "market_open": _is_nyse_open_now(),
        },
        error=None if has_any else "no cached candles for any basket symbol",
    )


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
    from .data_sources import latest_state_from_df

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
    # Read entry_delta from live config (was hardcoded — caused the gates
    # UI to show stale thresholds while regime_config edits silently updated
    # the actual strategy behavior. Operator saw "blocked at 0.57" while the
    # strategy was using -0.15 → 0.47 threshold).
    _entry_delta_cfg = _rg_live.get("entry_delta") or {}
    REGIME_DELTA = {
        "trending_up":     _entry_delta_cfg.get("trending_up", -0.05),
        "trending_down":   None if _entry_delta_cfg.get("trending_down") is None else _entry_delta_cfg.get("trending_down"),
        "mean_reverting":  _entry_delta_cfg.get("mean_reverting", +0.10),
        "high_volatility": _entry_delta_cfg.get("high_volatility", +0.05),
        "unknown":         _entry_delta_cfg.get("unknown", 0.0),
    }
    # trending_down: if the config has a numeric value, treat it as the
    # confidence-floor relax threshold (P0 commit 5293e62). If absent or
    # None, fall back to hard-block.
    if "trending_down" in _entry_delta_cfg:
        _td = _entry_delta_cfg["trending_down"]
        REGIME_DELTA["trending_down"] = float(_td) if _td is not None else None

    # FreqAI authoritative model registry — used by the `model_freshness` gate
    # to surface MODEL EXPIRED before the strategy-level do_predict gate.
    _pair_dict: dict = {}
    try:
        import json as _json2, time as _time
        _pd_path = Path(f"/app/user_data/models/{_identifier}/pair_dictionary.json")
        if _pd_path.exists():
            _pair_dict = _json2.loads(_pd_path.read_text()) or {}
    except Exception as _exc:
        logger.warning("gates: pair_dictionary read failed: %s", _exc)
    _now_ts = int(__import__("time").time())

    pairs = [p.strip() for p in os.environ.get("DASHBOARD_PAIRS", "BTC/USD,ETH/USD,SOL/USD").split(",") if p.strip()]
    timeframe = os.environ.get("DASHBOARD_TIMEFRAME", "5m")

    # Account-level inputs.
    # H-2 audit fix: post-cutover the freqtrade ft_authed_get call
    # silently returns None (jwt short-circuits in V4 mode), leaving
    # open_count=0 and breaker_active=False — a confidently-displayed
    # lie about account capacity. Read from the V4 sources of truth:
    #   open_count       = count(*) FROM public.trade_journal WHERE closed_at IS NULL
    #   breaker_active   = quanta_schema.run_state.paused
    open_count = 0
    max_open = int(os.environ.get("MAX_OPEN_TRADES", "6"))
    breaker_active = False
    try:
        if ops_db._HAVE_PG:
            with ops_db._connect() as _conn, _conn.cursor() as _cur:
                _cur.execute(
                    "SELECT COUNT(*) AS n FROM public.trade_journal WHERE closed_at IS NULL"
                )
                _row = _cur.fetchone() or {}
                open_count = int(_row.get("n") or 0)
                try:
                    _cur.execute(
                        "SELECT paused FROM quanta_schema.run_state WHERE id = 1"
                    )
                    _rs = _cur.fetchone() or {}
                    breaker_active = bool(_rs.get("paused"))
                except Exception as _exc:
                    logger.debug("gates: run_state read failed: %s", _exc)
    except Exception as exc:
        logger.warning("gates: account-level pg read failed: %s", exc)

    # Post-2026-05-14: freqtrade max_open_trades probe removed (container
    # retired). MAX_OPEN_TRADES env var (default 6) is now the source of
    # truth for the gates display.

    # Per-pair gate evaluation
    # Pre-fetch the V4-era canonical regime once (single postgres roundtrip);
    # we reuse it as the fallback when freqtrade's per-pair df is empty.
    _v4_regime: str | None = None
    try:
        _v4_row = ops_db.regime_latest()
        if _v4_row and _v4_row.get("regime"):
            _v4_regime = _v4_row["regime"]
    except Exception:
        pass

    # H-3 audit fix: pre-fetch the LATEST row per symbol from
    # public.classifier_log and public.meta_signal_log so the gates
    # snapshot reflects fresh quanta-core writes. Without this every
    # crypto pair returned {up:null, tft_confidence:null, meta_signal:
    # null} despite the producers writing every 5 min.
    _classifier_by_symbol: dict[str, dict] = {}
    _meta_by_symbol: dict[str, dict] = {}
    try:
        if ops_db._HAVE_PG:
            with ops_db._connect() as _conn, _conn.cursor() as _cur:
                _cur.execute(
                    """
                    SELECT DISTINCT ON (symbol)
                        symbol, ts, p_up, p_flat, p_down, confidence,
                        classifier
                    FROM public.classifier_log
                    ORDER BY symbol, ts DESC
                    """
                )
                for _r in _cur.fetchall():
                    _classifier_by_symbol[str(_r["symbol"])] = dict(_r)
                _cur.execute(
                    """
                    SELECT DISTINCT ON (symbol)
                        symbol, ts, signal, confidence, regime, strategies
                    FROM public.meta_signal_log
                    ORDER BY symbol, ts DESC
                    """
                )
                for _r in _cur.fetchall():
                    _meta_by_symbol[str(_r["symbol"])] = dict(_r)
    except Exception as _exc:
        logger.warning("gates: classifier_log/meta_signal_log prefetch failed: %s", _exc)

    # Post-cutover: surface V4 strategy entry conditions instead of the dead
    # FreqAI gate set. When LIVE_ENGINE_MODE is live/shadow we'll emit a
    # parallel `v4_gates` array per pair (cap, regime, mr_dip, tf_break,
    # tf_aligned, open) with concrete prices in the WHY string so the
    # operator can see literally what each strategy is waiting for.
    _v4_active = _v4_is_active_engine()
    _v4_open_count = 0
    if _v4_active:
        try:
            _v4_open_count = len(_v4_crypto_open_positions())
        except Exception:
            _v4_open_count = 0

    rows = []
    for pair in pairs:
        try:
            df = await fetch_coinbase_candles(pair, timeframe, limit=5)
            state = latest_state_from_df(df, pair) if df is not None else {}
        except Exception as exc:
            logger.warning("gates: coinbase candles failed for %s: %s", pair, exc)
            state = {"_error": str(exc)}

        # Post-cutover: coinbase df has no FreqAI columns → state.regime is
        # None → fall back to the V4 hourly regime write (single source of
        # truth post-cutover).
        regime = state.get("regime") or _v4_regime or "unknown"
        delta = REGIME_DELTA.get(regime, 0.0)
        threshold = (BASE_ENTRY + delta) if delta is not None else None
        up = state.get("tft_up")
        tft_conf = state.get("tft_confidence")
        meta_sig = state.get("meta_signal")
        meta_conf = state.get("meta_confidence")
        volume = state.get("volume")
        do_predict = state.get("do_predict")

        # H-3 audit fix: when the freqtrade df path didn't populate these
        # (always the case post-cutover), fall back to the V4 producers.
        _cls_row = _classifier_by_symbol.get(pair) or {}
        _meta_row = _meta_by_symbol.get(pair) or {}
        if up is None and _cls_row.get("p_up") is not None:
            up = float(_cls_row["p_up"])
        if tft_conf is None and _cls_row.get("confidence") is not None:
            tft_conf = float(_cls_row["confidence"])
        if meta_sig is None and _meta_row.get("signal") is not None:
            meta_sig = int(_meta_row["signal"])
        if meta_conf is None and _meta_row.get("confidence") is not None:
            meta_conf = float(_meta_row["confidence"])

        gate_results: list[dict[str, Any]] = []

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
        row = {
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
                # H-3: source timestamps so the UI can show "as of HH:MM" badges
                "classifier_ts": (
                    _cls_row.get("ts").isoformat()
                    if _cls_row.get("ts") is not None and hasattr(_cls_row.get("ts"), "isoformat")
                    else _cls_row.get("ts")
                ),
                "meta_signal_ts": (
                    _meta_row.get("ts").isoformat()
                    if _meta_row.get("ts") is not None and hasattr(_meta_row.get("ts"), "isoformat")
                    else _meta_row.get("ts")
                ),
            },
        }

        # Post-cutover: when quanta-core is the active engine, the V3
        # FreqAI gate set above (model_freshness / freqai_predict /
        # up_prob_threshold / tft_confidence) is dead — pair_dictionary
        # is no longer written and quanta-core's MeanRevBB + TrendFollow
        # strategies don't consult those columns. Surface the V4 gate set
        # (capital, regime, mr_dip, tf_break, tf_aligned, open) as the
        # primary `gates` array so the BlockerBanner reports what's
        # actually waiting on a fire. Keep the V3 set under `v3_gates`
        # for any legacy panels still wired to that schema.
        if _v4_active:
            try:
                v4_payload = await _eval_v4_gates(
                    pair=pair,
                    regime=regime,
                    open_count=_v4_open_count,
                    max_open=max_open,
                )
                row["v4_gates"] = v4_payload
                row["v3_gates"] = row["gates"]
                row["gates"] = v4_payload.get("gates", [])
                row["n_gates"] = v4_payload.get("n_gates", len(row["gates"]))
                row["n_blocking"] = v4_payload.get("n_blocking", 0)
                row["first_blocker"] = v4_payload.get("first_blocker")
            except Exception as exc:
                logger.debug("v4 gates eval failed for %s: %s", pair, exc)

        rows.append(row)

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
    # Dashboard watchlist — non-wheel symbols get a passive gate row that
    # surfaces the global stocks regime so the pair telemetry strip can
    # show "regime · trending_up" instead of "regime · —" for every row.
    watchlist_symbols: list[str] = []
    try:
        _uni = _read_json(USER_DATA_ROOT_FOR_BACKUPS / "universe.json") or {}
        watchlist_symbols = [
            s.strip().upper()
            for s in ((_uni.get("stocks") or {}).get("dashboard_basket") or [])
            if s and str(s).strip().upper() not in wheel_symbols
        ]
    except Exception:
        pass
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
            "venue_type": "wheel",
            "snapshot": {
                "cash": cash,
                "buying_power": bp,
                "paper": paper,
            },
        })

    # Watchlist symbols (non-wheel): emit a single shark_phase_decision
    # gate per ticker so the UI shows real status instead of n_gates=0
    # (which the dashboard reads as "all permitted" — a confidently-
    # displayed lie when no shark phase ran today).
    #
    # FIX-I bug 1 (2026-05-14): historically every watchlist row carried
    # `n_gates=0` regardless of whether the shark briefing knew anything
    # about the symbol. Now we surface the most-recent shark phase
    # decision per ticker, parsed from stocks/memory/DAILY-HANDOFF.md
    # (the same file the rest of the dashboard reads for the briefing
    # card). If the file is missing/stale or the symbol isn't mentioned
    # in today's phases at all, the gate reports that explicitly —
    # `pass=None` + a "no shark phase ran today" detail — which is
    # honest and renders distinctly from a true block.
    shark_decisions = _parse_shark_phase_decisions(
        STOCKS_ROOT / "memory" / "DAILY-HANDOFF.md"
    )
    for sym in watchlist_symbols:
        decision = shark_decisions.get("by_symbol", {}).get(sym)
        if decision is None:
            if shark_decisions.get("missing"):
                gate_detail = "no shark DAILY-HANDOFF.md found"
                gate_pass: bool | None = None
            elif shark_decisions.get("stale_date"):
                gate_detail = (
                    f"shark briefing is for {shark_decisions['stale_date']} "
                    f"(not today)"
                )
                gate_pass = None
            else:
                gate_detail = "no shark phase decision for this symbol today"
                gate_pass = None
        else:
            gate_pass = decision["pass"]
            gate_detail = decision["detail"]
        shark_gate = {
            "gate": "shark_phase_decision",
            "pass": gate_pass,
            "detail": gate_detail,
        }
        blocking = [shark_gate] if gate_pass is False else []
        stock_rows.append({
            "pair": sym,
            "regime": stock_regime or "—",
            "n_gates": 1,
            "n_blocking": len(blocking),
            "first_blocker": "shark_phase_decision" if blocking else None,
            "gates": [shark_gate],
            "venue_type": "watchlist",
            "snapshot": {
                "shark_briefing_date": shark_decisions.get("briefing_date"),
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


def _v4_is_active_engine() -> bool:
    return (os.environ.get("LIVE_ENGINE_MODE") or "").lower() in ("live", "shadow")


# Permissive regime sets — keep in sync with the strategies in
# src/quanta_core/strategy/{mean_rev_bb,trend_follow}.py. Centralised here
# so the gates endpoint surfaces the EXACT condition each strategy checks.
_MR_PERMISSIVE_REGIMES = frozenset({"trending_up", "mean_reverting"})
_TF_PERMISSIVE_REGIMES = frozenset({"trending_up"})
_MR_BB_WINDOW = 20
_MR_BB_STD = 2.0
_TF_SHORT_WINDOW = 8
_TF_LONG_WINDOW = 21


async def _eval_v4_gates(
    *,
    pair: str,
    regime: str,
    open_count: int,
    max_open: int,
) -> dict[str, Any]:
    """Compute V4 strategy entry gates for one crypto pair.

    Returns a payload the frontend can render in place of the FreqAI gate
    set:

        {
          "gates": [
              {gate: "capital_allocation", pass: True,  detail: "..."},
              {gate: "regime",             pass: True,  detail: "..."},
              {gate: "mr_dip",             pass: False, detail: "close $80.2k > lower_bb $79.8k"},
              {gate: "tf_break",           pass: False, detail: "close $80.2k > short_ma $80.4k"},
              {gate: "tf_aligned",         pass: False, detail: "short_ma 80.4 < long_ma 80.7"},
              {gate: "account_capacity",   pass: True,  detail: "0/6 open"},
          ],
          "n_blocking": 3,
          "first_blocker": "mr_dip",
          "snapshot": {close, lower_bb, middle_bb, short_ma, long_ma},
          "why": "no entry: mr waiting for close < $79.8k · tf waiting for close > $80.4k AND ma aligned"
        }
    """
    gates: list[dict[str, Any]] = []
    snap: dict[str, Any] = {}

    # 1. capital allocation — same assumption as the legacy gate
    gates.append({
        "gate": "capital_allocation",
        "pass": True,
        "detail": "weight > 0 (assumed)",
    })

    # 2. regime — passes if at least ONE strategy's permissive set allows it
    regime_ok = (regime in _MR_PERMISSIVE_REGIMES) or (regime in _TF_PERMISSIVE_REGIMES)
    regime_strats: list[str] = []
    if regime in _MR_PERMISSIVE_REGIMES:
        regime_strats.append("mr")
    if regime in _TF_PERMISSIVE_REGIMES:
        regime_strats.append("tf")
    gates.append({
        "gate": "regime",
        "pass": regime_ok,
        "detail": f"{regime} → " + (", ".join(regime_strats) if regime_ok else "neither strategy"),
    })

    # 3-5. MeanRevBB + TrendFollow signal conditions — need closes to compute.
    # Use Coinbase REST fallback (30 bars covers BB window 20 + long_ma 21).
    closes: list[float] = []
    try:
        from .data_sources import fetch_coinbase_candles
        df = await fetch_coinbase_candles(pair, timeframe="5m", limit=30)
        if df is not None and not df.empty and "close" in df.columns:
            closes = [float(x) for x in df["close"].tolist()]
    except Exception as exc:
        logger.debug("v4 gates: coinbase fetch %s failed: %s", pair, exc)

    if len(closes) < _TF_LONG_WINDOW:
        # warm-up — surface the wait condition explicitly
        for g in ("mr_dip", "tf_break", "tf_aligned"):
            gates.append({
                "gate": g,
                "pass": False,
                "detail": f"warm-up: {len(closes)}/{_TF_LONG_WINDOW} bars",
            })
        snap = {"close": None, "lower_bb": None, "short_ma": None, "long_ma": None}
    else:
        close = closes[-1]
        # Bollinger lower band (population std, talib convention)
        bb_closes = closes[-_MR_BB_WINDOW:]
        bb_mean = sum(bb_closes) / _MR_BB_WINDOW
        bb_var = sum((c - bb_mean) ** 2 for c in bb_closes) / _MR_BB_WINDOW
        bb_std = bb_var ** 0.5
        lower_bb = bb_mean - _MR_BB_STD * bb_std
        middle_bb = bb_mean
        # Short & long simple MAs
        short_ma = sum(closes[-_TF_SHORT_WINDOW:]) / _TF_SHORT_WINDOW
        long_ma = sum(closes[-_TF_LONG_WINDOW:]) / _TF_LONG_WINDOW

        snap = {
            "close": round(close, 6),
            "lower_bb": round(lower_bb, 6),
            "middle_bb": round(middle_bb, 6),
            "short_ma": round(short_ma, 6),
            "long_ma": round(long_ma, 6),
        }

        mr_dip = close < lower_bb
        gates.append({
            "gate": "mr_dip",
            "pass": mr_dip,
            "detail": (
                f"close {_fmt_px(close)} {'<' if mr_dip else '≥'} lower_bb {_fmt_px(lower_bb)}"
            ),
        })

        tf_break = close > short_ma
        gates.append({
            "gate": "tf_break",
            "pass": tf_break,
            "detail": (
                f"close {_fmt_px(close)} {'>' if tf_break else '≤'} short_ma {_fmt_px(short_ma)}"
            ),
        })

        tf_aligned = short_ma > long_ma
        gates.append({
            "gate": "tf_aligned",
            "pass": tf_aligned,
            "detail": (
                f"short_ma {_fmt_px(short_ma)} {'>' if tf_aligned else '≤'} long_ma {_fmt_px(long_ma)}"
            ),
        })

    # 6. account capacity — V4 paper open count vs max
    cap_ok = open_count < max_open
    gates.append({
        "gate": "account_capacity",
        "pass": cap_ok,
        "detail": f"{open_count}/{max_open} V4 paper open",
    })

    blocking = [g for g in gates if g["pass"] is False]
    # Build operator-readable WHY string
    mr_block = next((g for g in gates if g["gate"] == "mr_dip" and not g["pass"]), None)
    tf_break_block = next((g for g in gates if g["gate"] == "tf_break" and not g["pass"]), None)
    tf_aligned_block = next((g for g in gates if g["gate"] == "tf_aligned" and not g["pass"]), None)
    why_parts: list[str] = []
    if mr_block:
        why_parts.append(f"mr: {mr_block['detail']}")
    if tf_break_block or tf_aligned_block:
        if tf_break_block:
            why_parts.append(f"tf: {tf_break_block['detail']}")
        elif tf_aligned_block:
            why_parts.append(f"tf: {tf_aligned_block['detail']}")
    if not why_parts and not blocking:
        why = "all gates clear · entry pending strategy decision"
    elif not why_parts:
        why = blocking[0]["detail"]
    else:
        why = " · ".join(why_parts)

    return {
        "gates": gates,
        "n_gates": len(gates),
        "n_blocking": len(blocking),
        "first_blocker": blocking[0]["gate"] if blocking else None,
        "snapshot": snap,
        "why": why,
    }


def _fmt_px(p: float) -> str:
    """Compact USD-aware price format for V4 gate WHY strings."""
    if p is None:
        return "—"
    if p >= 1000:
        return f"${p:,.0f}"
    if p >= 1:
        return f"${p:.2f}"
    return f"${p:.4f}"


# --------------------------------------------------------------------------
# /api/ops/flash_status — top-of-dashboard "flash news" strip
# --------------------------------------------------------------------------
# A single-row at-a-glance summary the operator sees the instant they load
# the page: which engine is live, how many positions open right now, how
# much closed-trade P&L today, current regime, time since last fill.
# Designed to occupy ONE row above the today's scoreboard — never a column.


@router.get("/flash_status")
async def flash_status():
    """Compact ticker payload for the FlashNewsStripLive card.

    Pulls (in parallel where possible) from quanta_schema (V4 open positions
    + last fill), public.trade_journal (today's closed P&L count), regime_log
    (current regime). Cheap — typical response < 200 bytes.
    """
    out: dict[str, Any] = {
        "engine": "quanta_core" if _v4_is_active_engine() else "freqtrade",
        "mode": os.environ.get("LIVE_ENGINE_MODE", "unknown"),
        "open_positions": 0,
        "open_symbols": [],
        "open_notional_usd": 0.0,
        "closed_today": 0,
        "closed_today_pnl_usd": 0.0,
        "regime": None,
        "regime_prob": None,
        "last_fill_ts": None,
        "last_fill_symbol": None,
        "last_fill_side": None,
    }

    if _v4_is_active_engine():
        try:
            loop = asyncio.get_running_loop()

            # Also include the wheel-runner stocks positions so the
            # operator's at-a-glance summary covers ALL open trades, not
            # just V4 paper. The wheel state lives in stocks/wheel/state/
            # positions.json (read by the existing /api/ops/live_trades).
            wheel_open_count = 0
            wheel_open_symbols: list[str] = []
            try:
                pos_file = STOCKS_ROOT / "wheel" / "state" / "positions.json"
                wheel_positions = _read_json(pos_file) or []
                if isinstance(wheel_positions, list):
                    seen: set[str] = set()
                    for p in wheel_positions:
                        sym = (p.get("underlying") or "").upper()
                        if not sym or sym in seen:
                            continue
                        seen.add(sym)
                        wheel_open_symbols.append(sym)
                    wheel_open_count = len(wheel_open_symbols)
            except Exception as exc:
                logger.debug("flash_status wheel read failed: %s", exc)

            def _pull() -> dict[str, Any]:
                from . import ops_db
                if not ops_db._HAVE_PG:
                    return {}
                with ops_db._connect() as conn, conn.cursor() as cur:
                    # Open paper positions (sum net qty > 0)
                    cur.execute(
                        """
                        SELECT p.symbol,
                               SUM(CASE WHEN f.side='BUY'  THEN f.qty ELSE 0 END) -
                               SUM(CASE WHEN f.side='SELL' THEN f.qty ELSE 0 END)            AS net_qty,
                               SUM(CASE WHEN f.side='BUY' THEN f.qty * f.price ELSE 0 END) /
                               NULLIF(SUM(CASE WHEN f.side='BUY' THEN f.qty ELSE 0 END), 0)  AS avg_buy_px
                        FROM quanta_schema.fills f
                        JOIN quanta_schema.proposals p USING (client_order_id)
                        GROUP BY p.symbol
                        HAVING SUM(CASE WHEN f.side='BUY' THEN f.qty ELSE 0 END) -
                               SUM(CASE WHEN f.side='SELL' THEN f.qty ELSE 0 END) > 0
                        """
                    )
                    open_rows = cur.fetchall()

                    # Closed-today P&L from trade_journal
                    cur.execute(
                        """
                        SELECT count(*), COALESCE(SUM(pnl), 0)
                        FROM public.trade_journal
                        WHERE closed_at IS NOT NULL
                          AND closed_at >= date_trunc('day', NOW())
                        """
                    )
                    closed_row = cur.fetchone()

                    # Latest fill
                    cur.execute(
                        """
                        SELECT f.ts, p.symbol, f.side
                        FROM quanta_schema.fills f
                        JOIN quanta_schema.proposals p USING (client_order_id)
                        ORDER BY f.ts DESC LIMIT 1
                        """
                    )
                    last_fill = cur.fetchone()

                    # Current regime
                    cur.execute(
                        """
                        SELECT regime, probability
                        FROM regime_log
                        ORDER BY ts DESC LIMIT 1
                        """
                    )
                    regime_row = cur.fetchone()

                # dict_row factory — extract by named keys
                positions = []
                notional = 0.0
                for r in open_rows:
                    sym = r["symbol"]
                    qty = float(r["net_qty"] or 0)
                    avg_px = float(r["avg_buy_px"] or 0)
                    positions.append(sym)
                    notional += qty * avg_px
                return {
                    "positions": positions,
                    "notional": round(notional, 2),
                    "closed_count": int((closed_row or {}).get("count", 0) or 0),
                    "closed_pnl": float((closed_row or {}).get("coalesce", 0) or 0),
                    "last_fill_ts": (last_fill or {}).get("ts"),
                    "last_fill_symbol": (last_fill or {}).get("symbol"),
                    "last_fill_side": (last_fill or {}).get("side"),
                    "regime": (regime_row or {}).get("regime"),
                    "regime_prob": float((regime_row or {}).get("probability") or 0),
                }

            res = await asyncio.wait_for(loop.run_in_executor(None, _pull),
                                          timeout=ENDPOINT_TIMEOUT_S)
            # Aggregate V4 crypto + wheel stocks for the flash row
            v4_syms = res.get("positions") or []
            all_syms = list(v4_syms) + wheel_open_symbols
            out["open_positions"] = len(all_syms)
            out["open_symbols"] = all_syms
            out["open_v4"] = len(v4_syms)
            out["open_wheel"] = wheel_open_count
            out["open_notional_usd"] = res.get("notional") or 0.0
            out["closed_today"] = res.get("closed_count") or 0
            out["closed_today_pnl_usd"] = round(res.get("closed_pnl") or 0.0, 4)
            last_ts = res.get("last_fill_ts")
            out["last_fill_ts"] = last_ts.isoformat() if last_ts else None
            out["last_fill_symbol"] = res.get("last_fill_symbol")
            out["last_fill_side"] = res.get("last_fill_side")
            out["regime"] = res.get("regime")
            out["regime_prob"] = res.get("regime_prob")
        except Exception as exc:
            logger.warning("flash_status pull failed: %s", exc)

    return _envelope("ok", data=out)


def _v4_crypto_open_positions() -> list[dict]:
    """Read open V4 paper positions from quanta_schema (post-cutover path).

    Returns crypto-kind trade rows in the same shape live_trades expects,
    so the hero ticker renders identically whether the active engine is
    freqtrade or V4.
    """
    rows: list[dict] = []
    try:
        from .ops_db import _connect, _HAVE_PG
    except Exception:
        return rows
    if not _HAVE_PG:
        return rows
    try:
        with _connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT p.symbol,
                       SUM(CASE WHEN f.side='BUY'  THEN f.qty ELSE 0 END) -
                       SUM(CASE WHEN f.side='SELL' THEN f.qty ELSE 0 END)            AS net_qty,
                       SUM(CASE WHEN f.side='BUY' THEN f.qty * f.price ELSE 0 END) /
                       NULLIF(SUM(CASE WHEN f.side='BUY' THEN f.qty ELSE 0 END), 0)  AS avg_buy_px,
                       MAX(f.ts)                                                     AS last_fill_ts,
                       MAX(p.strategy)                                               AS strategy
                FROM quanta_schema.fills f
                JOIN quanta_schema.proposals p USING (client_order_id)
                GROUP BY p.symbol
                HAVING SUM(CASE WHEN f.side='BUY' THEN f.qty ELSE 0 END) -
                       SUM(CASE WHEN f.side='SELL' THEN f.qty ELSE 0 END) > 0
                """
            )
            # ops_db._connect() uses dict_row factory; rows are dicts keyed
            # by the SELECT-alias names (symbol, net_qty, avg_buy_px, ...).
            for r in cur.fetchall():
                rows.append({
                    "kind": "crypto",
                    "subkind": "long",
                    "label": r["symbol"],
                    "entry": float(r["avg_buy_px"]) if r["avg_buy_px"] is not None else None,
                    "current": None,  # filled by client from /api/candles
                    "qty": float(r["net_qty"]),
                    "pnl_pct": None,
                    "pnl_usd": None,
                    "duration_s": None,
                    "opened_at": r["last_fill_ts"].isoformat() if r["last_fill_ts"] else None,
                    "extra": f"v4·strategy={r['strategy']}",
                })
    except Exception as exc:
        logger.warning("v4 positions read failed: %s", exc)
    return rows


@router.get("/live_trades")
async def live_trades():
    """Aggregate every active position across crypto + wheel + shark for
    the top hero strip. Post-cutover (LIVE_ENGINE_MODE set) reads crypto
    from quanta_schema; otherwise legacy freqtrade probe.
    """
    out: list[dict] = []

    # ── Crypto open trades ────────────────────────────
    # Post-2026-05-14: V4 (quanta-core) is the only crypto engine; the
    # freqtrade /api/v1/status fallback is gone with the container.
    out.extend(_v4_crypto_open_positions())

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
    # daily_pnl_usd comes from trade_journal via ops_db. We IGNORE the
    # daily_pnl_pct that ops_db returns — that field used to be SUM(pnl_pct)
    # across all closed rows in trade_journal, which inflates ~50× on days
    # with many intra-cycle round-trips (V4 paper engine logs 50+ fills/day).
    # Result: dashboard showed `day_pnl_pct=277.37%` for a real $14.55 loss
    # because 48 fills × ~5.8% each = 277%.
    #
    # The correct denominator is the day's STARTING equity. We don't store
    # a day_start_equity column, but algebraically:
    #     day_start_equity = current_total_equity - daily_pnl_usd
    # so the day pct is:
    #     daily_pnl_pct = daily_pnl_usd / day_start_equity * 100
    # which is robust to N-fill inflation.
    try:
        risk = await asyncio.wait_for(
            loop.run_in_executor(None, ops_db.trades_risk_summary),
            timeout=ENDPOINT_TIMEOUT_S,
        )
        day_pnl_usd = float(risk.get("daily_pnl_usd") or 0)
        status["day_pnl_usd"] = day_pnl_usd
        total_equity = float(status.get("total_equity") or 0)
        day_start_equity = total_equity - day_pnl_usd
        if day_start_equity > 0:
            status["day_pnl_pct"] = (day_pnl_usd / day_start_equity) * 100.0
        else:
            status["day_pnl_pct"] = 0.0
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


# --------------------------------------------------------------------------
# /api/ops/shark_override_health — paper-mode BEAR_VOLATILE override verifier
# --------------------------------------------------------------------------
#
# Reads the JSON status file written by ~/.hermes/scripts/shark_override_verify.sh
# (cron 45 9 * * 1-5) and surfaces it on the operator dashboard via the
# SharkOverrideHealthLive card. Verifier file lives at:
#   stocks/memory/override_verify.json
# Schema:
#   stocks/memory/override_verify.schema.json
#
# Envelope status:
#   "ok"       — verifier ran, override is healthy or not expected
#   "degraded" — verifier ran, 1-2 stalled runs (override expected, no fire)
#   "down"     — verifier ran, 3+ stalled runs OR file missing/stale > 36h
#
# The card colors map: green=healthy, yellow=degraded/stalled<3, red=stalled>=3.

# Candidate locations the verifier may write to depending on the
# repo layout (worktree vs main checkout vs container mount). First
# match wins. AUDIT 2026-05-12 Critical #1: previous revision hardcoded
# one operator's home path; we now resolve from $HOME so the dashboard
# works for any user without code edits.
_OVERRIDE_VERIFY_PATHS = [
    Path(__file__).resolve().parents[2] / "stocks" / "memory" / "override_verify.json",
    Path("/app/stocks/memory/override_verify.json"),
    Path(os.environ.get("HOME", "/root")) / "Documents" / "trading-bot" / "stocks" / "memory" / "override_verify.json",
]


def _read_override_verify_file() -> tuple[dict | None, str | None]:
    """Find and parse the verifier output. Returns (payload, error_str)."""
    for p in _OVERRIDE_VERIFY_PATHS:
        try:
            if p.is_file():
                return json.loads(p.read_text()), None
        except Exception as exc:
            return None, f"read_error at {p}: {exc}"
    return None, (
        "override_verify.json not found — verifier cron has not run yet. "
        "Manually trigger via: bash ~/.hermes/scripts/shark_override_verify.sh"
    )


@router.get("/shark_override_health")
async def shark_override_health() -> dict[str, Any]:
    """Surface the latest shark BEAR_VOLATILE paper-mode override verification.

    Returns the raw payload from override_verify.json plus envelope status:
      - status="ok"       when verifier reports healthy
      - status="degraded" when verifier reports degraded OR file > 36h old
      - status="down"     when verifier reports stalled (>=3 consecutive
                          BEAR-regime runs with candidates but no trade) OR
                          the file is missing.
    """
    payload, err = _read_override_verify_file()
    if payload is None:
        return _envelope("down", data=None, error=err)

    # File-age check — verifier runs Mon-Fri at 09:45 ET, so any payload
    # older than ~36h means the cron stopped firing or weekend gap.
    checked_at = payload.get("checked_at")
    age_s: int | None = None
    if checked_at:
        try:
            ts = datetime.fromisoformat(checked_at.replace("Z", "+00:00"))
            age_s = int((datetime.now(timezone.utc) - ts).total_seconds())
        except Exception:
            age_s = None

    verifier_status = (payload.get("status") or "unknown").lower()
    stalled_runs = int(payload.get("stalled_runs") or 0)

    # Map verifier status → HTTP envelope status
    #
    # 2026-05-14 fix: stalled / 3+-stalled cases used to map to "down" which
    # the dashboard's slotState renders as "ENDPOINT UNAVAILABLE". But the
    # endpoint isn't down — the SHARK process is stalled. Conflating the two
    # made card 00b lie about a healthy data source. We now reserve "down"
    # for genuinely-broken cases (file missing — see _read_override_verify_file
    # above) and map stalled state to "degraded" instead. The SharkOverrideHealthLive
    # component (ops_spa.js) already renders the stalled label correctly once
    # it gets past the EmptyState short-circuit.
    if verifier_status == "stalled" or stalled_runs >= 3:
        env_status = "degraded"
        env_error = (
            f"override stalled — {stalled_runs} consecutive run(s) with "
            f"candidates but no trades. See HANDOFF.md triage section."
        )
    elif verifier_status == "degraded" or stalled_runs >= 1:
        env_status = "degraded"
        env_error = f"override degraded — {stalled_runs} stalled run(s)"
    elif verifier_status == "unknown":
        env_status = "degraded"
        env_error = payload.get("reason") or "verifier reported unknown"
    elif age_s is not None and age_s > 36 * 3600:
        env_status = "degraded"
        env_error = f"verifier output {age_s}s old (>36h)"
    else:
        env_status = "ok"
        env_error = None

    enriched = dict(payload)
    enriched["age_s"] = age_s
    return _envelope(env_status, data=enriched, error=env_error)
# ──────────────────────────────────────────────────────────────────────────
# Backtest quality gates — written by the weekly bt_quality_gates.sh cron.
#
# Schema mirrors the JSON written by `scripts/backtest_with_gates.py`.
# We just glob the *_latest.json files and surface them, so the operator
# sees one row per strategy with 5 gate badges + an overall promotion-
# eligible flag.
#
# Read endpoint only (no mutation): no auth dep.
# ──────────────────────────────────────────────────────────────────────────

# Bind-mount path inside the dashboard container; falls back to the host
# path so the same endpoint works when the dashboard is run on the host
# (operator does this for local dev).
BACKTEST_RESULTS_DIR = Path(os.environ.get(
    "BACKTEST_RESULTS_DIR",
    "/app/user_data/backtest_results",
))


@router.get("/backtest_gates")
async def backtest_gates():
    """Latest gates_report per strategy + a promotion_eligible boolean.

    Walks ``BACKTEST_RESULTS_DIR`` for files matching
    ``gates_report_<strategy>_latest.json`` (written by the weekly Hermes
    cron). Each file is parsed and returned as a row in ``data.strategies``;
    cards on /ops_spa render one badge strip per row.

    The cron writes both a timestamped report and a stable *_latest.json
    pointer; we only read the pointer here so the endpoint never sees a
    partially-written file (the cron uses copy-then-rename atomicity).

    Stale = report older than 8 days (cron runs Sundays; 8d gives 1 missed
    week of grace before we surface the strategy as "stale").
    """
    results_dir = BACKTEST_RESULTS_DIR
    # Try the in-container path first; fall back to host path. Mirrors the
    # /api/universe pattern documented in the dashboard's path-lookup notes.
    if not results_dir.is_dir():
        # AUDIT 2026-05-12 Critical #1: prefer $HOME-relative fallback over
        # the hardcoded operator path. Container mount stays first.
        alt = Path(os.environ.get("HOME", "/root")) / "Documents" / "trading-bot" / "user_data" / "backtest_results"
        if alt.is_dir():
            results_dir = alt
    if not results_dir.is_dir():
        return _envelope("down",
                         error=f"backtest_results dir not found: {BACKTEST_RESULTS_DIR}",
                         data={"strategies": [], "any_eligible": False, "results_dir": str(results_dir)})

    rows: list[dict[str, Any]] = []
    now_ts = datetime.now(timezone.utc).timestamp()
    STALE_S = 8 * 24 * 3600

    for f in sorted(results_dir.glob("gates_report_*_latest.json")):
        try:
            payload = json.loads(f.read_text())
        except Exception as exc:  # noqa: BLE001 — malformed file shouldn't 500 the card
            logger.warning("backtest_gates: failed to parse %s: %s", f, exc)
            continue
        try:
            mtime = f.stat().st_mtime
        except OSError:
            mtime = 0.0
        age_s = max(0.0, now_ts - mtime)
        rows.append({
            "strategy": payload.get("strategy") or f.stem.replace("gates_report_", "").replace("_latest", ""),
            "promotion_eligible": bool(payload.get("promotion_eligible")),
            "n_trades": payload.get("n_trades"),
            "evaluated_at": payload.get("evaluated_at"),
            "timerange": payload.get("timerange"),
            "trades_per_year_estimate": payload.get("trades_per_year_estimate"),
            "thresholds": payload.get("thresholds") or {},
            "config": payload.get("config") or {},
            # Strip nested diagnostics from the gate list — the card uses
            # the simple {gate, pass, value, threshold, detail} shape and
            # the bootstrap_diag/windows trees would bloat the payload.
            "gates": [
                {
                    "gate": g.get("gate"),
                    "pass": g.get("pass"),
                    "value": g.get("value"),
                    "threshold": g.get("threshold"),
                    "detail": g.get("detail"),
                }
                for g in (payload.get("gates") or [])
            ],
            "report_age_seconds": int(age_s),
            "stale": age_s > STALE_S,
            "report_file": f.name,
        })

    any_eligible = any(r["promotion_eligible"] for r in rows)
    any_stale = any(r["stale"] for r in rows)
    summary = {
        "n_strategies": len(rows),
        "n_eligible": sum(1 for r in rows if r["promotion_eligible"]),
        "n_stale": sum(1 for r in rows if r["stale"]),
    }

    if not rows:
        return _envelope(
            "degraded",
            data={"strategies": [], "summary": summary, "any_eligible": False, "any_stale": False,
                  "results_dir": str(results_dir)},
            error="no gates_report_*_latest.json files yet — cron has not run, or wrong results dir",
        )
    return _envelope(
        "degraded" if any_stale else "ok",
        data={
            "strategies": rows,
            "summary": summary,
            "any_eligible": any_eligible,
            "any_stale": any_stale,
            "results_dir": str(results_dir),
        },
        error=("one or more reports are >8d old — weekly cron may have failed"
               if any_stale else None),
    )


# ──────────────────────────────────────────────────────────────────────────
# /api/ops/weekly_training — ModelForge LoRA training pipeline status
# ──────────────────────────────────────────────────────────────────────────
#
# Surfaces the weekly LoRA training pipeline status to the operator. The
# trading-bot exports nightly reflections + LLM logs into ModelForge's
# curated dir; ModelForge runs a per-track refresh every Sunday 02:00 ET
# (see docs/4_WEEK_EXECUTION_PLAN.md § "Per-role training cadence").
#
# This endpoint fans out to two sources:
#   (1) ModelForge HTTP API on :8000  (GET /api/forge/tracks)
#       → current_adapter, last_train_ts, last_eval_scores, examples_trained
#   (2) Local trading-bot files
#       → reflections_this_week (count from stocks/memory/decisions.md)
#       → lessons_injected      (count get_past_context() invocations,
#                                read from llm-calls.jsonl if present)
#
# Envelope:
#   status="ok"       — model-forge reachable, ≥1 track has a champion
#   status="degraded" — model-forge unreachable OR no champions yet (early
#                       in the build-up week). Local-only fields still set
#                       so the card has something to render.
#   data["model_forge_reachable"] is a boolean the frontend uses to colour
#   the connectivity pip orange when False.
#
# Operator note: this card is the **viral screenshot** for the week 4
# launch — "watch the AI learn". Keep its envelope shape stable so the
# launch demo doesn't break.

MODELFORGE_API_URL = os.environ.get("MODELFORGE_API_URL", "http://localhost:8000")

# Canonical track order — matches docs/MODELFORGE_INTEGRATION_PLAN.md § 2.
# We always return these 6 rows, even when model-forge has zero tracks
# registered (the card shows a "registered, no data yet" empty state per
# track). Stable order = stable screenshots.
_WEEKLY_TRAINING_TRACKS: tuple[tuple[str, str, str], ...] = (
    # (track_id,                   role label,                headline_metric)
    ("trading-reflector",          "Reflector",               "predictive_hit_rate_30d"),
    ("trading-bull",               "Bull analyst",            "judge_preference_pct"),
    ("trading-bear",               "Bear analyst",            "judge_preference_pct"),
    ("trading-arbiter",            "Portfolio manager",       "decision_consistency"),
    ("trading-regime-tagger",      "Regime tagger",           "json_schema_validity_rate"),
    ("trading-indicator-selector", "Indicator selector",      "selected_indicator_avg_sharpe"),
)

# Candidate locations for the decisions log — worktree vs main checkout vs
# container mount. First match wins (mirrors the override_verify pattern).
# AUDIT 2026-05-12 Critical #1: $HOME-relative fallback replaces hardcoded path.
_HOME_REPO = Path(os.environ.get("HOME", "/root")) / "Documents" / "trading-bot"
_DECISIONS_PATHS = [
    Path(__file__).resolve().parents[2] / "stocks" / "memory" / "decisions.md",
    Path("/app/stocks/memory/decisions.md"),
    _HOME_REPO / "stocks" / "memory" / "decisions.md",
]

# Candidate locations for the LLM call log — same pattern.
_LLM_CALLS_PATHS = [
    Path(__file__).resolve().parents[2] / "stocks" / "memory" / "llm-calls.jsonl",
    Path("/app/stocks/memory/llm-calls.jsonl"),
    _HOME_REPO / "stocks" / "memory" / "llm-calls.jsonl",
]


def _monday_of_this_week_utc() -> datetime:
    """Return the UTC datetime for Monday 00:00 of the current ISO week.

    Used to scope "this week" counts (reflections, lessons injected).
    Monday rather than Sunday so the count resets just after the Sunday
    02:00 ET training run completes — operator sees "reflections trained
    last night vs new since" cleanly.
    """
    now = datetime.now(timezone.utc)
    # weekday(): Mon=0 … Sun=6
    monday = now - timedelta(days=now.weekday(),
                             hours=now.hour, minutes=now.minute,
                             seconds=now.second, microseconds=now.microsecond)
    return monday


def _count_reflections_since(path: Path, since: datetime) -> int:
    """Count REFLECTION: blocks in decisions.md added since ``since``.

    The file is append-only; each closed trade adds a block like::

        [2026-05-12 | NVDA | ... | +1.2% | +0.5% alpha | 3d]
        DECISION: ...
        REFLECTION: <2-4 sentences>
        ---

    We count lines starting with ``REFLECTION:`` whose enclosing block
    bears a date >= ``since``. Cheap parser — no regex backreferences.
    Returns 0 on any read error (operator card must never 500).

    Fallback: when decisions.md has no REFLECTION blocks in window, count
    Shark's ``trade_reviewer`` rows in ``stocks/memory/llm-calls.jsonl``
    (its post-trade analysis lives there since the stage/12-reflector cron
    that would write decisions.md was never reactivated post-V4-cutover).
    Returns whichever source had more, so once decisions.md starts being
    populated it will dominate.
    """
    decisions_count = 0
    if path.is_file():
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
            current_date: datetime | None = None
            for line in text.splitlines():
                ls = line.strip()
                if ls.startswith("[") and "|" in ls:
                    head = ls.lstrip("[").split("|", 1)[0].strip()
                    try:
                        current_date = datetime.strptime(head[:10], "%Y-%m-%d").replace(
                            tzinfo=timezone.utc
                        )
                    except ValueError:
                        current_date = None
                elif ls.startswith("REFLECTION:"):
                    if current_date is not None and current_date >= since:
                        body = ls[len("REFLECTION:"):].strip()
                        if body:
                            decisions_count += 1
        except OSError:
            pass

    # Fallback to trade_reviewer JSONL rows.
    llm_calls_path = _first_existing(_LLM_CALLS_PATHS)
    reviewer_count = 0
    if llm_calls_path is not None:
        try:
            cutoff_iso = since.isoformat()
            with llm_calls_path.open("r", encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        obj = json.loads(line)
                    except (json.JSONDecodeError, ValueError):
                        continue
                    if (obj.get("agent") or "") != "trade_reviewer":
                        continue
                    ts = obj.get("timestamp") or obj.get("ts") or ""
                    if cutoff_iso and ts and ts < cutoff_iso:
                        continue
                    reviewer_count += 1
        except OSError:
            pass

    return max(decisions_count, reviewer_count)


def _count_lessons_injected_since(path: Path, since: datetime) -> int | None:
    """Count get_past_context() invocations in llm-calls.jsonl since ``since``.

    The LLM logger writes one JSON object per line. We look for records
    where ``tool == "get_past_context"`` OR ``event == "lesson_injected"``.
    Returns ``None`` (not 0) when the file does not exist — the card uses
    None to mean "logger not yet capturing this signal" vs 0 = "zero so
    far this week".
    """
    if not path.is_file():
        return None
    try:
        cutoff_iso = since.isoformat()
    except Exception:
        cutoff_iso = ""

    count = 0
    try:
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except (json.JSONDecodeError, ValueError):
                    continue
                ts = (obj.get("ts") or obj.get("timestamp")
                      or obj.get("created_at") or "")
                if cutoff_iso and ts and ts < cutoff_iso:
                    continue
                tool = (obj.get("tool") or obj.get("event") or "").lower()
                if tool in ("get_past_context", "lesson_injected"):
                    count += 1
    except OSError:
        return None
    return count


def _first_existing(paths: list[Path]) -> Path | None:
    for p in paths:
        try:
            if p.is_file():
                return p
        except OSError:
            continue
    return None


def _next_sunday_training_iso() -> str:
    """Return ISO-8601 UTC timestamp of the next Sunday 14:00 America/New_York
    (2 PM ET = the operator's chosen weekly LoRA training window).

    Source of truth: ``~/.hermes/config/gpu_reservation.yaml`` line
    ``schedule_cron: "0 14 * * 0"``. The dashboard container does NOT
    mount that file, so we hardcode 14:00 ET here and rely on the
    operator keeping the two in sync (verified by §D acceptance check).

    ET = UTC-5 (EST) or UTC-4 (EDT). We approximate via month-band
    (DST is Mar-Nov in the US); a 1-hour DST-transition wobble twice a
    year is acceptable for a countdown display.
    """
    now = datetime.now(timezone.utc)
    days_until_sun = (6 - now.weekday()) % 7
    sunday = now + timedelta(days=days_until_sun)
    # ET 14:00 → 18:00 UTC (EDT) or 19:00 UTC (EST).
    is_edt = 3 <= sunday.month <= 11
    target_utc_hour = 18 if is_edt else 19
    target = sunday.replace(hour=target_utc_hour, minute=0, second=0, microsecond=0)
    if target <= now:
        target = target + timedelta(days=7)
    return target.isoformat()


# Backwards-compat alias — the function used to be named
# `_next_sunday_0200_et_iso` (operator originally said 02:00 ET, then
# revised to 14:00 ET on 2026-05-12 once the Sunday 2 PM ET GPU reservation
# went in). Keep the old name pointing at the new function so any in-flight
# callers don't break.
_next_sunday_0200_et_iso = _next_sunday_training_iso


def _empty_track_row(track_id: str, role: str, headline_metric: str) -> dict[str, Any]:
    """Stable row shape for an unregistered / no-data track."""
    return {
        "track_id": track_id,
        "role": role,
        "headline_metric": headline_metric,
        "current_adapter": None,
        "current_adapter_version": None,
        "last_train_ts": None,
        "last_eval_scores": {},
        "headline_score": None,
        "eligibility": "no-data",
        "examples_trained_this_week": 0,
    }


def _eligibility_for(track: dict[str, Any]) -> str:
    """Map a model-forge track payload → one of:
        "promoted" | "shadow" | "regressed" | "no-data"

    Heuristics (cheap; the card only colour-codes off this):
      - no champion or no scores → "no-data"
      - champion exists + last_run_status == "regressed_rollback" → "regressed"
      - champion exists + last_run shadowed (not yet promoted) → "shadow"
      - else → "promoted"
    """
    champion = track.get("champion_adapter_path") or track.get("current_adapter")
    if not champion:
        return "no-data"
    last_status = (track.get("last_run_status") or
                   track.get("last_status") or "").lower()
    if "regress" in last_status or "rollback" in last_status:
        return "regressed"
    if "shadow" in last_status:
        return "shadow"
    return "promoted"


async def _fetch_modelforge_tracks() -> tuple[list[dict[str, Any]] | None, str | None]:
    """GET ModelForge /api/forge/tracks. Returns (tracks_list, error_str).

    Connection refused / timeout → (None, "model-forge unreachable: …").
    Returns the raw track list on success (ModelForge wraps in a list or a
    {tracks: [...]} envelope; we accept both).
    """
    url = MODELFORGE_API_URL.rstrip("/") + "/api/forge/tracks"
    api_key = os.environ.get("MODELFORGE_API_KEY", "").strip()
    headers = {"X-API-Key": api_key} if api_key else {}
    try:
        async with httpx.AsyncClient(timeout=ENDPOINT_TIMEOUT_S) as client:
            resp = await client.get(url, headers=headers)
        if resp.status_code != 200:
            return None, f"model-forge HTTP {resp.status_code}"
        body = resp.json()
    except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
        return None, f"model-forge unreachable: {exc}"
    except (httpx.HTTPError, ValueError) as exc:
        return None, f"model-forge response error: {exc}"

    # Accept either a bare list, {"tracks": [...]}, or an envelope.
    if isinstance(body, list):
        return body, None
    if isinstance(body, dict):
        for key in ("tracks", "data"):
            v = body.get(key)
            if isinstance(v, list):
                return v, None
            if isinstance(v, dict) and isinstance(v.get("tracks"), list):
                return v["tracks"], None
    return [], None


@router.get("/weekly_training")
async def weekly_training() -> dict[str, Any]:
    """Weekly LoRA training pipeline status — one row per ModelForge track.

    Read-only, no auth dep. Degrades soft: if model-forge is unreachable
    we still return the 6-track skeleton + the local reflection count so
    the operator card has something useful to show during the build-up
    week before the training pipeline goes live.

    Envelope contract (see docs/WEEKLY_TRAINING_CARD.md for full schema):

        {
            "status": "ok" | "degraded",
            "data": {
                "tracks": [
                    {
                        "track_id": "trading-reflector",
                        "role": "Reflector",
                        "headline_metric": "predictive_hit_rate_30d",
                        "current_adapter": "run-XXX__gen3" | None,
                        "current_adapter_version": "v20260512" | None,
                        "last_train_ts": "2026-05-12T07:30:00+00:00" | None,
                        "last_eval_scores": {"faithfulness_regex": 0.81, ...},
                        "headline_score": 0.62 | None,
                        "eligibility": "promoted" | "shadow" | "regressed" | "no-data",
                        "examples_trained_this_week": 47,
                    }, ... 6 total
                ],
                "summary": {
                    "n_tracks_registered": 6,
                    "n_tracks_trained": 4,
                    "n_promoted_this_week": 2,
                },
                "reflections_this_week": 12,
                "lessons_injected": 41 | None,
                "next_training_ts": "2026-05-18T06:00:00+00:00",
                "model_forge_url": "http://localhost:8000",
                "model_forge_reachable": True | False,
                "model_forge_error": null | "model-forge unreachable: …",
                "week_started": "2026-05-12T00:00:00+00:00",
            },
            "error": null | "human-readable summary",
            "checked_at": ISO-8601 UTC,
        }
    """
    # ─── Local-only signals (always available, even if MF is down) ────────
    week_started = _monday_of_this_week_utc()
    decisions_path = _first_existing(_DECISIONS_PATHS)
    reflections_this_week = (
        _count_reflections_since(decisions_path, week_started)
        if decisions_path is not None else 0
    )
    llm_calls_path = _first_existing(_LLM_CALLS_PATHS)
    lessons_injected = (
        _count_lessons_injected_since(llm_calls_path, week_started)
        if llm_calls_path is not None else None
    )

    # ─── Remote signal: ModelForge /api/forge/tracks ──────────────────────
    mf_tracks, mf_err = await _fetch_modelforge_tracks()
    mf_reachable = mf_err is None

    # Index returned tracks by track_id for quick lookup. Tolerate either
    # `track_id` or `name` keys (ModelForge schema varies by version).
    mf_by_id: dict[str, dict[str, Any]] = {}
    if mf_tracks:
        for t in mf_tracks:
            if not isinstance(t, dict):
                continue
            tid = t.get("track_id") or t.get("name") or t.get("id")
            if isinstance(tid, str):
                mf_by_id[tid] = t

    rows: list[dict[str, Any]] = []
    n_promoted_this_week = 0
    n_trained = 0

    for track_id, role, headline_metric in _WEEKLY_TRAINING_TRACKS:
        mf_t = mf_by_id.get(track_id)
        if not mf_t:
            rows.append(_empty_track_row(track_id, role, headline_metric))
            continue

        # Adapter id / version label. ModelForge stores `champion_adapter_path`
        # like `data/adapters/{run_id}/gen-{N}/`; we surface the basename plus
        # a date-version derived from champion_promoted_at if available.
        adapter_id = (mf_t.get("champion_adapter_id")
                      or mf_t.get("champion_run_id")
                      or mf_t.get("current_adapter"))
        adapter_path = mf_t.get("champion_adapter_path")
        if adapter_id is None and adapter_path:
            # `data/adapters/run-XYZ/gen-3` → "run-XYZ__gen3"
            parts = [p for p in str(adapter_path).strip("/").split("/") if p]
            if len(parts) >= 2:
                adapter_id = f"{parts[-2]}__{parts[-1]}"

        promoted_at = (mf_t.get("champion_promoted_at")
                       or mf_t.get("last_train_ts")
                       or mf_t.get("last_train_finished_at"))
        adapter_version = None
        if isinstance(promoted_at, str):
            # Strip to a date stamp like "v20260512" for the badge.
            try:
                dt = datetime.fromisoformat(promoted_at.replace("Z", "+00:00"))
                adapter_version = "v" + dt.strftime("%Y%m%d")
            except ValueError:
                adapter_version = None

        scores = mf_t.get("champion_scores") or mf_t.get("last_eval_scores") or {}
        if not isinstance(scores, dict):
            scores = {}

        # Headline score = the metric we want on the dashboard row.
        headline_score = scores.get(headline_metric)
        if headline_score is None and scores:
            # Fallback to any numeric value so the row isn't empty.
            for v in scores.values():
                if isinstance(v, (int, float)):
                    headline_score = float(v)
                    break

        examples = (mf_t.get("examples_trained_this_week")
                    or mf_t.get("last_train_num_samples")
                    or mf_t.get("max_samples")
                    or 0)
        try:
            examples = int(examples)
        except (TypeError, ValueError):
            examples = 0

        eligibility = _eligibility_for(mf_t)
        if adapter_id:
            n_trained += 1

        # "Promoted this week" = champion_promoted_at >= week_started.
        if eligibility == "promoted" and isinstance(promoted_at, str):
            try:
                dt = datetime.fromisoformat(promoted_at.replace("Z", "+00:00"))
                if dt >= week_started:
                    n_promoted_this_week += 1
            except ValueError:
                pass

        rows.append({
            "track_id": track_id,
            "role": role,
            "headline_metric": headline_metric,
            "current_adapter": adapter_id,
            "current_adapter_version": adapter_version,
            "last_train_ts": promoted_at,
            "last_eval_scores": scores,
            "headline_score": headline_score,
            "eligibility": eligibility,
            "examples_trained_this_week": examples,
        })

    summary = {
        "n_tracks_registered": len(rows),
        "n_tracks_trained": n_trained,
        "n_promoted_this_week": n_promoted_this_week,
    }

    # Envelope status:
    #   "degraded" when model-forge is unreachable OR no track has been
    #               trained yet (early build-up week — operator still wants
    #               to see the card render, with a clear "training pipeline
    #               starting up" message).
    #   "ok"       otherwise.
    if not mf_reachable:
        env_status = "degraded"
        env_error = mf_err
    elif n_trained == 0:
        # mf-api is reachable, 6 tracks registered, just no LoRA adapter
        # has been trained yet. NOT a failure state — the operator's
        # weekly Sunday 14:00 ET window is when training kicks off.
        env_status = "ready"
        env_error = f"ready · {len(rows)} tracks registered · awaiting first training cycle"
    else:
        env_status = "ok"
        env_error = None

    data = {
        "tracks": rows,
        "summary": summary,
        "reflections_this_week": reflections_this_week,
        "lessons_injected": lessons_injected,
        "next_training_ts": _next_sunday_0200_et_iso(),
        "model_forge_url": MODELFORGE_API_URL,
        "model_forge_reachable": mf_reachable,
        "model_forge_error": mf_err,
        "week_started": week_started.isoformat(),
    }
    return _envelope(env_status, data=data, error=env_error)
# /api/ops/llm_calls — the "LLM activity" dashboard card
# /api/ops/llm_calls/{call_id} — single-record drill-down for the modal
#
# Reads ``stocks/memory/llm-calls.jsonl`` (the same file the LLM tracker
# appends to). The list endpoint returns metadata-only by default to keep
# the response small even when the file has SHARK_LLM_LOG_FULL_TEXT=1 lines
# (which can be 1-4 KB each). The detail endpoint always returns the full
# record. Both are read-only, no auth dep — matches the rest of the ops
# read endpoints.
#
# call_id is the URL-encoded ISO timestamp. The tracker doesn't generate
# a UUID per call but the timestamps include microseconds, so collisions
# are effectively impossible in practice.
# ──────────────────────────────────────────────────────────────────────────

import re as _llm_re
from urllib.parse import unquote as _llm_unquote


def _llm_log_paths() -> list[Path]:
    """The live JSONL is searched in three candidate locations: bind-mount
    path inside the dashboard container, repo-relative path (worktree or
    main checkout), and a $HOME-relative fallback (replaces the previous
    hardcoded operator path). First match wins. Mirrors the pattern used
    by shark_override_health. AUDIT 2026-05-12 Critical #1."""
    return [
        STOCKS_ROOT / "memory" / "llm-calls.jsonl",
        Path(__file__).resolve().parents[2] / "stocks" / "memory" / "llm-calls.jsonl",
        _HOME_REPO / "stocks" / "memory" / "llm-calls.jsonl",
    ]


def _resolve_llm_log() -> Path | None:
    for p in _llm_log_paths():
        if p.is_file():
            return p
    return None


# Strip the heavy text fields when ``include_text=0`` so the index payload
# stays under a few KB even if every record was written with the flag on.
_LLM_HEAVY_FIELDS = ("prompt", "system_message", "response_text", "messages")


def _strip_heavy(rec: dict) -> dict:
    return {k: v for k, v in rec.items() if k not in _LLM_HEAVY_FIELDS}


def _read_jsonl_tail(path: Path, *, max_records: int) -> list[dict]:
    """Read up to ``max_records`` lines from the tail of the file.

    Reverse-line read so we don't slurp a 50 MB file just to get the
    last 50 records. Works in chunks from the end of the file, finds
    line boundaries, and parses only the relevant tail.

    Note: order returned is *most-recent-first* — callers can reverse
    if they want oldest-first. The dashboard wants newest-first anyway.
    """
    if not path.is_file() or max_records <= 0:
        return []
    out: list[dict] = []
    try:
        with path.open("rb") as fh:
            fh.seek(0, os.SEEK_END)
            file_size = fh.tell()
            chunk = 65536
            buf = b""
            pos = file_size
            while pos > 0 and len(out) < max_records:
                read = min(chunk, pos)
                pos -= read
                fh.seek(pos)
                buf = fh.read(read) + buf
                # Split into complete lines. The first element may be a
                # partial line if we haven't reached the file start, so
                # keep it in buf and process the rest.
                lines = buf.split(b"\n")
                if pos > 0:
                    buf = lines[0]
                    lines = lines[1:]
                else:
                    buf = b""
                # Walk newest → oldest within this chunk
                for line in reversed(lines):
                    if not line.strip():
                        continue
                    try:
                        out.append(json.loads(line.decode("utf-8")))
                    except (json.JSONDecodeError, UnicodeDecodeError):
                        continue
                    if len(out) >= max_records:
                        break
    except OSError as exc:
        logger.warning("llm_calls: tail read failed for %s: %s", path, exc)
        return out
    return out


# Canonical-role mapping for the AgentFlow strip (dashboard SPA). Maps the
# raw ``agent`` field written by the tracker to one of six canonical roles
# the operator thinks in: regime_tagger → indicator_selector → bull_debater
# → bear_debater → arbiter → reflector. Anything not listed here is filed
# under its own raw name (still visible in the LLM activity list below the
# strip). Cheap dict-lookup + prefix check, runs in the same single pass as
# the rest of the summary so it adds < 1 ms even on 5k-row windows.
_AGENT_ROLE_MAP = {
    # Conceptual role  : list of agent-name prefixes that map to it
    "regime_tagger": ("regime_tagger", "trading-regime-tagger"),
    "indicator_selector": ("indicator_selector",),
    "bull_debater": ("analyst_bull", "debate.bull", "risk_debate.aggressive"),
    "bear_debater": ("analyst_bear", "debate.bear", "risk_debate.conservative"),
    "arbiter": (
        "decision_arbiter",
        "debate.arbiter",
        "combined_analyst",  # the merged bull+bear+arbiter call
        "risk_debate.judge",
        "risk_debate.neutral",
        "trade_reviewer",
    ),
    "reflector": ("outcome_resolver", "reflector"),
}


def _canonical_role(agent: str) -> str | None:
    """Return the canonical AgentFlow role for ``agent``, or None if the
    agent name doesn't fit any of the strip's six conceptual roles."""
    a = (agent or "").lower()
    for role, prefixes in _AGENT_ROLE_MAP.items():
        for p in prefixes:
            if a == p or a.startswith(p + ".") or a.startswith(p + "_"):
                return role
    return None


def _summarise_llm_window(calls: list[dict]) -> dict[str, Any]:
    """Card-summary numbers — total calls, tokens, avg latency, ollama
    fraction, success-rate, by-agent counts. Mirrors what TodayScoreboard
    does for trades but for LLM calls.

    Also computes ``by_role_detail`` keyed by canonical AgentFlow roles
    (regime_tagger, indicator_selector, bull_debater, bear_debater,
    arbiter, reflector). Each entry is shaped like::

        {
          "count": int, "success": int, "fail": int,
          "avg_latency_s": float, "p95_latency_s": float,
          "last_ts": iso-string | None, "last_success": bool,
          "last_gist": "first ~120 chars of response_text" | None,
          "model": "most-common model for this role",
          "raw_agents": ["analyst_bull", "debate.bull.r1", ...],
        }

    The AgentFlow strip on the ops dashboard renders one box per role from
    this dict. Roles with zero calls are NOT included — the frontend pads
    missing roles with placeholder boxes so the strip stays a fixed shape.
    """
    if not calls:
        return {
            "total_calls": 0,
            "total_prompt_tokens": 0,
            "total_completion_tokens": 0,
            "total_tokens": 0,
            "avg_latency_s": 0.0,
            "p95_latency_s": 0.0,
            "max_latency_s": 0.0,
            "ollama_pct": 0.0,
            "anthropic_pct": 0.0,
            "success_pct": 100.0,
            "by_agent": {},
            "by_model": {},
            "by_tier": {"fast": 0, "deep": 0},
            "by_role_detail": {},
        }

    by_agent: dict[str, int] = {}
    by_model: dict[str, int] = {}
    by_tier = {"fast": 0, "deep": 0}
    provider_counts: dict[str, int] = {}
    successes = 0
    p_toks = 0
    c_toks = 0
    latencies: list[float] = []

    # AgentFlow per-role accumulators. Keyed by canonical role name.
    role_acc: dict[str, dict[str, Any]] = {}

    for c in calls:
        agent = c.get("agent", "unknown")
        by_agent[agent] = by_agent.get(agent, 0) + 1
        m = c.get("model", "?")
        by_model[m] = by_model.get(m, 0) + 1
        t = c.get("tier", "deep")
        if t in by_tier:
            by_tier[t] += 1
        prov = c.get("provider", "unknown")
        provider_counts[prov] = provider_counts.get(prov, 0) + 1
        # Success heuristic: a record exists ⇒ the tracker captured a
        # response; explicit ``success=false`` (added by future versions)
        # would flip this. For now, any record with completion_tokens > 0
        # OR no explicit ``error`` field counts as successful.
        rec_success = not (c.get("success") is False or c.get("error"))
        if rec_success:
            successes += 1
        p_toks += int(c.get("prompt_tokens") or 0)
        c_toks += int(c.get("completion_tokens") or 0)
        latencies.append(float(c.get("latency_seconds") or 0))

        # ── AgentFlow per-role aggregation ────────────────────────────
        role = _canonical_role(agent)
        if role is None:
            continue
        slot = role_acc.setdefault(role, {
            "count": 0, "success": 0, "fail": 0,
            "latencies": [],
            "completion_tokens": [],
            "last_ts": None, "last_success": True,
            "last_gist": None, "last_agent": None,
            "models": {}, "raw_agents": {},
        })
        slot["count"] += 1
        if rec_success:
            slot["success"] += 1
        else:
            slot["fail"] += 1
        slot["latencies"].append(float(c.get("latency_seconds") or 0))
        slot["completion_tokens"].append(int(c.get("completion_tokens") or 0))
        slot["models"][m] = slot["models"].get(m, 0) + 1
        slot["raw_agents"][agent] = slot["raw_agents"].get(agent, 0) + 1
        # The tail-reader returns newest-first, so the FIRST record we see
        # per role is the most recent. Only set ``last_*`` on the first hit.
        if slot["last_ts"] is None:
            slot["last_ts"] = c.get("timestamp")
            slot["last_success"] = rec_success
            slot["last_agent"] = agent
            resp = c.get("response_text") or ""
            if resp:
                # One-line gist: collapse whitespace + cap at 120 chars.
                gist = " ".join(str(resp).split())
                if len(gist) > 120:
                    gist = gist[:117] + "…"
                slot["last_gist"] = gist

    n = len(calls)
    avg_lat = sum(latencies) / n
    lat_sorted = sorted(latencies)
    p95_idx = max(0, int(0.95 * n) - 1)
    p95 = lat_sorted[p95_idx] if lat_sorted else 0.0
    max_lat = max(latencies) if latencies else 0.0
    ollama_n = provider_counts.get("ollama", 0)
    anthropic_n = provider_counts.get("anthropic", 0)

    # Reduce per-role accumulators to the public shape. Latencies become
    # avg + p95 (matches the headline summary). Models reduces to the
    # most-common single model string for the strip's "Model" line.
    by_role_detail: dict[str, dict[str, Any]] = {}
    for role, slot in role_acc.items():
        lats = slot["latencies"]
        avg_r = sum(lats) / len(lats) if lats else 0.0
        ls = sorted(lats)
        p95_r = ls[max(0, int(0.95 * len(ls)) - 1)] if ls else 0.0
        toks = slot["completion_tokens"]
        avg_tok = (sum(toks) / len(toks)) if toks else 0.0
        top_model = max(slot["models"].items(), key=lambda kv: kv[1])[0] if slot["models"] else "?"
        # ``last_gist`` keeps the legacy field name (mirrors what AgentFlow
        # already reads) but Tier E also wants a copy under
        # ``last_response_gist`` for the inline preview line in each agent
        # box. Same value — two keys keeps both call-sites happy without a
        # rename migration on the existing AgentFlow strip.
        by_role_detail[role] = {
            "count": slot["count"],
            "success": slot["success"],
            "fail": slot["fail"],
            "avg_latency_s": round(avg_r, 2),
            "p95_latency_s": round(p95_r, 2),
            "tokens_avg": round(avg_tok, 1),
            "last_ts": slot["last_ts"],
            "last_success": slot["last_success"],
            "last_gist": slot["last_gist"],
            "last_response_gist": slot["last_gist"],
            "last_agent": slot["last_agent"],
            "model": top_model,
            "raw_agents": dict(sorted(slot["raw_agents"].items(), key=lambda kv: -kv[1])),
        }

    return {
        "total_calls": n,
        "total_prompt_tokens": p_toks,
        "total_completion_tokens": c_toks,
        "total_tokens": p_toks + c_toks,
        "avg_latency_s": round(avg_lat, 2),
        "p95_latency_s": round(p95, 2),
        "max_latency_s": round(max_lat, 2),
        "ollama_pct": round(100.0 * ollama_n / n, 1),
        "anthropic_pct": round(100.0 * anthropic_n / n, 1),
        "success_pct": round(100.0 * successes / n, 1),
        "by_agent": dict(sorted(by_agent.items(), key=lambda kv: -kv[1])),
        "by_model": dict(sorted(by_model.items(), key=lambda kv: -kv[1])),
        "by_tier": by_tier,
        "providers": provider_counts,
        "by_role_detail": by_role_detail,
    }


@router.get("/llm_calls")
async def llm_calls(
    limit: int = 50,
    agent: str | None = None,
    since: str | None = None,
    q: str | None = None,
    include_text: int = 0,
    model: str | None = None,
    min_latency: float | None = None,
    max_latency: float | None = None,
    role: str | None = None,
):
    """LLM activity feed — paginated list of recent LLM calls.

    Query parameters
    ----------------
    limit         : int, default 50, max 500
    agent         : substring filter (case-insensitive) on the ``agent`` field
    model         : substring filter on the ``model`` field
    since         : ISO timestamp; only calls newer than this are returned
    q             : regex filter; matches against agent + model + tier + role.
                    If ``include_text=1`` is also set, the regex is ALSO
                    applied to prompt + system_message + response_text.
    include_text  : 0 (default) → strip prompt/system/response from the
                    response (tiny payload). 1 → keep them.
    min_latency   : seconds; reject calls faster than this
    max_latency   : seconds; reject calls slower than this
    role          : canonical AgentFlow role (regime_tagger, bull_debater,
                    bear_debater, arbiter, reflector, indicator_selector).
                    Filters records whose raw agent name maps to that
                    canonical role via ``_canonical_role()``. Used by the
                    Tier-E AgentLogsDrawer to fetch the last N calls for
                    one specific pipeline stage with FULL prompt/response.

    Response: ``{status, data, error, checked_at}`` where ``data`` is::

        {
          "calls": [<record>, ...],          # newest first
          "total_in_window": <int>,          # before pagination
          "summary": {...},                  # 24h-window aggregates
          "log_path": "...",                 # for the empty-state hint
          "log_size_bytes": <int>,
        }
    """
    limit = max(1, min(500, int(limit)))
    include_full = int(include_text or 0) == 1

    log_path = _resolve_llm_log()
    if log_path is None:
        return _envelope(
            "degraded",
            data={
                "calls": [],
                "total_in_window": 0,
                "summary": _summarise_llm_window([]),
                "log_path": str(_llm_log_paths()[0]),
                "log_size_bytes": 0,
                "include_text": include_full,
            },
            error="llm-calls.jsonl not found — tracker has not written yet",
        )

    # Read enough tail to satisfy the largest reasonable request. Cap at
    # 5000 so the summary numbers stay stable but we don't churn the
    # whole disk if the file is large.
    tail_records = _read_jsonl_tail(log_path, max_records=5000)
    # Tail returns newest-first; preserve that order for the API.

    # ── Filters ──────────────────────────────────────────────────────
    cutoff_ts: float | None = None
    if since:
        try:
            cutoff_ts = datetime.fromisoformat(since.replace("Z", "+00:00")).timestamp()
        except (ValueError, TypeError):
            cutoff_ts = None

    pattern = None
    if q:
        try:
            pattern = _llm_re.compile(q, _llm_re.IGNORECASE)
        except _llm_re.error as exc:
            return _envelope("down", data=None, error=f"bad regex: {exc}")

    # Canonical role filter — separate from substring ``agent`` filter
    # because the canonical role is a logical grouping (multiple raw agent
    # names map to one role).
    role_norm = (role or "").strip().lower() or None

    filtered: list[dict] = []
    for rec in tail_records:
        if agent and agent.lower() not in str(rec.get("agent", "")).lower():
            continue
        if model and model.lower() not in str(rec.get("model", "")).lower():
            continue
        if role_norm is not None:
            if _canonical_role(rec.get("agent")) != role_norm:
                continue
        if min_latency is not None and float(rec.get("latency_seconds") or 0) < float(min_latency):
            continue
        if max_latency is not None and float(rec.get("latency_seconds") or 0) > float(max_latency):
            continue
        if cutoff_ts is not None:
            ts_str = rec.get("timestamp") or ""
            try:
                ts = datetime.fromisoformat(str(ts_str).replace("Z", "+00:00")).timestamp()
            except (ValueError, TypeError):
                continue
            if ts < cutoff_ts:
                continue
        if pattern is not None:
            hay = " ".join(str(rec.get(k, "")) for k in ("agent", "model", "tier", "role"))
            if include_full:
                hay += " " + " ".join(
                    str(rec.get(k) or "") for k in ("prompt", "system_message", "response_text")
                )
            if not pattern.search(hay):
                continue
        filtered.append(rec)

    total_in_window = len(filtered)
    page = filtered[:limit]
    if not include_full:
        page = [_strip_heavy(r) for r in page]

    # Summary uses the FULL 24h window (not just the filtered page) so the
    # card's "calls / tokens / avg lat" numbers don't shrink when the user
    # types a search term — operator wants the search to filter the rows
    # but keep the headline numbers honest about overall activity.
    cutoff_24h = datetime.now(timezone.utc).timestamp() - 86400
    window_24h: list[dict] = []
    for rec in tail_records:
        ts_str = rec.get("timestamp") or ""
        try:
            ts = datetime.fromisoformat(str(ts_str).replace("Z", "+00:00")).timestamp()
        except (ValueError, TypeError):
            continue
        if ts >= cutoff_24h:
            window_24h.append(rec)

    summary = _summarise_llm_window(window_24h)

    return _envelope(
        "ok",
        data={
            "calls": page,
            "total_in_window": total_in_window,
            "total_24h": len(window_24h),
            "summary": summary,
            "log_path": str(log_path),
            "log_size_bytes": file_size_or_zero(log_path),
            "include_text": include_full,
        },
    )


def file_size_or_zero(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0


@router.get("/llm_calls/{call_id:path}")
async def llm_call_detail(call_id: str):
    """Single-record drill-down for the modal.

    ``call_id`` is the URL-encoded ISO timestamp (the tracker doesn't
    generate UUIDs; timestamps have microsecond resolution so collisions
    are practically impossible).

    Status codes:
      200 — record found in the live file (full payload)
      404 — record not found anywhere (live + archives both miss it)
      410 — record is in an archive (file rotated); returns the archive
            path in ``data.archive_path`` so operator can grep manually
    """
    target = _llm_unquote(call_id).strip()
    if not target:
        raise HTTPException(status_code=400, detail="empty call_id")

    log_path = _resolve_llm_log()

    # Live file first — walk the whole file (don't truncate to a tail) so
    # an older record from earlier in the day still resolves.
    if log_path is not None and log_path.is_file():
        try:
            with log_path.open("r", encoding="utf-8") as fh:
                for line in fh:
                    if not line.strip():
                        continue
                    try:
                        rec = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if str(rec.get("timestamp")) == target:
                        return _envelope("ok", data={"call": rec, "source": "live"})
        except OSError as exc:
            logger.warning("llm_call_detail: live read failed: %s", exc)

    # Archives — delegate to the rotator helper so the archive format
    # (.jsonl.gz) and live format (.jsonl) live in one place.
    try:
        # Import lazily so the dashboard process doesn't pay the cost
        # at module load — the archive code is rarely hit.
        import sys as _sys
        repo_root = Path(__file__).resolve().parents[2]
        stocks_path = repo_root / "stocks"
        if str(stocks_path) not in _sys.path:
            _sys.path.insert(0, str(stocks_path))
        from shark.llm.rotate import find_record_in_archives  # type: ignore
    except ImportError as exc:
        logger.warning("llm_call_detail: rotate import failed: %s", exc)
        raise HTTPException(
            status_code=404,
            detail=f"record {target} not in live log; archive search unavailable: {exc}",
        )

    if log_path is None:
        log_path = _llm_log_paths()[0]

    rec, archive = find_record_in_archives(log_path, target)
    if rec is not None:
        # Found in archive — operator still wants the data, but flag it.
        raise HTTPException(
            status_code=410,
            detail={
                "error": "record rotated to archive",
                "archive_path": str(archive),
                "call": rec,
                "hint": f"grep manually: zcat '{archive}' | jq 'select(.timestamp==\"{target}\")'",
            },
        )
    if archive is not None:
        raise HTTPException(
            status_code=410,
            detail={
                "error": "record not in live log; likely rotated",
                "newest_archive": str(archive),
                "hint": f"try: zcat '{archive}' | grep '{target}'",
            },
        )

    raise HTTPException(status_code=404, detail=f"call_id {target} not found")
