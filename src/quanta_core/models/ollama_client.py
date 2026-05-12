"""Ollama HTTP client — keep-alive choreography + 503 retry + telemetry.

This module wraps Ollama's REST surface (``/api/generate``, ``/api/chat``,
``/api/ps``, ``/api/pull``) with the discipline the rev2 design locked in:

- ``keep_alive`` is a first-class per-request override AND a client-level
  default (rev2 §4.1).
- Transient 503 responses (model still loading on a cold page-in) and
  ``httpx.HTTPError`` connection failures get one bounded retry with
  exponential backoff. Non-idempotent endpoints (``pull``) skip the
  retry.
- Every request emits a :class:`OllamaResponse` with measured wall-time;
  the caller's ledger writer reads ``response.latency_seconds`` for the
  telemetry tile described in rev2 §3.3 and the LLM_LOGGER_SCHEMA spec.

The client does NOT depend on any ledger module — it accepts an optional
``telemetry`` callback so the caller can wire its writer of choice
without forcing a circular import. Today's production trading bot writes
JSONL via ``user_data/modules/llm_logger.py``; the v4 ledger will swap
in a structured psycopg writer behind the same callback shape.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

import httpx

__all__ = ["OllamaClient", "OllamaError", "OllamaResponse"]


class OllamaError(Exception):
    """Raised on non-retriable Ollama failures (4xx, repeated 5xx, JSON decode)."""

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


@dataclass(frozen=True)
class OllamaResponse:
    """Parsed Ollama response with latency telemetry.

    Attributes
    ----------
    text:
        The completion or final chat-message content. Empty for endpoints
        that don't return text (``ps``, ``pull``).
    model:
        Model tag the daemon claims to have served (``hermes3:8b`` etc.).
        May differ from the requested tag if an alias resolved server-side.
    raw:
        Full JSON-decoded response body, for callers that need fields the
        dataclass doesn't surface (eval-count, prompt-eval-duration, etc.).
    latency_seconds:
        Wall-time from request send to response decode, measured by
        :func:`time.monotonic`. Includes the daemon's cold-load cost on
        first call after eviction.
    """

    text: str
    model: str
    raw: Mapping[str, Any]
    latency_seconds: float


TelemetryCallback = Callable[[str, OllamaResponse], None]


class OllamaClient:
    """HTTP client for Ollama with per-request keep_alive + retry.

    Parameters
    ----------
    base_url:
        Ollama daemon root (no trailing slash). Defaults to
        ``http://127.0.0.1:11434`` to match the production deployment.
    keep_alive_default:
        Default ``keep_alive`` value applied to every generate/chat call
        that does not pass an override. Strings (``"5m"``, ``"12h"``,
        ``"0s"``) and integers (seconds) are both accepted per Ollama's
        documented surface.
    timeout:
        Per-request timeout in seconds. Defaults to 180 — long enough to
        cover the 25-40 s 70B cold-load + a 30 s deliberate debate turn.
    max_retries:
        Maximum retry attempts on transient 503 / connection errors.
        Default 2 (one initial + one retry). Idempotent endpoints only.
    retry_backoff_seconds:
        Base for exponential backoff between retries. Attempt ``n`` waits
        ``retry_backoff_seconds * 2 ** (n - 1)``.
    telemetry:
        Optional callback invoked with ``(endpoint, response)`` after
        every successful request. Use to emit latency to the ledger.
    transport:
        Optional ``httpx.BaseTransport`` for testing. When provided,
        :class:`httpx.MockTransport` lets us assert on request bodies
        without standing up a real daemon.
    sleep:
        Injected sleep function used between retries. Defaults to
        :func:`time.sleep`; tests pass a no-op to keep the suite fast.
    """

    def __init__(
        self,
        base_url: str = "http://127.0.0.1:11434",
        *,
        keep_alive_default: str | int = "5m",
        timeout: float = 180.0,
        max_retries: int = 2,
        retry_backoff_seconds: float = 0.5,
        telemetry: TelemetryCallback | None = None,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] | None = None,
    ) -> None:
        if max_retries < 1:
            raise ValueError("max_retries must be >= 1")
        if timeout <= 0:
            raise ValueError("timeout must be positive")
        self._base_url = base_url.rstrip("/")
        self._keep_alive_default: str | int = keep_alive_default
        self._max_retries = max_retries
        self._retry_backoff = retry_backoff_seconds
        self._telemetry = telemetry
        self._sleep: Callable[[float], None] = sleep or time.sleep
        self._client = httpx.Client(
            base_url=self._base_url,
            timeout=timeout,
            transport=transport,
        )

    # ------------------------------------------------------------------
    # Context manager / lifecycle
    # ------------------------------------------------------------------

    def close(self) -> None:
        """Close the underlying ``httpx.Client``."""
        self._client.close()

    def __enter__(self) -> OllamaClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def generate(
        self,
        model: str,
        prompt: str,
        *,
        keep_alive: str | int | None = None,
        num_predict: int | None = None,
        temperature: float | None = None,
        options: Mapping[str, Any] | None = None,
        system: str | None = None,
    ) -> OllamaResponse:
        """Call ``POST /api/generate`` with streaming disabled.

        Parameters
        ----------
        model:
            Ollama tag (``hermes3:8b``, ``hermes3:70b-arbiter-current``…).
        prompt:
            User prompt.
        keep_alive:
            Per-request override. ``None`` → use client default.
            ``"0s"`` (or ``0``) requests immediate eviction — the rev2
            post-debate hook pattern.
        num_predict:
            Max output tokens. ``None`` defers to Ollama's per-model
            default.
        temperature:
            Sampling temperature. Passed through ``options``.
        options:
            Additional Ollama options. Merged into the request payload's
            ``options`` field; explicit ``temperature``/``num_predict``
            arguments override matching keys.
        system:
            Optional system prompt.

        Returns
        -------
        OllamaResponse
            Decoded response with ``text`` = the completion body.
        """
        payload: dict[str, Any] = {
            "model": model,
            "prompt": prompt,
            "stream": False,
            "keep_alive": self._resolve_keep_alive(keep_alive),
        }
        merged = self._merge_options(options, num_predict=num_predict, temperature=temperature)
        if merged:
            payload["options"] = merged
        if system is not None:
            payload["system"] = system
        return self._post_with_retry("/api/generate", payload, text_key="response")

    def chat(
        self,
        model: str,
        messages: Sequence[Mapping[str, Any]],
        *,
        keep_alive: str | int | None = None,
        num_predict: int | None = None,
        temperature: float | None = None,
        options: Mapping[str, Any] | None = None,
    ) -> OllamaResponse:
        """Call ``POST /api/chat`` with streaming disabled.

        ``messages`` is a sequence of ``{"role": ..., "content": ...}``
        dicts. The chat endpoint returns its final message body under
        ``message.content``; :class:`OllamaResponse.text` exposes it as
        a string.
        """
        payload: dict[str, Any] = {
            "model": model,
            "messages": list(messages),
            "stream": False,
            "keep_alive": self._resolve_keep_alive(keep_alive),
        }
        merged = self._merge_options(options, num_predict=num_predict, temperature=temperature)
        if merged:
            payload["options"] = merged
        return self._post_with_retry("/api/chat", payload, text_key="chat.message.content")

    def ps(self) -> list[Mapping[str, Any]]:
        """Call ``GET /api/ps`` — return the resident-model list.

        Used by the memory watchdog (rev2 §3.3) to detect 70B loitering
        outside a debate window.
        """
        response, _ = self._do_request("GET", "/api/ps", None)
        data = response.get("models")
        if not isinstance(data, list):
            raise OllamaError("ps response missing 'models' list")
        # Each entry is a JSON object; copy into a list of mappings so
        # the public type is invariant of whatever httpx hands us.
        return [dict(m) for m in data if isinstance(m, Mapping)]

    def pull(self, model: str) -> OllamaResponse:
        """Call ``POST /api/pull`` to fetch a model tag.

        Streaming disabled so we get a single terminal response. Pull is
        NOT retried automatically — re-pulling a half-fetched tag is
        safe, but the retry delay can mask a stuck mirror; the caller
        decides.
        """
        payload = {"model": model, "stream": False}
        response, latency = self._do_request("POST", "/api/pull", payload)
        return self._build_response("/api/pull", response, latency, text="")

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _resolve_keep_alive(self, override: str | int | None) -> str | int:
        return override if override is not None else self._keep_alive_default

    @staticmethod
    def _merge_options(
        options: Mapping[str, Any] | None,
        *,
        num_predict: int | None,
        temperature: float | None,
    ) -> dict[str, Any]:
        merged: dict[str, Any] = dict(options) if options else {}
        if num_predict is not None:
            merged["num_predict"] = num_predict
        if temperature is not None:
            merged["temperature"] = temperature
        return merged

    def _post_with_retry(
        self,
        path: str,
        payload: Mapping[str, Any],
        *,
        text_key: str,
    ) -> OllamaResponse:
        response, latency = self._do_request("POST", path, payload)
        text = self._extract_text(response, text_key)
        return self._build_response(path, response, latency, text=text)

    @staticmethod
    def _extract_text(response: Mapping[str, Any], text_key: str) -> str:
        """Read ``text_key`` from the response payload.

        ``text_key`` is either a single top-level key (``"response"`` for
        ``/api/generate``) or the dotted path ``"chat.message.content"``
        — a magic sentinel for ``/api/chat`` so we don't litter the call
        site with conditional shape parsing.
        """
        if text_key == "chat.message.content":
            message = response.get("message")
            if not isinstance(message, Mapping):
                raise OllamaError("chat response missing 'message'")
            content = message.get("content", "")
            if not isinstance(content, str):
                raise OllamaError("chat response 'message.content' is not a string")
            return content
        value = response.get(text_key, "")
        if not isinstance(value, str):
            raise OllamaError(f"response key {text_key!r} is not a string")
        return value

    def _build_response(
        self,
        path: str,
        response: Mapping[str, Any],
        latency: float,
        *,
        text: str,
    ) -> OllamaResponse:
        model = response.get("model")
        result = OllamaResponse(
            text=text,
            model=str(model) if isinstance(model, str) else "",
            raw=dict(response),
            latency_seconds=latency,
        )
        if self._telemetry is not None:
            try:
                self._telemetry(path, result)
            except Exception:
                # Telemetry must NEVER break the trading hot path. The
                # caller's structlog logger is the right place for
                # visibility; the registry-level callback failing is a
                # bug in the callback, not a daemon issue.
                pass
        return result

    def _do_request(
        self,
        method: str,
        path: str,
        payload: Mapping[str, Any] | None,
    ) -> tuple[Mapping[str, Any], float]:
        """Run one request with retries on 503 + connection failure.

        Returns
        -------
        tuple[Mapping[str, Any], float]
            Decoded JSON body + wall-time latency in seconds.

        Raises
        ------
        OllamaError
            On non-retriable failure or exhausted retries.
        """
        last_error: Exception | None = None
        for attempt in range(1, self._max_retries + 1):
            start = time.monotonic()
            try:
                http_response = self._send(method, path, payload)
            except httpx.HTTPError as exc:
                last_error = exc
                if attempt < self._max_retries:
                    self._sleep(self._backoff_for(attempt))
                    continue
                raise OllamaError(f"connection error to {path}: {exc}") from exc

            if http_response.status_code == 503 and attempt < self._max_retries:
                # 503 → model still loading or daemon under pressure.
                # Back off and retry. The daemon's own load-on-demand
                # path will be ready on the second attempt.
                self._sleep(self._backoff_for(attempt))
                continue

            latency = time.monotonic() - start
            if http_response.status_code >= 400:
                raise OllamaError(
                    f"{method} {path} returned {http_response.status_code}: "
                    f"{http_response.text[:256]}",
                    status_code=http_response.status_code,
                )
            try:
                body = http_response.json()
            except ValueError as exc:
                raise OllamaError(f"{path} response was not JSON: {exc}") from exc
            if not isinstance(body, dict):
                raise OllamaError(f"{path} returned non-object JSON: {type(body).__name__}")
            return body, latency

        # Exhausted retries on 503. Last response was 503 by construction.
        message = "all retries returned 503"
        if last_error is not None:
            message = f"{message} (last error: {last_error})"
        raise OllamaError(message, status_code=503)

    def _send(
        self,
        method: str,
        path: str,
        payload: Mapping[str, Any] | None,
    ) -> httpx.Response:
        if method == "GET":
            return self._client.get(path)
        return self._client.post(path, json=payload)

    def _backoff_for(self, attempt: int) -> float:
        return float(self._retry_backoff * (2 ** (attempt - 1)))
