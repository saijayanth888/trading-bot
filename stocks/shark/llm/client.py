"""
Multi-provider LLM client abstraction.

Inspired by TradingAgents' provider-agnostic client architecture.
Allows Shark to use different LLM providers for different agent roles:
  - Decision arbiter → Claude (highest quality)
  - Debate rounds → GPT-4o-mini or Gemini Flash (cheaper, faster)
  - Risk review → configurable per use case

Providers:
  - ollama (default): local Hermes-3 / Llama models via Ollama (zero cost)
  - anthropic: Claude models via Anthropic API
  - openai: GPT models via OpenAI API
  - google: Gemini models via Google AI API

Usage:
    from shark.llm.client import get_llm_client
    client = get_llm_client("anthropic", model="claude-sonnet-4-6")
    response = client.chat("You are an analyst.", "Analyze AAPL")
"""

from __future__ import annotations

import json
import logging
import os
import time
from abc import ABC, abstractmethod
from typing import Any, Optional

logger = logging.getLogger(__name__)


class LLMResponse:
    """Unified response object across providers."""

    def __init__(self, content: str, model: str, usage: dict[str, int] | None = None):
        self.content = content
        self.model = model
        self.usage = usage or {}

    def __str__(self) -> str:
        return self.content

    def to_json(self) -> dict | None:
        """Try to parse content as JSON. Returns None on failure."""
        raw = self.content.strip()
        if raw.startswith("```"):
            raw = "\n".join(l for l in raw.splitlines() if not l.startswith("```")).strip()
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return None


class LLMClient(ABC):
    """Abstract base class for LLM providers."""

    def __init__(self, model: str, **kwargs):
        self.model = model
        self.kwargs = kwargs

    @abstractmethod
    def chat(
        self,
        system_prompt: str,
        user_message: str,
        max_tokens: int = 1000,
        temperature: float = 0.3,
        **kwargs,
    ) -> LLMResponse:
        """Send a chat message and get a response."""
        ...

    @abstractmethod
    def chat_with_tools(
        self,
        system_prompt: str,
        user_message: str,
        tools: list[dict],
        max_tokens: int = 1000,
        temperature: float = 0.3,
        **kwargs,
    ) -> LLMResponse:
        """Send a chat message with tool-use schemas for structured output."""
        ...

    @property
    @abstractmethod
    def provider_name(self) -> str:
        """Return the provider name."""
        ...


# ---------------------------------------------------------------------------
# Anthropic (Claude)
# ---------------------------------------------------------------------------

class AnthropicClient(LLMClient):
    """Claude via the Anthropic API."""

    def __init__(self, model: str = "claude-sonnet-4-6", **kwargs):
        super().__init__(model, **kwargs)
        try:
            import anthropic
            self._lib = anthropic
            self._client = anthropic.Anthropic(
                api_key=kwargs.get("api_key", os.environ.get("ANTHROPIC_API_KEY")),
            )
        except ImportError:
            raise ImportError("anthropic package required: pip install anthropic")

    @property
    def provider_name(self) -> str:
        return "anthropic"

    def chat(self, system_prompt, user_message, max_tokens=1000, temperature=0.3, **kwargs):
        response = self._client.messages.create(
            model=self.model,
            max_tokens=max_tokens,
            temperature=temperature,
            system=[{"type": "text", "text": system_prompt,
                     "cache_control": {"type": "ephemeral"}}],
            messages=[{"role": "user", "content": user_message}],
        )
        usage = {
            "input_tokens": response.usage.input_tokens,
            "output_tokens": response.usage.output_tokens,
        }
        return LLMResponse(response.content[0].text, self.model, usage)

    def chat_with_tools(self, system_prompt, user_message, tools, max_tokens=1000,
                        temperature=0.3, **kwargs):
        response = self._client.messages.create(
            model=self.model,
            max_tokens=max_tokens,
            temperature=temperature,
            system=[{"type": "text", "text": system_prompt,
                     "cache_control": {"type": "ephemeral"}}],
            messages=[{"role": "user", "content": user_message}],
            tools=tools,
            tool_choice={"type": "any"},
        )
        # Extract tool-use result as JSON string
        for block in response.content:
            if block.type == "tool_use":
                content = json.dumps(block.input)
                usage = {
                    "input_tokens": response.usage.input_tokens,
                    "output_tokens": response.usage.output_tokens,
                }
                return LLMResponse(content, self.model, usage)
        # Fallback to text
        return self.chat(system_prompt, user_message, max_tokens, temperature)


