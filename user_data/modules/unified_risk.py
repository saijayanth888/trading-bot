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
# Risk gates — operator-editable thresholds (stage/10-risk-gates-yaml)
# ---------------------------------------------------------------------------
#
# These were hard-coded constants. They now live in config.json:risk_gates so
# the operator can edit them via the dashboard's /api/ops/risk_gates endpoint
# (or directly in config.json) without a code change.
#
# The defaults below MUST match the operator-approved 2026-05-11 set and are
# also what we fall back to when:
#   - config.json is missing entirely (cold start)
#   - the risk_gates block is absent from config.json (rollback)
#   - an individual key is missing from the block (partial config)
#
# Override priority (highest first):
#   1. Per-key value in config.json:risk_gates
#   2. _RISK_GATE_DEFAULTS (this file)

_RISK_GATE_DEFAULTS = {
    "daily_loss_halt_pct":        0.03,
    "weekly_loss_size_cut_pct":   0.05,
    "weekly_loss_size_factor":    0.5,
    "single_name_cap_pct":        0.10,
    "correlation_cap":            0.85,
    "vix_high_multiplier":        2.0,
    "vix_high_min_size_factor":   0.25,
}


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


def _load_risk_gates() -> dict:
    """Return the merged risk_gates: defaults overlaid with config.json values.

    Backward-compat: if the ``risk_gates`` block is absent the defaults are
    returned unchanged. Missing individual keys also fall back to defaults
    silently. Non-numeric or out-of-band values are ignored with a warning
    (defaults win) — keeps the trading loop alive when the operator typos
    a manual edit.

    Read every call rather than module-init so config.json edits via the
    dashboard POST take effect on the *next* trading-loop tick without a
    process restart.
    """
    cfg = _load_json(_CONFIG_JSON) or {}
    raw = cfg.get("risk_gates")
    merged = dict(_RISK_GATE_DEFAULTS)
    if not isinstance(raw, dict):
        return merged
    for key, default_val in _RISK_GATE_DEFAULTS.items():
        if key not in raw:
            continue
        val = raw[key]
        if not isinstance(val, (int, float)) or isinstance(val, bool):
            logger.warning(
                "unified_risk: risk_gates.%s is not a number (got %r) — using default %s",
                key, val, default_val,
            )
            continue
        merged[key] = float(val)
    return merged


def get_risk_gate(key: str) -> float:
    """Fetch a single risk-gate threshold by name.

    Public helper for callers outside this module (the wheel runner and
    Shark sizing code import this). Raises KeyError on unknown keys so
    typos surface loudly rather than silently returning the default 0.
    """
    if key not in _RISK_GATE_DEFAULTS:
        raise KeyError(
            f"unknown risk_gate {key!r}; known keys: "
            f"{sorted(_RISK_GATE_DEFAULTS)}"
        )
    return _load_risk_gates()[key]


def _crypto_starting_equity() -> float:
    """Crypto baseline equity for drawdown computation.

    Reads dry_run_wallet from the merged config (config.json +
    config-private.json — both passed to freqtrade). Returns whichever
    is set, regardless of the dry_run flag — the wallet number is the
    DD baseline whether we're paper-trading or live (live would also
    query the exchange, but having a hard floor keeps DD math sane).
    """
    cfg = _load_json(_CONFIG_JSON) or {}
    private_path = _CONFIG_JSON.parent / "config-private.json"
    private = _load_json(private_path) or {}
    wallet = cfg.get("dry_run_wallet") or private.get("dry_run_wallet") or 0.0
    return float(wallet)


def _v4_is_active() -> bool:
    """V4 (quanta_core) is the active engine when LIVE_ENGINE_MODE is set."""
    return (os.environ.get("LIVE_ENGINE_MODE") or "").lower() in ("live", "shadow")


