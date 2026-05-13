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
        with self._lock:
            items = list(self._ring)
        if limit <= 0:
            return []
        return items[-limit:]
