"""
Tests for `shark.schemas` and the `chat_structured` helper.

Run:
    pytest stocks/shark/tests/test_schemas.py -v

Covers:
  - Each schema accepts a representative valid example.
  - Each schema rejects out-of-range, missing, or wrong-literal inputs.
  - String-length caps reject too-long fields (we chose `max_length`
    semantics rather than silent truncation, so callers see the bug).
  - `chat_structured` retries on bad JSON and bad validation, then
    raises `StructuredOutputError` once `max_retries` is exhausted.
"""

from __future__ import annotations

import json
import sys
from datetime import date
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from pydantic import ValidationError

# Make `import shark.*` resolve when pytest is invoked from the repo root.
_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))


from shark.llm.structured import (  # noqa: E402
    StructuredOutputError,
    chat_structured,
)
from shark.schemas import (  # noqa: E402
    OutcomeLabel,
    RegimeTag,
    TraderProposal,
    WheelDecision,
)

# ---------------------------------------------------------------------------
# RegimeTag
# ---------------------------------------------------------------------------


class TestRegimeTag:
    def test_accepts_valid(self) -> None:
        tag = RegimeTag(
            ticker="BTC/USD",
            regime="trending_up",
            confidence=0.82,
            narrative="20MA crossed above 50MA on rising volume.",
        )
        assert tag.regime == "trending_up"
        assert tag.confidence == 0.82

    def test_rejects_unknown_regime(self) -> None:
        with pytest.raises(ValidationError):
            RegimeTag(
                ticker="BTC/USD",
                regime="moonshot",  # type: ignore[arg-type]
                confidence=0.5,
                narrative="x",
            )

    def test_rejects_confidence_out_of_range(self) -> None:
        with pytest.raises(ValidationError):
            RegimeTag(
                ticker="ETH/USD", regime="unknown",
                confidence=1.5, narrative="x",
            )
        with pytest.raises(ValidationError):
            RegimeTag(
                ticker="ETH/USD", regime="unknown",
                confidence=-0.1, narrative="x",
            )

    def test_rejects_missing_required(self) -> None:
        with pytest.raises(ValidationError):
            # `narrative` is required.
            RegimeTag(ticker="SOL/USD", regime="unknown", confidence=0.5)  # type: ignore[call-arg]

    def test_narrative_length_cap(self) -> None:
        with pytest.raises(ValidationError):
            RegimeTag(
                ticker="BTC/USD",
                regime="unknown",
                confidence=0.1,
                narrative="x" * 281,
            )


# ---------------------------------------------------------------------------
# TraderProposal
# ---------------------------------------------------------------------------


class TestTraderProposal:
    def test_accepts_valid_full(self) -> None:
        prop = TraderProposal(
            ticker="nvda",
            action="BUY",
            conviction=0.78,
            thesis="Strong AI tailwind; RSI 58 in optimal zone.",
            entry_price=125.0,
            stop_loss=115.0,
            target=145.0,
            position_sizing_pct=0.10,
            invalidation="Closes below 200-day SMA.",
        )
        assert prop.ticker == "NVDA"  # auto-uppercased
        assert prop.action == "BUY"

    def test_accepts_minimal(self) -> None:
        prop = TraderProposal(
            ticker="AAPL",
            action="HOLD",
            conviction=0.4,
            thesis="Mixed signals; awaiting earnings.",
            invalidation="Earnings beat by >10%.",
        )
        assert prop.entry_price is None

    def test_rejects_bad_action(self) -> None:
        with pytest.raises(ValidationError):
            TraderProposal(
                ticker="AAPL",
                action="MAYBE",  # type: ignore[arg-type]
                conviction=0.5,
                thesis="x",
                invalidation="x",
            )

    def test_rejects_negative_price(self) -> None:
        with pytest.raises(ValidationError):
            TraderProposal(
                ticker="AAPL",
                action="BUY",
                conviction=0.7,
                thesis="x",
                invalidation="x",
                entry_price=-1.0,
            )

    def test_rejects_position_sizing_above_one(self) -> None:
        with pytest.raises(ValidationError):
            TraderProposal(
                ticker="AAPL",
                action="BUY",
                conviction=0.7,
                thesis="x",
                invalidation="x",
                position_sizing_pct=1.5,
            )

    def test_thesis_length_cap(self) -> None:
        with pytest.raises(ValidationError):
            TraderProposal(
                ticker="AAPL",
                action="BUY",
                conviction=0.7,
                thesis="x" * 501,
                invalidation="x",
            )


# ---------------------------------------------------------------------------
# WheelDecision
# ---------------------------------------------------------------------------


class TestWheelDecision:
    def test_accepts_csp(self) -> None:
        d = WheelDecision(
            underlying="sofi",
            kind="CSP",
            strike=8.0,
            expiry=date(2026, 6, 20),
            premium_target=0.40,
            rationale="Delta -0.30, IV rank 45, 38 DTE.",
        )
        assert d.underlying == "SOFI"
        assert d.kind == "CSP"

    def test_accepts_skip_minimal(self) -> None:
        d = WheelDecision(
            underlying="HOOD",
            kind="SKIP",
            rationale="IV crushed below 20 — premium not worth it.",
        )
        assert d.strike is None
        assert d.expiry is None

    def test_rejects_bad_kind(self) -> None:
        with pytest.raises(ValidationError):
            WheelDecision(
                underlying="HOOD",
                kind="STRADDLE",  # type: ignore[arg-type]
                rationale="x",
            )

    def test_rejects_negative_strike(self) -> None:
        with pytest.raises(ValidationError):
            WheelDecision(
                underlying="HOOD",
                kind="CSP",
                strike=-1.0,
                rationale="x",
            )


