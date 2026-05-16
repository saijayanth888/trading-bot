"""
v5 router: ``GET /api/v5/metrics``.

Single-truth Sharpe + max-DD + win rate. Closes B3.

Loads daily-return series from `quanta_schema.equity_snapshots` (crypto)
and the shark equity history (stocks), hands to `producers.metrics` for
pure computation. Returns raw producer output.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v5", tags=["v5"])

_REPO_ROOT = Path(__file__).resolve().parents[3]
_SHARK_DATA_PATH = _REPO_ROOT / "stocks" / "docs" / "dashboard" / "data.json"


def _crypto_daily_returns(lookback_days: int) -> list[float]:
    """Compute daily returns from quanta_schema.equity_snapshots.

    Returns empty list on any failure — the producer treats that as
    `degenerate=true` (zero rows) so the operator UI renders "—".
    """
    try:
        from user_data.dashboard.data_sources import DATABASE_URL
        import psycopg
        from psycopg.rows import dict_row
    except Exception as exc:  # noqa: BLE001
        logger.debug("crypto returns: psycopg/datasrc unavailable: %s", exc)
        return []

    try:
        with psycopg.connect(DATABASE_URL, row_factory=dict_row) as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT ts, equity
                FROM quanta_schema.equity_snapshots
                WHERE ts >= NOW() - (%s || ' days')::interval
                ORDER BY ts ASC
                """,
                (str(lookback_days),),
            )
            rows = cur.fetchall()
    except Exception as exc:  # noqa: BLE001
        logger.debug("crypto returns: query failed: %s", exc)
        return []

    eqs: list[float] = []
    for r in rows:
        v = r.get("equity")
        if v is None:
            continue
        try:
            eqs.append(float(v))
        except (TypeError, ValueError):
            continue
    if len(eqs) < 2:
        return []
    rets: list[float] = []
    prev = eqs[0]
    for cur_eq in eqs[1:]:
        if prev > 0:
            rets.append((cur_eq - prev) / prev)
        prev = cur_eq
    return rets


def _stocks_daily_returns() -> list[float]:
    """Daily returns from the shark `equity_history` array.

    READ-ONLY from stocks/docs/dashboard/data.json. We don't need DB for
    the stocks side — the shark cron writes a daily equity snapshot.
    """
    try:
        data = json.loads(_SHARK_DATA_PATH.read_text())
    except (FileNotFoundError, json.JSONDecodeError, OSError) as exc:
        logger.debug("stocks returns: read failed: %s", exc)
        return []
    hist = data.get("equity_history") or []
    eqs: list[float] = []
    for row in hist:
        v = (row or {}).get("equity")
        if v is None:
            continue
        try:
            eqs.append(float(v))
        except (TypeError, ValueError):
            continue
    if len(eqs) < 2:
        return []
    rets: list[float] = []
    prev = eqs[0]
    for cur_eq in eqs[1:]:
        if prev > 0:
            rets.append((cur_eq - prev) / prev)
        prev = cur_eq
    return rets


@router.get("/metrics")
async def get_metrics(lookback_days: int = Query(30, ge=1, le=365)) -> dict:
    """Sharpe + max-DD + win rate per side, single-truth.

    `lookback_days` controls the crypto-side window; stocks uses the full
    `equity_history` from the shark daily cron (typically 90 days max).
    """
    try:
        from user_data.modules.producers.metrics import metrics_snapshot
        crypto_rets = _crypto_daily_returns(lookback_days)
        stocks_rets = _stocks_daily_returns()
        # Trade lists — keep None; win rate is not yet wired to a single
        # trade source on the v5 path. The shark side has its own
        # win_rate via `producers.shark_stats`; the crypto side is
        # covered separately by the existing trade_journal aggregation.
        snap = metrics_snapshot(
            crypto_returns=crypto_rets,
            stocks_returns=stocks_rets,
        )
        snap["_meta"]["lookback_days"] = lookback_days
        # Top-level aggregates for the v1 hero strips — the frontend reads
        # `metrics.sharpe` and `metrics.win_rate_pct` directly. Prefer the
        # combined/stocks side when both exist; fall back to crypto.
        for side_name in ("stocks", "crypto"):
            side = snap.get(side_name) or {}
            if side.get("sharpe") is not None:
                snap.setdefault("sharpe", side.get("sharpe"))
                snap.setdefault("max_dd_pct", side.get("max_dd_pct"))
                snap.setdefault("win_rate_pct", side.get("win_rate_pct"))
                break
        return snap
    except Exception as exc:
        logger.exception("v5/metrics: %s", exc)
        raise HTTPException(
            status_code=503,
            detail={
                "type": "about:blank",
                "title": "Metrics producer unavailable",
                "status": 503,
                "detail": str(exc),
            },
        ) from exc
