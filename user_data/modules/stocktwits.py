"""StockTwits public symbol-stream fetcher for the sentiment aggregator.

Free public endpoint, no API key required for read-only stream access.
Rate limit: ~200 req/hour. The aggregator iterates dashboard universe
stocks (15 symbols) and hits each once per refresh window — well under
the cap.

The aggregator at `user_data.modules.news_aggregator.NewsAggregator`
wraps this via `_fetch_stocktwits` and converts STItem → NewsItem with
source=f"stocktwits:{symbol}".

Endpoint:
    GET https://api.stocktwits.com/api/2/streams/symbol/{symbol}.json

Response shape (truncated):
    {
      "messages": [
        {
          "id": 1234,
          "body": "$NVDA breaking out",
          "created_at": "2026-05-13T11:00:00Z",
          "entities": {"sentiment": {"basic": "Bullish"}},  // or absent
          "likes": {"total": 12},
          "user": {"username": "trader1"}
        }, ...
      ]
    }
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime

import aiohttp

logger = logging.getLogger(__name__)

_ST_BASE = "https://api.stocktwits.com/api/2"
_MAX_BODY_CHARS = 400


@dataclass(frozen=True)
class STItem:
    """Normalized StockTwits message."""

    id: int
    symbol: str
    body: str
    sentiment: str | None  # "Bullish" | "Bearish" | None (most messages are untagged)
    likes: int
    user: str
    ts: datetime


async def _http_get_json(session: aiohttp.ClientSession, url: str) -> dict:
    """Tiny wrapper so tests can monkey-patch network at one seam."""
    async with session.get(
        url,
        timeout=aiohttp.ClientTimeout(total=10),
        headers={"User-Agent": "trading-bot/v4"},
    ) as r:
        r.raise_for_status()
        data = await r.json()
        if not isinstance(data, dict):
            raise ValueError(f"unexpected response shape: {type(data).__name__}")
        return data


async def fetch_stocktwits_symbol_stream(symbol: str, limit: int = 30) -> list[STItem]:
    """Fetch the public symbol stream for ``symbol`` (e.g., "NVDA", "BTC.X").

    StockTwits uses `.X` suffix for crypto symbols (BTC.X, ETH.X). The
    aggregator should translate accordingly when feeding crypto pairs.

    Returns at most ``limit`` items, newest-first per StockTwits' ordering.
    """
    out: list[STItem] = []
    async with aiohttp.ClientSession() as session:
        try:
            data = await _http_get_json(
                session, f"{_ST_BASE}/streams/symbol/{symbol}.json",
            )
        except Exception as exc:
            logger.warning("stocktwits %s fetch failed: %s", symbol, exc)
            return out

        for m in (data.get("messages") or [])[:limit]:
            try:
                sent_block = (m.get("entities") or {}).get("sentiment") or {}
                ts_raw = m["created_at"]
                # ST uses "Z" suffix; fromisoformat in 3.12 accepts it directly,
                # but we replace defensively to support older Python at-import.
                ts = datetime.fromisoformat(ts_raw.replace("Z", "+00:00"))
                out.append(
                    STItem(
                        id=int(m["id"]),
                        symbol=symbol,
                        body=str(m.get("body", ""))[:_MAX_BODY_CHARS],
                        sentiment=sent_block.get("basic"),
                        likes=int((m.get("likes") or {}).get("total", 0)),
                        user=str((m.get("user") or {}).get("username", "?")),
                        ts=ts,
                    )
                )
            except (KeyError, ValueError, TypeError) as exc:
                logger.debug("stocktwits %s skip malformed msg: %s", symbol, exc)
                continue
    return out
