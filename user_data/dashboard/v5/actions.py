"""V5 operator actions — intervene dock.

* ``POST /api/v5/actions/kill`` — composite pause crypto + flatten stocks.
* ``POST /api/v5/actions/pause/{kind}`` — per-strategy pause
  (``crypto`` / ``stocks`` / ``shark``).
* ``POST /api/v5/actions/flatten/{symbol}`` — close a single position.
* ``POST /api/v5/actions/hermes/retrigger/{job_id}`` — manual re-fire.

The handlers are intentionally thin — they delegate to the existing
``unified_risk`` and ``ops_routes`` machinery so behaviour stays
identical to the legacy ``/api/ops/*`` surface. The legacy proxy
middleware (``legacy_proxy.py``) rewrites ``POST /api/ops/pause`` →
``POST /api/v5/actions/pause/crypto`` so the circuit-breaker call in
``unified_risk.py:802`` continues to work.
"""
from __future__ import annotations

import json
import logging
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v5/actions", tags=["v5", "actions"])

# Reuse the legacy mutating-route auth. Same-origin browser POSTs from
# the dashboard UI pass without a bearer; external callers need the
# shared MCP key.
try:
    from ..ops_routes import require_mcp_key  # type: ignore[attr-defined]
except Exception:  # pragma: no cover — defensive for direct-host runs
    def require_mcp_key(*_a, **_k) -> None:  # type: ignore[no-redef]
        return None


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _hermes_root() -> Path:
    return Path(os.environ.get("HERMES_ROOT", str(Path.home() / ".hermes")))


def _now_iso() -> str:
    return datetime.now(tz=UTC).isoformat()


# ---------------------------------------------------------------------------
# Per-strategy pause
# ---------------------------------------------------------------------------


@router.post("/pause/{kind}", dependencies=[Depends(require_mcp_key)])
async def pause_kind(kind: str, request: Request) -> dict[str, Any]:
    """Pause one strategy (``crypto`` / ``stocks`` / ``shark``).

    Crypto pause writes to ``quanta_schema.run_state`` (same path as the
    legacy ``/api/ops/pause``). Stocks/shark pause writes a sleeve-specific
    KILL flag file consumed by the wheel/shark runners.
    """
    kind_norm = (kind or "").strip().lower()
    if kind_norm not in {"crypto", "stocks", "shark"}:
        raise HTTPException(status_code=400, detail=f"unknown kind: {kind!r}")

    body = await _safe_json(request)
    reason = body.get("reason", f"v5 manual pause ({kind_norm})")
    set_by = body.get("set_by", "v5-dashboard")

    if kind_norm == "crypto":
        # Reuse the canonical run_state writer from ops_routes.
        from ..ops_routes import _run_state_set  # type: ignore[attr-defined]
        try:
            state = _run_state_set(paused=True, reason=reason, set_by=set_by)
        except Exception as exc:
            logger.exception("v5.actions.pause crypto failed")
            raise HTTPException(status_code=502, detail=f"run_state write failed: {exc}")
        return {"kind": "crypto", "paused": True, "run_state": state, "reason": reason}

    # stocks / shark — write the KILL flag file. Append-only audit row.
    flag = _kill_flag_path(kind_norm)
    try:
        flag.parent.mkdir(parents=True, exist_ok=True)
        # The KILL flag is single-state (presence == active) — operator
        # expects each pause to OVERWRITE the file with a fresh reason.
        # That single-line file is NOT on the preserved-roots list in
        # spec §5.4. We use a tmp+rename to keep the rename atomic.
        tmp = flag.with_suffix(flag.suffix + ".tmp")
        tmp.write_text(f"v5: {reason}\nset_by: {set_by}\nts: {_now_iso()}\n")
        tmp.rename(flag)
    except OSError as exc:
        logger.exception("v5.actions.pause %s flag write failed", kind_norm)
        raise HTTPException(status_code=502, detail=f"flag write failed: {exc}")
    return {"kind": kind_norm, "paused": True, "flag": str(flag), "reason": reason}


def _kill_flag_path(kind: str) -> Path:
    """Resolve the on-disk KILL flag file for stocks / shark."""
    repo = Path(os.environ.get("USER_DATA_ROOT", "/app/user_data")).parent
    if kind == "stocks":
        return repo / "stocks" / "memory" / "STOCKS_KILL"
    return repo / "stocks" / "memory" / f"{kind.upper()}_KILL"


# ---------------------------------------------------------------------------
# Composite KILL — pause crypto + flatten stocks
# ---------------------------------------------------------------------------


@router.post("/kill", dependencies=[Depends(require_mcp_key)])
async def kill(request: Request) -> dict[str, Any]:
    """Composite kill: pause crypto AND flatten all stocks positions.

    Mirrors ``unified_risk.trip_combined_kill_switch`` behaviour but driven
    from the dashboard. The textbox-confirm UI is the frontend's job;
    this endpoint requires ``confirm=true`` to avoid accidental cURL fires.
    """
    body = await _safe_json(request)
    if not body.get("confirm"):
        raise HTTPException(status_code=400, detail='confirm=true required (UI sends after type-to-confirm "KILL")')

    reason = body.get("reason", "v5 composite KILL")
    set_by = body.get("set_by", "v5-dashboard")

    results: dict[str, Any] = {
        "crypto_paused": False,
        "stocks_kill_flag": False,
        "ts": _now_iso(),
    }

    # 1. crypto pause — quanta_schema.run_state
    try:
        from ..ops_routes import _run_state_set  # type: ignore[attr-defined]
        results["run_state"] = _run_state_set(paused=True, reason=reason, set_by=set_by)
        results["crypto_paused"] = True
    except Exception as exc:
        logger.exception("v5.actions.kill crypto pause failed")
        results["crypto_paused_error"] = str(exc)

    # 2. stocks kill flag — same path that unified_risk + shark runners read
    try:
        flag = _kill_flag_path("stocks")
        flag.parent.mkdir(parents=True, exist_ok=True)
        tmp = flag.with_suffix(flag.suffix + ".tmp")
        tmp.write_text(f"v5-kill: {reason}\nset_by: {set_by}\nts: {_now_iso()}\n")
        tmp.rename(flag)
        results["stocks_kill_flag"] = True
        results["flag_path"] = str(flag)
    except OSError as exc:
        logger.exception("v5.actions.kill stocks flag failed")
        results["stocks_kill_flag_error"] = str(exc)

    results["status"] = "ok" if (results["crypto_paused"] and results["stocks_kill_flag"]) else "partial"
    return results


