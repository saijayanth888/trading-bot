"""DebateOrchestrator end-to-end tests.

Every test uses :class:`FakeOllama` (defined in ``conftest.py``) — no real
network calls. The orchestrator must produce a :class:`DebateResult` for
every input and never raise. Coverage walks all 9 fail codes per doc 05 rev2:

    1. unanimous                    (happy path)
    2. veto_risk_engine
    3. veto_microstructure
    4. veto_quorum
    5. no_consensus
    6. low_conviction
    7. pre_screen_veto
    8. repoll_no_consensus
    9. abstain_default_closed       (asserted via aggregator default path)
"""

from __future__ import annotations

from collections.abc import Callable

import pytest

from quanta_core.agents import (
    AgentVote,
    ArbiterSynthesis,
    BlindPanelConfig,
    DebateConfig,
    DebateOrchestrator,
    DefaultRoleRegistry,
    Direction,
    FailCode,
    MicroState,
    RiskState,
    RoleName,
    SetupContext,
)

# Import the conftest fakes by name (pytest auto-injects fixtures; we just
# pull the bare classes for direct construction in some paths).
from .conftest import FakeOllama, make_micro_gate, make_risk_gate

VoteFactory = Callable[..., AgentVote]


def _make_orch(
    fake_ollama: FakeOllama,
    *,
    risk_state: RiskState | None = None,
    micro_state: MicroState | None = None,
    risk_raises: BaseException | None = None,
    micro_raises: BaseException | None = None,
    config: DebateConfig | None = None,
) -> DebateOrchestrator:
    return DebateOrchestrator(
        fake_ollama,
        DefaultRoleRegistry(),
        make_risk_gate(risk_state, raise_exc=risk_raises),
        make_micro_gate(micro_state, raise_exc=micro_raises),
        config=config,
    )


def _seed_all_long(
    fake: FakeOllama, vote_factory: VoteFactory, conviction: float = 0.9
) -> None:
    for role in (RoleName.REGIME, RoleName.MICROSTRUCTURE, RoleName.BULL, RoleName.BEAR):
        fake.set_vote(role, vote_factory(role, direction=Direction.LONG, conviction=conviction))
    fake.set_arbiter(
        ArbiterSynthesis(synthesis_rationale="unanimous LONG, no inconsistencies.")
    )


# ---------------------------------------------------------------------------
# 1. Happy path — unanimous LONG
# ---------------------------------------------------------------------------


async def test_happy_path_unanimous_long(
    fake_ollama: FakeOllama, vote_factory: VoteFactory, setup_ctx: SetupContext
) -> None:
    _seed_all_long(fake_ollama, vote_factory)
    orch = _make_orch(fake_ollama)
    result = await orch.deliberate(setup_ctx)

    assert result.action == "LONG"
    assert result.method is FailCode.UNANIMOUS
    assert result.size_hint > 0
    assert result.consensus == "unanimous"
    assert len(result.panel) == 4
    assert result.arbiter_synthesis is not None
    assert result.repoll is None
    assert result.panel_latency_ms >= 0
    # Calls in expected order
    assert fake_ollama.calls == [
        "vote:regime",
        "vote:microstructure",
        "vote:bull",
        "vote:bear",
        "arbiter",
    ]


async def test_happy_path_unanimous_short(
    fake_ollama: FakeOllama, vote_factory: VoteFactory, setup_ctx: SetupContext
) -> None:
    for role in (RoleName.REGIME, RoleName.MICROSTRUCTURE, RoleName.BULL, RoleName.BEAR):
        fake_ollama.set_vote(
            role, vote_factory(role, direction=Direction.SHORT, conviction=0.9)
        )
    fake_ollama.set_arbiter(ArbiterSynthesis(synthesis_rationale="unanimous SHORT."))
    orch = _make_orch(fake_ollama)
    result = await orch.deliberate(setup_ctx)
    assert result.action == "SHORT"
    assert result.method is FailCode.UNANIMOUS


# ---------------------------------------------------------------------------
# 2. veto_risk_engine
# ---------------------------------------------------------------------------


async def test_fail_veto_risk_engine(
    fake_ollama: FakeOllama, vote_factory: VoteFactory, setup_ctx: SetupContext
) -> None:
    _seed_all_long(fake_ollama, vote_factory)
    risk_veto = RiskState(
        veto=True, drawdown_4w_pct=0.10, proposed_size_usd=1000, expected_loss_usd=300,
        reason="dd>8%",
    )
    orch = _make_orch(fake_ollama, risk_state=risk_veto)
    result = await orch.deliberate(setup_ctx)
    assert result.action == "FLAT"
    assert result.method is FailCode.VETO_RISK_ENGINE