# ---------------------------------------------------------------------------
# OpenAI (GPT)
# ---------------------------------------------------------------------------

class OpenAIClient(LLMClient):
    """GPT models via the OpenAI API."""

    def __init__(self, model: str = "gpt-4o-mini", **kwargs):
        super().__init__(model, **kwargs)
        try:
            from openai import OpenAI
            self._client = OpenAI(
                api_key=kwargs.get("api_key", os.environ.get("OPENAI_API_KEY")),
            )
        except ImportError:
            raise ImportError("openai package required: pip install openai")

    @property
    def provider_name(self) -> str:
        return "openai"

    def chat(self, system_prompt, user_message, max_tokens=1000, temperature=0.3, **kwargs):
        response = self._client.chat.completions.create(
            model=self.model,
            max_tokens=max_tokens,
            temperature=temperature,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_message},
            ],
        )
        content = response.choices[0].message.content or ""
        usage = {
            "input_tokens": response.usage.prompt_tokens if response.usage else 0,
            "output_tokens": response.usage.completion_tokens if response.usage else 0,
        }
        return LLMResponse(content, self.model, usage)

    def chat_with_tools(self, system_prompt, user_message, tools, max_tokens=1000,
                        temperature=0.3, **kwargs):
        # Convert Anthropic tool format to OpenAI function format
        functions = []
        for tool in tools:
            functions.append({
                "type": "function",
                "function": {
                    "name": tool["name"],
                    "description": tool.get("description", ""),
                    "parameters": tool.get("input_schema", {}),
                },
            })

        response = self._client.chat.completions.create(
            model=self.model,
            max_tokens=max_tokens,
            temperature=temperature,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_message},
            ],
            tools=functions,
            tool_choice="required",
        )
        msg = response.choices[0].message
        if msg.tool_calls:
            content = msg.tool_calls[0].function.arguments
            usage = {
                "input_tokens": response.usage.prompt_tokens if response.usage else 0,
                "output_tokens": response.usage.completion_tokens if response.usage else 0,
            }
            return LLMResponse(content, self.model, usage)
        return self.chat(system_prompt, user_message, max_tokens, temperature)


# ---------------------------------------------------------------------------
# Google (Gemini)
# ---------------------------------------------------------------------------

class GoogleClient(LLMClient):
    """Gemini models via the Google Generative AI API."""

    def __init__(self, model: str = "gemini-2.0-flash", **kwargs):
        super().__init__(model, **kwargs)
        try:
            import google.generativeai as genai
            genai.configure(api_key=kwargs.get("api_key", os.environ.get("GOOGLE_API_KEY")))
            self._genai = genai
        except ImportError:
            raise ImportError("google-generativeai package required: pip install google-generativeai")

    @property
    def provider_name(self) -> str:
        return "google"

    def chat(self, system_prompt, user_message, max_tokens=1000, temperature=0.3, **kwargs):
        model = self._genai.GenerativeModel(
            self.model,
            system_instruction=system_prompt,
        )
        response = model.generate_content(
            user_message,
            generation_config=self._genai.types.GenerationConfig(
                max_output_tokens=max_tokens,
                temperature=temperature,
            ),
        )
        return LLMResponse(response.text, self.model)

    def chat_with_tools(self, system_prompt, user_message, tools, max_tokens=1000,
                        temperature=0.3, **kwargs):
        # Gemini structured output — fall back to plain chat with JSON instruction
        enhanced_prompt = (
            f"{user_message}\n\nRespond with valid JSON matching the required schema."
        )
        return self.chat(system_prompt, enhanced_prompt, max_tokens, temperature)


