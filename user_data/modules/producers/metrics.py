"""
producers/metrics.py — single source of truth for Sharpe, max-DD, win rate.

Closes **B3** (Sharpe 10.58 vs −306 in two different surfaces) by:
  1. Computing Sharpe + max-DD ONCE here, in pure-Python, against a single
     equity / returns sample. Every consumer (`/api/v5/metrics`, the daily
     Slack brief, the readiness gate, the backtest gate card) reads the
     SAME function output.
  2. Guarding against near-zero-mean walk-forward windows — when
     `|mean(returns)| < eps` the Sharpe degenerates to ±inf and the legacy
     code printed `sharpe: -306`. We collapse to `abs(stddev)` (i.e. the
     plain unitless dispersion) as the dispersion stat, and mark the
     window `degenerate: true` in metadata so the caller can render it
     as `—` instead of a misleading number.
  3. Picking ONE annualization convention per asset class:
        - crypto (daily returns):   × sqrt(365)
        - stocks (daily returns):   × sqrt(252)
     The legacy code mixed both (`* math.sqrt(365)` at line 1352 of
     ops_routes.py applied to a stocks PF backtest → ~×1.20 inflation).

Public API:

    sharpe_max_dd(returns, *, annualizer="daily_crypto", eps=1e-9)
        Returns:
            {
                "sharpe":          float,
                "max_drawdown":    float,  # fraction (0.10 = 10% DD)
                "max_drawdown_pct":float,  # percent (10.0)
                "annualizer":      "daily_crypto" | "daily_stocks" | "none",
                "n":               int,
                "mean":            float,
                "stddev":          float,
                "degenerate":      bool,   # true ↔ |mean| < eps
                "windows":         list[dict] | None,   # walk-forward
            }

    walk_forward_variance(window_returns, *, annualizer, eps=1e-9)
        Returns the dispersion stat used to flag unstable strategies.
        Replaces the legacy `stddev/mean` quotient which goes to inf on
        zero-mean windows. We return `abs(stddev)` when `|mean| < eps`,
        else `stddev / abs(mean)` (coefficient of variation).

    win_rate(trades)
        Single classifier. `trades` is a list of dicts each with `pnl` OR
        `pnl_pct` (we DO NOT mix the two — the B2 root cause was exactly
        this dual-key fallback in `shark/dashboard/generate.py`).

Tests in `tests/test_producers_metrics.py` pin:
  - the BTC 34× single-name-cap case (B8 forensic — confirms the producer
    surfaces the violation honestly; risk-engine fix is Builder C scope).
  - the zero-mean walk-forward case (B3 guard).
"""

from __future__ import annotations

import logging
import math
from typing import Any, Iterable

logger = logging.getLogger(__name__)

# Annualization factors per asset class. Daily-bar convention.
_ANNUALIZERS = {
    "daily_crypto": math.sqrt(365),  # crypto trades 24/7
    "daily_stocks": math.sqrt(252),  # NYSE session count
    "none": 1.0,                     # caller already annualized
}


def _coerce_floats(xs: Iterable[Any]) -> list[float]:
    out: list[float] = []
    for x in xs:
        try:
            out.append(float(x))
        except (TypeError, ValueError):
            continue
    return out


def _mean_stddev(returns: list[float]) -> tuple[float, float]:
    """Population stddev (n divisor) — matches the legacy code's
    `np.std(returns)` default (NumPy ddof=0). Tests pin this convention
    so swapping to sample stddev is a versioned change."""
    n = len(returns)
    if n == 0:
        return 0.0, 0.0
    mu = sum(returns) / n
    var = sum((r - mu) ** 2 for r in returns) / n
    return mu, math.sqrt(var)


def sharpe_max_dd(
    returns: Iterable[Any],
    *,
    annualizer: str = "daily_crypto",
    eps: float = 1e-9,
) -> dict[str, Any]:
    """Sharpe + max-DD for a returns series. Guarded against zero mean.

    `returns` is a periodic return series (fractional — 0.01 = 1% bar).
    Max-DD is computed from the CUMULATIVE equity curve produced by
    compounding (1 + r). Returns the DD as a positive fraction (0.10 = 10%
    DD, never negative).

    Annualizer options: "daily_crypto" (×√365), "daily_stocks" (×√252),
    "none" (caller already annualized — multiplier is 1.0).

    Degenerate guard (B3): when `|mean| < eps`, the Sharpe ratio is
    mathematically undefined; the legacy code reported numbers like
    `-306.15` purely from float noise on a near-zero series. We:
      - set `sharpe = 0.0`
      - set `degenerate = True`
      - leave `stddev` populated for the operator to inspect
    The caller (UI) renders `—` on `degenerate: true` instead of "0.00".
    """
    rs = _coerce_floats(returns)
    n = len(rs)
    if n == 0:
        return {
            "sharpe": 0.0,
            "max_drawdown": 0.0,
            "max_drawdown_pct": 0.0,
            "annualizer": annualizer,
            "n": 0,
            "mean": 0.0,
            "stddev": 0.0,
            "degenerate": True,
            "reason": "no-returns",
        }

    mu, sd = _mean_stddev(rs)
    mult = _ANNUALIZERS.get(annualizer, 1.0)

    degenerate = abs(mu) < eps
    if degenerate or sd <= 0:
        sharpe = 0.0
        reason = "zero-mean" if degenerate else "zero-stddev"
    else:
        sharpe = (mu / sd) * mult
        reason = None

    # Max-DD from compounded equity curve. Use 1.0 base, walk forward.
    equity = 1.0
    peak = 1.0
    max_dd = 0.0
    for r in rs:
        equity *= (1.0 + r)
        if equity > peak:
            peak = equity
        if peak > 0:
            dd = (peak - equity) / peak
            if dd > max_dd:
                max_dd = dd

    return {
        "sharpe": round(sharpe, 4),
        "max_drawdown": round(max_dd, 6),
        "max_drawdown_pct": round(max_dd * 100, 4),
        "annualizer": annualizer,
        "n": n,
        "mean": round(mu, 8),
        "stddev": round(sd, 8),
        "degenerate": degenerate,
        "reason": reason,
    }


