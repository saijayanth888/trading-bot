"""Tests for the grounded sentiment pre-fetch pipeline.

Covers:
* HTTP-layer mocking for each of the 3 sources
* Aggregator graceful degradation under 1, 2, and 3 source failures
* Token-cap enforcement on the formatted block
* Cache TTL boundary behavior
"""

from __future__ import annotations

import io
import json
import time
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from shark.data import sentiment as agg
from shark.data import sentiment_reddit as rd
from shark.data import sentiment_stocktwits as st
from shark.data import sentiment_yahoo as yh


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _fake_urlopen_response(payload: dict[str, Any]) -> MagicMock:
    """Build a mock that mimics urllib.request.urlopen's context manager."""
    fake = MagicMock()
    fake.read.return_value = json.dumps(payload).encode("utf-8")
    cm = MagicMock()
    cm.__enter__ = MagicMock(return_value=fake)
    cm.__exit__ = MagicMock(return_value=False)
    return cm


@pytest.fixture(autouse=True)
def _isolate_cache(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect every module's cache root into a fresh temp dir.

    Prevents tests from mutating the real ``stocks/kb/sentiment/`` tree and
    guarantees TTL tests start from a cold cache.
    """
    st._KB_ROOT = tmp_path / "stocktwits"
    rd._KB_ROOT = tmp_path / "reddit"
    yh._KB_ROOT = tmp_path / "yahoo"
    return tmp_path


# ---------------------------------------------------------------------------
# StockTwits
# ---------------------------------------------------------------------------


class TestStockTwits:
    def test_happy_path_counts_sentiment(self) -> None:
        payload = {
            "messages": [
                {
                    "created_at": "2026-05-11T18:00:00Z",
                    "body": "NVDA to the moon",
                    "user": {"username": "alice"},
                    "entities": {"sentiment": {"basic": "Bullish"}},
                    "likes": {"total": 10},
                },
                {
                    "created_at": "2026-05-11T17:30:00Z",
                    "body": "puts on NVDA",
                    "user": {"username": "bob"},
                    "entities": {"sentiment": {"basic": "Bearish"}},
                    "likes": {"total": 4},
                },
                {
                    "created_at": "2026-05-11T17:00:00Z",
                    "body": "watching NVDA",
                    "user": {"username": "carol"},
                    "entities": None,
                    "likes": {"total": 1},
                },
            ]
        }
        with patch.object(st, "urlopen", return_value=_fake_urlopen_response(payload)):
            with patch("shark.data.sentiment_stocktwits._is_recent", return_value=True):
                out = st.fetch_stocktwits("NVDA", date="2026-05-11", use_cache=False)

        assert out["available"] is True
        assert out["bullish_count"] == 1
        assert out["bearish_count"] == 1
        assert out["neutral_count"] == 1
        assert out["recent_post_count_24h"] == 3
        assert out["total_messages"] == 3
        # Top by likes
        assert out["top_posts"][0]["likes"] == 10
        assert out["top_posts"][0]["user"] == "alice"

    def test_http_error_returns_unavailable(self) -> None:
        from urllib.error import HTTPError

        err = HTTPError("url", 429, "rate limited", hdrs=None, fp=None)
        with patch.object(st, "urlopen", side_effect=err):
            out = st.fetch_stocktwits("NVDA", use_cache=False)
        assert out["available"] is False
        assert out["error"] == "HTTPError"
        assert out["bullish_count"] == 0
        assert out["top_posts"] == []

    def test_cache_round_trip(self, _isolate_cache: Path) -> None:
        payload = {"messages": [{"created_at": "2026-05-11T18:00:00Z",
                                  "body": "x", "user": {"username": "u"},
                                  "entities": {"sentiment": {"basic": "Bullish"}},
                                  "likes": {"total": 0}}]}
        with patch.object(st, "urlopen", return_value=_fake_urlopen_response(payload)):
            first = st.fetch_stocktwits("AAPL", date="2026-05-11")
        # Second call must hit cache, not HTTP — patch raises if HTTP is touched
        with patch.object(st, "urlopen", side_effect=AssertionError("cache miss!")):
            second = st.fetch_stocktwits("AAPL", date="2026-05-11")
        assert first["bullish_count"] == second["bullish_count"]
        assert second["available"] is True

    def test_cache_ttl_expiry_triggers_refetch(self, _isolate_cache: Path) -> None:
        payload = {"messages": []}
        with patch.object(st, "urlopen", return_value=_fake_urlopen_response(payload)):
            st.fetch_stocktwits("AAPL", date="2026-05-11")

        # Manually age the cache past TTL
        path = st._cache_path("AAPL", "2026-05-11")
        cached = json.loads(path.read_text())
        cached["_cached_at_epoch"] = time.time() - (st._CACHE_TTL_SECONDS + 60)
        path.write_text(json.dumps(cached))

        # Refetch should occur — track the call
        sentinel = MagicMock(return_value=_fake_urlopen_response({"messages": []}))
        with patch.object(st, "urlopen", sentinel):
            st.fetch_stocktwits("AAPL", date="2026-05-11")
        assert sentinel.call_count == 1


# ---------------------------------------------------------------------------
# Reddit
# ---------------------------------------------------------------------------


class TestReddit:
    def test_aggregates_across_subreddits(self) -> None:
        responses = {
            "wallstreetbets": {
                "data": {
                    "children": [
                        {"data": {"title": "NVDA YOLO", "score": 312,
                                   "num_comments": 88, "created_utc": 1000}},
                    ]
                }
            },
            "stocks": {
                "data": {
                    "children": [
                        {"data": {"title": "NVDA earnings", "score": 50,
                                   "num_comments": 12, "created_utc": 999}},
                    ]
                }
            },
            "investing": {"data": {"children": []}},
        }

        def fake_urlopen(req: Any, timeout: float = 0) -> MagicMock:
            url = req.full_url
            for sub, body in responses.items():
                if f"/r/{sub}/search.json" in url:
                    return _fake_urlopen_response(body)
            raise AssertionError(f"unexpected url: {url}")

        with patch.object(rd, "urlopen", side_effect=fake_urlopen), \
             patch.object(rd.time, "sleep", return_value=None):
            out = rd.fetch_reddit("NVDA", date="2026-05-11", use_cache=False)

        assert out["available"] is True
        assert out["mention_count"] == 2
        assert out["top_posts"][0]["score"] == 312
        assert out["top_posts"][0]["subreddit"] == "wallstreetbets"

    def test_partial_subreddit_failure_still_available(self) -> None:
        from urllib.error import HTTPError

        def fake_urlopen(req: Any, timeout: float = 0) -> MagicMock:
            if "/r/wallstreetbets/" in req.full_url:
                raise HTTPError("u", 403, "nope", hdrs=None, fp=None)
            return _fake_urlopen_response({"data": {"children": []}})

        with patch.object(rd, "urlopen", side_effect=fake_urlopen), \
             patch.object(rd.time, "sleep", return_value=None):
            out = rd.fetch_reddit("NVDA", date="2026-05-11", use_cache=False)

        assert out["available"] is True
        assert out["mention_count"] == 0
        assert "partial_errors" in out
        assert any("wallstreetbets" in e for e in out["partial_errors"])

    def test_total_subreddit_failure_unavailable(self) -> None:
        from urllib.error import HTTPError

        def fake_urlopen(req: Any, timeout: float = 0) -> MagicMock:
            raise HTTPError("u", 429, "blocked", hdrs=None, fp=None)

        with patch.object(rd, "urlopen", side_effect=fake_urlopen), \
             patch.object(rd.time, "sleep", return_value=None):
            out = rd.fetch_reddit("NVDA", date="2026-05-11", use_cache=False)

        assert out["available"] is False
        assert "HTTPError" in (out["error"] or "")


# ---------------------------------------------------------------------------
# Yahoo News
# ---------------------------------------------------------------------------


class TestYahoo:
    def test_normalizes_old_shape(self) -> None:
        item = {
            "title": "Nvidia announces Nemotron 3",
            "publisher": "Reuters",
            "providerPublishTime": int(time.time()) - 3600,
            "link": "https://example.com",
        }
        norm = yh._normalize_news_item(item)
        assert norm is not None
        assert norm["title"].startswith("Nvidia")
        assert norm["publisher"] == "Reuters"
        assert "T" in norm["published_at"]

    def test_normalizes_new_shape(self) -> None:
        item = {
            "id": "abc",
            "content": {
                "title": "Apple lifts guidance",
                "provider": {"displayName": "Bloomberg"},
                "pubDate": "2026-05-11T18:00:00Z",
                "canonicalUrl": {"url": "https://example.com"},
            },
        }
        norm = yh._normalize_news_item(item)
        assert norm is not None
        assert norm["title"] == "Apple lifts guidance"
        assert norm["publisher"] == "Bloomberg"
        assert norm["link"] == "https://example.com"

    def test_missing_yfinance_fails_soft(self) -> None:
        # Build the import error by patching the import lookup
        import builtins

        real_import = builtins.__import__

        def fake_import(name: str, *args: Any, **kwargs: Any) -> Any:
            if name == "yfinance":
                raise ImportError("yfinance not installed")
            return real_import(name, *args, **kwargs)

        with patch.object(builtins, "__import__", side_effect=fake_import):
            out = yh.fetch_yahoo_news("NVDA", use_cache=False)
        assert out["available"] is False
        assert out["error"] == "ImportError"

    def test_yfinance_exception_fails_soft(self) -> None:
        fake_yf = MagicMock()
        fake_yf.Ticker.side_effect = RuntimeError("ratelimit")
        with patch.dict("sys.modules", {"yfinance": fake_yf}):
            out = yh.fetch_yahoo_news("NVDA", use_cache=False)
        assert out["available"] is False
        assert out["error"] == "RuntimeError"


# ---------------------------------------------------------------------------
# Aggregator + token cap
# ---------------------------------------------------------------------------


def _ok_st() -> dict[str, Any]:
    return {
        "ticker": "NVDA", "available": True,
        "bullish_count": 18, "bearish_count": 4, "neutral_count": 6,
        "recent_post_count_24h": 28, "total_messages": 30,
        "top_posts": [
            {"body": "Q1 beat priors, IREN deal monster", "likes": 84,
             "user": "alice", "sentiment": "Bullish", "created_at": ""},
            {"body": "Nvidia ripping into earnings", "likes": 52,
             "user": "bob", "sentiment": "Bullish", "created_at": ""},
        ],
        "error": None,
    }


def _ok_rd() -> dict[str, Any]:
    return {
        "ticker": "NVDA", "available": True,
        "mention_count": 12,
        "subreddits_searched": ["wallstreetbets", "stocks", "investing"],
        "top_posts": [
            {"title": "NVDA earnings May 20 - DD inside", "score": 312,
             "comments": 88, "subreddit": "wallstreetbets"},
        ],
        "error": None,
    }


def _ok_yh() -> dict[str, Any]:
    return {
        "ticker": "NVDA", "available": True,
        "headlines": [
            {"title": "Nvidia announces Nemotron 3 Nano Omni",
             "publisher": "Reuters", "published_at": "2026-05-11T15:00:00Z",
             "link": ""},
        ],
        "error": None,
    }


def _down(source: str) -> dict[str, Any]:
    return {"ticker": "NVDA", "available": False, "error": "HTTPError",
            "bullish_count": 0, "bearish_count": 0, "neutral_count": 0,
            "recent_post_count_24h": 0, "total_messages": 0, "top_posts": [],
            "mention_count": 0, "subreddits_searched": [],
            "headlines": []}


class TestAggregator:
    def test_all_three_sources_ok(self) -> None:
        with patch.object(agg, "fetch_stocktwits", return_value=_ok_st()), \
             patch.object(agg, "fetch_reddit", return_value=_ok_rd()), \
             patch.object(agg, "fetch_yahoo_news", return_value=_ok_yh()):
            block = agg.fetch_grounded_sentiment("NVDA", "2026-05-11")
        assert "Retail sentiment for NVDA" in block
        assert "StockTwits" in block
        assert "Reddit" in block
        assert "Yahoo News" in block
        assert "unavailable" not in block

    def test_one_source_down(self) -> None:
        with patch.object(agg, "fetch_stocktwits", return_value=_down("st")), \
             patch.object(agg, "fetch_reddit", return_value=_ok_rd()), \
             patch.object(agg, "fetch_yahoo_news", return_value=_ok_yh()):
            block = agg.fetch_grounded_sentiment("NVDA", "2026-05-11")
        assert "StockTwits** (unavailable" in block
        assert "Reddit" in block and "unavailable" not in block.split("**Reddit**")[1].split("**Yahoo")[0]
        assert "Yahoo News" in block

    def test_two_sources_down(self) -> None:
        with patch.object(agg, "fetch_stocktwits", return_value=_down("st")), \
             patch.object(agg, "fetch_reddit", return_value=_down("rd")), \
             patch.object(agg, "fetch_yahoo_news", return_value=_ok_yh()):
            block = agg.fetch_grounded_sentiment("NVDA", "2026-05-11")
        assert block.count("unavailable") == 2
        assert "Yahoo News" in block

    def test_all_three_down(self) -> None:
        with patch.object(agg, "fetch_stocktwits", return_value=_down("st")), \
             patch.object(agg, "fetch_reddit", return_value=_down("rd")), \
             patch.object(agg, "fetch_yahoo_news", return_value=_down("yh")):
            block = agg.fetch_grounded_sentiment("NVDA", "2026-05-11")
        # Every source unavailable, but the block still renders
        assert block.count("unavailable") == 3
        assert "Retail sentiment for NVDA" in block

    def test_block_under_token_cap(self) -> None:
        # Pad the formatted block with very long top posts to verify truncation.
        big_st = _ok_st()
        big_st["top_posts"] = [
            {"body": "x" * 5000, "likes": i, "user": "u",
             "sentiment": "Bullish", "created_at": ""}
            for i in range(20)
        ]
        with patch.object(agg, "fetch_stocktwits", return_value=big_st), \
             patch.object(agg, "fetch_reddit", return_value=_ok_rd()), \
             patch.object(agg, "fetch_yahoo_news", return_value=_ok_yh()):
            block = agg.fetch_grounded_sentiment("NVDA", "2026-05-11")
        assert agg._count_tokens(block) <= agg._TOKEN_CAP

    def test_normal_block_well_under_cap(self) -> None:
        with patch.object(agg, "fetch_stocktwits", return_value=_ok_st()), \
             patch.object(agg, "fetch_reddit", return_value=_ok_rd()), \
             patch.object(agg, "fetch_yahoo_news", return_value=_ok_yh()):
            block = agg.fetch_grounded_sentiment("NVDA", "2026-05-11")
        assert agg._count_tokens(block) < 500  # nominal block is small


class TestRefreshTicker:
    def test_status_summary_shape(self) -> None:
        with patch.object(agg, "fetch_stocktwits", return_value=_ok_st()), \
             patch.object(agg, "fetch_reddit", return_value=_ok_rd()), \
             patch.object(agg, "fetch_yahoo_news", return_value=_ok_yh()):
            status = agg.refresh_ticker("NVDA", "2026-05-11")
        assert status["ticker"] == "NVDA"
        assert status["stocktwits_ok"] is True
        assert status["reddit_ok"] is True
        assert status["yahoo_ok"] is True
        assert status["stocktwits_total"] == 30
        assert status["reddit_mentions"] == 12
        assert status["yahoo_headlines"] == 1