# ---------------------------------------------------------------------------
# Ollama (local — zero-cost inference via Hermes-3 / Llama family)
# ---------------------------------------------------------------------------

class OllamaClient(LLMClient):
    """Local LLM via the Ollama REST API.

    Designed for the same Hermes-3 model the crypto-side sentiment engine
    uses, so the GPU stays warm. Tool-use is implemented via JSON-schema
    prompting (Hermes-3 follows schemas reliably) — Ollama's /api/chat
    doesn't have native function-calling.
    """

    DEFAULT_BASE_URL = "http://localhost:11434"

    def __init__(self, model: str = "hermes3:70b", **kwargs):
        super().__init__(model, **kwargs)
        self.base_url = kwargs.get(
            "base_url", os.environ.get("OLLAMA_BASE_URL", self.DEFAULT_BASE_URL),
        ).rstrip("/")
        self.timeout = float(kwargs.get("timeout", os.environ.get("OLLAMA_TIMEOUT_S", "180")))
        try:
            import requests  # noqa: F401  pylint: disable=unused-import
            self._lib_ok = True
        except ImportError:
            self._lib_ok = False
            logger.warning("OllamaClient: `requests` not available; chat() will raise")

    @property
    def provider_name(self) -> str:
        return "ollama"

    def chat(self, system_prompt, user_message, max_tokens=1000, temperature=0.3, **kwargs):
        if not self._lib_ok:
            raise RuntimeError("OllamaClient requires the `requests` package")
        import requests
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_message},
            ],
            "stream": False,
            "options": {
                "temperature": float(temperature),
                "num_predict": int(max_tokens),
            },
        }
        keep_alive = kwargs.get("keep_alive")
        if keep_alive is not None:
            payload["keep_alive"] = keep_alive
        resp = requests.post(
            f"{self.base_url}/api/chat", json=payload, timeout=self.timeout,
        )
        resp.raise_for_status()
        data = resp.json()
        content = (data.get("message") or {}).get("content", "") or ""
        usage = {
            "input_tokens": int(data.get("prompt_eval_count") or 0),
            "output_tokens": int(data.get("eval_count") or 0),
        }
        return LLMResponse(content, self.model, usage)

    def chat_with_tools(self, system_prompt, user_message, tools, max_tokens=1000,
                        temperature=0.3, **kwargs):
        # Ollama doesn't have an Anthropic-equivalent "tool_choice=any" guarantee,
        # but Hermes-3 reliably follows JSON-schema instructions. Embed the
        # schema in the prompt and request a JSON object response.
        if not tools:
            return self.chat(system_prompt, user_message, max_tokens, temperature)
        # Use the first tool as the target schema (matches AnthropicClient behavior)
        tool = tools[0]
        schema = tool.get("input_schema", {})
        enhanced_user = (
            f"{user_message}\n\n"
            f"Respond with a single JSON object matching this exact schema "
            f"(no prose, no markdown, no code-fence):\n"
            f"{json.dumps(schema, indent=2)}"
        )
        # Tighten temperature for JSON faithfulness
        return self.chat(
            system_prompt, enhanced_user, max_tokens=max_tokens,
            temperature=min(0.2, float(temperature)), **kwargs,
        )


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------

_PROVIDERS = {
    "ollama": OllamaClient,
    "anthropic": AnthropicClient,
    "openai": OpenAIClient,
    "google": GoogleClient,
}


