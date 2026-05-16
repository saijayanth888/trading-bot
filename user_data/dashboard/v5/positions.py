"""
v5 router: ``GET /api/v5/positions``.

UNION crypto fills + wheel state + shark holdings. Closes B6/B9.
Raw producer output, no envelope.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v5", tags=["v5"])


@router.get("/positions")
async def get_positions() -> dict:
    """Unified open positions across crypto / wheel / shark."""
    try:
        from user_data.modules.producers.positions import positions_snapshot
        return positions_snapshot()
    except Exception as exc:
        logger.exception("v5/positions: %s", exc)
        raise HTTPException(
            status_code=503,
            detail={
                "type": "about:blank",
                "title": "Positions producer unavailable",
                "status": 503,
                "detail": str(exc),
            },
        ) from exc
