"""
wheel.cli — operator entry point.

Usage:
    python -m wheel.cli sell-csps          # Friday morning
    python -m wheel.cli profit-take        # any weekday
    python -m wheel.cli sell-covered-calls # Monday after assignment
    python -m wheel.cli status             # show open positions + cumulative P&L
    python -m wheel.cli snapshot           # write account_snapshot.json (dashboard freshness)
    python -m wheel.cli cancel-stale       # cancel stale DAY orders
    python -m wheel.cli kill-ticker SOFI   # set 90-day kill flag manually
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import datetime, timezone
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

_STATE_DIR = Path(__file__).resolve().parent / "state"
_ACCOUNT_SNAPSHOT_FILE = _STATE_DIR / "account_snapshot.json"


def cmd_sell_csps(args: argparse.Namespace) -> int:
    summary = runner.sell_csps()
    print(json.dumps(summary, indent=2, default=str))
    return 0 if not summary.get("errors") else 1


def cmd_profit_take(args: argparse.Namespace) -> int:
    summary = runner.profit_take_check()
    print(json.dumps(summary, indent=2, default=str))
    return 0 if not summary.get("errors") else 1


def cmd_sell_covered_calls(args: argparse.Namespace) -> int:
    summary = runner.sell_covered_calls()
    print(json.dumps(summary, indent=2, default=str))
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


def cmd_candles(args: argparse.Namespace) -> int:
    """Fetch Alpaca bars for a symbol and write candles_{SYM}_{tf}.json.

    Used by the dashboard's stock candles endpoint — the dashboard never
    calls Alpaca; it reads this file. A Hermes cron refreshes every 5 min
    during market hours.
    """
    broker = from_env()
    bars = broker.get_stock_bars(args.symbol.upper(), timeframe=args.timeframe, limit=args.limit)
    if not bars:
        print(json.dumps({"error": "no bars returned", "symbol": args.symbol}))
        return 1
    payload = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "symbol": args.symbol.upper(),
        "timeframe": args.timeframe,
        "bars": bars,
    }
    _STATE_DIR.mkdir(parents=True, exist_ok=True)
    out_file = _STATE_DIR / f"candles_{args.symbol.upper()}_{args.timeframe}.json"
    tmp = out_file.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload))
    tmp.replace(out_file)
    last = bars[-1]
    print(json.dumps({
        "wrote": str(out_file.name),
        "bars": len(bars),
        "last_close": last.get("close"),
        "last_time": last.get("time"),
    }, indent=2))
    return 0


def cmd_snapshot(args: argparse.Namespace) -> int:
    """Write account_snapshot.json for the trading-bot dashboard to read.

    Hits Alpaca once for cash/BP/portfolio_value, then writes an atomic JSON
    file. The dashboard reads this file (no Alpaca call) and uses the `ts`
    field to display "Xm ago" so a stale snapshot is visually obvious.
    """
    broker = from_env()
    acct = broker.get_account()
    pnl = cumulative_pnl()
    payload = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "cash": round(acct.cash, 2),
        "buying_power": round(acct.buying_power, 2),
        "portfolio_value": round(acct.portfolio_value, 2),
        "paper": acct.paper,
        "wheel_cumulative_pnl": round(pnl, 2),
        "wheel_open_positions": len(load_positions()),
    }
    _STATE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = _ACCOUNT_SNAPSHOT_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, indent=2))
    tmp.replace(_ACCOUNT_SNAPSHOT_FILE)
    print(json.dumps(payload, indent=2))
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
    sub.add_parser("snapshot", help="Write account_snapshot.json for dashboard")

    p_candles = sub.add_parser("candles", help="Write Alpaca bars JSON for the dashboard chart")
    p_candles.add_argument("symbol", help="ticker like SOFI, AAPL")
    p_candles.add_argument("--timeframe", default="5Min",
                           help="1Min,5Min,15Min,1Hour,1Day (default 5Min)")
    p_candles.add_argument("--limit", type=int, default=288,
                           help="number of bars (default 288 = 24h of 5-min)")

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
        "snapshot": cmd_snapshot,
        "candles": cmd_candles,
        "cancel-stale": cmd_cancel_stale,
        "kill-ticker": cmd_kill_ticker,
    }
    return handlers[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
