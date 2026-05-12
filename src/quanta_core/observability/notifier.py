"""Slack notifier with severity routing + rate-limit dedup.

The notifier is fire-and-forget: callers ``await notifier.notify(...)`` and
the call returns once the request has been dispatched (or the dedup window
suppressed it). Network errors are caught + logged, never raised, so a Slack
outage cannot trip the trading engine.

Two implementations:

* :class:`SlackNotifier` — POSTs to a webhook URL via :mod:`httpx`.
* :class:`LogOnlyNotifier` — writes to ``structlog``; used in paper mode and
  when ``QUANTA_NOTIFIER=log`` is set.

Both honour the ``Severity`` enum and the dedup-key window (default 60s).
"""

from __future__ import annotations

import asyncio
import enum
import logging
import time
from abc import ABC, abstractmethod
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Final

import httpx

logger = logging.getLogger("quanta_core.observability.notifier")

_DEFAULT_DEDUP_WINDOW_S: Final[float] = 60.0
_DEFAULT_TIMEOUT_S: Final[float] = 5.0
_DEFAULT_RETRIES: Final[int] = 2


class Severity(enum.StrEnum):
    """Notification severity levels.

    Routing rule (default): ``INFO`` and ``WARN`` go to the configured
    webhook; ``ERROR`` and ``CRITICAL`` additionally trigger a stronger
    formatting (red bar + at-channel) on Slack.
    """

    INFO = "info"
    WARN = "warn"
    ERROR = "error"
    CRITICAL = "critical"


_SEVERITY_COLORS: Final[Mapping[Severity, str]] = {
    Severity.INFO: "#2eb886",
    Severity.WARN: "#daa038",
    Severity.ERROR: "#d63031",
    Severity.CRITICAL: "#9b2c2c",
}


class NotifierError(Exception):
    """Raised when a notifier is misconfigured at construction time.

    Runtime delivery failures are caught + logged, NOT raised.
    """


@dataclass(slots=True)
class _DedupCache:
    """In-memory dedup keyed by ``(dedup_key, severity)``."""

    window_s: float
    last_seen: dict[tuple[str, Severity], float]

    def should_send(self, dedup_key: str | None, severity: Severity) -> bool:
        if not dedup_key:
            return True
        now = time.monotonic()
        cache_key = (dedup_key, severity)
        last = self.last_seen.get(cache_key)
        if last is not None and (now - last) < self.window_s:
            return False
        self.last_seen[cache_key] = now
        return True

    def reset(self) -> None:
        self.last_seen.clear()


class Notifier(ABC):
    """Abstract notifier — every concrete impl must respect dedup + severity."""

    def __init__(
        self,
        *,
        dedup_window_s: float = _DEFAULT_DEDUP_WINDOW_S,
    ) -> None:
        if dedup_window_s < 0:
            raise NotifierError(f"dedup_window_s must be >= 0, got {dedup_window_s!r}")
        self._dedup = _DedupCache(window_s=dedup_window_s, last_seen={})

    async def notify(
        self,
        message: str,
        *,
        severity: Severity = Severity.INFO,
        dedup_key: str | None = None,
        context: Mapping[str, Any] | None = None,
    ) -> bool:
        """Dispatch a notification.

        Parameters
        ----------
        message:
            Free-form human-readable text. Slack formatting is supported.
        severity:
            One of :class:`Severity`.
        dedup_key:
            Stable key — repeated calls with the same key + severity within
            the dedup window are suppressed. ``None`` disables dedup.
        context:
            Optional structured payload appended as Slack attachment fields.

        Returns
        -------
        bool
            ``True`` if the message was delivered (or queued); ``False`` if
            dedup suppressed it OR the underlying transport raised + was
            logged.
        """
        if not isinstance(severity, Severity):
            raise NotifierError(f"severity must be a Severity, got {type(severity).__name__}")
        if not self._dedup.should_send(dedup_key, severity):
            logger.debug(
                "notifier_dedup_suppressed",
                extra={"dedup_key": dedup_key, "severity": severity.value},
            )
            return False
        try:
            await self._deliver(message, severity, context or {})
        except Exception as exc:
            logger.warning(
                "notifier_delivery_failed",
                extra={
                    "error": str(exc),
                    "severity": severity.value,
                    "dedup_key": dedup_key,
                },
            )
            return False
        return True

    @abstractmethod
    async def _deliver(
        self,
        message: str,
        severity: Severity,
        context: Mapping[str, Any],
    ) -> None:
        """Implementation hook — actually send the notification."""

    async def warning(self, subject: str, body: str) -> bool:
        """Route a (subject, body) WARN-level alert through :meth:`notify`.

        Convenience for the live engine + other call sites that pre-date the
        :meth:`notify` API. ``dedup_key`` defaults to ``subject`` so identical
        subjects collapse inside the dedup window.
        """
        return await self.notify(
            f"{subject}\n{body}" if body else subject,
            severity=Severity.WARN,
            dedup_key=subject,
        )

    async def info(self, subject: str, body: str) -> bool:
        """INFO-level twin of :meth:`warning`."""
        return await self.notify(
            f"{subject}\n{body}" if body else subject,
            severity=Severity.INFO,
            dedup_key=subject,
        )

    def clear_dedup_cache(self) -> None:
        """Reset the dedup cache. Tests use this between cases."""
        self._dedup.reset()


