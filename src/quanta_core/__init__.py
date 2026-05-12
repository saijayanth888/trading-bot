"""Quanta Core — V4 trading engine root package.

This package is the single source of truth for the V4 trading engine. It
replaces the Freqtrade-era ``user_data/modules`` shims with a typed, async,
ledger-backed engine. See ``docs/quanta-core-v4-rev2/DESIGN-LOCK.md`` for the
authoritative design.

Only ``__version__`` is exported here per ``docs/quanta-core-v4/10-CODE_PATTERNS.md``
§1.10.
"""

from __future__ import annotations

__version__: str = "0.1.0"

__all__ = ["__version__"]
