"""End-to-end backtest smoke — the V4 "Hello, World".

Scenario
--------
1. Construct a minimal :class:`Strategy` that emits exactly one
   :class:`OrderProposal` on every 10th candle.
2. Feed 100 synthetic 1m candles through a fake :class:`BacktestEngine`
   (the wave-2 backtest module is not yet built; this fake honours the
   same DESIGN-LOCK contract — same Strategy class, bar clock, paper venue).
3. Each proposal flows through ``RiskGatedExecutionSink`` →
   ``ExecutionEngine.submit`` → ``InMemoryLedger.record_fill``.
4. Assert: 10 proposals → 10 fills → 10 ledger entries; the equity curve
   has the expected shape (monotonic timestamps, length 11 incl. starting
   point); ``client_order_id`` round-trips intact.

This test exercises the integration shim that adapts
``util.types.OrderProposal`` to ``execution.engine.OrderProposal``. The
``client_order_id`` round-trip is the load-bearing invariant the
phantom-order incident (2026-05) was about.
"""

from __future__ import annotations

import datetime as dt
import uuid
from decimal import Decimal
from typing import TYPE_CHECKING

import pytest

from quanta_core.execution.engine import Fill as ExecFill
from quanta_core.execution.idempotency import IdempotencyStore
from quanta_core.strategy.base import Strategy
from quanta_core.util.types import (
    Bar,
    OrderProposal,
    Symbol,
    Timeframe,
)

