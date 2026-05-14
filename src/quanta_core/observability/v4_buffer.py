"""V4 runtime observability buffer.

In-memory ring + JSONL tail used as the live-data substrate for the
/api/v4/* dashboard surfaces. Writers (future debate orchestrator,
parity oracle, monte carlo) append events; the dashboard's v4_routes
handlers call `read_recent` for fast reads with mock fallback when the
ring is empty.

Why a buffer (not postgres)?
- Eliminates an SPOF on the ledger postgres for purely observational data.
- Lets v4_routes render live within ms; postgres reads would compete with
  freqtrade writes.
- JSONL is the durable record; a future cron at scripts/v4_rotate_runtime.sh
  will trim files >100 MB.

This module is intentionally tiny and dependency-free: stdlib only.
"""
from __future__ import annotations

import json
from collections import deque
from pathlib import Path
from threading import Lock
from typing import Any


class V4Buffer:
    """In-memory ring + JSONL append-only log.

    Thread-safe via a single Lock — writes happen on the dashboard's
    request-handler threads as well as any future worker that imports
    this module, so the lock keeps the JSONL flush atomic and the ring
    consistent.
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
        # in-process buffer instance. The tail is read at most once per
        # call and is bounded by ``limit`` so memory stays small even if
        # the JSONL has grown large between rotations.
        return self._tail_jsonl(limit)

    def _tail_jsonl(self, limit: int) -> list[dict[str, Any]]:
        """Return the last ``limit`` parseable rows from the JSONL file.

        Stdlib-only; reads the whole file but bounds the result to ``limit``.
        At 256-row capacity * O(200B) per row the file is ~50 KB before
        rotation, so a full read is cheap. Malformed lines are skipped.
        """
        try:
            if not self._path.is_file():
                return []
            # Read newest-first by walking lines from the end. We collect
            # at most `limit` valid rows, then reverse to oldest-first to
            # match the in-memory ring's natural order.
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
            # Buffer reads must never raise into the request path; an empty
            # list lets the dashboard's mock fallback take over.
            return []
