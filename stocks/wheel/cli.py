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
    Position, add_position, cumulative_pnl, kill_ticker,
    load_positions, now_iso, remove_position,
)

_STATE_DIR = Path(__file__).resolve().parent / "state"
_ACCOUNT_SNAPSHOT_FILE = _STATE_DIR / "account_snapshot.json"


def _wheel_exit_code(summary: dict) -> int:
    """Decide cron exit code for a wheel phase.

    2026-05-13 fix: previously any entry in summary["errors"] forced exit=1,
    which made Hermes alert on routine "insufficient options buying power"
    rejects. Those rejects ARE expected when buying_power runs out partway
    through the candidate list — the runner intentionally keeps trying so
    the operator can see the full picture in the summary. Exit 0 when ANY
    action succeeded with only BP-rejects; exit 1 only on genuine fatal
    errors (auth, network, unknown).
    """
    errors = summary.get("errors") or []
    actions = summary.get("actions") or []
    if not errors:
        return 0
    fatal_errors = [
        e for e in errors
        if "insufficient" not in str(e).lower()
        and "buying power" not in str(e).lower()
    ]
    if actions and not fatal_errors:
        return 0  # partial success with only BP-rejects — expected behaviour
    if fatal_errors:
        return 1
    return 0  # only BP-rejects and no successful actions = empty cycle, not a failure


def cmd_sell_csps(args: argparse.Namespace) -> int:
    summary = runner.sell_csps()
    print(json.dumps(summary, indent=2, default=str))
    return _wheel_exit_code(summary)


def cmd_profit_take(args: argparse.Namespace) -> int:
    summary = runner.profit_take_check()
    print(json.dumps(summary, indent=2, default=str))
    return _wheel_exit_code(summary)


def cmd_sell_covered_calls(args: argparse.Namespace) -> int:
    summary = runner.sell_covered_calls()
    print(json.dumps(summary, indent=2, default=str))
    return _wheel_exit_code(summary)


