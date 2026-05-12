"""Quanta Core — V4 quantitative trading stack.

Public API is exposed by sub-packages (``live``, ``backtest``, ``strategy``,
``exchanges``, ``execution``, ``risk``, ``models``). This top-level module
publishes only the package version; sub-packages declare their own public
surface via per-package ``__all__``.
"""

from __future__ import annotations

__version__: str = "0.4.0.dev0"
__all__ = ["__version__"]
