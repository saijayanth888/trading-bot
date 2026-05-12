"""Aggregator tests — every ladder step + every legal vote combo.

Each test names exactly one ladder step. Combined coverage walks the full
5-step ladder + the unanimous trade path.
"""

from __future__ import annotations

from collections.abc import Callable

import pytest

from quanta_core.agents import (
    AggregatorConfig,
    Direction,
    FailCode,
    MicroState,
    RiskState,
    RoleName,
    aggregate,
)
from quanta_core.agents.aggregator import (
    DEFAULT_LOW_CONVICTION_THRESHOLD,
    DEFAULT_SCORE_FULL_SIZE,
)
from quanta_core.agents.roles import AgentVote

VoteFactory = Callable[..., AgentVote]


# ---------------------------------------------------------------------------
# Tiny builders so the test bodies stay short
# ---------------------------------------------------------------------------


def _ok_risk() -> RiskState:
    return RiskState(
        veto=False, drawdown_4w_pct=0.01, proposed_size_usd=1000, expected_loss_usd=10
    )


def _veto_risk(reason: str = "drawdown") -> RiskState:
    return RiskState(
        veto=True, drawdown_4w_pct=0.10, proposed_size_usd=1000, expected_loss_usd=200,
        reason=reason,
    )


def _ok_micro() -> MicroState:
    return MicroState(
        veto=False, spread_bps=1.0, spread_ratio_vs_median=1.0, depth_ratio_vs_target=2.0
    )


def _veto_micro(reason: str = "spread") -> MicroState:
    return MicroState(
        veto=True, spread_bps=20.0, spread_ratio_vs_median=5.0, depth_ratio_vs_target=0.1,
        reason=reason,
    )


def _panel_all(
    factory: VoteFactory,
    direction: Direction,
    conviction: float = 0.8,
) -> tuple[AgentVote, ...]:
    return tuple(
        factory(role, direction=direction, conviction=conviction)
        for role in (RoleName.REGIME, RoleName.MICROSTRUCTURE, RoleName.BULL, RoleName.BEAR)
    )


# ---------------------------------------------------------------------------
# Ladder step 1 — Risk veto
# ---------------------------------------------------------------------------


def test_ladder_step1_risk_veto_overrides_unanimous_long(vote_factory: VoteFactory) -> None:
    panel = _panel_all(vote_factory, Direction.LONG)
    d = aggregate(panel, risk_state=_veto_risk("dd>8%"), micro_state=_ok_micro())
    assert d.action == "FLAT"
    assert d.method is FailCode.VETO_RISK_ENGINE
    assert d.size_hint == 0.0
    assert "dd>8%" in d.rationale


def test_ladder_step1_risk_state_missing_is_veto(vote_factory: VoteFactory) -> None:
    panel = _panel_all(vote_factory, Direction.LONG)
    d = aggregate(panel, risk_state=None, micro_state=_ok_micro())
    assert d.method is FailCode.VETO_RISK_ENGINE


# ---------------------------------------------------------------------------
# Ladder step 2 — Microstructure veto
# ---------------------------------------------------------------------------


def test_ladder_step2_micro_veto_overrides_unanimous_short(vote_factory: VoteFactory) -> None:
    panel = _panel_all(vote_factory, Direction.SHORT)
    d = aggregate(panel, risk_state=_ok_risk(), micro_state=_veto_micro("halt"))
    assert d.method is FailCode.VETO_MICROSTRUCTURE
    assert "halt" in d.rationale


def test_ladder_step2_micro_state_missing_is_veto(vote_factory: VoteFactory) -> None:
    panel = _panel_all(vote_factory, Direction.LONG)
    d = aggregate(panel, risk_state=_ok_risk(), micro_state=None)
    assert d.method is FailCode.VETO_MICROSTRUCTURE


def test_ladder_step2_risk_evaluated_before_micro(vote_factory: VoteFactory) -> None:
    """When BOTH risk and micro veto, the risk veto wins (ladder order)."""
    panel = _panel_all(vote_factory, Direction.LONG)
    d = aggregate(panel, risk_state=_veto_risk(), micro_state=_veto_micro())
    assert d.method is FailCode.VETO_RISK_ENGINE


# ---------------------------------------------------------------------------
# Ladder step 3 — Quorum
# ---------------------------------------------------------------------------


def test_ladder_step3_missing_bull_is_quorum_fail(vote_factory: VoteFactory) -> None:
    panel = (
        vote_factory(RoleName.REGIME, direction=Direction.LONG),
        vote_factory(RoleName.MICROSTRUCTURE, direction=Direction.LONG),
        vote_factory(RoleName.BEAR, direction=Direction.LONG),
    )
    d = aggregate(panel, risk_state=_ok_risk(), micro_state=_ok_micro())
    assert d.method is FailCode.VETO_QUORUM
    assert "3 of 4" in d.rationale


