"""Tests for chat_json failover (Ollama → Anthropic).

Run from stocks/:
    pytest tests/test_chat_json_failover.py -v
"""

from __future__ import annotations

from unittest.mock import patch, MagicMock

import pytest

from shark.llm import circuit_breaker as cb_module
from shark.llm import client as client_module


@pytest.fixture(autouse=True)
def isolated_breaker_state(tmp_path, monkeypatch):
    """Each test gets a fresh state dir so breaker state doesn't leak.
    Also redirects the LLM tracker log so test calls don't pollute the
    production dashboard's LLM-stats card."""
    monkeypatch.setattr(cb_module, "_STATE_DIR", tmp_path)
    monkeypatch.setattr(cb_module, "_breakers", {})
    # Reset the rate-limited fallback alert so each test can fire it
    monkeypatch.setattr(client_module, "_FALLBACK_ALERT_LAST_TS", 0.0)
    # Redirect tracker JSONL to tmp_path so prod log stays clean
    from shark.llm import tracker as tracker_module
    monkeypatch.setattr(tracker_module, "_LOG_PATH", tmp_path / "test-llm.jsonl")
    monkeypatch.setattr(tracker_module, "_singleton", None)
    yield


def _mock_response(content: str, model: str, in_toks: int = 10, out_toks: int = 5):
    return MagicMock(
        content=content, model=model,
        usage={"input_tokens": in_toks, "output_tokens": out_toks},
    )


# ---------------------------------------------------------------------------
# Happy path — Ollama works, no failover
# ---------------------------------------------------------------------------


class TestHappyPath:
    @patch("shark.llm.client.get_llm_client")
    def test_uses_ollama_when_healthy(self, mock_get_client, monkeypatch):
        monkeypatch.setenv("SHARK_LLM_PROVIDER", "ollama")
        mock_get_client.return_value.chat.return_value = _mock_response(
            "OK", "hermes3:8b"
        )
        mock_get_client.return_value.provider_name = "ollama"
        mock_get_client.return_value.model = "hermes3:8b"

        text, _, model = client_module.chat_json(
            "sys", "user", tier="fast", agent="happy_test", max_tokens=10,
        )
        assert text == "OK"
        # First positional/kwarg call should have provider="ollama"
        first_call = mock_get_client.call_args_list[0]
        assert first_call.kwargs.get("provider") == "ollama"


# ---------------------------------------------------------------------------
# Failover path — Ollama fails, Anthropic picks up
# ---------------------------------------------------------------------------


class TestFailoverToAnthropic:
    @patch("shark.llm.client.get_llm_client")
    def test_falls_over_when_ollama_raises(self, mock_get_client, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-fake-test")
        # Don't pin SHARK_LLM_PROVIDER=anthropic — that would skip the failover dance
        monkeypatch.delenv("SHARK_LLM_PROVIDER", raising=False)

        ollama_client = MagicMock(provider_name="ollama", model="hermes3:8b")
        ollama_client.chat.side_effect = ConnectionError("Ollama down")

        anthropic_client = MagicMock(
            provider_name="anthropic", model="claude-sonnet-4-6",
        )
        anthropic_client.chat.return_value = _mock_response(
            "fallback_ok", "claude-sonnet-4-6",
        )

        def selector(provider=None, model=None, **kw):
            return ollama_client if provider == "ollama" else anthropic_client

        mock_get_client.side_effect = selector

        text, _, model = client_module.chat_json(
            "sys", "user", tier="fast", agent="failover_test",
        )
        assert text == "fallback_ok"
        assert "claude" in model.lower()

    @patch("shark.llm.client.get_llm_client")
    def test_failover_records_failure_on_breaker(self, mock_get_client, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-fake-test")
        monkeypatch.delenv("SHARK_LLM_PROVIDER", raising=False)

        ollama_client = MagicMock(provider_name="ollama", model="hermes3:8b")
        ollama_client.chat.side_effect = ConnectionError("down")
        anthropic_client = MagicMock(
            provider_name="anthropic", model="claude-sonnet-4-6",
        )
        anthropic_client.chat.return_value = _mock_response("ok", "claude-sonnet-4-6")

        mock_get_client.side_effect = lambda provider=None, **kw: (
            ollama_client if provider == "ollama" else anthropic_client
        )

        client_module.chat_json("sys", "user", tier="fast", agent="t")

        # Ollama breaker should have a recorded failure
        ollama_breaker = cb_module.get_breaker("ollama:fast", tier="fast")
        assert ollama_breaker.get_status()["failure_count"] >= 1


# ---------------------------------------------------------------------------
# Both providers fail — no silent failure
# ---------------------------------------------------------------------------


class TestBothProvidersFail:
    @patch("shark.llm.client.get_llm_client")
    def test_raises_runtime_error_when_no_anthropic_key(
        self, mock_get_client, monkeypatch,
    ):
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.delenv("SHARK_LLM_PROVIDER", raising=False)

        ollama_client = MagicMock(provider_name="ollama", model="hermes3:8b")
        ollama_client.chat.side_effect = ConnectionError("Ollama down")
        mock_get_client.return_value = ollama_client

        with pytest.raises((ConnectionError, RuntimeError)) as exc_info:
            client_module.chat_json("sys", "user", tier="fast", agent="t")
        # Error message should mention Ollama unavailable + no fallback
        assert "ollama" in str(exc_info.value).lower() or "anthropic" in str(exc_info.value).lower()

    @patch("shark.llm.client.get_llm_client")
    def test_raises_runtime_error_when_anthropic_also_fails(
        self, mock_get_client, monkeypatch,
    ):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-fake")
        monkeypatch.delenv("SHARK_LLM_PROVIDER", raising=False)

        ollama_client = MagicMock(provider_name="ollama", model="hermes3:8b")
        ollama_client.chat.side_effect = ConnectionError("ollama refused")
        anthropic_client = MagicMock(
            provider_name="anthropic", model="claude-sonnet-4-6",
        )
        anthropic_client.chat.side_effect = RuntimeError("anthropic 503")

        mock_get_client.side_effect = lambda provider=None, **kw: (
            ollama_client if provider == "ollama" else anthropic_client
        )

        with pytest.raises(RuntimeError) as exc_info:
            client_module.chat_json("sys", "user", tier="fast", agent="t")
        assert "BOTH PROVIDERS" in str(exc_info.value)


# ---------------------------------------------------------------------------
# Pinned provider — bypasses failover dance
# ---------------------------------------------------------------------------


class TestPinnedProvider:
    @patch("shark.llm.client.get_llm_client")
    def test_pinned_anthropic_skips_ollama(self, mock_get_client, monkeypatch):
        monkeypatch.setenv("SHARK_LLM_PROVIDER", "anthropic")
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-fake")

        anthropic_client = MagicMock(
            provider_name="anthropic", model="claude-sonnet-4-6",
        )
        anthropic_client.chat.return_value = _mock_response("pinned", "claude-sonnet-4-6")
        mock_get_client.return_value = anthropic_client

        text, _, model = client_module.chat_json(
            "sys", "user", tier="deep", agent="pinned_test",
        )
        assert text == "pinned"
        # Should never have asked for "ollama"
        for call in mock_get_client.call_args_list:
            assert call.kwargs.get("provider") != "ollama"
