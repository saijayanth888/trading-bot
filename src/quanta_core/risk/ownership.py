"""Per-subsystem position ownership tracking (port of ``stocks/shared/subsystem_ownership.py``).

Why this exists
---------------
Shark (stocks-only swing trader) and Wheel (CSP/CC option strategy) both
trade on the same Alpaca paper account. The 2026-05-12 leak — Shark's
midday phase closing 5 Wheel-owned long puts at -7% hard stop — proved
that "filter by asset_class" alone is not enough:

* Asset-class filter stops Shark touching options.
* But Wheel can hold an *equity* row (assigned shares from a CSP).
* Without this module, Shark would see those shares, decide they're
  fair game, slap a 2x-ATR trailing stop on them, and unwind Wheel's
  cycle.

This module tags ownership at the subsystem level. Each subsystem
maintains a JSON state file listing the symbols IT has opened. Before
any modify/close action, the subsystem intersects the position list
against its own owned set.

State files
-----------
By default, ownership state lives at::

    ~/.quanta/state/owned_symbols-{subsystem}.json

This matches the architecture-lock convention (state files under
``~/.quanta/state/``). The path can be overridden via the
``QUANTA_STATE_DIR`` env var for tests, and individual unit-test
fixtures can monkey-patch :func:`_state_path` directly.

Schema
------
::

    {
      "updated_at": "2026-05-12T19:30:00Z",
      "symbols": ["NVDA", "AAPL"],
      "schema_version": 1
    }

API
---
* :func:`load_owned(subsystem) -> set[str]` — empty set if absent.
* :func:`save_owned(subsystem, symbols) -> None` — atomic write.
* :func:`owns(owned_set, symbol) -> bool`
* :func:`claim(subsystem, symbol) -> None` — idempotent.
* :func:`release(subsystem, symbol) -> None` — idempotent.

Concurrency
-----------
Atomic file writes via :func:`os.replace` over a same-directory temp
file. No file lock — the operator's cron schedule serializes Shark
phases and Wheel routines, so a true cross-process lock would be
over-engineering. This is documented in the design as a known limit.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import tempfile
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

logger = logging.getLogger(__name__)

Subsystem = Literal["shark", "wheel"]
SCHEMA_VERSION = 1

__all__ = [
    "SCHEMA_VERSION",
    "Subsystem",
    "claim",
    "load_owned",
    "owns",
    "release",
    "save_owned",
]


def _state_dir() -> Path:
    """Return the directory where ownership state files live.

    Override path via the ``QUANTA_STATE_DIR`` env var (used by tests
    and by alternate deployments that want state outside ``$HOME``).
    """
    override = os.environ.get("QUANTA_STATE_DIR", "").strip()
    if override:
        return Path(override)
    return Path(os.path.expanduser("~")) / ".quanta" / "state"


def _state_path(subsystem: Subsystem) -> Path:
    """Resolve the canonical state file for *subsystem*.

    Raises :class:`ValueError` for unknown subsystem strings — fail loud
    so a typo doesn't quietly create a third orphan state file.
    """
    if subsystem not in ("shark", "wheel"):
        raise ValueError(f"unknown subsystem {subsystem!r} — expected 'shark' or 'wheel'")
    return _state_dir() / f"owned_symbols-{subsystem}.json"


def _atomic_write_json(path: Path, payload: dict[str, object]) -> None:
    """Same-directory tempfile + ``os.replace`` = POSIX-atomic write.

    Crash safety: a SIGKILL between the write() and ``os.replace()``
    leaves *path* untouched (still readable). The temp file is cleaned
    up on failure so directory listings stay tidy.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_str = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=str(path.parent),
    )
    tmp = Path(tmp_str)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2, sort_keys=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except Exception:
        with contextlib.suppress(OSError):
            tmp.unlink()
        raise


def _now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


# ── Public API ──────────────────────────────────────────────────────────────


def load_owned(subsystem: Subsystem) -> set[str]:
    """Return the set of symbols *subsystem* currently claims to own.

    Returns an empty set when the state file does not exist or is
    unreadable / corrupt. Empty-set is the right cold-start default
    because the caller will then refuse to act on any position —
    fail-safe, not fail-open.
    """
    path = _state_path(subsystem)
    if not path.exists():
        return set()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.error(
            "[ownership.%s] state file corrupt (%s): %s — treating as empty",
            subsystem,
            path,
            exc,
        )
        return set()
    symbols = raw.get("symbols", []) if isinstance(raw, dict) else []
    return {str(s).upper() for s in symbols if isinstance(s, str) and s}


def save_owned(subsystem: Subsystem, symbols: Iterable[str]) -> None:
    """Atomically persist *symbols* as the ownership set for *subsystem*."""
    deduped = sorted({str(s).upper() for s in symbols if isinstance(s, str) and s})
    payload: dict[str, object] = {
        "updated_at": _now_iso(),
        "symbols": deduped,
        "schema_version": SCHEMA_VERSION,
    }
    _atomic_write_json(_state_path(subsystem), payload)
    logger.debug("[ownership.%s] saved %d symbols", subsystem, len(deduped))


def owns(owned_set: set[str], symbol: str) -> bool:
    """True iff *symbol* (case-insensitive) is in *owned_set*."""
    if not symbol:
        return False
    return symbol.upper() in {s.upper() for s in owned_set}


def claim(subsystem: Subsystem, symbol: str) -> None:
    """Append *symbol* to *subsystem*'s owned set. Idempotent."""
    if not symbol:
        return
    current = load_owned(subsystem)
    sym = symbol.upper()
    if sym in current:
        logger.debug("[ownership.%s] claim %s — already owned", subsystem, sym)
        return
    current.add(sym)
    save_owned(subsystem, current)
    logger.info("[ownership.%s] claimed %s (%d total)", subsystem, sym, len(current))


def release(subsystem: Subsystem, symbol: str) -> None:
    """Remove *symbol* from *subsystem*'s owned set. Idempotent."""
    if not symbol:
        return
    current = load_owned(subsystem)
    sym = symbol.upper()
    if sym not in current:
        logger.debug("[ownership.%s] release %s — not currently owned", subsystem, sym)
        return
    current.discard(sym)
    save_owned(subsystem, current)
    logger.info(
        "[ownership.%s] released %s (%d remaining)",
        subsystem,
        sym,
        len(current),
    )
