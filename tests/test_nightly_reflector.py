"""Tests for ``scripts/nightly_reflector.py``.

Strategy: import the module by path (it's a script, not a package), then
monkeypatch every external boundary — Postgres, yfinance, the sibling
chat_structured / update_with_outcome helpers.

We assert on:
  1. Three sample trades flowing all the way through to three writes.
  2. Alpha-not-cited triggers retry, then errors out cleanly.
  3. Idempotency — re-running on the same day skips already-reflected trades.
  4. Alpha-vs-benchmark math (mocked yfinance prices).
  5. Ollama unreachable → log + exit 0 (no traceback).
"""
from __future__ import annotations

import importlib.util
import sys
import types
from datetime import datetime, timezone
from pathlib import Path

import pytest


# --- Module-under-test loader ----------------------------------------------

def _load_module():
    """Import scripts/nightly_reflector.py as 'nr' from anywhere in the tree."""
    tests_dir = Path(__file__).resolve().parent
    # Walk upward to find a sibling 'scripts/nightly_reflector.py'.
    cur = tests_dir
    target: Path | None = None
    for _ in range(6):
        cand = cur / "scripts" / "nightly_reflector.py"
        if cand.exists():
            target = cand
            break
        cur = cur.parent
    assert target is not None, "could not locate scripts/nightly_reflector.py"

    spec = importlib.util.spec_from_file_location("nightly_reflector", target)
    mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    assert spec and spec.loader
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def nr(monkeypatch):
    """Module under test, with sys.argv neutralised so argparse stops
    consuming pytest's own CLI flags."""
    monkeypatch.setattr(sys, "argv", ["nightly_reflector"])
    return _load_module()


# --- Common fakes ----------------------------------------------------------

class _FakeReflection:
    """Stands in for the validated Pydantic model."""
    def __init__(self, text: str, alpha_cited: bool = True):
        self.text = text
        self.alpha_cited = alpha_cited


def _trade(pair: str, opened: str, closed: str, *,
           pnl=10.0, pnl_pct=2.0, entry=100.0, exit_=102.0,
           reason="exit_signal", regime="trending_up") -> dict:
    return {
        "trade_id": hash((pair, opened)) & 0xFFFF,
        "pair": pair,
        "direction": "long",
        "opened_at": datetime.fromisoformat(opened).replace(tzinfo=timezone.utc),
        "closed_at": datetime.fromisoformat(closed).replace(tzinfo=timezone.utc),
        "entry_price": entry,
        "exit_price": exit_,
        "pnl": pnl,
        "pnl_pct": pnl_pct,
        "exit_reason": reason,
        "regime": regime,
        "reasoning": "tested entry thesis",
    }


def _patch_siblings(monkeypatch, nr, *,
                    llm_text: str | Exception | list | None = None,
                    written: list | None = None,
                    update_raises: Exception | None = None):
    """Replace the two defensive-import shims with in-memory fakes.

    ``llm_text``:
      - str          → returned verbatim every call.
      - Exception    → raised every call.
      - list[str]    → popped FIFO; ``RuntimeError`` if exhausted.
      - None (default) → produces an alpha-cited 2-4 sentence reflection
        derived from the user-prompt's "Alpha vs ...: +X.XX%" line — the
        common case for happy-path tests.

    Returns the list passed in via ``written`` so callers can introspect.
    """
    written = written if written is not None else []
    import re as _re

    def _derive_text_from_prompt(user: str) -> str:
        m = _re.search(r"Alpha vs [^:]+:\s*([+-]?\d+\.\d+)%", user or "")
        alpha_str = m.group(1) if m else "+0.0"
        # Sign-prefix with explicit + so the reflection always quotes the
        # signed value the regex looks for.
        if not alpha_str.startswith(("+", "-")):
            alpha_str = "+" + alpha_str
        return (
            f"Directional call was correct with alpha of {alpha_str}%. "
            f"The trending_up regime entry tag held through the close. "
            f"Lesson: keep risk-on entries in confirmed trending regimes."
        )

    def _fake_chat_structured(provider, tier, system, user, schema,
                              max_retries=2, **kw):
        if isinstance(llm_text, Exception):
            raise llm_text
        if isinstance(llm_text, list):
            if not llm_text:
                raise RuntimeError("test exhausted llm_text list")
            return schema(text=llm_text.pop(0), alpha_cited=True)
        if isinstance(llm_text, str):
            return schema(text=llm_text, alpha_cited=True)
        # Default: synthesise a valid alpha-citing response
        return schema(text=_derive_text_from_prompt(user), alpha_cited=True)

    def _fake_update_with_outcome(*, date, ticker, pnl_pct, alpha_pct,
                                  holding_days, reflection):
        if update_raises is not None:
            raise update_raises
        written.append({
            "date": date, "ticker": ticker, "pnl_pct": pnl_pct,
            "alpha_pct": alpha_pct, "holding_days": holding_days,
            "reflection": reflection,
        })

    # Build a real Pydantic schema so the fake honours validation.
    from pydantic import BaseModel, Field, model_validator
    schema = nr._build_reflection_schema(BaseModel, Field, model_validator)

    monkeypatch.setattr(nr, "_import_chat_structured",
                        lambda: (_fake_chat_structured, BaseModel, Field, model_validator))
    monkeypatch.setattr(nr, "_build_reflection_schema",
                        lambda *a, **k: schema)
    monkeypatch.setattr(nr, "_import_memory",
                        lambda: _fake_update_with_outcome)
    return written


