"""
v5 router: ``GET /api/v5/portfolio``.

Returns the raw producer output (no envelope — spec §5.1).
RFC 7807 problem-detail on error (FastAPI default HTTPException).

Source of truth: ``user_data.modules.producers.portfolio.portfolio_snapshot``.
Closes B1.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v5", tags=["v5"])


@router.get("/portfolio")
async def get_portfolio() -> dict:
    """Capital, equity, peak, DD, day-PnL per side, with `_meta`.

    Day-PnL on the stocks side is `portfolio_value − last_equity` from
    Alpaca's session-boundary snapshot — fixes B1 (the legacy
    `stocks_equity − stocks_peak_equity` was drawdown, not day move).
    """
    try:
        from user_data.modules.producers.portfolio import portfolio_snapshot
        return portfolio_snapshot()
    except Exception as exc:
        logger.exception("v5/portfolio: %s", exc)
        # RFC 7807 — FastAPI serializes HTTPException(detail=...) to JSON.
        raise HTTPException(
            status_code=503,
            detail={
                "type": "about:blank",
                "title": "Portfolio producer unavailable",
                "status": 503,
                "detail": str(exc),
            },
        ) from exc