async def test_fail_veto_risk_engine_when_gate_raises(
    fake_ollama: FakeOllama, vote_factory: VoteFactory, setup_ctx: SetupContext
) -> None:
    _seed_all_long(fake_ollama, vote_factory)
    orch = _make_orch(fake_ollama, risk_raises=RuntimeError("MC service down"))
    result = await orch.deliberate(setup_ctx)
    assert result.method is FailCode.VETO_RISK_ENGINE
    assert result.risk_engine_state is None


# ---------------------------------------------------------------------------
# 3. veto_microstructure
# ---------------------------------------------------------------------------


async def test_fail_veto_microstructure(
    fake_ollama: FakeOllama, vote_factory: VoteFactory, setup_ctx: SetupContext
) -> None:
    _seed_all_long(fake_ollama, vote_factory)
    micro_veto = MicroState(
        veto=True, spread_bps=30, spread_ratio_vs_median=10, depth_ratio_vs_target=0.1,
        reason="spread blown out",
    )
    orch = _make_orch(fake_ollama, micro_state=micro_veto)
    result = await orch.deliberate(setup_ctx)
    assert result.method is FailCode.VETO_MICROSTRUCTURE


async def test_fail_veto_micro_when_gate_raises(
    fake_ollama: FakeOllama, vote_factory: VoteFactory, setup_ctx: SetupContext
) -> None:
    _seed_all_long(fake_ollama, vote_factory)
    orch = _make_orch(fake_ollama, micro_raises=RuntimeError("feed stale"))
    result = await orch.deliberate(setup_ctx)
    assert result.method is FailCode.VETO_MICROSTRUCTURE
    assert result.microstructure_state is None


# ---------------------------------------------------------------------------
# 4. veto_quorum — bull times out
# ---------------------------------------------------------------------------


async def test_fail_veto_quorum_bull_timeout(
    fake_ollama: FakeOllama, vote_factory: VoteFactory, setup_ctx: SetupContext
) -> None:
    fake_ollama.set_vote(
        RoleName.REGIME, vote_factory(RoleName.REGIME, direction=Direction.LONG)
    )
    fake_ollama.set_vote(
        RoleName.MICROSTRUCTURE, vote_factory(RoleName.MICROSTRUCTURE, direction=Direction.LONG)
    )
    # Bull sleeps past its hard timeout
    fake_ollama.set_vote(RoleName.BULL, 1.0)  # sleep 1.0s — orch shrinks hard_timeout below
    fake_ollama.set_vote(
        RoleName.BEAR, vote_factory(RoleName.BEAR, direction=Direction.LONG)
    )
    fake_ollama.set_arbiter(ArbiterSynthesis(synthesis_rationale="bull timed out."))

    # Use a tiny hard timeout on bull/bear so the 1.0s sleep blows past it.
    from quanta_core.agents import DEFAULT_ROLE_SPECS
    from quanta_core.agents.roles import RoleSpec

    overrides = {
        RoleName.BULL: RoleSpec(
            **{
                **DEFAULT_ROLE_SPECS[RoleName.BULL].model_dump(),
                "soft_timeout_s": 0.05,
                "hard_timeout_s": 0.10,
            }
        ),
    }
    registry = DefaultRoleRegistry(overrides=overrides)

    orch = DebateOrchestrator(
        fake_ollama,
        registry,
        make_risk_gate(),
        make_micro_gate(),
    )
    result = await orch.deliberate(setup_ctx)
    assert result.method is FailCode.VETO_QUORUM
    # Bull is now an abstain entry on the panel
    bull = next(v for v in result.panel if v.role is RoleName.BULL)
    assert bull.abstained is True


async def test_fail_veto_quorum_bull_raises(
    fake_ollama: FakeOllama, vote_factory: VoteFactory, setup_ctx: SetupContext
) -> None:
    fake_ollama.set_vote(
        RoleName.REGIME, vote_factory(RoleName.REGIME, direction=Direction.LONG)
    )
    fake_ollama.set_vote(
        RoleName.MICROSTRUCTURE, vote_factory(RoleName.MICROSTRUCTURE, direction=Direction.LONG)
    )
    fake_ollama.set_vote(RoleName.BULL, RuntimeError("schema validation fail"))
    fake_ollama.set_vote(
        RoleName.BEAR, vote_factory(RoleName.BEAR, direction=Direction.LONG)
    )
    fake_ollama.set_arbiter(ArbiterSynthesis(synthesis_rationale="bull schema fail."))
    orch = _make_orch(fake_ollama)
    result = await orch.deliberate(setup_ctx)
    assert result.method is FailCode.VETO_QUORUM


