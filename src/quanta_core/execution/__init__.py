"""quanta_core.execution — order placement, idempotency, slippage gating.

The single chokepoint where orders leave the process. Responsibilities are
split across four submodules:

* ``order_state_machine`` — strict NEW → SENT → ACK → FILLED state model.
* ``slippage_gate`` — pure pre-flight drift check (with stale-quote rejection).
* ``idempotency`` — SQLAlchemy-backed reserve-then-commit on a unique index.
* ``engine`` — port of ``user_data/modules/execution_engine.py`` with the
  two P0 fixes called out in DESIGN-LOCK.md:

    1. ``_cancel`` no longer ignores the venue response. If the cancel races
       a partial fill / fill, the engine records the fill and reports
       ``CancelOutcome.ALREADY_FILLED`` instead of silently losing the trade.
    2. ``_retry_order`` distinguishes 5xx / timeout (retryable) from 4xx
       (never-retryable). The previous implementation retried 422 duplicate
       client_order_id which is how we created phantom orders in 2026-05.

The four submodules are independently importable and independently unit-tested.
Cross-module wiring happens in ``ExecutionEngine.__init__``.
"""

from __future__ import annotations

from quanta_core.execution.engine import (
    CancelOutcome,
    ExecutionEngine,
    Fill,
    OrderProposal,
    RejectedReason,
    Side,
)
from quanta_core.execution.idempotency import (
    DuplicateClientOrderId,
    IdempotencyRow,
    IdempotencyStore,
    ReservationResult,
)
from quanta_core.execution.order_state_machine import (
    IllegalTransitionError,
    OrderState,
    OrderStateMachine,
    StateTransition,
)
from quanta_core.execution.slippage_gate import (
    SlippageGateResult,
    passes,
)

__all__ = [
    "CancelOutcome",
    "DuplicateClientOrderId",
    "ExecutionEngine",
    "Fill",
    "IdempotencyRow",
    "IdempotencyStore",
    "IllegalTransitionError",
    "OrderProposal",
    "OrderState",
    "OrderStateMachine",
    "RejectedReason",
    "ReservationResult",
    "Side",
    "SlippageGateResult",
    "StateTransition",
    "passes",
]
