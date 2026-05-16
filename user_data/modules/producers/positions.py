"""
producers/positions.py — UNION crypto fills + wheel state + shark holdings.

Closes **B6** (/api/v4/positions returns empty even though there's an open
wheel position) and **B9** (positions stored in two different truth
systems — postgres for crypto, JSON files for stocks/options — never
joined into one operator-visible list).

Sources (READ-ONLY):
  1. ``quanta_schema.fills`` ⨝ ``quanta_schema.proposals`` ⨝ ``trade_journal``
     — current open crypto paper positions with mark price.
     Same query as `user_data.dashboard.ops_db.open_positions`.
  2. ``~/Documents/.dgx-train/shark/wheel-state/account_snapshot.json``
     — wheel pilot's NVDA-class short-put positions (option contracts).
     The `account_snapshot.json` only carries counts; the actual position
     list lives in `positions.json` (sibling file). We read BOTH and
     prefer `positions.json` for the per-position list, falling back to
     `wheel_open_positions: int` for a count-only badge.
  3. ``stocks/docs/dashboard/data.json`` → ``open_trades`` — shark
     momentum holdings (the long-stock side). Per the legacy `ops_routes`
     this is the canonical surface; we reuse the same source.

Output shape (`positions_snapshot()`):

    {
        "positions": [
            {
                "source":      "crypto" | "wheel" | "shark",
                "symbol":      str,         # "BTC/USD", "NVDA260522P00220000", "NVDA"
                "side":        "long" | "short" | "short_put" | "short_call",
                "qty":         float,
                "entry_price": float | None,
                "mark_price":  float | None,
                "stake_usd":   float | None,
                "pnl_usd":     float | None,  # absolute, if computable
                "pnl_pct":     float | None,  # fractional, 0.01 = 1%
                "open_date":   ISO | None,
                "external_id": str | None,
                "extra":       dict,        # source-specific fields
            },
            ...
        ],
        "_meta": {
            "snapshot_ts":     ISO-8601,
            "age_s":           0,
            "stale":           bool,
            "market_open_now": bool,
            "source":          "union(quanta_schema.fills, wheel-state, shark)",
            "counts":          {"crypto": int, "wheel": int, "shark": int},
            "errors":          list[str],   # source-level failures (degrade, not fail)
        },
    }
"""

from __future__ import annotations

import json
import logging
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# --- READ-ONLY file paths --------------------------------------------------
_WHEEL_STATE_DIR = Path(os.environ.get(
    "WHEEL_STATE_DIR",
    str(Path.home() / "Documents/.dgx-train/shark/wheel-state"),
))
_WHEEL_SNAPSHOT_PATH = _WHEEL_STATE_DIR / "account_snapshot.json"
_WHEEL_POSITIONS_PATH = _WHEEL_STATE_DIR / "positions.json"

# Shark dashboard data lives in-repo (the `_compute_stats` author at
# stocks/shark/dashboard/generate.py writes this file).
_REPO_ROOT = Path(__file__).resolve().parents[3]
_SHARK_DATA_PATH = _REPO_ROOT / "stocks" / "docs" / "dashboard" / "data.json"


def _safe_read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text())
    except (FileNotFoundError, json.JSONDecodeError, OSError) as exc:
        logger.debug("read %s failed: %s", path, exc)
        return None


def _is_nyse_open_now() -> bool:
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


# ---------------------------------------------------------------------------
# Source 1: crypto fills + trade_journal
# ---------------------------------------------------------------------------

