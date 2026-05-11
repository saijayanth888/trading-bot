"""
Per-symbol stock news sentiment via Perplexity.

Scaffold only — the real Perplexity wiring is gated on the operator
provisioning the API key. Until then, :class:`StocksSentimentFetcher`
returns deterministic placeholder data so the dashboard card can render
its final layout against a realistic payload. The placeholder is shaped
exactly like the production response, so swapping the implementation of
:meth:`_fetch_perplexity` is the only change required to go live.

Pattern mirrors the crypto sentiment pipeline (Ollama Llama fast + Claude
deep + aggregate) but at a per-symbol granularity — operator wants to see
NVDA-specific headlines distinct from PLTR's, since the wheel + Shark TFT
allocators trade them independently.

Envelope returned by :meth:`snapshot`::

    {
      "symbols": [
        {
          "symbol": "SOFI",
          "score":        0.42,   # net sentiment, -1..+1
          "confidence":   0.65,   # 0..1
          "n_headlines":  8,
          "key_events":   [{"ts": "...", "title": "...", "url": "..."}],
          "last_fetched_ts": "2026-05-11T13:30:00Z"
        },
        ...
      ],
      "aggregate_score":      0.32,   # mean weighted by confidence
      "aggregate_confidence": 0.55,
    }
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

logger = logging.getLogger(__name__)

# TODO(perplexity): wire PERPLEXITY_API_KEY from env once provisioned. Suggested
# endpoint: https://api.perplexity.ai/chat/completions  (Sonar Online model for
# news with citations). Output schema for the prompt should request:
#   {symbol, score (-1..1), confidence (0..1), n_headlines, key_events:[{ts,title,url}]}
PERPLEXITY_API_KEY_ENV = "PERPLEXITY_API_KEY"
PERPLEXITY_MODEL = os.environ.get("PERPLEXITY_MODEL", "sonar")
PERPLEXITY_ENDPOINT = "https://api.perplexity.ai/chat/completions"


@dataclass
class SymbolSentiment:
    symbol: str
    score: float
    confidence: float
    n_headlines: int
    key_events: list[dict[str, Any]] = field(default_factory=list)
    last_fetched_ts: datetime | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol":         self.symbol,
            "score":          round(self.score, 3),
            "confidence":     round(self.confidence, 3),
            "n_headlines":    self.n_headlines,
            "key_events":     self.key_events,
            "last_fetched_ts": (
                self.last_fetched_ts.isoformat() if self.last_fetched_ts else None
            ),
        }


class StocksSentimentFetcher:
    """Per-symbol news sentiment, Perplexity-backed (placeholder for now).

    Cache is per-symbol with a TTL (default 30 min) so the dashboard's 10s
    fast-poll doesn't hammer the Perplexity quota. Real implementation should
    rate-limit further (one request per symbol per cache TTL).
    """

    def __init__(self, symbols: Iterable[str], ttl_seconds: int = 1800):
        self.symbols = [s.strip().upper() for s in symbols if s and s.strip()]
        self.ttl = timedelta(seconds=ttl_seconds)
        self._cache: dict[str, tuple[datetime, SymbolSentiment]] = {}

    @property
    def is_live(self) -> bool:
        """True once the operator wires PERPLEXITY_API_KEY into env."""
        return bool(os.environ.get(PERPLEXITY_API_KEY_ENV, "").strip())

    async def snapshot(self) -> dict[str, Any]:
        """Returns the full envelope: per-symbol rows + aggregates."""
        results = await asyncio.gather(
            *(self._fetch_or_cache(s) for s in self.symbols),
            return_exceptions=True,
        )
        rows: list[SymbolSentiment] = []
        for r in results:
            if isinstance(r, Exception):
                logger.warning("stocks_sentiment: symbol fetch failed: %s", r)
                continue
            rows.append(r)

        if not rows:
            return {"symbols": [], "aggregate_score": 0.0, "aggregate_confidence": 0.0}

        # Confidence-weighted aggregate so a low-confidence outlier doesn't
        # drag the headline number.
        total_w = sum(r.confidence for r in rows) or 1.0
        agg_score = sum(r.score * r.confidence for r in rows) / total_w
        agg_conf  = sum(r.confidence for r in rows) / len(rows)

        return {
            "symbols":              [r.to_dict() for r in rows],
            "aggregate_score":      round(agg_score, 3),
            "aggregate_confidence": round(agg_conf, 3),
        }

    async def _fetch_or_cache(self, symbol: str) -> SymbolSentiment:
        now = datetime.now(timezone.utc)
        cached = self._cache.get(symbol)
        if cached and (now - cached[0]) < self.ttl:
            return cached[1]
        row = await self._fetch_perplexity(symbol)
        self._cache[symbol] = (now, row)
        return row

    async def _fetch_perplexity(self, symbol: str) -> SymbolSentiment:
        """Fetch sentiment for one symbol from Perplexity.

        TODO(perplexity-live):
            1. Read API key from env (PERPLEXITY_API_KEY).
            2. POST to PERPLEXITY_ENDPOINT with model=PERPLEXITY_MODEL and a
               prompt asking for the last 24h of headlines for `symbol`, with
               a strict JSON schema {symbol, score:-1..1, confidence:0..1,
               n_headlines, key_events:[{ts, title, url}]}.
            3. Validate + clip score/confidence into ranges.
            4. Persist a row into a stocks_sentiment_log table (mirror of
               sentiment_log) so the timeline endpoint can plot it.
            5. Wire httpx.AsyncClient with the same 3.5s timeout the ops
               endpoints use; degrade gracefully on quota / rate-limit.

        Until then, return a deterministic placeholder so the operator can
        see the dashboard card layout against realistic-looking data.
        """
        # Deterministic placeholder — hash the symbol so the same symbol gets
        # the same mock score (otherwise the card flickers on every refetch).
        digest = hashlib.sha1(symbol.encode("utf-8")).digest()
        # Map first byte → score in [-0.6, +0.6]; second byte → confidence in [0.4, 0.85]
        score      = ((digest[0] / 255.0) - 0.5) * 1.2
        confidence = 0.4 + (digest[1] / 255.0) * 0.45
        n_headlines = 3 + (digest[2] % 9)  # 3..11 headlines
        now = datetime.now(timezone.utc)
        return SymbolSentiment(
            symbol=symbol,
            score=score,
            confidence=confidence,
            n_headlines=n_headlines,
            key_events=[
                {
                    "ts":    (now - timedelta(hours=2)).isoformat(),
                    "title": f"[placeholder] {symbol} headline (Perplexity not wired)",
                    "url":   "https://example.com/placeholder",
                }
            ],
            last_fetched_ts=now,
        )
