"""Per-subsystem position ownership tracking.

Why this exists
---------------
Shark (stocks-only swing trader) and Wheel (CSP/CC option strategy) both
trade on the same Alpaca paper account. The 2026-05-12 leak — Shark's
midday phase closing 5 Wheel-owned long puts at -7% hard stop — proved
that "filter by asset_class" alone is not enough:

  * Fix 1 stops Shark touching options.
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
- ``stocks/shark/state/owned_symbols.json``  (equity tickers Shark owns)
- ``stocks/wheel/state/owned_symbols.json``  (OCC tickers + underlying
                                             assigned-share symbols Wheel owns)

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
load_owned(subsystem)       → set[str]    (empty if state file absent)
save_owned(subsystem, sym)  → None        (atomic write)
owns(owned_set, symbol)     → bool
claim(subsystem, symbol)    → None        (idempotent append + save)
release(subsystem, symbol)  → None        (idempotent remove + save)

Concurrency
-----------
Atomic file writes via ``os.replace`` over a same-directory temp file.
No file lock — the operator's cron schedule serializes Shark phases and
Wheel routines, so a true cross-process lock would be over-engineering.
This is documented in the HANDOFF as a known limit.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Literal

logger = logging.getLogger(__name__)

Subsystem = Literal["shark", "wheel"]
SCHEMA_VERSION = 1

# Resolve state file locations relative to this module (`stocks/shared/`)
# rather than CWD so cron jobs from any working directory see the right
# file. Each subsystem keeps its state under its own `state/` directory
# alongside its existing journals.
_STOCKS_ROOT = Path(__file__).resolve().parent.parent  # → .../stocks


def _state_path(subsystem: Subsystem) -> Path:
    """Resolve the canonical state file for *subsystem*."""
    if subsystem not in ("shark", "wheel"):
        raise ValueError(
            f"unknown subsystem {subsystem!r} — expected 'shark' or 'wheel'"
        )
    return _STOCKS_ROOT / subsystem / "state" / "owned_symbols.json"


def _atomic_write_json(path: Path, payload: dict) -> None:
    """Same-directory tempfile + os.replace = POSIX-atomic write.

    Crash safety: a SIGKILL between the write() and os.replace() leaves
    *path* untouched (still readable). The temp file is cleaned up on
    failure so directory listings stay tidy.
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
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ── Public API ──────────────────────────────────────────────────────────────


def load_owned(subsystem: Subsystem) -> set[str]:
    """Return the set of symbols *subsystem* currently claims to own.

    Returns an empty set when the state file does not exist or is
    unreadable/corrupt. Empty-set is the right cold-start default
    because the caller will then refuse to act on any position —
    fail-safe, not fail-open. Use the bootstrap script
    (``shared/migrate_ownership_bootstrap.py``) to seed the file from
    the live Alpaca state once.
    """
    path = _state_path(subsystem)
    if not path.exists():
        return set()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.error(
            "[ownership.%s] state file corrupt (%s): %s — treating as empty",
            subsystem, path, exc,
        )
        return set()
    symbols = raw.get("symbols", []) if isinstance(raw, dict) else []
    return {str(s).upper() for s in symbols if isinstance(s, str) and s}


def save_owned(subsystem: Subsystem, symbols: Iterable[str]) -> None:
    """Atomically persist *symbols* as the ownership set for *subsystem*."""
    deduped = sorted({str(s).upper() for s in symbols if isinstance(s, str) and s})
    payload = {
        "updated_at": _now_iso(),
        "symbols": deduped,
        "schema_version": SCHEMA_VERSION,
    }
    _atomic_write_json(_state_path(subsystem), payload)
    logger.debug(
        "[ownership.%s] saved %d symbols", subsystem, len(deduped),
    )


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
    logger.info("[ownership.%s] released %s (%d remaining)", subsystem, sym, len(current))


__all__ = [
    "Subsystem",
    "SCHEMA_VERSION",
    "load_owned",
    "save_owned",
    "owns",
    "claim",
    "release",
]