def _crypto_positions() -> tuple[list[dict[str, Any]], str | None]:
    """Return open crypto positions from `quanta_schema.fills ⨝ trade_journal`.

    Reuses `user_data.dashboard.ops_db.open_positions` — same query shape
    as the legacy `/api/v4/positions` so we don't duplicate the SQL.
    """
    try:
        # Use importlib so monkeypatched `sys.modules["user_data.dashboard.ops_db"]`
        # is respected at call time (tests stub the module; `from ... import`
        # at function scope would re-import the real one).
        import importlib
        ops_db = importlib.import_module("user_data.dashboard.ops_db")
        rows = ops_db.open_positions(limit=200)
    except Exception as exc:  # noqa: BLE001
        logger.warning("crypto positions read failed: %s", exc)
        return [], f"crypto: {exc}"

    out: list[dict[str, Any]] = []
    for r in rows:
        entry = r.get("open_rate")
        mark = r.get("mark_price")
        stake = r.get("stake_amount")
        cp = r.get("current_profit")  # fractional return on entry, sign-aware
        pnl_usd = None
        if cp is not None and stake is not None:
            try:
                pnl_usd = round(float(cp) * float(stake), 4)
            except (TypeError, ValueError):
                pnl_usd = None
        out.append({
            "source": "crypto",
            "symbol": r.get("pair"),
            "side": (r.get("direction") or "long").lower(),
            "qty": None,  # quanta paper engine sizes by stake_amount, not qty
            "entry_price": entry,
            "mark_price": mark,
            "stake_usd": stake,
            "pnl_usd": pnl_usd,
            "pnl_pct": cp,
            "open_date": r.get("open_date"),
            "external_id": r.get("external_id"),
            "extra": {
                "trade_id": r.get("trade_id"),
                "mark_ts": r.get("mark_ts"),
                "regime_at_entry": r.get("regime_at_entry"),
            },
        })
    return out, None


# ---------------------------------------------------------------------------
# Source 2: wheel-state JSON (READ-ONLY)
# ---------------------------------------------------------------------------

def _wheel_positions() -> tuple[list[dict[str, Any]], str | None]:
    """Wheel positions from the bind-mounted READ-ONLY JSON state.

    `account_snapshot.json` carries counts only (`wheel_open_positions`).
    `positions.json` is the per-position list with strike / qty / entry
    credit. We surface positions.json when present, fall back to a
    single placeholder row carrying just the count when only the snapshot
    is available.
    """
    positions_raw = _safe_read_json(_WHEEL_POSITIONS_PATH)
    snap = _safe_read_json(_WHEEL_SNAPSHOT_PATH) or {}

    if isinstance(positions_raw, list) and positions_raw:
        out: list[dict[str, Any]] = []
        for p in positions_raw:
            kind = (p.get("kind") or "short_put").lower()
            strike = p.get("strike")
            qty = p.get("qty")
            entry_credit = p.get("entry_credit")
            mark = p.get("mark") or p.get("mark_price")
            # Options stake = strike × 100 × abs(qty) for short puts (collateral)
            collateral = None
            try:
                if strike is not None and qty is not None and kind == "short_put":
                    collateral = round(float(strike) * 100.0 * abs(int(qty)), 2)
            except (TypeError, ValueError):
                collateral = None
            pnl_usd = None
            if entry_credit is not None and mark is not None:
                try:
                    # Short option PnL: credit collected − current mark (per contract × 100 × qty)
                    pnl_usd = round(
                        (float(entry_credit) - float(mark)) * 100.0 * abs(int(qty or 0)),
                        2,
                    )
                except (TypeError, ValueError):
                    pnl_usd = None
            out.append({
                "source": "wheel",
                "symbol": p.get("symbol") or p.get("occ_symbol") or p.get("underlying"),
                "side": kind,
                "qty": qty,
                "entry_price": entry_credit,  # credit per contract
                "mark_price": mark,
                "stake_usd": collateral,
                "pnl_usd": pnl_usd,
                "pnl_pct": None,  # options pnl_pct from credit basis not always meaningful
                "open_date": p.get("opened_at") or p.get("entry_date"),
                "external_id": p.get("order_id") or p.get("external_id"),
                "extra": {
                    "kind": kind,
                    "strike": strike,
                    "expiry": p.get("expiry"),
                    "underlying": p.get("underlying"),
                    "entry_credit": entry_credit,
                },
            })
        return out, None

    # Fallback: snapshot says N positions but positions.json is missing.
    n = int(snap.get("wheel_open_positions") or 0)
    if n > 0:
        return [{
            "source": "wheel",
            "symbol": None,
            "side": "short_put",
            "qty": None,
            "entry_price": None,
            "mark_price": None,
            "stake_usd": None,
            "pnl_usd": None,
            "pnl_pct": None,
            "open_date": None,
            "external_id": None,
            "extra": {
                "summary_only": True,
                "snapshot_count": n,
                "snapshot_ts": snap.get("ts"),
                "wheel_cumulative_pnl": snap.get("wheel_cumulative_pnl"),
            },
        }], "wheel: positions.json missing — count-only row from account_snapshot.json"
    return [], None


