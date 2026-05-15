"""
Schema-validated LLM helper for hermes3:8b (and Anthropic fallback).

Sits one level above `shark.llm.client.chat_json`. Where `chat_json`
returns raw text and the caller does `json.loads` + manual `setdefault`,
`chat_structured` returns an already-validated Pydantic model — or
raises `StructuredOutputError` after exhausting retries.

How it gets reliable JSON from Ollama
-------------------------------------
1. Build the JSON Schema from the Pydantic model
   (`schema.model_json_schema()`).
2. POST directly to `/api/chat` with `format="json"` so Ollama
   constrains the decoder to syntactically-valid JSON. The legacy
   `chat_json` helper does NOT pass `format`, so we bypass it for the
   primary attempt to take advantage of grammar constraints.
3. On parse / validation failure, retry with the validation error
   appended to the user prompt. After `max_retries`, give up and raise.

Anthropic fallback
------------------
Anthropic doesn't have an Ollama-equivalent grammar mode but the SDK's
tool-use API enforces the schema for us. We package the Pydantic schema
as a single tool, set `tool_choice={"type":"any"}`, and read
`block.input` (already a dict, no `json.loads` needed).

Public surface
--------------

    chat_structured(provider, tier, system, user, schema, max_retries=2)

The signature mirrors `chat_json` so callers can swap them with minimal
churn (see HANDOFF.md for the migration plan).
"""

from __future__ import annotations

import json
import logging
import os
import time
from typing import Any, TypeVar

from pydantic import BaseModel, ValidationError

logger = logging.getLogger(__name__)


T = TypeVar("T", bound=BaseModel)


class StructuredOutputError(RuntimeError):
    """Raised when an LLM call cannot produce schema-valid output."""

    def __init__(
        self,
        message: str,
        *,
        attempts: int = 0,
        last_raw: str = "",
        last_error: Exception | None = None,
        schema_name: str = "",
    ) -> None:
        super().__init__(message)
        self.attempts = attempts
        self.last_raw = last_raw
        self.last_error = last_error
        self.schema_name = schema_name


# ---------------------------------------------------------------------------
# Internal: schema → prompt scaffolding
# ---------------------------------------------------------------------------


def _schema_hint(schema: type[BaseModel]) -> str:
    """Render a JSON Schema string suitable for inlining in the user prompt.

    Hermes-3 follows JSON Schema reliably when it's present in the
    prompt and `format="json"` is set on the request.
    """
    try:
        return json.dumps(schema.model_json_schema(), indent=2)
    except Exception as exc:  # pragma: no cover — schema generation is stable
        logger.warning("Failed to render schema for %s: %s", schema.__name__, exc)
        return f"<schema {schema.__name__}>"


def _build_user_prompt(user: str, schema: type[BaseModel]) -> str:
    """Append the JSON-Schema hint to the user prompt."""
    return (
        f"{user}\n\n"
        f"Respond with a single JSON object that validates against this "
        f"schema. Do not include markdown, prose, or code fences:\n"
        f"```json\n{_schema_hint(schema)}\n```"
    )


def _build_retry_prompt(
    original_user: str,
    schema: type[BaseModel],
    error: Exception,
    last_raw: str,
) -> str:
    """Build a retry prompt that quotes the validation error."""
    err_text = str(error)[:500]
    raw_snippet = (last_raw or "")[:400]
    return (
        f"{_build_user_prompt(original_user, schema)}\n\n"
        f"Your previous response failed validation. Error:\n"
        f"{err_text}\n\n"
        f"Your previous response was (truncated):\n"
        f"{raw_snippet}\n\n"
        f"Output ONLY valid JSON matching the schema above. "
        f"Fix the error and try again."
    )


# ---------------------------------------------------------------------------
# Backend calls
# ---------------------------------------------------------------------------


def _resolve_ollama_model(tier: str) -> str:
    """Match the resolution rules in `shark.llm.client._resolve_ollama_model`."""
    tier = (tier or "deep").lower()
    if tier == "fast":
        return (
            os.environ.get("OLLAMA_FAST_MODEL", "")
            or os.environ.get("OLLAMA_MODEL_FAST", "")
            or "hermes3:8b"
        )
    return (
        os.environ.get("OLLAMA_MODEL", "")
        or os.environ.get("OLLAMA_MODEL_DEEP", "")
        or "hermes3:70b"
    )


def _call_ollama_json(
    system: str,
    user: str,
    *,
    model: str,
    max_tokens: int,
    temperature: float,
    base_url: str | None = None,
    timeout: float | None = None,
) -> str:
    """POST to Ollama with `format="json"` and return the raw content string.

    Kept as a thin wrapper so tests can monkeypatch it without faking
    HTTP. Use the requests lib directly to avoid recursing through
    `client.chat_json`, which doesn't pass `format`.
    """
    import requests

    base = (
        base_url
        or os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434")
    ).rstrip("/")
    payload: dict[str, Any] = {
        "model": model,
        "format": "json",
        "stream": False,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "options": {
            "temperature": float(temperature),
            "num_predict": int(max_tokens),
        },
    }
    resp = requests.post(
        f"{base}/api/chat",
        json=payload,
        timeout=float(timeout or os.environ.get("OLLAMA_TIMEOUT_S", "180")),
    )
    resp.raise_for_status()
    data = resp.json()
    return (data.get("message") or {}).get("content", "") or ""