def test_ladder_step3_abstain_counts_as_quorum_fail(vote_factory: VoteFactory) -> None:
    panel = (
        vote_factory(RoleName.REGIME, direction=Direction.LONG, conviction=0.9),
        vote_factory(RoleName.MICROSTRUCTURE, direction=Direction.LONG, conviction=0.9),
        vote_factory(RoleName.BULL, direction=Direction.LONG, conviction=0.9),
        vote_factory(RoleName.BEAR, direction=Direction.FLAT, conviction=0.0, abstained=True),
    )
    d = aggregate(panel, risk_state=_ok_risk(), micro_state=_ok_micro())
    assert d.method is FailCode.VETO_QUORUM


def test_ladder_step3_custom_quorum_passes_with_three(vote_factory: VoteFactory) -> None:
    panel = (
        vote_factory(RoleName.REGIME, direction=Direction.LONG),
        vote_factory(RoleName.MICROSTRUCTURE, direction=Direction.LONG),
        vote_factory(RoleName.BULL, direction=Direction.LONG),
        # bear abstains
        vote_factory(RoleName.BEAR, direction=Direction.FLAT, conviction=0.0, abstained=True),
    )
    cfg = AggregatorConfig(quorum=3)
    d = aggregate(panel, risk_state=_ok_risk(), micro_state=_ok_micro(), config=cfg)
    # Three LONG with default conviction 0.8 -> score = 2.4, above 1.5 threshold
    assert d.action == "LONG"
    assert d.method is FailCode.UNANIMOUS


# ---------------------------------------------------------------------------
# Ladder step 4 — Unanimity
# ---------------------------------------------------------------------------


def test_ladder_step4_bull_long_bear_short_no_consensus(vote_factory: VoteFactory) -> None:
    panel = (
        vote_factory(RoleName.REGIME, direction=Direction.LONG),
        vote_factory(RoleName.MICROSTRUCTURE, direction=Direction.LONG),
        vote_factory(RoleName.BULL, direction=Direction.LONG),
        vote_factory(RoleName.BEAR, direction=Direction.SHORT),
    )
    d = aggregate(panel, risk_state=_ok_risk(), micro_state=_ok_micro())
    assert d.method is FailCode.NO_CONSENSUS
    assert d.consensus == "split"


def test_ladder_step4_panel_flat_unanimity_is_no_consensus(vote_factory: VoteFactory) -> None:
    """All four FLAT should NOT trade — FLAT-as-unanimity is treated as
    no-consensus so the loop fails closed rather than 'unanimously do nothing'.
    """
    panel = _panel_all(vote_factory, Direction.FLAT)
    d = aggregate(panel, risk_state=_ok_risk(), micro_state=_ok_micro())
    assert d.action == "FLAT"
    assert d.method is FailCode.NO_CONSENSUS


# ---------------------------------------------------------------------------
# Ladder step 5 — Low conviction
# ---------------------------------------------------------------------------


def test_ladder_step5_low_conviction_returns_flat(vote_factory: VoteFactory) -> None:
    # 4 × 0.3 × 1.0 = 1.2, below the default 1.5 threshold
    panel = _panel_all(vote_factory, Direction.LONG, conviction=0.3)
    d = aggregate(panel, risk_state=_ok_risk(), micro_state=_ok_micro())
    assert d.method is FailCode.LOW_CONVICTION
    assert d.consensus == "unanimous"
    assert d.size_hint == 0.0


def test_ladder_step5_low_conviction_triggers_repoll_when_allowed(
    vote_factory: VoteFactory,
) -> None:
    panel = _panel_all(vote_factory, Direction.LONG, conviction=0.3)
    d = aggregate(
        panel,
        risk_state=_ok_risk(),
        micro_state=_ok_micro(),
        allow_repoll=True,
    )
    assert d.trigger_repoll is True
    assert d.action == "FLAT"
    assert d.method is FailCode.LOW_CONVICTION


def test_ladder_step5_threshold_boundary(vote_factory: VoteFactory) -> None:
    """A score exactly at the threshold should still fail the strict check."""
    # 4 × LONG with conviction exactly at threshold/4
    convict = DEFAULT_LOW_CONVICTION_THRESHOLD / 4.0
    panel = _panel_all(vote_factory, Direction.LONG, conviction=convict)
    d = aggregate(panel, risk_state=_ok_risk(), micro_state=_ok_micro())
    # |score| == threshold → strict-less-than fails → trade allowed
    assert d.action == "LONG"


# ---------------------------------------------------------------------------
# Ladder step 6 — Unanimous trade
# ---------------------------------------------------------------------------


def test_ladder_step6_unanimous_long(vote_factory: VoteFactory) -> None:
    panel = _panel_all(vote_factory, Direction.LONG, conviction=0.9)
    d = aggregate(panel, risk_state=_ok_risk(), micro_state=_ok_micro())
    assert d.action == "LONG"
    assert d.method is FailCode.UNANIMOUS
    # 4 × 1.0 × 0.9 = 3.6, |score|/3.0 → min(1.0, 1.2) = 1.0
    assert d.size_hint == 1.0
    assert d.consensus == "unanimous"


