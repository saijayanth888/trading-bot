"""Ledger anomaly writer — append-only JSONL ring on the host filesystem.

The reconciler calls ``record_anomaly`` whenever a REST snapshot diverges
from our in-memory state by more than the configured epsilon. The on-disk
format is intentionally trivial so the dashboard + Hermes nightly reflector
can ``grep`` for anomalies without needing a Python parser.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


def record_anomaly(
    path: Path,
    *,
    kind: str,
    detail: dict[str, Any],
    now: datetime | None = None,
) -> None:
    """Append one JSONL row to ``path``.

    Parameters
    ----------
    path
        Output JSONL file. Parent directories are created as needed.
    kind
        Short anomaly classifier, e.g. ``"position_gap"``.
    detail
        Free-form payload; must be JSON-serialisable.
    now
        Override timestamp for tests. Defaults to ``datetime.now(timezone.utc)``.

    Notes
    -----
    Writes are best-effort; on filesystem failure we silently propagate the
    exception to the caller (the reconciler) which decides whether to alert
    on the failure path. We do NOT swallow ``OSError`` here.
    """

    ts = now or datetime.now(UTC)
    path.parent.mkdir(parents=True, exist_ok=True)
    row = {
        "ts": ts.isoformat(),
        "kind": kind,
        "detail": detail,
    }
    line = json.dumps(row, separators=(",", ":"), default=str)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(line + "\n")


__all__ = ["record_anomaly"]
