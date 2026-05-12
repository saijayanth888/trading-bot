"""Role definitions and Pydantic I/O schemas for the debate panel.

Per doc 05 rev2 (``docs/quanta-core-v4-rev2/05-RESEARCH-PARALLEL_AGENTS.md``)
the panel has five live roles + one out-of-band role:

============================================================================
Role             Model            Latency    Purpose
============================================================================
regime           hermes3:8b       ~1.7 s     macro/regime pre-screen
microstructure   hermes3:8b       ~1.7 s     book sanity pre-screen
bull             hermes3:70b      ~10 s      argue strongest LONG case (blind)
bear             hermes3:70b      ~10 s      argue strongest SHORT case (blind)
arbiter          hermes3:70b      ~4 s       synthesise rationale — NO VOTE
reflector        hermes3:8b       async      nightly outcome critique (OOB)
============================================================================

The arbiter is rationale-only; voting and gating belong to the deterministic
aggregator (``quanta_core.agents.aggregator``).

All prompt templates here are deliberately short placeholders. The
production prompts will be ported from ``stocks/shark/agents/`` by a
later wave — this module exposes the right seam (``prompt_template``)
so the swap is one assignment, no code refactor.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum, StrEnum
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

# ---------------------------------------------------------------------------
# Core enums / type aliases
# ---------------------------------------------------------------------------


class Direction(int, Enum):
    """Tri-state direction vote. Encoded as int for ``Σ w·d·c`` arithmetic."""

    LONG = 1
    FLAT = 0
    SHORT = -1


class RoleName(StrEnum):
    """The six well-known role names."""

    REGIME = "regime"
    MICROSTRUCTURE = "microstructure"
    BULL = "bull"
    BEAR = "bear"
    ARBITER = "arbiter"
    REFLECTOR = "reflector"


# Voting roles — the four panel roles whose AgentVote counts in the aggregator.
VOTING_ROLES: frozenset[RoleName] = frozenset(
    {RoleName.REGIME, RoleName.MICROSTRUCTURE, RoleName.BULL, RoleName.BEAR}
)


type ActionLiteral = Literal["LONG", "SHORT", "FLAT"]


class FailCode(StrEnum):
    """The 9 documented FLAT fail-codes from doc 05 rev2 §4 + §6.

    Every non-trade outcome lands on one of these. The reflector reads them
    overnight and the dashboard renders them in the deliberations card.
    """

    UNANIMOUS = "unanimous"  # not a fail — listed for completeness
    VETO_RISK_ENGINE = "veto_risk_engine"
    VETO_MICROSTRUCTURE = "veto_microstructure"
    VETO_QUORUM = "veto_quorum"
    NO_CONSENSUS = "no_consensus"
    LOW_CONVICTION = "low_conviction"
    PRE_SCREEN_VETO = "pre_screen_veto"
    REPOLL_NO_CONSENSUS = "repoll_no_consensus"
    ABSTAIN_DEFAULT_CLOSED = "abstain_default_closed"


# ---------------------------------------------------------------------------
# Inbound context (input to every role)
# ---------------------------------------------------------------------------


class AccountState(BaseModel):
    """Subset of account state the panel actually needs."""

    model_config = ConfigDict(frozen=True)

    equity_usd: float = Field(..., gt=0, description="Current account equity, USD.")
    drawdown_4w_pct: float = Field(
        ..., ge=0, le=1.0, description="Trailing-4-week drawdown as a fraction."
    )
    open_positions: int = Field(..., ge=0)
    cash_secured_ratio: float = Field(..., ge=0, le=1.0)


class SetupContext(BaseModel):
    """The state-snapshot input to the entire panel.

    Built by the StateAssembler at t=0 (Redis-cached, ~5–15 ms per doc 05 §3).
    Frozen so passing it to roles cannot mutate shared state.
    """

    model_config = ConfigDict(frozen=True)

    decision_id: UUID
    ts: datetime
    symbol: str
    state_snapshot_hash: str = Field(
        ..., min_length=8, description="Replayable snapshot hash (SHA256 hex prefix is fine)."
    )
    regime_features: dict[str, float] = Field(default_factory=dict)
    last_bars: list[dict[str, float]] = Field(
        default_factory=list,
        description="Most recent N OHLCV bars; shape is up to the caller.",
    )
    kb_bull_context: list[str] = Field(default_factory=list)
    kb_bear_context: list[str] = Field(default_factory=list)
    microstructure: dict[str, float] = Field(default_factory=dict)
    account_state: AccountState


# ---------------------------------------------------------------------------
# Voter output
# ---------------------------------------------------------------------------


class AgentVote(BaseModel):
    """The structured output every voting role must produce.

    ``direction`` is the discrete vote; ``conviction`` ∈ [0, 1] scales it; the
    aggregator computes ``Σ w_i · d_i · c_i``. ``rationale`` is free-form prose
    that the reflector reads overnight. ``evidence_keys`` cite KB / feature
    ids — they are the auditable backing for the rationale.
    """

    model_config = ConfigDict(frozen=True)

    role: RoleName
    direction: Direction
    conviction: float = Field(..., ge=0.0, le=1.0)
    horizon_min: int = Field(default=0, ge=0)
    rationale: str = Field(default="", max_length=4000)
    evidence_keys: tuple[str, ...] = Field(default_factory=tuple)
    abstained: bool = Field(
        default=False,
        description=(
            "Marks a vote that did not produce a usable opinion (timeout, schema fail). "
            "Quorum check counts abstentions as missing."
        ),
    )

    @field_validator("conviction")
    @classmethod
    def _abstain_zero_conviction(cls, v: float) -> float:
        return v


class ArbiterSynthesis(BaseModel):
    """Arbiter output — RATIONALE only. No vote, no direction.

    Doc 05 rev2 §4: "The arbiter LLM writes a synthesis paragraph — it does
    not vote. The vote is deterministic Python."
    """

    model_config = ConfigDict(frozen=True)

    synthesis_rationale: str = Field(..., min_length=1, max_length=8000)
    agreement_pattern: str = Field(default="", max_length=500)
    dissent_notes: str = Field(default="", max_length=2000)
    timed_out: bool = Field(default=False)


# ---------------------------------------------------------------------------
# Veto-gate state
# ---------------------------------------------------------------------------


class RiskState(BaseModel):
    """Output of the 50-ms Monte-Carlo risk gate at t=29 s."""

    model_config = ConfigDict(frozen=True)

    veto: bool
    drawdown_4w_pct: float = Field(..., ge=0, le=1.0)
    proposed_size_usd: float = Field(..., ge=0)
    expected_loss_usd: float = Field(..., ge=0)
    reason: str = Field(default="", max_length=500)


class MicroState(BaseModel):
    """Output of the microstructure final check at t=29 s."""

    model_config = ConfigDict(frozen=True)

    veto: bool
    spread_bps: float = Field(..., ge=0)
    spread_ratio_vs_median: float = Field(..., ge=0)
    depth_ratio_vs_target: float = Field(..., ge=0)
    halted: bool = False
    stale: bool = False
    reason: str = Field(default="", max_length=500)


# ---------------------------------------------------------------------------
# Persisted decision record (audit log row)
# ---------------------------------------------------------------------------


class RepollRecord(BaseModel):
    """Round-2 re-poll record. Present only when the re-poll branch fired."""

    model_config = ConfigDict(frozen=True)

    bull_r2: AgentVote
    bear_r2: AgentVote
    triggered_by: Literal["low_conviction"]


class DebateResult(BaseModel):
    """Top-level orchestrator output.

    This is the single row the persistence layer writes per deliberation,
    and the single object the strategy layer consumes to decide whether
    to send orders.
    """

    model_config = ConfigDict(frozen=True)

    decision_id: UUID
    ts: datetime
    symbol: str
    state_snapshot_hash: str
    action: ActionLiteral
    method: FailCode
    size_hint: float = Field(..., ge=0, le=1.0)
    weighted_score: float
    consensus: Literal["unanimous", "split", "no_quorum"]
    panel: tuple[AgentVote, ...]
    arbiter_synthesis: ArbiterSynthesis | None = None
    repoll: RepollRecord | None = None
    risk_engine_state: RiskState | None = None
    microstructure_state: MicroState | None = None
    panel_latency_ms: int = Field(..., ge=0)


# ---------------------------------------------------------------------------
# Per-role prompt templates (deliberately minimal — placeholders)
# ---------------------------------------------------------------------------


class RoleSpec(BaseModel):
    """Static spec for one role: which model to call, timeout, weight."""

    model_config = ConfigDict(frozen=True)

    name: RoleName
    model: str = Field(..., description="Ollama model tag, e.g. 'hermes3:70b'.")
    soft_timeout_s: float = Field(..., gt=0)
    hard_timeout_s: float = Field(..., gt=0)
    weight: float = Field(default=1.0, ge=0, description="0 for non-voting roles (arbiter, reflector).")
    prompt_template: str = Field(..., min_length=1)


# Default specs match the latency tiers in doc 05 §3.1.
# Prompts are intentionally short — production prompts get ported later.
DEFAULT_ROLE_SPECS: dict[RoleName, RoleSpec] = {
    RoleName.REGIME: RoleSpec(
        name=RoleName.REGIME,
        model="hermes3:8b",
        soft_timeout_s=2.5,
        hard_timeout_s=3.0,
        weight=1.0,
        prompt_template=(
            "You are the REGIME analyst. Given regime features and the last bars,"
            " answer: is the macro context favourable for a new entry? Return JSON"
            " {direction: LONG|SHORT|FLAT, conviction: 0..1, evidence_keys: [...]}."
        ),
    ),
    RoleName.MICROSTRUCTURE: RoleSpec(
        name=RoleName.MICROSTRUCTURE,
        model="hermes3:8b",
        soft_timeout_s=2.5,
        hard_timeout_s=3.0,
        weight=1.0,
        prompt_template=(
            "You are the MICROSTRUCTURE analyst. Given spread, depth and recent"
            " prints, answer: is the book healthy enough to act? Return JSON"
            " {direction: LONG|SHORT|FLAT, conviction: 0..1, evidence_keys: [...]}."
        ),
    ),
    RoleName.BULL: RoleSpec(
        name=RoleName.BULL,
        model="hermes3:70b",
        soft_timeout_s=12.0,
        hard_timeout_s=15.0,
        weight=1.0,
        prompt_template=(
            "Argue the strongest LONG case. Cite evidence_keys. If no LONG case"
            " exists, vote FLAT — do not manufacture one. Return JSON AgentVote."
        ),
    ),
    RoleName.BEAR: RoleSpec(
        name=RoleName.BEAR,
        model="hermes3:70b",
        soft_timeout_s=12.0,
        hard_timeout_s=15.0,
        weight=1.0,
        prompt_template=(
            "Argue the strongest SHORT case. Cite evidence_keys. If no SHORT case"
            " exists, vote FLAT — do not manufacture one. Return JSON AgentVote."
        ),
    ),
    RoleName.ARBITER: RoleSpec(
        name=RoleName.ARBITER,
        model="hermes3:70b",
        soft_timeout_s=6.0,
        hard_timeout_s=8.0,
        weight=0.0,  # arbiter does not vote
        prompt_template=(
            "Synthesize the four panel votes into a one-paragraph rationale."
            " Flag logical inconsistencies. DO NOT decide; the aggregator decides."
        ),
    ),
    RoleName.REFLECTOR: RoleSpec(
        name=RoleName.REFLECTOR,
        model="hermes3:8b",
        soft_timeout_s=30.0,
        hard_timeout_s=60.0,
        weight=0.0,  # reflector runs out of band
        prompt_template=(
            "Read the outcome of yesterday's decisions and write a critique:"
            " which role(s) dissented, which evidence_keys were noisy, what to"
            " re-weight next Sunday."
        ),
    ),
}


__all__ = [
    "DEFAULT_ROLE_SPECS",
    "VOTING_ROLES",
    "AccountState",
    "ActionLiteral",
    "AgentVote",
    "ArbiterSynthesis",
    "DebateResult",
    "Direction",
    "FailCode",
    "MicroState",
    "RepollRecord",
    "RiskState",
    "RoleName",
    "RoleSpec",
    "SetupContext",
]
