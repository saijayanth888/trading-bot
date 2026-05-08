#!/usr/bin/env python3
"""
Validate that paper-trading results clear the go-live bar.

Reads the closed-trade history from PostgreSQL
(the trade_journal table created by trade_journal.py) and checks:

    sharpe_ratio  > 1.5     (annualised, daily-binned)
    max_drawdown  < 12%
    profit_factor > 1.4
    win_rate      > 55%
    total_trades  ≥ 200

Usage:

    python scripts/validate_readiness.py
    python scripts/validate_readiness.py --window-days 90
    python scripts/validate_readiness.py --json   # emits a JSON report

Exit code 0 only when *every* metric passes. Anything else is non-zero so
go_live.sh can chain `validate_readiness.py && deploy ...`.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import psycopg
from psycopg.rows import dict_row

def _resolve_dsn() -> str:
    """URL-encode-safe DSN — same pattern as user_data/modules/db.py."""
    from urllib.parse import quote_plus
    explicit = os.environ.get("DATABASE_URL", "").strip()
    if explicit:
        return explicit
    user = os.environ.get("POSTGRES_USER", "tradebot")
    password = os.environ.get("POSTGRES_PASSWORD", "tradebot-change-me")
    host = os.environ.get("POSTGRES_HOST", "localhost")
    port = os.environ.get("POSTGRES_PORT", "5434")
    db = os.environ.get("POSTGRES_DB", "tradebot")
    return f"postgresql://{quote_plus(user)}:{quote_plus(password)}@{host}:{port}/{db}"


DEFAULT_DSN = _resolve_dsn()

# Annualisation factor for crypto (24/7 markets). 365 because crypto
# doesn't take weekends off — using 252 underestimates volatility.
ANNUALISATION_DAYS = 365.0


@dataclass
class CheckResult:
    name: str
    actual: float | int
    threshold: float | int
    passed: bool
    op: str                      # ">", "<", ">="
    fmt: str = "{:.4f}"          # display format for `actual`

    def line(self, color: bool = True) -> str:
        tick = "PASS" if self.passed else "FAIL"
        if color and sys.stdout.isatty():
            tick = f"\033[32mPASS\033[0m" if self.passed else f"\033[31mFAIL\033[0m"
        actual = self.fmt.format(self.actual) if not isinstance(self.actual, str) else self.actual
        thr = self.fmt.format(self.threshold) if not isinstance(self.threshold, str) else self.threshold
        return f"  [{tick}]  {self.name:<14} {actual} {self.op} {thr}"


@dataclass
class ReadinessReport:
    window_start: str | None
    window_end: str | None
    n_trades: int
    checks: list[CheckResult] = field(default_factory=list)
    diagnostics: dict[str, Any] = field(default_factory=dict)

    @property
    def all_passed(self) -> bool:
        return bool(self.checks) and all(c.passed for c in self.checks)

    def to_dict(self) -> dict:
        return {
            "window_start": self.window_start,
            "window_end": self.window_end,
            "n_trades": self.n_trades,
            "all_passed": self.all_passed,
            "checks": [
                {
                    "name": c.name, "actual": c.actual, "threshold": c.threshold,
                    "op": c.op, "passed": c.passed,
                } for c in self.checks
            ],
            "diagnostics": self.diagnostics,
        }


def _load_closed_trades(
    dsn: str, window_days: int | None,
) -> tuple[list[dict], str | None, str | None]:
    where = "closed_at IS NOT NULL"
    params: list[Any] = []
    window_start: str | None = None
    window_end: str | None = None
    if window_days is not None and window_days > 0:
        cutoff = datetime.now(timezone.utc) - timedelta(days=window_days)
        where += " AND closed_at >= %s"
        params.append(cutoff)
        window_start = cutoff.isoformat()
    sql = (
        f"SELECT closed_at, pnl, pnl_pct, stake "
        f"FROM trade_journal WHERE {where} ORDER BY closed_at ASC"
    )
    try:
        with psycopg.connect(dsn, connect_timeout=5) as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute(sql, tuple(params))
                rows = list(cur.fetchall())
    except Exception as exc:
        raise SystemExit(f"could not query trade journal: {exc}") from exc

    # Normalise datetimes to ISO strings so the rest of the script can treat
    # them as strings (and JSON-serialise them in --json mode).
    for r in rows:
        for k in ("closed_at",):
            v = r.get(k)
            if isinstance(v, datetime):
                r[k] = v.astimezone(timezone.utc).isoformat()

    if rows:
        window_end = rows[-1]["closed_at"]
        if window_start is None:
            window_start = rows[0]["closed_at"]
    return rows, window_start, window_end


def _daily_pnl_pct(rows: list[dict]) -> list[float]:
    """Sum each trade's pnl_pct into the day it closed (UTC)."""
    by_day: dict[str, float] = {}
    for r in rows:
        ts = r.get("closed_at") or ""
        day = ts[:10] if ts else "0000-00-00"
        by_day[day] = by_day.get(day, 0.0) + float(r.get("pnl_pct") or 0.0)
    return [v for _, v in sorted(by_day.items())]


