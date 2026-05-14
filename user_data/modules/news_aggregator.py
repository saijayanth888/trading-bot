"""
NewsAggregator — multi-source crypto news + market-mood ingestion.

Six free sources fetched concurrently every poll cycle, deduplicated by
fuzzy title match, normalised into a common ``NewsItem`` shape, and stored
in TimescaleDB (``news_headlines``). Two side-channel signals (Fear & Greed
Index, CoinGecko trending) are also collected and surfaced as direct features
without LLM scoring.

Sources (zero external API costs):

    1. Perplexity Sonar          — paid ($1/M tokens), uses PERPLEXITY_API_KEY.
                                   Called separately by sentiment_engine.py
                                   so it can use the existing rate limiter.
    2. cryptocurrency.cv         — free, no key
    3. Reddit (json endpoints)   — free, no key, needs User-Agent.
                                   upvote_ratio + comment volume on
                                   pair-specific posts gives a free
                                   crowd-sentiment signal.
    4. CoinGecko trending        — free, no key, low rate limit
    5. Direct RSS feeds          — free, no key (CoinDesk / CoinTelegraph /
                                   The Block / Decrypt)

Plus:
    Fear & Greed Index           — free, no key (alternative.me)

Each fetcher is best-effort: a source that times out / rate-limits / returns
malformed data is skipped; the rest of the aggregation continues. The poll
result records which sources responded so the operator can spot-check.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from difflib import SequenceMatcher
from typing import Any

import aiohttp

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Knobs
# ---------------------------------------------------------------------------

USER_AGENT = "trading-bot/1.0 (research; +https://github.com/local)"
HTTP_TIMEOUT = aiohttp.ClientTimeout(total=20)
PER_SOURCE_TIMEOUT = 15.0   # individual fetcher hard ceiling


def _load_config() -> dict:
    """Read user_data/config.json once per call. Cheap; called per poll."""
    import json
    config_path = os.environ.get(
        "FREQTRADE_CONFIG_PATH", "/app/user_data/config.json",
    )
    try:
        with open(config_path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _config_or_fallback(cfg: dict, key: str, fallback: Any) -> Any:
    block = (cfg.get("news_sources_config") or {})
    return block.get(key, fallback)


# Hardcoded fallbacks — used only when config.json[news_sources_config] is
# missing or unreadable. Operators tune these via config.json, not here.
_FALLBACK_WATCHED_PAIRS: tuple[str, ...] = (
    "BTC", "ETH", "SOL", "ADA", "XRP", "DOGE", "AVAX", "LINK", "POL", "MATIC",
)
_FALLBACK_REDDIT_SUBS = (
    {"sub": "cryptocurrency", "limit": 25},
    {"sub": "bitcoin",        "limit": 25},
    {"sub": "ethtrader",      "limit": 15},
)
_FALLBACK_RSS_FEEDS = (
    {"name": "coindesk",      "url": "https://www.coindesk.com/arc/outboundfeeds/rss/"},
    {"name": "cointelegraph", "url": "https://cointelegraph.com/rss"},
    {"name": "theblock",      "url": "https://www.theblock.co/rss"},
    {"name": "decrypt",       "url": "https://decrypt.co/feed"},
)


# Backwards-compatible module-level constants (used by the regex builder).
# Refreshed at module import time; the live aggregator re-reads on each poll.
_cfg_at_import = _load_config()
WATCHED_PAIRS: tuple[str, ...] = tuple(
    _config_or_fallback(_cfg_at_import, "watched_pairs", list(_FALLBACK_WATCHED_PAIRS))
)
REDDIT_SUBS: tuple[tuple[str, int], ...] = tuple(
    (s["sub"], s["limit"])
    for s in _config_or_fallback(_cfg_at_import, "reddit_subs", list(_FALLBACK_REDDIT_SUBS))
)
RSS_FEEDS: tuple[tuple[str, str], ...] = tuple(
    (f["name"], f["url"])
    for f in _config_or_fallback(_cfg_at_import, "rss_feeds", list(_FALLBACK_RSS_FEEDS))
)

CRYPTOCURRENCY_CV_BASE = os.environ.get(
    "CRYPTOCURRENCY_CV_URL", "https://cryptocurrency.cv/api/news"
)

FNG_URL = "https://api.alternative.me/fng/?limit=10&format=json"
COINGECKO_TRENDING = "https://api.coingecko.com/api/v3/search/trending"

# Dedup tuning. SequenceMatcher.ratio() of two normalised titles ≥ this →
# treat as the same article. 0.80 is the spec; lower → more aggressive dedup.
DEDUP_RATIO_THRESHOLD = float(os.environ.get("NEWS_DEDUP_THRESHOLD", "0.80"))

# Reddit attention weighting: score + 2 × num_comments, normalised across the
# fetched window. Comments weighted 2× because they reflect deeper engagement.
REDDIT_COMMENT_WEIGHT = 2.0


# ---------------------------------------------------------------------------
# Normalised data classes
# ---------------------------------------------------------------------------


@dataclass
class NewsItem:
    title: str
    summary: str
    source: str
    url: str
    timestamp: datetime
    pair_mentions: list[str] = field(default_factory=list)
    community_sentiment: float | None = None   # -1..+1 (Reddit upvote_ratio mapped to [-1,1])
    attention_score: float | None = None       # 0..1 (Reddit normalised)


@dataclass
class FearGreedSnapshot:
    value: int                  # 0..100
    classification: str         # "Extreme Fear" | "Fear" | "Neutral" | "Greed" | "Extreme Greed"
    timestamp: datetime
    history_7d: list[int] = field(default_factory=list)


@dataclass
class TrendingSnapshot:
    coins: list[str]            # uppercase symbols, e.g. ["BTC", "SOL"]
    timestamp: datetime


@dataclass
class AggregatedNews:
    items: list[NewsItem]
    fear_greed: FearGreedSnapshot | None
    trending: TrendingSnapshot | None
    sources_ok: list[str] = field(default_factory=list)
    sources_failed: list[tuple[str, str]] = field(default_factory=list)
    sources_total: int = 0


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


_PAIR_RE = re.compile(
    r"\b(" + "|".join(WATCHED_PAIRS) + r")\b", re.IGNORECASE,
)


def _detect_pairs(*texts: str) -> list[str]:
    """Return uppercase pair symbols mentioned in any of the supplied strings."""
    out: set[str] = set()
    for t in texts:
        if not t:
            continue
        for m in _PAIR_RE.finditer(t):
            out.add(m.group(1).upper())
    return sorted(out)


def _norm_title(title: str) -> str:
    """Lowercase + strip non-alphanumerics so dedup is comparing meaning, not punctuation."""
    if not title:
        return ""
    return re.sub(r"[^a-z0-9 ]+", " ", title.lower()).strip()


def _trunc(text: str, n: int = 400) -> str:
    text = (text or "").strip()
    if len(text) <= n:
        return text
    return text[: n - 1].rstrip() + "…"


def _utc_from_epoch(seconds: float | int | None) -> datetime:
    if seconds is None:
        return datetime.now(timezone.utc)
    try:
        return datetime.fromtimestamp(float(seconds), tz=timezone.utc)
    except (TypeError, ValueError, OSError):
        return datetime.now(timezone.utc)


def _utc_from_iso(value: str | None) -> datetime:
    if not value:
        return datetime.now(timezone.utc)
    try:
        # Strict ISO 8601; trailing 'Z' is allowed.
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError:
        return datetime.now(timezone.utc)


def _dedup(items: list[NewsItem], threshold: float = DEDUP_RATIO_THRESHOLD) -> list[NewsItem]:
    """Drop near-duplicate titles. Newer (or higher-attention) item wins."""
    items = sorted(
        items,
        key=lambda i: (i.attention_score or 0.0, i.timestamp.timestamp()),
        reverse=True,
    )
    kept: list[NewsItem] = []
    kept_norm: list[str] = []
    for item in items:
        norm = _norm_title(item.title)
        if not norm:
            continue
        is_dup = False
        for k in kept_norm:
            if SequenceMatcher(None, norm, k).ratio() >= threshold:
                is_dup = True
                break
        if not is_dup:
            kept.append(item)
            kept_norm.append(norm)
    return kept


# ---------------------------------------------------------------------------
# Aggregator
# ---------------------------------------------------------------------------


class NewsAggregator:
    """Fan out to all sources concurrently, normalise, dedup."""

    async def poll_all_sources(self) -> AggregatedNews:
        async with aiohttp.ClientSession(
            timeout=HTTP_TIMEOUT,
            headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
        ) as session:
            all_fetchers = [
                ("cryptocurrency_cv", self._fetch_cryptocurrency_cv),
                ("reddit",            self._fetch_reddit),
                ("rss",               self._fetch_rss_feeds),
                ("fear_greed",        self._fetch_fear_greed),
                ("coingecko_trending", self._fetch_coingecko_trending),
                ("hackernews",        self._fetch_hackernews),
                ("stocktwits",        self._fetch_stocktwits),
            ]
            _src_cfg = (_load_config().get("sentiment_sources") or {})
            _alias = {"rss": "rss_feeds"}
            fetchers = [
                (name, fn) for (name, fn) in all_fetchers
                if _src_cfg.get(_alias.get(name, name), {}).get("enabled", True)
            ]
            tasks = [
                asyncio.wait_for(fn(session), timeout=PER_SOURCE_TIMEOUT)
                for _name, fn in fetchers
            ]
            results = await asyncio.gather(*tasks, return_exceptions=True)

        all_items: list[NewsItem] = []
        fear_greed: FearGreedSnapshot | None = None
        trending: TrendingSnapshot | None = None
        sources_ok: list[str] = []
        sources_failed: list[tuple[str, str]] = []

        for (name, _fn), result in zip(fetchers, results):
            if isinstance(result, Exception):
                sources_failed.append((name, str(result)[:200]))
                logger.warning("[news] %s failed: %s", name, result)
                continue
            sources_ok.append(name)
            if name == "fear_greed":
                fear_greed = result
            elif name == "coingecko_trending":
                trending = result
            elif isinstance(result, list):
                all_items.extend(result)

        deduped = _dedup(all_items)
        return AggregatedNews(
            items=deduped, fear_greed=fear_greed, trending=trending,
            sources_ok=sources_ok, sources_failed=sources_failed,
            sources_total=len(fetchers),
        )

    # ---- cryptocurrency.cv -------------------------------------------------

    async def _fetch_cryptocurrency_cv(self, session: aiohttp.ClientSession) -> list[NewsItem]:
        out: list[NewsItem] = []
        try:
            async with session.get(CRYPTOCURRENCY_CV_BASE) as r:
                if r.status != 200:
                    raise RuntimeError(f"HTTP {r.status}")
                data = await r.json(content_type=None)
        except Exception as exc:
            raise RuntimeError(f"cryptocurrency.cv: {exc}") from exc

        # Endpoint shape varies; defensive parsing covers the common forms.
        articles = data if isinstance(data, list) else (
            data.get("data") or data.get("articles") or data.get("news") or []
        )
        for a in articles[:50]:
            if not isinstance(a, dict):
                continue
            title = str(a.get("title") or a.get("headline") or "")
            if not title:
                continue
            url = str(a.get("url") or a.get("link") or "")
            summary = _trunc(a.get("summary") or a.get("description") or "")
            ts = a.get("published_at") or a.get("timestamp") or a.get("date")
            if isinstance(ts, (int, float)):
                ts_dt = _utc_from_epoch(ts)
            else:
                ts_dt = _utc_from_iso(ts)
            out.append(NewsItem(
                title=title, summary=summary, source="cryptocurrency_cv",
                url=url, timestamp=ts_dt,
                pair_mentions=_detect_pairs(title, summary),
            ))
        return out

    # ---- Fear & Greed ------------------------------------------------------

    async def _fetch_fear_greed(self, session: aiohttp.ClientSession) -> FearGreedSnapshot | None:
        try:
            async with session.get(FNG_URL) as r:
                if r.status != 200:
                    raise RuntimeError(f"HTTP {r.status}")
                data = await r.json(content_type=None)
        except Exception as exc:
            raise RuntimeError(f"fear_greed: {exc}") from exc
        records = data.get("data") or []
        if not records:
            return None
        latest = records[0]
        history = [int(r.get("value", 0)) for r in records[:7]]
        return FearGreedSnapshot(
            value=int(latest.get("value", 0)),
            classification=str(latest.get("value_classification", "Neutral")),
            timestamp=_utc_from_epoch(int(latest.get("timestamp", 0)) or None),
            history_7d=history,
        )

    # ---- Reddit ------------------------------------------------------------

    async def _fetch_reddit(self, session: aiohttp.ClientSession) -> list[NewsItem]:
        out: list[NewsItem] = []
        max_score = 1.0
        pre_normalise: list[tuple[NewsItem, float]] = []

        for sub, limit in REDDIT_SUBS:
            url = f"https://www.reddit.com/r/{sub}/hot.json?limit={limit}"
            try:
                async with session.get(url, headers={"User-Agent": USER_AGENT}) as r:
                    if r.status != 200:
                        continue
                    data = await r.json(content_type=None)
            except Exception:
                continue
            children = ((data.get("data") or {}).get("children")) or []
            for c in children[:limit]:
                p = (c or {}).get("data") or {}
                title = str(p.get("title") or "")
                if not title or p.get("stickied"):
                    continue
                score = int(p.get("score") or 0)
                comments = int(p.get("num_comments") or 0)
                attention_raw = score + REDDIT_COMMENT_WEIGHT * comments
                max_score = max(max_score, attention_raw)
                ts_dt = _utc_from_epoch(p.get("created_utc"))
                permalink = p.get("permalink") or ""

                # Reddit's `upvote_ratio` is the fraction in [0, 1] of users
                # who upvoted (vs downvoted) the post. Map to [-1, +1] so a
                # 1.0 means unanimous bullish, 0.5 mixed, 0.0 bearish
                # consensus. Only emit when the post has enough engagement
                # (≥ 5 score) to be statistically meaningful.
                upvote_ratio = p.get("upvote_ratio")
                community_sentiment: float | None = None
                if isinstance(upvote_ratio, (int, float)) and score >= 5:
                    community_sentiment = float(upvote_ratio) * 2.0 - 1.0

                item = NewsItem(
                    title=title,
                    summary=_trunc(p.get("selftext") or ""),
                    source=f"reddit:{sub}",
                    url=f"https://www.reddit.com{permalink}",
                    timestamp=ts_dt,
                    pair_mentions=_detect_pairs(title, p.get("selftext") or ""),
                    community_sentiment=community_sentiment,
                )
                pre_normalise.append((item, attention_raw))

        for item, raw in pre_normalise:
            item.attention_score = round(raw / max_score, 4) if max_score > 0 else 0.0
            out.append(item)
        return out

    # ---- CoinGecko trending ------------------------------------------------

    async def _fetch_coingecko_trending(
        self, session: aiohttp.ClientSession,
    ) -> TrendingSnapshot | None:
        try:
            async with session.get(COINGECKO_TRENDING) as r:
                if r.status != 200:
                    raise RuntimeError(f"HTTP {r.status}")
                data = await r.json(content_type=None)
        except Exception as exc:
            raise RuntimeError(f"coingecko: {exc}") from exc
        coins_raw = (data.get("coins") or [])[:7]
        coins: list[str] = []
        for c in coins_raw:
            item = c.get("item") or {}
            sym = item.get("symbol")
            if sym:
                coins.append(sym.upper())
        return TrendingSnapshot(coins=coins, timestamp=datetime.now(timezone.utc))

    # ---- RSS feeds ---------------------------------------------------------

    async def _fetch_rss_feeds(self, session: aiohttp.ClientSession) -> list[NewsItem]:
        try:
            import feedparser
        except ImportError:
            logger.debug("[news] feedparser not installed — skipping RSS sources")
            return []

        out: list[NewsItem] = []
        # feedparser does sync HTTP; do it in the thread pool so we don't block
        # the event loop. Fetch the feed text via aiohttp first to keep the
        # overall HTTP_TIMEOUT honoured, then hand the bytes to feedparser.
        for source_name, url in RSS_FEEDS:
            try:
                async with session.get(url, headers={"User-Agent": USER_AGENT}) as r:
                    if r.status != 200:
                        continue
                    body = await r.read()
            except Exception:
                continue

            loop = asyncio.get_running_loop()
            try:
                feed = await loop.run_in_executor(None, feedparser.parse, body)
            except Exception:
                continue
            for entry in (feed.entries or [])[:15]:
                title = str(getattr(entry, "title", "") or "")
                if not title:
                    continue
                summary = _trunc(str(getattr(entry, "summary", "") or ""))
                ts_struct = getattr(entry, "published_parsed", None) or getattr(
                    entry, "updated_parsed", None,
                )
                if ts_struct:
                    import calendar
                    ts_dt = _utc_from_epoch(calendar.timegm(ts_struct))
                else:
                    ts_dt = datetime.now(timezone.utc)
                out.append(NewsItem(
                    title=title, summary=summary,
                    source=source_name, url=str(getattr(entry, "link", "") or ""),
                    timestamp=ts_dt,
                    pair_mentions=_detect_pairs(title, summary),
                ))
        return out

    # ---- Hacker News -------------------------------------------------------

    async def _fetch_hackernews(self, session: aiohttp.ClientSession) -> list[NewsItem]:
        """HN top stories — title-only signal (no body in the firehose).

        Filters out items where pair_mentions is empty AND the title scores
        low on crypto/finance keywords; otherwise the sentiment scorer gets
        flooded with unrelated tech headlines. We keep stories that mention
        a watched pair OR have a strong finance keyword in the title.
        """
        # Sibling-module import — works regardless of how the parent module
        # is loaded (whether `user_data.modules.news_aggregator` from inside
        # freqtrade, or `modules.news_aggregator` from the cron wrapper which
        # only puts user_data/ on sys.path). Absolute `from user_data.modules.X`
        # would silently fail in the latter case and the source would emit []
        # for every poll — exactly the bug we hit pre-fix.
        from .hackernews import fetch_hn_top

        try:
            items = await fetch_hn_top(limit=40)
        except Exception as exc:
            raise RuntimeError(f"hackernews: {exc}") from exc

        # Crypto / finance keyword filter on the title. Note: HN's front
        # page is mostly software / startup news, so on a typical day this
        # filter rejects 80–100% of items. That's expected — HN is a
        # supplementary signal, not a primary one. We still emit a
        # per-poll log line so operators can see whether the filter is
        # too aggressive on a given day.
        finance_kw = re.compile(
            r"\b(bitcoin|btc|ethereum|eth|crypto|sec |fed |inflation|"
            r"stock|nasdaq|s&p|fomc|interest rate|treasury|earnings|"
            r"options|defi|rally|crash|bull|bear|liquidity|"
            r"market|trade|trading|ipo|stocks|equity|equities|"
            r"recession|tariff|gdp|cpi|jobs|unemployment|"
            r"yield|bond|bonds|tesla|nvidia|apple|google|microsoft|"
            r"openai|anthropic|meta|amazon|amd|intel|tsmc|broadcom|palantir|"
            r"ai|llm|chip|chips|semiconductor|gpu|data center|datacenter|"
            r"finance|financial|economy|economic)\b",
            re.IGNORECASE,
        )
        out: list[NewsItem] = []
        for it in items:
            pairs = _detect_pairs(it.title)
            if not pairs and not finance_kw.search(it.title):
                continue
            out.append(NewsItem(
                title=it.title,
                summary="",  # HN top-list doesn't include body
                source="hackernews",
                url=it.url or f"https://news.ycombinator.com/item?id={it.id}",
                timestamp=it.ts,
                pair_mentions=pairs,
            ))
        return out

    # ---- StockTwits --------------------------------------------------------

    async def _fetch_stocktwits(self, session: aiohttp.ClientSession) -> list[NewsItem]:
        """StockTwits per-symbol streams for the dashboard universe.

        Iterates a small set of high-volume tickers (capped to keep the
        per-refresh fan-out under the ~200 req/hr free-tier limit) and
        converts messages into NewsItem rows with source=f"stocktwits:{symbol}".
        Carries StockTwits' explicit Bull/Bear sentiment tag in
        community_sentiment when present.
        """
        # Sibling-module import — see comment in _fetch_hackernews above for
        # why we use relative form. Absolute `from user_data.modules.stocktwits`
        # silently fails when news_aggregator is loaded as `modules.news_aggregator`
        # (cron path), making the entire source emit []. Relative form is robust
        # regardless of the parent package's name.
        from .stocktwits import fetch_stocktwits_symbol_stream

        # Universe: high-attention names from the dashboard config.
        symbols = _config_or_fallback(
            _load_config(), "stocktwits_symbols",
            ["NVDA", "TSLA", "AAPL", "MSFT", "PLTR", "SOFI", "HOOD",
             "BTC.X", "ETH.X", "SOL.X"],
        )

        out: list[NewsItem] = []
        for sym in symbols[:10]:  # cap fan-out — 10 calls per refresh
            try:
                items = await fetch_stocktwits_symbol_stream(sym, limit=15)
            except Exception as exc:
                logger.debug("[news] stocktwits %s failed: %s", sym, exc)
                continue
            for it in items:
                # Map Bull/Bear tag → [-1, +1]
                community_sentiment: float | None = None
                if it.sentiment == "Bullish":
                    community_sentiment = 1.0
                elif it.sentiment == "Bearish":
                    community_sentiment = -1.0

                # Synthesize a pair_mentions list — StockTwits crypto symbols
                # use the BTC.X / ETH.X convention; map to our watched pairs.
                bare_sym = sym.replace(".X", "")
                pairs = _detect_pairs(it.body, f"${bare_sym}", bare_sym)

                out.append(NewsItem(
                    title=it.body[:160],
                    summary=it.body,
                    source=f"stocktwits:{sym}",
                    url=f"https://stocktwits.com/symbol/{sym}",
                    timestamp=it.ts,
                    pair_mentions=pairs,
                    community_sentiment=community_sentiment,
                ))
        return out


# ---------------------------------------------------------------------------
# DB persistence
# ---------------------------------------------------------------------------


def store_aggregated(result: AggregatedNews) -> None:
    """Append the poll result to TimescaleDB.

    Two tables:
      news_headlines  — one row per deduplicated NewsItem (hypertable on ts)
      fear_greed_log  — one row per Fear & Greed snapshot (hypertable on ts)

    Tables are created idempotently from user_data/data/schema.sql; this fn
    is a no-op if the tables don't exist yet (caller logs the error once).
    """
    from . import db
    import json
    from collections import Counter

    # Operator-visible per-poll summary: which sources contributed how many
    # items + which sources failed. Cheap to compute and invaluable for
    # spotting silent-empty bugs like the cron-path import failure that
    # gave HN/StockTwits 0 rows pre-fix (was: silent empty list, looked OK).
    _src_counts = Counter(i.source.split(":")[0] for i in result.items)
    logger.info(
        "[news] store_aggregated: items=%d per_source=%s failed=%s",
        len(result.items),
        dict(_src_counts),
        [s for s, _ in result.sources_failed],
    )

    try:
        with db.cursor() as cur:
            for item in result.items:
                cur.execute(
                    """
                    INSERT INTO news_headlines
                        (ts, source, title, summary, url, pair_mentions,
                         community_sentiment, attention_score)
                    VALUES (%s, %s, %s, %s, %s, %s::jsonb, %s, %s)
                    ON CONFLICT DO NOTHING
                    """,
                    (
                        item.timestamp, item.source, item.title[:500],
                        item.summary, item.url[:2000],
                        json.dumps(item.pair_mentions),
                        item.community_sentiment, item.attention_score,
                    ),
                )
            if result.fear_greed:
                fg = result.fear_greed
                cur.execute(
                    """
                    INSERT INTO fear_greed_log
                        (ts, value, classification, history_7d)
                    VALUES (%s, %s, %s, %s::jsonb)
                    ON CONFLICT (ts) DO UPDATE SET
                        value = EXCLUDED.value,
                        classification = EXCLUDED.classification,
                        history_7d = EXCLUDED.history_7d
                    """,
                    (
                        fg.timestamp, fg.value, fg.classification,
                        json.dumps(fg.history_7d),
                    ),
                )
    except Exception as exc:
        logger.warning("[news] DB write failed: %s", exc)


# ---------------------------------------------------------------------------
# Convenience for the sentiment engine
# ---------------------------------------------------------------------------


def aggregator() -> NewsAggregator:
    """Return a fresh NewsAggregator. Stateless — safe to instantiate per poll."""
    return NewsAggregator()


__all__ = (
    "NewsAggregator", "NewsItem", "FearGreedSnapshot", "TrendingSnapshot",
    "AggregatedNews", "store_aggregated", "aggregator", "WATCHED_PAIRS",
)