def get_llm_client(
    provider: str | None = None,
    model: str | None = None,
    **kwargs,
) -> LLMClient:
    """
    Create an LLM client for the specified provider.

    Args:
        provider: One of "anthropic", "openai", "google". Defaults to env SHARK_LLM_PROVIDER or "anthropic".
        model: Model name. Defaults to provider-specific default.
        **kwargs: Additional kwargs passed to the client (e.g., api_key).

    Returns:
        LLMClient instance.
    """
    provider = provider or os.environ.get("SHARK_LLM_PROVIDER", "ollama")
    provider = provider.lower()

    cls = _PROVIDERS.get(provider)
    if cls is None:
        raise ValueError(
            f"Unknown LLM provider: {provider}. "
            f"Available: {', '.join(_PROVIDERS.keys())}"
        )

    if model is None:
        model_defaults = {
            "ollama":    os.environ.get("OLLAMA_MODEL", "hermes3:70b"),
            "anthropic": os.environ.get("CLAUDE_MODEL", "claude-sonnet-4-6"),
            "openai":    os.environ.get("OPENAI_MODEL", "gpt-4o-mini"),
            "google":    os.environ.get("GOOGLE_MODEL", "gemini-2.0-flash"),
        }
        model = model_defaults.get(provider, "")

    logger.info("Creating LLM client: provider=%s model=%s", provider, model)
    return cls(model=model, **kwargs)


# ---------------------------------------------------------------------------
# Migration shim — lets agents go from "anthropic.messages.create()" to the
# provider-agnostic abstraction with one line of code. Minimises churn while
# we move all 7 shark agents off the bare Anthropic SDK call path.
# ---------------------------------------------------------------------------


def _resolve_ollama_model(role: str, tier: str) -> str:
    """Pick the Ollama model honouring per-role overrides + legacy var names."""
    if tier == "fast":
        return (
            os.environ.get(f"SHARK_{role.upper()}_LLM_MODEL", "")
            or os.environ.get("OLLAMA_FAST_MODEL", "")
            or os.environ.get("OLLAMA_MODEL_FAST", "")     # legacy/crypto name
            or "hermes3:8b"
        )
    return (
        os.environ.get(f"SHARK_{role.upper()}_LLM_MODEL", "")
        or os.environ.get("OLLAMA_MODEL", "")
        or os.environ.get("OLLAMA_MODEL_DEEP", "")         # legacy/crypto name
        or "hermes3:70b"
    )


def _emit_tracker(
    agent: str, model: str, provider: str, elapsed: float,
    usage: dict, tier: str, role: str,
) -> None:
    """Best-effort tracker emit — never let tracking break the agent path."""
    try:
        from shark.llm.tracker import get_tracker
        get_tracker().record(
            agent=agent, model=model, provider=provider,
            latency_seconds=elapsed,
            prompt_tokens=int(
                (usage or {}).get("input_tokens", 0)
                or (usage or {}).get("prompt_tokens", 0)
                or 0
            ),
            completion_tokens=int(
                (usage or {}).get("output_tokens", 0)
                or (usage or {}).get("completion_tokens", 0)
                or 0
            ),
            tier=tier, role=role,
        )
    except Exception as exc:  # pragma: no cover
        logger.debug("tracker emit failed: %s", exc)


_FALLBACK_ALERT_LAST_TS = 0.0
_FALLBACK_ALERT_COOLDOWN_S = 300  # 5-minute dedup so we don't spam Slack


def _maybe_alert_fallback_active(agent: str, primary_status: dict) -> None:
    """Slack-alert when we're using Anthropic — we're paying real money now."""
    global _FALLBACK_ALERT_LAST_TS
    now = time.time()
    if now - _FALLBACK_ALERT_LAST_TS < _FALLBACK_ALERT_COOLDOWN_S:
        return
    _FALLBACK_ALERT_LAST_TS = now
    try:
        # Slack via direct webhook — avoids importing freqtrade-side modules
        # that may not be on PYTHONPATH for the shark process.
        import json as _json
        webhook = os.environ.get("SLACK_WEBHOOK_URL", "")
        if not webhook:
            return
        import requests
        text = (
            f":rotating_light: *LLM failover active — paying for Anthropic*\n"
            f"Ollama breaker `{primary_status.get('name', '?')}` is "
            f"`{primary_status.get('state', '?')}` "
            f"(p95 latency {primary_status.get('p95_latency_s')}s, "
            f"{primary_status.get('failure_count', 0)} failures). "
            f"Agent `{agent}` just used the Anthropic API. "
            f"Investigate Ollama on the Spark."
        )
        requests.post(webhook, json={"text": text}, timeout=5)
    except Exception:
        pass


