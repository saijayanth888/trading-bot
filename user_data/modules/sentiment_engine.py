"""
Sentiment engine — Perplexity (news fetcher) + local Ollama (scorer).

Every 15 minutes:
  1. Ask Perplexity Sonar for crypto market headlines + summaries from the
     last hour. Perplexity does the web crawling and de-duplication for us.
  2. Pass the structured headline list to a local Llama (or any Ollama
     model) and ask it to emit a single JSON sentiment verdict.
  3. Store {score, confidence, market_impact, key_events} in the
     `sentiment_log` table inside the on-chain SQLite.

Why this shape:
  - Perplexity replaces the brittle RSS/Reddit scrapers (one API to
    maintain, no parser rot, mainstream + niche coverage in one call).
  - Ollama is the *judgment* layer — runs on the Spark, no third party
    sees how we score. Privacy-friendly and free.

`get_sentiment_features(pair)` returns a DataFrame with FreqAI-prefixed
columns suitable for `pd.merge_asof` onto a candle dataframe.

Environment:
  PERPLEXITY_API_KEY  — required to fetch news (graceful skip → neutral signal).
  PERPLEXITY_MODEL    — default "sonar" (Online). "sonar-pro" for better
                        synthesis at higher cost.
  PERPLEXITY_RECENCY  — search recency filter, default "hour".
                        ("hour" | "day" | "week" | "month")
  OLLAMA_HOST         — default "http://host.docker.internal:11434".
  OLLAMA_MODEL        — default "llama3.1:8b".
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import threading
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

import aiohttp
import pandas as pd

from . import db

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

_HERE = Path(__file__).resolve()
_USER_DATA = _HERE.parent.parent
LOG_PATH = _USER_DATA / "logs" / "sentiment.log"
LOG_PATH.parent.mkdir(parents=True, exist_ok=True)

# ---------------------------------------------------------------------------
# Tunables
# ---------------------------------------------------------------------------

POLL_INTERVAL_S = 15 * 60                                    # 15 minutes
HISTORY_DAYS = 7                                             # rows returned by accessor
HTTP_TIMEOUT = aiohttp.ClientTimeout(total=60)

PERPLEXITY_BASE = os.getenv("PERPLEXITY_BASE", "https://api.perplexity.ai").rstrip("/")
PERPLEXITY_MODEL = os.getenv("PERPLEXITY_MODEL", "sonar")
PERPLEXITY_RECENCY = os.getenv("PERPLEXITY_RECENCY", "hour")  # hour|day|week|month
PERPLEXITY_MAX_TOKENS = int(os.getenv("PERPLEXITY_MAX_TOKENS", "1500"))

OLLAMA_BASE = os.getenv("OLLAMA_HOST", "http://host.docker.internal:11434").rstrip("/")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "llama3.1:8b")

# Truncate the headline list passed to Ollama so prompts stay bounded.
MAX_HEADLINES_TO_LLM = 60

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

# Schema lives in user_data/data/schema.sql — db.ensure_schema() runs it
# on first cursor().

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


# Perplexity Sonar is generous (60 req/min on paid plans). 12/min is plenty
# for our 15-minute polling cadence.
_PERPLEXITY_RL = AsyncRateLimiter(calls_per_minute=12)
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
# Perplexity — news fetcher
# ---------------------------------------------------------------------------


_PERPLEXITY_SYSTEM = (
    "You are a crypto news desk assistant. Reply with ONLY a compact JSON "
    "array of recent headlines from reputable sources. Each entry must have "
    "exactly these keys: title (string, <=160 chars), summary (string, "
    "<=400 chars), source (string, the publisher name).\n"
    "Constraints:\n"
    "- Up to 30 entries, sorted by recency.\n"
    "- Skip pure price-action recaps; prefer regulation, macro, exchange / "
    "protocol incidents, ETF flows, on-chain milestones, hacks/exploits.\n"
    "- No markdown, no commentary, no surrounding object — just the JSON array."
)

_PERPLEXITY_USER_TEMPLATE = (
    "List recent crypto market news from the last {window} that could move "
    "Bitcoin, Ethereum, or major altcoins. UTC now = {now}. Output the JSON "
    "array as instructed."
)


def _ts_to_iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat(timespec="seconds")


def _extract_json_array(text: str) -> list[dict[str, Any]]:
    """Pull the first JSON array out of a string that *should* be an array."""
    text = text.strip()
    # Strip common code fences if present
    fenced = re.match(r"^```(?:json)?\s*(.*?)```\s*$", text, re.DOTALL)
    if fenced:
        text = fenced.group(1).strip()
    # If the whole thing parses, great
    try:
        v = json.loads(text)
        if isinstance(v, list):
            return v
    except json.JSONDecodeError:
        pass
    # Otherwise grab the first top-level [ ... ]
    start = text.find("[")
    end = text.rfind("]")
    if start == -1 or end == -1 or end <= start:
        return []
    try:
        v = json.loads(text[start : end + 1])
        return v if isinstance(v, list) else []
    except json.JSONDecodeError:
        return []


async def _fetch_perplexity_news(
    session: aiohttp.ClientSession,
) -> list[dict[str, Any]]:
    """
    Ask Perplexity for the latest crypto market news. Returns a list of
    dicts with title / summary / source. Empty list on any failure (the
    pipeline degrades gracefully — Ollama gets called with no items and
    returns a low-confidence neutral verdict).
    """
    api_key = os.getenv("PERPLEXITY_API_KEY", "").strip()
    if not api_key:
        logger.info("perplexity skipped — PERPLEXITY_API_KEY unset")
        return []

    user = _PERPLEXITY_USER_TEMPLATE.format(
        window=PERPLEXITY_RECENCY,
        now=datetime.now(timezone.utc).isoformat(timespec="seconds"),
    )
    payload = {
        "model": PERPLEXITY_MODEL,
        "messages": [
            {"role": "system", "content": _PERPLEXITY_SYSTEM},
            {"role": "user", "content": user},
        ],
        "max_tokens": PERPLEXITY_MAX_TOKENS,
        "temperature": 0.2,
        "search_recency_filter": PERPLEXITY_RECENCY,
    }
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    async with _PERPLEXITY_RL():
        resp = await _request_with_backoff(
            session, "POST", f"{PERPLEXITY_BASE}/chat/completions",
            headers=headers, json_body=payload,
            timeout=aiohttp.ClientTimeout(total=60),
        )
    if resp is None:
        return []
    try:
        body = await resp.json(content_type=None)
    except (json.JSONDecodeError, aiohttp.ContentTypeError) as exc:
        logger.warning("perplexity bad envelope: %s", exc)
        return []
    finally:
        resp.release()

    try:
        content = body["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        logger.warning("perplexity unexpected response shape: %s | body=%s",
                       exc, json.dumps(body)[:300])
        return []

    items_raw = _extract_json_array(content)
    citations = body.get("citations") or []
    out: list[dict[str, Any]] = []
    for i, item in enumerate(items_raw):
        if not isinstance(item, dict):
            continue
        title = str(item.get("title") or "").strip()
        if not title:
            continue
        out.append({
            "title": title[:240],
            "summary": str(item.get("summary") or "").strip()[:600],
            "source": str(item.get("source") or "").strip()[:80],
            "citation": (
                str(citations[i]) if i < len(citations) else ""
            ),
        })
    logger.info(
        "perplexity: model=%s recency=%s items=%d (citations=%d)",
        PERPLEXITY_MODEL, PERPLEXITY_RECENCY, len(out), len(citations),
    )
    return out[:MAX_HEADLINES_TO_LLM]


# ---------------------------------------------------------------------------
# Ollama — local scorer
# ---------------------------------------------------------------------------


async def _analyze_ollama(
    session: aiohttp.ClientSession,
    items: list[dict[str, Any]],
) -> dict | None:
    from .sentiment_prompts import OLLAMA_SYSTEM_PROMPT, build_user_prompt

    user_prompt = build_user_prompt(
        items=items,
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
# Result coercion
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


def _finalize_single_source(llama: dict | None) -> dict:
    """
    Single-source finalisation: trust Ollama directly. `agreement` is set
    to True iff Ollama returned a directional verdict (bullish/bearish);
    a neutral verdict still emits a row but with score=0 and conf=0 so
    downstream sizing logic ignores it.
    """
    if not llama or llama["market_impact"] == "neutral":
        return {
            "sentiment_score": 0.0,
            "confidence": 0.0,
            "market_impact": "neutral",
            "key_events": list((llama or {}).get("key_events") or [])[:5],
            "agreement": False,
        }
    return {
        "sentiment_score": llama["sentiment_score"],
        "confidence": llama["confidence"],
        "market_impact": llama["market_impact"],
        "key_events": list(llama["key_events"])[:5],
        "agreement": True,
    }


# ---------------------------------------------------------------------------
# One poll cycle
# ---------------------------------------------------------------------------


async def _poll_once() -> dict | None:
    async with aiohttp.ClientSession(timeout=HTTP_TIMEOUT) as session:
        items = await _fetch_perplexity_news(session)
        if not items:
            logger.warning("no news items — emitting neutral row anyway")

        llama = await _analyze_ollama(session, items)

    final = _finalize_single_source(llama)
    ts_dt = datetime.now(timezone.utc)
    final["ts"] = int(ts_dt.timestamp())

    db.execute_one(
        """
        INSERT INTO sentiment_log
            (ts, sentiment_score, confidence, market_impact, agreement, key_events,
             llama_score, llama_impact, raw_llama, n_headlines)
        VALUES (%s, %s, %s, %s, %s, %s::jsonb, %s, %s, %s::jsonb, %s)
        ON CONFLICT (ts) DO UPDATE SET
            sentiment_score = EXCLUDED.sentiment_score,
            confidence      = EXCLUDED.confidence,
            market_impact   = EXCLUDED.market_impact,
            agreement       = EXCLUDED.agreement,
            key_events      = EXCLUDED.key_events,
            llama_score     = EXCLUDED.llama_score,
            llama_impact    = EXCLUDED.llama_impact,
            raw_llama       = EXCLUDED.raw_llama,
            n_headlines     = EXCLUDED.n_headlines
        """,
        (
            ts_dt,
            float(final["sentiment_score"]),
            float(final["confidence"]),
            final["market_impact"],
            bool(final["agreement"]),
            json.dumps(final["key_events"]),
            llama["sentiment_score"] if llama else None,
            llama["market_impact"] if llama else None,
            json.dumps(llama) if llama else None,
            len(items),
        ),
    )

    logger.info(
        "poll done: impact=%s score=%+.2f conf=%.2f items=%d",
        final["market_impact"], final["sentiment_score"],
        final["confidence"], len(items),
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
            "sentiment engine started (interval=%ds, perplexity=%s, ollama=%s)",
            POLL_INTERVAL_S, PERPLEXITY_MODEL, OLLAMA_MODEL,
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

    cutoff = datetime.now(timezone.utc) - pd.Timedelta(days=HISTORY_DAYS)
    try:
        rows = db.fetch_all(
            "SELECT ts, sentiment_score, confidence, market_impact, agreement "
            "FROM sentiment_log WHERE ts >= %s ORDER BY ts ASC",
            (cutoff,),
        )
    except Exception as exc:
        logger.warning("get_sentiment_features db error: %s", exc)
        return _empty_features()

    if not rows:
        return _empty_features()

    df = pd.DataFrame(rows)
    df["dt"] = pd.to_datetime(df["ts"], utc=True)
    df = df.set_index("dt").drop(columns=["ts"])

    out = pd.DataFrame(index=df.index)
    out["%-sentiment_score"] = df["sentiment_score"].astype(float)
    out["%-sentiment_confidence"] = df["confidence"].astype(float)
    out["%-sentiment_bullish"] = (df["market_impact"] == "bullish").astype(float)
    out["%-sentiment_bearish"] = (df["market_impact"] == "bearish").astype(float)
    out["%-sentiment_agreement"] = df["agreement"].astype(float)
    return out
