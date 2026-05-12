"""Notifier protocol used by the live module.

The live engine emits two classes of alert:

- **Stale feed** — heartbeat watchdog tripped, Slack at ``:warning:``.
- **Reconciliation drift** — REST positions diverge from in-memory state,
  Slack at ``:warning:`` + a ledger anomaly row.

The concrete Slack / Telegram clients live in Hermes (Layer 8). Within
quanta_core we depend on this minimal Protocol so unit tests can supply a
``NullNotifier`` or a recording fake without pulling HTTP libs.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class Notifier(Protocol):
    """Tiny alerting surface — implementations push to Slack/Telegram.

    Implementations MUST be non-blocking on a slow channel: if Slack is
    rate-limited, the implementation should drop the alert (log it) rather
    than back-pressure the live engine. We never want an alert path to
    take down trading.
    """

    async def warning(self, subject: str, body: str) -> None:
        """Emit a warning-level alert."""

    async def info(self, subject: str, body: str) -> None:
        """Emit an info-level alert."""


class NullNotifier:
    """Notifier that swallows every alert. Useful for tests + paper-mode."""

    async def warning(self, subject: str, body: str) -> None:
        """No-op."""
        return

    async def info(self, subject: str, body: str) -> None:
        """No-op."""
        return


__all__ = ["Notifier", "NullNotifier"]
