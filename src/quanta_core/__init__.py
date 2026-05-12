"""Quanta Core — V4 quantitative trading stack.

This package is additive to the legacy ``user_data/`` (Freqtrade) and
``stocks/`` (Shark/Wheel) stacks. It is migrated behind a single
``runtime.mode`` toggle and never touches production state until shadow
parity proves out.

Public API is exposed by sub-packages (``live``, ``backtest``, ``strategy``,
``exchanges``, ``execution``, ``risk``, ``models``). This top-level module
publishes only the package version.
"""

from __future__ import annotations

__version__: str = "0.4.0.dev0"
__all__ = ["__version__"]
