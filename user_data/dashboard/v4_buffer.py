"""V4 runtime observability buffer — vendored copy.

This is the in-container twin of `src/quanta_core/observability/v4_buffer.py`.
The dashboard's Dockerfile build context is `./user_data/dashboard/`, which
excludes `src/`; vendoring the buffer here keeps the import path local and
avoids a build-context rewrite. The canonical implementation lives at
`src/quanta_core/observability/v4_buffer.py` (same file content, kept in
sync — DRY tax accepted in exchange for a non-invasive deploy).

If this file and the canonical one drift, prefer the canonical version.
"""
from __future__ import annotations

import json
from collections import deque
from pathlib import Path
from threading import Lock
from typing import Any


class V4Buffer:
    """In-memory ring + JSONL append-only log.

    Thread-safe via a single Lock. Stdlib-only; safe to import early.
    """

    def __init__(self, jsonl_path: Path, capacity: int = 256) -> None:
        self._path = Path(jsonl_path)
        self._ring: deque[dict[str, Any]] = deque(maxlen=capacity)
        self._lock = Lock()
        self._path.parent.mkdir(parents=True, exist_ok=True)

    def append(self, event: dict[str, Any]) -> None:
        with self._lock:
            self._ring.append(event)
            with self._path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(event, default=str) + "\n")

    def read_recent(self, limit: int = 64) -> list[dict[str, Any]]:
        if limit <= 0:
            return []
        with self._lock:
            items = list(self._ring)
        if items:
            return items[-limit:]
        # In-memory ring empty — fall back to the durable JSONL tail. This
        # is what makes out-of-process writers (e.g. host-side
        # ``scripts/parity_oracle_tick.py``) visible to the dashboard's
        # /api/v4/* handlers without coupling the writer to the dashboard's
        # in-process buffer instance. Kept in sync with the canonical
        # implementation at src/quanta_core/observability/v4_buffer.py.
        return self._tail_jsonl(limit)

    def _tail_jsonl(self, limit: int) -> list[dict[str, Any]]:
        """Return the last ``limit`` parseable rows from the JSONL file.

        Stdlib-only; bounded by ``limit``. Malformed lines are skipped.
        Buffer reads must never raise — on any failure we return [] and let
        the v4_routes mock-fallback take over.
        """
        try:
            if not self._path.is_file():
                return []
            collected: list[dict[str, Any]] = []
            with self._path.open("r", encoding="utf-8") as f:
                lines = f.readlines()
            for line in reversed(lines):
                line = line.strip()
                if not line:
                    continue
                try:
                    collected.append(json.loads(line))
                except Exception:
                    continue
                if len(collected) >= limit:
                    break
            collected.reverse()
            return collected
        except Exception:
            return []
