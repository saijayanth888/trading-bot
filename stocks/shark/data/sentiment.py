"""Grounded sentiment aggregator: StockTwits + Reddit + Yahoo News.

Fetches each source through its dedicated module (which handles caching and
fail-soft behavior), then formats a compact human-readable block that is
baked into the Sentiment Analyst's system prompt. The agent does NO
tool-calling — the formatted block IS the data.

Why no tool-calling?
--------------------
TradingAgents v0.2.5 issue #557 documented that small models (≤8B) often
hallucinate API responses when given tool definitions, and that pre-fetched
context blocks produce more grounded reasoning. We follow that pattern:
the cron pre-warms the cache, the agent reads the cache through this
module, and the LLM only sees verified data.

Module CLI
----------
``python -m stocks.shark.data.sentiment refresh --ticker NVDA``
    Force-refreshes all three sources for the ticker (used by the cron).

``python -m stocks.shark.data.sentiment block --ticker NVDA``
    Prints the formatted block to stdout (for debugging).

``python -m stocks.shark.data.sentiment refresh-universe``
    Refreshes every ticker in ``user_data/universe.json``.

Token cap
---------
The block is hard-capped at 1500 tokens (using ``tiktoken`` if available,
falling back to a 4-chars-per-token heuristic). This keeps the full
Sentiment Analyst system prompt safely under hermes3:8b's 8k context.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from shark.data.sentiment_reddit import fetch_reddit
from shark.data.sentiment_stocktwits import fetch_stocktwits
from shark.data.sentiment_yahoo import fetch_yahoo_news

logger = logging.getLogger(__name__)

_TOKEN_CAP = 1500
_CHARS_PER_TOKEN_FALLBACK = 4  # Conservative; real ratio is ~3.5 for English

_UNIVERSE_PATH = (
    Path(__file__).resolve().parents[3] / "user_data" / "universe.json"
)


# ---------------------------------------------------------------------------
# Token counting
# ---------------------------------------------------------------------------


def _count_tokens(text: str) -> int:
    """Best-effort token count. Uses tiktoken if importable, else a byte proxy."""
    try:
        import tiktoken  # type: ignore

        # cl100k_base is close enough for non-OpenAI models; we only need a
        # ceiling, not exact parity with a hermes3 tokenizer.
        enc = tiktoken.get_encoding("cl100k_base")
        return len(enc.encode(text))
    except Exception:
        return max(1, len(text) // _CHARS_PER_TOKEN_FALLBACK)


def _truncate_to_token_cap(text: str, cap: int = _TOKEN_CAP) -> str:
    """Truncate ``text`` so the token estimate is <= ``cap``.

    Reserves headroom for the "[truncated]" suffix so that the FINAL string
    (suffix included) stays under the cap. Truncates by characters via
    binary search because a partial-token slice is still token-safe (an
    over-cut never undercounts).
    """
    if _count_tokens(text) <= cap:
        return text

    suffix = "\n... [truncated to fit context window]"
    suffix_tokens = _count_tokens(suffix)
    # Leave a small additional margin so the final concatenation is safely
    # below the cap even if tiktoken sees boundary tokens at the splice.
    effective_cap = max(1, cap - suffix_tokens - 4)

    # Binary-search the character cut-off
    lo, hi = 0, len(text)
    best = ""
    while lo < hi:
        mid = (lo + hi + 1) // 2
        candidate = text[:mid]
        if _count_tokens(candidate) <= effective_cap:
            best = candidate
            lo = mid
        else:
            hi = mid - 1
    return best.rstrip() + suffix


# ---------------------------------------------------------------------------
# Formatting
# ---------------------------------------------------------------------------


def _format_stocktwits(payload: dict[str, Any]) -> str:
    if not payload.get("available"):
        return f"**StockTwits** (unavailable: {payload.get('error') or 'unknown'})"
    n_recent = payload.get("recent_post_count_24h", 0)
    bullish = payload.get("bullish_count", 0)
    bearish = payload.get("bearish_count", 0)
    neutral = payload.get("neutral_count", 0)
    lines = [
        f"**StockTwits** (n={n_recent} in 24h, {payload.get('total_messages', 0)} most-recent)  "
        f"Bullish: {bullish}  Bearish: {bearish}  Neutral: {neutral}"
    ]
    top = payload.get("top_posts") or []
    if top:
        lines.append("Top posts:")
        for p in top:
            body = p.get("body", "").strip()
            if not body:
                continue
            likes = p.get("likes", 0)
            lines.append(f'- "{body}" - {likes} likes')
    return "\n".join(lines)


def _format_reddit(payload: dict[str, Any]) -> str:
    if not payload.get("available"):
        return f"**Reddit** (unavailable: {payload.get('error') or 'unknown'})"
    subs = payload.get("subreddits_searched") or []
    sub_label = "|".join(f"r/{s}" for s in subs)
    n = payload.get("mention_count", 0)
    lines = [f"**Reddit** ({n} mentions, {sub_label})"]
    top = payload.get("top_posts") or []
    for p in top:
        title = (p.get("title") or "").strip()
        if not title:
            continue
        score = p.get("score", 0)
        sub = p.get("subreddit", "")
        lines.append(f'- "{title}" - {score} score (r/{sub})')
    if not top:
        lines.append("- (no posts in last 24h)")
    return "\n".join(lines)


def _humanize_age(published_at: str, now: datetime | None = None) -> str:
    """Return a relative-time label like ``4h ago`` from an ISO-8601 timestamp."""
    if not published_at:
        return "unknown time"
    try:
        ts = published_at.rstrip("Z")
        dt = datetime.fromisoformat(ts)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
    except ValueError:
        return "unknown time"
    now = now or datetime.now(timezone.utc)
    delta = now - dt
    seconds = int(delta.total_seconds())
    if seconds < 60:
        return f"{seconds}s ago"
    if seconds < 3600:
        return f"{seconds // 60}m ago"
    if seconds < 86400:
        return f"{seconds // 3600}h ago"
    return f"{seconds // 86400}d ago"


def _format_yahoo(payload: dict[str, Any], now: datetime | None = None) -> str:
    if not payload.get("available"):
        return f"**Yahoo News** (unavailable: {payload.get('error') or 'unknown'})"
    headlines = payload.get("headlines") or []
    lines = [f"**Yahoo News** ({len(headlines)} most recent)"]
    for h in headlines:
        title = (h.get("title") or "").strip()
        if not title:
            continue
        publisher = h.get("publisher") or "Unknown"
        age = _humanize_age(h.get("published_at", ""), now=now)
        lines.append(f'- "{title}" - {publisher} - {age}')
    if len(lines) == 1:
        lines.append("- (no headlines available)")
    return "\n".join(lines)


def format_block(
    ticker: str,
    date_str: str,
    stocktwits_payload: dict[str, Any],
    reddit_payload: dict[str, Any],
    yahoo_payload: dict[str, Any],
    *,
    token_cap: int = _TOKEN_CAP,
) -> str:
    """Assemble the formatted retail-sentiment block.

    Always returns a non-empty string. If every source is unavailable the
    block clearly says so — the agent should treat that as "no signal" and
    weight other inputs accordingly.
    """
    parts = [
        f"## Retail sentiment for {ticker.upper()} ({date_str})",
        "",
        _format_stocktwits(stocktwits_payload),
        "",
        _format_reddit(reddit_payload),
        "",
        _format_yahoo(yahoo_payload),
    ]
    block = "\n".join(parts).rstrip() + "\n"
    return _truncate_to_token_cap(block, cap=token_cap)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def fetch_grounded_sentiment(
    ticker: str,
    date: str | None = None,
    *,
    force_refresh: bool = False,
    token_cap: int = _TOKEN_CAP,
) -> str:
    """Pre-fetch retail sentiment from 3 sources and return a formatted block.

    Used by the Sentiment Analyst as the entire body of its system message.
    Never raises — degrades gracefully when any/all sources are down.
    """
    ticker = ticker.upper()
    date_str = date or datetime.now(timezone.utc).strftime("%Y-%m-%d")

    stocktwits = fetch_stocktwits(ticker, date=date_str, force_refresh=force_refresh)
    reddit = fetch_reddit(ticker, date=date_str, force_refresh=force_refresh)
    yahoo = fetch_yahoo_news(ticker, date=date_str, force_refresh=force_refresh)

    return format_block(
        ticker, date_str, stocktwits, reddit, yahoo, token_cap=token_cap
    )


def refresh_ticker(ticker: str, date: str | None = None) -> dict[str, Any]:
    """Force-refresh all three sources and return a status summary.

    Used by the Hermes cron job. Does not return the formatted block —
    callers that want the block should call ``fetch_grounded_sentiment``.
    """
    ticker = ticker.upper()
    date_str = date or datetime.now(timezone.utc).strftime("%Y-%m-%d")

    st = fetch_stocktwits(ticker, date=date_str, force_refresh=True)
    rd = fetch_reddit(ticker, date=date_str, force_refresh=True)
    yh = fetch_yahoo_news(ticker, date=date_str, force_refresh=True)

    return {
        "ticker": ticker,
        "date": date_str,
        "stocktwits_ok": bool(st.get("available")),
        "reddit_ok": bool(rd.get("available")),
        "yahoo_ok": bool(yh.get("available")),
        "stocktwits_total": st.get("total_messages", 0),
        "reddit_mentions": rd.get("mention_count", 0),
        "yahoo_headlines": len(yh.get("headlines") or []),
    }


def _load_universe_tickers() -> list[str]:
    """Read the Shark + Wheel universe from ``user_data/universe.json``."""
    try:
        data = json.loads(_UNIVERSE_PATH.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("Could not read universe.json at %s: %s", _UNIVERSE_PATH, exc)
        return []
    stocks = data.get("stocks") or {}
    wheel = stocks.get("wheel_universe") or []
    dashboard = stocks.get("dashboard_basket") or []
    # De-dup while preserving order
    seen: set[str] = set()
    tickers: list[str] = []
    for t in list(wheel) + list(dashboard):
        u = str(t).upper().strip()
        if u and u not in seen:
            seen.add(u)
            tickers.append(u)
    return tickers


# ---------------------------------------------------------------------------
# CLI entrypoint
# ---------------------------------------------------------------------------


def _main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m shark.data.sentiment",
        description="Grounded sentiment pre-fetch for the Shark Sentiment Analyst.",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_refresh = sub.add_parser("refresh", help="Force-refresh one ticker's cache.")
    p_refresh.add_argument("--ticker", required=True)
    p_refresh.add_argument("--date", default=None)

    p_block = sub.add_parser("block", help="Print the formatted block for a ticker.")
    p_block.add_argument("--ticker", required=True)
    p_block.add_argument("--date", default=None)
    p_block.add_argument("--force", action="store_true")

    sub.add_parser(
        "refresh-universe",
        help="Refresh every ticker in user_data/universe.json (cron entry point).",
    )

    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    if args.cmd == "refresh":
        status = refresh_ticker(args.ticker, args.date)
        print(json.dumps(status, indent=2))
        return 0

    if args.cmd == "block":
        block = fetch_grounded_sentiment(
            args.ticker, args.date, force_refresh=args.force
        )
        print(block)
        return 0

    if args.cmd == "refresh-universe":
        tickers = _load_universe_tickers()
        if not tickers:
            print("No tickers found in universe.json", file=sys.stderr)
            return 1
        results = []
        for t in tickers:
            try:
                results.append(refresh_ticker(t))
            except Exception as exc:  # belt-and-braces; the inner calls fail-soft already
                logger.warning("refresh failed for %s: %s", t, exc)
                results.append({"ticker": t, "error": type(exc).__name__})
        print(json.dumps({"refreshed": results, "count": len(results)}, indent=2))
        return 0

    parser.print_help()
    return 2


if __name__ == "__main__":
    raise SystemExit(_main())


__all__ = [
    "fetch_grounded_sentiment",
    "refresh_ticker",
    "format_block",
]