# ---------------------------------------------------------------------------
# Source 3: shark momentum-bot holdings
# ---------------------------------------------------------------------------

def _shark_positions() -> tuple[list[dict[str, Any]], str | None]:
    """Shark long-stock holdings from the bot's generated dashboard JSON.

    READ-ONLY. The actual open-position list is `open_trades` in
    `stocks/docs/dashboard/data.json`.
    """
    data = _safe_read_json(_SHARK_DATA_PATH)
    if not isinstance(data, dict):
        return [], None
    open_trades = data.get("open_trades")
    if isinstance(open_trades, dict):
        rows = list(open_trades.values())
    elif isinstance(open_trades, list):
        rows = open_trades
    else:
        return [], None

    out: list[dict[str, Any]] = []
    for t in rows:
        if not isinstance(t, dict):
            continue
        entry = t.get("entry_price") or t.get("entry") or t.get("avg_price")
        mark = t.get("mark") or t.get("current_price") or t.get("last_price")
        qty = t.get("qty") or t.get("quantity")
        stake = None
        if entry is not None and qty is not None:
            try:
                stake = round(float(entry) * float(qty), 2)
            except (TypeError, ValueError):
                stake = None
        pnl_usd = None
        pnl_pct = None
        if entry is not None and mark is not None and qty is not None:
            try:
                pnl_usd = round((float(mark) - float(entry)) * float(qty), 2)
                if float(entry) > 0:
                    pnl_pct = round((float(mark) - float(entry)) / float(entry), 6)
            except (TypeError, ValueError):
                pass
        out.append({
            "source": "shark",
            "symbol": t.get("symbol") or t.get("ticker"),
            "side": "long",
            "qty": qty,
            "entry_price": entry,
            "mark_price": mark,
            "stake_usd": stake,
            "pnl_usd": pnl_usd,
            "pnl_pct": pnl_pct,
            "open_date": t.get("entry_date") or t.get("opened_at"),
            "external_id": t.get("order_id") or t.get("external_id"),
            "extra": {
                "regime_at_entry": t.get("regime"),
                "stop": t.get("stop") or t.get("stop_loss"),
                "target": t.get("target"),
                "setup": t.get("setup_tag"),
            },
        })
    return out, None


# ---------------------------------------------------------------------------
# Union producer
# ---------------------------------------------------------------------------

def positions_snapshot() -> dict[str, Any]:
    """UNION crypto + wheel + shark. Surface degraded sources in `_meta.errors`."""
    errors: list[str] = []
    crypto, err = _crypto_positions()
    if err:
        errors.append(err)
    wheel, err = _wheel_positions()
    if err:
        errors.append(err)
    shark, err = _shark_positions()
    if err:
        errors.append(err)

    positions = crypto + wheel + shark
    now = datetime.now(UTC).isoformat()
    market_open = _is_nyse_open_now()
    return {
        "positions": positions,
        "_meta": {
            "snapshot_ts": now,
            "age_s": 0,
            "stale": False,
            "market_open_now": market_open,
            "source": "union(quanta_schema.fills, wheel-state, shark)",
            "counts": {
                "crypto": len(crypto),
                "wheel": len(wheel),
                "shark": len(shark),
                "total": len(positions),
            },
            "errors": errors,
        },
    }
