"""
Meta-agent: combines the TFT classifier output with the DRL ensemble vote.

Inputs:
    - tft_probs: {"down": p, "flat": p, "up": p}     OR  {"down": p, "up": p}
    - tft_confidence: in [0, 1] (from quantile spread)
    - drl_vote: ensemble_voter.VoteResult, or None when DRL weights are
      absent — in which case the function runs in TFT-only mode (weights
      1.0/0.0, lower confidence floor TFT_ONLY_MIN_CONFIDENCE).
    - regime: str — one of trending_up, trending_down, mean_reverting,
      high_volatility, unknown
    - regime_confidence: in [0, 1]

Output (MetaSignal):
    final_signal: int in {-1, 0, +1}
    final_confidence: in [0, 1]
    position_size_pct: in [0, 1]   — fraction of available stake to deploy

Regime-conditional weighting:
    trending_up / trending_down → TFT 0.6, DRL 0.4
    mean_reverting              → TFT 0.4, DRL 0.6
    high_volatility             → both reduced; trade only if both agree on
                                  the same non-flat direction; size halved
    unknown                     → TFT 0.5, DRL 0.5
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Mapping

from .ensemble_voter import VoteResult

logger = logging.getLogger(__name__)

# (tft_weight, drl_weight)
DEFAULT_REGIME_WEIGHTS: dict[str, tuple[float, float]] = {
    "trending_up":     (0.6, 0.4),
    "trending_down":   (0.6, 0.4),
    "mean_reverting":  (0.4, 0.6),
    "high_volatility": (0.5, 0.5),     # only used as a tie-break — see below
    "unknown":         (0.5, 0.5),
}

HIGH_VOL_SIZE_FACTOR: float = 0.5
MIN_TRADE_CONFIDENCE: float = 0.4

# P0-M: hard floor on the high-vol position size. Without this, a low
# confidence × low magnitude × low regime_confidence product could sink
# size to ~0, silently disabling trading even after both legs agreed on
# direction. If we're going to trade at all in high-vol, we trade at
# least this fraction of base stake.
HIGH_VOL_MIN_SIZE: float = 0.25

# TFT-only mode threshold (DRL absent). DRL has never been trained; the
# strategy falls back to TFT alone. Lower than MIN_TRADE_CONFIDENCE
# because losing the DRL co-signer cuts the blended confidence by ~40%
# in trending regimes — without this drop the floor effectively becomes
# unreachable. 0.40 keeps signal quality close to the original blend's
# 0.4 × 0.6 = 0.24 minimum tft_conf threshold.
TFT_ONLY_MIN_CONFIDENCE: float = 0.4


@dataclass
class MetaSignal:
    final_signal: int                   # -1, 0, +1
    final_confidence: float             # [0, 1]
    position_size_pct: float            # [0, 1]
    tft_signal: int                     # -1, 0, +1
    tft_confidence: float               # [0, 1] — from quantile spread
    drl_signal: int                     # -1, 0, +1
    drl_confidence: float               # [0, 1] — from voter agreement
    regime: str
    weights: tuple[float, float]
    blocked_reason: str | None = None   # e.g. "high_vol_disagreement"


def compute_signal(
    tft_probs: Mapping[str, float],
    tft_confidence: float,
    drl_vote: VoteResult | None,
    regime: str,
    regime_confidence: float = 1.0,
    *,
    regime_weights: dict[str, tuple[float, float]] | None = None,
    min_trade_confidence: float = MIN_TRADE_CONFIDENCE,
    high_vol_size_factor: float = HIGH_VOL_SIZE_FACTOR,
    high_vol_min_size: float = HIGH_VOL_MIN_SIZE,
    tft_only_min_confidence: float = TFT_ONLY_MIN_CONFIDENCE,
) -> MetaSignal:
    """Combine TFT + DRL into a final trading signal.

    If ``drl_vote`` is None, run TFT-only: full TFT weight (1.0/0.0), the
    lower ``tft_only_min_confidence`` floor, and the high-vol gate
    degrades to a TFT-only block (no DRL co-signer to agree with).
    """
    weights = (regime_weights or DEFAULT_REGIME_WEIGHTS).get(
        regime, DEFAULT_REGIME_WEIGHTS["unknown"]
    )

    tft_signal, tft_strength = _tft_to_signal(tft_probs)

    # ----- DRL absent → TFT-only path ---------------------------------
    if drl_vote is None:
        return _tft_only_signal(
            tft_signal, tft_strength, tft_confidence,
            regime, regime_confidence,
            min_confidence=tft_only_min_confidence,
        )

    drl_signal = drl_vote.direction
    drl_conf = drl_vote.confidence
    drl_mag = drl_vote.magnitude

    # ----- High volatility regime: gate harder ------------------------
    if regime == "high_volatility":
        if drl_vote.all_disagree or tft_signal == 0 or drl_signal == 0:
            return _block(
                tft_signal, tft_confidence, drl_signal, drl_conf,
                regime, weights, "high_vol_no_consensus",
            )
        if tft_signal != drl_signal:
            return _block(
                tft_signal, tft_confidence, drl_signal, drl_conf,
                regime, weights, "high_vol_disagreement",
            )
        # Both agree, non-flat — trade with reduced size
        agree_conf = (tft_confidence + drl_conf) / 2.0
        size = high_vol_size_factor * agree_conf * drl_mag * regime_confidence
        # P0-M: floor the size so the product of three [0,1] factors can't
        # collapse to ~0 when each individual term is "good enough" to trade.
        # If the meta-agent decided to take the trade in a high-vol regime,
        # we trade at *at least* high_vol_min_size of base stake.
        sized = max(high_vol_min_size, float(_clip01(size)))
        return MetaSignal(
            final_signal=tft_signal,
            final_confidence=agree_conf,
            position_size_pct=float(_clip01(sized)),
            tft_signal=tft_signal,
            tft_confidence=tft_confidence,
            drl_signal=drl_signal,
            drl_confidence=drl_conf,
            regime=regime,
            weights=weights,
        )

    # ----- All-disagree on the DRL side → fall back to TFT alone -------
    if drl_vote.all_disagree:
        if tft_confidence < min_trade_confidence or tft_signal == 0:
            return _block(
                tft_signal, tft_confidence, drl_signal, drl_conf,
                regime, weights, "drl_disagreement_low_tft",
            )
        # Use TFT but discount confidence (DRL is noise, not a co-signer)
        size = (weights[0] * tft_confidence * tft_strength) * regime_confidence
        return MetaSignal(
            final_signal=tft_signal,
            final_confidence=tft_confidence * weights[0],
            position_size_pct=float(_clip01(size)),
            tft_signal=tft_signal,
            tft_confidence=tft_confidence,
            drl_signal=0,
            drl_confidence=0.0,
            regime=regime,
            weights=weights,
            blocked_reason=None,
        )

    # ----- Standard weighted combination -------------------------------
    tft_score = tft_signal * tft_confidence * weights[0]
    drl_score = drl_signal * drl_conf * weights[1]
    combined = tft_score + drl_score

    if abs(combined) < min_trade_confidence:
        return _block(
            tft_signal, tft_confidence, drl_signal, drl_conf,
            regime, weights, "below_min_confidence",
        )

    final_signal = 1 if combined > 0 else (-1 if combined < 0 else 0)
    final_confidence = min(1.0, abs(combined))

    # Position sizing scales with combined confidence, DRL magnitude
    # (so strong_buy outweighs buy), and regime confidence.
    size = final_confidence * (0.5 + 0.5 * drl_mag) * regime_confidence
    return MetaSignal(
        final_signal=final_signal,
        final_confidence=final_confidence,
        position_size_pct=float(_clip01(size)),
        tft_signal=tft_signal,
        tft_confidence=tft_confidence,
        drl_signal=drl_signal,
        drl_confidence=drl_conf,
        regime=regime,
        weights=weights,
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _tft_to_signal(probs: Mapping[str, float]) -> tuple[int, float]:
    """
    Convert TFT class probabilities to (direction, strength).
    direction ∈ {-1, 0, +1}; strength = max prob - second-best prob (margin).
    """
    p_up = float(probs.get("up", 0.0))
    p_down = float(probs.get("down", 0.0))
    p_flat = float(probs.get("flat", max(0.0, 1.0 - p_up - p_down)))

    triples = [(p_up, 1), (p_flat, 0), (p_down, -1)]
    triples.sort(reverse=True)
    top, second = triples[0], triples[1]
    strength = float(max(0.0, top[0] - second[0]))
    return int(top[1]), strength


def _clip01(x: float) -> float:
    if x < 0.0:
        return 0.0
    if x > 1.0:
        return 1.0
    return float(x)


def _block(
    tft_signal: int, tft_confidence: float,
    drl_signal: int, drl_confidence: float,
    regime: str, weights: tuple[float, float], reason: str,
) -> MetaSignal:
    return MetaSignal(
        final_signal=0,
        final_confidence=0.0,
        position_size_pct=0.0,
        tft_signal=tft_signal,
        tft_confidence=tft_confidence,
        drl_signal=drl_signal,
        drl_confidence=drl_confidence,
        regime=regime,
        weights=weights,
        blocked_reason=reason,
    )


def _tft_only_signal(
    tft_signal: int,
    tft_strength: float,
    tft_confidence: float,
    regime: str,
    regime_confidence: float,
    *,
    min_confidence: float,
) -> MetaSignal:
    """TFT-only fallback (DRL absent). Full TFT weight, no DRL blend."""
    weights = (1.0, 0.0)
    if tft_signal == 0 or tft_confidence < min_confidence:
        return _block(
            tft_signal, tft_confidence, 0, 0.0,
            regime, weights, "tft_only_low_confidence",
        )
    # No DRL magnitude to scale by; fall back to TFT strength (max-prob
    # margin) so a sharp class peak still sizes larger than a noisy one.
    size = tft_confidence * (0.5 + 0.5 * tft_strength) * regime_confidence
    return MetaSignal(
        final_signal=tft_signal,
        final_confidence=tft_confidence,
        position_size_pct=float(_clip01(size)),
        tft_signal=tft_signal,
        tft_confidence=tft_confidence,
        drl_signal=0,
        drl_confidence=0.0,
        regime=regime,
        weights=weights,
    )
