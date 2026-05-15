"""Hacker News front-page fetcher for the sentiment aggregator.

Uses the Firebase API (no auth, no key, public). Pulls top stories and
filters out job posts, deleted items, and items with missing titles.

The aggregator at `user_data.modules.news_aggregator.NewsAggregator`
wraps this via `_fetch_hackernews` and converts HNItem → NewsItem with
source="hackernews".

Endpoint refs:
    https://github.com/HackerNews/API
    GET /v0/topstories.json         → list[int] of story ids (top 500)
    GET /v0/item/{id}.json          → individual item details

Rate limit: none documented; we cap at limit=30 by default which is
~31 HTTP calls per refresh. Free, no key.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime

import aiohttp

logger = logging.getLogger(__name__)

_HN_BASE = "https://hacker-news.firebaseio.com/v0"


@dataclass(frozen=True)
class HNItem:
    """Normalized HN story."""

    id: int
    title: str
    url: str | None
    score: int
    descendants: int
    ts: datetime


async def _http_get_json(session: aiohttp.ClientSession, url: str) -> dict | list:
    """Tiny wrapper so tests can monkey-patch network at one seam."""
    async with session.get(
        url, timeout=aiohttp.ClientTimeout(total=10),
    ) as r:
        r.raise_for_status()
        return await r.json()


async def fetch_hn_top(limit: int = 30) -> list[HNItem]:
    """Fetch top HN stories. Filters non-story / dead / titleless items.

    Returns at most ``limit`` items, in HN-ranking order.
    """
    out: list[HNItem] = []
    async with aiohttp.ClientSession() as session:
        try:
            ids = await _http_get_json(session, f"{_HN_BASE}/topstories.json")
        except Exception as exc:
            logger.warning("hackernews topstories fetch failed: %s", exc)
            return out

        # Take a generous oversample so filtered items don't starve `limit`.
        candidate_ids = list(ids)[: max(limit * 2, 60)]

        for hid in candidate_ids:
            if len(out) >= limit:
                break
            try:
                item = await _http_get_json(session, f"{_HN_BASE}/item/{hid}.json")
            except Exception as exc:
                logger.warning("hackernews item %s fetch failed: %s", hid, exc)
                continue
            if not isinstance(item, dict):
                continue
            if item.get("deleted") or item.get("dead"):
                continue
            if item.get("type") != "story":
                continue
            title = item.get("title")
            if not title:
                continue
            out.append(
                HNItem(
                    id=int(item["id"]),
                    title=str(title),
                    url=item.get("url"),
                    score=int(item.get("score", 0)),
                    descendants=int(item.get("descendants", 0)),
                    ts=datetime.fromtimestamp(int(item["time"]), tz=UTC),
                )
            )
    return out
