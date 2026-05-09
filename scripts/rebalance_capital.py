#!/usr/bin/env python3
"""
Auto-rebalance the per-pair capital allocation in config.json based on
each pair's rolling 14-day Sharpe ratio (live paper-trading P&L).

Workflow per run:
  1. Read config.json[capital_allocation] for the existing weights and the
     min_sharpe_for_trading floor.
  2. Compute each pair's annualised 14-day Sharpe from trade_journal.
  3. Build new weights:
       - pairs below the floor get weight 0 (data-only)
       - the rest get weight ∝ max(0, sharpe), normalised to sum to 1.0
       - per-pair cap of MAX_WEIGHT (default 0.50) so no single pair eats
         more than half the book; renormalise after the cap.
       - per-pair floor of MIN_WEIGHT (default 0.05) for any pair that's
         still tradeable, so a borderline-strong pair doesn't starve.
  4. Snapshot the existing config.json to user_data/data/config-backup-<ts>.json.
  5. Atomic-write (tmp + rename) the new pair_weights.
  6. POST a Slack summary if SLACK_WEBHOOK_URL is set.

Designed to be safe to run on a 14-day cadence (Hermes cron) AND idempotent
on shorter cadences — if the change set is empty we no-op without writing.

Usage:
    python scripts/rebalance_capital.py            # apply
    python scripts/rebalance_capital.py --dry-run  # print proposed change, no write
    python scripts/rebalance_capital.py --window 14 --max-weight 0.5 --min-weight 0.05
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote_plus

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "user_data" / "config.json"
BACKUP_DIR = ROOT / "user_data" / "data"


# --------------------------------------------------------------------------
# DSN — same precedence as user_data/modules/db.py
# --------------------------------------------------------------------------


def _dsn() -> str:
    explicit = os.environ.get("DATABASE_URL", "").strip()
    if explicit:
        return explicit
    user = os.environ.get("POSTGRES_USER", "tradebot")
    password = os.environ.get("POSTGRES_PASSWORD", "")
    if not password:
        raise RuntimeError(
            "POSTGRES_PASSWORD env var required. Source ~/Documents/trading-bot/.env first."
        )
    host = os.environ.get("POSTGRES_HOST", "localhost")
    port = os.environ.get("POSTGRES_PORT", "5434")
    db = os.environ.get("POSTGRES_DB", "tradebot")
    return f"postgresql://{quote_plus(user)}:{quote_plus(password)}@{host}:{port}/{db}"


# --------------------------------------------------------------------------
# Rolling Sharpe per pair
# --------------------------------------------------------------------------


def _rolling_sharpe_per_pair(window_days: int) -> dict[str, float]:
    """Annualised Sharpe (sqrt(365)) on daily-summed P&L pct, per pair."""
    import psycopg
    from psycopg.rows import dict_row

    sql = """
        SELECT pair, closed_at, pnl_pct
        FROM trade_journal
        WHERE closed_at IS NOT NULL
          AND closed_at > NOW() - (%s || ' days')::interval
        ORDER BY pair, closed_at
    """
    with psycopg.connect(_dsn(), row_factory=dict_row) as conn, conn.cursor() as cur:
        cur.execute(sql, (str(window_days),))
        rows = cur.fetchall()

    daily_by_pair: dict[str, dict[str, float]] = {}
    for r in rows:
        pair = r["pair"]
        day = r["closed_at"].strftime("%Y-%m-%d")
        d = daily_by_pair.setdefault(pair, {})
        d[day] = d.get(day, 0.0) + float(r["pnl_pct"] or 0.0)

    out: dict[str, float] = {}
    for pair, daily in daily_by_pair.items():
        pcts = list(daily.values())
        if len(pcts) < 2:
            continue
        mean = sum(pcts) / len(pcts)
        var = sum((x - mean) ** 2 for x in pcts) / (len(pcts) - 1)
        sd = math.sqrt(var)
        if sd <= 0:
            continue
        out[pair] = (mean / sd) * math.sqrt(365)
    return out


# --------------------------------------------------------------------------
# Weight builder
# --------------------------------------------------------------------------


def compute_new_weights(
    *,
    sharpes: dict[str, float],
    current_weights: dict[str, float],
    min_sharpe_for_trading: float,
    max_weight: float,
    min_weight: float,
) -> dict[str, float]:
    """Return new pair → weight (0..1) keyed by all pairs in current_weights.

    Rules:
      - sharpe < min_sharpe_for_trading → weight 0 (data-only)
      - missing-from-sharpes (no live data yet) → keep current weight as-is
        so a fresh-start rebalance doesn't accidentally zero everyone
      - eligible pairs share weight ∝ max(0, sharpe), normalised to 1
      - apply per-pair caps then re-normalise so the total still sums to ≤1
    """
    new_weights: dict[str, float] = {}

    # Eligible = has live Sharpe ≥ floor
    eligible = {
        p: max(0.0, s)
        for p, s in sharpes.items()
        if s >= min_sharpe_for_trading and p in current_weights
    }

    if not eligible:
        # No live data yet → keep current allocation untouched.
        return dict(current_weights)

    total_sharpe = sum(eligible.values())
    if total_sharpe <= 0:
        # All-zero edge case → equal-weight the eligible pairs.
        equal = 1.0 / len(eligible)
        eligible = {p: equal for p in eligible}
        total_sharpe = 1.0

    # First pass: weight ∝ sharpe
    for p in current_weights:
        new_weights[p] = (eligible[p] / total_sharpe) if p in eligible else 0.0

    # Apply per-pair max cap, re-normalise the rest.
    capped = {p: min(w, max_weight) for p, w in new_weights.items()}
    overflow = 1.0 - sum(capped.values())
    if overflow > 0.001:
        # Distribute overflow proportionally to the uncapped pairs.
        uncapped = {p: w for p, w in capped.items() if w < max_weight and p in eligible}
        if uncapped:
            uc_total = sum(uncapped.values())
            for p in uncapped:
                bonus = overflow * (uncapped[p] / uc_total) if uc_total > 0 else 0
                capped[p] = min(max_weight, capped[p] + bonus)

    # Apply per-pair min floor for tradeable pairs (avoid 1% allocations).
    for p in eligible:
        if 0 < capped[p] < min_weight:
            capped[p] = min_weight

    # Final normalisation so weights sum to ≤ 1.0 (slight over due to floor).
    total = sum(capped.values())
    if total > 1.0:
        capped = {p: w / total for p, w in capped.items()}

    return {p: round(w, 4) for p, w in capped.items()}


# --------------------------------------------------------------------------
# Apply
# --------------------------------------------------------------------------


def _slack_post(text: str) -> None:
    url = os.environ.get("SLACK_WEBHOOK_URL", "").strip()
    if not url:
        return
    try:
        req = urllib.request.Request(
            url, data=json.dumps({"text": text}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        urllib.request.urlopen(req, timeout=5)
    except Exception:
        pass


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--window", type=int, default=14,
                   help="Rolling Sharpe window in days (default 14)")
    p.add_argument("--max-weight", type=float, default=0.50,
                   help="Per-pair cap (default 0.50)")
    p.add_argument("--min-weight", type=float, default=0.05,
                   help="Per-pair floor for tradeable pairs (default 0.05)")
    p.add_argument("--dry-run", action="store_true",
                   help="Compute and print only — don't write config.json")
    args = p.parse_args()

    cfg_text = CONFIG_PATH.read_text()
    cfg = json.loads(cfg_text)
    alloc = cfg.get("capital_allocation") or {}
    current = dict(alloc.get("pair_weights") or {})
    if not current:
        print("No capital_allocation.pair_weights in config.json — nothing to rebalance.")
        return 1
    floor = float(alloc.get("min_sharpe_for_trading", 0.0))

    sharpes = _rolling_sharpe_per_pair(args.window)
    new = compute_new_weights(
        sharpes=sharpes,
        current_weights=current,
        min_sharpe_for_trading=floor,
        max_weight=args.max_weight,
        min_weight=args.min_weight,
    )

    diffs = []
    for p in sorted(set(current) | set(new)):
        old, n = current.get(p, 0.0), new.get(p, 0.0)
        if abs(old - n) > 1e-4:
            diffs.append(f"{p}: {old:.2%} → {n:.2%}  (sharpe={sharpes.get(p, '—')})")

    print(f"=== rebalance_capital · window={args.window}d · floor={floor} ===")
    print(f"  {len(sharpes)} pairs with live Sharpe; {len(diffs)} weight changes")
    for d in diffs:
        print(f"    {d}")
    if not diffs:
        print("  (no changes — current weights are stable)")
        return 0

    if args.dry_run:
        print("\n[--dry-run] not writing config.json")
        return 0

    # Snapshot + atomic-write.
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%SZ")
    backup = BACKUP_DIR / f"config-backup-{stamp}-rebalance.json"
    backup.write_text(cfg_text)
    cfg["capital_allocation"]["pair_weights"] = new
    tmp = CONFIG_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(cfg, indent=4))
    tmp.replace(CONFIG_PATH)
    print(f"  ✓ config.json updated · backup at {backup}")

    # Slack summary so the operator sees the rebalance even if asleep.
    body_lines = [
        f":bar_chart: *Capital rebalance* — window={args.window}d, floor={floor}",
        "Sharpe (live, last %dd):" % args.window,
    ]
    for p in sorted(current):
        s = sharpes.get(p)
        s_str = f"{s:+.2f}" if s is not None else "—"
        body_lines.append(f"  • {p}: sharpe={s_str}, "
                          f"weight {current[p]:.2%} → {new.get(p, 0):.2%}")
    body_lines.append("\nFreqtrade will pick up the new weights on the next "
                      "bot_loop_start refresh (no restart needed).")
    _slack_post("\n".join(body_lines))
    return 0


if __name__ == "__main__":
    sys.exit(main())
