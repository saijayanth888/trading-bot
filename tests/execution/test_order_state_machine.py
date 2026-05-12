"""Order state machine — every legal edge + every illegal move."""

from __future__ import annotations

import datetime as dt

import pytest

from quanta_core.execution.order_state_machine import (
    IllegalTransitionError,
    OrderState,
    OrderStateMachine,
    StateTransition,
    legal_targets,
)

UTC = dt.UTC
NOW = dt.datetime(2026, 5, 12, 12, 0, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# Legal transitions
# ---------------------------------------------------------------------------


LEGAL_PATHS: list[list[OrderState]] = [
    [OrderState.NEW, OrderState.SENT, OrderState.ACK, OrderState.FILLED],
    [OrderState.NEW, OrderState.SENT, OrderState.ACK, OrderState.PARTIAL_FILL, OrderState.FILLED],
    [
        OrderState.NEW,
        OrderState.SENT,
        OrderState.ACK,
        OrderState.PARTIAL_FILL,
        OrderState.PARTIAL_FILL,
        OrderState.CANCELED,
    ],
    [OrderState.NEW, OrderState.SENT, OrderState.ACK, OrderState.CANCELED],
    [OrderState.NEW, OrderState.SENT, OrderState.ACK, OrderState.REJECTED],
    [OrderState.NEW, OrderState.SENT, OrderState.CANCELED],
    [OrderState.NEW, OrderState.SENT, OrderState.REJECTED],
    [OrderState.NEW, OrderState.REJECTED],
]


@pytest.mark.parametrize("path", LEGAL_PATHS)
def test_legal_paths(path: list[OrderState]) -> None:
    machine = OrderStateMachine()
    for target in path[1:]:
        machine.transition(target, at=NOW)
    assert machine.state == path[-1]
    assert len(machine.history) == len(path) - 1
    assert machine.is_terminal() == (
        path[-1]
        in (
            OrderState.FILLED,
            OrderState.REJECTED,
            OrderState.CANCELED,
        )
    )


# ---------------------------------------------------------------------------
# Illegal transitions — exhaustive
# ---------------------------------------------------------------------------


def _all_illegal_pairs() -> list[tuple[OrderState, OrderState]]:
    out: list[tuple[OrderState, OrderState]] = []
    for src in OrderState:
        legal = legal_targets(src)
        out.extend((src, dst) for dst in OrderState if dst not in legal)
    return out


@pytest.mark.parametrize("src,dst", _all_illegal_pairs())
def test_illegal_transitions_raise(src: OrderState, dst: OrderState) -> None:
    machine = OrderStateMachine(state=src)
    with pytest.raises(IllegalTransitionError) as exc_info:
        machine.transition(dst, at=NOW)
    assert exc_info.value.frm == src
    assert exc_info.value.to == dst


# ---------------------------------------------------------------------------
# Audit trail
# ---------------------------------------------------------------------------


def test_history_records_every_transition_with_reason() -> None:
    machine = OrderStateMachine()
    machine.transition(OrderState.SENT, at=NOW, reason="placed")
    machine.transition(OrderState.ACK, at=NOW, reason="venue_ack")
    machine.transition(OrderState.FILLED, at=NOW, reason="all_filled")
    assert [h.to for h in machine.history] == [
        OrderState.SENT,
        OrderState.ACK,
        OrderState.FILLED,
    ]
    assert [h.reason for h in machine.history] == ["placed", "venue_ack", "all_filled"]


def test_state_transition_is_frozen() -> None:
    t = StateTransition(frm=OrderState.NEW, to=OrderState.SENT, at=NOW, reason="x")
    with pytest.raises(Exception):  # noqa: B017 - frozen Pydantic raises ValidationError
        t.frm = OrderState.ACK  # type: ignore[misc]


def test_terminal_states_block_further_moves() -> None:
    for terminal in (OrderState.FILLED, OrderState.REJECTED, OrderState.CANCELED):
        machine = OrderStateMachine(state=terminal)
        for target in OrderState:
            with pytest.raises(IllegalTransitionError):
                machine.transition(target, at=NOW)


def test_can_transition_matches_transition_behaviour() -> None:
    for src in OrderState:
        machine = OrderStateMachine(state=src)
        for dst in OrderState:
            expected = dst in legal_targets(src)
            assert machine.can_transition(dst) is expected


def test_partial_to_partial_is_legal() -> None:
    """Two-step partial fill is the common Coinbase path."""
    machine = OrderStateMachine()
    machine.transition(OrderState.SENT, at=NOW)
    machine.transition(OrderState.ACK, at=NOW)
    machine.transition(OrderState.PARTIAL_FILL, at=NOW)
    machine.transition(OrderState.PARTIAL_FILL, at=NOW)
    machine.transition(OrderState.FILLED, at=NOW)
    assert machine.state == OrderState.FILLED


def test_illegal_transition_error_message() -> None:
    machine = OrderStateMachine()
    with pytest.raises(IllegalTransitionError) as exc_info:
        machine.transition(OrderState.FILLED, at=NOW)
    assert "NEW -> FILLED" in str(exc_info.value)
