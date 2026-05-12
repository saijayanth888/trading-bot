"""Strategy package — ABC and helpers.

The concrete strategies (MeanRevTFT, Wheel, NFI X6, ...) are ported by the
strategy agent. Only the abstract base is needed by the live engine.
"""

from __future__ import annotations

from quanta_core.strategy.base import Strategy

__all__ = ["Strategy"]
