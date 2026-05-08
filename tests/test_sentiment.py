"""
Smoke test for the sentiment engine (Perplexity → Ollama → PostgreSQL).

Set DATABASE_URL to a reachable Postgres+TimescaleDB instance. The full
poll cycle additionally needs PERPLEXITY_API_KEY and a running Ollama;
the test skips those steps cleanly when missing.

    export DATABASE_URL=postgresql://tradebot:test@localhost:5434/tradebot
    export PERPLEXITY_API_KEY=...                 # optional
    export OLLAMA_HOST=http://localhost:11434     # optional
    python tests/test_sentiment.py
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "user_data"))

from modules.sentiment_engine import (   # noqa: E402
    FEATURE_COLUMNS,
    LOG_PATH,
    OLLAMA_BASE,
    OLLAMA_MODEL,
    PERPLEXITY_BASE,
    PERPLEXITY_MODEL,
    PERPLEXITY_RECENCY,
    SentimentEngine,
    _fetch_perplexity_news,
    _poll_once,
    get_sentiment_features,
)
from modules import db                     # noqa: E402


def _ok(msg: str) -> None: print(f"  [✓] {msg}")
def _skip(msg: str) -> None: print(f"  [-] SKIP: {msg}")


def test_paths() -> None:
    print("== paths ==")
    assert LOG_PATH.parent.exists(), f"logs dir missing: {LOG_PATH.parent}"
    _ok(f"LOG_PATH ready: {LOG_PATH}")
    _ok(f"PERPLEXITY: {PERPLEXITY_MODEL} (recency={PERPLEXITY_RECENCY}) @ {PERPLEXITY_BASE}")
    _ok(f"OLLAMA: {OLLAMA_MODEL} @ {OLLAMA_BASE}")


def test_schema() -> None:
    print("== sentiment_log schema ==")
    if not db.is_reachable():
        _skip(f"Postgres not reachable at {db._redacted_dsn()}")
        return
    db.ensure_schema()
    cols = [r["column_name"] for r in db.fetch_all(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_name = 'sentiment_log' ORDER BY ordinal_position"
    )]
    expected = {
        "ts", "sentiment_score", "confidence", "market_impact", "agreement",
        "key_events", "claude_score", "llama_score", "claude_impact",
        "llama_impact", "n_headlines", "n_reddit", "raw_claude", "raw_llama",
    }
    missing = expected - set(cols)
    assert not missing, f"missing columns: {missing}"
    _ok(f"columns present: {sorted(cols)}")


def test_perplexity_fetch() -> None:
    print("== Perplexity headline fetch ==")
    if not os.getenv("PERPLEXITY_API_KEY", "").strip():
        _skip("PERPLEXITY_API_KEY not set")
        return
    import aiohttp

    async def _go() -> int:
        async with aiohttp.ClientSession() as s:
            items = await _fetch_perplexity_news(s)
        return len(items)

    n = asyncio.run(_go())
    _ok(f"perplexity items: {n}")
    if n == 0:
        _skip("perplexity returned 0 items (network or model issue)")


def test_full_poll_cycle() -> None:
    print("== full poll cycle (perplexity + ollama + DB) ==")
    if not os.getenv("PERPLEXITY_API_KEY", "").strip():
        _skip("PERPLEXITY_API_KEY not set — cannot exercise full pipeline")
        return
    if not db.is_reachable():
        _skip("Postgres not reachable — set DATABASE_URL")
        return
    print("  running one poll cycle (may take 30-90s)...")
    result = asyncio.run(_poll_once())
    if result is None:
        _skip("poll returned None — see sentiment.log")
        return
    _ok(
        f"poll done: agreement={result['agreement']} "
        f"impact={result['market_impact']} "
        f"score={result['sentiment_score']:+.2f} "
        f"conf={result['confidence']:.2f}"
    )
    n = db.fetch_one("SELECT COUNT(*)::bigint AS n FROM sentiment_log")["n"]
    assert n >= 1
    _ok(f"sentiment_log rows: {n}")


def test_get_sentiment_features() -> None:
    print("== get_sentiment_features contract ==")
    if not db.is_reachable():
        _skip("Postgres not reachable")
        return
    df = get_sentiment_features("BTC/USD")
    expected = set(FEATURE_COLUMNS)
    missing = expected - set(df.columns)
    assert not missing, f"missing feature columns: {missing}"
    _ok(f"columns: {sorted(df.columns)} | rows: {len(df)}")
    if df.empty:
        _skip("DataFrame empty — no sentiment_log rows yet")
    else:
        assert df.index.is_monotonic_increasing, "index must be sorted"
        assert df.index.tz is not None, "index must be tz-aware"
        _ok(f"index: tz={df.index.tz}, range=[{df.index[0]} .. {df.index[-1]}]")


def main() -> int:
    print("=" * 62)
    print(" sentiment engine smoke tests")
    print("=" * 62)
    try:
        test_paths()
        test_schema()
        test_perplexity_fetch()
        test_full_poll_cycle()
        test_get_sentiment_features()
    except AssertionError as exc:
        print(f"\nFAIL: {exc}")
        return 1
    except Exception as exc:
        print(f"\nERROR: {type(exc).__name__}: {exc}")
        return 2
    print("\nALL CHECKS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
