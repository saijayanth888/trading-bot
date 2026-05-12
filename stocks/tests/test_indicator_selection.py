"""Tests for the LLM-driven indicator selector.

Run from `stocks/`::

    pytest tests/test_indicator_selection.py -v

Covers:
  - parser truncates >8 picks down to MAX_PICKS=8
  - parser de-duplicates repeat indicators
  - parser drops unknown indicators
  - schema validation rejects unknown ids when constructed directly
  - mocked LLM is consulted on first call
  - cache hit avoids the LLM on second call (same key)
  - regime change for same ticker invalidates cache (different key → re-query)
  - graceful default fallback when LLM raises
  - summarize_bars accepts dict-of-dicts and Alpaca-style keys
  - thin adapter returns ids in pick order
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
from pydantic import ValidationError

from shark.agents import market_analyst as ma
from shark.agents.market_analyst import (
    MAX_PICKS,
    IndicatorPick,
    IndicatorSelection,
    parse_selection,
    select_indicators,
    summarize_bars,
)
from shark.data import indicator_selection as adapter


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def isolated_kb(tmp_path, monkeypatch):
    """Redirect kb/indicator_selection writes to a tmp dir per test."""
    monkeypatch.setenv("SHARK_KB_DIR", str(tmp_path / "kb"))
    yield


def _bars(n: int = 25) -> list[dict[str, Any]]:
    """Generate plausible OHLCV bars for the prompt builder."""
    out = []
    price = 100.0
    for i in range(n):
        price *= 1.005 if i % 2 == 0 else 0.998
        out.append(
            {
                "o": price * 0.999,
                "h": price * 1.01,
                "l": price * 0.99,
                "c": price,
                "v": 1_000_000 + i * 1000,
            }
        )
    return out


def _llm_returning(payload: dict[str, Any] | str) -> Any:
    """Build a chat_json mock whose return value is the JSON encoding of
    `payload` (or `payload` itself if already a string)."""
    body = payload if isinstance(payload, str) else json.dumps(payload)
    fake = MagicMock(return_value=(body, {"input_tokens": 10, "output_tokens": 20}, "hermes3:8b"))
    return fake


# ---------------------------------------------------------------------------
# Parser / schema tests
# ---------------------------------------------------------------------------


class TestParser:
    def test_truncates_more_than_max_picks(self):
        # Build 11 distinct valid picks; parser must cap at 8.
        ids = [
            "close_50_sma", "close_200_sma", "close_10_ema", "close_20_ema",
            "macd", "macd_signal", "macd_hist", "rsi",
            "boll", "boll_ub", "boll_lb",  # 11 total
        ]
        payload = {"picks": [{"indicator": i, "why": "x"} for i in ids]}
        sel = parse_selection(json.dumps(payload), "NVDA", "trending_up")
        assert len(sel.picks) == MAX_PICKS == 8
        # First 8 (in order) preserved
        assert [p.indicator for p in sel.picks] == ids[:8]

    def test_dedupes_repeat_indicators(self):
        payload = {
            "picks": [
                {"indicator": "rsi", "why": "first"},
                {"indicator": "rsi", "why": "duplicate"},
                {"indicator": "macd", "why": "ok"},
            ]
        }
        sel = parse_selection(json.dumps(payload), "NVDA", "mean_reverting")
        assert [p.indicator for p in sel.picks] == ["rsi", "macd"]

    def test_drops_unknown_indicators(self):
        payload = {
            "picks": [
                {"indicator": "rsi", "why": "ok"},
                {"indicator": "stochrsi", "why": "not in menu"},
                {"indicator": "made_up", "why": "nope"},
                {"indicator": "atr", "why": "ok"},
            ]
        }
        sel = parse_selection(json.dumps(payload), "NVDA", "trending_up")
        assert [p.indicator for p in sel.picks] == ["rsi", "atr"]

    def test_handles_code_fences_and_prose(self):
        body = (
            "Sure! Here is the selection:\n"
            "```json\n"
            '{"picks": [{"indicator": "rsi", "why": "ok"}]}\n'
            "```\n"
            "Hope that helps."
        )
        sel = parse_selection(body, "NVDA", "trending_up")
        assert len(sel.picks) == 1
        assert sel.picks[0].indicator == "rsi"

    def test_handles_bare_list_output(self):
        body = '[{"indicator": "rsi", "why": "ok"}, {"indicator": "atr", "why": "ok"}]'
        sel = parse_selection(body, "NVDA", "trending_up")
        assert [p.indicator for p in sel.picks] == ["rsi", "atr"]

    def test_invalid_json_returns_empty_selection(self):
        sel = parse_selection("totally not json", "NVDA", "trending_up")
        assert sel.picks == []
        assert sel.ticker == "NVDA"

    def test_schema_rejects_unknown_indicator_directly(self):
        with pytest.raises(ValidationError):
            IndicatorPick(indicator="not_a_real_indicator", why="x")

    def test_indicator_selection_truncates_oversized_picks(self):
        # Direct construction with too many picks — validator truncates.
        ids = [
            "close_50_sma", "close_200_sma", "close_10_ema", "close_20_ema",
            "macd", "macd_signal", "macd_hist", "rsi", "boll",
        ]
        sel = IndicatorSelection(
            ticker="NVDA",
            regime="trending_up",
            picks=[IndicatorPick(indicator=i, why="x") for i in ids],
        )
        assert len(sel.picks) == MAX_PICKS


# ---------------------------------------------------------------------------
# OHLCV summary
# ---------------------------------------------------------------------------


class TestSummarizeBars:
    def test_alpaca_style_keys(self):
        bars = [{"open": 1, "high": 2, "low": 0.5, "close": 1.5, "volume": 100}] * 10
        summary = summarize_bars(bars, lookback=10)
        assert summary["bars_observed"] == 10
        assert summary["current_close"] == 1.5

    def test_short_keys(self):
        summary = summarize_bars(_bars(20), lookback=20)
        assert summary["bars_observed"] == 20
        assert summary["current_close"] > 0

    def test_empty_input(self):
        assert summarize_bars([]) == {}

    def test_lookback_caps_window(self):
        summary = summarize_bars(_bars(50), lookback=10)
        assert summary["bars_observed"] == 10


# ---------------------------------------------------------------------------
# select_indicators end-to-end (mocked LLM)
# ---------------------------------------------------------------------------


class TestSelectIndicators:
    def test_llm_called_on_first_invocation(self):
        fake_llm = _llm_returning({
            "picks": [
                {"indicator": "rsi", "why": "MR setup"},
                {"indicator": "boll", "why": "range anchor"},
                {"indicator": "atr", "why": "stop sizing"},
            ]
        })
        sel = select_indicators(
            ticker="NVDA",
            regime="mean_reverting",
            bars=_bars(),
            chat_json_fn=fake_llm,
        )
        assert fake_llm.call_count == 1
        assert [p.indicator for p in sel.picks] == ["rsi", "boll", "atr"]

    def test_cache_hit_skips_llm_on_second_call(self):
        fake_llm = _llm_returning({
            "picks": [{"indicator": "rsi", "why": "first call"}]
        })
        # First call — LLM consulted, written to cache.
        sel1 = select_indicators(
            ticker="NVDA",
            regime="trending_up",
            bars=_bars(),
            chat_json_fn=fake_llm,
            on_date=date(2026, 5, 11),
        )
        assert fake_llm.call_count == 1
        # Second call — same key — must NOT consult LLM.
        sel2 = select_indicators(
            ticker="NVDA",
            regime="trending_up",
            bars=_bars(),
            chat_json_fn=fake_llm,
            on_date=date(2026, 5, 11),
        )
        assert fake_llm.call_count == 1, "cache hit expected, LLM should not be re-called"
        assert sel2.model_dump() == sel1.model_dump()

    def test_regime_change_invalidates_cache_for_same_ticker(self):
        # Two different LLM responses — one per regime.
        bullish = _llm_returning({
            "picks": [
                {"indicator": "close_50_sma", "why": "trend"},
                {"indicator": "macd", "why": "momentum"},
            ]
        })
        bearish = _llm_returning({
            "picks": [
                {"indicator": "boll_lb", "why": "vol bottom"},
                {"indicator": "atr", "why": "wide stops"},
            ]
        })
        sel_up = select_indicators(
            ticker="NVDA", regime="trending_up", bars=_bars(),
            chat_json_fn=bullish, on_date=date(2026, 5, 11),
        )
        sel_bear = select_indicators(
            ticker="NVDA", regime="BEAR_VOLATILE", bars=_bars(),
            chat_json_fn=bearish, on_date=date(2026, 5, 11),
        )
        # Each regime gets its own LLM call (different cache key).
        assert bullish.call_count == 1
        assert bearish.call_count == 1
        assert {p.indicator for p in sel_up.picks} != {
            p.indicator for p in sel_bear.picks
        }
        # Re-query trending_up — must hit cache, NOT trigger bearish llm.
        sel_up_again = select_indicators(
            ticker="NVDA", regime="trending_up", bars=_bars(),
            chat_json_fn=bullish, on_date=date(2026, 5, 11),
        )
        assert bullish.call_count == 1, "should be a cache hit"
        assert sel_up_again.model_dump() == sel_up.model_dump()

    def test_use_cache_false_always_consults_llm(self):
        fake_llm = _llm_returning({"picks": [{"indicator": "rsi", "why": "x"}]})
        select_indicators(
            "NVDA", "trending_up", bars=_bars(),
            chat_json_fn=fake_llm, use_cache=False,
        )
        select_indicators(
            "NVDA", "trending_up", bars=_bars(),
            chat_json_fn=fake_llm, use_cache=False,
        )
        assert fake_llm.call_count == 2

    def test_llm_failure_falls_back_to_defaults(self):
        def boom(**_kw):
            raise RuntimeError("ollama down, anthropic missing")
        sel = select_indicators(
            "NVDA", "BEAR_VOLATILE", bars=_bars(),
            chat_json_fn=boom, on_date=date(2026, 5, 11),
        )
        assert sel.picks, "fallback should still produce picks"
        # Bear-volatile defaults must include atr (stop sizing in vol).
        assert "atr" in {p.indicator for p in sel.picks}

    def test_empty_llm_response_falls_back_to_defaults(self):
        fake_llm = _llm_returning({"picks": []})
        sel = select_indicators(
            "NVDA", "trending_up", bars=_bars(),
            chat_json_fn=fake_llm, on_date=date(2026, 5, 11),
        )
        assert len(sel.picks) > 0, "empty LLM picks must trigger defaults"

    def test_cache_file_written_to_kb(self, tmp_path):
        fake_llm = _llm_returning({"picks": [{"indicator": "rsi", "why": "x"}]})
        select_indicators(
            "NVDA", "trending_up", bars=_bars(),
            chat_json_fn=fake_llm, on_date=date(2026, 5, 11),
        )
        kb_root = Path(tmp_path) / "kb" / "indicator_selection"
        files = list(kb_root.glob("NVDA_TRENDING_UP_2026-05-11.json"))
        assert files, f"expected cache file under {kb_root}"
        cached = json.loads(files[0].read_text())
        assert cached["ticker"] == "NVDA"
        assert cached["regime"] == "trending_up"


# ---------------------------------------------------------------------------
# Adapter
# ---------------------------------------------------------------------------


class TestAdapter:
    def test_indicators_for_pair_returns_id_list(self):
        fake_llm = _llm_returning({
            "picks": [
                {"indicator": "rsi", "why": "x"},
                {"indicator": "atr", "why": "x"},
            ]
        })
        ids = adapter.indicators_for_pair(
            "NVDA", "trending_up", bars=_bars(),
            chat_json_fn=fake_llm, on_date=date(2026, 5, 11),
        )
        assert ids == ["rsi", "atr"]

    def test_picks_as_dict_flattens(self):
        sel = IndicatorSelection(
            ticker="NVDA", regime="trending_up",
            picks=[
                IndicatorPick(indicator="rsi", why="momentum"),
                IndicatorPick(indicator="atr", why="stops"),
            ],
        )
        d = adapter.picks_as_dict(sel)
        assert d == {"rsi": "momentum", "atr": "stops"}
