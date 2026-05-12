"""Reddit ticker-search fetcher with on-disk caching.

Uses the public ``reddit.com/r/{sub}/search.json`` JSON endpoints, which
do not require an API key. Public throughput is ~10 requests/minute per
IP — adequate for a 30-minute cron over a 14-symbol universe.

Pattern adapted from TradingAgents v0.2.5 (Apache-2.0,
``tradingagents/dataflows/reddit.py``). Differences from upstream:

* Cached to ``stocks/kb/sentiment/reddit/{ticker}_YYYY-MM-DD.json`` with a
  30-minute TTL.
* Returns a structured dict (mention_count, top_posts) instead of formatted
  text — the aggregator owns formatting.
* Fail-soft: every code path returns a dict; nothing raises. Reddit's anti-
  bot rejection (HTTP 403/429) is treated like a missing source, with
  ``available: False`` and an ``error`` tag.
* No PRAW dependency. We try the public JSON endpoints first; if a future
  operator wants OAuth, the caller can switch to PRAW without API changes
  (just swap this module's internals).

Rate-limit ceiling (observed): unauthenticated ~10 req/min per IP. With 3
subreddits per ticker and 14 symbols on a 30-minute cadence we issue
~84 calls per refresh — paced at ~0.4s between requests stays inside
the bucket.
"""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

logger = logging.getLogger(__name__)

_API = "https://www.reddit.com/r/{sub}/search.json?{qs}"
_UA = "shark-trading-bot/1.0 (+sentiment-pre-fetch)"

DEFAULT_SUBREDDITS = ("wallstreetbets", "stocks", "investing")

_KB_ROOT = Path(__file__).resolve().parents[2] / "kb" / "sentiment" / "reddit"
_CACHE_TTL_SECONDS = 30 * 60


def _cache_path(ticker: str, date_str: str) -> Path:
    return _KB_ROOT / f"{ticker.upper()}_{date_str}.json"


def _read_cache(ticker: str, date_str: str) -> dict[str, Any] | None:
    path = _cache_path(ticker, date_str)
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("Reddit cache unreadable for %s: %s", ticker, exc)
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
        logger.warning("Reddit cache write failed for %s: %s", ticker, exc)


def _fetch_subreddit(
    ticker: str,
    sub: str,
    limit: int,
    timeout: float,
) -> tuple[list[dict[str, Any]], str | None]:
    """Return ``(posts, error)`` — never raises."""
    qs = urlencode(
        {
            "q": ticker,
            "restrict_sr": "on",
            "sort": "new",
            "t": "day",  # Reddit accepts hour/day/week/month — day == ~24h
            "limit": limit,
        }
    )
    url = _API.format(sub=sub, qs=qs)
    req = Request(url, headers={"User-Agent": _UA, "Accept": "application/json"})
    try:
        with urlopen(req, timeout=timeout) as resp:
            payload = json.loads(resp.read())
    except (HTTPError, URLError, json.JSONDecodeError, TimeoutError, OSError) as exc:
        logger.warning("Reddit fetch failed for r/%s · %s: %s", sub, ticker, exc)
        return [], type(exc).__name__
    children = (payload.get("data") or {}).get("children") or []
    posts = [c.get("data", {}) for c in children if isinstance(c, dict)]
    return posts, None


def fetch_reddit(
    ticker: str,
    *,
    date: str | None = None,
    subreddits: Iterable[str] = DEFAULT_SUBREDDITS,
    limit_per_sub: int = 10,
    timeout: float = 10.0,
    inter_request_delay: float = 0.4,
    use_cache: bool = True,
    force_refresh: bool = False,
) -> dict[str, Any]:
    """Fetch recent Reddit posts mentioning ``ticker`` across finance subs.

    Returns::

        {
          "ticker": "NVDA",
          "available": True,
          "mention_count": 12,
          "subreddits_searched": ["wallstreetbets", "stocks", "investing"],
          "top_posts": [
              {"title": "...", "score": 312, "comments": 88, "subreddit": "wallstreetbets"},
              ...
          ],
          "error": None,
        }

    Never raises. Treats per-subreddit failures as soft — if at least one
    sub responds, ``available`` is ``True``. If every sub fails, returns
    ``available: False`` and the last error class.
    """
    ticker = ticker.upper()
    date_str = date or datetime.now(timezone.utc).strftime("%Y-%m-%d")
    subs = list(subreddits)

    if use_cache and not force_refresh:
        cached = _read_cache(ticker, date_str)
        if cached is not None:
            return cached

    all_posts: list[dict[str, Any]] = []
    errors: list[str] = []
    successes = 0
    for i, sub in enumerate(subs):
        if i > 0:
            time.sleep(inter_request_delay)
        posts, err = _fetch_subreddit(ticker, sub, limit_per_sub, timeout)
        if err is not None:
            errors.append(f"r/{sub}:{err}")
            continue
        successes += 1
        for p in posts:
            all_posts.append(
                {
                    "title": (p.get("title") or "").replace("\n", " ").strip(),
                    "score": int(p.get("score", 0) or 0),
                    "comments": int(p.get("num_comments", 0) or 0),
                    "subreddit": sub,
                    "created_utc": p.get("created_utc"),
                }
            )

    available = successes > 0
    all_posts.sort(key=lambda p: p["score"], reverse=True)
    top_posts = all_posts[:3]

    result: dict[str, Any] = {
        "ticker": ticker,
        "available": available,
        "mention_count": len(all_posts),
        "subreddits_searched": subs,
        "top_posts": top_posts,
        "error": "; ".join(errors) if errors and not available else None,
    }
    if errors and available:
        # Partial failure: keep the data but record it for debugging
        result["partial_errors"] = errors

    if use_cache:
        _write_cache(ticker, date_str, result)
    return result


__all__ = ["fetch_reddit", "DEFAULT_SUBREDDITS"]
