"""
vLLM OpenAI-compatible client with hot-swappable LoRA adapter support.

Why a dedicated client (vs reusing OpenAIClient):
    The standard OpenAI client doesn't carry the per-request `adapter_name`
    field vLLM uses to pick the active LoRA. We could shove it into
    `extra_body=` on every call site, but the trading-bot calls the LLM
    from a dozen places. Centralising the contract here means callers
    only deal with one knob ("which adapter?") and we get a single
    failure surface to fall back from when vLLM is unreachable.

How vLLM exposes adapters
-------------------------
vLLM 0.5+ launches with `--enable-lora --lora-modules name=path ...` so a
set of adapters is preloaded at boot. New adapters can be registered at
runtime by POSTing to ``/v1/load_lora_adapter`` (no restart). Per-request
adapter selection happens by passing the adapter's registered name as the
``model`` field of the OpenAI chat-completions request — vLLM routes that
name to the matching LoRA on top of the base model.

NOTE: An earlier vLLM proposal accepted ``extra_body={"adapter_name": ...}``
but the merged behaviour in 0.5+ is ``model=<adapter_name>``. We pass BOTH
for forward compatibility: callers can rely on whichever the running vLLM
build honours. Either form is harmless to the other.

Failure contract
----------------
A 5xx, timeout, or connection error raises ``VLLMUnavailableError``. The
shark.llm.client routing layer catches that exception and transparently
falls back to Ollama with the base model and NO adapter. See HANDOFF.md
("Fallback behavior").

Public surface
--------------
- ``VLLMClient`` — implements the ``LLMClient`` ABC.
- ``VLLMUnavailableError`` — raised on 5xx / connection failures so the
  caller can decide to fall back.
- ``register_adapter(name, path)`` — convenience wrapper around the
  ``/v1/load_lora_adapter`` endpoint for runtime adapter registration.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any, Optional

from shark.llm.client import LLMClient, LLMResponse

logger = logging.getLogger(__name__)

# Module-level import so tests can `unittest.mock.patch(
# "shark.llm.vllm_client.requests")` cleanly. We tolerate the absence of
# `requests` so `pip install` order doesn't break the import chain — the
# methods will raise RuntimeError if called without the lib.
try:
    import requests as _real_requests
    requests = _real_requests
except ImportError:  # pragma: no cover — production environment has requests
    requests = None  # type: ignore[assignment]


class VLLMUnavailableError(RuntimeError):
    """vLLM returned 5xx/connection-error/timeout — caller should fall back."""

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


# Status codes we treat as "vLLM is unhealthy → fall back to Ollama".
# 4xx other than 429 are surfaced as plain RuntimeErrors because they
# usually indicate a programming bug (bad adapter name, bad schema, …)
# and silently falling back would hide that.
_FALLBACK_STATUS_CODES = {429, 500, 502, 503, 504}


class VLLMClient(LLMClient):
    """OpenAI-compatible client for a vLLM server with multi-LoRA support.

    The ``model`` constructor arg is the BASE model name (e.g.
    ``qwen3:30b``). Per-call adapter selection is done by passing
    ``adapter`` to ``chat()`` / ``chat_with_tools()``. When ``adapter`` is
    None or empty, the base model is used with no LoRA.
    """

    DEFAULT_BASE_URL = "http://localhost:8090"

    def __init__(self, model: str = "qwen3:30b", **kwargs: Any) -> None:
        super().__init__(model, **kwargs)
        self.base_url = kwargs.get(
            "base_url", os.environ.get("VLLM_BASE_URL", self.DEFAULT_BASE_URL),
        ).rstrip("/")
        # Generous default — first request after cold-start can load the
        # weights from disk. Operator can tighten via env.
        self.timeout = float(
            kwargs.get("timeout", os.environ.get("VLLM_TIMEOUT_S", "180"))
        )
        self._lib_ok = requests is not None
        if not self._lib_ok:
            logger.warning("VLLMClient: `requests` not available; chat() will raise")

    @property
    def provider_name(self) -> str:
        return "vllm"

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _select_model(self, adapter: str | None) -> str:
        """vLLM uses the OpenAI ``model`` field as the adapter selector.

        When an adapter is named, the request must address that adapter by
        name (vLLM has already mounted it on top of the base). Empty /
        None → use the base.
        """
        return adapter.strip() if adapter else self.model

    def _post_chat(
        self,
        *,
        adapter: str | None,
        messages: list[dict[str, str]],
        max_tokens: int,
        temperature: float,
        response_format: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Send a chat-completions POST and return the parsed JSON body.

        Raises ``VLLMUnavailableError`` on 5xx / 429 / timeout / connection
        errors. Raises plain ``RuntimeError`` on 4xx (programmer error).
        """
        if not self._lib_ok or requests is None:
            raise RuntimeError("VLLMClient requires the `requests` package")

        payload: dict[str, Any] = {
            "model": self._select_model(adapter),
            "messages": messages,
            "max_tokens": int(max_tokens),
            "temperature": float(temperature),
            "stream": False,
        }
        # vLLM accepts the OpenAI response_format spec for JSON-mode output.
        if response_format is not None:
            payload["response_format"] = response_format
        # Forward-compat: some vLLM builds honour `extra_body.adapter_name`.
        # Harmless when ignored. Lets callers that pin the BASE in `model`
        # still select an adapter.
        if adapter:
            payload["extra_body"] = {"adapter_name": adapter}

        url = f"{self.base_url}/v1/chat/completions"
        try:
            resp = requests.post(url, json=payload, timeout=self.timeout)
        except (requests.ConnectionError, requests.Timeout) as exc:
            raise VLLMUnavailableError(
                f"vLLM unreachable at {self.base_url}: {exc}",
            ) from exc

        if resp.status_code in _FALLBACK_STATUS_CODES:
            raise VLLMUnavailableError(
                f"vLLM returned {resp.status_code}: {resp.text[:200]}",
                status_code=resp.status_code,
            )
        if not resp.ok:
            # 4xx that aren't 429: surface as a programmer error.
            raise RuntimeError(
                f"vLLM {resp.status_code} for adapter={adapter!r}: "
                f"{resp.text[:400]}"
            )
        try:
            return resp.json()
        except json.JSONDecodeError as exc:
            raise RuntimeError(
                f"vLLM returned non-JSON body: {resp.text[:200]}"
            ) from exc

    @staticmethod
    def _parse_openai_response(data: dict[str, Any], requested_model: str) -> LLMResponse:
        """Pull content + usage out of an OpenAI chat-completions response."""
        choices = data.get("choices") or []
        if not choices:
            raise RuntimeError(f"vLLM returned no choices: {data!r}")
        message = (choices[0] or {}).get("message") or {}
        content = message.get("content") or ""
        usage_raw = data.get("usage") or {}
        usage = {
            "input_tokens": int(usage_raw.get("prompt_tokens", 0) or 0),
            "output_tokens": int(usage_raw.get("completion_tokens", 0) or 0),
        }
        # `data["model"]` echoes whatever name vLLM resolved (often the
        # adapter name). Carry that through so the tracker logs the
        # adapter that actually served the call.
        served_model = data.get("model") or requested_model
        return LLMResponse(content, served_model, usage)

    # ------------------------------------------------------------------
    # LLMClient ABC
    # ------------------------------------------------------------------

    def chat(
        self,
        system_prompt: str,
        user_message: str,
        max_tokens: int = 1000,
        temperature: float = 0.3,
        **kwargs: Any,
    ) -> LLMResponse:
        adapter = kwargs.get("adapter") or kwargs.get("adapter_name")
        # Optional structured-output: caller passes format="json" the same
        # way the Ollama path does.
        response_format: dict[str, Any] | None = None
        if kwargs.get("format") == "json":
            response_format = {"type": "json_object"}
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_message},
        ]
        data = self._post_chat(
            adapter=adapter,
            messages=messages,
            max_tokens=max_tokens,
            temperature=temperature,
            response_format=response_format,
        )
        return self._parse_openai_response(data, self._select_model(adapter))

    def chat_with_tools(
        self,
        system_prompt: str,
        user_message: str,
        tools: list[dict],
        max_tokens: int = 1000,
        temperature: float = 0.3,
        **kwargs: Any,
    ) -> LLMResponse:
        """vLLM supports OpenAI-style function-calling.

        We pass tools through using the OpenAI function-calling shape and
        force a tool choice. When the running vLLM build doesn't support
        function-calling (older trees), we fall back to embedding the
        schema in the user prompt — same approach Ollama uses.
        """
        adapter = kwargs.get("adapter") or kwargs.get("adapter_name")
        if not tools:
            return self.chat(system_prompt, user_message, max_tokens, temperature,
                             **kwargs)
        # Convert Anthropic-style tools (the shark canonical shape) into
        # OpenAI function-calling shape.
        functions = []
        for t in tools:
            functions.append({
                "type": "function",
                "function": {
                    "name": t["name"],
                    "description": t.get("description", ""),
                    "parameters": t.get("input_schema", {}),
                },
            })

        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_message},
        ]
        try:
            if requests is None:
                raise RuntimeError("VLLMClient requires the `requests` package")
            payload: dict[str, Any] = {
                "model": self._select_model(adapter),
                "messages": messages,
                "max_tokens": int(max_tokens),
                "temperature": float(temperature),
                "stream": False,
                "tools": functions,
                "tool_choice": "required",
            }
            if adapter:
                payload["extra_body"] = {"adapter_name": adapter}
            try:
                resp = requests.post(
                    f"{self.base_url}/v1/chat/completions",
                    json=payload, timeout=self.timeout,
                )
            except (requests.ConnectionError, requests.Timeout) as exc:
                raise VLLMUnavailableError(
                    f"vLLM unreachable at {self.base_url}: {exc}",
                ) from exc
            if resp.status_code in _FALLBACK_STATUS_CODES:
                raise VLLMUnavailableError(
                    f"vLLM returned {resp.status_code}: {resp.text[:200]}",
                    status_code=resp.status_code,
                )
            if not resp.ok:
                # Function-calling not supported on this vLLM build? Drop
                # back to schema-in-prompt.
                logger.info(
                    "vLLM tool-call POST returned %s; falling back to "
                    "schema-in-prompt", resp.status_code,
                )
                return self._tool_fallback(
                    system_prompt, user_message, tools, max_tokens,
                    temperature, **kwargs,
                )
            data = resp.json()
        except VLLMUnavailableError:
            raise
        except Exception as exc:  # pragma: no cover — defensive
            logger.warning("vLLM tool-call path errored: %s — using fallback", exc)
            return self._tool_fallback(
                system_prompt, user_message, tools, max_tokens,
                temperature, **kwargs,
            )

        choices = data.get("choices") or []
        if choices:
            msg = (choices[0] or {}).get("message") or {}
            tool_calls = msg.get("tool_calls") or []
            if tool_calls:
                args = (tool_calls[0] or {}).get("function", {}).get("arguments", "")
                # arguments is a JSON-encoded string per the OpenAI spec.
                usage_raw = data.get("usage") or {}
                usage = {
                    "input_tokens": int(usage_raw.get("prompt_tokens", 0) or 0),
                    "output_tokens": int(usage_raw.get("completion_tokens", 0) or 0),
                }
                served = data.get("model") or self._select_model(adapter)
                return LLMResponse(args, served, usage)
        # No tool_call returned — fall back so caller still gets a parseable string.
        return self._tool_fallback(
            system_prompt, user_message, tools, max_tokens,
            temperature, **kwargs,
        )

    def _tool_fallback(
        self,
        system_prompt: str,
        user_message: str,
        tools: list[dict],
        max_tokens: int,
        temperature: float,
        **kwargs: Any,
    ) -> LLMResponse:
        """Embed the JSON Schema in the user prompt + request json_object mode."""
        schema = tools[0].get("input_schema", {}) if tools else {}
        enhanced = (
            f"{user_message}\n\n"
            f"Respond with a single JSON object matching this schema "
            f"(no prose, no markdown, no code-fence):\n"
            f"{json.dumps(schema, indent=2)}"
        )
        return self.chat(
            system_prompt, enhanced,
            max_tokens=max_tokens,
            temperature=min(0.2, float(temperature)),
            format="json",
            **{k: v for k, v in kwargs.items() if k not in ("format",)},
        )


