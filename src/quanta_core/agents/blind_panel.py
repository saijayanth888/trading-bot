"""Blind-panel pattern: round-1 isolated voting, round-2 optional visibility.

Doc 05 rev2 §7. In round 1 each role sees only the shared SetupContext and
its own KB slice — never any other role's vote. In round 2 (only fired when
the aggregator returns ``LOW_CONVICTION`` AND the feature flag is on) the
adversarial pair (bull + bear) re-runs with full panel visibility.

This module owns *prompt assembly*. It does not call the LLM directly; the
debate orchestrator does. Keeping prompt assembly here makes the round-1 /
round-2 distinction unit-testable without a live Ollama.
"""

from __future__ import annotations

from dataclasses import dataclass

from .roles import (
    DEFAULT_ROLE_SPECS,
    AgentVote,
    ArbiterSynthesis,
    RoleName,
    RoleSpec,
    SetupContext,
)


@dataclass(frozen=True, slots=True)
class BlindPanelConfig:
    """Feature flags for round-2 behaviour."""

    enable_repoll_for_low_conviction: bool = False
    """Round-2 re-poll on low-conviction unanimous. Default OFF — the
    operator's 8-week paper-mode window decides whether to enable it."""

    max_rounds: int = 2
    """Hard cap. Round 2 is the most we ever do."""


# ---------------------------------------------------------------------------
# Round-1 prompt assembly (BLIND — no peer outputs visible)
# ---------------------------------------------------------------------------


def build_round1_prompt(
    role: RoleName,
    setup: SetupContext,
    *,
    spec: RoleSpec | None = None,
) -> str:
    """Assemble the round-1 prompt for ``role``.

    The output is a single deterministic string built by appending the role's
    prompt template to a JSON-y description of the SetupContext. The KB
    context is sliced per role:

    * regime / micro: see neither KB slice (they reason from features only).
    * bull: sees ``kb_bull_context`` ONLY.
    * bear: sees ``kb_bear_context`` ONLY.
    * arbiter: sees both (but arbiter never runs in round 1 — it runs after
      all four voters at t=24 s, so calling this with ``role=ARBITER`` will
      raise ``ValueError``).
    """
    if role is RoleName.ARBITER:
        raise ValueError(
            "Arbiter does not run in round 1; use build_arbiter_prompt() at t=24s."
        )
    if role is RoleName.REFLECTOR:
        raise ValueError(
            "Reflector runs out of band, not in the deliberation panel."
        )

    spec = spec or DEFAULT_ROLE_SPECS[role]

    kb_lines: list[str] = []
    if role is RoleName.BULL:
        kb_lines = list(setup.kb_bull_context)
    elif role is RoleName.BEAR:
        kb_lines = list(setup.kb_bear_context)

    parts: list[str] = [
        spec.prompt_template,
        "",
        "## Setup",
        f"symbol: {setup.symbol}",
        f"ts: {setup.ts.isoformat()}",
        f"state_snapshot_hash: {setup.state_snapshot_hash}",
        f"regime_features: {sorted(setup.regime_features.items())}",
        f"microstructure: {sorted(setup.microstructure.items())}",
        f"last_bars (n): {len(setup.last_bars)}",
        f"account.equity_usd: {setup.account_state.equity_usd:.2f}",
        f"account.drawdown_4w_pct: {setup.account_state.drawdown_4w_pct:.4f}",
    ]
    if kb_lines:
        parts.append("")
        parts.append(f"## KB (role={role.value})")
        parts.extend(f"- {line}" for line in kb_lines)
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Arbiter prompt — sees all four panel votes (NOT round-2 re-poll)
# ---------------------------------------------------------------------------


def build_arbiter_prompt(
    setup: SetupContext,
    panel: tuple[AgentVote, ...],
    *,
    spec: RoleSpec | None = None,
) -> str:
    """Assemble the arbiter prompt at t=24 s.

    The arbiter sees the full round-1 panel verbatim and writes a synthesis
    paragraph. It does NOT vote.
    """
    spec = spec or DEFAULT_ROLE_SPECS[RoleName.ARBITER]
    lines: list[str] = [
        spec.prompt_template,
        "",
        "## Setup",
        f"symbol: {setup.symbol}",
        f"ts: {setup.ts.isoformat()}",
        "",
        "## Round-1 panel",
    ]
    for v in panel:
        lines.append(
            f"- {v.role.value}: dir={v.direction.name} conv={v.conviction:.2f}"
            f" abstain={v.abstained} evidence={list(v.evidence_keys)}"
        )
        if v.rationale:
            # Trim long rationales so the prompt stays bounded.
            snippet = v.rationale[:400]
            lines.append(f"    rationale: {snippet}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Round-2 prompt assembly (VISIBILITY — full round-1 panel + arbiter)
# ---------------------------------------------------------------------------


def build_round2_prompt(
    role: RoleName,
    setup: SetupContext,
    round1_panel: tuple[AgentVote, ...],
    arbiter: ArbiterSynthesis | None,
    *,
    spec: RoleSpec | None = None,
) -> str:
    """Assemble the round-2 prompt for bull or bear with full panel visibility.

    Doc 05 rev2 §7.2: "Argue specifically against the weakest point, OR raise
    your conviction with new evidence_keys. If you cannot, vote FLAT."

    Round-2 is only ever called for :attr:`RoleName.BULL` or
    :attr:`RoleName.BEAR`. Any other role raises :class:`ValueError`.
    """
    if role not in (RoleName.BULL, RoleName.BEAR):
        raise ValueError(
            f"Round-2 re-poll is bull/bear only; got {role.value}."
        )

    spec = spec or DEFAULT_ROLE_SPECS[role]
    base = build_round1_prompt(role, setup, spec=spec)

    extras: list[str] = [
        "",
        "## Round-1 panel (VISIBLE in round 2)",
    ]
    for v in round1_panel:
        extras.append(
            f"- {v.role.value}: dir={v.direction.name} conv={v.conviction:.2f}"
            f" abstain={v.abstained}"
        )
        if v.rationale:
            extras.append(f"    rationale: {v.rationale[:400]}")
    if arbiter is not None:
        extras.append("")
        extras.append("## Arbiter synthesis")
        extras.append(arbiter.synthesis_rationale[:1200])

    extras.append("")
    extras.append(
        "Round 1 was LUKEWARM-UNANIMOUS. Argue against the weakest point OR raise"
        " conviction with new evidence_keys. If you cannot, vote FLAT."
    )
    return base + "\n" + "\n".join(extras)


__all__ = [
    "BlindPanelConfig",
    "build_arbiter_prompt",
    "build_round1_prompt",
    "build_round2_prompt",
]
