"""Strategy abstract base class.

Defines the five-hook lifecycle every concrete strategy plugs into. Hooks
fire in a stable order — ``on_start`` -> {``on_candle``, ``on_tick``,
``on_fill``}* -> ``on_stop`` — and are serialised per ``(strategy, symbol)``
by the dispatcher; concurrency between strategies is the framework's job,
never the strategy's.

The base class is intentionally **synchronous** (no ``async``). Async hooks
require an event loop, which complicates the backtest engine and adds zero
value for the workloads we run (numpy + Decimal indicator math). Strategies
that want async work can hand off to ``asyncio.to_thread`` from inside the
``Context``.

This mirrors the foundation branch (``feat/v4-build-foundation``) so the
morning merge between foundation and wave-2 is a no-op rename.

See ``docs/quanta-core-v4-rev2/DESIGN-LOCK.md`` §5 ("Strategy ABC hooks")
for the locked signature; ``docs/quanta-core-v4/06-ARCHITECTURE.md`` §3.7
for the long-form description.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover — typing only
    from collections.abc import Sequence

    from quanta_core.types import Bar, Context, Fill, OrderProposal, Tick


class Strategy(ABC):
    """Abstract base class for every concrete trading strategy.

    Parameters
    ----------
    ctx
        Runtime :class:`quanta_core.types.Context` (live or backtest).
    config
        Strategy-specific configuration dict, parsed by the loader from the
        ``[strategy_overrides.<name>]`` section of the TOML config.

    Notes
    -----
    Only :meth:`on_candle` is mandatory; the other hooks default to no-ops
    so a strategy can opt in lazily. The framework guarantees that no hook
    is invoked before :meth:`on_start` returns and no hook is invoked after
    :meth:`on_stop` returns.
    """

    name: str = "strategy"

    def __init__(self, ctx: Context, config: dict[str, Any]) -> None:
        """Construct the strategy with a runtime context and config dict."""
        self.ctx = ctx
        self.config = dict(config)

    # ------------------------------------------------------------------
    # Mandatory hook
    # ------------------------------------------------------------------

    @abstractmethod
    def on_candle(self, bar: Bar) -> Sequence[OrderProposal]:
        """Process one closed bar and return any orders to submit.

        Returns
        -------
        Sequence[OrderProposal]
            Orders the strategy wants to submit. May be empty.
        """

    # ------------------------------------------------------------------
    # Optional hooks — safe no-op defaults so subclasses opt in lazily.
    # ------------------------------------------------------------------

    def on_tick(self, tick: Tick) -> Sequence[OrderProposal]:
        """Handle a raw tick before bar aggregation. Default: empty tuple."""
        return ()

    def on_fill(self, fill: Fill) -> None:
        """Handle a confirmed venue fill. Default: no-op."""

    def on_start(self) -> None:
        """Run pre-event warm-up. Default: no-op."""

    def on_stop(self) -> None:
        """Run shutdown cleanup. Default: no-op."""

    def train_hook(self, samples: list[Any]) -> None:
        """Receive a walk-forward training slice. Default: no-op."""

    # ------------------------------------------------------------------
    # Convenience repr — strategies are frequently logged.
    # ------------------------------------------------------------------

    def __repr__(self) -> str:
        """Render ``ClassName(name=...)`` for log lines."""
        return f"{type(self).__name__}(name={self.name!r})"