def chat_json(
    system_prompt: str,
    user_message: str,
    *,
    max_tokens: int = 1000,
    temperature: float = 0.3,
    role: str = "default",
    tier: str = "deep",
    agent: str = "unknown",
    schema_hint: str = "",
) -> tuple[str, dict[str, int], str]:
    """LLM call with automatic Ollama → Anthropic failover.

    Order of attempts:
      1. Ollama (unless its circuit breaker is OPEN)
      2. Anthropic (when ANTHROPIC_API_KEY is set and its breaker isn't OPEN)
      3. If both fail, raise RuntimeError so the caller's agent-side
         deterministic fallback can take over.

    The breaker is per (provider, tier) — the 70B can fail without affecting
    8B-tier calls. Latency-based trip means slow responses also fail over;
    a slow response is functionally an outage for live trading.

    Args
      role:   "default" | "debate" | "arbiter" | "risk" — env-var override key.
      tier:   "fast" (8B/cheaper) or "deep" (70B/quality).
      agent:  caller's name (for tracker stats).
    """
    from shark.llm.circuit_breaker import get_breaker

    role = (role or "default").lower()
    tier = (tier or "deep").lower()

    # ── Resolve provider preference + models ──────────────────────────
    requested_provider = (
        os.environ.get(f"SHARK_{role.upper()}_LLM_PROVIDER", "")
        or os.environ.get("SHARK_LLM_PROVIDER", "")
        or "ollama"
    ).lower()

    ollama_model = _resolve_ollama_model(role, tier)
    anthropic_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    anthropic_model = os.environ.get("CLAUDE_MODEL", "claude-sonnet-4-6")
    has_anthropic = bool(anthropic_key)

    # Per-role explicit Anthropic-only routing skips the failover dance
    if requested_provider == "anthropic" and has_anthropic:
        client = get_llm_client(provider="anthropic", model=anthropic_model)
        return _direct_call(client, system_prompt, user_message, schema_hint,
                            max_tokens, temperature, agent, tier, role)

    # ── Build prompt; schema hint helps Ollama (Anthropic does JSON natively) ──
    user = user_message
    if schema_hint:
        user = (
            f"{user_message}\n\n"
            f"Respond with a single JSON object matching this exact schema "
            f"(no prose, no markdown, no code-fence):\n{schema_hint}"
        )

    primary_breaker = get_breaker(f"ollama:{tier}", tier=tier)
    fallback_breaker = get_breaker(f"anthropic:{tier}", tier=tier)
    last_error: Optional[Exception] = None

    # ── Attempt 1: Ollama (primary) ───────────────────────────────────
    can_primary, reason = primary_breaker.can_execute()
    if can_primary:
        try:
            client = get_llm_client(provider="ollama", model=ollama_model)
            start = time.monotonic()
            resp = client.chat(
                system_prompt=system_prompt, user_message=user,
                max_tokens=max_tokens, temperature=temperature,
            )
            elapsed = time.monotonic() - start
            primary_breaker.record_success(elapsed)
            _emit_tracker(agent, client.model, client.provider_name,
                          elapsed, resp.usage, tier, role)
            return resp.content, resp.usage, client.model
        except Exception as exc:
            primary_breaker.record_failure(str(exc))
            last_error = exc
            logger.warning(
                "Ollama call failed (agent=%s): %s — trying Anthropic fallback",
                agent, exc,
            )
    else:
        logger.info(
            "Ollama breaker for %s tier is %s — using Anthropic fallback",
            tier, reason,
        )

    # ── Attempt 2: Anthropic (fallback) ───────────────────────────────
    if not has_anthropic:
        if last_error:
            raise RuntimeError(
                f"Ollama unavailable and ANTHROPIC_API_KEY not configured. "
                f"Last Ollama error: {last_error}"
            ) from last_error
        raise RuntimeError(
            f"Ollama breaker {primary_breaker.get_status()['state']} "
            f"and ANTHROPIC_API_KEY not configured — no LLM path available."
        )

    can_fallback, _ = fallback_breaker.can_execute()
    if not can_fallback:
        raise RuntimeError(
            f"BOTH PROVIDERS DOWN. Ollama: {primary_breaker.get_status()['state']}, "
            f"Anthropic: {fallback_breaker.get_status()['state']}. "
            f"Last Ollama error: {last_error}"
        )

    try:
        client = get_llm_client(provider="anthropic", model=anthropic_model)
        start = time.monotonic()
        # Anthropic does JSON output natively — strip the schema hint suffix
        # since the AnthropicClient.chat() doesn't need it.
        resp = client.chat(
            system_prompt=system_prompt, user_message=user_message,
            max_tokens=max_tokens, temperature=temperature,
        )
        elapsed = time.monotonic() - start
        fallback_breaker.record_success(elapsed)
        _maybe_alert_fallback_active(agent, primary_breaker.get_status())
        _emit_tracker(agent, client.model, client.provider_name,
                      elapsed, resp.usage, tier, role)
        return resp.content, resp.usage, client.model
    except Exception as exc:
        fallback_breaker.record_failure(str(exc))
        raise RuntimeError(
            f"BOTH PROVIDERS FAILED. Ollama: {last_error}. Anthropic: {exc}"
        ) from exc