# ---------------------------------------------------------------------------
# OutcomeLabel
# ---------------------------------------------------------------------------


class TestOutcomeLabel:
    def test_accepts_valid(self) -> None:
        ol = OutcomeLabel(
            trade_id="NVDA_2026-04-15",
            label="tft_correct",
            confidence=0.9,
            reason="TFT predicted +3% over 5d; actual +3.4%.",
        )
        assert ol.label == "tft_correct"

    def test_rejects_unknown_label(self) -> None:
        with pytest.raises(ValidationError):
            OutcomeLabel(
                trade_id="x",
                label="vibes_were_off",  # type: ignore[arg-type]
                confidence=0.5,
                reason="x",
            )

    def test_rejects_missing_required(self) -> None:
        with pytest.raises(ValidationError):
            OutcomeLabel(
                trade_id="x", label="exec_failed", confidence=0.5,  # type: ignore[call-arg]
            )

    def test_reason_length_cap(self) -> None:
        with pytest.raises(ValidationError):
            OutcomeLabel(
                trade_id="x",
                label="exec_failed",
                confidence=0.5,
                reason="x" * 201,
            )


# ---------------------------------------------------------------------------
# chat_structured retry behaviour (mocked LLM backend)
# ---------------------------------------------------------------------------


class _FakeBackend:
    """Captures call count and returns a scripted sequence of payloads."""

    def __init__(self, responses: list[Any]) -> None:
        self.responses = list(responses)
        self.calls = 0
        self.last_user = ""

    def __call__(
        self,
        system: str,
        user: str,
        *,
        model: str,
        max_tokens: int,
        temperature: float,
        base_url: Any = None,
        timeout: Any = None,
    ) -> str:
        self.calls += 1
        self.last_user = user
        if not self.responses:
            raise RuntimeError("FakeBackend ran out of scripted responses")
        nxt = self.responses.pop(0)
        if isinstance(nxt, Exception):
            raise nxt
        return nxt


class TestChatStructured:
    """All tests use Ollama path with `_call_ollama_json` patched."""

    _GOOD_REGIME = json.dumps({
        "ticker": "BTC/USD",
        "regime": "trending_up",
        "confidence": 0.7,
        "narrative": "MA cross + rising volume.",
    })

    def test_returns_validated_model_first_try(self) -> None:
        backend = _FakeBackend([self._GOOD_REGIME])
        with patch(
            "shark.llm.structured._call_ollama_json", side_effect=backend,
        ):
            result = chat_structured(
                provider="ollama",
                tier="fast",
                system="sys",
                user="user",
                schema=RegimeTag,
                max_retries=2,
            )
        assert isinstance(result, RegimeTag)
        assert result.regime == "trending_up"
        assert backend.calls == 1

    def test_retries_on_bad_json_then_succeeds(self) -> None:
        backend = _FakeBackend([
            "not actually json {{{",
            self._GOOD_REGIME,
        ])
        with patch(
            "shark.llm.structured._call_ollama_json", side_effect=backend,
        ):
            result = chat_structured(
                provider="ollama",
                tier="fast",
                system="sys",
                user="user",
                schema=RegimeTag,
                max_retries=2,
            )
        assert isinstance(result, RegimeTag)
        assert backend.calls == 2
        # Retry prompt should reference the previous failure.
        assert "previous response failed validation" in backend.last_user

    def test_retries_on_validation_error_then_succeeds(self) -> None:
        bad_payload = json.dumps({
            "ticker": "BTC/USD",
            "regime": "moonshot",  # not in Literal
            "confidence": 0.7,
            "narrative": "x",
        })
        backend = _FakeBackend([bad_payload, self._GOOD_REGIME])
        with patch(
            "shark.llm.structured._call_ollama_json", side_effect=backend,
        ):
            result = chat_structured(
                provider="ollama",
                tier="fast",
                system="sys",
                user="user",
                schema=RegimeTag,
                max_retries=3,
            )
        assert isinstance(result, RegimeTag)
        assert backend.calls == 2

    def test_raises_after_max_retries(self) -> None:
        backend = _FakeBackend([
            "garbage 1",
            "garbage 2",
            "garbage 3",
        ])
        with patch(
            "shark.llm.structured._call_ollama_json", side_effect=backend,
        ):
            with pytest.raises(StructuredOutputError) as exc_info:
                chat_structured(
                    provider="ollama",
                    tier="fast",
                    system="sys",
                    user="user",
                    schema=RegimeTag,
                    max_retries=2,
                )
        # 1 initial + 2 retries == 3 attempts.
        assert backend.calls == 3
        assert exc_info.value.attempts == 3
        assert exc_info.value.schema_name == "RegimeTag"
        assert exc_info.value.last_raw == "garbage 3"

    def test_backend_exception_counts_as_attempt(self) -> None:
        """Network / API errors burn an attempt — keep in mind for budgets."""
        backend = _FakeBackend([
            RuntimeError("connection refused"),
            self._GOOD_REGIME,
        ])
        with patch(
            "shark.llm.structured._call_ollama_json", side_effect=backend,
        ):
            result = chat_structured(
                provider="ollama",
                tier="fast",
                system="sys",
                user="user",
                schema=RegimeTag,
                max_retries=2,
            )
        assert isinstance(result, RegimeTag)
        assert backend.calls == 2