class NullNotifier(Notifier):
    """No-op notifier — silently discards every message.

    Used in paper mode, unit tests, and as the default fallback when no
    webhook is configured. ``_deliver`` does nothing; ``dedup_window_s``
    defaults to 0 so back-to-back test calls aren't suppressed.
    """

    def __init__(self, *, dedup_window_s: float = 0.0) -> None:
        super().__init__(dedup_window_s=dedup_window_s)

    async def _deliver(
        self,
        message: str,
        severity: Severity,
        context: Mapping[str, Any],
    ) -> None:
        return None


class LogOnlyNotifier(Notifier):
    """Fallback notifier — writes to a logger instead of an external service.

    Used in paper-mode and when no webhook is configured. The log call
    carries enough structure (``severity``, ``context``) for the dashboard
    to render it the same way it renders Slack messages.
    """

    def __init__(
        self,
        *,
        dedup_window_s: float = _DEFAULT_DEDUP_WINDOW_S,
        log: logging.Logger | None = None,
    ) -> None:
        super().__init__(dedup_window_s=dedup_window_s)
        self._log = log or logger

    async def _deliver(
        self,
        message: str,
        severity: Severity,
        context: Mapping[str, Any],
    ) -> None:
        level = {
            Severity.INFO: logging.INFO,
            Severity.WARN: logging.WARNING,
            Severity.ERROR: logging.ERROR,
            Severity.CRITICAL: logging.CRITICAL,
        }[severity]
        self._log.log(
            level,
            "notifier_message: %s",
            message,
            extra={
                "severity": severity.value,
                "notifier_context": dict(context),
            },
        )


class SlackNotifier(Notifier):
    """Webhook-based Slack notifier with bounded exponential retry.

    Parameters
    ----------
    webhook_url:
        Slack incoming-webhook URL.
    client:
        Optional pre-configured :class:`httpx.AsyncClient` (tests pass a
        mock transport). If ``None``, a default client is created lazily and
        owned by this notifier.
    timeout_s:
        Per-request timeout in seconds.
    max_retries:
        Number of retries on a 5xx / connection error (default 2).
    dedup_window_s:
        See :class:`Notifier`.
    """

    def __init__(
        self,
        webhook_url: str,
        *,
        client: httpx.AsyncClient | None = None,
        timeout_s: float = _DEFAULT_TIMEOUT_S,
        max_retries: int = _DEFAULT_RETRIES,
        dedup_window_s: float = _DEFAULT_DEDUP_WINDOW_S,
    ) -> None:
        super().__init__(dedup_window_s=dedup_window_s)
        if not webhook_url or not webhook_url.startswith(("http://", "https://")):
            raise NotifierError("SlackNotifier requires an http(s) webhook URL")
        if max_retries < 0:
            raise NotifierError(f"max_retries must be >= 0, got {max_retries!r}")
        if timeout_s <= 0:
            raise NotifierError(f"timeout_s must be > 0, got {timeout_s!r}")
        self._webhook_url = webhook_url
        self._timeout_s = timeout_s
        self._max_retries = max_retries
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(timeout=timeout_s)

    async def aclose(self) -> None:
        """Close the underlying httpx client (if owned)."""
        if self._owns_client:
            await self._client.aclose()

    async def _deliver(
        self,
        message: str,
        severity: Severity,
        context: Mapping[str, Any],
    ) -> None:
        payload = self._build_payload(message, severity, context)
        last_error: Exception | None = None
        for attempt in range(self._max_retries + 1):
            try:
                response = await self._client.post(
                    self._webhook_url, json=payload, timeout=self._timeout_s
                )
                if response.status_code < 500:
                    response.raise_for_status()
                    return
                last_error = httpx.HTTPStatusError(
                    f"slack {response.status_code}",
                    request=response.request,
                    response=response,
                )
            except (
                httpx.TransportError,
                httpx.HTTPStatusError,
            ) as exc:
                last_error = exc
            if attempt < self._max_retries:
                await asyncio.sleep(0.1 * (2**attempt))
        assert last_error is not None
        raise last_error

    def _build_payload(
        self,
        message: str,
        severity: Severity,
        context: Mapping[str, Any],
    ) -> dict[str, Any]:
        attachment: dict[str, Any] = {
            "color": _SEVERITY_COLORS[severity],
            "text": message,
            "fields": [{"title": k, "value": str(v), "short": True} for k, v in context.items()],
        }
        text = (
            f"<!channel> *[{severity.value.upper()}]* {message}"
            if severity in {Severity.ERROR, Severity.CRITICAL}
            else f"*[{severity.value.upper()}]* {message}"
        )
        return {"text": text, "attachments": [attachment]}
