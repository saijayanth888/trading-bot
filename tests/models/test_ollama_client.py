"""Tests for :mod:`quanta_core.models.ollama_client`.

Uses ``httpx.MockTransport`` instead of vcrpy because the runtime
environment doesn't ship vcrpy and the mock-transport surface gives us
the same assertion power (recorded request inspection + scripted
response sequences) without an HTTP-replay cassette file.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

import httpx
import pytest

from quanta_core.models.ollama_client import (
    OllamaClient,
    OllamaError,
    OllamaResponse,
)


def _no_sleep(_seconds: float) -> None:
    """Replacement for ``time.sleep`` in retry tests."""


def _json_response(status: int, body: dict[str, Any]) -> httpx.Response:
    return httpx.Response(
        status,
        content=json.dumps(body).encode(),
        headers={"content-type": "application/json"},
    )


def _make_transport(
    handler: Callable[[httpx.Request], httpx.Response],
) -> httpx.MockTransport:
    return httpx.MockTransport(handler)


def test_generate_sends_keep_alive_default() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return _json_response(200, {"model": "hermes3:8b", "response": "hello world"})

    transport = _make_transport(handler)
    client = OllamaClient(
        keep_alive_default="12h",
        transport=transport,
        sleep=_no_sleep,
    )
    try:
        result = client.generate("hermes3:8b", "hi")
    finally:
        client.close()

    assert isinstance(result, OllamaResponse)
    assert result.text == "hello world"
    assert result.model == "hermes3:8b"
    assert len(seen) == 1
    body = json.loads(seen[0].content.decode())
    assert body["model"] == "hermes3:8b"
    assert body["keep_alive"] == "12h"
    assert body["stream"] is False


def test_generate_respects_per_request_keep_alive_override() -> None:
    seen: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content.decode()))
        return _json_response(200, {"model": "hermes3:70b", "response": "out"})

    transport = _make_transport(handler)
    with OllamaClient(transport=transport, sleep=_no_sleep) as client:
        client.generate("hermes3:70b", "x", keep_alive="0s", num_predict=128)

    body = seen[0]
    assert body["keep_alive"] == "0s"
    assert body["options"]["num_predict"] == 128


def test_chat_extracts_message_content() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return _json_response(
            200,
            {
                "model": "hermes3:8b",
                "message": {"role": "assistant", "content": "ok"},
            },
        )

    transport = _make_transport(handler)
    with OllamaClient(transport=transport, sleep=_no_sleep) as client:
        result = client.chat("hermes3:8b", [{"role": "user", "content": "hi"}])
    assert result.text == "ok"


def test_retry_on_503_then_success() -> None:
    calls = [0]

    def handler(_request: httpx.Request) -> httpx.Response:
        calls[0] += 1
        if calls[0] == 1:
            return httpx.Response(503, text="loading")
        return _json_response(200, {"model": "hermes3:70b", "response": "after-retry"})

    transport = _make_transport(handler)
    with OllamaClient(
        transport=transport,
        sleep=_no_sleep,
        max_retries=2,
    ) as client:
        result = client.generate("hermes3:70b", "x")

    assert calls[0] == 2
    assert result.text == "after-retry"


def test_repeated_503_raises_ollama_error() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="still loading")

    transport = _make_transport(handler)
    with OllamaClient(
        transport=transport,
        sleep=_no_sleep,
        max_retries=3,
    ) as client:
        with pytest.raises(OllamaError) as exc_info:
            client.generate("hermes3:70b", "x")
    assert exc_info.value.status_code == 503


def test_non_503_4xx_raises_immediately() -> None:
    calls = [0]

    def handler(_request: httpx.Request) -> httpx.Response:
        calls[0] += 1
        return httpx.Response(400, text="bad model")

    transport = _make_transport(handler)
    with OllamaClient(
        transport=transport,
        sleep=_no_sleep,
        max_retries=3,
    ) as client:
        with pytest.raises(OllamaError) as exc_info:
            client.generate("nope", "x")
    assert exc_info.value.status_code == 400
    # No retry on 4xx — caller fixes the request.
    assert calls[0] == 1


def test_connection_error_retries_then_raises() -> None:
    calls = [0]

    def handler(_request: httpx.Request) -> httpx.Response:
        calls[0] += 1
        raise httpx.ConnectError("daemon down")

    transport = _make_transport(handler)
    with OllamaClient(
        transport=transport,
        sleep=_no_sleep,
        max_retries=2,
    ) as client:
        with pytest.raises(OllamaError, match="connection error"):
            client.generate("hermes3:8b", "x")
    assert calls[0] == 2


def test_ps_returns_list_of_models() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        return _json_response(
            200,
            {"models": [{"name": "hermes3:8b", "size_vram": 1234}]},
        )

    transport = _make_transport(handler)
    with OllamaClient(transport=transport, sleep=_no_sleep) as client:
        models = client.ps()
    assert models == [{"name": "hermes3:8b", "size_vram": 1234}]


def test_ps_missing_models_key_raises() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return _json_response(200, {"oops": []})

    transport = _make_transport(handler)
    with OllamaClient(transport=transport, sleep=_no_sleep) as client:
        with pytest.raises(OllamaError, match="missing 'models'"):
            client.ps()


def test_pull_calls_pull_endpoint() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        return _json_response(200, {"status": "success", "model": "hermes3:70b"})

    transport = _make_transport(handler)
    with OllamaClient(transport=transport, sleep=_no_sleep) as client:
        result = client.pull("hermes3:70b")
    assert seen == ["/api/pull"]
    assert result.text == ""
    assert result.model == "hermes3:70b"


def test_telemetry_callback_invoked_with_latency() -> None:
    seen: list[tuple[str, OllamaResponse]] = []

    def telemetry(endpoint: str, response: OllamaResponse) -> None:
        seen.append((endpoint, response))

    def handler(_request: httpx.Request) -> httpx.Response:
        return _json_response(200, {"model": "hermes3:8b", "response": "x"})

    transport = _make_transport(handler)
    with OllamaClient(
        transport=transport,
        sleep=_no_sleep,
        telemetry=telemetry,
    ) as client:
        client.generate("hermes3:8b", "x")

    assert len(seen) == 1
    endpoint, response = seen[0]
    assert endpoint == "/api/generate"
    assert response.latency_seconds >= 0.0


def test_telemetry_exceptions_are_swallowed() -> None:
    def bad_telemetry(_endpoint: str, _response: OllamaResponse) -> None:
        raise RuntimeError("telemetry exploded")

    def handler(_request: httpx.Request) -> httpx.Response:
        return _json_response(200, {"model": "hermes3:8b", "response": "x"})

    transport = _make_transport(handler)
    with OllamaClient(
        transport=transport,
        sleep=_no_sleep,
        telemetry=bad_telemetry,
    ) as client:
        # Telemetry failure must NOT propagate to the caller.
        result = client.generate("hermes3:8b", "x")
    assert result.text == "x"


def test_generate_with_options_and_temperature_merge() -> None:
    seen: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content.decode()))
        return _json_response(200, {"model": "hermes3:8b", "response": "y"})

    transport = _make_transport(handler)
    with OllamaClient(transport=transport, sleep=_no_sleep) as client:
        client.generate(
            "hermes3:8b",
            "x",
            temperature=0.7,
            options={"top_p": 0.9, "temperature": 0.1},  # explicit kw wins
        )
    body = seen[0]
    assert body["options"]["top_p"] == 0.9
    assert body["options"]["temperature"] == 0.7  # kwarg overrides options dict


def test_chat_missing_message_raises() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return _json_response(200, {"model": "hermes3:8b"})

    transport = _make_transport(handler)
    with OllamaClient(transport=transport, sleep=_no_sleep) as client:
        with pytest.raises(OllamaError, match="missing 'message'"):
            client.chat("hermes3:8b", [{"role": "user", "content": "hi"}])


def test_invalid_json_body_raises() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="not json")

    transport = _make_transport(handler)
    with OllamaClient(transport=transport, sleep=_no_sleep) as client:
        with pytest.raises(OllamaError, match="not JSON"):
            client.generate("hermes3:8b", "x")


def test_invalid_constructor_args() -> None:
    with pytest.raises(ValueError):
        OllamaClient(max_retries=0)
    with pytest.raises(ValueError):
        OllamaClient(timeout=0.0)


def test_system_prompt_passed_through() -> None:
    seen: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content.decode()))
        return _json_response(200, {"model": "hermes3:8b", "response": "ok"})

    transport = _make_transport(handler)
    with OllamaClient(transport=transport, sleep=_no_sleep) as client:
        client.generate("hermes3:8b", "x", system="be terse")
    assert seen[0]["system"] == "be terse"


def test_post_503_then_503_within_max_retries_succeeds() -> None:
    """Two 503s with max_retries=3 → third call succeeds."""
    calls = [0]

    def handler(_request: httpx.Request) -> httpx.Response:
        calls[0] += 1
        if calls[0] < 3:
            return httpx.Response(503, text="loading")
        return _json_response(200, {"model": "hermes3:70b", "response": "win"})

    transport = _make_transport(handler)
    with OllamaClient(
        transport=transport,
        sleep=_no_sleep,
        max_retries=3,
    ) as client:
        result = client.generate("hermes3:70b", "x")
    assert calls[0] == 3
    assert result.text == "win"
