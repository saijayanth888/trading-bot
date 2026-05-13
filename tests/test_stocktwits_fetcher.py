"""Tests for user_data.modules.stocktwits.fetch_stocktwits_symbol_stream."""
from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest

from user_data.modules.stocktwits import STItem, fetch_stocktwits_symbol_stream


@pytest.mark.asyncio
async def test_fetch_symbol_stream_basic() -> None:
    fake = {
        "messages": [
            {
                "id": 1,
                "body": "$NVDA breaking out",
                "created_at": "2026-05-13T11:00:00Z",
                "entities": {"sentiment": {"basic": "Bullish"}},
                "likes": {"total": 12},
                "user": {"username": "trader1"},
            },
            {
                "id": 2,
                "body": "$NVDA reversing hard, watch the 200ma",
                "created_at": "2026-05-13T11:05:00Z",
                "entities": {"sentiment": {"basic": "Bearish"}},
                "likes": {"total": 5},
                "user": {"username": "trader2"},
            },
        ]
    }
    with patch(
        "user_data.modules.stocktwits._http_get_json",
        new=AsyncMock(return_value=fake),
    ):
        items = await fetch_stocktwits_symbol_stream("NVDA", limit=10)

    assert len(items) == 2
    assert items[0].symbol == "NVDA"
    assert items[0].sentiment == "Bullish"
    assert items[0].likes == 12
    assert items[0].user == "trader1"
    assert isinstance(items[0].ts, datetime)
    assert items[0].ts.tzinfo == timezone.utc
    assert items[1].sentiment == "Bearish"


@pytest.mark.asyncio
async def test_sentiment_can_be_none() -> None:
    """Most posts don't carry an explicit Bull/Bear tag — sentiment=None is normal."""
    fake = {
        "messages": [
            {
                "id": 3,
                "body": "$AAPL looking interesting today",
                "created_at": "2026-05-13T12:00:00Z",
                "entities": {},
                "likes": {"total": 0},
                "user": {"username": "neutral_observer"},
            }
        ]
    }
    with patch(
        "user_data.modules.stocktwits._http_get_json",
        new=AsyncMock(return_value=fake),
    ):
        items = await fetch_stocktwits_symbol_stream("AAPL", limit=10)

    assert items[0].sentiment is None


@pytest.mark.asyncio
async def test_empty_messages_returns_empty_list() -> None:
    """A symbol with no recent activity returns []. Don't crash."""
    fake = {"messages": []}
    with patch(
        "user_data.modules.stocktwits._http_get_json",
        new=AsyncMock(return_value=fake),
    ):
        items = await fetch_stocktwits_symbol_stream("OBSCURE", limit=10)

    assert items == []


@pytest.mark.asyncio
async def test_body_is_truncated() -> None:
    """Long bodies are truncated to 400 chars to keep aggregator payloads small."""
    long_body = "x" * 1000
    fake = {
        "messages": [
            {
                "id": 99,
                "body": long_body,
                "created_at": "2026-05-13T12:00:00Z",
                "entities": {},
                "likes": {"total": 0},
                "user": {"username": "tester"},
            }
        ]
    }
    with patch(
        "user_data.modules.stocktwits._http_get_json",
        new=AsyncMock(return_value=fake),
    ):
        items = await fetch_stocktwits_symbol_stream("NVDA", limit=1)

    assert len(items[0].body) == 400


@pytest.mark.asyncio
async def test_limit_caps_results() -> None:
    fake = {
        "messages": [
            {
                "id": i,
                "body": f"msg {i}",
                "created_at": "2026-05-13T12:00:00Z",
                "entities": {},
                "likes": {"total": 0},
                "user": {"username": "u"},
            }
            for i in range(20)
        ]
    }
    with patch(
        "user_data.modules.stocktwits._http_get_json",
        new=AsyncMock(return_value=fake),
    ):
        items = await fetch_stocktwits_symbol_stream("NVDA", limit=5)

    assert len(items) == 5