# ---------------------------------------------------------------------------
# 5. no_consensus
# ---------------------------------------------------------------------------


async def test_fail_no_consensus_bull_long_bear_short(
    fake_ollama: FakeOllama, vote_factory: VoteFactory, setup_ctx: SetupContext
) -> None:
    fake_ollama.set_vote(
        RoleName.REGIME, vote_factory(RoleName.REGIME, direction=Direction.LONG)
    )
    fake_ollama.set_vote(
        RoleName.MICROSTRUCTURE, vote_factory(RoleName.MICROSTRUCTURE, direction=Direction.LONG)
    )
    fake_ollama.set_vote(
        RoleName.BULL, vote_factory(RoleName.BULL, direction=Direction.LONG)
    )
    fake_ollama.set_vote(
        RoleName.BEAR, vote_factory(RoleName.BEAR, direction=Direction.SHORT)
    )
    fake_ollama.set_arbiter(ArbiterSynthesis(synthesis_rationale="split panel."))
    orch = _make_orch(fake_ollama)
    result = await orch.deliberate(setup_ctx)
    assert result.method is FailCode.NO_CONSENSUS
    assert result.consensus == "split"


# ---------------------------------------------------------------------------
# 6. low_conviction (no re-poll)
# ---------------------------------------------------------------------------


async def test_fail_low_conviction(
    fake_ollama: FakeOllama, vote_factory: VoteFactory, setup_ctx: SetupContext
) -> None:
    _seed_all_long(fake_ollama, vote_factory, conviction=0.3)  # score = 1.2 < 1.5
    orch = _make_orch(fake_ollama)
    result = await orch.deliberate(setup_ctx)
    assert result.method is FailCode.LOW_CONVICTION
    assert result.size_hint == 0.0


# ---------------------------------------------------------------------------
# 7. pre_screen_veto
# ---------------------------------------------------------------------------


async def test_fail_pre_screen_veto_regime_flat_high_conviction(
    fake_ollama: FakeOllama, vote_factory: VoteFactory, setup_ctx: SetupContext
) -> None:
    fake_ollama.set_vote(
        RoleName.REGIME,
        vote_factory(RoleName.REGIME, direction=Direction.FLAT, conviction=0.9),
    )
    # micro never gets called -- only one item queued, but if a second call
    # is attempted, the FakeOllama raises AssertionError. So we have to seed
    # micro too; the orch will call regime then micro before deciding to abort.
    fake_ollama.set_vote(
        RoleName.MICROSTRUCTURE, vote_factory(RoleName.MICROSTRUCTURE, direction=Direction.LONG)
    )
    orch = _make_orch(fake_ollama)
    result = await orch.deliberate(setup_ctx)
    assert result.method is FailCode.PRE_SCREEN_VETO
    # Bull and bear were NEVER called — abort fires at t=4s
    assert "vote:bull" not in fake_ollama.calls
    assert "vote:bear" not in fake_ollama.calls


async def test_fail_pre_screen_veto_micro_flat_high_conviction(
    fake_ollama: FakeOllama, vote_factory: VoteFactory, setup_ctx: SetupContext
) -> None:
    fake_ollama.set_vote(
        RoleName.REGIME, vote_factory(RoleName.REGIME, direction=Direction.LONG)
    )
    fake_ollama.set_vote(
        RoleName.MICROSTRUCTURE,
        vote_factory(RoleName.MICROSTRUCTURE, direction=Direction.FLAT, conviction=0.8),
    )
    orch = _make_orch(fake_ollama)
    result = await orch.deliberate(setup_ctx)
    assert result.method is FailCode.PRE_SCREEN_VETO