def _call_anthropic_tool(
    system: str,
    user: str,
    schema: type[BaseModel],
    *,
    model: str,
    max_tokens: int,
    temperature: float,
) -> dict[str, Any]:
    """Use Anthropic tool-use to enforce the schema. Returns parsed dict."""
    import anthropic

    client = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))
    tool = {
        "name": f"emit_{schema.__name__.lower()}",
        "description": (
            f"Emit a {schema.__name__} record. All fields are required "
            f"unless the schema marks them optional."
        ),
        "input_schema": schema.model_json_schema(),
    }
    response = client.messages.create(
        model=model,
        max_tokens=max_tokens,
        temperature=temperature,
        system=system,
        messages=[{"role": "user", "content": user}],
        tools=[tool],
        tool_choice={"type": "any"},
    )
    for block in response.content:
        if getattr(block, "type", "") == "tool_use":
            return block.input  # already a dict
    # No tool block — surface the text so the retry loop can react.
    text_parts = [
        getattr(b, "text", "") for b in response.content
        if getattr(b, "type", "") == "text"
    ]
    raise StructuredOutputError(
        "Anthropic response had no tool_use block",
        last_raw="\n".join(text_parts),
        schema_name=schema.__name__,
    )


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def chat_structured(
    provider: str,
    tier: str,
    system: str,
    user: str,
    schema: type[T],
    max_retries: int = 2,
    *,
    max_tokens: int = 1000,
    temperature: float = 0.2,
    model: str | None = None,
) -> T:
    """Send a prompt to the LLM and return a validated `schema` instance.

    Args:
        provider:    "ollama" or "anthropic". Case-insensitive.
        tier:        "fast" (8B/cheap) or "deep" (70B/quality). Selects
                     the Ollama model when `model` is not pinned.
        system:      System prompt.
        user:        User prompt; the JSON Schema is appended for the
                     model.
        schema:      Pydantic v2 model class to validate against.
        max_retries: How many times to retry on validation failure. The
                     initial attempt does NOT count toward this — total
                     attempts = 1 + max_retries.
        max_tokens:  Generation cap.
        temperature: Sampling temperature. Defaults low for JSON
                     faithfulness.
        model:       Optional override; ignores `tier` when set.

    Returns:
        An instance of `schema`.

    Raises:
        StructuredOutputError: when every attempt failed validation
            or the backend errored. The exception carries `.last_raw`
            and `.last_error` for debugging.
    """
    provider_norm = (provider or "ollama").lower().strip()
    tier_norm = (tier or "deep").lower().strip()
    last_raw = ""
    last_error: Exception | None = None
    user_prompt = _build_user_prompt(user, schema)

    total_attempts = 1 + max(0, int(max_retries))
    for attempt in range(total_attempts):
        try:
            if provider_norm == "anthropic":
                anthropic_model = model or os.environ.get(
                    "CLAUDE_MODEL", "claude-sonnet-4-6",
                )
                parsed = _call_anthropic_tool(
                    system, user_prompt, schema,
                    model=anthropic_model,
                    max_tokens=max_tokens,
                    temperature=temperature,
                )
                last_raw = json.dumps(parsed)
                return schema.model_validate(parsed)

            # Default: Ollama
            ollama_model = model or _resolve_ollama_model(tier_norm)
            last_raw = _call_ollama_json(
                system, user_prompt,
                model=ollama_model,
                max_tokens=max_tokens,
                temperature=temperature,
            )
            parsed = json.loads(last_raw)
            return schema.model_validate(parsed)

        except (ValidationError, json.JSONDecodeError) as exc:
            last_error = exc
            logger.warning(
                "chat_structured attempt %d/%d failed validation for %s: %s",
                attempt + 1, total_attempts, schema.__name__, exc,
            )
            # Rewrite the user prompt with the validation error before retry.
            user_prompt = _build_retry_prompt(user, schema, exc, last_raw)
            continue
        except StructuredOutputError as exc:
            # Anthropic returned no tool_use; preserve raw, then retry.
            last_error = exc
            last_raw = exc.last_raw or last_raw
            logger.warning(
                "chat_structured attempt %d/%d: Anthropic returned no tool block",
                attempt + 1, total_attempts,
            )
            user_prompt = _build_retry_prompt(user, schema, exc, last_raw)
            continue
        except Exception as exc:
            # Network / API errors: retry once with a brief backoff. We
            # still count it toward the budget so a permanently-down
            # backend doesn't hang the caller.
            last_error = exc
            logger.warning(
                "chat_structured attempt %d/%d backend error for %s: %s",
                attempt + 1, total_attempts, schema.__name__, exc,
            )
            time.sleep(min(2.0, 0.5 * (attempt + 1)))
            continue

    raise StructuredOutputError(
        f"chat_structured exhausted {total_attempts} attempts for "
        f"{schema.__name__}: {last_error}",
        attempts=total_attempts,
        last_raw=last_raw,
        last_error=last_error,
        schema_name=schema.__name__,
    )


__all__ = ["chat_structured", "StructuredOutputError"]
