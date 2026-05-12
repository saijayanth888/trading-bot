"""Thin Ollama HTTP client for Hermes modules.

Wraps just the two endpoints we need:

* ``POST /api/generate`` — single-shot prompt → text completion.
* ``GET  /api/ps``       — list resident models (used by healthcheck +
  gpu-yield observability).

Network failures return ``None`` / ``False`` rather than raising — every
Hermes module is expected to degrade gracefully when the LLM is down.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

try:
    import httpx
except Exception:  # pragma: no cover
    httpx = None  # type: ignore[assignment]


@dataclass
class OllamaClient:
    base_url: str = "http://localhost:11434"
    timeout_seconds: float = 60.0
    logger: logging.Logger = field(
        default_factory=lambda: logging.getLogger("quanta_core.hermes.ollama")
    )

    def generate(self, model: str, prompt: str, system: str | None = None) -> str | None:
        """Single-shot completion. Returns ``None`` on any error."""

        if httpx is None:  # pragma: no cover
            self.logger.warning("httpx unavailable")
            return None
        body: dict[str, Any] = {
            "model": model,
            "prompt": prompt,
            "stream": False,
            "options": {"temperature": 0.2, "num_ctx": 4096},
        }
        if system:
            body["system"] = system
        try:
            resp = httpx.post(
                f"{self.base_url}/api/generate",
                json=body,
                timeout=self.timeout_seconds,
            )
            if resp.status_code != 200:
                self.logger.warning(
                    "ollama generate non-200: %d %s",
                    resp.status_code,
                    resp.text[:120],
                )
                return None
            data = resp.json()
            response = data.get("response")
            if isinstance(response, str):
                return response.strip()
            return None
        except Exception as exc:
            self.logger.warning("ollama generate failed: %s", exc)
            return None

    def list_resident(self) -> list[str]:
        """Return the names of currently-resident models."""

        if httpx is None:  # pragma: no cover
            return []
        try:
            resp = httpx.get(
                f"{self.base_url}/api/ps", timeout=self.timeout_seconds
            )
            if resp.status_code != 200:
                return []
            data = resp.json()
            models = data.get("models", []) or []
            return [m.get("name", "") for m in models if m.get("name")]
        except Exception as exc:
            self.logger.warning("ollama /api/ps failed: %s", exc)
            return []

    def ping(self) -> tuple[bool, float, list[str]]:
        """Return ``(ok, latency_ms, resident_models)``.

        ``ok=False`` covers both connection refused and non-2xx.
        """

        if httpx is None:  # pragma: no cover
            return (False, 0.0, [])
        import time

        start = time.monotonic()
        try:
            resp = httpx.get(
                f"{self.base_url}/api/ps", timeout=self.timeout_seconds
            )
            latency = (time.monotonic() - start) * 1000.0
            ok = resp.status_code == 200
            resident: list[str] = []
            if ok:
                try:
                    data = resp.json()
                    resident = [
                        m.get("name", "")
                        for m in (data.get("models") or [])
                        if m.get("name")
                    ]
                except Exception:
                    pass
            return (ok, latency, resident)
        except Exception as exc:
            self.logger.warning("ollama ping failed: %s", exc)
            return (False, 0.0, [])


__all__ = ["OllamaClient"]