def _patch_no_yf(monkeypatch, nr, return_pct=1.0):
    """Bypass yfinance — return a fixed benchmark return (in percent)."""
    monkeypatch.setattr(nr, "_benchmark_return_pct",
                        lambda benchmark, opened_at, closed_at: return_pct)


def _patch_decisions_empty(monkeypatch, nr):
    monkeypatch.setattr(nr, "_decisions_text", lambda: "")


# --- 1. End-to-end happy path ----------------------------------------------

def test_three_trades_three_reflections(monkeypatch, nr):
    rows = [
        _trade("BTC/USD", "2026-05-10T14:00:00", "2026-05-10T20:00:00",
               pnl=15.0, pnl_pct=1.5),
        _trade("AAPL",    "2026-05-10T13:30:00", "2026-05-10T19:55:00",
               pnl=-3.0, pnl_pct=-0.5),
        _trade("ETH/USD", "2026-05-10T10:00:00", "2026-05-10T17:00:00",
               pnl=22.0, pnl_pct=3.3),
    ]
    monkeypatch.setattr(nr, "_query_closed_trades", lambda **kw: rows)
    _patch_no_yf(monkeypatch, nr, return_pct=0.5)  # alpha = pnl_pct - 0.5
    _patch_decisions_empty(monkeypatch, nr)
    # llm_text=None → default synthesises an alpha-citing reflection per call
    written = _patch_siblings(monkeypatch, nr, llm_text=None)

    rc = nr.main()
    assert rc == 0
    assert len(written) == 3
    pairs = [w["ticker"] for w in written]
    assert pairs == ["BTC/USD", "AAPL", "ETH/USD"]


# --- 2. Alpha-not-cited → retry → fail → skip ------------------------------

def test_alpha_not_cited_retries_then_skips(monkeypatch, nr):
    """If the LLM returns text without the alpha figure, the script must
    retry once and then mark the trade as errored (count++) without
    raising."""
    row = _trade("BTC/USD", "2026-05-10T14:00:00", "2026-05-10T20:00:00",
                 pnl=15.0, pnl_pct=1.5)
    monkeypatch.setattr(nr, "_query_closed_trades", lambda **kw: [row])
    _patch_no_yf(monkeypatch, nr, return_pct=0.5)  # alpha = +1.0%
    _patch_decisions_empty(monkeypatch, nr)
    # Two responses neither of which mention "+1.0%" — both should fail
    # the deterministic post-LLM regex check.
    written = _patch_siblings(
        monkeypatch, nr,
        llm_text=[
            "We were broadly correct with strong returns and the regime "
            "thesis worked well; lesson learned about position size.",
            "Again broadly correct with the regime thesis intact and "
            "lesson learned for the next similar trade in this regime.",
        ],
    )

    rc = nr.main()
    assert rc == 0
    assert written == []  # never wrote because LLM never cited alpha


# --- 3. Idempotency --------------------------------------------------------

def test_idempotent_skip_when_already_reflected(monkeypatch, nr):
    row = _trade("AAPL", "2026-05-10T13:30:00", "2026-05-10T19:55:00",
                 pnl=-3.0, pnl_pct=-0.5)
    monkeypatch.setattr(nr, "_query_closed_trades", lambda **kw: [row])
    _patch_no_yf(monkeypatch, nr, return_pct=0.5)
    # Pre-populate decisions.md with a non-pending entry for this trade.
    monkeypatch.setattr(
        nr, "_decisions_text",
        lambda: "- [LOSS] 2026-05-10 AAPL — alpha -1.0% — already reflected\n",
    )
    written = _patch_siblings(monkeypatch, nr,
                               llm_text="should not be called")

    rc = nr.main()
    assert rc == 0
    assert written == []


def test_idempotent_does_not_skip_when_pending(monkeypatch, nr):
    row = _trade("AAPL", "2026-05-10T13:30:00", "2026-05-10T19:55:00",
                 pnl=-3.0, pnl_pct=-0.5)
    monkeypatch.setattr(nr, "_query_closed_trades", lambda **kw: [row])
    _patch_no_yf(monkeypatch, nr, return_pct=0.5)
    monkeypatch.setattr(
        nr, "_decisions_text",
        lambda: "- [pending] 2026-05-10 AAPL — open thesis\n",
    )
    # Use the default llm_text=None so the reflection cites the actual
    # computed alpha (-1.0%) and passes the post-LLM regex check.
    written = _patch_siblings(monkeypatch, nr, llm_text=None)

    rc = nr.main()
    assert rc == 0
    assert len(written) == 1
    assert written[0]["alpha_pct"] == pytest.approx(-1.0)