def test_ladder_step6_unanimous_short(vote_factory: VoteFactory) -> None:
    panel = _panel_all(vote_factory, Direction.SHORT, conviction=0.9)
    d = aggregate(panel, risk_state=_ok_risk(), micro_state=_ok_micro())
    assert d.action == "SHORT"
    assert d.method is FailCode.UNANIMOUS
    assert d.weighted_score < 0


def test_ladder_step6_size_hint_scales_with_score(vote_factory: VoteFactory) -> None:
    # Average conviction 0.5 → score = 2.0 → size_hint = 2/3
    panel = _panel_all(vote_factory, Direction.LONG, conviction=0.5)
    d = aggregate(panel, risk_state=_ok_risk(), micro_state=_ok_micro())
    assert d.action == "LONG"
    assert d.size_hint == pytest.approx(2.0 / DEFAULT_SCORE_FULL_SIZE, abs=1e-6)


def test_ladder_step6_custom_role_weights(vote_factory: VoteFactory) -> None:
    """Reflector-learned weights tilt the score but cannot flip a unanimous panel."""
    weights = {
        RoleName.REGIME: 2.0,
        RoleName.MICROSTRUCTURE: 0.5,
        RoleName.BULL: 1.5,
        RoleName.BEAR: 1.0,
    }
    cfg = AggregatorConfig(role_weights=weights)
    panel = _panel_all(vote_factory, Direction.LONG, conviction=0.9)
    d = aggregate(panel, risk_state=_ok_risk(), micro_state=_ok_micro(), config=cfg)
    assert d.action == "LONG"
    # (2 + 0.5 + 1.5 + 1.0) × 0.9 = 4.5
    assert d.weighted_score == pytest.approx(4.5, abs=1e-6)


# ---------------------------------------------------------------------------
# Vote-combo smoke test — every (direction × 4) combo
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "directions,expected_method",
    [
        ((Direction.LONG,) * 4, FailCode.UNANIMOUS),
        ((Direction.SHORT,) * 4, FailCode.UNANIMOUS),
        ((Direction.FLAT,) * 4, FailCode.NO_CONSENSUS),
        ((Direction.LONG, Direction.LONG, Direction.LONG, Direction.SHORT), FailCode.NO_CONSENSUS),
        ((Direction.LONG, Direction.LONG, Direction.LONG, Direction.FLAT), FailCode.NO_CONSENSUS),
        ((Direction.SHORT, Direction.SHORT, Direction.LONG, Direction.LONG), FailCode.NO_CONSENSUS),
        ((Direction.SHORT, Direction.LONG, Direction.SHORT, Direction.LONG), FailCode.NO_CONSENSUS),
    ],
)
def test_every_legal_direction_combo(
    vote_factory: VoteFactory,
    directions: tuple[Direction, ...],
    expected_method: FailCode,
) -> None:
    roles = (RoleName.REGIME, RoleName.MICROSTRUCTURE, RoleName.BULL, RoleName.BEAR)
    panel = tuple(
        vote_factory(r, direction=d, conviction=0.9) for r, d in zip(roles, directions, strict=False)
    )
    d = aggregate(panel, risk_state=_ok_risk(), micro_state=_ok_micro())
    assert d.method is expected_method


# ---------------------------------------------------------------------------
# Defensive: duplicate role votes — last wins (no crash)
# ---------------------------------------------------------------------------


def test_duplicate_role_last_wins(vote_factory: VoteFactory) -> None:
    panel = (
        vote_factory(RoleName.BULL, direction=Direction.LONG, conviction=0.5),
        vote_factory(RoleName.BULL, direction=Direction.SHORT, conviction=0.9),  # override
        vote_factory(RoleName.BEAR, direction=Direction.SHORT, conviction=0.9),
        vote_factory(RoleName.REGIME, direction=Direction.SHORT, conviction=0.9),
        vote_factory(RoleName.MICROSTRUCTURE, direction=Direction.SHORT, conviction=0.9),
    )
    d = aggregate(panel, risk_state=_ok_risk(), micro_state=_ok_micro())
    assert d.action == "SHORT"


# ---------------------------------------------------------------------------
# Defensive: non-voting roles in panel are ignored
# ---------------------------------------------------------------------------


def test_arbiter_in_panel_is_ignored(vote_factory: VoteFactory) -> None:
    panel = (
        vote_factory(RoleName.ARBITER, direction=Direction.SHORT, conviction=1.0),  # ignored
        *_panel_all(vote_factory, Direction.LONG, conviction=0.9),
    )
    d = aggregate(panel, risk_state=_ok_risk(), micro_state=_ok_micro())
    assert d.action == "LONG"
