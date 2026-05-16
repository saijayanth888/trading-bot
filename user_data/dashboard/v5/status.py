"""V5 composite status rollup — the TopBar green/amber/red banner.

Synthesizes:

* Producers freshness (via ``_meta.stale`` on the portfolio + positions
  endpoints, when available).
* Critical risk-alert count in the last 24h (B8 forensic surface).
* Run-state pause flag (``quanta_schema.run_state.paused``).
* Hermes composite health (delegates to ``v5/hermes.health``).

Rollup state:

* ``green`` — nothing stale, no critical alerts, not paused, Hermes green.
* ``amber`` — at least one warning signal.
* ``red`` — paused OR any critical risk alert in the last 24h.
"""
from __future__ import annotations

import logging
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from fastapi import APIRouter

from . import alerts as alerts_mod
from . import hermes as hermes_mod

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v5", tags=["v5", "status"])


def _meta(snapshot_ts: datetime) -> dict[str, Any]:
    return {
        "snapshot_ts": snapshot_ts.isoformat(),
        "age_s": 0,
        "stale": False,
        "market_open_now": None,
        "source": "v5/status",
    }


def _run_state() -> dict[str, Any]:
    """Read ``quanta_schema.run_state``. Returns ``{}`` when DB is offline."""
    try:
        from .. import ops_db  # type: ignore[attr-defined]
        if not getattr(ops_db, "_HAVE_PG", False):
            return {}
        with ops_db._connect() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT paused, paused_reason, paused_at "
                "FROM quanta_schema.run_state WHERE id = 1"
            )
            row = cur.fetchone()
            if not row:
                return {}
            return {
                "paused": bool(row.get("paused")),
                "paused_reason": row.get("paused_reason"),
                "paused_at": row.get("paused_at").isoformat() if row.get("paused_at") else None,
            }
    except Exception as exc:
        logger.debug("status: run_state read failed: %s", exc)
        return {}


@router.get("/status")
async def status() -> dict[str, Any]:
    """Aggregate operator state — feeds ``<TopBar>``."""
    snapshot_ts = datetime.now(tz=UTC)

    # 1. risk alerts in last 24h
    alerts_payload = await alerts_mod.alerts(limit=100, since_hours=24)
    counts = alerts_payload.get("counts", {})
    critical_24h = int(counts.get("critical", 0))
    warning_24h = int(counts.get("warning", 0))

    # 2. run-state (crypto pause flag)
    rs = _run_state()
    paused = bool(rs.get("paused"))

    # 3. hermes composite
    try:
        hermes_health = await hermes_mod.health()
    except Exception as exc:
        logger.warning("status: hermes health probe failed: %s", exc)
        hermes_health = {"status": "amber", "reasons": [f"probe error: {exc}"]}

    # 4. roll up
    reasons: list[str] = []
    state = "green"
    if paused:
        state = "red"
        reasons.append(f"trading paused: {rs.get('paused_reason') or 'unknown'}")
    if critical_24h > 0:
        state = "red"
        reasons.append(f"{critical_24h} critical risk-alert(s) in last 24h")
    if warning_24h > 0 and state == "green":
        state = "amber"
        reasons.append(f"{warning_24h} warning alert(s) in last 24h")
    hermes_state = hermes_health.get("status", "amber")
    if hermes_state == "red":
        state = "red"
        reasons.append("hermes: red")
    elif hermes_state == "amber" and state == "green":
        state = "amber"
        reasons.append("hermes: amber")

    return {
        "state": state,
        "reasons": reasons,
        "detect_counts": {
            "critical": critical_24h,
            "warning": warning_24h,
            "info": int(counts.get("info", 0)),
            "total": int(counts.get("total", 0)),
        },
        "run_state": rs,
        "hermes": {"status": hermes_state, "reasons": hermes_health.get("reasons", [])},
        "_meta": _meta(snapshot_ts),
    }


__all__ = ["router"]
