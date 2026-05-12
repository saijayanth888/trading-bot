"""Strategy ABC and concrete strategies.

Wave 2 only ships the abstract :class:`quanta_core.strategy.base.Strategy`.
Concrete strategies (``mean_rev_tft``, ``wheel_csp``, ``shark_debate``,
``nfi_x6``) land in their own wave branches.
"""

from quanta_core.strategy.base import Strategy

__all__ = ["Strategy"]
