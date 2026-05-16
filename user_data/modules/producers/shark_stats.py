"""producers/shark_stats.py - B2 root-cause fix + idempotent backfill.

B2 ROOT-CAUSE FINDING (2026-05-16, this audit)
==============================================

Symptom (/api/ops/stocks.shark.stats):
    {"total_trades": 5, "wins": 0, "losses": 0, "win_rate": 0.0,
     "total_pnl": 0.0, "current_drawdown_pct": 0.29}

1. Read ~/Documents/.dgx-train/shark/memory/cron-shark-daily_summary.log:
   daily-summary runs are HEALTHY post `flock` guard (F3, commit 8296e03).
   Every daily-summary 2026-05-11 through 2026-05-15 wrote successfully
   (with one ff-only merge failure on 2026-05-13 that re-ran cleanly).
   The total_trades / wins / losses fields are NOT populated by the
   cron itself; they're recomputed by shark/dashboard/generate.py
   :_compute_stats from disk every time the dashboard data.json
   refreshes.

2. Read shark/dashboard/generate.py:_compute_stats (line 168):
      pnl = float(t.get("realized_pnl", t.get("pnl", 0.0)))
      if pnl > 0: stats["wins"] += 1
      elif pnl < 0: stats["losses"] += 1

3. Read stocks/kb/trades/*.json (the 5 trade files producing total=5):
      {"symbol":"SOFI260522P00015500", "pnl_pct":-45.714,
       "exit_reason":"stop-out", ... NO realized_pnl, NO pnl ...}

CONCLUSION: B2 is a HISTORICAL SCHEMA MISMATCH. The kb/trades/ files
store pnl_pct (percentage) but _compute_stats only looks for absolute
realized_pnl / pnl. Every record falls through the .get(... 0.0)
default -> pnl == 0.0 -> fails both > 0 and < 0 -> wins=0, losses=0,
total_pnl=0.0, even though total_trades=5 (which uses len() and IS
correct). The win_rate computes from wins(0) / total(5) -> 0.0.

This is pre-flock historical data; those 5 trades were closed on
2026-05-12 (regime BEAR_VOLATILE, stop-out). The cron is healthy; the
data schema is the bug.

FIX SHAPE (per spec §6 B2 + functional-debate G4):
  - Ship an additive idempotent backfill that recomputes wins/losses
    using pnl_pct as a sign-only classifier when absolute pnl is absent.
  - Write to a NEW file ~/Documents/.dgx-train/shark/memory/
    shark-stats-rebuilt.json. NEVER overwrite the existing dashboard
    data.json or any kb/trades/*.json (spec §5.4 - preserved roots).
  - The dashboard /api/v5/strategies/shark consumes the rebuilt file
    when present, falls back to the legacy stats block when absent.

Pre-flock memory notes freqtrade_decommissioned + frontend_audit_and_
12_bug_fixes_2026_05_14 already disarmed the upstream cron-double-fire.
This producer is the read-side cleanup.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# READ-ONLY roots — never write under these paths (spec §5.4).
_REPO_ROOT = Path(__file__).resolve().parents[3]
_SHARK_DATA_PATH = _REPO_ROOT / "stocks" / "docs" / "dashboard" / "data.json"
_KB_TRADES_DIR = _REPO_ROOT / "stocks" / "kb" / "trades"

# WRITE root — a NEW file, sibling to the existing journal logs. Mode
# "w" on this specific file is the EXPECTED_WRITE_TRUNCATE per spec §5.4
# (the file is the rebuilt-stats artifact, owned by this producer).
# We never touch any existing file under shark/memory/.
_SHARK_MEMORY_DIR = Path(os.environ.get(
    "SHARK_MEMORY_DIR",
    str(Path.home() / "Documents/.dgx-train/shark/memory"),
))
_REBUILT_STATS_PATH = _SHARK_MEMORY_DIR / "shark-stats-rebuilt.json"


def _safe_read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text())
    except (FileNotFoundError, json.JSONDecodeError, OSError) as exc:
        logger.debug("read %s failed: %s", path, exc)
        return None


def _load_kb_trades() -> list[dict[str, Any]]:
    if not _KB_TRADES_DIR.is_dir():
        return []
    out: list[dict[str, Any]] = []
    for f in sorted(_KB_TRADES_DIR.glob("*.json"), reverse=True):
        rec = _safe_read_json(f)
        if isinstance(rec, dict):
            rec.setdefault("_file", f.name)
            out.append(rec)
    return out


def recompute_stats(trades: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """Recompute wins/losses/total_pnl/win_rate using a schema-tolerant
    classifier:

      1. If ``realized_pnl`` or ``pnl`` or ``pnl_usd`` is present, use that
         absolute number — sums into `total_pnl`.
      2. If only ``pnl_pct`` is present, use it for SIGN classification
         only (wins/losses) and surface the count under
         ``pct_only_classified`` so the operator sees the data-schema gap.

    Returns a dict matching the legacy `stats` block schema PLUS new
    forensic fields (`pct_only_classified`, `missing_pnl_count`).
    """
    rows = trades if trades is not None else _load_kb_trades()
    total = len(rows)
    wins = 0
    losses = 0
    scratches = 0
    pct_only = 0
    missing = 0
    pnls_abs: list[float] = []
    for t in rows:
        # Absolute pnl, if any
        abs_pnl: float | None = None
        for k in ("realized_pnl", "pnl", "pnl_usd"):
            v = t.get(k)
            if v is None:
                continue
            try:
                abs_pnl = float(v)
                break
            except (TypeError, ValueError):
                continue
        if abs_pnl is not None:
            pnls_abs.append(abs_pnl)
            if abs_pnl > 0:
                wins += 1
            elif abs_pnl < 0:
                losses += 1
            else:
                scratches += 1
            continue
        # No absolute — try pnl_pct for sign-only classification
        pct = t.get("pnl_pct")
        if pct is None:
            missing += 1
            continue
        try:
            pct_f = float(pct)
        except (TypeError, ValueError):
            missing += 1
            continue
        pct_only += 1
        if pct_f > 0:
            wins += 1
        elif pct_f < 0:
            losses += 1
        else:
            scratches += 1

    classified = wins + losses + scratches
    out: dict[str, Any] = {
        "total_trades": total,
        "wins": wins,
        "losses": losses,
        "scratches": scratches,
        "missing_pnl_count": missing,
        "pct_only_classified": pct_only,
        "win_rate": round(wins / classified * 100, 2) if classified > 0 else 0.0,
        "total_pnl": round(sum(pnls_abs), 2) if pnls_abs else 0.0,
        "best_trade": round(max(pnls_abs), 2) if pnls_abs else 0.0,
        "worst_trade": round(min(pnls_abs), 2) if pnls_abs else 0.0,
        "schema_health": "ok" if pct_only == 0 and missing == 0 else "pct-only-historical",
    }
    return out


def backfill_rebuilt_stats() -> dict[str, Any]:
    """Idempotent: recompute stats + write to NEW file.

    Writes ``shark-stats-rebuilt.json`` (NOT the existing data.json or
    any kb/trades/*.json). Safe to run repeatedly — full replace of the
    rebuilt file is OK per spec (file is owned by this producer).

    Returns the payload that was written so callers can use it inline.
    """
    stats = recompute_stats()
    payload = {
        "stats": stats,
        "_meta": {
            "snapshot_ts": datetime.now(UTC).isoformat(),
            "age_s": 0,
            "stale": False,
            "source": "producers.shark_stats.backfill_rebuilt_stats",
            "kb_trades_dir": str(_KB_TRADES_DIR),
            "rebuilt_path": str(_REBUILT_STATS_PATH),
        },
    }
    try:
        _SHARK_MEMORY_DIR.mkdir(parents=True, exist_ok=True)
        # Atomic write — temp file then rename. Mode "w" on a path
        # OWNED by this producer (the rebuilt file is new; we never
        # truncate any other shark/memory/ artifact).
        tmp = _REBUILT_STATS_PATH.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, indent=2))
        tmp.replace(_REBUILT_STATS_PATH)
    except OSError as exc:
        logger.warning("backfill_rebuilt_stats: write failed: %s", exc)
        payload["_meta"]["write_error"] = str(exc)
    return payload


def shark_stats_snapshot() -> dict[str, Any]:
    """Read-side accessor used by `/api/v5/strategies/shark`.

    Preference order:
      1. The rebuilt file if present (this producer's backfill output).
      2. Fall back to recomputing in-memory (read-only — does not write).
      3. If neither is available, return all-zero stats + schema_health
         marker so the operator UI can render "—" + a stale-chip.
    """
    rebuilt = _safe_read_json(_REBUILT_STATS_PATH)
    if isinstance(rebuilt, dict) and isinstance(rebuilt.get("stats"), dict):
        stats = rebuilt["stats"]
        meta = rebuilt.get("_meta") or {}
        return {
            "stats": stats,
            "_meta": {
                "snapshot_ts": meta.get("snapshot_ts"),
                "age_s": _age_s(meta.get("snapshot_ts")),
                "stale": False,
                "market_open_now": False,
                "source": "shark-stats-rebuilt.json",
                "schema_health": stats.get("schema_health", "unknown"),
            },
        }
    # Read-only recompute
    stats = recompute_stats()
    return {
        "stats": stats,
        "_meta": {
            "snapshot_ts": datetime.now(UTC).isoformat(),
            "age_s": 0,
            "stale": False,
            "market_open_now": False,
            "source": "producers.shark_stats.recompute_stats (read-only)",
            "schema_health": stats.get("schema_health", "unknown"),
        },
    }


def _age_s(ts: str | None) -> int | None:
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        return int((datetime.now(UTC) - dt).total_seconds())
    except (ValueError, TypeError):
        return None
