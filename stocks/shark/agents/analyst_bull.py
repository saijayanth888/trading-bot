"""
Bull Analyst Agent — generates a bullish thesis for a given symbol via the
provider-agnostic shark.llm.client. Default provider is local Ollama
(hermes3:70b); set SHARK_LLM_PROVIDER=anthropic in .env to route to Claude.
"""

import json
import logging
from typing import Any

try:
    import anthropic as _anthropic_lib
except ImportError:  # safety net — anthropic SDK is optional now
    _anthropic_lib = None

from shark.config import get_settings
from shark.llm.client import chat_json

logger = logging.getLogger(__name__)


def generate_bull_thesis(
    symbol: str,
    market_data: dict[str, Any],
    perplexity_intel: dict[str, Any],
) -> dict[str, Any]:
    """
    Generate a bullish investment thesis for the given symbol.

    Uses the configured LLM provider (default: local Ollama / Hermes-3 70B)
    to produce a structured JSON bull thesis including target price, entry
    zone, catalysts, and confidence.

    Args:
        symbol: Ticker symbol (e.g. "AAPL").
        market_data: Dict of OHLCV, technicals, and fundamentals for the symbol.
        perplexity_intel: Dict of recent news, sentiment, and analyst opinions.

    Returns:
        A dict with keys: symbol, thesis, catalysts, target_price, entry_zone,
        timeframe_days, confidence, supporting_data.
        On failure: includes an "error" key and confidence=0.0.
    """
    system_prompt = (
        "You are an experienced bullish equity analyst at a top hedge fund. "
        "Your job is to find compelling long opportunities and build conviction. "
        "Be specific, cite data, and quantify your thesis. Always return valid JSON."
    )

    user_prompt = f"""Analyze the following data for {symbol} and generate a bullish investment thesis.

## Market Data for {symbol}
```json
{json.dumps(market_data, indent=2, default=str)}
```

## Recent Intelligence (News, Sentiment, Analyst Views)
```json
{json.dumps(perplexity_intel, indent=2, default=str)}
```

Return ONLY a valid JSON object with this exact structure:
{{
  "symbol": "{symbol}",
  "thesis": "<2-3 sentence bull thesis with specific data points>",
  "catalysts": ["<catalyst 1>", "<catalyst 2>", "<catalyst 3>"],
  "target_price": <float>,
  "entry_zone": {{"low": <float>, "high": <float>}},
  "timeframe_days": <int>,
  "confidence": <float between 0.0 and 1.0>,
  "supporting_data": "<key data points that support the thesis>"
}}

Be specific about price levels based on the market data provided. Do not include any text outside the JSON object."""

    try:
        raw_text, _usage, _model = chat_json(
            system_prompt=system_prompt,
            user_message=user_prompt,
            max_tokens=1000,
            temperature=0.3,
            tier="fast",
            agent="analyst_bull",
        )
        raw_text = (raw_text or "").strip()

        # Strip markdown code fences if present
        if raw_text.startswith("```"):
            lines = raw_text.splitlines()
            raw_text = "\n".join(
                line for line in lines if not line.startswith("```")
            ).strip()

        thesis = json.loads(raw_text)

        # Ensure required keys are present and typed correctly
        thesis.setdefault("symbol", symbol)
        thesis.setdefault("thesis", "")
        thesis.setdefault("catalysts", [])
        thesis.setdefault("target_price", 0.0)
        thesis.setdefault("entry_zone", {"low": 0.0, "high": 0.0})
        thesis.setdefault("timeframe_days", 0)
        thesis.setdefault("confidence", 0.0)
        thesis.setdefault("supporting_data", "")

        thesis["confidence"] = max(0.0, min(1.0, float(thesis["confidence"])))

        return thesis

    except json.JSONDecodeError as exc:
        logger.error("Bull analyst JSON parse error for %s: %s", symbol, exc)
        return {
            "symbol": symbol,
            "thesis": "",
            "catalysts": [],
            "target_price": 0.0,
            "entry_zone": {"low": 0.0, "high": 0.0},
            "timeframe_days": 0,
            "confidence": 0.0,
            "supporting_data": "",
            "error": f"JSON parse error: {exc}",
        }

    except (
        _anthropic_lib.APIError if _anthropic_lib else Exception
    ) as exc:  # type: ignore[misc]
        logger.error("LLM API error in bull analyst for %s: %s", symbol, exc)
        return {
            "symbol": symbol,
            "thesis": "",
            "catalysts": [],
            "target_price": 0.0,
            "entry_zone": {"low": 0.0, "high": 0.0},
            "timeframe_days": 0,
            "confidence": 0.0,
            "supporting_data": "",
            "error": f"API error: {exc}",
        }

    except Exception as exc:
        logger.error("Unexpected error in bull analyst for %s: %s", symbol, exc)
        return {
            "symbol": symbol,
            "thesis": "",
            "catalysts": [],
            "target_price": 0.0,
            "entry_zone": {"low": 0.0, "high": 0.0},
            "timeframe_days": 0,
            "confidence": 0.0,
            "supporting_data": "",
            "error": f"Unexpected error: {exc}",
        }
