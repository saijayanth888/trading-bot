"""
Single source of truth for confidence and risk-reward floors that gate
LLM-decided BUYs.

Pre-2026-05-14 the constants `_MIN_CONFIDENCE = 0.70` and `_MIN_RISK_REWARD
= 2.0` were duplicated as module-level constants across three files:
  - shark/phases/market_open.py
  - shark/agents/decision_arbiter.py
  - shark/signals/generator.py

The stagnant-config audit flagged this as classic drift bait: change one,
forget the other two, the floor inconsistently fires depending on which
code path made the decision. Worse, the values are hardcoded despite the
regime detector already returning a per-regime `confidence_threshold`
(0.65 in quiet bull, 0.75 in volatile bull, 1.0 in bear).

This module is the single home. Defaults match the pre-audit constants
so behavior is unchanged for any call site that doesn't yet have
regime_rules in scope. The regime-aware variants (`min_confidence(rules)`,
`min_risk_reward(rules)`) are the path forward — callers that have
`regime_rules` in scope (e.g. market_open's _execute() when it loads
analysis_data['regime']) get a tighter floor in volatile/bear markets
without any threading work in signals/generator or decision_arbiter.

To extend: add a new `min_X(regime_rules)` helper here, NOT a constant
elsewhere. Per-regime values live in REGIME_RULES (shark/data/market_regime.py).
"""

from __future__ import annotations

from typing import Any


# Conservative defaults — match the pre-audit hardcoded values in the
# three deduplicated call sites. When callers pass regime_rules these
# are overridden per-regime (see helpers below).
DEFAULT_MIN_CONFIDENCE: float = 0.70
DEFAULT_MIN_RISK_REWARD: float = 2.0
DEFAULT_MIN_RISK_REWARD_TOL: float = 1.8


def min_confidence(regime_rules: dict[str, Any] | None = None) -> float:
    """Confidence floor for LLM BUY decisions.

    When ``regime_rules`` is provided, uses its ``confidence_threshold``
    (ladders 0.65 quiet bull → 0.75 volatile bull → 1.0 bear). Otherwise
    returns ``DEFAULT_MIN_CONFIDENCE``.
    """
    if regime_rules and "confidence_threshold" in regime_rules:
        return float(regime_rules["confidence_threshold"])
    return DEFAULT_MIN_CONFIDENCE


def min_risk_reward(regime_rules: dict[str, Any] | None = None) -> float:
    """Risk-reward floor for LLM-claimed numbers.

    When ``regime_rules`` is provided, uses its ``min_risk_reward`` field
    if present (gives ladders 2.0 quiet → 2.5 volatile → 3.0 bear-quiet);
    otherwise returns ``DEFAULT_MIN_RISK_REWARD``.
    """
    if regime_rules and "min_risk_reward" in regime_rules:
        return float(regime_rules["min_risk_reward"])
    return DEFAULT_MIN_RISK_REWARD


def min_risk_reward_tol(regime_rules: dict[str, Any] | None = None) -> float:
    """Tolerance R:R floor (used to validate derived stop/target/entry
    math against the LLM's claimed R:R). Always 0.2 below
    ``min_risk_reward`` to absorb rounding error.
    """
    return min_risk_reward(regime_rules) - 0.2


__all__ = [
    "DEFAULT_MIN_CONFIDENCE",
    "DEFAULT_MIN_RISK_REWARD",
    "DEFAULT_MIN_RISK_REWARD_TOL",
    "min_confidence",
    "min_risk_reward",
    "min_risk_reward_tol",
]
