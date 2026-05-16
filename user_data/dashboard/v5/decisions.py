"""V5 decision-audit / explainability feed.

Scaffold endpoint that surfaces the most recent decisions from
``quanta_schema.decisions``. The B8 forensic-surface requirement (display
24h historical single-name-cap breaches) is met by joining with
``user_data/data/risk_alerts.jsonl`` — handled in the alerts router; this
endpoint focuses on entry/exit reasoning rows.
"""
from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v5/decisions", tags=["v5", "decisions"])


def _meta(snapshot_ts: datetime) -> dict[str, Any]:
    return {
        "snapshot_ts": snapshot_ts.isoformat(),
        "age_s": 0,
        "stale": False,
        "market_open_now": None,
        "source": "v5/decisions",
    }


@router.get("")
async def decisions(limit: int = 50) -> dict[str, Any]:
    """Most recent decision rows (entry/exit reasoning).

    Returns an empty list when postgres is unavailable — the dashboard
    must still render. ``limit`` clamps to ``[1, 500]``.
    """
    limit = max(1, min(int(limit), 500))
    rows: list[dict[str, Any]] = []
    try:
        from .. import ops_db  # type: ignore[attr-defined]
        if getattr(ops_db, "_HAVE_PG", False):
            with ops_db._connect() as conn, conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT ts, symbol, side, action, reason, regime, score
                      FROM quanta_schema.decisions
                  ORDER BY ts DESC
                     LIMIT %s
                    """,
                    (limit,),
                )
                for r in cur.fetchall() or []:
                    rows.append({
                        "ts": r.get("ts").isoformat() if r.get("ts") else None,
                        "symbol": r.get("symbol"),
                        "side": r.get("side"),
                        "action": r.get("action"),
                        "reason": r.get("reason"),
                        "regime": r.get("regime"),
                        "score": float(r.get("score")) if r.get("score") is not None else None,
                    })
    except Exception as exc:
        logger.debug("decisions: db read failed: %s", exc)

    return {
        "decisions": rows,
        "_meta": _meta(datetime.now(tz=UTC)),
    }


__all__ = ["router"]