# --- 4. Alpha-vs-benchmark math (mocked yfinance) --------------------------

def test_alpha_vs_benchmark_uses_yfinance(monkeypatch, nr):
    """The alpha figure passed to update_with_outcome must equal
    pnl_pct - benchmark_pct."""
    row = _trade("ETH/USD", "2026-05-10T10:00:00", "2026-05-10T17:00:00",
                 pnl=22.0, pnl_pct=3.3)
    monkeypatch.setattr(nr, "_query_closed_trades", lambda **kw: [row])
    _patch_decisions_empty(monkeypatch, nr)

    # Capture the call so we can assert on the inputs.
    seen = {}

    def fake_bench(benchmark, opened_at, closed_at):
        seen["benchmark"] = benchmark
        seen["opened_at"] = opened_at
        seen["closed_at"] = closed_at
        return 1.1  # benchmark moved +1.1% over the holding window

    monkeypatch.setattr(nr, "_benchmark_return_pct", fake_bench)
    written = _patch_siblings(monkeypatch, nr, llm_text=None)

    rc = nr.main()
    assert rc == 0
    assert seen["benchmark"] == "BTC/USD"  # crypto pair → BTC benchmark
    assert len(written) == 1
    assert written[0]["alpha_pct"] == pytest.approx(3.3 - 1.1)
    assert written[0]["holding_days"] == 0


def test_stock_uses_spy_benchmark(monkeypatch, nr):
    row = _trade("AAPL", "2026-05-10T13:30:00", "2026-05-10T19:55:00",
                 pnl=5.0, pnl_pct=1.0)
    monkeypatch.setattr(nr, "_query_closed_trades", lambda **kw: [row])
    _patch_decisions_empty(monkeypatch, nr)

    seen = {}

    def fake_bench(benchmark, opened_at, closed_at):
        seen["benchmark"] = benchmark
        return 0.4

    monkeypatch.setattr(nr, "_benchmark_return_pct", fake_bench)
    written = _patch_siblings(monkeypatch, nr, llm_text=None)
    rc = nr.main()
    assert rc == 0
    assert seen["benchmark"] == "SPY"
    assert written[0]["alpha_pct"] == pytest.approx(1.0 - 0.4)


# --- 5. Ollama unreachable → log + exit 0 ----------------------------------

def test_ollama_unreachable_exits_zero(monkeypatch, nr, caplog):
    """If chat_structured raises (e.g. ConnectionError to Ollama), the
    cron must NOT crash — it logs the error, marks the trade as errored,
    and exits 0."""
    row = _trade("BTC/USD", "2026-05-10T14:00:00", "2026-05-10T20:00:00",
                 pnl=15.0, pnl_pct=1.5)
    monkeypatch.setattr(nr, "_query_closed_trades", lambda **kw: [row])
    _patch_no_yf(monkeypatch, nr, return_pct=0.5)
    _patch_decisions_empty(monkeypatch, nr)
    written = _patch_siblings(
        monkeypatch, nr,
        llm_text=ConnectionError("ollama: connection refused"),
    )

    rc = nr.main()
    assert rc == 0
    assert written == []  # Nothing written because LLM never returned


def test_sibling_import_missing_exits_zero(monkeypatch, nr, caplog):
    """If shark.llm.structured can't be imported (sibling branch not
    merged yet), the cron logs and exits 0 without crashing."""
    monkeypatch.setattr(
        nr, "_import_chat_structured",
        lambda: (_ for _ in ()).throw(
            ImportError("structured.py not in branch")),
    )
    monkeypatch.setattr(nr, "_query_closed_trades", lambda **kw: [])

    rc = nr.main()
    assert rc == 0


# --- Misc --- helper-level coverage ----------------------------------------

def test_alpha_present_in_text_matches_signed_pct(nr):
    assert nr._alpha_present_in_text("alpha was +1.0% over the bench", 1.0)
    assert nr._alpha_present_in_text("alpha was -3.5% vs SPY", -3.5)
    assert not nr._alpha_present_in_text("we beat the bench by a hair", 2.0)
    # Rounded to 1dp — accept text within rounding tolerance.
    assert nr._alpha_present_in_text("alpha +2.2%", 2.16)


def test_is_crypto_classification(nr):
    assert nr._is_crypto("BTC/USD")
    assert nr._is_crypto("ETH/USDT")
    assert not nr._is_crypto("AAPL")
    assert not nr._is_crypto("TSLA")
