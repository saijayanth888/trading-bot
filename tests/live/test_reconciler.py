"""Tests for ``quanta_core.live.reconciler``."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path

import anyio
import pytest

from quanta_core.exchanges.base import Exchange, ExchangeStream, StreamEvent
from quanta_core.live.reconciler import PositionState, Reconciler
from quanta_core.util.types import Position, Symbol

# ----- test doubles -----


@dataclass
class _RecordingNotifier:
    warnings: list[tuple[str, str]] = field(default_factory=list)
    infos: list[tuple[str, str]] = field(default_factory=list)

    async def warning(self, subject: str, body: str) -> None:
        self.warnings.append((subject, body))

    async def info(self, subject: str, body: str) -> None:
        self.infos.append((subject, body))


class _StubStream(ExchangeStream):
    def __aiter__(self) -> AsyncIterator[StreamEvent]:
        async def _gen() -> AsyncIterator[StreamEvent]:
            return
            yield  # pragma: no cover - unreachable but makes it a generator

        return _gen()

    async def aclose(self) -> None:
        return None


class _StubExchange(Exchange):
    name = "paper"

    def __init__(self, positions: list[Position]) -> None:
        self._positions = positions
        self.list_calls = 0
        self.raise_on_list: BaseException | None = None

    async def open(self) -> ExchangeStream:
        return _StubStream()

    async def list_positions(self) -> list[Position]:
        self.list_calls += 1
        if self.raise_on_list is not None:
            raise self.raise_on_list
        return list(self._positions)

    async def close(self) -> None:
        return None


def _pos(symbol: str, qty: str) -> Position:
    return Position(
        symbol=Symbol(symbol),
        qty=Decimal(qty),
        avg_price=Decimal("100"),
        venue="paper",
    )


# ----- tests -----


@pytest.mark.anyio
async def test_sweep_no_drift_emits_no_alert(tmp_path: Path) -> None:
    exchange = _StubExchange([_pos("AAPL", "10")])
    notifier = _RecordingNotifier()
    state = PositionState()
    state.set(Symbol("AAPL"), Decimal("10"))
    reconciler = Reconciler(
        exchange=exchange,
        state=state,
        notifier=notifier,
        anomaly_path=tmp_path / "anomalies.jsonl",
    )
    drifts = await reconciler.sweep_once()
    assert drifts == []
    assert notifier.warnings == []
    assert reconciler.metrics.sweeps_completed == 1


@pytest.mark.anyio
async def test_sweep_detects_qty_gap(tmp_path: Path) -> None:
    exchange = _StubExchange([_pos("AAPL", "15")])
    notifier = _RecordingNotifier()
    state = PositionState()
    state.set(Symbol("AAPL"), Decimal("10"))
    reconciler = Reconciler(
        exchange=exchange,
        state=state,
        notifier=notifier,
        anomaly_path=tmp_path / "anomalies.jsonl",
    )
    drifts = await reconciler.sweep_once()
    assert len(drifts) == 1
    assert notifier.warnings, "expected slack warning"
    subject, body = notifier.warnings[0]
    assert ":warning:" in subject
    assert "AAPL" in body
    assert "gap=5" in body
    # Anomaly row was written.
    rows = (tmp_path / "anomalies.jsonl").read_text().splitlines()
    assert len(rows) == 1
    parsed = json.loads(rows[0])
    assert parsed["kind"] == "position_gap"
    assert parsed["detail"]["symbol"] == "AAPL"
    assert parsed["detail"]["gap"] == "5"
    assert reconciler.metrics.drift_events == 1


@pytest.mark.anyio
async def test_sweep_detects_phantom_local_position(tmp_path: Path) -> None:
    """Local says we hold a symbol the venue does not report."""

    exchange = _StubExchange([])  # venue: nothing
    notifier = _RecordingNotifier()
    state = PositionState()
    state.set(Symbol("BTC/USD"), Decimal("0.5"))
    reconciler = Reconciler(
        exchange=exchange,
        state=state,
        notifier=notifier,
        anomaly_path=tmp_path / "anomalies.jsonl",
    )
    drifts = await reconciler.sweep_once()
    assert len(drifts) == 1
    parsed = json.loads((tmp_path / "anomalies.jsonl").read_text())
    assert parsed["detail"]["symbol"] == "BTC/USD"
    assert parsed["detail"]["venue_qty"] == "0"
    assert parsed["detail"]["local_qty"] == "0.5"


@pytest.mark.anyio
async def test_sweep_ignores_drift_below_epsilon(tmp_path: Path) -> None:
    """Tiny floating-point gaps should not trigger an alert."""

    exchange = _StubExchange([_pos("AAPL", "10.0000000005")])
    notifier = _RecordingNotifier()
    state = PositionState()
    state.set(Symbol("AAPL"), Decimal("10"))
    reconciler = Reconciler(
        exchange=exchange,
        state=state,
        notifier=notifier,
        anomaly_path=tmp_path / "anomalies.jsonl",
        epsilon=Decimal("1e-6"),
    )
    drifts = await reconciler.sweep_once()
    assert drifts == []
    assert notifier.warnings == []


@pytest.mark.anyio
async def test_sweep_failure_counted_but_does_not_raise(tmp_path: Path) -> None:
    exchange = _StubExchange([])
    exchange.raise_on_list = RuntimeError("REST blew up")
    reconciler = Reconciler(
        exchange=exchange,
        state=PositionState(),
        notifier=_RecordingNotifier(),
        anomaly_path=tmp_path / "anomalies.jsonl",
    )
    drifts = await reconciler.sweep_once()
    assert drifts == []
    assert reconciler.metrics.sweeps_failed == 1
    assert reconciler.metrics.sweeps_completed == 0


@pytest.mark.anyio
async def test_run_loop_cancels_on_event(tmp_path: Path) -> None:
    """``run`` exits within an interval after cancel_event is set."""

    exchange = _StubExchange([_pos("AAPL", "10")])
    state = PositionState()
    state.set(Symbol("AAPL"), Decimal("10"))
    reconciler = Reconciler(
        exchange=exchange,
        state=state,
        notifier=_RecordingNotifier(),
        anomaly_path=tmp_path / "anomalies.jsonl",
        interval_seconds=0.05,
    )
    cancel = anyio.Event()

    async def _stop_soon() -> None:
        await anyio.sleep(0.1)
        cancel.set()

    async def _run() -> None:
        await reconciler.run(cancel_event=cancel)

    async with anyio.create_task_group() as tg:
        tg.start_soon(_run)
        tg.start_soon(_stop_soon)
    # If we got here, run() returned cleanly.
    assert reconciler.metrics.sweeps_completed >= 1


@pytest.mark.anyio
async def test_position_state_apply_fill_delta() -> None:
    state = PositionState()
    state.apply_fill_delta(Symbol("AAPL"), Decimal("10"))
    state.apply_fill_delta(Symbol("AAPL"), Decimal("-3"))
    assert state.snapshot() == {"AAPL": Decimal("7")}


# ----- anyio configuration -----


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"
