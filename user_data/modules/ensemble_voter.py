"""
Voting layer for the DRL ensemble.

The five discrete actions (strong_buy / buy / hold / sell / strong_sell)
collapse to a *direction* in {-1, 0, +1} and a *magnitude* in [0, 1].
The voter combines per-agent actions in two stages:

1. Direction vote — majority (mode) over {-1, 0, +1}.
2. Magnitude — average of the magnitudes of agents whose direction
   matches the winning vote.

Confidence is the agreement fraction (e.g. 3/3 → 1.0, 2/3 → 0.66).
If all three agents disagree (one each on -1, 0, +1) the voter falls
back to *hold* with confidence 0.0.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass

import numpy as np

# Mirror ACTION_MEANINGS from trading_env to keep this module decoupled.
ACTION_DIRECTION: tuple[int, ...] = (1, 1, 0, -1, -1)
ACTION_MAGNITUDE: tuple[float, ...] = (1.0, 0.5, 0.0, 0.5, 1.0)
HOLD_ACTION: int = 2


def _action_to_direction_magnitude(action: int) -> tuple[int, float]:
    a = int(action)
    if not 0 <= a < len(ACTION_DIRECTION):
        raise ValueError(f"invalid action {action}")
    return ACTION_DIRECTION[a], ACTION_MAGNITUDE[a]


@dataclass
class VoteResult:
    direction: int                 # {-1, 0, +1}
    magnitude: float               # in [0, 1]
    confidence: float              # in [0, 1]
    final_action: int              # 0..4 — Discrete(5) for compatibility
    agent_directions: dict[str, int]
    agent_magnitudes: dict[str, float]
    all_disagree: bool


def vote(actions: dict[str, int]) -> VoteResult:
    """
    Args:
        actions: {agent_name: discrete_action_index}

    Returns:
        VoteResult — see fields above.
    """
    if not actions:
        return VoteResult(
            direction=0, magnitude=0.0, confidence=0.0,
            final_action=HOLD_ACTION,
            agent_directions={}, agent_magnitudes={},
            all_disagree=False,
        )

    dirs: dict[str, int] = {}
    mags: dict[str, float] = {}
    for name, a in actions.items():
        d, m = _action_to_direction_magnitude(a)
        dirs[name] = d
        mags[name] = m

    counter = Counter(dirs.values())
    n = len(dirs)

    # All-disagree: every direction (-1, 0, +1) appears at least once
    # AND no direction commands a strict majority.
    distinct = set(counter.keys())
    most_common = counter.most_common(1)[0]
    winning_dir, winning_count = most_common[0], most_common[1]

    all_disagree = (
        distinct == {-1, 0, 1}
        and winning_count <= n // 3 + (n % 3 > 0)
        and winning_count * 3 <= n + 1   # no direction has > n/3 + small lead
    )
    # Simpler for the typical n=3 case: all three differ → disagree.
    if n == 3 and len(distinct) == 3:
        all_disagree = True

    if all_disagree:
        return VoteResult(
            direction=0, magnitude=0.0, confidence=0.0,
            final_action=HOLD_ACTION,
            agent_directions=dirs, agent_magnitudes=mags,
            all_disagree=True,
        )

    confidence = float(winning_count) / float(n)

    # Magnitude: mean of magnitudes from agents that agreed on the direction.
    agreeing_mags = [mags[name] for name, d in dirs.items() if d == winning_dir]
    magnitude = float(np.mean(agreeing_mags)) if agreeing_mags else 0.0

    final_action = _direction_magnitude_to_action(winning_dir, magnitude)

    return VoteResult(
        direction=int(winning_dir),
        magnitude=magnitude,
        confidence=confidence,
        final_action=final_action,
        agent_directions=dirs,
        agent_magnitudes=mags,
        all_disagree=False,
    )


def _direction_magnitude_to_action(direction: int, magnitude: float) -> int:
    """Map (direction, magnitude) back to a Discrete(5) index."""
    if direction == 0:
        return HOLD_ACTION
    strong = magnitude >= 0.75
    if direction > 0:
        return 0 if strong else 1
    return 4 if strong else 3


def vote_batch(actions: dict[str, np.ndarray]) -> list[VoteResult]:
    """
    Vectorised voting for a batch of observations. `actions[name]` is an
    ndarray of length B; output is a list of B VoteResults.
    """
    if not actions:
        return []
    lengths = {len(v) for v in actions.values()}
    if len(lengths) != 1:
        raise ValueError(f"agents returned different batch sizes: {lengths}")
    (b,) = lengths
    out: list[VoteResult] = []
    names = list(actions.keys())
    for i in range(b):
        out.append(vote({n: int(actions[n][i]) for n in names}))
    return out
