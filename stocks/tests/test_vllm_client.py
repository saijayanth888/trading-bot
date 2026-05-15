"""Tests for the vLLM client + role-based router.

Run from stocks/:
    pytest tests/test_vllm_client.py -v

Covers:
  - Request body shape (model=adapter, extra_body.adapter_name,
    response_format passes through).
  - OpenAI chat-completions response parsing.
  - 5xx / connection error → VLLMUnavailableError (caller can fall back).
  - chat_by_role falls back to Ollama transparently when vLLM is down.
  - resolve_role_route honours env override + json file.
  - Two consecutive calls to different adapters route correctly (the
    "hot-swap" guarantee).
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def isolate_state(tmp_path, monkeypatch):
    """Per-test isolation: fresh circuit breakers + tracker dir + routing
    cache, and a known vLLM base URL so the assertions don't have to
    care what the operator has set in .env."""
    from shark.llm import circuit_breaker as cb_module
    from shark.llm import client as client_module
    from shark.llm import tracker as tracker_module

    monkeypatch.setattr(cb_module, "_STATE_DIR", tmp_path)
    monkeypatch.setattr(cb_module, "_breakers", {})
    monkeypatch.setattr(client_module, "_FALLBACK_ALERT_LAST_TS", 0.0)
    monkeypatch.setattr(tracker_module, "_LOG_PATH", tmp_path / "test-llm.jsonl")
    monkeypatch.setattr(tracker_module, "_singleton", None)
    client_module._reset_routing_cache()
    monkeypatch.setenv("VLLM_BASE_URL", "http://test-vllm:8090")
    # Drop env overrides that would otherwise leak between tests.
    for key in [
        "SHARK_ROLE_TRADING_BULL_BACKEND",
        "SHARK_ROLE_TRADING_BULL_MODEL",
        "SHARK_ROLE_TRADING_BULL_ADAPTER",
        "SHARK_ROLE_TRADING_REFLECTOR_BACKEND",
        "SHARK_LLM_PROVIDER",
    ]:
        monkeypatch.delenv(key, raising=False)
    yield


def _ok_response(content: str = "ok", model: str = "qwen3:30b"):
    """Build a MagicMock that mimics a successful httpx/requests Response."""
    resp = MagicMock()
    resp.status_code = 200
    resp.ok = True
    resp.json.return_value = {
        "id": "chatcmpl-test",
        "object": "chat.completion",
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "finish_reason": "stop",
            }
        ],
        "usage": {
            "prompt_tokens": 12,
            "completion_tokens": 7,
            "total_tokens": 19,
        },
    }
    return resp


def _error_response(status_code: int, body: str = "boom"):
    resp = MagicMock()
    resp.status_code = status_code
    resp.ok = status_code < 400
    resp.text = body
    return resp


# ---------------------------------------------------------------------------
# Request shape
# ---------------------------------------------------------------------------


class TestRequestShape:
    @patch("shark.llm.vllm_client.requests")
    def test_passes_adapter_in_both_model_and_extra_body(self, mock_requests):
        """vLLM 0.5+ selects the LoRA via the `model` field; older builds
        accepted `extra_body.adapter_name`. We pass both for forward
        compatibility."""
        mock_requests.post.return_value = _ok_response("bull says go", "bull")

        from shark.llm.vllm_client import VLLMClient

        client = VLLMClient(model="qwen3:30b", base_url="http://t:8090")
        resp = client.chat(
            "system", "is AAPL a buy?",
            max_tokens=64, temperature=0.3, adapter="bull",
        )

        assert resp.content == "bull says go"
        assert mock_requests.post.call_count == 1
        called = mock_requests.post.call_args
        url = called.args[0]
        body = called.kwargs["json"]

        assert url.endswith("/v1/chat/completions")
        # The OpenAI `model` field carries the adapter name — vLLM's
        # canonical selection mechanism.
        assert body["model"] == "bull"
        # Forward-compat field for older vLLM builds.
        assert body.get("extra_body", {}).get("adapter_name") == "bull"
        # Messages are passed through verbatim.
        assert body["messages"][0] == {"role": "system", "content": "system"}
        assert body["messages"][1] == {
            "role": "user", "content": "is AAPL a buy?",
        }

    @patch("shark.llm.vllm_client.requests")
    def test_no_adapter_uses_base_model(self, mock_requests):
        """When adapter is None/empty, the request addresses the base."""
        mock_requests.post.return_value = _ok_response("base says hi", "qwen3:30b")

        from shark.llm.vllm_client import VLLMClient

        client = VLLMClient(model="qwen3:30b", base_url="http://t:8090")
        client.chat("system", "hello")

        body = mock_requests.post.call_args.kwargs["json"]
        assert body["model"] == "qwen3:30b"
        assert "extra_body" not in body

    @patch("shark.llm.vllm_client.requests")
    def test_json_mode_sets_response_format(self, mock_requests):
        mock_requests.post.return_value = _ok_response('{"x": 1}')

        from shark.llm.vllm_client import VLLMClient

        client = VLLMClient(model="qwen3:30b", base_url="http://t:8090")
        client.chat("sys", "emit json", format="json", adapter="arbiter")

        body = mock_requests.post.call_args.kwargs["json"]
        assert body["response_format"] == {"type": "json_object"}


# ---------------------------------------------------------------------------
# Response parsing
# ---------------------------------------------------------------------------


class TestResponseParsing:
    @patch("shark.llm.vllm_client.requests")
    def test_extracts_content_and_usage(self, mock_requests):
        mock_requests.post.return_value = _ok_response(
            "hello world", "reflector",
        )

        from shark.llm.vllm_client import VLLMClient

        client = VLLMClient(model="qwen3:30b", base_url="http://t:8090")
        resp = client.chat("s", "u", adapter="reflector")

        assert resp.content == "hello world"
        assert resp.model == "reflector"  # adapter is echoed back, not base
        assert resp.usage == {"input_tokens": 12, "output_tokens": 7}


# ---------------------------------------------------------------------------
# Failure → VLLMUnavailableError so caller can fall back
# ---------------------------------------------------------------------------


class TestFailureSurface:
    @patch("shark.llm.vllm_client.requests")
    def test_5xx_raises_unavailable(self, mock_requests):
        mock_requests.post.return_value = _error_response(503, "overloaded")

        from shark.llm.vllm_client import VLLMClient, VLLMUnavailableError

        client = VLLMClient(model="qwen3:30b", base_url="http://t:8090")
        with pytest.raises(VLLMUnavailableError) as exc_info:
            client.chat("s", "u", adapter="bull")
        assert exc_info.value.status_code == 503

    @patch("shark.llm.vllm_client.requests")
    def test_connection_error_raises_unavailable(self, mock_requests):
        # Install real exception types so the `except` clause sees them.
        import requests as real_requests
        mock_requests.ConnectionError = real_requests.ConnectionError
        mock_requests.Timeout = real_requests.Timeout
        mock_requests.post.side_effect = real_requests.ConnectionError("refused")

        from shark.llm.vllm_client import VLLMClient, VLLMUnavailableError

        client = VLLMClient(model="qwen3:30b", base_url="http://t:8090")
        with pytest.raises(VLLMUnavailableError):
            client.chat("s", "u")

    @patch("shark.llm.vllm_client.requests")
    def test_4xx_other_than_429_is_programmer_error(self, mock_requests):
        """Bad adapter name → 400 — that's a bug, not an outage. Don't
        silently fall back; raise plain RuntimeError so the caller sees
        it in the logs."""
        mock_requests.post.return_value = _error_response(400, "unknown adapter")

        from shark.llm.vllm_client import VLLMClient, VLLMUnavailableError

        client = VLLMClient(model="qwen3:30b", base_url="http://t:8090")
        with pytest.raises(RuntimeError) as exc_info:
            client.chat("s", "u", adapter="not-a-real-adapter")
        # And specifically NOT VLLMUnavailableError — caller shouldn't
        # interpret this as "vLLM is down".
        assert not isinstance(exc_info.value, VLLMUnavailableError)


# ---------------------------------------------------------------------------
# Two consecutive calls to different adapters (the hot-swap guarantee)
# ---------------------------------------------------------------------------


class TestAdapterSwap:
    @patch("shark.llm.vllm_client.requests")
    def test_two_calls_route_to_different_adapters(self, mock_requests):
        # First call hits "bull", second hits "bear".
        mock_requests.post.side_effect = [
            _ok_response("buy", "bull"),
            _ok_response("sell", "bear"),
        ]

        from shark.llm.vllm_client import VLLMClient

        client = VLLMClient(model="qwen3:30b", base_url="http://t:8090")
        r1 = client.chat("s", "u", adapter="bull")
        r2 = client.chat("s", "u", adapter="bear")

        assert r1.content == "buy"
        assert r2.content == "sell"

        bodies = [c.kwargs["json"] for c in mock_requests.post.call_args_list]
        assert bodies[0]["model"] == "bull"
        assert bodies[1]["model"] == "bear"
        # Same base URL — no client recreation needed between calls.
        urls = [c.args[0] for c in mock_requests.post.call_args_list]
        assert urls[0] == urls[1]


# ---------------------------------------------------------------------------
# Runtime adapter registration
# ---------------------------------------------------------------------------


class TestRegisterAdapter:
    @patch("shark.llm.vllm_client.requests")
    def test_posts_to_load_lora_adapter(self, mock_requests):
        resp = MagicMock(status_code=200, ok=True)
        resp.json.return_value = {"status": "registered"}
        mock_requests.post.return_value = resp

        from shark.llm.vllm_client import register_adapter

        out = register_adapter(
            "reflector-2026-05-12", "/lora/reflector-2026-05-12",
            base_url="http://t:8090",
        )
        assert out == {"status": "registered"}
        called = mock_requests.post.call_args
        assert called.args[0].endswith("/v1/load_lora_adapter")
        assert called.kwargs["json"] == {
            "lora_name": "reflector-2026-05-12",
            "lora_path": "/lora/reflector-2026-05-12",
        }


# ---------------------------------------------------------------------------
# Role router — reads model_tiers.json
# ---------------------------------------------------------------------------


class TestResolveRoleRoute:
    def test_known_vllm_role(self):
        from shark.llm.client import resolve_role_route

        route = resolve_role_route("trading-bull")
        assert route["backend"] == "vllm"
        assert route["model"] == "qwen3:30b"
        assert route["adapter"] == "bull"

    def test_known_ollama_role(self):
        from shark.llm.client import resolve_role_route

        route = resolve_role_route("trading-regime-tagger")
        assert route["backend"] == "ollama"
        assert route["model"] == "hermes3:8b-trader"

    def test_unknown_role_defaults_to_ollama_8b(self):
        from shark.llm.client import resolve_role_route

        route = resolve_role_route("does-not-exist")
        assert route["backend"] == "ollama"
        # Just check the safe-default branch; model depends on env.
        assert route["adapter"] == ""

    def test_env_override_beats_json(self, monkeypatch):
        monkeypatch.setenv("SHARK_ROLE_TRADING_BULL_BACKEND", "ollama")
        monkeypatch.setenv("SHARK_ROLE_TRADING_BULL_MODEL", "llama3:70b")

        from shark.llm import client as client_module
        client_module._reset_routing_cache()
        route = client_module.resolve_role_route("trading-bull")

        assert route["backend"] == "ollama"
        assert route["model"] == "llama3:70b"


# ---------------------------------------------------------------------------
# chat_by_role — falls back to Ollama when vLLM is down
# ---------------------------------------------------------------------------


class TestChatByRoleFallback:
    @patch("shark.llm.client.chat_json")
    @patch("shark.llm.vllm_client.requests")
    def test_vllm_5xx_falls_back_to_ollama(
        self, mock_vllm_requests, mock_chat_json,
    ):
        """When vLLM returns 503, chat_by_role transparently falls back
        to Ollama with the base model and NO adapter."""
        mock_vllm_requests.post.return_value = _error_response(503, "overloaded")
        mock_chat_json.return_value = ("fallback content", {"input_tokens": 5,
                                                            "output_tokens": 3},
                                       "qwen3:30b")

        from shark.llm.client import chat_by_role

        content, usage, model = chat_by_role(
            "trading-bull", "sys", "user",
            max_tokens=64, temperature=0.3, agent="test",
        )
        assert content == "fallback content"
        assert "qwen3:30b" in model
        # chat_json was called as the fallback path.
        assert mock_chat_json.called

    @patch("shark.llm.vllm_client.requests")
    def test_vllm_success_does_not_call_ollama(self, mock_requests):
        """Happy path — when vLLM works, we don't touch Ollama at all."""
        mock_requests.post.return_value = _ok_response("vllm content", "bull")

        with patch("shark.llm.client.chat_json") as mock_chat_json:
            from shark.llm.client import chat_by_role

            content, _, model = chat_by_role(
                "trading-bull", "sys", "user",
                max_tokens=64, temperature=0.3, agent="test",
            )
            assert content == "vllm content"
            assert model == "bull"
            mock_chat_json.assert_not_called()

    @patch("shark.llm.client.chat_json")
    def test_ollama_role_routes_directly(self, mock_chat_json):
        """Ollama-routed roles never touch the vLLM client path."""
        mock_chat_json.return_value = ("regime: bull", {}, "hermes3:8b-trader")

        from shark.llm.client import chat_by_role

        content, _, model = chat_by_role(
            "trading-regime-tagger", "sys", "tag this",
            max_tokens=64, temperature=0.0, agent="test",
        )
        assert content == "regime: bull"
        assert model == "hermes3:8b-trader"
        assert mock_chat_json.call_count == 1
