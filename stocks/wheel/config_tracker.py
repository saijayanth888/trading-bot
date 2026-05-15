"""Wheel-config drift + promotion tracker.

Records when the operator changes PCT_* values (Config A→B→…) so the
dashboard can surface "Config A is N days old; Config B promotion
criteria are X/Y met."

Why this exists
---------------
The original $50k-pilot caps (max_total=10 % / max_risk=3.4 %) silently
became the wrong defaults when the account scaled to $100k — they were
never tagged with a "set on YYYY-MM-DD by operator" timestamp so the
operator had no way to know they were stale. Config A was applied
2026-05-15; without this tracker it would suffer the same fate.

This module:
  1. Reads the current PCT_* values from `risk_caps.py`.
  2. Compares against the last persisted snapshot at
     `stocks/wheel/state/risk_config.json`.
  3. If different, writes a new snapshot with `applied_at = NOW`.
  4. Exposes `get_config_status()` → dict with the persisted snapshot
     plus computed `days_active` and `expected_config_b_eligible_at`
     (28 days after applied_at, by convention).

Idempotent — safe to call on every wheel run.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Persisted snapshot lives next to positions.json + owned_symbols.json.
_STATE_FILE = (
    Path(__file__).resolve().parent / "state" / "risk_config.json"
)

# Days between Config-A application and Config-B eligibility window.
# Matches the 4-week paper-trading recommendation in
# audit/2026-05-15-wheel-sizing-research.md.
_CONFIG_B_ELIGIBILITY_DAYS = 28


def _atomic_write(path: Path, payload: dict[str, Any]) -> None:
    """Write JSON atomically — temp + fsync + os.replace."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w") as f:
        json.dump(payload, f, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def _current_pct_values() -> dict[str, float]:
    """Read the live PCT_* values from risk_caps (env-overrides honoured)."""
    # Import lazily so a fresh module import picks up env overrides each
    # time the wheel cron fires.
    from wheel.risk_caps import (
        PCT_TOTAL_COLLATERAL,
        PCT_RISK_PER_TICKER,
        PCT_KILL_LOSS_PER_CYCLE,
    )
    return {
        "pct_total_collateral": round(PCT_TOTAL_COLLATERAL, 6),
        "pct_risk_per_ticker": round(PCT_RISK_PER_TICKER, 6),
        "pct_kill_loss_per_cycle": round(PCT_KILL_LOSS_PER_CYCLE, 6),
    }


def _classify_config(pcts: dict[str, float]) -> str:
    """Return a human-friendly label for a known config preset."""
    t, r = pcts["pct_total_collateral"], pcts["pct_risk_per_ticker"]
    # Loose matching — float equality is brittle.
    near = lambda a, b: abs(a - b) < 1e-4
    if near(t, 0.10) and near(r, 0.034):
        return "pilot-$50k-2026-05-14"  # the legacy $50k-pilot config
    if near(t, 0.25) and near(r, 0.10):
        return "Config-A"
    if near(t, 0.40) and near(r, 0.15):
        return "Config-B"
    return "custom"


def record_config_if_changed() -> dict[str, Any]:
    """Update the snapshot if PCT_* values differ from last persisted run.

    Returns the active snapshot dict. Idempotent — only writes when the
    PCT values actually differ from the last persisted version.
    """
    pcts = _current_pct_values()
    label = _classify_config(pcts)
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")

    prev: dict[str, Any] | None = None
    try:
        prev = json.loads(_STATE_FILE.read_text())
    except FileNotFoundError:
        prev = None
    except Exception as exc:
        logger.warning("wheel.config_tracker: bad snapshot — overwriting: %s", exc)
        prev = None

    if prev and all(
        abs(prev.get(k, -1) - v) < 1e-6 for k, v in pcts.items()
    ):
        # No change. Return the existing snapshot.
        return prev

    payload: dict[str, Any] = dict(pcts)
    payload["config_label"] = label
    payload["applied_at"] = now
    payload["previous"] = prev or {}
    _atomic_write(_STATE_FILE, payload)
    logger.info(
        "wheel.config_tracker: config changed → label=%s pcts=%s",
        label, pcts,
    )
    return payload


def get_config_status() -> dict[str, Any]:
    """Return the current config snapshot enriched with derived fields.

    Used by the dashboard endpoint /api/ops/wheel_config to surface a
    "Config A is N days old; eligible for Config-B in M days" card.
    """
    try:
        snap = json.loads(_STATE_FILE.read_text())
    except FileNotFoundError:
        # First-ever read — record now and return.
        snap = record_config_if_changed()
    except Exception as exc:
        return {"error": f"snapshot read failed: {exc}"}

    applied_at_iso = snap.get("applied_at")
    days_active: float | None = None
    eligible_in_days: float | None = None
    eligible_at_iso: str | None = None
    if applied_at_iso:
        try:
            applied_dt = datetime.fromisoformat(applied_at_iso)
            now = datetime.now(timezone.utc)
            days_active = (now - applied_dt).total_seconds() / 86400.0
            eligible_dt = applied_dt + timedelta(days=_CONFIG_B_ELIGIBILITY_DAYS)
            eligible_in_days = (eligible_dt - now).total_seconds() / 86400.0
            eligible_at_iso = eligible_dt.isoformat(timespec="seconds")
        except Exception:
            pass

    return {
        **snap,
        "days_active": round(days_active, 2) if days_active is not None else None,
        "config_b_eligible_at": eligible_at_iso,
        "config_b_eligible_in_days": round(eligible_in_days, 2) if eligible_in_days is not None else None,
        "config_b_eligible_now": (eligible_in_days is not None and eligible_in_days <= 0),
        "promotion_window_days": _CONFIG_B_ELIGIBILITY_DAYS,
    }
