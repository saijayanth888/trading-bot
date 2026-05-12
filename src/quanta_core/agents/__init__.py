"""Quanta Core agents — 30-second deliberate debate panel.

Public API:

* :class:`DebateOrchestrator` — coordinates the pipeline.
* :class:`DefaultRoleRegistry` — static role specs from doc 05 rev2.
* :class:`DebateConfig` / :class:`AggregatorConfig` / :class:`BlindPanelConfig`
  — tunables.
* :class:`SetupContext`, :class:`AgentVote`, :class:`DebateResult`,
  :class:`FailCode`, :class:`Direction`, :class:`RoleName` — wire types.
* :func:`aggregate` — pure-Python deterministic aggregator.
* :func:`build_round1_prompt` / :func:`build_round2_prompt` /
  :func:`build_arbiter_prompt` — prompt assembly.

See ``docs/quanta-core-v4-rev2/05-RESEARCH-PARALLEL_AGENTS.md`` for the
full spec.
"""

from __future__ import annotations

from .aggregator import (
    DEFAULT_LOW_CONVICTION_THRESHOLD,
    DEFAULT_QUORUM,
    DEFAULT_SCORE_FULL_SIZE,
    AggregatorConfig,
    AggregatorDecision,
    aggregate,
)
from .blind_panel import (
    BlindPanelConfig,
    build_arbiter_prompt,
    build_round1_prompt,
    build_round2_prompt,
)
from .debate import (
    DebateConfig,
    DebateOrchestrator,
    DefaultRoleRegistry,
    MicroGateCallable,
    OllamaClient,
    RiskGateCallable,
    RoleRegistry,
)
from .roles import (
    DEFAULT_ROLE_SPECS,
    VOTING_ROLES,
    AccountState,
    ActionLiteral,
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

__all__ = [
    "DEFAULT_LOW_CONVICTION_THRESHOLD",
    "DEFAULT_QUORUM",
    "DEFAULT_ROLE_SPECS",
    "DEFAULT_SCORE_FULL_SIZE",
    "VOTING_ROLES",
    "AccountState",
    "ActionLiteral",
    "AgentVote",
    "AggregatorConfig",
    "AggregatorDecision",
    "ArbiterSynthesis",
    "BlindPanelConfig",
    "DebateConfig",
    "DebateOrchestrator",
    "DebateResult",
    "DefaultRoleRegistry",
    "Direction",
    "FailCode",
    "MicroGateCallable",
    "MicroState",
    "OllamaClient",
    "RepollRecord",
    "RiskGateCallable",
    "RiskState",
    "RoleName",
    "RoleRegistry",
    "RoleSpec",
    "SetupContext",
    "aggregate",
    "build_arbiter_prompt",
    "build_round1_prompt",
    "build_round2_prompt",
]
