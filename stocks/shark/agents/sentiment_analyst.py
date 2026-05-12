"""Sentiment Analyst agent — grounded retail-sentiment opinion.

Pre-fetches StockTwits + Reddit + Yahoo News through ``shark.data.sentiment``
and bakes the formatted block directly into the system prompt. The LLM has
NO tool-calling — the block IS the data, eliminating the hallucinated-post
class of failures we saw with tool-using small models (TradingAgents
issue #557).

Output is a structured JSON dict suitable for joining into the bull/bear
debate state alongside the other analysts.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any

from shark.data.sentiment import fetch_grounded_sentiment
from shark.llm.client import chat_json

logger = logging.getLogger(__name__)


_SCHEMA_HINT = (
    '{"symbol": "<ticker>", '
    '"sentiment_label": "bullish|bearish|neutral", '
    '"sentiment_score": <float -1.0 to 1.0>, '
    '"retail_consensus": "<one-sentence consensus from retail forums>", '
    '"top_themes": ["<theme 1>", "<theme 2>", "<theme 3>"], '
    '"red_flags": ["<flag 1>", ...], '
    '"data_quality": "high|medium|low", '
    '"sources_used": ["stocktwits", "reddit", "yahoo"], '
    '"confidence": <float 0.0 to 1.0>}'
)


def _build_system_prompt(symbol: str, block: str) -> str:
    """Bake the grounded sentiment block into the analyst's instructions.

    The block is the entire data source — instruct the LLM to ground its
    answer in the block and to refuse to invent posts that are not present.
    """
    return (
        "You are the Sentiment Analyst on a hedge-fund debate team. Your job is "
        "to read the GROUNDED RETAIL SENTIMENT block below and produce a "
        "structured opinion. ABSOLUTE RULES:\n"
        "  1. Use ONLY the data in the block. Never invent posts, headlines, "
        "or numbers.\n"
        "  2. If a source is marked 'unavailable', do NOT hallucinate replacement "
        "data — say so in 'red_flags' and lower 'data_quality' accordingly.\n"
        "  3. Distinguish retail crowd sentiment from news-flow sentiment when "
        "they conflict.\n"
        "  4. Return valid JSON only — no prose, no markdown fences.\n\n"
        "GROUNDED RETAIL SENTIMENT BLOCK\n"
        "===============================\n"
        f"{block}\n"
        "===============================\n\n"
        f"Now write your analysis for {symbol}."
    )


def _empty_result(symbol: str, error: str | None = None) -> dict[str, Any]:
    return {
        "symbol": symbol,
        "sentiment_label": "neutral",
        "sentiment_score": 0.0,
        "retail_consensus": "",
        "top_themes": [],
        "red_flags": ["sentiment_analyst_failed"] if error else [],
        "data_quality": "low",
        "sources_used": [],
        "confidence": 0.0,
        "error": error,
    }


def analyze_sentiment(
    symbol: str,
    *,
    date: str | None = None,
    force_refresh: bool = False,
    tier: str = "fast",
) -> dict[str, Any]:
    """Run the Sentiment Analyst for one symbol.

    Returns a structured dict (see ``_SCHEMA_HINT`` for the shape). Never
    raises — every failure path returns ``_empty_result``.

    Args:
        symbol: Ticker symbol (e.g. ``"NVDA"``).
        date: Override the date used for cache keying. Defaults to UTC today.
        force_refresh: Bypass the 30-min cache and refetch every source.
        tier: ``"fast"`` (8B local) or ``"deep"`` (70B / Anthropic).
    """
    symbol = symbol.upper()
    date_str = date or datetime.now(timezone.utc).strftime("%Y-%m-%d")

    try:
        block = fetch_grounded_sentiment(
            symbol, date_str, force_refresh=force_refresh
        )
    except Exception as exc:
        # fetch_grounded_sentiment is fail-soft, but belt-and-braces
        logger.error("Sentiment block fetch failed for %s: %s", symbol, exc)
        return _empty_result(symbol, error=f"block_fetch:{exc}")

    system_prompt = _build_system_prompt(symbol, block)
    user_message = (
        f"Produce your structured sentiment analysis for {symbol} based on the "
        "block above."
    )

    try:
        raw_text, _usage, _model = chat_json(
            system_prompt=system_prompt,
            user_message=user_message,
            max_tokens=600,
            temperature=0.2,
            tier=tier,
            agent="sentiment_analyst",
            schema_hint=_SCHEMA_HINT,
        )
    except Exception as exc:
        logger.error("Sentiment Analyst LLM call failed for %s: %s", symbol, exc)
        result = _empty_result(symbol, error=f"llm:{exc}")
        result["raw_block"] = block
        return result

    raw_text = (raw_text or "").strip()
    if raw_text.startswith("```"):
        lines = raw_text.splitlines()
        raw_text = "\n".join(
            line for line in lines if not line.strip().startswith("```")
        ).strip()

    try:
        parsed = json.loads(raw_text)
    except json.JSONDecodeError as exc:
        logger.error("Sentiment Analyst JSON parse error for %s: %s", symbol, exc)
        result = _empty_result(symbol, error=f"json_parse:{exc}")
        result["raw_text"] = raw_text[:500]
        result["raw_block"] = block
        return result

    # Normalize / coerce fields
    label = str(parsed.get("sentiment_label") or "neutral").lower()
    if label not in {"bullish", "bearish", "neutral"}:
        label = "neutral"
    try:
        score = float(parsed.get("sentiment_score") or 0.0)
    except (TypeError, ValueError):
        score = 0.0
    score = max(-1.0, min(1.0, score))
    try:
        confidence = float(parsed.get("confidence") or 0.0)
    except (TypeError, ValueError):
        confidence = 0.0
    confidence = max(0.0, min(1.0, confidence))

    return {
        "symbol": symbol,
        "sentiment_label": label,
        "sentiment_score": score,
        "retail_consensus": str(parsed.get("retail_consensus") or ""),
        "top_themes": [str(t) for t in (parsed.get("top_themes") or [])][:5],
        "red_flags": [str(f) for f in (parsed.get("red_flags") or [])][:5],
        "data_quality": str(parsed.get("data_quality") or "low").lower(),
        "sources_used": [str(s) for s in (parsed.get("sources_used") or [])],
        "confidence": confidence,
        "error": None,
    }


__all__ = ["analyze_sentiment"]
