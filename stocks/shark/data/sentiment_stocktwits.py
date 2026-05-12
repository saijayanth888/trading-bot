"""StockTwits public symbol-stream fetcher with on-disk caching.

StockTwits exposes a per-symbol message stream at
``api.stocktwits.com/api/2/streams/symbol/{ticker}.json`` that requires no
API key, no OAuth, and no registration. Each message includes a
user-labeled sentiment field (``Bullish``/``Bearish``/null), the message
body, timestamp, like count, and posting user.

Pattern adapted from TradingAgents v0.2.5 (Apache-2.0,
``tradingagents/dataflows/stocktwits.py``). The major differences from the
upstream pattern are:

* Caches the parsed result to ``stocks/kb/sentiment/stocktwits/`` keyed by
  ``ticker_YYYY-MM-DD.json`` with a 30-minute TTL — the cron job populates
  the cache and the live agent reads from it.
* Returns a structured dict (``bullish_count``, ``bearish_count``, ...)
  rather than a pre-formatted string. The aggregator in ``sentiment.py``
  owns formatting.
* Fail-soft: never raises. The caller always gets back a dict — failures
  surface via the ``available`` and ``error`` keys.

Rate-limit ceiling (observed): unauthenticated, ~200 requests/hour per IP
before HTTP 429. With a 14-symbol universe and a 30-minute cron cadence
that is ~28 calls/hour — well under the cap.
"""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

logger = logging.getLogger(__name__)

_API = "https://api.stocktwits.com/api/2/streams/symbol/{ticker}.json"
_UA = "shark-trading-bot/1.0 (+sentiment-pre-fetch)"

_KB_ROOT = Path(__file__).resolve().parents[2] / "kb" / "sentiment" / "stocktwits"
_CACHE_TTL_SECONDS = 30 * 60  # 30 minutes — matches the Hermes cron cadence


def _cache_path(ticker: str, date_str: str) -> Path:
    return _KB_ROOT / f"{ticker.upper()}_{date_str}.json"


def _read_cache(ticker: str, date_str: str) -> dict[str, Any] | None:
    path = _cache_path(ticker, date_str)
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("StockTwits cache unreadable for %s: %s", ticker, exc)
        return None
    cached_at = payload.get("_cached_at_epoch")
    if not isinstance(cached_at, (int, float)):
        return None
    if (time.time() - cached_at) > _CACHE_TTL_SECONDS:
        return None
    return payload


def _write_cache(ticker: str, date_str: str, payload: dict[str, Any]) -> None:
    try:
        _KB_ROOT.mkdir(parents=True, exist_ok=True)
        path = _cache_path(ticker, date_str)
        payload = dict(payload)
        payload["_cached_at_epoch"] = time.time()
        path.write_text(json.dumps(payload, indent=2, default=str))
    except OSError as exc:
        logger.warning("StockTwits cache write failed for %s: %s", ticker, exc)


def _is_recent(created_at: str, hours: int = 24) -> bool:
    """StockTwits timestamps are ISO-8601 UTC like ``2026-05-11T18:42:03Z``."""
    if not created_at:
        return False
    try:
        # Tolerate trailing Z which fromisoformat in older Python can't parse
        ts = created_at.rstrip("Z")
        dt = datetime.fromisoformat(ts).replace(tzinfo=timezone.utc)
    except ValueError:
        return False
    return (datetime.now(timezone.utc) - dt) <= timedelta(hours=hours)


def fetch_stocktwits(
    ticker: str,
    *,
    date: str | None = None,
    limit: int = 30,
    timeout: float = 10.0,
    use_cache: bool = True,
    force_refresh: bool = False,
) -> dict[str, Any]:
    """Fetch and summarize the most recent StockTwits stream for ``ticker``.

    Returns a dict shaped like::

        {
          "ticker": "NVDA",
          "available": True,
          "bullish_count": 18,
          "bearish_count": 4,
          "neutral_count": 6,
          "recent_post_count_24h": 22,
          "total_messages": 28,
          "top_posts": [{"body": ..., "likes": 84, "user": "..."}, ...],
          "error": None,
        }

    Never raises. On failure ``available`` is ``False`` and ``error`` carries
    the exception class name.
    """
    ticker = ticker.upper()
    date_str = date or datetime.now(timezone.utc).strftime("%Y-%m-%d")

    if use_cache and not force_refresh:
        cached = _read_cache(ticker, date_str)
        if cached is not None:
            return cached

    url = _API.format(ticker=ticker)
    req = Request(url, headers={"User-Agent": _UA, "Accept": "application/json"})
    try:
        with urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read())
    except (HTTPError, URLError, json.JSONDecodeError, TimeoutError, OSError) as exc:
        logger.warning("StockTwits fetch failed for %s: %s", ticker, exc)
        return {
            "ticker": ticker,
            "available": False,
            "bullish_count": 0,
            "bearish_count": 0,
            "neutral_count": 0,
            "recent_post_count_24h": 0,
            "total_messages": 0,
            "top_posts": [],
            "error": type(exc).__name__,
        }

    messages = data.get("messages", []) if isinstance(data, dict) else []
    messages = messages[:limit]

    bullish = bearish = neutral = recent_24h = 0
    enriched: list[dict[str, Any]] = []
    for m in messages:
        sentiment_obj = (m.get("entities") or {}).get("sentiment") or {}
        sentiment = (
            sentiment_obj.get("basic") if isinstance(sentiment_obj, dict) else None
        )
        if sentiment == "Bullish":
            bullish += 1
        elif sentiment == "Bearish":
            bearish += 1
        else:
            neutral += 1

        created_at = m.get("created_at", "") or ""
        if _is_recent(created_at, hours=24):
            recent_24h += 1

        body = (m.get("body") or "").replace("\n", " ").strip()
        if len(body) > 240:
            body = body[:240] + "..."
        likes_obj = m.get("likes") or {}
        likes = likes_obj.get("total", 0) if isinstance(likes_obj, dict) else 0
        user = (m.get("user") or {}).get("username", "?")
        enriched.append(
            {
                "body": body,
                "likes": int(likes or 0),
                "user": user,
                "sentiment": sentiment or "None",
                "created_at": created_at,
            }
        )

    enriched.sort(key=lambda p: p["likes"], reverse=True)
    top_posts = enriched[:3]

    result: dict[str, Any] = {
        "ticker": ticker,
        "available": True,
        "bullish_count": bullish,
        "bearish_count": bearish,
        "neutral_count": neutral,
        "recent_post_count_24h": recent_24h,
        "total_messages": len(messages),
        "top_posts": top_posts,
        "error": None,
    }

    if use_cache:
        _write_cache(ticker, date_str, result)
    return result


__all__ = ["fetch_stocktwits"]