async def test_fail_pre_screen_veto_low_conviction_flat_does_not_abort(
    fake_ollama: FakeOllama, vote_factory: VoteFactory, setup_ctx: SetupContext
) -> None:
    """Regime FLAT but conviction < 0.6 should NOT abort the pre-screen.

    It will still ultimately end up FLAT because panel-FLAT-unanimity is
    not consensus, but it must reach the 70B stage first.
    """
    fake_ollama.set_vote(
        RoleName.REGIME,
        vote_factory(RoleName.REGIME, direction=Direction.FLAT, conviction=0.3),
    )
    fake_ollama.set_vote(
        RoleName.MICROSTRUCTURE, vote_factory(RoleName.MICROSTRUCTURE, direction=Direction.LONG)
    )
    fake_ollama.set_vote(
        RoleName.BULL, vote_factory(RoleName.BULL, direction=Direction.LONG)
    )
    fake_ollama.set_vote(
        RoleName.BEAR, vote_factory(RoleName.BEAR, direction=Direction.LONG)
    )
    fake_ollama.set_arbiter(ArbiterSynthesis(synthesis_rationale="regime weak FLAT."))
    orch = _make_orch(fake_ollama)
    result = await orch.deliberate(setup_ctx)
    # regime is FLAT-low — panel votes split between LONG and FLAT → no_consensus
    assert result.method is FailCode.NO_CONSENSUS
    assert "vote:bull" in fake_ollama.calls  # confirms we got past pre-screen


async def test_fail_pre_screen_veto_when_regime_call_errors(
    fake_ollama: FakeOllama, vote_factory: VoteFactory, setup_ctx: SetupContext
) -> None:
    fake_ollama.set_vote(RoleName.REGIME, RuntimeError("ollama down"))
    fake_ollama.set_vote(
        RoleName.MICROSTRUCTURE, vote_factory(RoleName.MICROSTRUCTURE, direction=Direction.LONG)
    )
    orch = _make_orch(fake_ollama)
    result = await orch.deliberate(setup_ctx)
    assert result.method is FailCode.PRE_SCREEN_VETO


# ---------------------------------------------------------------------------
# 8. repoll_no_consensus — round 2 still doesn't agree
# ---------------------------------------------------------------------------


async def test_repoll_succeeds_when_round2_unanimous_above_threshold(
    fake_ollama: FakeOllama, vote_factory: VoteFactory, setup_ctx: SetupContext
) -> None:
    # Round 1: lukewarm-unanimous LONG (conviction 0.3 → score 1.2 below 1.5)
    _seed_all_long(fake_ollama, vote_factory, conviction=0.3)
    # Round 2: bull + bear both raise conviction
    fake_ollama.set_vote(
        RoleName.BULL, vote_factory(RoleName.BULL, direction=Direction.LONG, conviction=0.9)
    )
    fake_ollama.set_vote(
        RoleName.BEAR, vote_factory(RoleName.BEAR, direction=Direction.LONG, conviction=0.9)
    )

    config = DebateConfig(
        blind_panel=BlindPanelConfig(enable_repoll_for_low_conviction=True)
    )
    orch = _make_orch(fake_ollama, config=config)
    result = await orch.deliberate(setup_ctx)
    assert result.action == "LONG"
    assert result.method is FailCode.UNANIMOUS
    assert result.repoll is not None
    assert result.repoll.bull_r2.conviction == pytest.approx(0.9)


async def test_repoll_no_consensus_when_round2_diverges(
    fake_ollama: FakeOllama, vote_factory: VoteFactory, setup_ctx: SetupContext
) -> None:
    _seed_all_long(fake_ollama, vote_factory, conviction=0.3)
    # Round 2: bull doubles down LONG, bear flips SHORT
    fake_ollama.set_vote(
        RoleName.BULL, vote_factory(RoleName.BULL, direction=Direction.LONG, conviction=0.9)
    )
    fake_ollama.set_vote(
        RoleName.BEAR, vote_factory(RoleName.BEAR, direction=Direction.SHORT, conviction=0.9)
    )

    config = DebateConfig(
        blind_panel=BlindPanelConfig(enable_repoll_for_low_conviction=True)
    )
    orch = _make_orch(fake_ollama, config=config)
    result = await orch.deliberate(setup_ctx)
    assert result.action == "FLAT"
    assert result.method is FailCode.REPOLL_NO_CONSENSUS
    assert result.repoll is not None


