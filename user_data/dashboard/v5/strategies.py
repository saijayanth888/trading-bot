"""
v5 router: ``GET /api/v5/strategies/{kind}``.

Per-strategy strip for crypto-v4 / stocks-wheel / shark. Includes a
per-side `regime` field (B12 closer — distinct producer per kind).

Raw producer output, no envelope.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, HTTPException, Path as FastPath

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v5", tags=["v5"])

_VALID_KINDS = {"crypto-v4", "stocks-wheel", "shark"}


def _crypto_v4_strategy() -> dict[str, Any]:
    """Crypto V4 paper engine — uses producers.portfolio.crypto + regime.

    Regime source: `regime_log` (DB) — the V4 engine writes a row per
    cycle. We fall back to "unknown" on any DB failure.
    """
    from datetime import datetime, UTC
    from user_data.modules.producers.portfolio import portfolio_snapshot
    snap = portfolio_snapshot()
    crypto = snap.get("crypto", {})

    regime = {"current": "unknown", "probability": None, "ts": None}
    try:
        from user_data.dashboard.data_sources import DATABASE_URL
        import psycopg
        from psycopg.rows import dict_row
        with psycopg.connect(DATABASE_URL, row_factory=dict_row) as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT ts, regime, probability "
                "FROM regime_log ORDER BY ts DESC LIMIT 1"
            )
            row = cur.fetchone()
            if row:
                regime = {
                    "current": row.get("regime") or "unknown",
                    "probability": float(row["probability"])
                        if row.get("probability") is not None else None,
                    "ts": row["ts"].isoformat() if row.get("ts") else None,
                }
    except Exception as exc:  # noqa: BLE001
        logger.debug("crypto-v4 regime read failed: %s", exc)

    return {
        "kind": "crypto-v4",
        "equity": crypto.get("equity"),
        "day_pnl_usd": crypto.get("day_pnl_usd"),
        "day_pnl_pct": crypto.get("day_pnl_pct"),
        "drawdown_pct": crypto.get("drawdown_pct"),
        "open_positions": crypto.get("open_positions"),
        "regime": regime,
        "_meta": {
            "snapshot_ts": datetime.now(UTC).isoformat(),
            "age_s": 0,
            "stale": False,
            "market_open_now": snap.get("_meta", {}).get("market_open_now", False),
            "source": "producers.portfolio + regime_log",
        },
    }


def _stocks_wheel_strategy() -> dict[str, Any]:
    """Stocks wheel — uses producers.portfolio.stocks + stock_regime.

    Per B12: the regime here MUST be the stocks-side regime (SPY proxy),
    NOT the crypto regime. We read `stock_regime` (DB) when present.
    """
    from datetime import datetime, UTC
    from user_data.modules.producers.portfolio import portfolio_snapshot
    snap = portfolio_snapshot()
    stocks = snap.get("stocks", {})

    regime = {"current": "unknown", "probability": None, "ts": None}
    try:
        from user_data.dashboard.data_sources import DATABASE_URL
        import psycopg
        from psycopg.rows import dict_row
        with psycopg.connect(DATABASE_URL, row_factory=dict_row) as conn, conn.cursor() as cur:
            # stock_regime table (per legacy `/api/ops/stock_regime`)
            try:
                cur.execute(
                    "SELECT ts, regime, probability "
                    "FROM stock_regime ORDER BY ts DESC LIMIT 1"
                )
                row = cur.fetchone()
            except Exception:
                row = None
            if row:
                regime = {
                    "current": row.get("regime") or "unknown",
                    "probability": float(row["probability"])
                        if row.get("probability") is not None else None,
                    "ts": row["ts"].isoformat() if row.get("ts") else None,
                }
    except Exception as exc:  # noqa: BLE001
        logger.debug("stocks-wheel regime read failed: %s", exc)

    return {
        "kind": "stocks-wheel",
        "equity": stocks.get("equity"),
        "last_equity": stocks.get("last_equity"),
        "day_pnl_usd": stocks.get("day_pnl_usd"),
        "day_pnl_pct": stocks.get("day_pnl_pct"),
        "drawdown_pct": stocks.get("drawdown_pct"),
        "open_positions": stocks.get("open_positions"),
        "cash": stocks.get("cash"),
        "buying_power": stocks.get("buying_power"),
        "regime": regime,
        "_meta": {
            **(snap.get("_meta") or {}),
            "source": "producers.portfolio + stock_regime",
        },
    }


def _shark_strategy() -> dict[str, Any]:
    """Shark momentum-bot stats — uses producers.shark_stats.

    Surfaces the B2 fix: when the rebuilt-stats file exists, we serve
    those numbers; otherwise we recompute in-memory and tag
    `schema_health` so the UI can mark it.
    """
    from datetime import datetime, UTC
    from user_data.modules.producers.shark_stats import shark_stats_snapshot
    stats_payload = shark_stats_snapshot()

    return {
        "kind": "shark",
        **stats_payload,
        # Surface regime parity with the other two strips. Shark's regime
        # decision lives in stocks/docs/dashboard/data.json.state — we
        # leave it None for now; backend builder B owns the shark regime
        # producer surface.
        "regime": {"current": "unknown", "probability": None, "ts": None},
    }


_DISPATCH = {
    "crypto-v4": _crypto_v4_strategy,
    "stocks-wheel": _stocks_wheel_strategy,
    "shark": _shark_strategy,
}


@router.get("/strategies/{kind}")
async def get_strategy(kind: str = FastPath(..., description="crypto-v4 | stocks-wheel | shark")) -> dict:
    if kind not in _VALID_KINDS:
        raise HTTPException(
            status_code=404,
            detail={
                "type": "about:blank",
                "title": "Unknown strategy kind",
                "status": 404,
                "detail": f"valid: {sorted(_VALID_KINDS)}",
            },
        )
    try:
        out = _DISPATCH[kind]()
        # Frontend-v5 types (StrategyPayload) expect `equity_usd`; producer
        # emits `equity`. Alias additively so both names work.
        if isinstance(out, dict) and "equity" in out and "equity_usd" not in out:
            out["equity_usd"] = out["equity"]
        # `enabled` defaults to True unless the producer set otherwise — the
        # UI uses this to decide "running" vs "paused".
        out.setdefault("enabled", True)
        return out
    except Exception as exc:
        logger.exception("v5/strategies/%s: %s", kind, exc)
        raise HTTPException(
            status_code=503,
            detail={
                "type": "about:blank",
                "title": f"{kind} producer unavailable",
                "status": 503,
                "detail": str(exc),
            },
        ) from exc
