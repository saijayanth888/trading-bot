"""
Smoke test for the sentiment engine.

Run from a host shell.  ANTHROPIC_API_KEY needs to be exported for the
Claude pass; OLLAMA_HOST + OLLAMA_MODEL need to point at a running
ollama if you want the Llama pass.

    export ANTHROPIC_API_KEY=sk-ant-...
    export OLLAMA_HOST=http://localhost:11434
    python tests/test_sentiment.py

The script never aborts on missing dependencies — it skips the live-fetch
section and still verifies the schema and the get_sentiment_features
contract.
"""

from __future__ import annotations

import asyncio
import os
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "user_data"))

from modules.sentiment_engine import (   # noqa: E402
    CLAUDE_MODEL,
    DB_PATH,
    FEATURE_COLUMNS,
    LOG_PATH,
    OLLAMA_BASE,
    OLLAMA_MODEL,
    SentimentEngine,
    _fetch_all_reddit,
    _fetch_all_rss,
    _poll_once,
    get_sentiment_features,
)


def _ok(msg: str) -> None:
    print(f"  [✓] {msg}")


def _skip(msg: str) -> None:
    print(f"  [-] SKIP: {msg}")


def test_paths() -> None:
    print("== paths ==")
    assert DB_PATH.parent.exists(), f"data dir missing: {DB_PATH.parent}"
    assert LOG_PATH.parent.exists(), f"logs dir missing: {LOG_PATH.parent}"
    _ok(f"DB_PATH ready: {DB_PATH}")
    _ok(f"LOG_PATH ready: {LOG_PATH}")
    _ok(f"CLAUDE_MODEL: {CLAUDE_MODEL}")
    _ok(f"OLLAMA: {OLLAMA_MODEL} @ {OLLAMA_BASE}")


def test_schema() -> None:
    print("== sentiment_log schema ==")
    assert DB_PATH.exists(), f"DB not initialised at {DB_PATH}"
    with sqlite3.connect(str(DB_PATH)) as conn:
        cols = [
            r[1] for r in conn.execute("PRAGMA table_info(sentiment_log)")
        ]
    expected = {
        "ts", "sentiment_score", "confidence", "market_impact", "agreement",
        "key_events", "claude_score", "llama_score", "claude_impact",
        "llama_impact", "n_headlines", "n_reddit", "raw_claude", "raw_llama",
    }
    missing = expected - set(cols)
    assert not missing, f"missing columns: {missing}"
    _ok(f"columns: {sorted(expected)}")


def test_rss_and_reddit() -> None:
    print("== RSS + Reddit fetch ==")
    import aiohttp

    async def _go() -> tuple[int, int]:
        async with aiohttp.ClientSession() as s:
            rss, reddit = await asyncio.gather(
                _fetch_all_rss(s), _fetch_all_reddit(s),
            )
        return len(rss), len(reddit)

    n_rss, n_reddit = asyncio.run(_go())
    _ok(f"rss items: {n_rss} | reddit posts: {n_reddit}")
    if n_rss + n_reddit == 0:
        _skip("network unreachable — RSS + Reddit returned 0 items")


def test_full_poll_cycle() -> None:
    print("== full poll cycle (RSS + Reddit + Claude + Ollama + DB) ==")
    have_claude = bool(os.getenv("ANTHROPIC_API_KEY", "").strip())
    if not have_claude:
        _skip("ANTHROPIC_API_KEY not set — cannot exercise Claude pass")
        return
    try:
        import anthropic            # noqa: F401
    except ImportError:
        _skip("anthropic SDK not installed — pip install anthropic")
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

    with sqlite3.connect(str(DB_PATH)) as conn:
        n = conn.execute("SELECT COUNT(*) FROM sentiment_log").fetchone()[0]
    assert n >= 1, "expected at least one row in sentiment_log"
    _ok(f"sentiment_log rows: {n}")


def test_get_sentiment_features() -> None:
    print("== get_sentiment_features contract ==")
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
        _ok(
            f"index: tz={df.index.tz}, "
            f"range=[{df.index[0]} .. {df.index[-1]}]"
        )


def main() -> int:
    print("=" * 62)
    print(" sentiment engine smoke tests")
    print("=" * 62)
    try:
        test_paths()
        test_schema()
        test_rss_and_reddit()
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
