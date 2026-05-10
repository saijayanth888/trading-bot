"""
Stocks DRL ensemble — SCAFFOLD (ALPHA).

⚠️  This is a v0.1 placeholder. The real DRL ensemble (PPO + A2C + DQN
on a custom StockTradingEnv) is a multi-week build that needs:
  - Realistic env (slippage, holiday calendar, partial fills)
  - Reward shaping (alpha vs SPY, not absolute returns)
  - Walk-forward training/eval
  - Proper hyperparameter search
  - Walk-forward eval, NOT random splits

For tonight's training cycle this module does ONE thing: turn TFT
direction probabilities into a {action, confidence, votes} structure
the rest of the codebase can call. When real DRL agents are added
later they slot in here without changing the call site.

The "ensemble" right now is a single TFT-derived heuristic. Three
"votes" are produced:
  1. tft_threshold:   up if prob_up > 0.55, down if prob_down > 0.55, else hold
  2. tft_confidence:  same direction as tft_threshold but only fires if
                      max prob - second prob ≥ 0.10
  3. tft_quantile:    same direction but requires up_prob > 2× down_prob
                      (or vice versa) for a strong directional bet

Voting: majority direction wins; confidence = avg of voters that agreed.
"""

from __future__ import annotations

import logging
from typing import Optional

logger = logging.getLogger(__name__)


def _vote_threshold(probs: dict, thr: float = 0.55) -> tuple[str, float]:
    if probs.get("up", 0) > thr:
        return ("buy", probs["up"])
    if probs.get("down", 0) > thr:
        return ("sell", probs["down"])
    return ("hold", probs.get("flat", 0.0))


def _vote_confidence(probs: dict, min_gap: float = 0.10) -> tuple[str, float]:
    sorted_p = sorted(probs.values(), reverse=True)
    if len(sorted_p) < 2 or (sorted_p[0] - sorted_p[1]) < min_gap:
        return ("hold", 0.0)
    return _vote_threshold(probs, thr=0.40)


def _vote_quantile(probs: dict, ratio: float = 2.0) -> tuple[str, float]:
    up = probs.get("up", 0.0)
    down = probs.get("down", 0.0)
    if up > ratio * max(down, 1e-6):
        return ("buy", up)
    if down > ratio * max(up, 1e-6):
        return ("sell", down)
    return ("hold", 0.0)


def get_ensemble_signal(symbol: str, tft_pred: dict) -> dict:
    """Combine TFT-derived votes into a single ensemble decision.

    Args
      symbol:    ticker
      tft_pred:  output of tft_stock.predict_direction(); dict with
                 keys "up", "down", "flat", "confidence".

    Returns
      {
        "action": "buy" | "sell" | "hold",
        "confidence": float in [0, 1],
        "votes": {"tft_threshold": (action, conf), ...},
        "rationale": str,
      }
    """
    if "error" in tft_pred or tft_pred.get("up") is None:
        return {
            "action": "hold",
            "confidence": 0.0,
            "votes": {},
            "rationale": f"TFT unavailable: {tft_pred.get('error', 'no prediction')}",
        }

    probs = {
        "up": float(tft_pred["up"]),
        "down": float(tft_pred["down"]),
        "flat": float(tft_pred["flat"]),
    }
    votes = {
        "tft_threshold": _vote_threshold(probs),
        "tft_confidence": _vote_confidence(probs),
        "tft_quantile": _vote_quantile(probs),
    }

    # Majority direction
    counts: dict[str, int] = {}
    confs: dict[str, list[float]] = {}
    for action, conf in votes.values():
        counts[action] = counts.get(action, 0) + 1
        confs.setdefault(action, []).append(conf)
    winner = max(counts.items(), key=lambda kv: kv[1])[0]
    if counts[winner] < 2:
        winner = "hold"

    avg_conf = (
        sum(confs[winner]) / len(confs[winner])
        if winner in confs and confs[winner] else 0.0
    )

    rationale = (
        f"votes: " + ", ".join(f"{name}={a}@{c:.2f}" for name, (a, c) in votes.items())
    )
    logger.info("[STOCKS_ML_ALPHA] %s ensemble → %s (conf=%.2f) %s",
                symbol, winner, avg_conf, rationale)

    return {
        "action": winner,
        "confidence": avg_conf,
        "votes": {k: {"action": v[0], "confidence": v[1]} for k, v in votes.items()},
        "rationale": rationale,
    }


def train_drl_placeholder() -> dict:
    """Placeholder train-step. Real DRL training comes later (weeks of
    work). For now, the cron just records that it ran."""
    logger.warning(
        "[STOCKS_ML_ALPHA] DRL ensemble is a SCAFFOLD — real PPO+A2C+DQN "
        "training not yet implemented. Cron logs the no-op for the schedule."
    )
    return {
        "status": "scaffold_only",
        "message": "DRL ensemble is a v0.1 placeholder. TFT-derived votes "
                   "are working; PPO/A2C/DQN agents are deferred to phase 2.",
    }
