"""Shared fixtures + in-memory fakes for the V4 integration smoke tests.

Why fakes (not real SDKs)?
--------------------------
The integration suite must run on a hermetic CI box without any network
egress. The DESIGN-LOCK calls for `mypy --strict` + ruff clean; we keep
the fakes tiny so they stay typed end-to-end.

Module map covered here
-----------------------
- ``quanta_core.util.types`` (Bar / Tick / Fill / OrderProposal / Position)
- ``quanta_core.strategy.base`` (Strategy ABC)
- ``quanta_core.live.engine`` (LiveEngine)
- ``quanta_core.live.dispatcher`` (StrategyDispatcher / OrderSink)
- ``quanta_core.live.tick_aggregator`` (TickAggregator)
- ``quanta_core.live.reconciler`` (Reconciler / PositionState)
- ``quanta_core.execution.engine`` (ExecutionEngine)
- ``quanta_core.execution.idempotency`` (IdempotencyStore)

Wave-2 modules (backtest, ledger, hermes, agents) are not yet built. We
stand in for them with minimal fakes that satisfy the documented
interfaces from ``docs/quanta-core-v4/06-ARCHITECTURE.md`` and the rev2
follow-ups. When the real modules land, these fakes get replaced one by
one; the test scenarios themselves do not change shape.
"""

from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import AsyncIterator, Iterable, Iterator
from dataclasses import dataclass, field
from decimal import Decimal
from typing import TYPE_CHECKING, Any

import pytest
from sqlalchemy import Engine, create_engine

