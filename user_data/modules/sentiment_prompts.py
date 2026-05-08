"""Prompts for the Ollama-based sentiment scorer."""

from __future__ import annotations

from typing import Any

OLLAMA_SYSTEM_PROMPT = """\
You are a crypto market sentiment analyst.

Read the news items the user gives you and reply with **only** a JSON \
object matching this exact schema. No markdown fences, no explanation, \
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


USER_PROMPT_TEMPLATE = """\
Time window: last {window_minutes} minutes (UTC now = {now}).

== NEWS ITEMS ({n_items}) ==
{items_block}
"""


def build_user_prompt(
    items: list[dict[str, Any]],
    window_minutes: int,
    now_iso: str,
) -> str:
    """Format a Perplexity headline list into the user message."""
    if items:
        lines = []
        for it in items:
            src = str(it.get("source") or "?").strip() or "?"
            title = str(it.get("title") or "").strip()
            summary = str(it.get("summary") or "").strip()
            line = f"- [{src}] {title}"
            if summary:
                line += f"  — {summary}"
            lines.append(line)
        items_block = "\n".join(lines)
    else:
        items_block = "(no items available)"

    return USER_PROMPT_TEMPLATE.format(
        window_minutes=window_minutes,
        now=now_iso,
        n_items=len(items),
        items_block=items_block,
    )
