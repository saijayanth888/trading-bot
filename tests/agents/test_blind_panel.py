"""Blind-panel pattern tests.

Round-1 prompts must NEVER mention another role's vote. Round-2 prompts
MUST include the round-1 panel + arbiter synthesis.
"""

from __future__ import annotations

from collections.abc import Callable

import pytest

from quanta_core.agents import (
    AgentVote,
    ArbiterSynthesis,
    Direction,
    RoleName,
    SetupContext,
)
from quanta_core.agents.blind_panel import (
    BlindPanelConfig,
    build_arbiter_prompt,
    build_round1_prompt,
    build_round2_prompt,
)

VoteFactory = Callable[..., AgentVote]


# ---------------------------------------------------------------------------
# Round-1 isolation
# ---------------------------------------------------------------------------


def test_round1_bull_does_not_see_bear_kb(setup_ctx: SetupContext) -> None:
    p = build_round1_prompt(RoleName.BULL, setup_ctx)
    assert "KB-bull-1" in p
    assert "KB-bear-1" not in p


def test_round1_bear_does_not_see_bull_kb(setup_ctx: SetupContext) -> None:
    p = build_round1_prompt(RoleName.BEAR, setup_ctx)
    assert "KB-bear-1" in p
    assert "KB-bull-1" not in p


def test_round1_regime_sees_neither_kb(setup_ctx: SetupContext) -> None:
    p = build_round1_prompt(RoleName.REGIME, setup_ctx)
    assert "KB-bull-1" not in p
    assert "KB-bear-1" not in p


def test_round1_micro_sees_neither_kb(setup_ctx: SetupContext) -> None:
    p = build_round1_prompt(RoleName.MICROSTRUCTURE, setup_ctx)
    assert "KB-bull-1" not in p
    assert "KB-bear-1" not in p


def test_round1_arbiter_role_rejected(setup_ctx: SetupContext) -> None:
    with pytest.raises(ValueError, match="Arbiter"):
        build_round1_prompt(RoleName.ARBITER, setup_ctx)


def test_round1_reflector_role_rejected(setup_ctx: SetupContext) -> None:
    with pytest.raises(ValueError, match="Reflector"):
        build_round1_prompt(RoleName.REFLECTOR, setup_ctx)


def test_round1_includes_state_snapshot_hash(setup_ctx: SetupContext) -> None:
    p = build_round1_prompt(RoleName.BULL, setup_ctx)
    assert setup_ctx.state_snapshot_hash in p


# ---------------------------------------------------------------------------
# Arbiter prompt — sees full panel
# ---------------------------------------------------------------------------


def test_arbiter_prompt_includes_every_panel_member(
    setup_ctx: SetupContext, vote_factory: VoteFactory
) -> None:
    panel = (
        vote_factory(RoleName.REGIME, direction=Direction.LONG),
        vote_factory(RoleName.MICROSTRUCTURE, direction=Direction.LONG),
        vote_factory(RoleName.BULL, direction=Direction.LONG),
        vote_factory(RoleName.BEAR, direction=Direction.LONG),
    )
    p = build_arbiter_prompt(setup_ctx, panel)
    for v in panel:
        assert v.role.value in p
        assert v.rationale[:50] in p


# ---------------------------------------------------------------------------
# Round-2 visibility
# ---------------------------------------------------------------------------


def test_round2_bull_sees_round1_panel_and_arbiter(
    setup_ctx: SetupContext,
    vote_factory: VoteFactory,
    arbiter_synthesis: ArbiterSynthesis,
) -> None:
    panel = (
        vote_factory(RoleName.REGIME, direction=Direction.LONG),
        vote_factory(RoleName.MICROSTRUCTURE, direction=Direction.LONG),
        vote_factory(RoleName.BULL, direction=Direction.LONG),
        vote_factory(RoleName.BEAR, direction=Direction.LONG),
    )
    p = build_round2_prompt(RoleName.BULL, setup_ctx, panel, arbiter_synthesis)
    # Round-2 prompt MUST surface round-1 votes
    for v in panel:
        assert v.role.value in p
    # And the arbiter synthesis
    assert arbiter_synthesis.synthesis_rationale[:30] in p
    # And the lukewarm-unanimous instruction
    assert "LUKEWARM" in p


def test_round2_rejects_non_adversarial_roles(
    setup_ctx: SetupContext,
    vote_factory: VoteFactory,
    arbiter_synthesis: ArbiterSynthesis,
) -> None:
    panel = (
        vote_factory(RoleName.REGIME, direction=Direction.LONG),
        vote_factory(RoleName.MICROSTRUCTURE, direction=Direction.LONG),
        vote_factory(RoleName.BULL, direction=Direction.LONG),
        vote_factory(RoleName.BEAR, direction=Direction.LONG),
    )
    for role in (RoleName.REGIME, RoleName.MICROSTRUCTURE, RoleName.ARBITER, RoleName.REFLECTOR):
        with pytest.raises(ValueError, match="bull/bear only"):
            build_round2_prompt(role, setup_ctx, panel, arbiter_synthesis)


def test_round2_arbiter_synthesis_optional(
    setup_ctx: SetupContext, vote_factory: VoteFactory
) -> None:
    panel = (
        vote_factory(RoleName.REGIME, direction=Direction.LONG),
        vote_factory(RoleName.MICROSTRUCTURE, direction=Direction.LONG),
        vote_factory(RoleName.BULL, direction=Direction.LONG),
        vote_factory(RoleName.BEAR, direction=Direction.LONG),
    )
    p = build_round2_prompt(RoleName.BEAR, setup_ctx, panel, arbiter=None)
    # No arbiter section, but everything else still present
    assert "Arbiter synthesis" not in p
    assert "LUKEWARM" in p


def test_blind_panel_config_defaults_repoll_off() -> None:
    cfg = BlindPanelConfig()
    assert cfg.enable_repoll_for_low_conviction is False
    assert cfg.max_rounds == 2