from quanta_core.exchanges.base import Exchange, ExchangeStream, StreamEvent
from quanta_core.execution.engine import (
    ExecutionEngine,
    OrderResponse,
    Quote,
    RejectedReason,
)
from quanta_core.execution.engine import Fill as ExecFill
from quanta_core.execution.engine import (
    OrderProposal as ExecOrderProposal,
)
from quanta_core.execution.engine import (
    Side as ExecSide,
)
from quanta_core.execution.idempotency import Base, IdempotencyStore
from quanta_core.util.types import (
    Bar,
    OrderProposal,
    Position,
    Symbol,
    Tick,
    Timeframe,
    Venue,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from quanta_core.strategy.base import Strategy

UTC = dt.UTC

DEFAULT_START_TS = dt.datetime(2026, 5, 12, 12, 0, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# Synthetic bar / tick generators
# ---------------------------------------------------------------------------


def synthetic_bars(
    *,
    symbol: str = "BTC-USD",
    timeframe: Timeframe = "1m",
    start: dt.datetime = DEFAULT_START_TS,
    n: int = 100,
    base_price: Decimal = Decimal("65000"),
    step: Decimal = Decimal("10"),
) -> list[Bar]:
    """Build a deterministic 100-candle 1m series.

    Each bar moves up by ``step``. The series is mid-volatile enough that
    the slippage gate (default 0.5% threshold) will let the orders through
    without ever rejecting on drift.
    """
    sym = Symbol(symbol)
    bars: list[Bar] = []
    for i in range(n):
        open_ts = start + dt.timedelta(minutes=i)
        close_ts = open_ts + dt.timedelta(minutes=1)
        o = base_price + step * i
        c = o + step
        h = c + Decimal("1")
        low = o - Decimal("1")
        bars.append(
            Bar(
                symbol=sym,
                timeframe=timeframe,
                open_ts=open_ts,
                close_ts=close_ts,
                open=o,
                high=h,
                low=low,
                close=c,
                volume=Decimal("1"),
                vwap=(o + c) / Decimal("2"),
                trades=1,
            )
        )
    return bars


def synthetic_ticks(
    *,
    symbol: str = "BTC-USD",
    start: dt.datetime = DEFAULT_START_TS,
    seconds: int = 60,
    base_price: Decimal = Decimal("65000"),
) -> list[Tick]:
    """One tick per second; price drifts +1 each step."""
    sym = Symbol(symbol)
    return [
        Tick(
            symbol=sym,
            ts=start + dt.timedelta(seconds=i),
            price=base_price + Decimal(i),
            size=Decimal("0.01"),
            side=None,
        )
        for i in range(seconds)
    ]


# ---------------------------------------------------------------------------
# In-memory ledger (stands in for the wave-2 ledger module).
#
# The wave-2 ``quanta_core.ledger`` module will own a Postgres-backed writer
# (see docs/quanta-core-v4/06-ARCHITECTURE.md §6). Until it lands, this fake
# implements the same minimum surface used by the execution engine
# (``record_fill`` / ``record_rejection``) plus the equity-curve view the
# integration tests inspect.
# ---------------------------------------------------------------------------


@dataclass
class LedgerEntry:
    """One row in the in-memory ledger."""

    kind: str  # "fill" | "rejection"
    client_order_id: str
    symbol: str | None
    side: str | None
    qty: Decimal | None
    price: Decimal | None
    ts: dt.datetime
    raw: dict[str, Any] = field(default_factory=dict)


class InMemoryLedger:
    """Test stand-in for ``quanta_core.ledger.writer.Writer``.

    Records every fill + rejection. Tracks a naive equity curve under a
    fixed starting balance so the smoke test can assert curve shape.
    """

    def __init__(self, *, starting_cash: Decimal = Decimal("100000")) -> None:
        self.entries: list[LedgerEntry] = []
        self._cash = starting_cash
        self._positions: dict[str, Decimal] = {}
        self._avg_cost: dict[str, Decimal] = {}
        self._equity_curve: list[tuple[dt.datetime, Decimal]] = [
            (DEFAULT_START_TS, starting_cash),
        ]

    # --- Ledger protocol used by ExecutionEngine ---------------------

    def record_fill(self, fill: ExecFill) -> None:
        self.entries.append(
            LedgerEntry(
                kind="fill",
                client_order_id=fill.client_order_id,
                symbol=fill.symbol,
                side=fill.side.value,
                qty=fill.filled_qty,
                price=fill.avg_price,
                ts=fill.venue_ts,
                raw={"exchange_order_id": fill.exchange_order_id, "status": fill.status},
            )
        )
        # Update notional positions for the equity curve.
        sgn = Decimal("1") if fill.side == ExecSide.BUY else Decimal("-1")
        delta_qty = sgn * fill.filled_qty
        prev_qty = self._positions.get(fill.symbol, Decimal("0"))
        new_qty = prev_qty + delta_qty
        # Naive weighted average cost on grow; on flip / reduce we keep the
        # previous cost — enough to compute a rough equity curve.
        if prev_qty == 0 or (sgn > 0 and prev_qty >= 0) or (sgn < 0 and prev_qty <= 0):
            prev_cost = self._avg_cost.get(fill.symbol, Decimal("0"))
            if new_qty != 0:
                self._avg_cost[fill.symbol] = (
                    prev_cost * abs(prev_qty) + fill.avg_price * fill.filled_qty
                ) / abs(new_qty)
        self._positions[fill.symbol] = new_qty
        self._cash -= delta_qty * fill.avg_price
        self._cash -= Decimal("0")  # fees baked into avg_price for the smoke test.
        equity = self._cash + sum(
            qty * self._avg_cost.get(sym, Decimal("0")) for sym, qty in self._positions.items()
        )
        self._equity_curve.append((fill.venue_ts, equity))

    def record_rejection(self, reason: RejectedReason) -> None:
        self.entries.append(
            LedgerEntry(
                kind="rejection",
                client_order_id=reason.client_order_id,
                symbol=None,
                side=None,
                qty=None,
                price=None,
                ts=reason.at,
                raw={"code": reason.code, "detail": reason.detail},
            )
        )

    # --- read API used by tests --------------------------------------

    @property
    def fills(self) -> list[LedgerEntry]:
        return [e for e in self.entries if e.kind == "fill"]

    @property
    def rejections(self) -> list[LedgerEntry]:
        return [e for e in self.entries if e.kind == "rejection"]

    @property
    def equity_curve(self) -> list[tuple[dt.datetime, Decimal]]:
        return list(self._equity_curve)


# ---------------------------------------------------------------------------
# Minimal paper-exchange used by the execution + live tests.
# ---------------------------------------------------------------------------


@dataclass
class _PaperState:
    """Internal book the paper exchange keeps so quotes track the strategy."""

    next_mid: Decimal


class PaperExecExchange:
    """In-memory exchange that satisfies the execution engine's protocol.

    Every ``place`` is filled immediately at the requested limit (or mid if
    market) and returns a synthetic ``OrderResponse`` whose status is
    ``FILLED``. The mid is bumped after each fill so subsequent quotes
    differ — useful for assertions about quote freshness.
    """

    name: str = "paper"

    def __init__(
        self,
        *,
        starting_mid: Decimal = Decimal("65000"),
        clock: Iterator[dt.datetime] | None = None,
    ) -> None:
        self._state = _PaperState(next_mid=starting_mid)
        self._clock = clock
        self.placed: list[ExecOrderProposal] = []
        self.canceled: list[str] = []

    def _now(self) -> dt.datetime:
        if self._clock is not None:
            try:
                return next(self._clock)
            except StopIteration:
                pass
        return dt.datetime.now(UTC)

    def place(self, proposal: ExecOrderProposal) -> OrderResponse:
        self.placed.append(proposal)
        fill_px = proposal.limit_px or self._state.next_mid
        venue_id = f"venue-{uuid.uuid4().hex[:12]}"
        # Bump the synthetic mid by 0.001% per fill so quotes evolve.
        self._state.next_mid = self._state.next_mid * Decimal("1.00001")
        return OrderResponse(
            client_order_id=proposal.client_order_id,
            exchange_order_id=venue_id,
            status="FILLED",
            filled_qty=proposal.qty,
            avg_price=fill_px,
            venue_ts=self._now(),
            raw={"engine": "paper"},
        )

    def cancel(self, client_order_id: str) -> OrderResponse:
        self.canceled.append(client_order_id)
        return OrderResponse(
            client_order_id=client_order_id,
            exchange_order_id="venue-canceled",
            status="CANCELED",
            filled_qty=Decimal("0"),
            avg_price=None,
            venue_ts=self._now(),
            raw={"engine": "paper"},
        )

    def get_quote(self, symbol: str) -> Quote:
        return Quote(symbol=symbol, mid=self._state.next_mid, ts=self._now())

    def cancel_all(self, symbol: str | None = None) -> Iterable[OrderResponse]:
        return []


# ---------------------------------------------------------------------------
# Risk engine — fake. The real ``quanta_core.risk.RiskEngine`` lives on
# ``feat/v4-build-risk`` (nested layout, not yet reconciled at root). We
# stand it in with a deterministic "always-approve below max notional"
# gate. The signature matches the doc-promised ``approve(proposal) -> bool``.
# ---------------------------------------------------------------------------


class StubRiskEngine:
    """Approve everything below ``max_notional``. Records decisions."""

    def __init__(self, *, max_notional: Decimal = Decimal("1000000")) -> None:
        self.max_notional = max_notional
        self.approvals: list[ExecOrderProposal] = []
        self.rejections: list[ExecOrderProposal] = []

    def approve(self, proposal: ExecOrderProposal) -> bool:
        notional = proposal.qty * proposal.signal_px
        if notional <= self.max_notional:
            self.approvals.append(proposal)
            return True
        self.rejections.append(proposal)
        return False


# ---------------------------------------------------------------------------
# Risk-gated execution sink. Implements the dispatcher's ``OrderSink``
# protocol AND adapts ``util.types.OrderProposal`` (from the strategy) into
# the Pydantic ``execution.engine.OrderProposal`` the execution layer
# requires. This is the wiring under test.
# ---------------------------------------------------------------------------


class RiskGatedExecutionSink:
    """The integration shim that connects strategy proposals to execution.

    Responsibilities (all of which are part of the contract being tested):

    1. Adapt the ``util.types.OrderProposal`` returned by a strategy to the
       ``execution.engine.OrderProposal`` Pydantic model the execution
       engine consumes. The translator preserves ``client_order_id``,
       ``symbol``, ``side``, ``qty`` and the strategy's reference price.
    2. Route the adapted proposal through the risk engine.
    3. Forward approvals to ``ExecutionEngine.submit``.
    4. Collect typed outcomes (``Fill`` / ``RejectedReason``) so tests can
       assert end-to-end shape.
    """

    def __init__(
        self,
        *,
        execution: ExecutionEngine,
        risk: StubRiskEngine,
        strategy_name: str = "smoke",
    ) -> None:
        self._execution = execution
        self._risk = risk
        self._strategy_name = strategy_name
        self.outcomes: list[ExecFill | RejectedReason] = []
        self.adapted: list[ExecOrderProposal] = []
        self.risk_rejected: list[OrderProposal] = []

    async def submit(self, proposal: OrderProposal) -> None:
        exec_prop = self._adapt(proposal)
        self.adapted.append(exec_prop)
        if not self._risk.approve(exec_prop):
            self.risk_rejected.append(proposal)
            return
        outcome = self._execution.submit(exec_prop)
        self.outcomes.append(outcome)

    def _adapt(self, proposal: OrderProposal) -> ExecOrderProposal:
        # Reference price for the slippage gate — the strategy carries it
        # in ``limit_price`` when the order is a limit, otherwise we fall
        # back to a metadata-provided ``signal_px`` (smoke tests always set
        # at least one).
        signal_px = (
            proposal.limit_price
            if proposal.limit_price is not None
            else Decimal(str(proposal.metadata.get("signal_px", "0")))
        )
        # The Pydantic model wants an enum value, not a literal string.
        side = ExecSide.BUY if proposal.side == "BUY" else ExecSide.SELL
        client_order_id = str(
            proposal.metadata.get("client_order_id")
            or f"qc4-{self._strategy_name}-{uuid.uuid4().hex[:12]}"
        )
        return ExecOrderProposal(
            client_order_id=client_order_id,
            symbol=str(proposal.symbol),
            side=side,
            qty=proposal.qty,
            limit_px=proposal.limit_price,
            signal_px=signal_px,
            strategy_name=proposal.strategy_name,
            intent_ts_ms=proposal.intent_timestamp_ms
            or int(dt.datetime.now(UTC).timestamp() * 1000),
            metadata={
                k: v
                for k, v in proposal.metadata.items()
                if k not in {"signal_px", "client_order_id"}
            },
        )


# ---------------------------------------------------------------------------
# Backtest engine — fake. The real wave-2 backtest engine will replay bars
# from disk and feed them through a Strategy in the same loop the live
# engine uses (DESIGN-LOCK §3). Until it lands we replay a list[Bar]
# directly.
#
# The fake is intentionally small: feed bars through Strategy.on_candle,
# forward proposals to the same risk-gated sink the live engine uses.
# ---------------------------------------------------------------------------


@dataclass
class BacktestResult:
    proposals_emitted: int
    fills: list[ExecFill]
    rejections: list[RejectedReason]
    equity_curve: list[tuple[dt.datetime, Decimal]]


class FakeBacktestEngine:
    """Bar-driven backtest replay used as the parity oracle stand-in."""

    def __init__(
        self,
        *,
        strategy: Strategy,
        sink: RiskGatedExecutionSink,
        ledger: InMemoryLedger,
    ) -> None:
        self._strategy = strategy
        self._sink = sink
        self._ledger = ledger

    async def run(self, bars: list[Bar]) -> BacktestResult:
        proposals_emitted = 0
        for bar in bars:
            if bar.symbol not in self._strategy.symbols:
                continue
            if bar.timeframe not in self._strategy.timeframes:
                continue
            proposals = await self._strategy.on_candle(bar, ctx=None)
            for proposal in proposals:
                proposals_emitted += 1
                await self._sink.submit(proposal)
        return BacktestResult(
            proposals_emitted=proposals_emitted,
            fills=[o for o in self._sink.outcomes if isinstance(o, ExecFill)],
            rejections=[o for o in self._sink.outcomes if isinstance(o, RejectedReason)],
            equity_curve=self._ledger.equity_curve,
        )


# ---------------------------------------------------------------------------
# Live-engine exchange fake — replays a fixed stream of StreamEvents.
# ---------------------------------------------------------------------------


class _ReplayStream(ExchangeStream):
    def __init__(self, events: list[StreamEvent]) -> None:
        self._events = events
        self._idx = 0

    def __aiter__(self) -> AsyncIterator[StreamEvent]:
        return self._aiter()

    async def _aiter(self) -> AsyncIterator[StreamEvent]:
        import anyio.lowlevel

        while self._idx < len(self._events):
            event = self._events[self._idx]
            self._idx += 1
            yield event
            await anyio.lowlevel.checkpoint()

    async def aclose(self) -> None:
        self._idx = len(self._events)


class FakeLiveExchange(Exchange):
    """Async exchange that yields a pre-scripted stream of events."""

    name: Venue = "paper"

    def __init__(
        self,
        events: list[StreamEvent],
        positions: list[Position] | None = None,
    ) -> None:
        self._events = list(events)
        self._positions = positions or []
        self.opened = False
        self.closed = False

    async def open(self) -> ExchangeStream:
        self.opened = True
        return _ReplayStream(self._events)

    async def list_positions(self) -> list[Position]:
        return list(self._positions)

    async def close(self) -> None:
        self.closed = True


# ---------------------------------------------------------------------------
# Pytest fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def fixed_now() -> dt.datetime:
    return DEFAULT_START_TS


@pytest.fixture
def sqlite_engine() -> Iterator[Engine]:
    """In-memory idempotency store backed by SQLite.

    SQLite honours the unique constraint the engine depends on identically
    to Postgres; we exercise the real engine plus real ORM plus real
    transaction boundaries.
    """
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    yield engine
    engine.dispose()


@pytest.fixture
def idem_store(sqlite_engine: Engine, fixed_now: dt.datetime) -> IdempotencyStore:
    counter = {"n": 0}

    def _clock() -> dt.datetime:
        # Move forward in 1s steps so committed_at differs from reserved_at.
        counter["n"] += 1
        return fixed_now + dt.timedelta(seconds=counter["n"])

    return IdempotencyStore(sqlite_engine, now_fn=_clock)


@pytest.fixture
def ledger() -> InMemoryLedger:
    return InMemoryLedger()


@pytest.fixture
def risk_engine() -> StubRiskEngine:
    return StubRiskEngine()


@pytest.fixture
def paper_exchange(fixed_now: dt.datetime) -> PaperExecExchange:
    # Yield 1s-spaced timestamps for the fills so the equity curve has
    # monotonic timestamps.
    def _clock() -> Iterator[dt.datetime]:
        i = 0
        while True:
            yield fixed_now + dt.timedelta(seconds=i)
            i += 1

    return PaperExecExchange(clock=_clock())


@pytest.fixture
def execution_engine(
    paper_exchange: PaperExecExchange,
    ledger: InMemoryLedger,
    idem_store: IdempotencyStore,
    fixed_now: dt.datetime,
) -> ExecutionEngine:
    counter = {"n": 0}

    def _now() -> dt.datetime:
        counter["n"] += 1
        return fixed_now + dt.timedelta(seconds=counter["n"])

    return ExecutionEngine(
        exchange=paper_exchange,
        ledger=ledger,
        idempotency_store=idem_store,
        # Wide slippage threshold so the synthetic price drift never trips it.
        slippage_threshold_pct=Decimal("10"),
        max_quote_age_s=3600.0,
        now_fn=_now,
        sleep_fn=lambda _s: None,
    )


@pytest.fixture
def risk_sink(
    execution_engine: ExecutionEngine,
    risk_engine: StubRiskEngine,
) -> RiskGatedExecutionSink:
    return RiskGatedExecutionSink(
        execution=execution_engine,
        risk=risk_engine,
        strategy_name="every_nth_candle",
    )


__all__ = [
    "BacktestResult",
    "DEFAULT_START_TS",
    "FakeBacktestEngine",
    "FakeLiveExchange",
    "InMemoryLedger",
    "LedgerEntry",
    "PaperExecExchange",
    "RiskGatedExecutionSink",
    "StubRiskEngine",
    "synthetic_bars",
    "synthetic_ticks",
]
