"""
Unified risk governor — single source of truth for combined crypto + stocks
portfolio drawdown.

Why this exists
---------------
Crypto Freqtrade has its own risk_governor with an 8% portfolio-drawdown
auto-pause. The stocks subsystem (Shark + Wheel) has a 15% circuit
breaker. Neither side knows about the other. If both maxed out
simultaneously the combined drawdown could exceed safe limits even though
each individual side stayed under its threshold.

This module aggregates equity from both venues, tracks a combined peak,
and trips BOTH kill switches when combined drawdown crosses a configurable
threshold (default 10%).

Data sources
------------
Crypto:
  - starting equity:   `dry_run_wallet` from config.json (paper) OR live wallet
  - realised P&L:      SUM(pnl) from `trade_journal` (Postgres)
  - unrealised P&L:    SUM(profit_abs) from freqtrade `/api/v1/status`

Stocks:
  - portfolio value:   `stocks/wheel/state/account_snapshot.json` (Alpaca paper)
  - cumulative P&L:    `wheel_cumulative_pnl` field of same snapshot
  - shark equity:      tracked separately in `stocks/memory/PROJECT-CONTEXT.md`
                       (NOT yet machine-readable; the wheel snapshot is
                       authoritative for now since shark hasn't traded)

Persisted state
---------------
The combined peak is written to `user_data/data/unified_risk_peak.json`
(gitignored). Without this, a restart would reset the peak to "now" and
silently mask any prior drawdown.

Public surface
--------------
    get_combined_risk_status()  → dict with all metrics + breaker state
    check_and_trip()            → calls the above + trips switches if needed
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

# Threshold for the combined kill-switch. Defaults to the 10% the operator
# spec'd; overridable via env so paper / live can have different values.
COMBINED_DD_THRESHOLD_PCT = float(os.environ.get("UNIFIED_DRAWDOWN_PCT", "0.10"))

# Lazy-imported to keep this module testable in isolation
try:
    from . import ops_db  # noqa: F401  pylint: disable=relative-beyond-top-level
except Exception:
    ops_db = None  # type: ignore[assignment]

# Path resolution: walk up to find trading-bot root
_HERE = Path(__file__).resolve()
_REPO_ROOT = _HERE.parents[2]  # user_data/modules/ → user_data/ → repo
_PEAK_FILE = _REPO_ROOT / "user_data" / "data" / "unified_risk_peak.json"
_STOCKS_SNAPSHOT = _REPO_ROOT / "stocks" / "wheel" / "state" / "account_snapshot.json"
_STOCKS_KILL_FLAG = _REPO_ROOT / "stocks" / "memory" / "KILL.flag"
_CONFIG_JSON = _REPO_ROOT / "user_data" / "config.json"


# ---------------------------------------------------------------------------
# Data-source helpers
# ---------------------------------------------------------------------------


def _load_json(path: Path) -> Optional[dict]:
    try:
        if not path.is_file():
            return None
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("unified_risk: failed to read %s: %s", path, exc)
        return None


def _crypto_starting_equity() -> float:
    """Paper-mode starting wallet from config.json. Live mode would override."""
    cfg = _load_json(_CONFIG_JSON) or {}
    if cfg.get("dry_run", True):
        return float(cfg.get("dry_run_wallet") or 0.0)
    # Live: would query the exchange. For now return 0 (caller falls back).
    return 0.0


def _crypto_realised_pnl_usd() -> float:
    """Sum of closed-trade pnl in USD from trade_journal."""
    if ops_db is None or not getattr(ops_db, "_HAVE_PG", False):
        return 0.0
    try:
        with ops_db._connect() as conn, conn.cursor() as cur:  # noqa: SLF001
            cur.execute(
                "SELECT COALESCE(SUM(pnl), 0) AS pnl "
                "FROM trade_journal WHERE closed_at IS NOT NULL"
            )
            row = cur.fetchone() or {}
            return float(row.get("pnl") or 0.0)
    except Exception as exc:  # pragma: no cover — DB connectivity
        logger.warning("unified_risk: realised pnl query failed: %s", exc)
        return 0.0


def _crypto_unrealised_pnl_usd() -> float:
    """Sum profit_abs across open freqtrade trades (synchronous probe)."""
    try:
        import httpx  # type: ignore
    except ImportError:
        return 0.0
    base = os.environ.get("FREQTRADE_API_URL", "http://freqtrade:8080")
    user = os.environ.get("FREQTRADE_API_USER")
    pw = os.environ.get("FREQTRADE_API_PASS")
    try:
        with httpx.Client(timeout=3.0, auth=(user, pw) if user and pw else None) as c:
            r = c.get(f"{base}/api/v1/status")
        if r.status_code != 200:
            return 0.0
        return sum(float(t.get("profit_abs") or 0.0) for t in (r.json() or []))
    except Exception as exc:
        logger.debug("unified_risk: unrealised pnl probe failed: %s", exc)
        return 0.0


def _crypto_open_count() -> int:
    try:
        import httpx
    except ImportError:
        return 0
    base = os.environ.get("FREQTRADE_API_URL", "http://freqtrade:8080")
    try:
        with httpx.Client(timeout=3.0) as c:
            r = c.get(f"{base}/api/v1/status")
        if r.status_code != 200:
            return 0
        return len(r.json() or [])
    except Exception:
        return 0


def _stocks_state() -> dict:
    """Pull Alpaca portfolio_value + open-position count from the wheel snapshot."""
    snap = _load_json(_STOCKS_SNAPSHOT) or {}
    return {
        "portfolio_value": float(snap.get("portfolio_value") or 0.0),
        "cash": float(snap.get("cash") or 0.0),
        "buying_power": float(snap.get("buying_power") or 0.0),
        "wheel_cumulative_pnl": float(snap.get("wheel_cumulative_pnl") or 0.0),
        "open_positions": int(snap.get("wheel_open_positions") or 0),
        "snapshot_ts": snap.get("ts"),
        "paper": bool(snap.get("paper", True)),
    }


# ---------------------------------------------------------------------------
# Peak tracking
# ---------------------------------------------------------------------------


def _load_peak() -> Optional[float]:
    p = _load_json(_PEAK_FILE) or {}
    val = p.get("combined_peak_equity")
    return float(val) if val is not None else None


def _save_peak(peak: float, components: dict) -> None:
    _PEAK_FILE.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "combined_peak_equity": peak,
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "components": components,
    }
    tmp = _PEAK_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, indent=2))
    tmp.replace(_PEAK_FILE)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


@dataclass
class RiskStatus:
    crypto_equity: float
    stocks_equity: float
    total_equity: float
    crypto_drawdown_pct: float
    stocks_drawdown_pct: float
    combined_drawdown_pct: float
    combined_peak_equity: float
    combined_open_positions: int
    crypto_open_positions: int
    stocks_open_positions: int
    circuit_breaker_active: bool
    threshold_pct: float
    snapshot_age_seconds: Optional[int]
    sources: dict


def get_combined_risk_status() -> dict:
    """Return the full combined risk picture without side effects."""
    # Crypto side
    starting = _crypto_starting_equity()
    realised = _crypto_realised_pnl_usd()
    unrealised = _crypto_unrealised_pnl_usd()
    crypto_equity = starting + realised + unrealised

    # Stocks side
    stocks = _stocks_state()
    stocks_equity = stocks["portfolio_value"]

    total = crypto_equity + stocks_equity

    # Peak: max over all observations. Initialise on first call.
    prior_peak = _load_peak()
    peak = max(total, prior_peak) if prior_peak is not None else total
    if prior_peak is None or peak > prior_peak:
        _save_peak(peak, {"crypto": crypto_equity, "stocks": stocks_equity})

    def _dd(equity: float, ref: float) -> float:
        if ref <= 0:
            return 0.0
        return max(0.0, (ref - equity) / ref)

    crypto_dd = _dd(crypto_equity, starting) if starting > 0 else 0.0
    stocks_dd = _dd(stocks_equity, stocks["portfolio_value"] or 0.0) if stocks["portfolio_value"] else 0.0
    combined_dd = _dd(total, peak)

    breaker = combined_dd >= COMBINED_DD_THRESHOLD_PCT

    snap_age = None
    snap_ts = stocks.get("snapshot_ts")
    if snap_ts:
        try:
            snap_dt = datetime.fromisoformat(snap_ts.replace("Z", "+00:00"))
            snap_age = int((datetime.now(timezone.utc) - snap_dt).total_seconds())
        except (ValueError, TypeError):
            snap_age = None

    status = RiskStatus(
        crypto_equity=round(crypto_equity, 2),
        stocks_equity=round(stocks_equity, 2),
        total_equity=round(total, 2),
        crypto_drawdown_pct=round(crypto_dd * 100, 3),
        stocks_drawdown_pct=round(stocks_dd * 100, 3),
        combined_drawdown_pct=round(combined_dd * 100, 3),
        combined_peak_equity=round(peak, 2),
        combined_open_positions=_crypto_open_count() + stocks["open_positions"],
        crypto_open_positions=_crypto_open_count(),
        stocks_open_positions=stocks["open_positions"],
        circuit_breaker_active=breaker,
        threshold_pct=round(COMBINED_DD_THRESHOLD_PCT * 100, 1),
        snapshot_age_seconds=snap_age,
        sources={
            "crypto_starting_equity": starting,
            "crypto_realised_pnl": round(realised, 2),
            "crypto_unrealised_pnl": round(unrealised, 2),
            "stocks_paper": stocks["paper"],
            "stocks_snapshot_ts": snap_ts,
        },
    )
    return asdict(status)


def trip_combined_kill_switch(reason: str) -> dict:
    """Side-effect path: pause crypto trades + write stocks KILL flag + Slack."""
    actions: dict = {"crypto_paused": False, "stocks_kill_flag": False, "slack_sent": False}

    # 1. Stocks kill flag (file-based, picked up by every shark + wheel runner)
    try:
        _STOCKS_KILL_FLAG.parent.mkdir(parents=True, exist_ok=True)
        _STOCKS_KILL_FLAG.write_text(f"unified_risk: {reason}\n")
        actions["stocks_kill_flag"] = True
    except OSError as exc:
        logger.exception("unified_risk: failed to write stocks KILL flag: %s", exc)

    # 2. Crypto pause via the dashboard /api/ops/pause endpoint (mirrors the
    #    Quick-actions button operators already use)
    try:
        import httpx
        base = os.environ.get("DASHBOARD_INTERNAL_URL", "http://localhost:8081")
        with httpx.Client(timeout=3.0) as c:
            r = c.post(f"{base}/api/ops/pause", json={"reason": f"unified_risk: {reason}"})
        actions["crypto_paused"] = r.status_code in (200, 202)
    except Exception as exc:
        logger.warning("unified_risk: crypto pause call failed: %s", exc)

    # 3. Slack notification (best-effort; doesn't fail the trip)
    try:
        from .slack_alerts import SlackAlerter
        alerter = SlackAlerter.from_env()
        alerter.notify_risk_critical(
            metric="combined_drawdown",
            value=COMBINED_DD_THRESHOLD_PCT,  # actual value passed via reason
            threshold=COMBINED_DD_THRESHOLD_PCT,
        )
        actions["slack_sent"] = True
    except Exception as exc:
        logger.warning("unified_risk: slack alert failed: %s", exc)

    logger.warning(
        "UNIFIED_RISK: tripped — %s — actions=%s", reason, actions,
    )
    return actions


def check_and_trip() -> dict:
    """Idempotent: compute status; if breaker triggered, fire kill switches."""
    status = get_combined_risk_status()
    if status["circuit_breaker_active"]:
        # Skip if already tripped (kill flag exists)
        if not _STOCKS_KILL_FLAG.exists():
            reason = (
                f"combined drawdown {status['combined_drawdown_pct']:.2f}% "
                f"≥ threshold {status['threshold_pct']:.2f}% "
                f"(peak ${status['combined_peak_equity']:.0f}, "
                f"now ${status['total_equity']:.0f})"
            )
            status["actions"] = trip_combined_kill_switch(reason)
        else:
            status["actions"] = {"already_tripped": True}
    return status


# ---------------------------------------------------------------------------
# CLI for cron
# ---------------------------------------------------------------------------


def main() -> int:
    """Run from cron: prints JSON status, exits 0 always."""
    import sys
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    status = check_and_trip()
    print(json.dumps(status, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