def walk_forward_variance(
    window_returns: Iterable[Any],
    *,
    annualizer: str = "daily_crypto",  # unused; kept for API parity
    eps: float = 1e-9,
) -> dict[str, Any]:
    """Dispersion stat for one walk-forward window.

    Legacy code (ops_routes.py ~line 1352-1366 + backtest_gates) used
    `stddev / mean` which is `inf` on zero-mean windows — produced the
    `walk_forward_variance: inf` row that the backtest gate card showed
    on `funding_rate_harvest`.

    We return `abs(stddev)` when `|mean| < eps` (plain dispersion), else
    the standard coefficient-of-variation `stddev / abs(mean)`. The
    operator-facing UI labels the column `dispersion (CV or |σ|)` so the
    unit is honest.
    """
    rs = _coerce_floats(window_returns)
    if not rs:
        return {"dispersion": 0.0, "mode": "empty", "n": 0,
                "mean": 0.0, "stddev": 0.0, "degenerate": True}
    mu, sd = _mean_stddev(rs)
    if abs(mu) < eps:
        return {
            "dispersion": round(abs(sd), 8),
            "mode": "abs_stddev",  # B3 zero-mean guard
            "n": len(rs),
            "mean": round(mu, 8),
            "stddev": round(sd, 8),
            "degenerate": True,
        }
    cv = sd / abs(mu)
    return {
        "dispersion": round(cv, 8),
        "mode": "coef_variation",
        "n": len(rs),
        "mean": round(mu, 8),
        "stddev": round(sd, 8),
        "degenerate": False,
    }


def win_rate(trades: Iterable[dict[str, Any]]) -> dict[str, Any]:
    """Compute wins / losses / total_pnl from a closed-trades list.

    `trades`: list of dicts. Pulls pnl in this priority order — DOCUMENTED
    so a schema change doesn't silently regress wins/losses:

        1. ``realized_pnl``       (legacy quanta-core key)
        2. ``pnl``                (shark wheel `trades.jsonl`, shark journal)
        3. ``pnl_usd``            (V4 explainability decisions)

    `pnl_pct` (without an absolute) is **explicitly ignored**: classifying
    by pct alone made every `kb/trades/*.json` row land in `wins=0,
    losses=0` (B2 root cause: those files have `pnl_pct` but no `pnl`).
    Callers that have ONLY pct should pre-multiply by stake before
    handing to this function.

    Returns:
        {
            "total_trades": int,
            "wins":         int,    # pnl > 0
            "losses":       int,    # pnl < 0
            "scratches":    int,    # pnl == 0
            "missing_pnl":  int,    # row had no recognized pnl key
            "total_pnl":    float,
            "win_rate":     float,  # wins / total_trades × 100
            "best_trade":   float,
            "worst_trade":  float,
        }
    """
    total = 0
    wins = 0
    losses = 0
    scratches = 0
    missing = 0
    pnls: list[float] = []
    for t in trades:
        total += 1
        pnl = None
        for k in ("realized_pnl", "pnl", "pnl_usd"):
            v = t.get(k)
            if v is None:
                continue
            try:
                pnl = float(v)
                break
            except (TypeError, ValueError):
                continue
        if pnl is None:
            missing += 1
            continue
        pnls.append(pnl)
        if pnl > 0:
            wins += 1
        elif pnl < 0:
            losses += 1
        else:
            scratches += 1

    classified = wins + losses + scratches
    out: dict[str, Any] = {
        "total_trades": total,
        "wins": wins,
        "losses": losses,
        "scratches": scratches,
        "missing_pnl": missing,
        "total_pnl": round(sum(pnls), 2) if pnls else 0.0,
        "win_rate": round(wins / classified * 100, 2) if classified > 0 else 0.0,
        "best_trade": round(max(pnls), 2) if pnls else 0.0,
        "worst_trade": round(min(pnls), 2) if pnls else 0.0,
    }
    return out


def metrics_snapshot(
    crypto_returns: Iterable[Any] | None = None,
    stocks_returns: Iterable[Any] | None = None,
    crypto_trades: Iterable[dict[str, Any]] | None = None,
    stocks_trades: Iterable[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Bundle Sharpe + max-DD + win-rate per side + combined for the
    `/api/v5/metrics` endpoint.

    Callers pass already-loaded returns + trades; the producer doesn't
    open the DB itself (keeps it pure-CPU and unit-testable).
    """
    from datetime import datetime, UTC
    crypto = sharpe_max_dd(crypto_returns or [], annualizer="daily_crypto")
    stocks = sharpe_max_dd(stocks_returns or [], annualizer="daily_stocks")
    crypto_wr = win_rate(crypto_trades or [])
    stocks_wr = win_rate(stocks_trades or [])

    return {
        "crypto": {**crypto, **crypto_wr},
        "stocks": {**stocks, **stocks_wr},
        "_meta": {
            "snapshot_ts": datetime.now(UTC).isoformat(),
            "age_s": 0,
            "stale": False,
            "market_open_now": False,  # metrics are bar-of-day, market-state irrelevant
            "source": "producers.metrics",
        },
    }
