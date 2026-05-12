#!/usr/bin/env python3
"""Parse a freqtrade NFI X6 backtest result and emit gate-3 PASS/FAIL summary.

Usage:
    python3 scripts/nfi_x6_parse_backtest.py path/to/result.json [--json]

Inputs:
    The freqtrade `--export trades --export-filename FILE` writes:
        FILE                                # the trades CSV/JSON the strategy emitted
        FILE                                # plus a sibling .meta.json
        Behind the scenes freqtrade also writes the *full* backtest result
        as a zip in the same backtest_results/ dir
        (backtest-result-YYYY-MM-DD_HH-MM-SS.zip). The zip is what we want.

    To make this script ergonomic we accept either:
      - the *.zip  (preferred)
      - the unzipped *.json (the full strategy result)
      - a directory containing the latest backtest result (we'll pick most recent zip)

Thresholds (from docs/NFI_X6_ACTIVATION_2026-05-11.md §3 gate 3):
    Sharpe          > 1.4
    Max drawdown    < 12 %  (account_pct, so 0.12)
    Profit factor   > 1.4
    Win rate        > 38 %
    Trades / month  > 30
    Total trades    > 30  (sanity gate)
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import zipfile
from pathlib import Path
from typing import Any


THRESHOLDS = {
    "sharpe":             1.4,
    "max_drawdown_pct":  12.0,   # max — we fail if exceeded
    "profit_factor":      1.4,
    "winrate_pct":       38.0,
    "trades_per_month":  30.0,
    "min_trades":        30,
}


def _find_zip(p: Path) -> Path:
    if p.is_file():
        if p.suffix == ".zip":
            return p
        if p.suffix == ".json":
            return p
    if p.is_dir():
        zips = sorted(p.glob("backtest-result-*.zip"), key=lambda x: x.stat().st_mtime)
        if zips:
            return zips[-1]
    raise SystemExit(f"no backtest result zip/json found at {p}")


def _load_strategy_block(p: Path) -> dict[str, Any]:
    if p.suffix == ".json":
        d = json.loads(p.read_text())
    elif p.suffix == ".zip":
        with zipfile.ZipFile(p) as zf:
            json_names = [n for n in zf.namelist() if n.endswith(".json") and "config" not in n]
            if not json_names:
                raise SystemExit(f"{p} contains no result JSON")
            json_names.sort(key=lambda n: -len(n))
            with zf.open(json_names[0]) as fh:
                d = json.load(fh)
    else:
        raise SystemExit(f"unsupported result file: {p}")
    strat = d.get("strategy") or {}
    if not strat:
        raise SystemExit("no 'strategy' block in result JSON")
    name, block = next(iter(strat.items()))
    block["_strategy_name"] = name
    return block


def _percent(x: float) -> float:
    """Return x as a percentage. freqtrade reports drawdown both as fraction
    (0.12 = 12%) and as account_pct (already a percent)."""
    if abs(x) <= 1.0:
        return x * 100.0
    return x


def evaluate(block: dict) -> dict:
    days = float(block.get("backtest_days") or 0) or 1.0
    months = days / 30.4375
    total_trades = int(block.get("total_trades") or 0)

    # ── Prefer wallet-based daily metrics for risk gates ──────────────────
    # The closed-trades Sharpe/Sortino degrade when the strategy is a long
    # holder with rare big wins (NFI X6's profile — see Sortino=-100 in the
    # closed-trades view). Wallet-based daily series matches the
    # "annualized from daily returns" definition the task spec calls out.
    ws = block.get("wallet_stats") or {}
    sharpe_closed = float(block.get("sharpe") or 0.0)
    sharpe_wallet = float(ws.get("sharpe") or 0.0)
    sharpe = sharpe_wallet if sharpe_wallet else sharpe_closed

    sortino_closed = float(block.get("sortino") or 0.0)
    sortino_wallet = float(ws.get("sortino") or 0.0)
    sortino = sortino_wallet if sortino_wallet else sortino_closed

    calmar_closed = float(block.get("calmar") or 0.0)
    calmar_wallet = float(ws.get("calmar") or 0.0)
    calmar = calmar_wallet if calmar_wallet else calmar_closed

    # Wallet-based drawdown captures unrealized peaks; the closed-trades
    # version understates risk for long-hold strategies like NFI X6.
    max_dd_account_closed = _percent(float(block.get("max_drawdown_account") or 0.0))
    max_dd_account_wallet = _percent(float(ws.get("max_drawdown_account") or 0.0))
    max_dd_pct = max_dd_account_wallet if max_dd_account_wallet else max_dd_account_closed

    profit_factor = float(block.get("profit_factor") or 0.0)
    winrate_raw = float(block.get("winrate") or 0.0)
    winrate_pct = winrate_raw * 100.0 if winrate_raw <= 1.0 else winrate_raw
    trades_per_month = total_trades / months if months > 0 else 0.0
    cagr = _percent(float(block.get("cagr") or 0.0))
    profit_total_abs = float(block.get("profit_total_abs") or 0.0)
    profit_total_pct = _percent(float(block.get("profit_total") or 0.0))
    max_loss_streak = int(block.get("max_consecutive_losses") or 0)
    max_win_streak = int(block.get("max_consecutive_wins") or 0)
    expectancy = float(block.get("expectancy") or 0.0)
    sqn = float(block.get("sqn") or 0.0)
    starting_balance = float(block.get("starting_balance") or 0)
    final_balance = float(block.get("final_balance") or 0)

    # Task-spec gates (these decide activation) — 4 quality + 1 sanity.
    task_gates = {
        "Sharpe > 1.4":          (sharpe, THRESHOLDS["sharpe"], "gt", sharpe > THRESHOLDS["sharpe"]),
        "Max DD < 12%":          (max_dd_pct, THRESHOLDS["max_drawdown_pct"], "lt", max_dd_pct < THRESHOLDS["max_drawdown_pct"]),
        "Profit factor > 1.4":   (profit_factor, THRESHOLDS["profit_factor"], "gt", profit_factor > THRESHOLDS["profit_factor"]),
        "Min 30 trades":         (total_trades, THRESHOLDS["min_trades"], "gte", total_trades >= THRESHOLDS["min_trades"]),
    }
    task_pass = all(g[3] for g in task_gates.values())

    # Runbook-only extras — informational; NOT used for the activation decision.
    runbook_extras = {
        "Win rate > 38%":        (winrate_pct, THRESHOLDS["winrate_pct"], "gt", winrate_pct > THRESHOLDS["winrate_pct"]),
        "Trades/month > 30":     (trades_per_month, THRESHOLDS["trades_per_month"], "gt", trades_per_month > THRESHOLDS["trades_per_month"]),
    }

    # Backwards-compat alias used by the report renderer.
    gates = {**task_gates, **runbook_extras}
    overall_pass = task_pass

    # monthly distribution from periodic_breakdown if present
    monthly = []
    pb = block.get("periodic_breakdown") or {}
    for row in (pb.get("month") or []):
        wins = int(row.get("wins") or 0)
        losses = int(row.get("losses") or 0)
        trades = int(row.get("trades") or row.get("trade_count") or (wins + losses))
        wr = (wins / trades * 100) if trades else 0.0
        monthly.append({
            "date":    row.get("date"),
            "trades":  trades,
            "wins":    wins,
            "losses":  losses,
            "profit":  row.get("profit_abs", 0),
            "winrate": wr,
        })

    return {
        "strategy":         block.get("_strategy_name"),
        "timerange":        block.get("timerange"),
        "backtest_days":    int(days),
        "starting_balance": starting_balance,
        "final_balance":    final_balance,
        "profit_total_abs": profit_total_abs,
        "profit_total_pct": profit_total_pct,
        "metrics": {
            "sharpe":           sharpe,
            "sharpe_closed":    sharpe_closed,
            "sortino":          sortino,
            "sortino_closed":   sortino_closed,
            "calmar":           calmar,
            "cagr_pct":         cagr,
            "profit_factor":    profit_factor,
            "max_dd_pct":              max_dd_pct,
            "max_dd_account_closed":   max_dd_account_closed,
            "winrate_pct":      winrate_pct,
            "total_trades":     total_trades,
            "trades_per_month": trades_per_month,
            "expectancy":       expectancy,
            "sqn":              sqn,
            "max_loss_streak":  max_loss_streak,
            "max_win_streak":   max_win_streak,
        },
        "task_gates":     task_gates,
        "runbook_extras": runbook_extras,
        "gates":          gates,
        "task_pass":      task_pass,
        "pass":           overall_pass,
        "monthly":        monthly,
    }


def render_text(r: dict) -> str:
    lines = []
    lines.append(f"# NFI X6 Backtest — Gate 3 evaluation")
    lines.append("")
    lines.append(f"Strategy:       {r['strategy']}")
    lines.append(f"Timerange:      {r['timerange']}  ({r['backtest_days']} days)")
    lines.append(f"Wallet:         ${r['starting_balance']:,.0f} → ${r['final_balance']:,.2f}  "
                 f"({r['profit_total_pct']:+.2f}%, ${r['profit_total_abs']:+,.2f})")
    lines.append("")
    lines.append("## Task-spec activation gates (decide activation)")
    lines.append("")
    lines.append("| Gate | Threshold | Measured | Pass? |")
    lines.append("|---|---|---|---|")
    for name, (measured, threshold, op, ok) in r["task_gates"].items():
        sym = "PASS" if ok else "FAIL"
        op_sym = {"gt": ">", "lt": "<", "gte": ">="}[op]
        lines.append(f"| {name} | {op_sym} {threshold} | {measured:.3f} | **{sym}** |")
    lines.append("")
    lines.append(f"**Overall: {'PASS' if r['task_pass'] else 'FAIL'}**")
    lines.append("")
    lines.append("## Runbook extras (informational, NOT a gate)")
    lines.append("")
    lines.append("Runbook §3 lists Win-rate >38% and Trades/month >30 alongside the")
    lines.append("quality thresholds. The task spec narrows the activation decision to")
    lines.append("Sharpe / DD / PF / min-trades; these extras are reported for context.")
    lines.append("")
    lines.append("| Extra | Threshold | Measured | Pass? |")
    lines.append("|---|---|---|---|")
    for name, (measured, threshold, op, ok) in r["runbook_extras"].items():
        sym = "PASS" if ok else "FAIL"
        op_sym = {"gt": ">", "lt": "<", "gte": ">="}[op]
        lines.append(f"| {name} | {op_sym} {threshold} | {measured:.3f} | {sym} |")
    lines.append("")
    m = r["metrics"]
    lines.append("## Other diagnostics")
    lines.append("")
    lines.append(f"- Sortino:               {m['sortino']:.3f}")
    lines.append(f"- Calmar:                {m['calmar']:.3f}")
    lines.append(f"- CAGR:                  {m['cagr_pct']:.2f}%")
    lines.append(f"- Expectancy:            {m['expectancy']:.4f}")
    lines.append(f"- SQN:                   {m['sqn']:.3f}")
    lines.append(f"- Longest losing streak: {m['max_loss_streak']}")
    lines.append(f"- Longest winning streak: {m['max_win_streak']}")
    if r["monthly"]:
        lines.append("")
        lines.append("## Monthly returns")
        lines.append("")
        lines.append("| Month | Trades | Profit ($) | Winrate (%) |")
        lines.append("|---|---|---|---|")
        for row in r["monthly"]:
            lines.append(f"| {row['date']} | {row['trades']} | {row['profit']:+,.2f} | {row['winrate']:.1f} |")
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("path", help="zip / json / dir")
    ap.add_argument("--json", action="store_true", help="emit JSON instead of markdown")
    args = ap.parse_args()

    p = _find_zip(Path(args.path))
    block = _load_strategy_block(p)
    r = evaluate(block)
    if args.json:
        print(json.dumps(r, indent=2, default=str))
    else:
        print(render_text(r))
    return 0 if r["pass"] else 2  # exit 2 on FAIL so a caller can branch


if __name__ == "__main__":
    sys.exit(main())
