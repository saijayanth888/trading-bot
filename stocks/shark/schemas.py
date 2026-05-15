"""
Pydantic v2 schemas for hermes3:8b structured outputs.

Purpose
-------
Eliminate two recurring bug classes in the shark pipeline:

  1. f-string regex parsing — `analyst_bull.py`, `combined_analyst.py`,
     and `outcome_resolver.py` currently take raw LLM text, strip code
     fences with regex, then `json.loads()`. Any stray prose, smart-quote,
     trailing comma, or "Sure!" preamble crashes the parse.
  2. Dropdown / field extraction — downstream code does
     `result.get("decision", {}).get("confidence", 0.0)` and silently
     papers over missing or wrong-type fields, so a malformed LLM reply
     produces a zero-confidence "NO_TRADE" that looks legitimate.

Both go away when we validate against a Pydantic schema with
`Literal[...]` types and `Field(..., ge=, le=, max_length=)` constraints.

Models defined here
-------------------

  - RegimeTag       — per-ticker regime classification used by the
                      regime engine + UI.
  - TraderProposal  — concrete BUY/SELL/HOLD/SKIP proposal from the
                      analyst / arbiter path (supersedes the prose
                      block parsed by `combined_analyst.py`).
  - WheelDecision   — CSP / Covered-Call / SKIP suggestion for the
                      options-wheel sleeve.
  - OutcomeLabel    — deferred-reflection grading used by
                      `outcome_resolver.py` (replaces the current free
                      prose "lesson" line).

Hermes3:8b has an 8k context window, so every field carries a tight
character cap. Long-form prose belongs in the markdown logs, not in
schema-enforced JSON.

Attribution
-----------
Schema structure adapted from TradingAgents (Apache-2.0):
https://github.com/TauricResearch/TradingAgents — see
`tradingagents/agents/schemas.py`. We diverge on:
  - 5-tier vs 3-tier Trader action set (we add SKIP for risk-gate fails)
  - All fields use `Literal[...]` instead of `Enum` to stay JSON-clean
    when piped through Ollama's `format="json"`.
  - Hard character caps on every free-text field (8B context budget).
"""

from __future__ import annotations

from datetime import date
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

# ---------------------------------------------------------------------------
# RegimeTag — per-ticker regime label
# ---------------------------------------------------------------------------


RegimeLiteral = Literal[
    "trending_up",
    "trending_down",
    "mean_reverting",
    "high_volatility",
    "unknown",
]


class RegimeTag(BaseModel):
    """Regime classification for a single ticker.

    Produced by the regime engine's LLM-narrator step. Drives the
    `meta_up_regime` strategy gate and the `TodayScoreboard` UI card.
    """

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    ticker: str = Field(
        ...,
        min_length=1,
        max_length=12,
        description="Ticker symbol (e.g. 'BTC/USD', 'NVDA').",
    )
    regime: RegimeLiteral = Field(
        ...,
        description=(
            "One of: trending_up, trending_down, mean_reverting, "
            "high_volatility, unknown."
        ),
    )
    confidence: float = Field(
        ...,
        ge=0.0,
        le=1.0,
        description="Confidence in the regime label, 0.0-1.0.",
    )
    narrative: str = Field(
        ...,
        max_length=280,
        description=(
            "One-sentence rationale citing the dominant signal "
            "(MA cross, RSI extreme, BB width, etc.)."
        ),
    )


# ---------------------------------------------------------------------------
# TraderProposal — concrete trade action
# ---------------------------------------------------------------------------


TraderActionLiteral = Literal["BUY", "SELL", "HOLD", "SKIP"]


