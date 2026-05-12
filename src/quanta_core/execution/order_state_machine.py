"""Order state machine.

Strict, audited state model for an order's lifecycle. Illegal transitions
raise — they are programmer bugs, not runtime conditions.

States
------
* ``NEW`` — proposal accepted into the engine; no network call yet.
* ``SENT`` — sent to the venue, awaiting acknowledgement.
* ``ACK`` — venue accepted the order (resting on book).
* ``PARTIAL_FILL`` — at least one fill but more size remains.
* ``FILLED`` — terminal: fully filled.
* ``REJECTED`` — terminal: venue rejected or local gate rejected.
* ``CANCELED`` — terminal: cancelled before fully filled.

Transitions
-----------
::

    NEW         → SENT | REJECTED
    SENT        → ACK  | REJECTED   | CANCELED
    ACK         → PARTIAL_FILL | FILLED | CANCELED | REJECTED
    PARTIAL_FILL → PARTIAL_FILL | FILLED | CANCELED
    FILLED      → (terminal)
    REJECTED    → (terminal)
    CANCELED    → (terminal)

Every transition is recorded with a timestamp and an optional reason. The
full audit trail is available via ``OrderStateMachine.history``.

This is a small, pure module: no I/O, no time-dependence (the caller passes
the wall clock in). It can be exercised by hypothesis property tests in
isolation from the rest of the execution stack.
"""

from __future__ import annotations

import datetime as dt
from enum import StrEnum
from typing import Final

from pydantic import BaseModel, ConfigDict, Field

__all__ = [
    "IllegalTransitionError",
    "OrderState",
    "OrderStateMachine",
    "StateTransition",
]


class OrderState(StrEnum):
    """Lifecycle states. ``StrEnum`` so logs/JSON round-trip naturally."""

    NEW = "NEW"
    SENT = "SENT"
    ACK = "ACK"
    PARTIAL_FILL = "PARTIAL_FILL"
    FILLED = "FILLED"
    REJECTED = "REJECTED"
    CANCELED = "CANCELED"


_TERMINAL: Final[frozenset[OrderState]] = frozenset(
    {OrderState.FILLED, OrderState.REJECTED, OrderState.CANCELED}
)


# Adjacency map. Keep this static — the test suite enumerates every entry
# and asserts illegal moves raise.
_ALLOWED: Final[dict[OrderState, frozenset[OrderState]]] = {
    OrderState.NEW: frozenset({OrderState.SENT, OrderState.REJECTED}),
    OrderState.SENT: frozenset({OrderState.ACK, OrderState.REJECTED, OrderState.CANCELED}),
    OrderState.ACK: frozenset(
        {
            OrderState.PARTIAL_FILL,
            OrderState.FILLED,
            OrderState.CANCELED,
            OrderState.REJECTED,
        }
    ),
    OrderState.PARTIAL_FILL: frozenset(
        {OrderState.PARTIAL_FILL, OrderState.FILLED, OrderState.CANCELED}
    ),
    OrderState.FILLED: frozenset(),
    OrderState.REJECTED: frozenset(),
    OrderState.CANCELED: frozenset(),
}


class IllegalTransitionError(Exception):
    """A move from ``frm`` to ``to`` is not allowed by the state machine.

    This is always a programmer bug. Bubbles up; never caught silently.
    """

    def __init__(self, frm: OrderState, to: OrderState) -> None:
        super().__init__(f"illegal transition {frm.value} -> {to.value}")
        self.frm = frm
        self.to = to


class StateTransition(BaseModel):
    """One entry in the audit trail."""

    model_config = ConfigDict(frozen=True)

    frm: OrderState
    to: OrderState
    at: dt.datetime
    reason: str | None = None


class OrderStateMachine(BaseModel):
    """Strict order-lifecycle tracker with a full audit trail.

    The machine starts in :attr:`OrderState.NEW`. Use :meth:`transition` to
    move forward; :meth:`is_terminal` and :attr:`state` for inspection.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    state: OrderState = OrderState.NEW
    history: list[StateTransition] = Field(default_factory=list)

    def transition(
        self,
        to: OrderState,
        *,
        at: dt.datetime,
        reason: str | None = None,
    ) -> None:
        """Move to ``to``. Raises :class:`IllegalTransitionError` if disallowed.

        Parameters
        ----------
        to
            Target state.
        at
            Wall-clock timestamp; caller injects so tests can freeze it.
        reason
            Optional human-readable note (logged + persisted in audit trail).
        """
        if to not in _ALLOWED[self.state]:
            raise IllegalTransitionError(self.state, to)
        self.history.append(StateTransition(frm=self.state, to=to, at=at, reason=reason))
        self.state = to

    def is_terminal(self) -> bool:
        """Return ``True`` if the current state is FILLED / REJECTED / CANCELED."""
        return self.state in _TERMINAL

    def can_transition(self, to: OrderState) -> bool:
        """Return ``True`` if a move to ``to`` would succeed right now."""
        return to in _ALLOWED[self.state]


def legal_targets(state: OrderState) -> frozenset[OrderState]:
    """Pure helper: legal next states from ``state``. Useful for tests + UI hints."""
    return _ALLOWED[state]
