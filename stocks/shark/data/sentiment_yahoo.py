"""Yahoo Finance news fetcher with on-disk caching.

Thin wrapper around ``yfinance.Ticker(ticker).news``. ``yfinance`` is
already pulled in by ``shark.agents.outcome_resolver`` for return
calculations, so it is in the runtime image. We import lazily so a missing
``yfinance`` install fails soft rather than breaking the aggregator.

Caches to ``stocks/kb/sentiment/yahoo/{ticker}_YYYY-MM-DD.json`` with a
30-minute TTL — matching the other sentiment sources and the cron cadence.

Yahoo's news endpoint is unauthenticated and unrated officially. We have
seen it tolerate ~2 calls/second per IP without throttling. With our
30-minute cron over a small universe this is comfortably below any plausible
limit.
"""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_KB_ROOT = Path(__file__).resolve().parents[2] / "kb" / "sentiment" / "yahoo"
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
        logger.warning("Yahoo cache unreadable for %s: %s", ticker, exc)
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
        logger.warning("Yahoo cache write failed for %s: %s", ticker, exc)


def _normalize_news_item(item: Any) -> dict[str, Any] | None:
    """yfinance has changed news shapes between versions; tolerate both.

    Old shape (pre-0.2.40):
        {"title": ..., "publisher": ..., "providerPublishTime": <epoch>, "link": ...}

    New shape (0.2.40+):
        {"id": ..., "content": {"title": ..., "provider": {"displayName": ...},
                                "pubDate": "<iso8601>", "canonicalUrl": {...}}}
    """
    if not isinstance(item, dict):
        return None

    # New shape
    content = item.get("content")
    if isinstance(content, dict):
        title = content.get("title") or ""
        provider = content.get("provider") or {}
        publisher = (
            provider.get("displayName") if isinstance(provider, dict) else ""
        ) or ""
        pub_date = content.get("pubDate") or content.get("displayTime") or ""
        link = ""
        url_obj = content.get("canonicalUrl")
        if isinstance(url_obj, dict):
            link = url_obj.get("url", "") or ""
        return {
            "title": str(title),
            "publisher": str(publisher),
            "published_at": str(pub_date),
            "link": str(link),
        }

    # Old shape
    title = item.get("title") or ""
    publisher = item.get("publisher") or ""
    epoch = item.get("providerPublishTime")
    if isinstance(epoch, (int, float)) and epoch > 0:
        published_at = datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat()
    else:
        published_at = ""
    return {
        "title": str(title),
        "publisher": str(publisher),
        "published_at": published_at,
        "link": str(item.get("link") or ""),
    }


def fetch_yahoo_news(
    ticker: str,
    *,
    date: str | None = None,
    limit: int = 5,
    use_cache: bool = True,
    force_refresh: bool = False,
) -> dict[str, Any]:
    """Fetch the most recent Yahoo Finance headlines for ``ticker``.

    Returns::

        {
          "ticker": "NVDA",
          "available": True,
          "headlines": [
              {"title": ..., "publisher": ..., "published_at": ..., "link": ...},
              ...
          ],
          "error": None,
        }

    Never raises. Missing ``yfinance`` library is treated as a soft failure
    — the caller will see ``available: False`` and ``error: "ImportError"``.
    """
    ticker = ticker.upper()
    date_str = date or datetime.now(timezone.utc).strftime("%Y-%m-%d")

    if use_cache and not force_refresh:
        cached = _read_cache(ticker, date_str)
        if cached is not None:
            return cached

    try:
        import yfinance as yf  # type: ignore
    except ImportError as exc:
        logger.warning("yfinance not installed — Yahoo news unavailable: %s", exc)
        return {
            "ticker": ticker,
            "available": False,
            "headlines": [],
            "error": "ImportError",
        }

    try:
        raw_news = yf.Ticker(ticker).news or []
    except Exception as exc:  # yfinance can raise many things; treat all as soft
        logger.warning("Yahoo news fetch failed for %s: %s", ticker, exc)
        return {
            "ticker": ticker,
            "available": False,
            "headlines": [],
            "error": type(exc).__name__,
        }

    headlines: list[dict[str, Any]] = []
    for item in raw_news[:limit]:
        norm = _normalize_news_item(item)
        if norm and norm["title"]:
            headlines.append(norm)

    result: dict[str, Any] = {
        "ticker": ticker,
        "available": True,
        "headlines": headlines,
        "error": None,
    }

    if use_cache:
        _write_cache(ticker, date_str, result)
    return result


__all__ = ["fetch_yahoo_news"]