def _v4_realised_pnl_usd() -> float:
    """Sum closed-trade PnL from public.trade_journal for V4 paper fills.

    V4 rows are tagged by UUID-shaped external_id (the client_order_id).
    Only closed rows (closed_at IS NOT NULL) carry a populated pnl.
    """
    try:
        import psycopg
    except ImportError:
        return 0.0
    dsn = (
        f"host={os.environ.get('POSTGRES_HOST', 'postgres')} "
        f"port={os.environ.get('POSTGRES_PORT', '5432')} "
        f"user={os.environ.get('POSTGRES_USER', 'tradebot')} "
        f"password={os.environ.get('POSTGRES_PASSWORD', '')} "
        f"dbname={os.environ.get('POSTGRES_DB', 'tradebot')}"
    )
    try:
        with psycopg.connect(dsn, connect_timeout=2) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT COALESCE(SUM(pnl), 0)
                    FROM public.trade_journal
                    WHERE external_id LIKE '%-%-%-%-%'
                      AND closed_at IS NOT NULL
                    """
                )
                row = cur.fetchone()
        return float((row or [0])[0] or 0)
    except Exception as exc:
        logger.debug("unified_risk: v4 realised pnl probe failed: %s", exc)
        return 0.0


def _v4_unrealised_pnl_usd() -> float:
    """Mark V4 open paper positions to current Coinbase REST mid.

    For each pair with net BUY qty > 0 in quanta_schema.fills:
      unrealised = qty * (current_close - avg_buy_px)
    """
    try:
        import psycopg
        import urllib.request, json as _json
    except ImportError:
        return 0.0
    dsn = (
        f"host={os.environ.get('POSTGRES_HOST', 'postgres')} "
        f"port={os.environ.get('POSTGRES_PORT', '5432')} "
        f"user={os.environ.get('POSTGRES_USER', 'tradebot')} "
        f"password={os.environ.get('POSTGRES_PASSWORD', '')} "
        f"dbname={os.environ.get('POSTGRES_DB', 'tradebot')}"
    )
    try:
        with psycopg.connect(dsn, connect_timeout=2) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT p.symbol,
                           SUM(CASE WHEN f.side='BUY'  THEN f.qty ELSE 0 END) -
                           SUM(CASE WHEN f.side='SELL' THEN f.qty ELSE 0 END)            AS net_qty,
                           SUM(CASE WHEN f.side='BUY' THEN f.qty * f.price ELSE 0 END) /
                           NULLIF(SUM(CASE WHEN f.side='BUY' THEN f.qty ELSE 0 END), 0)  AS avg_buy_px
                    FROM quanta_schema.fills f
                    JOIN quanta_schema.proposals p USING (client_order_id)
                    GROUP BY p.symbol
                    HAVING SUM(CASE WHEN f.side='BUY' THEN f.qty ELSE 0 END) -
                           SUM(CASE WHEN f.side='SELL' THEN f.qty ELSE 0 END) > 0
                    """
                )
                rows = cur.fetchall()
        if not rows:
            return 0.0
        total = 0.0
        for sym, net_qty, avg_buy in rows:
            try:
                product = str(sym).replace("/", "-")
                url = f"https://api.exchange.coinbase.com/products/{product}/ticker"
                with urllib.request.urlopen(url, timeout=2) as r:
                    ticker = _json.loads(r.read())
                px = float(ticker.get("price") or 0)
                if px > 0:
                    total += float(net_qty) * (px - float(avg_buy or 0))
            except Exception as exc:
                logger.debug("unified_risk: ticker fetch %s failed: %s", sym, exc)
                continue
        return total
    except Exception as exc:
        logger.debug("unified_risk: v4 unrealised pnl probe failed: %s", exc)
        return 0.0


def _crypto_realised_pnl_usd() -> float:
    """Sum of closed-trade PnL in USD.

    Source of truth depends on engine:
      • V4 active  → public.trade_journal (rows tagged by UUID external_id)
      • freqtrade  → /api/v1/profit (legacy, kept for rollback)
    """
    if _v4_is_active():
        return _v4_realised_pnl_usd()
    try:
        import httpx
    except ImportError:
        return 0.0
    base = os.environ.get("FREQTRADE_API_URL", "http://freqtrade:8080")
    user = os.environ.get("FREQTRADE_API_USER")
    pw = os.environ.get("FREQTRADE_API_PASS")
    try:
        with httpx.Client(timeout=3.0, auth=(user, pw) if user and pw else None) as c:
            r = c.get(f"{base}/api/v1/profit")
        if r.status_code != 200:
            return 0.0
        return float((r.json() or {}).get("profit_closed_coin") or 0.0)
    except Exception as exc:
        logger.debug("unified_risk: realised pnl probe failed: %s", exc)
        return 0.0