# ---------------------------------------------------------------------------
# Runtime adapter registration
# ---------------------------------------------------------------------------


def register_adapter(
    name: str,
    path: str,
    *,
    base_url: Optional[str] = None,
    timeout: float = 30.0,
) -> dict[str, Any]:
    """POST /v1/load_lora_adapter to register a LoRA adapter at runtime.

    No vLLM restart required. Use this when ModelForge promotes a new
    champion mid-session and the trading-bot wants to start routing to
    it without bouncing the serving plane.

    Args:
        name: The adapter's registered name (callers will pass this as
              the ``adapter`` kwarg on chat()).
        path: Absolute path inside the vLLM container (e.g.
              ``/lora/reflector-2026-05-12``).
        base_url: vLLM base URL. Defaults to env VLLM_BASE_URL.
        timeout: Request timeout in seconds.

    Returns:
        The parsed JSON response from vLLM.

    Raises:
        VLLMUnavailableError on 5xx / connection error.
        RuntimeError on 4xx.
    """
    if requests is None:
        raise RuntimeError("register_adapter requires the `requests` package")

    url = (
        base_url
        or os.environ.get("VLLM_BASE_URL", VLLMClient.DEFAULT_BASE_URL)
    ).rstrip("/") + "/v1/load_lora_adapter"
    try:
        resp = requests.post(
            url, json={"lora_name": name, "lora_path": path}, timeout=timeout,
        )
    except (requests.ConnectionError, requests.Timeout) as exc:
        raise VLLMUnavailableError(
            f"vLLM unreachable for adapter registration: {exc}",
        ) from exc

    if resp.status_code in _FALLBACK_STATUS_CODES:
        raise VLLMUnavailableError(
            f"vLLM returned {resp.status_code} on adapter registration",
            status_code=resp.status_code,
        )
    if not resp.ok:
        raise RuntimeError(
            f"Adapter registration failed ({resp.status_code}): {resp.text[:200]}"
        )
    try:
        return resp.json()
    except json.JSONDecodeError:
        return {"raw": resp.text}


__all__ = ["VLLMClient", "VLLMUnavailableError", "register_adapter"]
