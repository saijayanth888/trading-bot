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
        snap = portfolio_snapshot()
        # Frontend types (frontend-v5/src/lib/types-fallback.ts:SidePortfolio)
        # expect `equity_usd` / `peak_usd` plus a top-level pause/kill threshold.
        # Producer emits `equity` / `peak_equity`. Alias the response here so
        # both names work — additive, doesn't break the producer contract.
        for side in ("combined", "crypto", "stocks"):
            block = snap.get(side)
            if not isinstance(block, dict):
                continue
            if "equity" in block and "equity_usd" not in block:
                block["equity_usd"] = block["equity"]
            if "peak_equity" in block and "peak_usd" not in block:
                block["peak_usd"] = block["peak_equity"]
            block.setdefault("sparkline_usd", [])
        # Top-level risk thresholds the operator UI shows next to the DD ribbon.
        # Sourced from the risk_gates config the producer already reads.
        try:
            from user_data.modules.unified_risk import _load_risk_gates  # type: ignore[attr-defined]
            gates = _load_risk_gates() or {}
            snap.setdefault("pause_threshold_pct", round(float(gates.get("daily_loss_halt_pct") or 0.03) * 100, 2))
            snap.setdefault("kill_threshold_pct", round(float(gates.get("combined_dd_threshold_pct") or 0.10) * 100, 2))
        except Exception:
            snap.setdefault("pause_threshold_pct", 3.0)
            snap.setdefault("kill_threshold_pct", 10.0)
        return snap
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
