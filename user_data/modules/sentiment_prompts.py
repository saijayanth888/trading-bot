"""Prompt and tool-spec definitions for sentiment_engine.py."""

from __future__ import annotations

from typing import Any

# ---------------------------------------------------------------------------
# Claude
# ---------------------------------------------------------------------------

# Stable system text — sent with cache_control so repeated calls hit the
# Anthropic prompt cache.
SYSTEM_PROMPT = """\
You are a senior crypto market sentiment analyst.

You will be given a batch of recent cryptocurrency news headlines and Reddit \
posts from the last few minutes. Analyse them holistically and produce a single \
sentiment assessment for the broad crypto market.

Rules:
1. Weight reputable news sources (CoinDesk, The Block, CoinTelegraph) more \
   heavily than Reddit chatter, but use Reddit to gauge retail intensity.
2. Distinguish noise (memes, generic price-only talk, shitcoin shilling) from \
   signal (regulatory action, macro events, exchange / protocol incidents, \
   ETF flows, on-chain milestones, large hacks or exploits).
3. Be calibrated. Most 15-minute windows are neutral. Reserve \
   |sentiment_score| > 0.6 for clearly directional catalysts.
4. `confidence` reflects how unambiguous the signal is, not how strong.
5. If the batch is dominated by price commentary with no fundamental drivers, \
   set market_impact = "neutral".

You MUST respond by calling the `report_sentiment` tool exactly once. Do not \
include any other prose."""

# Tool definition — Claude will be forced to invoke this with structured args.
SENTIMENT_TOOL: dict[str, Any] = {
    "name": "report_sentiment",
    "description": (
        "Report the sentiment analysis for the supplied batch of crypto "
        "headlines and Reddit posts."
    ),
    "input_schema": {
        "type": "object",
        "required": [
            "sentiment_score",
            "confidence",
            "key_events",
            "market_impact",
        ],
        "properties": {
            "sentiment_score": {
                "type": "number",
                "minimum": -1,
                "maximum": 1,
                "description": (
                    "-1 = extremely bearish, 0 = neutral, 1 = extremely bullish."
                ),
            },
            "confidence": {
                "type": "number",
                "minimum": 0,
                "maximum": 1,
                "description": "How unambiguous the signal is.",
            },
            "key_events": {
                "type": "array",
                "items": {"type": "string"},
                "maxItems": 5,
                "description": (
                    "Up to 5 short strings summarising the most market-moving "
                    "items in the batch."
                ),
            },
            "market_impact": {
                "type": "string",
                "enum": ["bullish", "bearish", "neutral"],
            },
        },
    },
}

# ---------------------------------------------------------------------------
# Local model (Ollama uses `format: json` rather than tool use)
# ---------------------------------------------------------------------------

OLLAMA_SYSTEM_PROMPT = """\
You are a crypto market sentiment analyst.

Read the headlines and Reddit posts the user gives you and reply with **only** \
a JSON object matching this exact schema. No markdown fences, no explanation, \
no extra keys.

{
  "sentiment_score": <float between -1.0 and 1.0>,
  "confidence":      <float between 0.0 and 1.0>,
  "key_events":      [<string>, ...   max 5 entries],
  "market_impact":   "bullish" | "bearish" | "neutral"
}

Be calibrated. Reserve |sentiment_score| > 0.6 for clearly directional \
catalysts (regulation, macro, exchange/protocol incidents, ETF flows, large \
hacks). Pure price chatter is neutral."""

# ---------------------------------------------------------------------------
# Shared user-message builder
# ---------------------------------------------------------------------------

USER_PROMPT_TEMPLATE = """\
Time window: last {window_minutes} minutes (UTC now = {now}).

== HEADLINES ({n_headlines}) ==
{headlines_block}

== REDDIT TOP POSTS ({n_reddit}) ==
{reddit_block}
"""


def build_user_prompt(
    headlines: list[dict[str, Any]],
    reddit_posts: list[dict[str, Any]],
    window_minutes: int,
    now_iso: str,
) -> str:
    """Format a batch of headlines + Reddit posts into the user message."""
    if headlines:
        headlines_block = "\n".join(
            f"- [{h.get('source', '?')}] {h['title']}" for h in headlines
        )
    else:
        headlines_block = "(no headlines available)"

    if reddit_posts:
        reddit_block = "\n".join(
            f"- /r/{p['subreddit']} ({p['score']} upvotes, "
            f"{p['num_comments']} comments): {p['title']}"
            for p in reddit_posts
        )
    else:
        reddit_block = "(no reddit posts available)"

    return USER_PROMPT_TEMPLATE.format(
        window_minutes=window_minutes,
        now=now_iso,
        n_headlines=len(headlines),
        n_reddit=len(reddit_posts),
        headlines_block=headlines_block,
        reddit_block=reddit_block,
    )
