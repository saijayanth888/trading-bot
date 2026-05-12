"""Exchange ABC contract — interface for the live engine.

This file is **interface-only**. The Alpaca and Coinbase implementations are
owned by the exchanges build agent (sibling worktree). The live engine and
the tests import only this module so we never accidentally pull a real SDK
into the unit-test path.

Interface assumptions (locked here so the sibling agent can fulfill them):

- ``ExchangeStream`` is an async-iterable producer of ``StreamEvent`` objects.
  Each event carries exactly one of ``tick`` or ``fill`` (mutually exclusive
  for v0 — quotes/news will be additional payload kinds in later revisions).
- ``Exchange.list_positions()`` returns a snapshot from the venue REST surface.
  The reconciler calls this every 60 seconds.
- ``Exchange.name`` is a stable identifier matching ``Venue``.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from quanta_core.util.types import Fill, Position, Tick, Venue


@dataclass(frozen=True)
class StreamEvent:
    """A single normalised event off an exchange stream.

    Exactly one of ``tick`` / ``fill`` is populated. The discriminant is the
    field that is not ``None``; consumers branch on identity rather than
    introspecting a ``kind`` string (smaller mistake surface).
    """

    tick: Tick | None = None
    fill: Fill | None = None


class ExchangeStream(ABC):
    """Async stream of market + user events from one venue."""

    @abstractmethod
    def __aiter__(self) -> AsyncIterator[StreamEvent]:
        """Return an async iterator over StreamEvents.

        The iterator MUST yield until ``aclose()`` is called or the
        underlying connection is permanently closed by the consumer.
        Transient disconnects are handled by the implementation
        (reconnect + replay subscriptions); they are invisible to the
        consumer.
        """

    @abstractmethod
    async def aclose(self) -> None:
        """Cleanly shut down the stream and release resources."""


class Exchange(ABC):
    """Venue-agnostic trading + data surface.

    Concrete implementations live in ``quanta_core.exchanges.{alpaca,coinbase,paper}``
    and are built by the exchanges agent. The live module only ever calls
    the methods on this ABC.
    """

    name: Venue

    @abstractmethod
    async def open(self) -> ExchangeStream:
        """Open the data + user stream. Returns once authenticated."""

    @abstractmethod
    async def list_positions(self) -> list[Position]:
        """REST snapshot of open positions.

        Called by the reconciler on a 60-second cadence. Must include every
        venue position even those opened outside our system (manual venue
        trades show up as discrepancies in the reconciler).
        """

    @abstractmethod
    async def close(self) -> None:
        """Close REST + WS clients. Safe to call multiple times."""


__all__ = ["Exchange", "ExchangeStream", "StreamEvent"]