async def test_repoll_still_low_conviction_maps_to_repoll_no_consensus(
    fake_ollama: FakeOllama, vote_factory: VoteFactory, setup_ctx: SetupContext
) -> None:
    _seed_all_long(fake_ollama, vote_factory, conviction=0.3)
    # Round 2: still lukewarm
    fake_ollama.set_vote(
        RoleName.BULL, vote_factory(RoleName.BULL, direction=Direction.LONG, conviction=0.3)
    )
    fake_ollama.set_vote(
        RoleName.BEAR, vote_factory(RoleName.BEAR, direction=Direction.LONG, conviction=0.3)
    )
    config = DebateConfig(
        blind_panel=BlindPanelConfig(enable_repoll_for_low_conviction=True)
    )
    orch = _make_orch(fake_ollama, config=config)
    result = await orch.deliberate(setup_ctx)
    assert result.method is FailCode.REPOLL_NO_CONSENSUS


# ---------------------------------------------------------------------------
# 9. abstain_default_closed — represented via the orchestrator's hard-deadline path
# ---------------------------------------------------------------------------


async def test_hard_deadline_aborts_to_veto_quorum(
    fake_ollama: FakeOllama, vote_factory: VoteFactory, setup_ctx: SetupContext
) -> None:
    """When the WHOLE pipeline blows the hard deadline, we surface VETO_QUORUM.

    abstain_default_closed is the bottom-of-ladder fallback in the aggregator;
    this orchestrator-level test asserts the wrapping ``asyncio.wait_for``
    converts a runaway pipeline into a FLAT result rather than an exception.
    """
    # Stage everything but make regime sleep 2s; hard deadline is 0.5s.
    fake_ollama.set_vote(RoleName.REGIME, 2.0)
    fake_ollama.set_vote(
        RoleName.MICROSTRUCTURE, vote_factory(RoleName.MICROSTRUCTURE, direction=Direction.LONG)
    )
    config = DebateConfig(soft_deadline_s=0.3, hard_deadline_s=0.5)
    orch = _make_orch(fake_ollama, config=config)
    result = await orch.deliberate(setup_ctx)
    assert result.action == "FLAT"
    assert result.method is FailCode.VETO_QUORUM


# ---------------------------------------------------------------------------
# Arbiter timeout — should NOT block the decision (rationale-only)
# ---------------------------------------------------------------------------


async def test_arbiter_timeout_does_not_block_unanimous_trade(
    fake_ollama: FakeOllama, vote_factory: VoteFactory, setup_ctx: SetupContext
) -> None:
    for role in (RoleName.REGIME, RoleName.MICROSTRUCTURE, RoleName.BULL, RoleName.BEAR):
        fake_ollama.set_vote(
            role, vote_factory(role, direction=Direction.LONG, conviction=0.9)
        )
    fake_ollama.set_arbiter(RuntimeError("arbiter crashed"))

    orch = _make_orch(fake_ollama)
    result = await orch.deliberate(setup_ctx)
    assert result.action == "LONG"
    assert result.method is FailCode.UNANIMOUS
    assert result.arbiter_synthesis is None


# ---------------------------------------------------------------------------
# Defensive — vote returned with wrong role becomes abstain
# ---------------------------------------------------------------------------


async def test_wrong_role_in_vote_is_treated_as_abstain(
    fake_ollama: FakeOllama, vote_factory: VoteFactory, setup_ctx: SetupContext
) -> None:
    # Regime returns a vote tagged as BULL — schema validation drops it.
    fake_ollama.set_vote(RoleName.REGIME, vote_factory(RoleName.BULL, direction=Direction.LONG))
    fake_ollama.set_vote(
        RoleName.MICROSTRUCTURE, vote_factory(RoleName.MICROSTRUCTURE, direction=Direction.LONG)
    )
    orch = _make_orch(fake_ollama)
    result = await orch.deliberate(setup_ctx)
    # regime abstains → pre-screen abort (since pre-screen abstain is fail-closed)
    assert result.method is FailCode.PRE_SCREEN_VETO


# ---------------------------------------------------------------------------
# Network-call discipline — no real network ever touched
# ---------------------------------------------------------------------------


async def test_fake_ollama_only_no_real_calls(
    fake_ollama: FakeOllama,
    vote_factory: VoteFactory,
    setup_ctx: SetupContext,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Whatever happens, the only thing called is the injected FakeOllama."""
    _seed_all_long(fake_ollama, vote_factory)
    orch = _make_orch(fake_ollama)
    # Patch socket to PROVE we don't touch the network.
    import socket

    def _no_net(*args: object, **kwargs: object) -> object:
        raise AssertionError("Test attempted to open a real socket")

    monkeypatch.setattr(socket, "socket", _no_net)
    result = await orch.deliberate(setup_ctx)
    assert result.action == "LONG"
