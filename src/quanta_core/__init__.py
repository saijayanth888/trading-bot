"""Quanta Core — V4 trading engine package.

Public API is the ``live``, ``backtest``, ``strategy``, ``exchanges`` and
``risk`` subpackages. This top-level module exposes only ``__version__``.
"""

from __future__ import annotations

__version__: str = "0.4.0.dev0"
__all__ = ["__version__"]
