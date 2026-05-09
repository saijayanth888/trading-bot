"""
Sentiment engine — Perplexity (news fetcher, optional) + dual local Ollama
(Hermes-3 trust-the-majority scorer).

Every 15 minutes:
  1. (Optional) Ask Perplexity Sonar for crypto market headlines from the
     last hour. If `PERPLEXITY_API_KEY` is unset, we feed an empty list to
     the scorers and let them emit a low-confidence neutral.
  2. Score the headlines with TWO local Ollama models in parallel:
        fast → OLLAMA_MODEL_FAST (default hermes3:8b)
        deep → OLLAMA_MODEL_DEEP (default hermes3:70b)
  3. Trust-The-Majority: emit a directional signal only when both models
     agree on `market_impact`; otherwise emit neutral with low confidence.
  4. Store the verdict + both raw responses in `sentiment_log` (Postgres).

Both scoring models run locally on the Spark via Ollama — zero external
API calls in the hot path. Perplexity is the single optional outbound.

`get_sentiment_features(pair)` returns a DataFrame with FreqAI-prefixed
columns suitable for `pd.merge_asof` onto a candle dataframe.

Environment:
  PERPLEXITY_API_KEY  — optional; if set, fetches news from Sonar.
  PERPLEXITY_MODEL    — default "sonar".
  PERPLEXITY_RECENCY  — default "hour".
  OLLAMA_HOST         — default "http://host.docker.internal:11434".
  OLLAMA_MODEL_FAST   — default "hermes3:8b" — used as the fast scanner.
  OLLAMA_MODEL_DEEP   — default "hermes3:70b" — used as the deep
                        thinker. If the model isn't pulled yet, the engine
                        falls back to fast-only mode and logs a warning.
  OLLAMA_MODEL        — legacy alias for OLLAMA_MODEL_FAST (still honoured).
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

# Tunable via env or config.json[sentiment_pipeline]; SENTIMENT_POLL_INTERVAL_S
# is the standard freqtrade-style env override. Defaults match the prior
# hardcoded values so nothing changes for existing deployments.
POLL_INTERVAL_S = int(os.environ.get("SENTIMENT_POLL_INTERVAL_S", "900"))     # default 15 min
HISTORY_DAYS = int(os.environ.get("SENTIMENT_HISTORY_DAYS", "7"))             # accessor lookback
HTTP_TIMEOUT = aiohttp.ClientTimeout(total=60)

PERPLEXITY_BASE = os.getenv("PERPLEXITY_BASE", "https://api.perplexity.ai").rstrip("/")
PERPLEXITY_MODEL = os.getenv("PERPLEXITY_MODEL", "sonar")
PERPLEXITY_RECENCY = os.getenv("PERPLEXITY_RECENCY", "hour")  # hour|day|week|month
PERPLEXITY_MAX_TOKENS = int(os.getenv("PERPLEXITY_MAX_TOKENS", "1500"))

OLLAMA_BASE = os.getenv("OLLAMA_HOST", "http://host.docker.internal:11434").rstrip("/")
# OLLAMA_MODEL is kept as a backwards-compatible alias for OLLAMA_MODEL_FAST.
OLLAMA_MODEL_FAST = os.getenv("OLLAMA_MODEL_FAST",
                              os.getenv("OLLAMA_MODEL", "hermes3:8b"))
OLLAMA_MODEL_DEEP = os.getenv("OLLAMA_MODEL_DEEP", "hermes3:70b")
# Exposed for downstream code that imports OLLAMA_MODEL by name.
OLLAMA_MODEL = OLLAMA_MODEL_FAST

# Truncate the headline list passed to Ollama so prompts stay bounded.
MAX_HEADLINES_TO_LLM = int(os.environ.get("SENTIMENT_MAX_HEADLINES_TO_LLM", "60"))

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


def _dedup_dicts_by_title(items: list[dict[str, Any]], threshold: float = 0.80) -> list[dict[str, Any]]:
    """Drop near-duplicate ``items`` by fuzzy title match.

    Used after merging Perplexity's items with the multi-source aggregator's —
    the same headline often shows up in both. Newer item wins on tie.
    """
    import re as _re
    from difflib import SequenceMatcher as _SM
    if not items:
        return []
    norm = lambda s: _re.sub(r"[^a-z0-9 ]+", " ", str(s or "").lower()).strip()  # noqa: E731
    kept: list[dict[str, Any]] = []
    kept_norm: list[str] = []
    for it in items:
        n = norm(it.get("title"))
        if not n:
            continue
        if any(_SM(None, n, k).ratio() >= threshold for k in kept_norm):
            continue
        kept.append(it)
        kept_norm.append(n)
    return kept


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
    model: str | None = None,
    *,
    num_ctx: int = 4096,
    timeout_total: float = 180,
    keep_alive: str = "30s",
) -> dict | None:
    """
    Score `items` with the given Ollama model. Returns None on failure.

    `keep_alive` controls how long Ollama keeps the model in VRAM after
    this call. We default to "30s" — long enough to absorb a same-cycle
    follow-up, short enough that the 70B (~91 GB allocation) doesn't park
    in GPU between 15-min poll cycles. Override to "0s" to evict
    immediately (good for one-shot tools).
    """
    from .sentiment_prompts import OLLAMA_SYSTEM_PROMPT, build_user_prompt

    target = model or OLLAMA_MODEL_FAST
    user_prompt = build_user_prompt(
        items=items,
        window_minutes=POLL_INTERVAL_S // 60,
        now_iso=datetime.now(timezone.utc).isoformat(timespec="seconds"),
    )

    payload = {
        "model": target,
        "messages": [
            {"role": "system", "content": OLLAMA_SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ],
        "format": "json",
        "stream": False,
        "keep_alive": keep_alive,
        "options": {"temperature": 0.2, "num_ctx": num_ctx},
    }

    async with _OLLAMA_RL():
        resp = await _request_with_backoff(
            session, "POST", f"{OLLAMA_BASE}/api/chat",
            json_body=payload,
            timeout=aiohttp.ClientTimeout(total=timeout_total),
        )
    if resp is None:
        return None
    try:
        body = await resp.json(content_type=None)
    except (json.JSONDecodeError, aiohttp.ContentTypeError) as exc:
        logger.warning("ollama[%s] bad JSON envelope: %s", target, exc)
        return None
    finally:
        resp.release()

    # Detect "model not pulled yet" so the caller can fall back gracefully.
    if isinstance(body, dict) and "error" in body:
        logger.warning("ollama[%s] error: %s", target, body["error"])
        return None

    content = (body.get("message") or {}).get("content", "")
    if not content:
        logger.warning("ollama[%s] empty content; body=%s", target, body)
        return None
    try:
        data = json.loads(content)
    except json.JSONDecodeError as exc:
        logger.warning("ollama[%s] non-JSON content: %s | snippet=%s",
                       target, exc, content[:200])
        return None
    return _coerce_result(data)


def _trust_the_majority(fast: dict | None, deep: dict | None) -> dict:
    """
    Both Hermes models must agree on direction for a non-neutral verdict.
    Confidence is the min of the two (worst-case bound). If only one model
    returned (e.g. 70B not pulled yet), trust it but halve confidence.
    """
    have_both = bool(fast) and bool(deep)
    if have_both:
        same_dir = (
            fast["market_impact"] == deep["market_impact"]
            and fast["market_impact"] in ("bullish", "bearish")
        )
        if same_dir:
            return {
                "sentiment_score": (fast["sentiment_score"] + deep["sentiment_score"]) / 2,
                "confidence": min(fast["confidence"], deep["confidence"]),
                "market_impact": fast["market_impact"],
                "key_events": list(fast["key_events"])[:5],
                "agreement": True,
            }
        # Disagreement — neutral
        return {
            "sentiment_score": 0.0,
            "confidence": 0.0,
            "market_impact": "neutral",
            "key_events": list((fast or deep or {}).get("key_events") or [])[:5],
            "agreement": False,
        }

    # Single-model fallback
    src = fast or deep
    if not src or src["market_impact"] == "neutral":
        return {
            "sentiment_score": 0.0, "confidence": 0.0,
            "market_impact": "neutral",
            "key_events": list((src or {}).get("key_events") or [])[:5],
            "agreement": False,
        }
    return {
        "sentiment_score": src["sentiment_score"],
        "confidence": src["confidence"] * 0.5,    # halved — single source
        "market_impact": src["market_impact"],
        "key_events": list(src["key_events"])[:5],
        "agreement": False,
    }


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


# `_finalize_single_source` retired — `_trust_the_majority` above handles
# both single-source and dual-model cases.


# ---------------------------------------------------------------------------
# One poll cycle
# ---------------------------------------------------------------------------


async def _poll_once() -> dict | None:
    # Multi-source news aggregation: kick off the 6-source aggregator alongside
    # the Perplexity fetcher so we have 7 channels to dedup + reason over.
    from . import news_aggregator as _news
    aggregator = _news.aggregator()

    async with aiohttp.ClientSession(timeout=HTTP_TIMEOUT) as session:
        # Run Perplexity + 6-source aggregator concurrently.
        perp_task = asyncio.create_task(_fetch_perplexity_news(session))
        agg_task  = asyncio.create_task(aggregator.poll_all_sources())
        perp_items, agg_result = await asyncio.gather(
            perp_task, agg_task, return_exceptions=False,
        )

        # Convert aggregator NewsItem instances into the LLM-prompt shape and
        # merge with Perplexity's. Aggregator-side items are already deduped;
        # we re-dedup once more after the merge to drop Perplexity duplicates.
        merged_items: list[dict[str, Any]] = list(perp_items or [])
        for ni in agg_result.items:
            merged_items.append({
                "title":   ni.title[:240],
                "summary": ni.summary[:600] if ni.summary else "",
                "source":  ni.source,
                "citation": ni.url,
            })
        items = _dedup_dicts_by_title(merged_items)
        items = items[:MAX_HEADLINES_TO_LLM]

        # Persist the deduped aggregated headlines + Fear & Greed snapshot.
        try:
            _news.store_aggregated(agg_result)
        except Exception as exc:
            logger.debug("[sentiment] news_aggregator DB write skipped: %s", exc)

        if not items:
            logger.info(
                "no news items from any source — both models will see an empty list "
                "(sources_ok=%s sources_failed=%s)",
                agg_result.sources_ok, [s for s, _ in agg_result.sources_failed],
            )

        # Fast + deep run in parallel; deep model gets a larger context window.
        # The 70B costs ~50-91 GB of GPU memory to keep loaded, so we use
        # keep_alive="0s" on the deep call — model evicts from VRAM right
        # after the response, freeing the GPU for TFT training between
        # 15-min sentiment polls. The 8B is small enough to keep warm.
        # If a model isn't pulled yet, _analyze_ollama returns None and the
        # majority logic falls back to single-source mode.
        fast, deep = await asyncio.gather(
            _analyze_ollama(session, items, OLLAMA_MODEL_FAST,
                            num_ctx=4096, timeout_total=120,
                            keep_alive="5m"),
            _analyze_ollama(session, items, OLLAMA_MODEL_DEEP,
                            num_ctx=8192, timeout_total=300,
                            keep_alive="0s"),
            return_exceptions=False,
        )

    final = _trust_the_majority(fast, deep)
    ts_dt = datetime.now(timezone.utc)
    final["ts"] = int(ts_dt.timestamp())

    # ── Multi-source side-channel signals (no LLM scoring) ──
    fg_value = agg_result.fear_greed.value if agg_result.fear_greed else None
    fg_class = agg_result.fear_greed.classification if agg_result.fear_greed else None

    community_scores = [
        ni.community_sentiment for ni in agg_result.items
        if ni.community_sentiment is not None
    ]
    community_avg = (sum(community_scores) / len(community_scores)) if community_scores else None

    reddit_scores = [
        ni.attention_score for ni in agg_result.items
        if ni.source.startswith("reddit:") and ni.attention_score is not None
    ]
    reddit_avg = (sum(reddit_scores) / len(reddit_scores)) if reddit_scores else None

    trending_pairs = (agg_result.trending.coins if agg_result.trending else [])
    final["fear_greed_value"] = fg_value
    final["fear_greed_classification"] = fg_class
    final["community_score_avg"] = community_avg
    final["reddit_attention_avg"] = reddit_avg
    final["trending_pairs"] = trending_pairs
    final["sources_ok"] = agg_result.sources_ok
    final["sources_failed"] = [s for s, _ in agg_result.sources_failed]

    # Reuse the legacy schema columns: claude_* now hold the deep model's
    # output, llama_* hold the fast model's output. Avoids a migration.
    # Plus the new multi-source columns added by news_aggregator (Fear &
    # Greed, community sentiment, Reddit attention, trending pairs).
    db.execute_one(
        """
        INSERT INTO sentiment_log
            (ts, sentiment_score, confidence, market_impact, agreement, key_events,
             claude_score, claude_impact, raw_claude,
             llama_score, llama_impact, raw_llama,
             n_headlines,
             fear_greed_value, fear_greed_classification,
             community_score_avg, reddit_attention_avg, trending_pairs,
             sources_ok, sources_failed)
        VALUES (%s, %s, %s, %s, %s, %s::jsonb,
                %s, %s, %s::jsonb,
                %s, %s, %s::jsonb,
                %s,
                %s, %s,
                %s, %s, %s::jsonb,
                %s::jsonb, %s::jsonb)
        ON CONFLICT (ts) DO UPDATE SET
            sentiment_score = EXCLUDED.sentiment_score,
            confidence      = EXCLUDED.confidence,
            market_impact   = EXCLUDED.market_impact,
            agreement       = EXCLUDED.agreement,
            key_events      = EXCLUDED.key_events,
            claude_score    = EXCLUDED.claude_score,
            claude_impact   = EXCLUDED.claude_impact,
            raw_claude      = EXCLUDED.raw_claude,
            llama_score     = EXCLUDED.llama_score,
            llama_impact    = EXCLUDED.llama_impact,
            raw_llama       = EXCLUDED.raw_llama,
            n_headlines     = EXCLUDED.n_headlines,
            fear_greed_value = EXCLUDED.fear_greed_value,
            fear_greed_classification = EXCLUDED.fear_greed_classification,
            community_score_avg  = EXCLUDED.community_score_avg,
            reddit_attention_avg = EXCLUDED.reddit_attention_avg,
            trending_pairs       = EXCLUDED.trending_pairs,
            sources_ok           = EXCLUDED.sources_ok,
            sources_failed       = EXCLUDED.sources_failed
        """,
        (
            ts_dt,
            float(final["sentiment_score"]),
            float(final["confidence"]),
            final["market_impact"],
            bool(final["agreement"]),
            json.dumps(final["key_events"]),
            deep["sentiment_score"] if deep else None,
            deep["market_impact"] if deep else None,
            json.dumps(deep) if deep else None,
            fast["sentiment_score"] if fast else None,
            fast["market_impact"] if fast else None,
            json.dumps(fast) if fast else None,
            len(items),
            final.get("fear_greed_value"),
            final.get("fear_greed_classification"),
            final.get("community_score_avg"),
            final.get("reddit_attention_avg"),
            json.dumps(final.get("trending_pairs") or []),
            json.dumps(final.get("sources_ok") or []),
            json.dumps(final.get("sources_failed") or []),
        ),
    )

    logger.info(
        "poll done: impact=%s score=%+.2f conf=%.2f agree=%s items=%d "
        "(fast=%s deep=%s)",
        final["market_impact"], final["sentiment_score"],
        final["confidence"], final["agreement"], len(items),
        fast["market_impact"] if fast else "—",
        deep["market_impact"] if deep else "—",
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
            "sentiment engine started (interval=%ds, perplexity=%s, "
            "ollama_fast=%s, ollama_deep=%s)",
            POLL_INTERVAL_S, PERPLEXITY_MODEL, OLLAMA_MODEL_FAST, OLLAMA_MODEL_DEEP,
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
    # Existing — LLM-scored sentiment from the Hermes 3 70B + 8B "Trust the Majority" pair.
    "%-sentiment_score",
    "%-sentiment_confidence",
    "%-sentiment_bullish",
    "%-sentiment_bearish",
    "%-sentiment_agreement",
    # New — direct features from the multi-source aggregator (no LLM scoring).
    # Fear & Greed: 0..1 (raw 0–100 / 100). Bridge between extreme greed (potential
    # blow-off) and extreme fear (capitulation / accumulation zone).
    "%-sentiment_fear_greed",
    # One-hot encoding for the FNG classification — lets the TFT pick up regime-
    # like effects of market mood without having to threshold the continuous value.
    "%-sentiment_fng_extreme_fear",
    "%-sentiment_fng_fear",
    "%-sentiment_fng_neutral",
    "%-sentiment_fng_greed",
    "%-sentiment_fng_extreme_greed",
    # Reddit upvote-ratio average across pair-tagged posts, mapped to (-1..+1).
    "%-sentiment_community_score",
    # Reddit attention proxy: avg normalised score across r/cryptocurrency, r/bitcoin,
    # r/ethtrader hot posts (0..1). Spikes flag attention surges.
    "%-sentiment_reddit_attention",
    # 1.0 if any of our pairs is in CoinGecko's top-7 trending search list.
    "%-sentiment_trending",
)

_NEUTRAL_FEATURE_VALUES: dict[str, float] = {c: 0.0 for c in FEATURE_COLUMNS}


def _empty_features() -> pd.DataFrame:
    return pd.DataFrame(columns=list(FEATURE_COLUMNS))


_FNG_CLASS_TO_ONEHOT: dict[str, str] = {
    "Extreme Fear":  "%-sentiment_fng_extreme_fear",
    "Fear":          "%-sentiment_fng_fear",
    "Neutral":       "%-sentiment_fng_neutral",
    "Greed":         "%-sentiment_fng_greed",
    "Extreme Greed": "%-sentiment_fng_extreme_greed",
}


def get_sentiment_features(pair: str) -> pd.DataFrame:
    """
    Return a DataFrame indexed by UTC datetime with sentiment features.

    ``pair`` is accepted for symmetry with ``onchain_signals.get_features`` —
    the current implementation returns broad-market sentiment that is the
    same for all pairs, but the ``%-sentiment_trending`` column is per-pair
    (1.0 only if THIS pair is in the trending list at that timestamp).

    Caller should ``pd.merge_asof`` the result onto its candle dataframe with
    ``direction='backward'`` and ffill missing values.
    """
    SentimentEngine.instance().start()                   # lazy start

    cutoff = datetime.now(timezone.utc) - pd.Timedelta(days=HISTORY_DAYS)
    try:
        rows = db.fetch_all(
            """
            SELECT ts, sentiment_score, confidence, market_impact, agreement,
                   fear_greed_value, fear_greed_classification,
                   community_score_avg, reddit_attention_avg, trending_pairs
            FROM sentiment_log
            WHERE ts >= %s
            ORDER BY ts ASC
            """,
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
    # Existing LLM-scored features
    out["%-sentiment_score"] = df["sentiment_score"].astype(float)
    out["%-sentiment_confidence"] = df["confidence"].astype(float)
    out["%-sentiment_bullish"] = (df["market_impact"] == "bullish").astype(float)
    out["%-sentiment_bearish"] = (df["market_impact"] == "bearish").astype(float)
    out["%-sentiment_agreement"] = df["agreement"].astype(float)

    # Fear & Greed continuous + one-hot
    fng_val = df.get("fear_greed_value")
    out["%-sentiment_fear_greed"] = (
        fng_val.astype(float) / 100.0 if fng_val is not None else 0.0
    )
    fng_cls = df.get("fear_greed_classification")
    for col in (
        "%-sentiment_fng_extreme_fear",
        "%-sentiment_fng_fear",
        "%-sentiment_fng_neutral",
        "%-sentiment_fng_greed",
        "%-sentiment_fng_extreme_greed",
    ):
        out[col] = 0.0
    if fng_cls is not None:
        for cls_name, col in _FNG_CLASS_TO_ONEHOT.items():
            out[col] = (fng_cls == cls_name).astype(float)

    # Community + Reddit attention
    out["%-sentiment_community_score"] = (
        df.get("community_score_avg", pd.Series([0.0] * len(df), index=df.index))
        .astype(float).fillna(0.0)
    )
    out["%-sentiment_reddit_attention"] = (
        df.get("reddit_attention_avg", pd.Series([0.0] * len(df), index=df.index))
        .astype(float).fillna(0.0)
    )

    # CoinGecko trending — per-pair: extract the symbol prefix of `pair`
    # (e.g. "BTC/USD" → "BTC") and check membership in trending_pairs at each ts.
    pair_sym = (pair.split("/")[0] if pair else "").upper()
    if pair_sym and "trending_pairs" in df.columns:
        def _is_trending(v: Any) -> float:
            if v is None:
                return 0.0
            if isinstance(v, str):
                # JSONB returned as string in some envs — defensive parse.
                try:
                    import json as _json
                    v = _json.loads(v)
                except (TypeError, ValueError):
                    return 0.0
            if isinstance(v, (list, tuple)):
                return 1.0 if pair_sym in {str(x).upper() for x in v} else 0.0
            return 0.0
        out["%-sentiment_trending"] = df["trending_pairs"].apply(_is_trending)
    else:
        out["%-sentiment_trending"] = 0.0

    return out