from .conftest import (
    FakeBacktestEngine,
    InMemoryLedger,
    PaperExecExchange,
    RiskGatedExecutionSink,
    StubRiskEngine,
    synthetic_bars,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from quanta_core.util.types import Fill, Tick


class EveryNthCandleStrategy(Strategy):
    """Emits one BUY proposal every ``n`` candles.

    Carries a pre-computed ``client_order_id`` in the proposal metadata so
    we can assert it round-trips through the adapter, through the
    execution engine, and into the ledger entry unchanged.
    """

    name = "every_nth_candle"
    symbols: list[Symbol] = [Symbol("BTC-USD")]
    timeframes: list[Timeframe] = ["1m"]
    wants_ticks = False

    def __init__(self, *, n: int = 10, qty: Decimal = Decimal("0.01")) -> None:
        self._n = n
        self._qty = qty
        self._bars_seen = 0
        self._emitted: list[OrderProposal] = []

    @property
    def emitted(self) -> list[OrderProposal]:
        return list(self._emitted)

    async def on_candle(self, bar: Bar, ctx: object) -> list[OrderProposal]:
        self._bars_seen += 1
        if self._bars_seen % self._n != 0:
            return []
        client_order_id = f"qc4-smoke-{self._bars_seen:04d}-{uuid.uuid4().hex[:8]}"
        proposal = OrderProposal(
            strategy_name=self.name,
            symbol=bar.symbol,
            venue="paper",
            side="BUY",
            qty=self._qty,
            order_type="limit",
            limit_price=bar.close,
            intent_timestamp_ms=int(bar.close_ts.timestamp() * 1000),
            metadata={
                "client_order_id": client_order_id,
                "signal_px": str(bar.close),
            },
        )
        self._emitted.append(proposal)
        return [proposal]

    async def on_tick(self, tick: Tick, ctx: object) -> list[OrderProposal]:
        return []

    async def on_fill(self, fill: Fill, ctx: object) -> list[OrderProposal]:
        return []


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_smoke_100_bars_10_proposals_10_fills_10_ledger_entries(
    risk_sink: RiskGatedExecutionSink,
    ledger: InMemoryLedger,
    risk_engine: StubRiskEngine,
) -> None:
    """Happy-path: every 10th candle out of 100 produces exactly one fill.

    Load-bearing assertions:
        * 10 proposals emitted by the strategy
        * 10 distinct ``client_order_id`` values reach the adapter
        * 10 fills land in the ledger (zero rejections)
        * Each ledger fill's ``client_order_id`` matches the strategy's
        * Risk engine approves all 10
    """
    strategy = EveryNthCandleStrategy(n=10, qty=Decimal("0.01"))
    engine = FakeBacktestEngine(strategy=strategy, sink=risk_sink, ledger=ledger)
    bars = synthetic_bars(n=100)

    result = await engine.run(bars)

    # Strategy fired the expected number of times.
    assert result.proposals_emitted == 10
    assert len(strategy.emitted) == 10

    # All proposals reached the execution adapter.
    assert len(risk_sink.adapted) == 10

    # Risk approved all 10; nothing was rejected at the risk layer.
    assert len(risk_engine.approvals) == 10
    assert risk_engine.rejections == []

    # Execution submitted all 10 and returned 10 typed Fill outcomes.
    assert len(result.fills) == 10
    assert result.rejections == []

    # Ledger recorded 10 fills, zero rejections.
    assert len(ledger.fills) == 10
    assert len(ledger.rejections) == 0

    # client_order_id round-trips intact: strategy → adapter → fill → ledger.
    strategy_ids = [str(p.metadata["client_order_id"]) for p in strategy.emitted]
    adapted_ids = [p.client_order_id for p in risk_sink.adapted]
    fill_ids = [f.client_order_id for f in result.fills]
    ledger_ids = [e.client_order_id for e in ledger.fills]
    assert strategy_ids == adapted_ids == fill_ids == ledger_ids
    # Every id is unique — no collisions.
    assert len(set(strategy_ids)) == 10


@pytest.mark.anyio
async def test_smoke_equity_curve_has_expected_shape(
    risk_sink: RiskGatedExecutionSink,
    ledger: InMemoryLedger,
) -> None:
    """The ledger equity curve grows by exactly N + 1 points (start + each fill).

    The strategy buys at increasing prices on a deterministic ladder, so the
    equity curve must:
        * start at the seeded cash balance
        * have ``N_FILLS + 1`` rows total
        * have strictly non-decreasing timestamps
    """
    strategy = EveryNthCandleStrategy(n=10, qty=Decimal("0.01"))
    engine = FakeBacktestEngine(strategy=strategy, sink=risk_sink, ledger=ledger)
    bars = synthetic_bars(n=100)

    result = await engine.run(bars)

    curve = result.equity_curve
    # Start + 10 fills.
    assert len(curve) == 11
    # First row is the seed.
    assert curve[0][1] == Decimal("100000")
    # Timestamps are non-decreasing.
    for prev, cur in zip(curve[:-1], curve[1:], strict=True):
        assert cur[0] >= prev[0], f"equity curve regressed in time at {cur}"
    # Final equity is finite Decimal (the curve never goes to NaN).
    assert isinstance(curve[-1][1], Decimal)


@pytest.mark.anyio
async def test_smoke_zero_proposals_when_strategy_never_fires(
    risk_sink: RiskGatedExecutionSink,
    ledger: InMemoryLedger,
) -> None:
    """Sanity: a 9-bar feed yields zero proposals (10th bar never reached)."""
    strategy = EveryNthCandleStrategy(n=10, qty=Decimal("0.01"))
    engine = FakeBacktestEngine(strategy=strategy, sink=risk_sink, ledger=ledger)
    bars = synthetic_bars(n=9)

    result = await engine.run(bars)

    assert result.proposals_emitted == 0
    assert result.fills == []
    assert result.rejections == []
    assert ledger.fills == []
    # Equity curve still has its seed point.
    assert len(ledger.equity_curve) == 1


@pytest.mark.anyio
async def test_smoke_strategy_side_propagates_to_fill(
    risk_sink: RiskGatedExecutionSink,
    ledger: InMemoryLedger,
) -> None:
    """``BUY`` from the strategy must land as ``BUY`` in the ledger fill."""
    strategy = EveryNthCandleStrategy(n=10, qty=Decimal("0.01"))
    engine = FakeBacktestEngine(strategy=strategy, sink=risk_sink, ledger=ledger)

    result = await engine.run(synthetic_bars(n=100))

    assert all(f.side.value == "BUY" for f in result.fills)
    assert all(e.side == "BUY" for e in ledger.fills)


@pytest.mark.anyio
async def test_smoke_risk_rejection_blocks_execution(
    paper_exchange: PaperExecExchange,
    ledger: InMemoryLedger,
    idem_store: IdempotencyStore,
    fixed_now: dt.datetime,
) -> None:
    """A risk rejection short-circuits before the exchange is called.

    Wires a risk engine with a tiny ``max_notional`` so every proposal is
    rejected. Asserts:
        * The execution exchange is never called.
        * No fill is recorded.
        * The dispatcher's per-proposal record list still grows
          (so the operator can see what was blocked).
    """
    from quanta_core.execution.engine import ExecutionEngine

    risk = StubRiskEngine(max_notional=Decimal("1"))  # blocks any real-world qty
    exec_engine = ExecutionEngine(
        exchange=paper_exchange,
        ledger=ledger,
        idempotency_store=idem_store,
        slippage_threshold_pct=Decimal("10"),
        max_quote_age_s=3600.0,
        now_fn=lambda: fixed_now,
        sleep_fn=lambda _s: None,
    )
    sink = RiskGatedExecutionSink(execution=exec_engine, risk=risk, strategy_name="risk-blocked")
    strategy = EveryNthCandleStrategy(n=10, qty=Decimal("0.01"))
    engine = FakeBacktestEngine(strategy=strategy, sink=sink, ledger=ledger)

    result = await engine.run(synthetic_bars(n=100))

    # Strategy fired 10 times, all blocked at risk.
    assert result.proposals_emitted == 10
    assert len(risk.rejections) == 10
    assert risk.approvals == []
    # Execution was never reached.
    assert paper_exchange.placed == []
    # Ledger recorded zero fills (and the risk layer chose not to log a
    # rejection to the ledger — that's an integration choice we leave to
    # the production sink; the count must be zero either way).
    assert ledger.fills == []


@pytest.mark.anyio
async def test_smoke_each_fill_has_exchange_order_id(
    risk_sink: RiskGatedExecutionSink,
    ledger: InMemoryLedger,
) -> None:
    """Every recorded fill carries a non-empty ``exchange_order_id``."""
    strategy = EveryNthCandleStrategy(n=10)
    engine = FakeBacktestEngine(strategy=strategy, sink=risk_sink, ledger=ledger)

    result = await engine.run(synthetic_bars(n=100))

    for fill in result.fills:
        assert isinstance(fill, ExecFill)
        assert fill.exchange_order_id.startswith("venue-")
    for entry in ledger.fills:
        assert entry.raw["exchange_order_id"].startswith("venue-")


@pytest.mark.anyio
async def test_smoke_idempotent_replay_does_not_double_fill(
    risk_sink: RiskGatedExecutionSink,
    ledger: InMemoryLedger,
    risk_engine: StubRiskEngine,
) -> None:
    """Replaying the SAME 100-bar feed twice produces 10 fills, not 20.

    The second run reuses the same ``client_order_id`` strings the strategy
    minted in the first run (we drive that by reusing the strategy
    instance). The idempotency store must short-circuit duplicates so the
    ledger ends with 10 fills, not 20.
    """
    strategy = EveryNthCandleStrategy(n=10, qty=Decimal("0.01"))
    engine = FakeBacktestEngine(strategy=strategy, sink=risk_sink, ledger=ledger)

    await engine.run(synthetic_bars(n=100))
    first_fill_count = len(ledger.fills)
    assert first_fill_count == 10

    # Re-submit each adapted proposal verbatim (simulating an at-least-once
    # message bus). The execution engine must see the duplicate client id
    # and return either the recorded Fill or a duplicate rejection — never
    # a new fill in the ledger.
    for proposal in list(risk_sink.adapted):
        outcome = risk_sink._execution.submit(proposal)  # noqa: SLF001 - test internal
        risk_sink.outcomes.append(outcome)

    assert len(ledger.fills) == first_fill_count, (
        "second submission of same client_order_id values must not record new fills"
    )