def _max_drawdown(rows: list[dict]) -> float:
    """Peak-to-trough drawdown of the cumulative-pnl curve, normalized
    against the running peak. Returns the absolute fraction (0..1)."""
    if not rows:
        return 0.0
    cum = 0.0
    peak = 0.0
    max_dd = 0.0
    starting_equity = 0.0
    # Estimate "starting equity" as the sum of all stakes / number of trades
    # (proxy when no equity series is recorded). Floors at 1 to avoid div0.
    stakes = [float(r.get("stake") or 0.0) for r in rows]
    starting_equity = max(1.0, sum(stakes) / max(len(stakes), 1))
    for r in rows:
        cum += float(r.get("pnl") or 0.0)
        peak = max(peak, cum)
        dd_quote = peak - cum                       # in quote currency
        # Normalise against (starting_equity + peak) so a profitable run
        # giving up half its gains scores ~50% of accumulated profits as
        # drawdown — same shape as the standard equity-curve metric.
        denom = starting_equity + max(peak, 0.0)
        if denom > 0:
            max_dd = max(max_dd, dd_quote / denom)
    return float(max_dd)


def _profit_factor(rows: list[dict]) -> float:
    gross_win = sum(float(r.get("pnl") or 0.0) for r in rows if (r.get("pnl") or 0.0) > 0)
    gross_loss = sum(-float(r.get("pnl") or 0.0) for r in rows if (r.get("pnl") or 0.0) < 0)
    if gross_loss == 0.0:
        return float("inf") if gross_win > 0 else 0.0
    return float(gross_win / gross_loss)


def _win_rate(rows: list[dict]) -> float:
    if not rows:
        return 0.0
    wins = sum(1 for r in rows if (r.get("pnl") or 0.0) > 0)
    return float(wins / len(rows))


def _annualised_sharpe(daily_pcts: list[float]) -> float:
    if len(daily_pcts) < 2:
        return 0.0
    mean = sum(daily_pcts) / len(daily_pcts)
    var = sum((x - mean) ** 2 for x in daily_pcts) / (len(daily_pcts) - 1)
    sd = math.sqrt(var)
    if sd <= 0.0:
        return 0.0
    return float((mean / sd) * math.sqrt(ANNUALISATION_DAYS))


def evaluate_readiness(
    dsn: str,
    *,
    window_days: int | None = None,
    sharpe_min: float = 1.5,
    drawdown_max: float = 0.12,
    profit_factor_min: float = 1.4,
    win_rate_min: float = 0.55,
    trades_min: int = 200,
) -> ReadinessReport:
    rows, win_start, win_end = _load_closed_trades(dsn, window_days)
    n = len(rows)
    daily = _daily_pnl_pct(rows)
    sharpe = _annualised_sharpe(daily)
    max_dd = _max_drawdown(rows)
    pf = _profit_factor(rows)
    wr = _win_rate(rows)

    checks = [
        CheckResult("sharpe",        round(sharpe, 4),  sharpe_min,        sharpe > sharpe_min,             ">"),
        CheckResult("max_drawdown",  round(max_dd, 4),  drawdown_max,      max_dd < drawdown_max,           "<", fmt="{:.2%}"),
        CheckResult("profit_factor", round(pf, 4),      profit_factor_min, pf > profit_factor_min,          ">"),
        CheckResult("win_rate",      round(wr, 4),      win_rate_min,      wr > win_rate_min,               ">", fmt="{:.2%}"),
        CheckResult("total_trades",  n,                 trades_min,        n >= trades_min,                 ">=", fmt="{:d}"),
    ]
    return ReadinessReport(
        window_start=win_start, window_end=win_end, n_trades=n, checks=checks,
        diagnostics={
            "daily_buckets": len(daily),
            "annualisation_days": ANNUALISATION_DAYS,
            "starting_equity_proxy": (
                sum(float(r.get("stake") or 0.0) for r in rows) / max(n, 1)
            ),
        },
    )


def _print_report(report: ReadinessReport) -> None:
    print("=" * 64)
    print(" Go-live readiness check")
    print("=" * 64)
    print(f"  window:    {report.window_start} → {report.window_end}")
    print(f"  trades:    {report.n_trades}")
    print(f"  daily bkt: {report.diagnostics.get('daily_buckets', 0)}")
    print()
    for c in report.checks:
        print(c.line())
    print()
    if report.all_passed:
        msg = "READY — all checks passed."
        if sys.stdout.isatty():
            msg = f"\033[32m{msg}\033[0m"
        print(msg)
    else:
        msg = "NOT READY — fix the FAIL items above before going live."
        if sys.stdout.isatty():
            msg = f"\033[31m{msg}\033[0m"
        print(msg)


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--dsn", default=DEFAULT_DSN,
                   help="PostgreSQL DSN (default: $DATABASE_URL or compose default)")
    p.add_argument("--window-days", type=int, default=None,
                   help="Restrict evaluation to the last N days (default: all-time)")
    p.add_argument("--sharpe-min", type=float, default=1.5)
    p.add_argument("--drawdown-max", type=float, default=0.12)
    p.add_argument("--profit-factor-min", type=float, default=1.4)
    p.add_argument("--win-rate-min", type=float, default=0.55)
    p.add_argument("--trades-min", type=int, default=200)
    p.add_argument("--json", action="store_true", help="Emit JSON report only")
    args = p.parse_args()

    report = evaluate_readiness(
        args.dsn, window_days=args.window_days,
        sharpe_min=args.sharpe_min, drawdown_max=args.drawdown_max,
        profit_factor_min=args.profit_factor_min,
        win_rate_min=args.win_rate_min, trades_min=args.trades_min,
    )
    if args.json:
        print(json.dumps(report.to_dict(), indent=2))
    else:
        _print_report(report)
    return 0 if report.all_passed else 1


if __name__ == "__main__":
    sys.exit(main())
