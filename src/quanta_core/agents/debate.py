"""30-second parallel-deliberation orchestrator (doc 05 rev2 §3).

Pipeline (wall-clock relative to t=0 = "setup formed"):

    t=0   StateAssembler caches features (caller's responsibility).
    t=0   regime 8B pre-screen      (~1.7 s warm)
    t=2   microstructure 8B pre-screen (~1.7 s warm)
    t=4   PRE-SCREEN GATE — if either voted FLAT with conviction ≥ 0.6 → ABORT
    t=4   bull 70B                  (~10 s, single-load)
    t=14  bear 70B                  (~10 s, single-load)
    t=24  arbiter 70B               (~4 s, hot KV-cache)
    t=29  risk_engine MC + microstructure final check
    t=30  aggregator runs; commit or abort

This module owns sequencing, timeouts, and fail-closed defaults. Aggregation
lives in :mod:`quanta_core.agents.aggregator`; prompt assembly in
:mod:`quanta_core.agents.blind_panel`.

Concurrency primitive is :class:`asyncio.TaskGroup` (Python 3.11+) — per
validator finding P1-1, LangGraph is OUT.

The Ollama client is injected as an :class:`OllamaClient` protocol so unit
tests pass a mock that never touches the network.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from .aggregator import AggregatorConfig, aggregate
from .blind_panel import (
    BlindPanelConfig,
    build_arbiter_prompt,
    build_round1_prompt,
    build_round2_prompt,
)
from .roles import (
    DEFAULT_ROLE_SPECS,
    AgentVote,
    ArbiterSynthesis,
    DebateResult,
    Direction,
    FailCode,
    MicroState,
    RepollRecord,
    RiskState,
    RoleName,
    RoleSpec,
    SetupContext,
)

# ---------------------------------------------------------------------------
# Injected dependencies (Protocols → easy to mock in tests)
# ---------------------------------------------------------------------------


@runtime_checkable
class OllamaClient(Protocol):
    """Minimal Ollama-shaped client interface used by the orchestrator.

    Production wraps the real ``ollama`` Python SDK; tests pass a fake that
    returns canned ``AgentVote`` / ``ArbiterSynthesis`` instances.
    """

    async def vote(
        self,
        *,
        role: RoleName,
        model: str,
        prompt: str,
        timeout_s: float,
    ) -> AgentVote: ...

    async def arbiter(
        self,
        *,
        model: str,
        prompt: str,
        timeout_s: float,
    ) -> ArbiterSynthesis: ...


@runtime_checkable
class RoleRegistry(Protocol):
    """Source of :class:`RoleSpec` objects, keyed by role.

    Production loads from config; tests pass a dict-backed instance.
    """

    def spec(self, role: RoleName) -> RoleSpec: ...


# A risk-engine / micro-feed call. Both return the gate state at t=29 s.
RiskGateCallable = Callable[[SetupContext], Awaitable[RiskState]]
MicroGateCallable = Callable[[SetupContext], Awaitable[MicroState]]


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class DebateConfig:
    """Top-level orchestrator config. Defaults match doc 05 rev2 §3."""

    soft_deadline_s: float = 30.0
    hard_deadline_s: float = 45.0
    pre_screen_flat_conviction: float = 0.6
    aggregator: AggregatorConfig = field(default_factory=AggregatorConfig)
    blind_panel: BlindPanelConfig = field(default_factory=BlindPanelConfig)


# ---------------------------------------------------------------------------
# Built-in default registry — uses DEFAULT_ROLE_SPECS
# ---------------------------------------------------------------------------


class DefaultRoleRegistry:
    """Trivial registry backed by :data:`DEFAULT_ROLE_SPECS`."""

    def __init__(self, overrides: dict[RoleName, RoleSpec] | None = None) -> None:
        self._specs: dict[RoleName, RoleSpec] = dict(DEFAULT_ROLE_SPECS)
        if overrides:
            self._specs.update(overrides)

    def spec(self, role: RoleName) -> RoleSpec:
        return self._specs[role]


# ---------------------------------------------------------------------------
# The orchestrator
# ---------------------------------------------------------------------------


class DebateOrchestrator:
    """Coordinates one 30-second parallel deliberation.

    Use:

    >>> orch = DebateOrchestrator(client, registry, risk_gate, micro_gate)
    >>> result = await orch.deliberate(setup_context)

    The orchestrator is stateless across deliberations; one instance is safe
    to share across symbols and decisions.
    """

    def __init__(
        self,
        ollama_client: OllamaClient,
        registry: RoleRegistry,
        risk_gate: RiskGateCallable,
        micro_gate: MicroGateCallable,
        config: DebateConfig | None = None,
    ) -> None:
        self._client = ollama_client
        self._registry = registry
        self._risk_gate = risk_gate
        self._micro_gate = micro_gate
        self._config = config or DebateConfig()

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------

    async def deliberate(self, setup: SetupContext) -> DebateResult:
        """Run the full pipeline. Returns a :class:`DebateResult` always.

        On any infrastructure failure, the result has ``action=FLAT`` and a
        ``method`` chosen per doc 05 rev2 §6. The function never raises.
        """
        t0 = time.monotonic()
        try:
            return await asyncio.wait_for(
                self._run_pipeline(setup, t0),
                timeout=self._config.hard_deadline_s,
            )
        except TimeoutError:
            return self._abort(
                setup,
                FailCode.VETO_QUORUM,
                panel=(),
                rationale="Hard deadline exceeded.",
                t0=t0,
            )

    # ------------------------------------------------------------------
    # Pipeline
    # ------------------------------------------------------------------

    async def _run_pipeline(self, setup: SetupContext, t0: float) -> DebateResult:
        # --- t=0..4 s : 8B pre-screen (regime + micro, sequential per §3) ---
        regime_vote = await self._safe_vote(RoleName.REGIME, setup)
        micro_vote = await self._safe_vote(RoleName.MICROSTRUCTURE, setup)

        # --- t=4 s : PRE-SCREEN GATE -------------------------------------
        # If either pre-screen role abstained, that is an infra failure on
        # the cheap path — fail closed with pre_screen_veto.
        if regime_vote.abstained or micro_vote.abstained:
            return self._abort(
                setup,
                FailCode.PRE_SCREEN_VETO,
                panel=(regime_vote, micro_vote),
                rationale=(
                    f"Pre-screen abstention: regime.abstained={regime_vote.abstained},"
                    f" micro.abstained={micro_vote.abstained}"
                ),
                t0=t0,
            )

        if self._is_flat_veto(regime_vote) or self._is_flat_veto(micro_vote):
            return self._abort(
                setup,
                FailCode.PRE_SCREEN_VETO,
                panel=(regime_vote, micro_vote),
                rationale=(
                    f"Pre-screen FLAT veto: regime={regime_vote.direction.name}"
                    f"@{regime_vote.conviction:.2f}, micro={micro_vote.direction.name}"
                    f"@{micro_vote.conviction:.2f}"
                ),
                t0=t0,
            )

        # --- t=4..24 s : 70B bull → bear (sequential, single-load Ollama) ---
        bull_vote = await self._safe_vote(RoleName.BULL, setup)
        bear_vote = await self._safe_vote(RoleName.BEAR, setup)

        round1_panel: tuple[AgentVote, ...] = (
            regime_vote, micro_vote, bull_vote, bear_vote,
        )

        # --- t=24..28 s : arbiter synthesis (rationale only) -------------
        arbiter = await self._safe_arbiter(setup, round1_panel)

        # --- t=28..29 s : risk MC + microstructure final check -----------
        risk_state, micro_state = await self._run_gates(setup)

        # --- t=29..30 s : aggregator --------------------------------------
        decision = aggregate(
            round1_panel,
            risk_state=risk_state,
            micro_state=micro_state,
            config=self._config.aggregator,
            allow_repoll=self._config.blind_panel.enable_repoll_for_low_conviction,
        )

        # Optional round-2 re-poll on low-conviction unanimous.
        if decision.trigger_repoll:
            return await self._run_round2(
                setup,
                round1_panel=round1_panel,
                arbiter=arbiter,
                risk_state=risk_state,
                micro_state=micro_state,
                t0=t0,
            )

        return DebateResult(
            decision_id=setup.decision_id,
            ts=setup.ts,
            symbol=setup.symbol,
            state_snapshot_hash=setup.state_snapshot_hash,
            action=decision.action,
            method=decision.method,
            size_hint=decision.size_hint,
            weighted_score=decision.weighted_score,
            consensus=decision.consensus,
            panel=round1_panel,
            arbiter_synthesis=arbiter,
            repoll=None,
            risk_engine_state=risk_state,
            microstructure_state=micro_state,
            panel_latency_ms=self._elapsed_ms(t0),
        )

    # ------------------------------------------------------------------
    # Round-2 re-poll (feature-flagged)
    # ------------------------------------------------------------------

    async def _run_round2(
        self,
        setup: SetupContext,
        *,
        round1_panel: tuple[AgentVote, ...],
        arbiter: ArbiterSynthesis | None,
        risk_state: RiskState | None,
        micro_state: MicroState | None,
        t0: float,
    ) -> DebateResult:
        bull_r2 = await self._safe_vote_round2(
            RoleName.BULL, setup, round1_panel, arbiter
        )
        bear_r2 = await self._safe_vote_round2(
            RoleName.BEAR, setup, round1_panel, arbiter
        )

        # Build round-2 panel by replacing bull/bear with r2 versions; keep
        # regime + micro identical (they don't re-vote in round 2).
        regime = next(v for v in round1_panel if v.role is RoleName.REGIME)
        micro = next(v for v in round1_panel if v.role is RoleName.MICROSTRUCTURE)
        r2_panel: tuple[AgentVote, ...] = (regime, micro, bull_r2, bear_r2)

        decision = aggregate(
            r2_panel,
            risk_state=risk_state,
            micro_state=micro_state,
            config=self._config.aggregator,
            allow_repoll=False,  # never re-poll a re-poll
        )

        # If round 2 still doesn't pass, surface that with the dedicated code.
        method = decision.method
        if decision.action == "FLAT" and method == FailCode.NO_CONSENSUS or decision.action == "FLAT" and method == FailCode.LOW_CONVICTION:
            method = FailCode.REPOLL_NO_CONSENSUS

        return DebateResult(
            decision_id=setup.decision_id,
            ts=setup.ts,
            symbol=setup.symbol,
            state_snapshot_hash=setup.state_snapshot_hash,
            action=decision.action,
            method=method,
            size_hint=decision.size_hint,
            weighted_score=decision.weighted_score,
            consensus=decision.consensus,
            panel=r2_panel,
            arbiter_synthesis=arbiter,
            repoll=RepollRecord(
                bull_r2=bull_r2, bear_r2=bear_r2, triggered_by="low_conviction"
            ),
            risk_engine_state=risk_state,
            microstructure_state=micro_state,
            panel_latency_ms=self._elapsed_ms(t0),
        )

    # ------------------------------------------------------------------
    # Veto gates — concurrent (independent, no shared state)
    # ------------------------------------------------------------------

    async def _run_gates(
        self, setup: SetupContext
    ) -> tuple[RiskState | None, MicroState | None]:
        """Run risk + micro veto checks concurrently. Either may return None
        on infra failure (treated as a hard veto by the aggregator)."""
        risk_state: RiskState | None = None
        micro_state: MicroState | None = None

        async def _risk() -> None:
            nonlocal risk_state
            try:
                risk_state = await self._risk_gate(setup)
            except Exception:  # noqa: BLE001 — fail-closed by design
                risk_state = None

        async def _micro() -> None:
            nonlocal micro_state
            try:
                micro_state = await self._micro_gate(setup)
            except Exception:  # noqa: BLE001
                micro_state = None

        async with asyncio.TaskGroup() as tg:
            tg.create_task(_risk())
            tg.create_task(_micro())

        return risk_state, micro_state

    # ------------------------------------------------------------------
    # Safe LLM call wrappers — convert ALL failures into ABSTAIN votes
    # ------------------------------------------------------------------

    async def _safe_vote(self, role: RoleName, setup: SetupContext) -> AgentVote:
        spec = self._registry.spec(role)
        prompt = build_round1_prompt(role, setup, spec=spec)
        return await self._call_vote(role, spec, prompt)

    async def _safe_vote_round2(
        self,
        role: RoleName,
        setup: SetupContext,
        round1_panel: tuple[AgentVote, ...],
        arbiter: ArbiterSynthesis | None,
    ) -> AgentVote:
        spec = self._registry.spec(role)
        prompt = build_round2_prompt(role, setup, round1_panel, arbiter, spec=spec)
        return await self._call_vote(role, spec, prompt)

    async def _call_vote(
        self, role: RoleName, spec: RoleSpec, prompt: str
    ) -> AgentVote:
        try:
            vote = await asyncio.wait_for(
                self._client.vote(
                    role=role,
                    model=spec.model,
                    prompt=prompt,
                    timeout_s=spec.soft_timeout_s,
                ),
                timeout=spec.hard_timeout_s,
            )
        except (TimeoutError, Exception):  # noqa: BLE001
            return _abstain_vote(role)
        # Defensive: enforce role identity on the returned vote.
        if vote.role is not role:
            return _abstain_vote(role)
        return vote

    async def _safe_arbiter(
        self, setup: SetupContext, panel: tuple[AgentVote, ...]
    ) -> ArbiterSynthesis | None:
        spec = self._registry.spec(RoleName.ARBITER)
        prompt = build_arbiter_prompt(setup, panel, spec=spec)
        try:
            return await asyncio.wait_for(
                self._client.arbiter(
                    model=spec.model,
                    prompt=prompt,
                    timeout_s=spec.soft_timeout_s,
                ),
                timeout=spec.hard_timeout_s,
            )
        except (TimeoutError, Exception):  # noqa: BLE001
            # Arbiter is rationale-only — its absence does not block the
            # decision (per doc §6 row 5).
            return None

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _is_flat_veto(v: AgentVote) -> bool:
        return (
            v.direction is Direction.FLAT
            and v.conviction >= DEFAULT_PRE_SCREEN_FLAT_CONVICTION_FALLBACK
        )

    def _abort(
        self,
        setup: SetupContext,
        method: FailCode,
        panel: tuple[AgentVote, ...],
        rationale: str,
        t0: float,
    ) -> DebateResult:
        return DebateResult(
            decision_id=setup.decision_id,
            ts=setup.ts,
            symbol=setup.symbol,
            state_snapshot_hash=setup.state_snapshot_hash,
            action="FLAT",
            method=method,
            size_hint=0.0,
            weighted_score=0.0,
            consensus="no_quorum",
            panel=panel,
            arbiter_synthesis=None,
            repoll=None,
            risk_engine_state=None,
            microstructure_state=None,
            panel_latency_ms=self._elapsed_ms(t0),
        )

    @staticmethod
    def _elapsed_ms(t0: float) -> int:
        return int((time.monotonic() - t0) * 1000)


# Module-level constant so :meth:`DebateOrchestrator._is_flat_veto` can stay
# a staticmethod (and thus trivial to unit-test in isolation). The value
# matches :attr:`DebateConfig.pre_screen_flat_conviction`.
DEFAULT_PRE_SCREEN_FLAT_CONVICTION_FALLBACK: float = 0.6


def _abstain_vote(role: RoleName) -> AgentVote:
    """Construct a canonical ABSTAIN vote (used on LLM timeout / error)."""
    return AgentVote(
        role=role,
        direction=Direction.FLAT,
        conviction=0.0,
        rationale="abstain (timeout or error)",
        evidence_keys=(),
        abstained=True,
    )


__all__ = [
    "DEFAULT_PRE_SCREEN_FLAT_CONVICTION_FALLBACK",
    "DebateConfig",
    "DebateOrchestrator",
    "DefaultRoleRegistry",
    "MicroGateCallable",
    "OllamaClient",
    "RiskGateCallable",
    "RoleRegistry",
]
