"""
Sentiment engine — Claude + local Llama via Ollama, "Trust-The-Majority" gate.

Every 15 minutes:
  1. Pull crypto headlines from CoinDesk, CoinTelegraph and The Block (RSS).
  2. Pull the top 25 hot posts from /r/cryptocurrency and /r/bitcoin (public
     .json endpoint, no auth).
  3. Send the batch to Claude (`claude-sonnet-4-20250514` by default) using
     forced tool-use for structured JSON output. The system prompt is
     marked with cache_control so repeated calls hit the prompt cache.
  4. Send the same batch to a local Llama 3.1 8B served by Ollama
     (`format: json`).
  5. Trust-The-Majority: emit a directional signal **only** when both models
     agree on `market_impact`; otherwise emit neutral.
  6. Append the result to `sentiment_log` in the on-chain SQLite.

`get_sentiment_features(pair)` returns a DataFrame with FreqAI-prefixed
columns suitable for `pd.merge_asof` onto a candle dataframe.

Environment:
  ANTHROPIC_API_KEY   — required for the Claude call (graceful skip if unset).
  OLLAMA_HOST         — default "http://host.docker.internal:11434".
  OLLAMA_MODEL        — default "llama3.1:8b".
  CLAUDE_MODEL        — default "claude-sonnet-4-20250514".
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sqlite3
import threading
import time
import xml.etree.ElementTree as ET
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

import aiohttp
import pandas as pd

# ---------------------------------------------------------------------------
# Paths (share the on-chain DB)
# ---------------------------------------------------------------------------

_HERE = Path(__file__).resolve()
_USER_DATA = _HERE.parent.parent
DB_PATH = _USER_DATA / "data" / "onchain.db"
LOG_PATH = _USER_DATA / "logs" / "sentiment.log"
DB_PATH.parent.mkdir(parents=True, exist_ok=True)
LOG_PATH.parent.mkdir(parents=True, exist_ok=True)

# ---------------------------------------------------------------------------
# Tunables
# ---------------------------------------------------------------------------

POLL_INTERVAL_S = 15 * 60                                    # 15 minutes
HISTORY_DAYS = 7                                             # rows returned by accessor
HTTP_TIMEOUT = aiohttp.ClientTimeout(total=30)

RSS_FEEDS: list[tuple[str, str]] = [
    ("CoinDesk",      "https://www.coindesk.com/arc/outboundfeeds/rss/"),
    ("CoinTelegraph", "https://cointelegraph.com/rss"),
    ("TheBlock",      "https://www.theblock.co/rss.xml"),
]

REDDIT_SUBS = ["cryptocurrency", "bitcoin"]
REDDIT_POSTS_PER_SUB = 25
REDDIT_USER_AGENT = (
    "freqtrade-sentiment/0.1 (research; contact: ops@local)"
)

CLAUDE_MODEL = os.getenv("CLAUDE_MODEL", "claude-sonnet-4-20250514")
CLAUDE_MAX_TOKENS = 1024

OLLAMA_BASE = os.getenv("OLLAMA_HOST", "http://host.docker.internal:11434").rstrip("/")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "llama3.1:8b")

# Truncate the batch handed to LLMs so prompts stay bounded.
MAX_HEADLINES_TO_LLM = 60
MAX_REDDIT_TO_LLM = 50

# ---------------------------------------------------------------------------
# Logger (file only)
# ---------------------------------------------------------------------------

logger = logging.getLogger("sentiment")
if not logger.handlers:
    h = RotatingFileHandler(
        LOG_PATH, maxBytes=2 * 1024 * 1024, backupCount=3, encoding="utf-8",
    )
    h.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
    logger.addHandler(h)
    logger.setLevel(logging.INFO)
    logger.propagate = False

# ---------------------------------------------------------------------------
# SQLite — sentiment_log (in the same DB as the on-chain tables)
# ---------------------------------------------------------------------------

_SCHEMA = """
CREATE TABLE IF NOT EXISTS sentiment_log (
    ts              INTEGER PRIMARY KEY,
    sentiment_score REAL    NOT NULL,
    confidence      REAL    NOT NULL,
    market_impact   TEXT    NOT NULL,
    agreement       INTEGER NOT NULL,
    key_events      TEXT,
    claude_score    REAL,
    llama_score     REAL,
    claude_impact   TEXT,
    llama_impact    TEXT,
    n_headlines     INTEGER,
    n_reddit        INTEGER,
    raw_claude      TEXT,
    raw_llama       TEXT
);
CREATE INDEX IF NOT EXISTS ix_sentiment_ts ON sentiment_log(ts);
"""


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH), timeout=10, isolation_level=None)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def _init_db() -> None:
    with _connect() as conn:
        conn.executescript(_SCHEMA)


_init_db()

# ---------------------------------------------------------------------------
# Async rate limiter — evenly spaced calls
# ---------------------------------------------------------------------------


class AsyncRateLimiter:
    def __init__(self, calls_per_minute: float) -> None:
        self._interval = 60.0 / max(calls_per_minute, 0.1)
        self._lock = asyncio.Lock()
        self._last = 0.0

    @asynccontextmanager
    async def __call__(self):
        async with self._lock:
            wait = self._interval - (time.monotonic() - self._last)
            if wait > 0:
                await asyncio.sleep(wait)
            self._last = time.monotonic()
        yield


_RSS_RL = AsyncRateLimiter(calls_per_minute=30)
_REDDIT_RL = AsyncRateLimiter(calls_per_minute=20)
_CLAUDE_RL = AsyncRateLimiter(calls_per_minute=20)
_OLLAMA_RL = AsyncRateLimiter(calls_per_minute=20)


async def _request_with_backoff(
    session: aiohttp.ClientSession,
    method: str,
    url: str,
    *,
    headers: dict | None = None,
    params: dict | None = None,
    json_body: dict | None = None,
    timeout: aiohttp.ClientTimeout | None = None,
    max_retries: int = 4,
) -> aiohttp.ClientResponse | None:
    """Issue one request with exponential backoff on 429 / 5xx / network errors."""
    delay = 1.0
    for attempt in range(1, max_retries + 1):
        try:
            resp = await session.request(
                method, url,
                headers=headers, params=params, json=json_body,
                timeout=timeout or HTTP_TIMEOUT,
            )
        except (asyncio.TimeoutError, aiohttp.ClientError) as exc:
            logger.warning("[%s] network err (try %d/%d): %s",
                           url, attempt, max_retries, exc)
            await asyncio.sleep(delay)
            delay = min(delay * 2, 60.0)
            continue

        if resp.status == 429 or 500 <= resp.status < 600:
            retry_after = float(resp.headers.get("Retry-After", delay))
            logger.warning("[%s] HTTP %d (try %d/%d), backoff %.1fs",
                           url, resp.status, attempt, max_retries, retry_after)
            resp.release()
            await asyncio.sleep(retry_after)
            delay = min(delay * 2, 60.0)
            continue

        return resp

    logger.error("[%s] gave up after %d attempts", url, max_retries)
    return None


# ---------------------------------------------------------------------------
# RSS scraping (xml.etree, no extra dependency)
# ---------------------------------------------------------------------------


async def _fetch_rss_feed(
    session: aiohttp.ClientSession, source: str, url: str,
) -> list[dict[str, Any]]:
    async with _RSS_RL():
        resp = await _request_with_backoff(session, "GET", url)
    if resp is None:
        return []
    try:
        body = await resp.text()
    finally:
        resp.release()

    items: list[dict[str, Any]] = []
    try:
        root = ET.fromstring(body)
    except ET.ParseError as exc:
        logger.warning("rss %s: XML parse error: %s", source, exc)
        return items

    # Strip XML namespaces so .iter("item") matches Atom + RSS uniformly.
    for elem in root.iter():
        if "}" in elem.tag:
            elem.tag = elem.tag.split("}", 1)[1]

    for tag in ("item", "entry"):
        for entry in root.iter(tag):
            title = (entry.findtext("title") or "").strip()
            if title:
                items.append({"source": source, "title": title})
        if items:
            break

    logger.info("rss %s: %d items", source, len(items))
    return items[:30]


async def _fetch_all_rss(session: aiohttp.ClientSession) -> list[dict[str, Any]]:
    tasks = [_fetch_rss_feed(session, src, url) for src, url in RSS_FEEDS]
    out: list[dict[str, Any]] = []
    for r in await asyncio.gather(*tasks, return_exceptions=True):
        if isinstance(r, list):
            out.extend(r)
        else:
            logger.warning("rss task error: %s", r)
    return out


# ---------------------------------------------------------------------------
# Reddit scraping (public .json — no OAuth required)
# ---------------------------------------------------------------------------


async def _fetch_reddit_sub(
    session: aiohttp.ClientSession, sub: str,
) -> list[dict[str, Any]]:
    url = f"https://www.reddit.com/r/{sub}/hot.json"
    headers = {"User-Agent": REDDIT_USER_AGENT}
    params = {"limit": REDDIT_POSTS_PER_SUB}
    async with _REDDIT_RL():
        resp = await _request_with_backoff(
            session, "GET", url, headers=headers, params=params,
        )
    if resp is None:
        return []
    try:
        payload = await resp.json(content_type=None)
    except (json.JSONDecodeError, aiohttp.ContentTypeError) as exc:
        logger.warning("reddit %s: bad JSON: %s", sub, exc)
        return []
    finally:
        resp.release()

    posts: list[dict[str, Any]] = []
    for child in (payload.get("data") or {}).get("children", []) or []:
        d = child.get("data") or {}
        if d.get("stickied") or d.get("pinned"):
            continue
        title = (d.get("title") or "").strip()
        if not title:
            continue
        posts.append({
            "subreddit":    sub,
            "title":        title,
            "score":        int(d.get("score") or 0),
            "num_comments": int(d.get("num_comments") or 0),
        })
        if len(posts) >= REDDIT_POSTS_PER_SUB:
            break
    logger.info("reddit %s: %d posts", sub, len(posts))
    return posts


async def _fetch_all_reddit(
    session: aiohttp.ClientSession,
) -> list[dict[str, Any]]:
    tasks = [_fetch_reddit_sub(session, s) for s in REDDIT_SUBS]
    out: list[dict[str, Any]] = []
    for r in await asyncio.gather(*tasks, return_exceptions=True):
        if isinstance(r, list):
            out.extend(r)
        else:
            logger.warning("reddit task error: %s", r)
    return out


# ---------------------------------------------------------------------------
# Claude analysis (Anthropic SDK, forced tool-use, prompt cache enabled)
# ---------------------------------------------------------------------------

_anthropic_client = None
_anthropic_lock = threading.Lock()


def _get_anthropic_client():
    global _anthropic_client
    if _anthropic_client is not None:
        return _anthropic_client
    if not os.getenv("ANTHROPIC_API_KEY", "").strip():
        return None
    try:
        from anthropic import AsyncAnthropic
    except ImportError:
        logger.error("anthropic SDK missing — `pip install anthropic` in the container")
        return None
    with _anthropic_lock:
        if _anthropic_client is None:
            _anthropic_client = AsyncAnthropic()
    return _anthropic_client


async def _analyze_claude(
    headlines: list[dict[str, Any]],
    reddit_posts: list[dict[str, Any]],
) -> dict | None:
    client = _get_anthropic_client()
    if client is None:
        logger.info("claude skipped — no client")
        return None

    from .sentiment_prompts import (
        SYSTEM_PROMPT, SENTIMENT_TOOL, build_user_prompt,
    )

    user_prompt = build_user_prompt(
        headlines, reddit_posts,
        window_minutes=POLL_INTERVAL_S // 60,
        now_iso=datetime.now(timezone.utc).isoformat(timespec="seconds"),
    )

    delay = 1.0
    last_exc: Exception | None = None
    for attempt in range(1, 5):
        async with _CLAUDE_RL():
            try:
                resp = await client.messages.create(
                    model=CLAUDE_MODEL,
                    max_tokens=CLAUDE_MAX_TOKENS,
                    system=[{
                        "type": "text",
                        "text": SYSTEM_PROMPT,
                        "cache_control": {"type": "ephemeral"},
                    }],
                    tools=[SENTIMENT_TOOL],
                    tool_choice={"type": "tool", "name": "report_sentiment"},
                    messages=[{"role": "user", "content": user_prompt}],
                )
                break
            except Exception as exc:
                last_exc = exc
                logger.warning("claude error (try %d): %s", attempt, exc)
                await asyncio.sleep(delay)
                delay = min(delay * 2, 30.0)
    else:
        logger.exception("claude permanently failed: %s", last_exc)
        return None

    usage = getattr(resp, "usage", None)
    if usage is not None:
        logger.info(
            "claude usage: in=%s out=%s cache_read=%s cache_create=%s",
            getattr(usage, "input_tokens", "?"),
            getattr(usage, "output_tokens", "?"),
            getattr(usage, "cache_read_input_tokens", 0),
            getattr(usage, "cache_creation_input_tokens", 0),
        )

    for block in resp.content:
        if getattr(block, "type", None) == "tool_use" \
           and getattr(block, "name", "") == "report_sentiment":
            data = dict(block.input)
            return _coerce_result(data)
    logger.warning("claude returned no report_sentiment tool_use")
    return None


# ---------------------------------------------------------------------------
# Ollama analysis (Llama 3.1 8B, format=json)
# ---------------------------------------------------------------------------


async def _analyze_ollama(
    session: aiohttp.ClientSession,
    headlines: list[dict[str, Any]],
    reddit_posts: list[dict[str, Any]],
) -> dict | None:
    from .sentiment_prompts import OLLAMA_SYSTEM_PROMPT, build_user_prompt

    user_prompt = build_user_prompt(
        headlines, reddit_posts,
        window_minutes=POLL_INTERVAL_S // 60,
        now_iso=datetime.now(timezone.utc).isoformat(timespec="seconds"),
    )

    payload = {
        "model": OLLAMA_MODEL,
        "messages": [
            {"role": "system", "content": OLLAMA_SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ],
        "format": "json",
        "stream": False,
        "options": {"temperature": 0.2, "num_ctx": 4096},
    }

    async with _OLLAMA_RL():
        resp = await _request_with_backoff(
            session, "POST", f"{OLLAMA_BASE}/api/chat",
            json_body=payload,
            timeout=aiohttp.ClientTimeout(total=180),
        )
    if resp is None:
        return None
    try:
        body = await resp.json(content_type=None)
    except (json.JSONDecodeError, aiohttp.ContentTypeError) as exc:
        logger.warning("ollama bad JSON envelope: %s", exc)
        return None
    finally:
        resp.release()

    content = (body.get("message") or {}).get("content", "")
    if not content:
        logger.warning("ollama empty content; full response: %s", body)
        return None
    try:
        data = json.loads(content)
    except json.JSONDecodeError as exc:
        logger.warning("ollama non-JSON content: %s | snippet=%s",
                       exc, content[:200])
        return None
    return _coerce_result(data)


# ---------------------------------------------------------------------------
# Result coercion + Trust-The-Majority
# ---------------------------------------------------------------------------


def _coerce_result(data: dict) -> dict:
    """Clamp ranges and normalise market_impact spelling."""
    impact = str(data.get("market_impact", "neutral")).lower().strip()
    if impact not in ("bullish", "bearish", "neutral"):
        impact = "neutral"
    try:
        score = float(data.get("sentiment_score") or 0.0)
    except (TypeError, ValueError):
        score = 0.0
    try:
        conf = float(data.get("confidence") or 0.0)
    except (TypeError, ValueError):
        conf = 0.0
    score = max(-1.0, min(1.0, score))
    conf = max(0.0, min(1.0, conf))
    events = data.get("key_events") or []
    if not isinstance(events, list):
        events = []
    return {
        "sentiment_score": score,
        "confidence": conf,
        "market_impact": impact,
        "key_events": [str(e)[:200] for e in events][:5],
    }


def _trust_the_majority(claude: dict | None, llama: dict | None) -> dict:
    """Emit a directional signal only if both agree on direction."""
    have_both = bool(claude) and bool(llama)
    same_dir = (
        have_both
        and claude["market_impact"] == llama["market_impact"]
        and claude["market_impact"] in ("bullish", "bearish")
    )

    if same_dir:
        return {
            "sentiment_score": (
                claude["sentiment_score"] + llama["sentiment_score"]
            ) / 2,
            "confidence": min(claude["confidence"], llama["confidence"]),
            "market_impact": claude["market_impact"],
            "key_events": list(claude["key_events"])[:5],
            "agreement": True,
        }

    fallback_events: list[str] = []
    for src in (claude, llama):
        if src and src.get("key_events"):
            fallback_events = list(src["key_events"])[:5]
            break
    return {
        "sentiment_score": 0.0,
        "confidence": 0.0,
        "market_impact": "neutral",
        "key_events": fallback_events,
        "agreement": False,
    }


# ---------------------------------------------------------------------------
# One poll cycle
# ---------------------------------------------------------------------------


async def _poll_once() -> dict | None:
    async with aiohttp.ClientSession(timeout=HTTP_TIMEOUT) as session:
        headlines, reddit_posts = await asyncio.gather(
            _fetch_all_rss(session),
            _fetch_all_reddit(session),
        )

        if not headlines and not reddit_posts:
            logger.warning("no content fetched — skipping LLM analysis")
            return None

        headlines = headlines[:MAX_HEADLINES_TO_LLM]
        reddit_posts = reddit_posts[:MAX_REDDIT_TO_LLM]

        claude, llama = await asyncio.gather(
            _analyze_claude(headlines, reddit_posts),
            _analyze_ollama(session, headlines, reddit_posts),
            return_exceptions=False,
        )

    final = _trust_the_majority(claude, llama)
    ts = int(time.time())
    final["ts"] = ts

    with _connect() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO sentiment_log "
            "(ts, sentiment_score, confidence, market_impact, agreement, key_events, "
            " claude_score, llama_score, claude_impact, llama_impact, "
            " n_headlines, n_reddit, raw_claude, raw_llama) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                ts,
                float(final["sentiment_score"]),
                float(final["confidence"]),
                final["market_impact"],
                int(final["agreement"]),
                json.dumps(final["key_events"]),
                claude["sentiment_score"] if claude else None,
                llama["sentiment_score"] if llama else None,
                claude["market_impact"] if claude else None,
                llama["market_impact"] if llama else None,
                len(headlines),
                len(reddit_posts),
                json.dumps(claude) if claude else None,
                json.dumps(llama) if llama else None,
            ),
        )

    logger.info(
        "poll done: agreement=%s impact=%s score=%+.2f conf=%.2f "
        "(claude=%s llama=%s headlines=%d reddit=%d)",
        final["agreement"], final["market_impact"],
        final["sentiment_score"], final["confidence"],
        claude and claude["market_impact"],
        llama and llama["market_impact"],
        len(headlines), len(reddit_posts),
    )
    return final


# ---------------------------------------------------------------------------
# Background loop (asyncio in a daemon thread)
# ---------------------------------------------------------------------------


class SentimentEngine:
    _instance: "SentimentEngine | None" = None
    _instance_lock = threading.Lock()

    def __init__(self) -> None:
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.last_poll_ts: float = 0.0

    @classmethod
    def instance(cls) -> "SentimentEngine":
        with cls._instance_lock:
            if cls._instance is None:
                cls._instance = cls()
            return cls._instance

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._thread_target,
            name="sentiment-engine",
            daemon=True,
        )
        self._thread.start()
        logger.info(
            "sentiment engine started (interval=%ds, claude=%s, ollama=%s)",
            POLL_INTERVAL_S, CLAUDE_MODEL, OLLAMA_MODEL,
        )

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=10)

    def _thread_target(self) -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(self._async_main())
        finally:
            loop.close()

    async def _async_main(self) -> None:
        while not self._stop.is_set():
            try:
                await _poll_once()
                self.last_poll_ts = time.time()
            except Exception:
                logger.exception("poll cycle crashed")
            for _ in range(POLL_INTERVAL_S):
                if self._stop.is_set():
                    return
                await asyncio.sleep(1)


# ---------------------------------------------------------------------------
# Public sync accessor for FreqAI
# ---------------------------------------------------------------------------

FEATURE_COLUMNS: tuple[str, ...] = (
    "%-sentiment_score",
    "%-sentiment_confidence",
    "%-sentiment_bullish",
    "%-sentiment_bearish",
    "%-sentiment_agreement",
)

_NEUTRAL_FEATURE_VALUES: dict[str, float] = {
    "%-sentiment_score": 0.0,
    "%-sentiment_confidence": 0.0,
    "%-sentiment_bullish": 0.0,
    "%-sentiment_bearish": 0.0,
    "%-sentiment_agreement": 0.0,
}


def _empty_features() -> pd.DataFrame:
    return pd.DataFrame(columns=list(FEATURE_COLUMNS))


def get_sentiment_features(pair: str) -> pd.DataFrame:
    """
    Return a DataFrame indexed by UTC datetime with sentiment features.

    `pair` is accepted for symmetry with `onchain_signals.get_features` —
    the current implementation returns broad-market sentiment that is the
    same for all pairs.

    Caller should `pd.merge_asof` the result onto its candle dataframe with
    `direction='backward'` and ffill missing values.
    """
    SentimentEngine.instance().start()                   # lazy start

    cutoff = int(time.time()) - HISTORY_DAYS * 86_400
    with _connect() as conn:
        rows = pd.read_sql_query(
            "SELECT ts, sentiment_score, confidence, market_impact, agreement "
            "FROM sentiment_log WHERE ts>=? ORDER BY ts",
            conn, params=(cutoff,),
        )
    if rows.empty:
        return _empty_features()

    idx = pd.DatetimeIndex(
        pd.to_datetime(rows["ts"], unit="s", utc=True), name="date",
    )
    impact = rows["market_impact"].astype(str).str.lower()
    out = pd.DataFrame(index=idx)
    out["%-sentiment_score"] = rows["sentiment_score"].astype(float).values
    out["%-sentiment_confidence"] = rows["confidence"].astype(float).values
    out["%-sentiment_bullish"] = (impact == "bullish").astype(float).values
    out["%-sentiment_bearish"] = (impact == "bearish").astype(float).values
    out["%-sentiment_agreement"] = rows["agreement"].astype(float).values
    return out[list(FEATURE_COLUMNS)]
