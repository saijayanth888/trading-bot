"""V5 priority alert feed.

Reads ``user_data/data/risk_alerts.jsonl`` (append-only) and produces the
``<DetectFeed>`` payload. Sorted newest-first within each severity bucket
(``critical`` > ``warning`` > ``info``).

The endpoint NEVER opens the jsonl file with ``"w"`` (spec §5.4 hard
constraint) — read-only here.
"""
from __future__ import annotations

import json
import logging
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fastapi import APIRouter

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v5", tags=["v5", "alerts"])


_SEVERITY_RANK = {"critical": 0, "warning": 1, "info": 2}


def _alerts_path() -> Path:
    root = Path(os.environ.get("USER_DATA_ROOT", "/app/user_data"))
    return root / "data" / "risk_alerts.jsonl"


def _meta(snapshot_ts: datetime, source: Path) -> dict[str, Any]:
    age_s = int((datetime.now(tz=UTC) - snapshot_ts).total_seconds())
    return {
        "snapshot_ts": snapshot_ts.isoformat(),
        "age_s": age_s,
        "stale": False,  # operator-state, never market-stale
        "market_open_now": None,
        "source": str(source),
    }


def _read_rows(path: Path, max_rows: int = 500) -> list[dict[str, Any]]:
    """Tail-read the last ``max_rows`` lines from the append-only jsonl.

    The file is opened in **read** mode only. We read the whole file and
    keep the last N rows — fine at the volumes we expect (one row per
    risk-cap breach, typically <100/day).
    """
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    try:
        with path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except OSError as exc:
        logger.warning("alerts: read failed for %s: %s", path, exc)
        return []
    return rows[-max_rows:]


@router.get("/alerts")
async def alerts(limit: int = 50, since_hours: int = 24) -> dict[str, Any]:
    """Priority feed for the detect zone.

    Parameters
    ----------
    limit
        Max rows returned. Clamped to ``[1, 500]``.
    since_hours
        Only return alerts younger than this many hours. Default 24h
        (matches the spec's B8 forensic-surface requirement).
    """
    limit = max(1, min(int(limit), 500))
    since_hours = max(1, min(int(since_hours), 24 * 30))

    path = _alerts_path()
    snapshot_ts = (
        datetime.fromtimestamp(path.stat().st_mtime, tz=UTC)
        if path.exists() else datetime.now(tz=UTC)
    )
    rows = _read_rows(path)

    # Time-window filter
    cutoff = datetime.now(tz=UTC).timestamp() - since_hours * 3600
    filtered: list[dict[str, Any]] = []
    for r in rows:
        try:
            ts = datetime.fromisoformat(r.get("ts", "")).timestamp()
        except (TypeError, ValueError):
            continue
        if ts >= cutoff:
            filtered.append(r)

    # Sort: by severity first (critical → warning → info), then by ts desc.
    filtered.sort(
        key=lambda r: (
            _SEVERITY_RANK.get(str(r.get("severity") or "info"), 99),
            -(datetime.fromisoformat(r.get("ts", "1970-01-01T00:00:00+00:00")).timestamp()),
        )
    )

    return {
        "alerts": filtered[:limit],
        "counts": {
            "critical": sum(1 for r in filtered if r.get("severity") == "critical"),
            "warning": sum(1 for r in filtered if r.get("severity") == "warning"),
            "info": sum(1 for r in filtered if r.get("severity") == "info"),
            "total": len(filtered),
        },
        "since_hours": since_hours,
        "_meta": _meta(snapshot_ts, source=path),
    }


__all__ = ["router"]