def _direct_call(
    client: LLMClient, system_prompt: str, user_message: str,
    schema_hint: str, max_tokens: int, temperature: float,
    agent: str, tier: str, role: str,
) -> tuple[str, dict, str]:
    """Single-provider call with no failover — used when operator pinned a
    specific provider via SHARK_*_LLM_PROVIDER=anthropic."""
    user = user_message
    if schema_hint and client.provider_name == "ollama":
        user = (
            f"{user_message}\n\n"
            f"Respond with a single JSON object matching this exact schema "
            f"(no prose, no markdown, no code-fence):\n{schema_hint}"
        )
    start = time.monotonic()
    resp = client.chat(
        system_prompt=system_prompt, user_message=user,
        max_tokens=max_tokens, temperature=temperature,
    )
    elapsed = time.monotonic() - start
    _emit_tracker(agent, client.model, client.provider_name,
                  elapsed, resp.usage, tier, role)
    return resp.content, resp.usage, client.model


# Role-based client helpers
def get_debate_client(**kwargs) -> LLMClient:
    """Get the LLM client configured for debate rounds (can be a cheaper model)."""
    provider = os.environ.get("SHARK_DEBATE_LLM_PROVIDER",
                              os.environ.get("SHARK_LLM_PROVIDER", "ollama"))
    model = os.environ.get("SHARK_DEBATE_LLM_MODEL")
    return get_llm_client(provider=provider, model=model, **kwargs)


def get_arbiter_client(**kwargs) -> LLMClient:
    """Get the LLM client for the decision arbiter (highest quality)."""
    provider = os.environ.get("SHARK_ARBITER_LLM_PROVIDER",
                              os.environ.get("SHARK_LLM_PROVIDER", "ollama"))
    model = os.environ.get("SHARK_ARBITER_LLM_MODEL")
    return get_llm_client(provider=provider, model=model, **kwargs)


def get_risk_client(**kwargs) -> LLMClient:
    """Get the LLM client for risk review."""
    provider = os.environ.get("SHARK_RISK_LLM_PROVIDER",
                              os.environ.get("SHARK_LLM_PROVIDER", "ollama"))
    model = os.environ.get("SHARK_RISK_LLM_MODEL")
    return get_llm_client(provider=provider, model=model, **kwargs)