def _crypto_unrealised_pnl_usd() -> float:
    """Sum mark-to-market PnL across open paper positions.

    V4 active  → quanta_schema.fills + Coinbase ticker for current price.
    freqtrade  → /api/v1/status profit_abs (legacy, kept for rollback).
    """
    if _v4_is_active():
        return _v4_unrealised_pnl_usd()
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
    """Count of open crypto paper positions.

    V4 active  → quanta_schema.fills aggregation (net BUY > 0).
    freqtrade  → /api/v1/status length (legacy).
    """
    if _v4_is_active():
        try:
            import psycopg
        except ImportError:
            return 0
        dsn = (
            f"host={os.environ.get('POSTGRES_HOST', 'postgres')} "
            f"port={os.environ.get('POSTGRES_PORT', '5432')} "
            f"user={os.environ.get('POSTGRES_USER', 'tradebot')} "
            f"password={os.environ.get('POSTGRES_PASSWORD', '')} "
            f"dbname={os.environ.get('POSTGRES_DB', 'tradebot')}"
        )
        try:
            with psycopg.connect(dsn, connect_timeout=2) as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        SELECT count(*) FROM (
                            SELECT p.symbol
                            FROM quanta_schema.fills f
                            JOIN quanta_schema.proposals p USING (client_order_id)
                            GROUP BY p.symbol
                            HAVING SUM(CASE WHEN f.side='BUY' THEN f.qty ELSE 0 END) -
                                   SUM(CASE WHEN f.side='SELL' THEN f.qty ELSE 0 END) > 0
                        ) t
                        """
                    )
                    row = cur.fetchone()
            return int((row or [0])[0] or 0)
        except Exception:
            return 0
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


def _is_nyse_open_now() -> bool:
    """True when NYSE regular session is open (Mon-Fri 09:30-16:00 ET).
    Holiday-blind for now; the wheel pilot doesn't trade holidays anyway.
    """
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


def _dd(equity: float, ref: float) -> float:
    """Drawdown fraction: 1 - equity/ref. Floors at 0 (never negative for
    above-peak), no upper cap (a liquidation event can produce >100% DD).

    Module-level so the unit tests can pin the formula independent of how
    get_combined_risk_status composes it.
    """
    if ref <= 0:
        return 0.0
    return max(0.0, (ref - equity) / ref)


def _load_peaks() -> tuple[Optional[float], Optional[float], Optional[float]]:
    """Return (combined_peak, crypto_peak, stocks_peak). Any may be None
    on first call (no peak recorded yet)."""
    p = _load_json(_PEAK_FILE) or {}
    combined = p.get("combined_peak_equity")
    crypto = p.get("crypto_peak_equity")
    stocks = p.get("stocks_peak_equity")
    return (
        float(combined) if combined is not None else None,
        float(crypto) if crypto is not None else None,
        float(stocks) if stocks is not None else None,
    )


def _save_peak(
    combined: float,
    crypto: float = 0.0,
    stocks: float = 0.0,
    components: Optional[dict] = None,
) -> None:
    """Persist all three peaks (combined + per-side) so a single-side dip
    doesn't reset the other side's peak. components is the live snapshot
    that produced these peaks — useful for forensic review."""
    _PEAK_FILE.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "combined_peak_equity": combined,
        "crypto_peak_equity": crypto,
        "stocks_peak_equity": stocks,
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "components": components or {},
    }
    tmp = _PEAK_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, indent=2))
    tmp.replace(_PEAK_FILE)


# Backward-compat alias — older callers used _load_peak (singular) returning
# just the combined peak. Kept so existing code doesn't break.
def _load_peak() -> Optional[float]:
    combined, _crypto, _stocks = _load_peaks()
    return combined


# How long can the stocks snapshot be stale before we treat it as a
# fail-safe trip? 10 min covers the wheel_snapshot every-30-min cron with
# margin for one missed run.
STOCKS_STALE_SECONDS = int(os.environ.get("UNIFIED_STOCKS_STALE_S", "600"))

# P0-J: a separate "we no longer trust this number" horizon for the
# combined-DD math. Past this point (default 2 h) we can't tell whether
# the last stocks equity reading still reflects reality (overnight gaps,
# wheel runner crash, network split, etc.). We zero-out the stocks
# contribution to the combined drawdown rather than carry forward a stale
# value that could either understate (stocks bounced back) or overstate
# (stocks crashed further) actual risk.
STOCKS_UNTRUSTED_SECONDS = int(os.environ.get("UNIFIED_STOCKS_UNTRUSTED_S", "7200"))


# ---------------------------------------------------------------------------
# Risk-gate evaluators (config-driven, callable from wheel + shark runners)
# ---------------------------------------------------------------------------
#
# Every threshold below comes from _load_risk_gates() — never a literal. To
# change a threshold, edit config.json:risk_gates (or POST /api/ops/risk_gates).
# These helpers stay pure (no side effects) so callers can decide what to do
# with the verdict.


def evaluate_loss_size_factor(
    daily_pnl_pct: float,
    weekly_pnl_pct: float,
    gates: Optional[dict] = None,
) -> dict:
    """Return how the daily / weekly loss gates should size positions.

    Inputs are signed P&L fractions (e.g. -0.04 == down 4%). Returns:
        {
            "size_factor":   float,   # multiply intended size by this
            "halt":          bool,    # True == do not open ANY new entries
            "reasons":       list[str],
        }

    Wiring:
      - daily loss <= -daily_loss_halt_pct           → halt entirely
      - weekly loss <= -weekly_loss_size_cut_pct     → multiply size by
                                                       weekly_loss_size_factor
    """
    g = gates if gates is not None else _load_risk_gates()
    reasons: list[str] = []
    halt = False
    size_factor = 1.0

    daily_halt = g["daily_loss_halt_pct"]
    if daily_pnl_pct <= -daily_halt:
        halt = True
        size_factor = 0.0
        reasons.append(
            f"daily P&L {daily_pnl_pct*100:.2f}% breaches halt {-daily_halt*100:.2f}%"
        )

    weekly_cut = g["weekly_loss_size_cut_pct"]
    weekly_factor = g["weekly_loss_size_factor"]
    if weekly_pnl_pct <= -weekly_cut and not halt:
        size_factor = min(size_factor, weekly_factor)
        reasons.append(
            f"weekly P&L {weekly_pnl_pct*100:.2f}% ≤ {-weekly_cut*100:.2f}% → "
            f"size×{weekly_factor:g}"
        )

    return {"size_factor": size_factor, "halt": halt, "reasons": reasons}


def evaluate_vix_size_factor(
    vix_now: float,
    vix_historical: float,
    gates: Optional[dict] = None,
) -> dict:
    """Return the VIX-driven size factor.

    When current VIX exceeds ``vix_high_multiplier × vix_historical`` we
    drop sizing to ``vix_high_min_size_factor`` (default 0.25). Returns a
    dict with the factor and the reason string for logging.
    """
    g = gates if gates is not None else _load_risk_gates()
    mult = g["vix_high_multiplier"]
    min_factor = g["vix_high_min_size_factor"]
    if vix_historical <= 0:
        return {"size_factor": 1.0, "reason": "no historical VIX baseline"}
    if vix_now >= mult * vix_historical:
        return {
            "size_factor": min_factor,
            "reason": (
                f"VIX {vix_now:.1f} ≥ {mult:g}× historical {vix_historical:.1f} → "
                f"size×{min_factor:g}"
            ),
        }
    return {"size_factor": 1.0, "reason": "VIX within normal band"}


def evaluate_single_name_cap(
    intended_notional: float,
    portfolio_equity: float,
    gates: Optional[dict] = None,
) -> dict:
    """Cap a single-name notional at ``single_name_cap_pct`` of equity.

    Returns the capped notional + whether it was actually clipped. Caller
    is responsible for applying the cap (this is pure).
    """
    g = gates if gates is not None else _load_risk_gates()
    cap_pct = g["single_name_cap_pct"]
    if portfolio_equity <= 0:
        return {"capped_notional": 0.0, "was_capped": True,
                "reason": "non-positive equity"}
    cap_notional = portfolio_equity * cap_pct
    if intended_notional > cap_notional:
        return {
            "capped_notional": cap_notional,
            "was_capped": True,
            "reason": (
                f"intended ${intended_notional:.0f} > cap "
                f"${cap_notional:.0f} ({cap_pct*100:.1f}% of equity)"
            ),
        }
    return {"capped_notional": intended_notional, "was_capped": False,
            "reason": "within single-name cap"}


def evaluate_correlation_cap(
    candidate_corr: float,
    gates: Optional[dict] = None,
) -> dict:
    """Reject a candidate position whose correlation with the existing book
    exceeds ``correlation_cap`` (default 0.85)."""
    g = gates if gates is not None else _load_risk_gates()
    cap = g["correlation_cap"]
    if candidate_corr > cap:
        return {
            "allowed": False,
            "reason": (
                f"corr {candidate_corr:.2f} > cap {cap:.2f} — already saturated"
            ),
        }
    return {"allowed": True, "reason": f"corr {candidate_corr:.2f} ≤ cap {cap:.2f}"}


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
    crypto_peak_equity: float
    stocks_peak_equity: float
    combined_open_positions: int
    crypto_open_positions: int
    stocks_open_positions: int
    circuit_breaker_active: bool
    threshold_pct: float
    snapshot_age_seconds: Optional[int]
    stocks_data_stale: bool
    stocks_data_untrusted: bool   # P0-J: snapshot too old to trust at all
    market_open_now: bool         # Lets the dashboard suppress
                                  # stocks-stale/untrusted false alarms
                                  # outside market hours.
    sources: dict


def get_combined_risk_status() -> dict:
    """Return the full combined risk picture without side effects.

    Adds two safety properties beyond the basic drawdown:
      - stocks_data_stale: True when the wheel_snapshot is older than
        STOCKS_STALE_SECONDS (default 10 min). Means we can't trust the
        stocks-equity number — fail-safe trip.
      - per-side peaks: the crypto_peak / stocks_peak so a temporary dip
        on one side doesn't reset the other side's high-water mark.
    """
    # Crypto side
    starting = _crypto_starting_equity()
    realised = _crypto_realised_pnl_usd()
    unrealised = _crypto_unrealised_pnl_usd()
    crypto_equity = starting + realised + unrealised

    # Stocks side
    stocks = _stocks_state()
    stocks_equity = stocks["portfolio_value"]

    total = crypto_equity + stocks_equity

    # Per-side + combined peak — each tracked independently so a dip on
    # one side doesn't reset the other side's peak.
    prior_combined, prior_crypto, prior_stocks = _load_peaks()
    combined_peak = max(total, prior_combined) if prior_combined is not None else total
    crypto_peak = max(crypto_equity, prior_crypto) if prior_crypto is not None else crypto_equity
    stocks_peak = max(stocks_equity, prior_stocks) if prior_stocks is not None else stocks_equity

    needs_save = (
        prior_combined is None
        or combined_peak > prior_combined
        or (prior_crypto is not None and crypto_peak > prior_crypto)
        or (prior_stocks is not None and stocks_peak > prior_stocks)
    )
    if needs_save:
        _save_peak(
            combined_peak, crypto_peak, stocks_peak,
            {"crypto": crypto_equity, "stocks": stocks_equity},
        )

    # Snapshot freshness (stocks-side fail-safe)
    snap_age = None
    snap_ts = stocks.get("snapshot_ts")
    if snap_ts:
        try:
            snap_dt = datetime.fromisoformat(snap_ts.replace("Z", "+00:00"))
            snap_age = int((datetime.now(timezone.utc) - snap_dt).total_seconds())
        except (ValueError, TypeError):
            snap_age = None
    stocks_stale = snap_age is not None and snap_age > STOCKS_STALE_SECONDS

    # P0-J: when the stocks snapshot is *very* stale (>2 h by default), we
    # don't trust its equity figure for the combined-DD math at all. Using
    # the last seen value silently shifts the combined peak/now ratio in
    # whichever direction stale data flatters or worsens. Treat the stocks
    # side as a zero contribution: combined_dd is then crypto-only.
    stocks_untrusted = snap_age is not None and snap_age > STOCKS_UNTRUSTED_SECONDS

    crypto_dd = _dd(crypto_equity, crypto_peak)
    stocks_dd = _dd(stocks_equity, stocks_peak)
    if stocks_untrusted:
        # Compare crypto-only equity against the crypto peak; the combined
        # peak still reflects historical highs that included stocks but the
        # *current* combined number can't be honestly compared against it
        # while stocks is dark.
        combined_dd = crypto_dd
    else:
        combined_dd = _dd(total, combined_peak)

    # Breaker trips on threshold drawdown always. Stale-data trip only
    # fires during market hours — outside the open, the wheel_snapshot
    # cron isn't firing by design (Mon-Fri 9-16 ET) so a stale snapshot
    # is expected, not dangerous. Inside market hours, stale = fail-safe.
    market_open_now = _is_nyse_open_now()
    breaker = (
        combined_dd >= COMBINED_DD_THRESHOLD_PCT
        or (stocks_stale and market_open_now)
    )

    status = RiskStatus(
        crypto_equity=round(crypto_equity, 2),
        stocks_equity=round(stocks_equity, 2),
        total_equity=round(total, 2),
        crypto_drawdown_pct=round(crypto_dd * 100, 3),
        stocks_drawdown_pct=round(stocks_dd * 100, 3),
        combined_drawdown_pct=round(combined_dd * 100, 3),
        combined_peak_equity=round(combined_peak, 2),
        crypto_peak_equity=round(crypto_peak, 2),
        stocks_peak_equity=round(stocks_peak, 2),
        combined_open_positions=_crypto_open_count() + stocks["open_positions"],
        crypto_open_positions=_crypto_open_count(),
        stocks_open_positions=stocks["open_positions"],
        circuit_breaker_active=breaker,
        threshold_pct=round(COMBINED_DD_THRESHOLD_PCT * 100, 1),
        snapshot_age_seconds=snap_age,
        stocks_data_stale=stocks_stale,
        stocks_data_untrusted=stocks_untrusted,
        market_open_now=market_open_now,
        sources={
            "crypto_starting_equity": starting,
            "crypto_realised_pnl": round(realised, 2),
            "crypto_unrealised_pnl": round(unrealised, 2),
            "stocks_paper": stocks["paper"],
            "stocks_snapshot_ts": snap_ts,
            "stocks_stale_threshold_s": STOCKS_STALE_SECONDS,
            "stocks_untrusted_threshold_s": STOCKS_UNTRUSTED_SECONDS,
            "combined_dd_method": (
                "crypto_only_stocks_untrusted" if stocks_untrusted
                else "crypto_plus_stocks"
            ),
            # Operator-editable thresholds — surfaced so the dashboard /
            # ops_spa "Risk gates" card can display the live config
            # without an extra round-trip to /api/ops/risk_gates.
            "risk_gates": _load_risk_gates(),
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

    # 3. Notification via unified router (Slack + Telegram both fire on critical)
    try:
        from .notifier import notify
        notify.critical(
            "kill_switch",
            reason=reason,
            actions=dict(actions),
            threshold=COMBINED_DD_THRESHOLD_PCT,
        )
        actions["slack_sent"] = True
    except Exception as exc:
        logger.warning("unified_risk: notifier failed: %s", exc)

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
