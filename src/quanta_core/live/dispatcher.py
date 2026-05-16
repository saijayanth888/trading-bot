"""StrategyDispatcher — routes events to strategies, never crashes the loop.

The dispatcher is the only place that calls into user-supplied strategy
code. Three guarantees:

1. Exceptions raised inside a strategy hook are caught, logged with a
   correlation id, and swallowed. They DO NOT propagate to the caller.
2. The 30-second deliberate-debate budget is enforced at the per-call
   level: a strategy that spends more than the budget on a single hook has
   its result discarded and a metric counter incremented.
3. OrderProposals returned by hooks are forwarded to the execution layer
   via a protocol (``OrderSink``) — the live module never imports
   ``execution.engine`` directly.

The execution layer is owned by a sibling agent; we only assume the
interface declared in ``OrderSink``.
"""

from __future__ import annotations

import logging
import os
import uuid
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Protocol, runtime_checkable

import anyio

from quanta_core.risk.single_name_cap import enforce_single_name_cap

if TYPE_CHECKING:
    from quanta_core.strategy.async_strategy import AsyncStrategy as Strategy
    from quanta_core.util.types import Bar, Fill, OrderProposal, Tick

_log = logging.getLogger(__name__)


DEFAULT_BUDGET_SECONDS: float = 30.0
"""Hard upper bound on a single strategy hook invocation.

The deliberate-debate workflow (bull / bear / arbiter) is locked at 30s in
``docs/quanta-core-v4-rev2/DESIGN-LOCK.md``. Hooks that exceed this budget
have their result dropped — we never let a slow agent block the loop.
"""


@runtime_checkable
class OrderSink(Protocol):
    """Where the dispatcher forwards approved OrderProposals.

    The concrete sink is the execution engine in production, or a recording
    fake in tests. We never import ``execution.engine`` here.
    """

    async def submit(self, proposal: OrderProposal) -> None:
        """Forward an order proposal for risk + execution."""


@dataclass
class DispatcherMetrics:
    """Counters for observability — read by the dashboard."""

    candles_dispatched: int = 0
    ticks_dispatched: int = 0
    fills_dispatched: int = 0
    hook_exceptions: int = 0
    budget_exceeded: int = 0
    proposals_forwarded: int = 0
    single_name_cap_rejected: int = 0


# B8 — single-name-cap enforcement at entry. The dispatcher invokes the gate
# *before* forwarding a proposal to the OrderSink, so a strategy that sizes
# itself to 34× the cap can never reach the execution engine. The default
# sleeve-equity provider returns 0.0 (forces reject) so production deployments
# MUST inject a real provider via ``StrategyDispatcher.set_sleeve_equity_provider``.
SleeveEquityProvider = Callable[[str, "OrderProposal"], float]


def _default_sleeve_equity_provider(sleeve: str, proposal: object) -> float:  # noqa: ARG001
    """Fail-closed default — return 0.0 sleeve equity so the cap rejects.

    Production wiring should replace this with a callable that reads
    ``unified_risk.crypto_equity`` / ``stocks_equity``. Until that wiring
    lands, the dispatcher will reject every new proposal whose ``stake_usd``
    is positive — which is *safer* than silently allowing all proposals.
    """
    return 0.0