class TraderProposal(BaseModel):
    """Concrete transaction proposal from the analyst / arbiter.

    SKIP is distinct from HOLD: SKIP means a hard gate failed (risk,
    macro block, circuit breaker), while HOLD means the LLM looked
    at the data and chose not to act this round.
    """

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    ticker: str = Field(..., min_length=1, max_length=12)
    action: TraderActionLiteral = Field(
        ...,
        description="BUY / SELL / HOLD / SKIP — exactly one.",
    )
    conviction: float = Field(
        ...,
        ge=0.0,
        le=1.0,
        description="Conviction 0.0-1.0. Gate threshold lives in the caller.",
    )
    thesis: str = Field(
        ...,
        max_length=500,
        description=(
            "2-3 sentences citing specific data points (price, RSI, "
            "catalyst). No markdown, no bullets."
        ),
    )
    entry_price: float | None = Field(
        default=None,
        gt=0.0,
        description="Optional limit / target entry price.",
    )
    stop_loss: float | None = Field(
        default=None,
        gt=0.0,
        description="Optional stop-loss price level.",
    )
    target: float | None = Field(
        default=None,
        gt=0.0,
        description="Optional take-profit / price-target level.",
    )
    position_sizing_pct: float | None = Field(
        default=None,
        ge=0.0,
        le=1.0,
        description="Optional fraction of portfolio (0.0-1.0).",
    )
    invalidation: str = Field(
        ...,
        max_length=200,
        description="What concrete event invalidates this thesis.",
    )

    @field_validator("ticker")
    @classmethod
    def _normalize_ticker(cls, v: str) -> str:
        return v.upper().strip()


# ---------------------------------------------------------------------------
# WheelDecision — options-wheel CSP / CC suggestion
# ---------------------------------------------------------------------------


WheelKindLiteral = Literal["CSP", "CC", "SKIP"]


class WheelDecision(BaseModel):
    """Options-wheel decision: sell a CSP, write a CC, or skip this leg.

    Used by the wheel sleeve (see `IMMEDIATE_BLOCKERS_2026-05-11.md`).
    Strike / expiry / premium are optional so SKIP rows stay minimal.
    """

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    underlying: str = Field(..., min_length=1, max_length=12)
    kind: WheelKindLiteral = Field(
        ...,
        description=(
            "CSP = cash-secured put (entry leg). "
            "CC = covered call (exit / income leg). "
            "SKIP = no action this cycle."
        ),
    )
    strike: float | None = Field(default=None, gt=0.0)
    expiry: date | None = Field(
        default=None,
        description="Option expiry date (YYYY-MM-DD).",
    )
    premium_target: float | None = Field(
        default=None,
        gt=0.0,
        description="Minimum acceptable premium per contract, in dollars.",
    )
    rationale: str = Field(
        ...,
        max_length=280,
        description="1-2 sentence rationale: delta, IV rank, days-to-expiry.",
    )

    @field_validator("underlying")
    @classmethod
    def _normalize_underlying(cls, v: str) -> str:
        return v.upper().strip()


# ---------------------------------------------------------------------------
# OutcomeLabel — deferred reflection grade
# ---------------------------------------------------------------------------


OutcomeLabelLiteral = Literal[
    "tft_correct",
    "tft_wrong",
    "regime_correct",
    "regime_wrong",
    "exec_failed",
]


class OutcomeLabel(BaseModel):
    """Per-trade reflection grade emitted by the outcome_resolver.

    Replaces the current free-prose `lesson` row with a structured label
    so downstream analytics can tally hit-rate by category (model vs
    regime vs execution).
    """

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    trade_id: str = Field(
        ...,
        min_length=1,
        max_length=64,
        description=(
            "Stable identifier for the trade (uuid or "
            "'<symbol>_<entry_date>')."
        ),
    )
    label: OutcomeLabelLiteral = Field(
        ...,
        description=(
            "Which root cause owns the outcome: TFT model correctness, "
            "regime tagging correctness, or execution path failure."
        ),
    )
    confidence: float = Field(
        ...,
        ge=0.0,
        le=1.0,
        description="Confidence in the assigned label.",
    )
    reason: str = Field(
        ...,
        max_length=200,
        description=(
            "One sentence citing the deciding piece of evidence "
            "(alpha figure, exit reason, regime flip, etc.)."
        ),
    )


__all__ = [
    # Models
    "RegimeTag",
    "TraderProposal",
    "WheelDecision",
    "OutcomeLabel",
    # Literal aliases — re-exported so callers can annotate locals
    "RegimeLiteral",
    "TraderActionLiteral",
    "WheelKindLiteral",
    "OutcomeLabelLiteral",
]
