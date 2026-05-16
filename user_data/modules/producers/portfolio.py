"""
producers/portfolio.py — capital + day P&L per side, with `_meta`.

Closes **B1** (stocksMove poisons day P&L): the legacy
`/api/ops/combined_portfolio` returned `stocks_equity − stocks_peak_equity`
as a stand-in for "today's stocks move", which is actually the all-time
peak-to-current drawdown and inflates the LIVE DAY P&L / Unrealized tile
with a phantom number on every flat / DD day.

Truth:
    stocks.day_pnl_usd = portfolio_value − last_equity

where:
    portfolio_value:  Alpaca `get_account().portfolio_value` (live equity)
    last_equity:      Alpaca `get_account().raw["last_equity"]`
                      (equity at yesterday's close — Alpaca's own day-start)

Alpaca's `last_equity` rolls over at the broker session boundary
(post-close → next pre-open), so this number is the operator's true
"today's stocks move" and matches the broker's UI exactly.

Output shape (`portfolio_snapshot()`):

    {
        "combined": {
            "equity":         float,  # crypto + stocks
            "day_pnl_usd":    float,  # crypto.day_pnl_usd + stocks.day_pnl_usd
            "day_pnl_pct":    float,  # day_pnl_usd / (equity − day_pnl_usd) × 100
            "peak_equity":    float,
            "drawdown_pct":   float,
        },
        "crypto": {
            "equity":         float,
            "day_pnl_usd":    float,  # realized today from trade_journal
            "day_pnl_pct":    float,
            "peak_equity":    float,
            "drawdown_pct":   float,
            "open_positions": int,
        },
        "stocks": {
            "equity":         float,  # Alpaca portfolio_value
            "last_equity":    float,  # Alpaca last_equity (day-start)
            "day_pnl_usd":    float,  # equity − last_equity   ← B1 fix
            "day_pnl_pct":    float,
            "peak_equity":    float,
            "drawdown_pct":   float,
            "open_positions": int,
            "cash":           float,
            "buying_power":   float,
        },
        "_meta": {
            "snapshot_ts":    ISO-8601,
            "age_s":          int | None,
            "stale":          bool,
            "market_open_now":bool,
            "source":         "alpaca+trade_journal+wheel_state",
            "stocks_snapshot_ts": ISO-8601 | None,
            "stocks_snapshot_age_s": int | None,
        },
    }

This producer never writes — pure read from the existing combined-risk
snapshot + Alpaca's `last_equity` field which `_stocks_state()` already
caches via the wheel snapshot file.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Wheel snapshot path — symlinked at ~/Documents/.dgx-train/shark/wheel-state/
# per the data-out-of-repo migration. READ-ONLY in this producer.
_WHEEL_SNAPSHOT_PATH = Path(os.environ.get(
    "WHEEL_SNAPSHOT_PATH",
    str(Path.home() / "Documents/.dgx-train/shark/wheel-state/account_snapshot.json"),
))

# Stocks snapshot stale threshold (seconds, during market hours).
# Defaults match the legacy `unified_risk.STOCKS_STALE_SECONDS` (10 min).
_STOCKS_STALE_S = int(os.environ.get("STOCKS_STALE_SECONDS", "600"))


def _read_wheel_snapshot() -> dict[str, Any]:
    """Read the wheel-state Alpaca snapshot. READ-ONLY.

    Returns {} on any failure — caller must treat missing fields as None.
    """
    try:
        return json.loads(_WHEEL_SNAPSHOT_PATH.read_text())
    except (FileNotFoundError, json.JSONDecodeError, OSError) as exc:
        logger.debug("wheel snapshot read failed: %s", exc)
        return {}


def _is_nyse_open_now() -> bool:
    """Mon-Fri 09:30-16:00 ET. Holiday-blind; matches `unified_risk._is_nyse_open_now`."""
    try:
        from zoneinfo import ZoneInfo
    except ImportError:  # pragma: no cover
        return False
    from datetime import time as _time
    et = datetime.now(ZoneInfo("America/New_York"))
    if et.weekday() >= 5:
        return False
    cur = et.time()
    return _time(9, 30) <= cur < _time(16, 0)


def _snap_age_seconds(ts: str | None) -> int | None:
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        return int((datetime.now(UTC) - dt).total_seconds())
    except (ValueError, TypeError):
        return None


def stocks_day_pnl(snapshot: dict[str, Any] | None = None) -> dict[str, Any]:
    """Per-side stocks day-P&L from the broker's own session boundary.

    B1 fix — `day_pnl_usd = portfolio_value − last_equity`. Returns 0.0
    when either field is missing (don't synthesize a phantom number).

    The wheel-state account_snapshot.json carries `portfolio_value`,
    `cash`, `buying_power`. `last_equity` is appended by the wheel
    snapshot CLI (`python -m wheel.cli snapshot`) — when absent, we
    return 0.0 day-PnL and `_meta.last_equity_present=false` so the UI
    can render "—" instead of a misleading zero. We never compute
    `portfolio_value − peak_equity` (that's drawdown, the B1 bug).
    """
    snap = snapshot if snapshot is not None else _read_wheel_snapshot()
    portfolio_value = snap.get("portfolio_value")
    last_equity = snap.get("last_equity")  # may not be present in older snapshots

    try:
        pv = float(portfolio_value) if portfolio_value is not None else None
    except (TypeError, ValueError):
        pv = None
    try:
        le = float(last_equity) if last_equity is not None else None
    except (TypeError, ValueError):
        le = None

    has_both = pv is not None and le is not None
    day_pnl = round(pv - le, 2) if has_both else 0.0
    day_pnl_pct = 0.0
    if has_both and le > 0:
        day_pnl_pct = round((pv - le) / le * 100, 4)

    return {
        "equity": round(pv, 2) if pv is not None else 0.0,
        "last_equity": round(le, 2) if le is not None else 0.0,
        "day_pnl_usd": day_pnl,
        "day_pnl_pct": day_pnl_pct,
        "cash": round(float(snap.get("cash") or 0.0), 2),
        "buying_power": round(float(snap.get("buying_power") or 0.0), 2),
        "open_positions": int(snap.get("wheel_open_positions") or 0),
        "_last_equity_present": has_both,
        "_snapshot_ts": snap.get("ts"),
    }


def portfolio_snapshot() -> dict[str, Any]:
    """Combined + per-side portfolio with `_meta`.

    Composes:
      - stocks: from Alpaca wheel snapshot (B1 — day_pnl_usd = pv − last_equity)
      - crypto: from existing `unified_risk.get_combined_risk_status()`
                (its day_pnl is already correct — closed-trade SUM from
                trade_journal — and was never poisoned by stocksMove).
      - combined: derived from the two sides.

    Producer is READ-ONLY. Never writes. Wheel snapshot path is the
    bind-mounted READ-ONLY mount per spec §5.4.
    """
    # Stocks (B1 truth)
    snap = _read_wheel_snapshot()
    stocks = stocks_day_pnl(snap)
    stocks_snap_ts = stocks.pop("_snapshot_ts", None)
    stocks_le_present = stocks.pop("_last_equity_present")
    stocks_age_s = _snap_age_seconds(stocks_snap_ts)
    market_open = _is_nyse_open_now()
    stocks_stale = (stocks_age_s is not None and stocks_age_s > _STOCKS_STALE_S) and market_open

    # Crypto + combined drawdown — reuse existing risk module (single
    # truth for peak tracking; we add the per-side day_pnl shape on top).
    crypto_equity = 0.0
    crypto_peak = 0.0
    crypto_dd_pct = 0.0
    crypto_open = 0
    crypto_day_pnl = 0.0
    combined_peak = 0.0
    combined_dd_pct = 0.0
    risk_status: dict[str, Any] = {}
    try:
        from user_data.modules.unified_risk import get_combined_risk_status
        risk_status = get_combined_risk_status()
        crypto_equity = float(risk_status.get("crypto_equity") or 0.0)
        crypto_peak = float(risk_status.get("crypto_peak_equity") or 0.0)
        crypto_dd_pct = float(risk_status.get("crypto_drawdown_pct") or 0.0)
        crypto_open = int(risk_status.get("crypto_open_positions") or 0)
        combined_peak = float(risk_status.get("combined_peak_equity") or 0.0)
        combined_dd_pct = float(risk_status.get("combined_drawdown_pct") or 0.0)
    except Exception as exc:  # noqa: BLE001
        logger.warning("portfolio_snapshot: unified_risk unavailable: %s", exc)

    # Crypto day P&L from trade_journal — closed-trade SUM today (UTC).
    # Reuse ops_db.trades_risk_summary if available; on failure we
    # surface 0.0 + _meta.crypto_day_pnl_source="unavailable".
    crypto_day_source = "trade_journal"
    try:
        from user_data.dashboard import ops_db
        rsum = ops_db.trades_risk_summary()
        crypto_day_pnl = float(rsum.get("daily_pnl_usd") or 0.0)
    except Exception as exc:  # noqa: BLE001
        logger.debug("portfolio_snapshot: trades_risk_summary failed: %s", exc)
        crypto_day_source = "unavailable"

    crypto_day_pnl_pct = 0.0
    if crypto_equity > 0:
        crypto_start = crypto_equity - crypto_day_pnl
        if crypto_start > 0:
            crypto_day_pnl_pct = round((crypto_day_pnl / crypto_start) * 100, 4)

    # Per-side peak for stocks — reuse from risk module if present.
    stocks_peak = float(risk_status.get("stocks_peak_equity") or stocks["equity"])
    stocks_dd_pct = float(risk_status.get("stocks_drawdown_pct") or 0.0)

    # Combined
    combined_equity = round(crypto_equity + stocks["equity"], 2)
    combined_day_pnl = round(crypto_day_pnl + stocks["day_pnl_usd"], 2)
    combined_day_start = combined_equity - combined_day_pnl
    combined_day_pct = 0.0
    if combined_day_start > 0:
        combined_day_pct = round(combined_day_pnl / combined_day_start * 100, 4)

    now_iso = datetime.now(UTC).isoformat()
    return {
        "combined": {
            "equity": combined_equity,
            "day_pnl_usd": combined_day_pnl,
            "day_pnl_pct": combined_day_pct,
            "peak_equity": round(combined_peak, 2),
            "drawdown_pct": round(combined_dd_pct, 3),
        },
        "crypto": {
            "equity": round(crypto_equity, 2),
            "day_pnl_usd": round(crypto_day_pnl, 2),
            "day_pnl_pct": crypto_day_pnl_pct,
            "peak_equity": round(crypto_peak, 2),
            "drawdown_pct": round(crypto_dd_pct, 3),
            "open_positions": crypto_open,
        },
        "stocks": {
            **stocks,
            "peak_equity": round(stocks_peak, 2),
            "drawdown_pct": round(stocks_dd_pct, 3),
        },
        "_meta": {
            "snapshot_ts": now_iso,
            "age_s": 0,
            "stale": bool(stocks_stale),
            "market_open_now": market_open,
            "source": "alpaca+trade_journal+wheel_state",
            "stocks_snapshot_ts": stocks_snap_ts,
            "stocks_snapshot_age_s": stocks_age_s,
            "last_equity_present": stocks_le_present,
            "crypto_day_pnl_source": crypto_day_source,
        },
    }