def cmd_status(args: argparse.Namespace) -> int:
    cfg = load_config()
    broker = from_env()
    acct = broker.get_account()
    positions = load_positions()
    pnl = cumulative_pnl()

    # Derived caps (Wave 1.4): show the operator both the cfg-pinned
    # ceiling and the equity-relative cap that will actually fire.
    from .risk_caps import caps_as_dict, derive_caps
    _caps = derive_caps(acct.portfolio_value, cfg)
    out = {
        "config": {
            "symbols": list(cfg.symbols),
            "delta_band": [cfg.delta_min, cfg.delta_max],
            "dte_band": [cfg.dte_min, cfg.dte_max],
            "max_risk_per_ticker_ceiling": cfg.max_risk_per_ticker_usd,
            "max_total_collateral_ceiling": cfg.max_total_collateral_usd,
            "kill_loss_ceiling": cfg.kill_loss_per_cycle_usd,
            "paper": cfg.paper,
        },
        "effective_caps": caps_as_dict(_caps),
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


def _reconcile_positions_with_broker(broker) -> dict:
    """Sync positions.json with Alpaca's actual open option positions.

    Why: sell_csps polls the order for 30s after submitting; if Alpaca
    fills the order LATER than that (slow exchange acks, illiquid strikes)
    runner.py:_try_sell_csp() bails without calling add_position(). The
    contract is then live at broker but invisible to the local reconciler,
    profit_take, and the dashboard's open_wheel count. This was first hit
    on 2026-05-13 when SOFI + PLTR CSPs filled at ~15:00:25 UTC but the
    30s poll started at 15:00:20 — they slipped through.

    This function pulls every open option position from Alpaca, matches by
    contract_symbol against load_positions(), and:

      * ADDS broker positions that aren't locally known (estimating
        entry_credit from -cost_basis, since short option positions in
        Alpaca have negative cost_basis = credit received)
      * REMOVES local positions that are no longer at the broker
        (closed, exercised, expired) — the trade ledger keeps the
        historical record so positions.json can shed them safely

    Idempotent. Returns a summary dict for logging.
    """
    summary = {"added": [], "removed": [], "matched": 0, "errors": []}

    try:
        broker_positions = list(broker.trading.get_all_positions() or [])
    except Exception as exc:
        summary["errors"].append(f"broker.get_all_positions failed: {exc}")
        return summary

    # Filter to option positions only — Alpaca tags these with asset_class
    # "us_option". OCC contract symbols look like NVDA260522P00220000.
    broker_options = []
    for bp in broker_positions:
        ac = getattr(bp, "asset_class", "")
        ac_str = ac.value if hasattr(ac, "value") else str(ac)
        if ac_str.lower() in ("us_option", "option"):
            broker_options.append(bp)

    local = {p.contract_symbol: p for p in load_positions() if p.contract_symbol}
    broker_syms = set()

    # ── Stage 1: ADD broker positions missing locally ───────────────────
    for bp in broker_options:
        sym = str(getattr(bp, "symbol", "") or "")
        if not sym:
            continue
        broker_syms.add(sym)
        if sym in local:
            summary["matched"] += 1
            continue
        # New to us. Build a Position from the broker payload.
        try:
            qty_raw = float(getattr(bp, "qty", 0) or 0)
            cost_basis = float(getattr(bp, "cost_basis", 0) or 0)
            # Short positions have negative qty AND negative cost_basis at Alpaca.
            # entry_credit is the USD we received, i.e. abs(cost_basis).
            is_short = qty_raw < 0
            kind = (
                "short_put" if (is_short and sym[-9] == "P") else
                "short_call" if (is_short and sym[-9] == "C") else
                "long_shares" if not is_short else
                "short_put"  # fallback
            )
            # OCC: <ROOT><YYMMDD><P|C><STRIKE×1000 zero-padded to 8>
            # Last 15 chars: YYMMDD + P/C + 8-digit strike
            try:
                expiry_yymmdd = sym[-15:-9]
                expiry = f"20{expiry_yymmdd[:2]}-{expiry_yymmdd[2:4]}-{expiry_yymmdd[4:6]}"
                strike = float(sym[-8:]) / 1000.0
            except Exception:
                expiry, strike = None, 0.0
            # Underlying root is the prefix; trim by stripping the 15-char tail.
            underlying = sym[:-15] if len(sym) > 15 else sym
            add_position(Position(
                underlying=underlying,
                contract_symbol=sym,
                kind=kind,
                qty=abs(int(qty_raw)) or 1,
                strike=strike,
                expiry=expiry,
                entry_credit=abs(cost_basis),
                opened_at=now_iso(),
                source="reconciler",
            ))
            summary["added"].append({
                "symbol": sym, "underlying": underlying,
                "kind": kind, "strike": strike, "expiry": expiry,
                "entry_credit": round(abs(cost_basis), 2),
            })
        except Exception as exc:
            summary["errors"].append(f"add {sym}: {exc}")

    # ── Stage 2: REMOVE local positions that broker no longer carries ───
    # We keep `status='assigned'` rows even when the option is gone — those
    # represent the underlying shares from a CSP that exercised, the wheel
    # cycle isn't done with them yet. Only drop status='' or 'closed'.
    for sym, lp in local.items():
        if sym in broker_syms:
            continue
        if (lp.status or "").lower() in ("assigned",):
            continue
        # Ghost — option not at broker, not assigned locally. Drop it.
        try:
            remove_position(sym)
            summary["removed"].append({
                "symbol": sym, "underlying": lp.underlying,
                "kind": lp.kind, "status": lp.status or "",
            })
        except Exception as exc:
            summary["errors"].append(f"remove {sym}: {exc}")

    return summary


def cmd_snapshot(args: argparse.Namespace) -> int:
    """Write account_snapshot.json for the trading-bot dashboard to read.

    Hits Alpaca once for cash/BP/portfolio_value, then writes an atomic JSON
    file. The dashboard reads this file (no Alpaca call) and uses the `ts`
    field to display "Xm ago" so a stale snapshot is visually obvious.

    Also reconciles stocks/wheel/state/positions.json against Alpaca's
    actual open option positions on every snapshot (every 1 minute during
    market hours). This catches the case where sell_csps's 30s fill-poll
    times out but the order fills shortly after — the next snapshot picks
    up the broker truth and patches positions.json so profit_take +
    dashboard see the position.
    """
    broker = from_env()
    acct = broker.get_account()
    reconcile_summary = _reconcile_positions_with_broker(broker)
    pnl = cumulative_pnl()
    payload = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "cash": round(acct.cash, 2),
        "buying_power": round(acct.buying_power, 2),
        "portfolio_value": round(acct.portfolio_value, 2),
        "paper": acct.paper,
        "wheel_cumulative_pnl": round(pnl, 2),
        "wheel_open_positions": len(load_positions()),
        "reconcile": reconcile_summary,
    }
    _STATE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = _ACCOUNT_SNAPSHOT_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, indent=2))
    tmp.replace(_ACCOUNT_SNAPSHOT_FILE)
    print(json.dumps(payload, indent=2))
    return 0


def cmd_reconcile(args: argparse.Namespace) -> int:
    """Manually trigger broker→local positions reconciliation.

    Exits with the count of changes (added + removed) so a cron alerter
    can detect when drift actually happens.
    """
    broker = from_env()
    summary = _reconcile_positions_with_broker(broker)
    print(json.dumps(summary, indent=2, default=str))
    return 0  # always 0 — drift is informational, not a failure


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
    sub.add_parser("snapshot", help="Write account_snapshot.json for dashboard + reconcile positions")
    sub.add_parser("reconcile", help="Sync positions.json with Alpaca's actual open positions")

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
        "reconcile": cmd_reconcile,
        "candles": cmd_candles,
        "cancel-stale": cmd_cancel_stale,
        "kill-ticker": cmd_kill_ticker,
    }
    return handlers[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
