"""Tests for user_data.modules.hackernews.fetch_hn_top."""
from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest

from user_data.modules.hackernews import HNItem, fetch_hn_top


@pytest.mark.asyncio
async def test_fetch_hn_top_returns_items() -> None:
    fake_top = [40000001, 40000002]
    fake_item_1 = {
        "id": 40000001,
        "title": "Bitcoin hits $82k",
        "url": "https://example.com/btc",
        "by": "alice",
        "score": 350,
        "descendants": 120,
        "time": 1747000000,
        "type": "story",
    }
    fake_item_2 = {
        "id": 40000002,
        "title": "NVDA earnings beat",
        "url": "https://example.com/nvda",
        "by": "bob",
        "score": 220,
        "descendants": 80,
        "time": 1747000600,
        "type": "story",
    }
    with patch(
        "user_data.modules.hackernews._http_get_json",
        new=AsyncMock(side_effect=[fake_top, fake_item_1, fake_item_2]),
    ):
        items = await fetch_hn_top(limit=2)

    assert len(items) == 2
    assert items[0].title == "Bitcoin hits $82k"
    assert items[0].score == 350
    assert items[0].descendants == 120
    assert items[0].url == "https://example.com/btc"
    assert isinstance(items[0].ts, datetime)
    assert items[1].title == "NVDA earnings beat"


@pytest.mark.asyncio
async def test_skips_non_story_items() -> None:
    """Jobs / Ask / poll items are skipped — only `story` is fetched."""
    fake_top = [1, 2]
    fake_story = {
        "id": 1,
        "title": "Real story",
        "url": "https://x.com",
        "score": 10,
        "descendants": 0,
        "time": 1747000000,
        "type": "story",
    }
    fake_job = {
        "id": 2,
        "title": "Hiring Senior Dev",
        "score": 5,
        "time": 1747000000,
        "type": "job",
    }
    with patch(
        "user_data.modules.hackernews._http_get_json",
        new=AsyncMock(side_effect=[fake_top, fake_story, fake_job]),
    ):
        items = await fetch_hn_top(limit=10)

    assert len(items) == 1
    assert items[0].title == "Real story"


@pytest.mark.asyncio
async def test_skips_items_missing_title() -> None:
    """A null/empty title means the API returned a deleted/dead item — skip."""
    fake_top = [1, 2]
    fake_good = {
        "id": 1, "title": "Live story", "url": "https://x.com",
        "score": 100, "descendants": 0, "time": 1747000000, "type": "story",
    }
    fake_dead = {"id": 2, "deleted": True, "time": 1747000000, "type": "story"}
    with patch(
        "user_data.modules.hackernews._http_get_json",
        new=AsyncMock(side_effect=[fake_top, fake_good, fake_dead]),
    ):
        items = await fetch_hn_top(limit=10)

    assert len(items) == 1


@pytest.mark.asyncio
async def test_timestamp_is_utc_aware() -> None:
    fake_top = [1]
    fake_item = {
        "id": 1, "title": "TS test", "url": None,
        "score": 1, "descendants": 0, "time": 1747000000, "type": "story",
    }
    with patch(
        "user_data.modules.hackernews._http_get_json",
        new=AsyncMock(side_effect=[fake_top, fake_item]),
    ):
        items = await fetch_hn_top(limit=1)

    assert items[0].ts.tzinfo is not None
    assert items[0].ts.tzinfo == timezone.utc


@pytest.mark.asyncio
async def test_url_can_be_none_for_text_posts() -> None:
    """Ask-HN style stories have no URL; the fetcher should not crash."""
    fake_top = [1]
    fake_item = {
        "id": 1, "title": "Ask HN: How do you trade?",
        "score": 50, "descendants": 20, "time": 1747000000, "type": "story",
    }
    with patch(
        "user_data.modules.hackernews._http_get_json",
        new=AsyncMock(side_effect=[fake_top, fake_item]),
    ):
        items = await fetch_hn_top(limit=1)

    assert items[0].url is None