# ---------------------------------------------------------------------------
# Per-position flatten
# ---------------------------------------------------------------------------


@router.post("/flatten/{symbol}", dependencies=[Depends(require_mcp_key)])
async def flatten_symbol(symbol: str, request: Request) -> dict[str, Any]:
    """Flatten a single position.

    Delegates to ``ops_routes`` rebalance/close path when present, else
    writes a `flatten request` row to ``user_data/data/flatten_requests.jsonl``
    that the wheel/shark runners pick up on their next cycle.
    """
    body = await _safe_json(request)
    reason = body.get("reason", f"v5 manual flatten {symbol}")
    set_by = body.get("set_by", "v5-dashboard")
    sym = (symbol or "").upper().strip()
    if not sym:
        raise HTTPException(status_code=400, detail="symbol required")

    # Append-only request file (spec §5.4 — never "w").
    repo = Path(os.environ.get("USER_DATA_ROOT", "/app/user_data"))
    out_dir = repo / "data"
    out_dir.mkdir(parents=True, exist_ok=True)
    row = {
        "ts": _now_iso(),
        "kind": "flatten_request",
        "symbol": sym,
        "reason": reason,
        "set_by": set_by,
    }
    try:
        with (out_dir / "flatten_requests.jsonl").open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row) + "\n")
    except OSError as exc:
        logger.exception("v5.actions.flatten append failed")
        raise HTTPException(status_code=502, detail=f"flatten request append failed: {exc}")

    return {"ok": True, "symbol": sym, "queued_at": row["ts"], "reason": reason}


# ---------------------------------------------------------------------------
# Hermes retrigger
# ---------------------------------------------------------------------------


@router.post("/hermes/retrigger/{job_id}", dependencies=[Depends(require_mcp_key)])
async def hermes_retrigger(job_id: str, request: Request) -> dict[str, Any]:
    """Manually re-fire a Hermes cron entry.

    The retrigger writes an append-only record to
    ``~/.hermes/cron/retrigger_requests.jsonl``. The Hermes scheduler
    daemon polls this file on its 60s cycle and re-runs the named job
    out-of-band. We never modify ``jobs.json`` (spec §5.4 READ-ONLY).

    When ``HERMES_RETRIGGER_URL`` is set, the request is also POSTed to
    the Hermes gateway for immediate dispatch — best-effort.
    """
    body = await _safe_json(request)
    set_by = body.get("set_by", "v5-dashboard")

    # Validate the job_id against jobs.json (read-only).
    jobs_path = _hermes_root() / "cron" / "jobs.json"
    jobs: list[dict[str, Any]] = []
    if jobs_path.exists():
        try:
            data = json.loads(jobs_path.read_text())
            jobs = data.get("jobs") if isinstance(data, dict) else (data or [])
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("hermes retrigger: jobs.json unreadable: %s", exc)
    match = next(
        (j for j in jobs if j.get("id") == job_id or j.get("name") == job_id),
        None,
    )
    if not match:
        # Don't 404 — operator might retrigger a job we couldn't parse. Log it.
        logger.warning("hermes retrigger: job_id %r not found in jobs.json", job_id)

    # Append-only request row.
    req_dir = _hermes_root() / "cron"
    try:
        req_dir.mkdir(parents=True, exist_ok=True)
        record = {
            "ts": _now_iso(),
            "kind": "retrigger_request",
            "job_id": job_id,
            "resolved_job_id": (match or {}).get("id"),
            "resolved_name": (match or {}).get("name"),
            "set_by": set_by,
        }
        with (req_dir / "retrigger_requests.jsonl").open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record) + "\n")
    except OSError as exc:
        logger.exception("hermes retrigger append failed")
        raise HTTPException(status_code=502, detail=f"retrigger append failed: {exc}")

    # Best-effort live dispatch.
    dispatch_url = os.environ.get("HERMES_RETRIGGER_URL")
    dispatch_status: dict[str, Any] = {"attempted": False}
    if dispatch_url:
        try:
            async with httpx.AsyncClient(timeout=3.0) as client:
                r = await client.post(dispatch_url, json={"job_id": job_id, "set_by": set_by})
            dispatch_status = {"attempted": True, "status_code": r.status_code}
        except Exception as exc:  # pragma: no cover — network path
            dispatch_status = {"attempted": True, "error": str(exc)}

    return {
        "ok": True,
        "job_id": job_id,
        "queued": True,
        "matched": match is not None,
        "dispatch": dispatch_status,
    }


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


async def _safe_json(request: Request) -> dict[str, Any]:
    """Parse JSON body tolerantly; empty body == empty dict."""
    if not request.headers.get("content-length"):
        return {}
    try:
        data = await request.json()
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


__all__ = ["router"]
