"""Deterministic weighted aggregator + 5-step tie-break ladder.

Implements doc 05 rev2 §4. The arbiter LLM writes rationale; THIS module
decides. Pure Python, no I/O, no LLM calls — every line is auditable.

The ladder is a "are we certain enough to trade?" ladder, NOT a "find a way
to trade" ladder. Default at every step is FLAT.

Ladder order:
    1. Risk hard veto         -> FLAT, ``method = veto_risk_engine``
    2. Microstructure veto    -> FLAT, ``method = veto_microstructure``
    3. Quorum check           -> FLAT, ``method = veto_quorum``
    4. Unanimity check        -> FLAT, ``method = no_consensus``
    5. Low conviction         -> FLAT, ``method = low_conviction``
                                 (may trigger a single optional re-poll)
    6. Trade                  -> action = LONG|SHORT, ``method = unanimous``
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from .roles import (
    VOTING_ROLES,
    ActionLiteral,
    AgentVote,
    Direction,
    FailCode,
    MicroState,
    RiskState,
    RoleName,
)

# ---------------------------------------------------------------------------
# Tunables
# ---------------------------------------------------------------------------

#: Default quorum: all four voting roles must vote (non-abstain).
DEFAULT_QUORUM: int = 4

#: Default low-conviction threshold. ``|score|`` must exceed this to trade.
#: With 4 unit weights and convictions ≤ 1.0, max ``|score|`` is 4.0; the
#: default 1.5 corresponds to "average conviction ≥ 0.375 across the panel".
DEFAULT_LOW_CONVICTION_THRESHOLD: float = 1.5

#: ``size_hint = min(1.0, |score| / SCORE_FULL_SIZE)``. With weights=1 and
#: convictions=1 across the panel the max is 4.0; the doc default of 3.0
#: maps "very conviction-y unanimous" to ``size_hint = 1.0``.
DEFAULT_SCORE_FULL_SIZE: float = 3.0


@dataclass(frozen=True, slots=True)
class AggregatorConfig:
    """Knobs for the aggregator. All defaults match doc 05 rev2 §4."""

    quorum: int = DEFAULT_QUORUM
    low_conviction_threshold: float = DEFAULT_LOW_CONVICTION_THRESHOLD
    score_full_size: float = DEFAULT_SCORE_FULL_SIZE
    role_weights: dict[RoleName, float] | None = None

    def weight(self, role: RoleName) -> float:
        """Return the weight for ``role``; default 1.0 across the board."""
        if self.role_weights is None:
            return 1.0
        return self.role_weights.get(role, 1.0)


# ---------------------------------------------------------------------------
# Output type
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class AggregatorDecision:
    """Result of the aggregator.

    ``rationale`` is a short audit-friendly string explaining which ladder
    step fired. The persistence layer copies it into ``DecisionRecord.method``
    + the deliberations dashboard card.
    """

    action: ActionLiteral
    method: FailCode
    size_hint: float
    weighted_score: float
    consensus: Literal["unanimous", "split", "no_quorum"]
    rationale: str
    trigger_repoll: bool = False  # set when ladder step 5 fires + repoll is allowed


# ---------------------------------------------------------------------------
# Core function
# ---------------------------------------------------------------------------


def aggregate(
    panel: tuple[AgentVote, ...] | list[AgentVote],
    *,
    risk_state: RiskState | None,
    micro_state: MicroState | None,
    config: AggregatorConfig | None = None,
    allow_repoll: bool = False,
) -> AggregatorDecision:
    """Run the 5-step ladder over a panel and return a decision.

    The function is total: every legal input maps to exactly one
    :class:`AggregatorDecision`. It never raises on logical contradictions —
    instead it returns FLAT with the appropriate :class:`FailCode`.

    Args:
        panel: tuple of AgentVotes from the four voting roles (regime, micro,
            bull, bear). Extra roles are ignored. Missing roles count as
            quorum failures.
        risk_state: output of the 50-ms MC gate, or ``None`` if the gate
            was unreachable (treated as veto-FLAT per doc §6 row 6).
        micro_state: output of the microstructure final check, or ``None``
            if the feed was stale (treated as veto-FLAT).
        config: optional override; defaults to :data:`AggregatorConfig()`.
        allow_repoll: when True, ladder step 5 (low-conviction) sets
            ``trigger_repoll=True`` instead of returning FLAT directly. The
            orchestrator is responsible for actually running round-2 and
            calling :func:`aggregate` again with ``allow_repoll=False``.

    Returns:
        :class:`AggregatorDecision`.
    """
    cfg = config or AggregatorConfig()
    votes_by_role = _index_panel(panel)
    score = _weighted_score(votes_by_role, cfg)

    # Step 1 — Risk hard veto. Missing risk state is ALSO a veto.
    if risk_state is None:
        return _flat(
            FailCode.VETO_RISK_ENGINE,
            score,
            "no_quorum",
            "Risk engine state missing — fail closed.",
        )
    if risk_state.veto:
        return _flat(
            FailCode.VETO_RISK_ENGINE,
            score,
            "no_quorum",
            f"Risk engine vetoed: {risk_state.reason or 'no reason given'}",
        )

    # Step 2 — Microstructure hard veto. Missing micro state is ALSO a veto.
    if micro_state is None:
        return _flat(
            FailCode.VETO_MICROSTRUCTURE,
            score,
            "no_quorum",
            "Microstructure state missing — fail closed.",
        )
    if micro_state.veto:
        return _flat(
            FailCode.VETO_MICROSTRUCTURE,
            score,
            "no_quorum",
            f"Microstructure vetoed: {micro_state.reason or 'no reason given'}",
        )

    # Step 3 — Quorum check. Every voting role must have a non-abstain vote.
    n_valid = sum(
        1
        for r in VOTING_ROLES
        if r in votes_by_role and not votes_by_role[r].abstained
    )
    if n_valid < cfg.quorum:
        return _flat(
            FailCode.VETO_QUORUM,
            score,
            "no_quorum",
            f"Quorum fail: {n_valid} of {cfg.quorum} voting roles produced a vote.",
        )

    # Step 4 — Unanimity. All non-abstain panel votes must agree on direction.
    directions = {
        votes_by_role[r].direction
        for r in VOTING_ROLES
        if r in votes_by_role and not votes_by_role[r].abstained
    }
    # FLAT votes break unanimity by definition (panel can't agree to do nothing
    # in a way that's distinguishable from genuine disagreement).
    if len(directions) != 1 or Direction.FLAT in directions:
        return _flat(
            FailCode.NO_CONSENSUS,
            score,
            "split",
            f"Panel split on direction: {sorted(d.name for d in directions)}.",
        )

    panel_direction = directions.pop()
    score_abs = abs(score)

    # Step 5 — Low conviction.
    if score_abs < cfg.low_conviction_threshold:
        if allow_repoll:
            return AggregatorDecision(
                action="FLAT",
                method=FailCode.LOW_CONVICTION,
                size_hint=0.0,
                weighted_score=score,
                consensus="unanimous",
                rationale=(
                    f"Lukewarm unanimous {panel_direction.name}: |score|={score_abs:.2f}"
                    f" < {cfg.low_conviction_threshold:.2f}. Trigger re-poll."
                ),
                trigger_repoll=True,
            )
        return _flat(
            FailCode.LOW_CONVICTION,
            score,
            "unanimous",
            (
                f"Lukewarm unanimous {panel_direction.name}: |score|={score_abs:.2f}"
                f" < {cfg.low_conviction_threshold:.2f}."
            ),
        )

    # Step 6 — Trade. Size by aggregate conviction.
    size_hint = min(1.0, score_abs / cfg.score_full_size)
    action: ActionLiteral = "LONG" if panel_direction is Direction.LONG else "SHORT"
    return AggregatorDecision(
        action=action,
        method=FailCode.UNANIMOUS,
        size_hint=size_hint,
        weighted_score=score,
        consensus="unanimous",
        rationale=(
            f"Unanimous {action} at score={score:+.2f}, size_hint={size_hint:.2f}."
        ),
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _index_panel(
    panel: tuple[AgentVote, ...] | list[AgentVote],
) -> dict[RoleName, AgentVote]:
    """Map panel votes by role, keeping only voting roles + at most one each."""
    out: dict[RoleName, AgentVote] = {}
    for v in panel:
        if v.role in VOTING_ROLES:
            # If duplicates appear (shouldn't, but defensive), the last wins.
            out[v.role] = v
    return out


def _weighted_score(
    votes_by_role: dict[RoleName, AgentVote], cfg: AggregatorConfig
) -> float:
    """Compute ``Σ w_i · d_i · c_i`` across voting roles. Abstains contribute 0."""
    total = 0.0
    for role in VOTING_ROLES:
        v = votes_by_role.get(role)
        if v is None or v.abstained:
            continue
        total += cfg.weight(role) * float(v.direction.value) * v.conviction
    return total


def _flat(
    method: FailCode,
    score: float,
    consensus: Literal["unanimous", "split", "no_quorum"],
    rationale: str,
) -> AggregatorDecision:
    return AggregatorDecision(
        action="FLAT",
        method=method,
        size_hint=0.0,
        weighted_score=score,
        consensus=consensus,
        rationale=rationale,
    )


__all__ = [
    "DEFAULT_LOW_CONVICTION_THRESHOLD",
    "DEFAULT_QUORUM",
    "DEFAULT_SCORE_FULL_SIZE",
    "AggregatorConfig",
    "AggregatorDecision",
    "aggregate",
]
