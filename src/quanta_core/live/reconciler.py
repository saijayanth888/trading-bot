"""Position reconciler — 60s REST sweep against in-memory state.

Alpaca does not expose WebSocket sequence numbers, so we cannot detect a
dropped position update from the stream alone. The reconciler closes the
gap by polling ``Exchange.list_positions()`` every ``interval_seconds``
(default 60) and diffing against the in-memory ``PositionState``.

Discrepancies above the configured epsilon (default 1e-8) trigger:

1. A Slack ``:warning:`` via the ``Notifier``.
2. A row appended to ``~/.quanta/logs/anomalies.jsonl`` via
   ``observability.ledger_anomaly.record_anomaly``.

The reconciler NEVER auto-corrects the in-memory state. The operator is
expected to investigate and either:

- accept the venue's view (the bug is in our event handling), or
- accept our view (the bug is at the venue / manual trade).

Either way the action is manual. The reconciler is a watchdog, not a
self-healer.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING

import anyio

from quanta_core.observability.ledger_anomaly import record_anomaly

if TYPE_CHECKING:
    from quanta_core.exchanges.base import Exchange
    from quanta_core.observability.notifier import Notifier
    from quanta_core.util.types import Position, Symbol

_log = logging.getLogger(__name__)


DEFAULT_INTERVAL_SECONDS: float = 60.0
DEFAULT_EPSILON: Decimal = Decimal("1e-8")


@dataclass
class PositionState:
    """In-memory ``Symbol -> qty`` view maintained by the live engine.

    The engine increments / decrements this on every fill; the reconciler
    reads it. We keep the surface minimal so the reconciler can be unit
    tested without spinning up the rest of the engine.
    """

    _book: dict[str, Decimal] = field(default_factory=dict)

    def set(self, symbol: Symbol, qty: Decimal) -> None:
        """Overwrite the recorded qty for one symbol."""

        self._book[str(symbol)] = qty

    def apply_fill_delta(self, symbol: Symbol, delta: Decimal) -> None:
        """Apply a signed qty delta on top of the current state."""

        key = str(symbol)
        self._book[key] = self._book.get(key, Decimal("0")) + delta

    def snapshot(self) -> dict[str, Decimal]:
        """Return a copy of the underlying book."""

        return dict(self._book)


@dataclass
class _Drift:
    """One row of the diff between venue + in-memory state."""

    symbol: str
    venue_qty: Decimal
    local_qty: Decimal

    @property
    def gap(self) -> Decimal:
        return self.venue_qty - self.local_qty


@dataclass
class ReconcilerMetrics:
    """Counters exposed for observability."""

    sweeps_completed: int = 0
    sweeps_failed: int = 0
    drift_events: int = 0
    last_gap_count: int = 0


class Reconciler:
    """Periodic REST snapshot vs in-memory diff.

    Parameters
    ----------
    exchange
        Source of REST snapshots.
    state
        In-memory position view maintained by the engine.
    notifier
        Slack/Telegram alert sink.
    anomaly_path
        Path to the append-only JSONL anomalies file.
    interval_seconds
        Sweep cadence; default 60.
    epsilon
        Quantity diff threshold below which a drift is treated as noise.
    """

    def __init__(
        self,
        exchange: Exchange,
        state: PositionState,
        notifier: Notifier,
        anomaly_path: Path,
        *,
        interval_seconds: float = DEFAULT_INTERVAL_SECONDS,
        epsilon: Decimal = DEFAULT_EPSILON,
    ) -> None:
        self.exchange = exchange
        self.state = state
        self.notifier = notifier
        self.anomaly_path = anomaly_path
        self.interval_seconds = interval_seconds
        self.epsilon = epsilon
        self.metrics = ReconcilerMetrics()

    async def run(self, *, cancel_event: anyio.Event | None = None) -> None:
        """Run sweeps in a loop until cancelled.

        Parameters
        ----------
        cancel_event
            Optional event used by the engine to request a stop. When set,
            the loop exits after the current sweep completes.
        """

        while True:
            if cancel_event is not None and cancel_event.is_set():
                return
            await self.sweep_once()
            try:
                with anyio.fail_after(self.interval_seconds):
                    if cancel_event is not None:
                        await cancel_event.wait()
                        return
                    # No cancel event provided — just sleep the interval.
                    await anyio.sleep(self.interval_seconds)
            except TimeoutError:
                continue

    async def sweep_once(self) -> list[_Drift]:
        """Run one reconciliation sweep. Returns the list of drift rows."""

        try:
            venue_positions: list[Position] = await self.exchange.list_positions()
        except Exception:
            self.metrics.sweeps_failed += 1
            _log.exception("reconciler.sweep_failed")
            return []

        drifts = self._diff(venue_positions, self.state.snapshot())
        self.metrics.sweeps_completed += 1
        self.metrics.last_gap_count = len(drifts)

        for drift in drifts:
            self.metrics.drift_events += 1
            await self._alert(drift)
            self._write_anomaly(drift)

        return drifts

    # ----- private helpers -----

    def _diff(
        self,
        venue_positions: list[Position],
        local: dict[str, Decimal],
    ) -> list[_Drift]:
        """Return drift rows where |venue_qty - local_qty| > epsilon."""

        drifts: list[_Drift] = []
        seen: set[str] = set()
        for pos in venue_positions:
            key = str(pos.symbol)
            seen.add(key)
            local_qty = local.get(key, Decimal("0"))
            if abs(pos.qty - local_qty) > self.epsilon:
                drifts.append(_Drift(symbol=key, venue_qty=pos.qty, local_qty=local_qty))

        # Symbols we think we hold but the venue does not report.
        for key, qty in local.items():
            if key in seen:
                continue
            if abs(qty) > self.epsilon:
                drifts.append(_Drift(symbol=key, venue_qty=Decimal("0"), local_qty=qty))

        return drifts

    async def _alert(self, drift: _Drift) -> None:
        subject = ":warning: reconciler: position gap"
        body = (
            f"symbol={drift.symbol} "
            f"venue_qty={drift.venue_qty} "
            f"local_qty={drift.local_qty} "
            f"gap={drift.gap}"
        )
        try:
            await self.notifier.warning(subject, body)
        except Exception:
            _log.exception("reconciler.notifier_failed")

    def _write_anomaly(self, drift: _Drift) -> None:
        try:
            record_anomaly(
                self.anomaly_path,
                kind="position_gap",
                detail={
                    "symbol": drift.symbol,
                    "venue_qty": str(drift.venue_qty),
                    "local_qty": str(drift.local_qty),
                    "gap": str(drift.gap),
                },
            )
        except OSError:
            _log.exception("reconciler.anomaly_write_failed")


__all__ = [
    "DEFAULT_EPSILON",
    "DEFAULT_INTERVAL_SECONDS",
    "PositionState",
    "Reconciler",
    "ReconcilerMetrics",
]