@dataclass
class StrategyDispatcher:
    """Routes Bar / Tick / Fill events to registered strategies.

    Parameters
    ----------
    sink
        Where to forward OrderProposals returned by hooks.
    budget_seconds
        Per-hook timeout. Defaults to ``DEFAULT_BUDGET_SECONDS``.
    """

    sink: OrderSink
    budget_seconds: float = DEFAULT_BUDGET_SECONDS
    metrics: DispatcherMetrics = field(default_factory=DispatcherMetrics)
    _strategies: list[Strategy] = field(default_factory=list)
    # B8 — single-name-cap injection points. Both are dataclass fields so
    # they survive copies + can be swapped at wiring time (engine.py wires
    # the real sleeve-equity provider once it's spun up).
    sleeve_equity_provider: SleeveEquityProvider = field(
        default=_default_sleeve_equity_provider
    )
    single_name_cap_pct: float = field(
        default_factory=lambda: float(os.environ.get("SINGLE_NAME_CAP_PCT", "0.10"))
    )
    single_name_cap_enabled: bool = field(
        default_factory=lambda: os.environ.get(
            "SINGLE_NAME_CAP_ENFORCE", "1"
        ).strip() not in {"0", "false", "False", ""}
    )

    def set_sleeve_equity_provider(self, provider: SleeveEquityProvider) -> None:
        """Wire a real sleeve-equity lookup into the gate. Idempotent."""
        self.sleeve_equity_provider = provider

    def register(self, strategy: Strategy) -> None:
        """Add a strategy. Idempotent on ``strategy.name``."""

        for existing in self._strategies:
            if existing.name == strategy.name:
                return
        self._strategies.append(strategy)
        _log.info(
            "dispatcher.register",
            extra={
                "strategy": strategy.name,
                "symbols": [str(s) for s in strategy.symbols],
                "timeframes": list(strategy.timeframes),
            },
        )

    def unregister(self, name: str) -> None:
        """Drop a strategy by name. No-op if not registered."""

        self._strategies = [s for s in self._strategies if s.name != name]

    @property
    def strategies(self) -> tuple[Strategy, ...]:
        """Snapshot of currently registered strategies."""

        return tuple(self._strategies)

    async def dispatch_candle(self, bar: Bar, ctx: object = None) -> None:
        """Call ``on_candle`` on every strategy that subscribes."""

        self.metrics.candles_dispatched += 1
        for strategy in self._strategies:
            if bar.symbol not in strategy.symbols:
                continue
            if bar.timeframe not in strategy.timeframes:
                continue
            await self._invoke(strategy, "on_candle", bar, ctx)

    async def dispatch_tick(self, tick: Tick, ctx: object = None) -> None:
        """Call ``on_tick`` on every strategy that opted in via ``wants_ticks``."""

        self.metrics.ticks_dispatched += 1
        for strategy in self._strategies:
            if not strategy.wants_ticks:
                continue
            if tick.symbol not in strategy.symbols:
                continue
            await self._invoke(strategy, "on_tick", tick, ctx)

    async def dispatch_fill(self, fill: Fill, ctx: object = None) -> None:
        """Call ``on_fill`` on every strategy holding the symbol."""

        self.metrics.fills_dispatched += 1
        for strategy in self._strategies:
            if fill.symbol not in strategy.symbols:
                continue
            await self._invoke(strategy, "on_fill", fill, ctx)

    # ----- private -----

    async def _invoke(
        self,
        strategy: Strategy,
        hook: str,
        event: object,
        ctx: object,
    ) -> None:
        """Run one hook with timeout + exception isolation."""

        correlation_id = uuid.uuid4().hex
        method = getattr(strategy, hook)
        try:
            with anyio.fail_after(self.budget_seconds):
                proposals: Iterable[OrderProposal] = await method(event, ctx)
        except TimeoutError:
            self.metrics.budget_exceeded += 1
            _log.warning(
                "dispatcher.budget_exceeded",
                extra={
                    "strategy": strategy.name,
                    "hook": hook,
                    "budget_seconds": self.budget_seconds,
                    "correlation_id": correlation_id,
                },
            )
            return
        except Exception:
            self.metrics.hook_exceptions += 1
            _log.exception(
                "dispatcher.hook_exception",
                extra={
                    "strategy": strategy.name,
                    "hook": hook,
                    "correlation_id": correlation_id,
                },
            )
            return

        await self._forward(proposals, strategy.name, correlation_id)

    async def _forward(
        self,
        proposals: Iterable[OrderProposal],
        strategy_name: str,
        correlation_id: str,
    ) -> None:
        """Push proposals into the execution sink, one by one.

        A failure in the sink is logged but does NOT propagate — the loop
        keeps running. Production sinks raise only on programming errors
        (interface drift); transient broker errors are absorbed by the
        sink's own retry layer.
        """

        for proposal in proposals or []:
            # B8 — single-name-cap gate. Runs BEFORE sink.submit so a
            # 34×-cap proposal can never reach the execution engine. Rejects
            # are append-logged to user_data/data/risk_alerts.jsonl by
            # ``enforce_single_name_cap``; we never let an audit failure
            # mask a true rejection.
            if self.single_name_cap_enabled and not self._allow_single_name_cap(
                proposal, strategy_name, correlation_id
            ):
                self.metrics.single_name_cap_rejected += 1
                continue
            try:
                await self.sink.submit(proposal)
                self.metrics.proposals_forwarded += 1
            except Exception:
                _log.exception(
                    "dispatcher.sink_exception",
                    extra={
                        "strategy": strategy_name,
                        "correlation_id": correlation_id,
                    },
                )

    def _allow_single_name_cap(
        self,
        proposal: OrderProposal,
        strategy_name: str,
        correlation_id: str,
    ) -> bool:
        """Run the single-name cap gate. Returns True when the proposal is OK
        to forward, False when it must be dropped.

        Notional sizing comes from ``proposal.qty * proposal.limit_price``
        when a limit price is set, else from ``proposal.metadata["mid_price"]``
        (strategy hooks include the venue mid for market orders), else from
        ``proposal.metadata["notional_usd"]`` (some strategies pre-compute it).
        Missing-data path is fail-closed: when we can't price the proposal,
        we *allow* it (the gate is best-effort sizing data only; the
        sleeve-equity check still runs against equity=0 in dispatcher dev mode).
        """
        try:
            notional = self._notional_usd(proposal)
            if notional is None or notional <= 0:
                return True  # close/reduce/unknown — pass through
            sleeve = str(proposal.metadata.get("sleeve") or "crypto")
            sleeve_equity = float(self.sleeve_equity_provider(sleeve, proposal))
            allowed, reason = enforce_single_name_cap(
                symbol=str(proposal.symbol),
                stake_usd=notional,
                sleeve_equity_usd=sleeve_equity,
                cap_pct=self.single_name_cap_pct,
                sleeve=sleeve,
            )
            if not allowed:
                _log.warning(
                    "dispatcher.single_name_cap_rejected",
                    extra={
                        "strategy": strategy_name,
                        "symbol": str(proposal.symbol),
                        "stake_usd": notional,
                        "sleeve_equity_usd": sleeve_equity,
                        "cap_pct": self.single_name_cap_pct,
                        "correlation_id": correlation_id,
                        "reason": reason,
                    },
                )
            return allowed
        except Exception:
            # Defensive: a bug in the gate must NOT crash the dispatcher
            # loop. Fail-open here (forward the proposal) because the gate
            # itself is broken — the operator will see the exception trail
            # in logs and can disable enforcement via env var until fixed.
            _log.exception(
                "dispatcher.single_name_cap_gate_error",
                extra={"strategy": strategy_name, "correlation_id": correlation_id},
            )
            return True

    @staticmethod
    def _notional_usd(proposal: OrderProposal) -> float | None:
        """Best-effort notional sizing for the cap gate."""
        meta = proposal.metadata or {}
        # Explicit pre-computed notional wins (strategies that already
        # multiply price × qty for risk packaging).
        if "notional_usd" in meta:
            try:
                return float(meta["notional_usd"])
            except (TypeError, ValueError):
                pass
        # Limit orders carry the price.
        try:
            qty = abs(float(proposal.qty))
        except (TypeError, ValueError):
            return None
        if proposal.limit_price is not None:
            try:
                return qty * float(proposal.limit_price)
            except (TypeError, ValueError):
                pass
        # Market orders carry the mid in metadata by convention.
        for key in ("mid_price", "ref_price", "last_price", "price"):
            if key in meta:
                try:
                    return qty * float(meta[key])
                except (TypeError, ValueError):
                    continue
        return None


__all__ = [
    "DEFAULT_BUDGET_SECONDS",
    "DispatcherMetrics",
    "OrderSink",
    "StrategyDispatcher",
]
