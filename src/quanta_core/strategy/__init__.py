"""Strategy package — canonical (sync) Strategy ABC + live-engine async variant.

The concrete strategies (MeanRevTFT, Wheel, NFI X6, ...) port to the canonical
synchronous :class:`Strategy` ABC; the live engine consumes the async variant
:class:`AsyncStrategy`. The two are kept separate so backtest determinism
(sync, foundation-locked) is not coupled to the live-engine scheduler.
"""

from __future__ import annotations

from quanta_core.strategy.async_strategy import AsyncStrategy
from quanta_core.strategy.base import Strategy

__all__ = ["AsyncStrategy", "Strategy"]
