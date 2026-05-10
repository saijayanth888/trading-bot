"""
wheel.cli — operator entry point.

Usage:
    python -m wheel.cli sell-csps          # Friday morning
    python -m wheel.cli profit-take        # any weekday
    python -m wheel.cli sell-covered-calls # Monday after assignment
    python -m wheel.cli status             # show open positions + cumulative P&L
    python -m wheel.cli cancel-stale       # cancel stale DAY orders
    python -m wheel.cli kill-ticker SOFI   # set 90-day kill flag manually
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

# Ensure stocks/ is on sys.path so imports work from any cwd
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# Triggers .env loader on import (via shark.run)
from wheel import runner
from wheel.broker import from_env
from wheel.config import load_config
from wheel.state import (
    cumulative_pnl, kill_ticker, load_positions,
)


def cmd_sell_csps(args: argparse.Namespace) -> int:
    summary = runner.sell_csps()
    print(json.dumps(summary, indent=2))
    return 0 if not summary.get("errors") else 1


def cmd_profit_take(args: argparse.Namespace) -> int:
    summary = runner.profit_take_check()
    print(json.dumps(summary, indent=2))
    return 0 if not summary.get("errors") else 1


def cmd_sell_covered_calls(args: argparse.Namespace) -> int:
    summary = runner.sell_covered_calls()
    print(json.dumps(summary, indent=2))
    return 0 if not summary.get("errors") else 1


def cmd_status(args: argparse.Namespace) -> int:
    cfg = load_config()
    broker = from_env()
    acct = broker.get_account()
    positions = load_positions()
    pnl = cumulative_pnl()

    out = {
        "config": {
            "symbols": list(cfg.symbols),
            "delta_band": [cfg.delta_min, cfg.delta_max],
            "dte_band": [cfg.dte_min, cfg.dte_max],
            "max_risk_per_ticker": cfg.max_risk_per_ticker_usd,
            "max_total_collateral": cfg.max_total_collateral_usd,
            "paper": cfg.paper,
        },
        "account": {
            "cash": round(acct.cash, 2),
            "buying_power": round(acct.buying_power, 2),
            "portfolio_value": round(acct.portfolio_value, 2),
        },
        "positions": [
            {
                "underlying": p.underlying,
                "kind": p.kind,
                "qty": p.qty,
                "strike": p.strike,
                "expiry": p.expiry,
                "entry_credit": round(p.entry_credit, 2),
                "contract": p.contract_symbol,
            }
            for p in positions
        ],
        "cumulative_pnl_usd": round(pnl, 2),
    }
    print(json.dumps(out, indent=2))
    return 0


def cmd_cancel_stale(args: argparse.Namespace) -> int:
    broker = from_env()
    n = broker.cancel_stale_orders(max_age_minutes=args.max_age)
    print(json.dumps({"cancelled": n}, indent=2))
    return 0


def cmd_kill_ticker(args: argparse.Namespace) -> int:
    kill_ticker(args.ticker.upper(), days=args.days)
    print(json.dumps({"killed": args.ticker.upper(), "days": args.days}))
    return 0


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    p = argparse.ArgumentParser(
        prog="wheel",
        description="Wheel income strategy (cash-secured puts + covered calls)",
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("sell-csps", help="Sell new CSPs on allowed tickers")
    sub.add_parser("profit-take", help="Buy-to-close at profit-take threshold")
    sub.add_parser("sell-covered-calls", help="Sell CCs on assigned shares")
    sub.add_parser("status", help="Show config, account, positions, P&L")

    p_cancel = sub.add_parser("cancel-stale", help="Cancel stale DAY orders")
    p_cancel.add_argument("--max-age", type=int, default=240,
                          help="minutes (default 240)")

    p_kill = sub.add_parser("kill-ticker", help="Set per-ticker kill flag")
    p_kill.add_argument("ticker")
    p_kill.add_argument("--days", type=int, default=90)

    args = p.parse_args(argv)

    handlers = {
        "sell-csps": cmd_sell_csps,
        "profit-take": cmd_profit_take,
        "sell-covered-calls": cmd_sell_covered_calls,
        "status": cmd_status,
        "cancel-stale": cmd_cancel_stale,
        "kill-ticker": cmd_kill_ticker,
    }
    return handlers[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
