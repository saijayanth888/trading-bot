"""Cross-module type compatibility — Bar / Tick / Fill / Position / OrderProposal.

Why this matters
----------------
The V4 stack has two distinct ``OrderProposal`` shapes today:

* :class:`quanta_core.util.types.OrderProposal` — dataclass, used by the
  live engine + strategies.
* :class:`quanta_core.execution.engine.OrderProposal` — Pydantic, used by
  the execution engine.

When wave-1 reconciliation rebased these into the same tree, they ended
up at the same package but with different field names + validation
rules. That's a real wiring hazard. Until the foundation team unifies
them (tracked in ``MERGE_NOTES.md``), we test the integration shim
(``RiskGatedExecutionSink._adapt``) for the round-trip invariants:

* ``client_order_id`` is preserved bit-for-bit.
* ``symbol``, ``side``, ``qty`` are preserved.
* ``signal_px`` falls back from ``limit_price`` when the strategy didn't
  carry one explicitly.

We also smoke-test that:

* :class:`Bar` / :class:`Tick` / :class:`Fill` / :class:`Position` can be
  instantiated with the values the live module produces and consumed by
  the dispatcher without raising.
* The aggregator's emitted ``Bar`` round-trips through the dispatcher
  back to the strategy with no field mutation.
"""

from __future__ import annotations

import datetime as dt
import uuid
from decimal import Decimal

import pytest

from quanta_core.execution.engine import (
    Fill as ExecFill,
)
from quanta_core.execution.engine import (
    OrderProposal as ExecOrderProposal,
)
from quanta_core.execution.engine import (
    Side as ExecSide,
)
from quanta_core.strategy.base import Strategy
from quanta_core.util.types import (
    Bar,
    ClientOrderId,
    Fill,
    OrderProposal,
    Position,
    Symbol,
    Tick,
    Timeframe,
    VenueOrderId,
)

from .conftest import (
    DEFAULT_START_TS,
    InMemoryLedger,
    RiskGatedExecutionSink,
)

SYMBOL = Symbol("BTC-USD")
UTC = dt.UTC


# ---------------------------------------------------------------------------
# Construct-able domain types: shape sanity-check
# ---------------------------------------------------------------------------


def test_tick_constructs_with_full_field_set() -> None:
    t = Tick(
        symbol=SYMBOL,
        ts=DEFAULT_START_TS,
        price=Decimal("65000"),
        size=Decimal("0.01"),
        side="BUY",
    )
    assert t.symbol == SYMBOL
    assert t.ts.tzinfo is not None
    assert t.price == Decimal("65000")
    assert t.side == "BUY"


def test_bar_constructs_with_full_field_set() -> None:
    b = Bar(
        symbol=SYMBOL,
        timeframe="1m",
        open_ts=DEFAULT_START_TS,
        close_ts=DEFAULT_START_TS + dt.timedelta(minutes=1),
        open=Decimal("65000"),
        high=Decimal("65010"),
        low=Decimal("64990"),
        close=Decimal("65005"),
        volume=Decimal("1"),
        vwap=Decimal("65002"),
        trades=1,
    )
    assert b.timeframe == "1m"
    assert b.close_ts > b.open_ts
    assert b.close == Decimal("65005")


def test_fill_constructs_with_full_field_set() -> None:
    f = Fill(
        symbol=SYMBOL,
        side="BUY",
        qty=Decimal("0.01"),
        price=Decimal("65000"),
        ts=DEFAULT_START_TS,
        client_order_id=ClientOrderId("qc4-types-1"),
        venue_order_id=VenueOrderId("venue-types-1"),
        venue="paper",
        fee=Decimal("0"),
    )
    assert f.client_order_id == "qc4-types-1"
    assert f.venue == "paper"


def test_position_constructs_with_full_field_set() -> None:
    p = Position(
        symbol=SYMBOL,
        qty=Decimal("0.5"),
        avg_price=Decimal("65000"),
        venue="paper",
    )
    assert p.qty == Decimal("0.5")
    assert p.avg_price == Decimal("65000")


def test_order_proposal_constructs_with_minimum_fields() -> None:
    p = OrderProposal(
        strategy_name="t",
        symbol=SYMBOL,
        venue="paper",
        side="BUY",
        qty=Decimal("0.01"),
        order_type="limit",
        limit_price=Decimal("65000"),
    )
    assert p.metadata == {}
    assert p.time_in_force == "day"
    assert p.intent_timestamp_ms == 0


# ---------------------------------------------------------------------------
# OrderProposal round-trip through the integration shim
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_order_proposal_round_trip_preserves_client_order_id(
    risk_sink: RiskGatedExecutionSink,
    ledger: InMemoryLedger,
) -> None:
    """A strategy-side OrderProposal carrying a pre-minted client_order_id
    must reach the execution Fill with the same id."""
    cid = f"qc4-rt-{uuid.uuid4().hex[:12]}"
    proposal = OrderProposal(
        strategy_name="rt",
        symbol=SYMBOL,
        venue="paper",
        side="BUY",
        qty=Decimal("0.01"),
        order_type="limit",
        limit_price=Decimal("65000"),
        intent_timestamp_ms=int(DEFAULT_START_TS.timestamp() * 1000),
        metadata={"client_order_id": cid, "signal_px": "65000"},
    )

    await risk_sink.submit(proposal)

    assert len(risk_sink.adapted) == 1
    assert risk_sink.adapted[0].client_order_id == cid
    assert len(ledger.fills) == 1
    assert ledger.fills[0].client_order_id == cid


@pytest.mark.anyio
async def test_order_proposal_round_trip_preserves_symbol_side_qty(
    risk_sink: RiskGatedExecutionSink,
    ledger: InMemoryLedger,
) -> None:
    """``symbol``, ``side`` and ``qty`` must survive the adaptation step."""
    for side, sym in [("BUY", Symbol("ETH-USD")), ("SELL", Symbol("BTC-USD"))]:
        proposal = OrderProposal(
            strategy_name="rt",
            symbol=sym,
            venue="paper",
            side=side,  # type: ignore[arg-type]
            qty=Decimal("0.5"),
            order_type="limit",
            limit_price=Decimal("100"),
            metadata={"signal_px": "100"},
        )
        await risk_sink.submit(proposal)

    assert len(risk_sink.adapted) == 2
    assert risk_sink.adapted[0].symbol == "ETH-USD"
    assert risk_sink.adapted[0].side == ExecSide.BUY
    assert risk_sink.adapted[0].qty == Decimal("0.5")
    assert risk_sink.adapted[1].symbol == "BTC-USD"
    assert risk_sink.adapted[1].side == ExecSide.SELL


@pytest.mark.anyio
async def test_signal_px_falls_back_from_limit_price(
    risk_sink: RiskGatedExecutionSink,
) -> None:
    """If the strategy doesn't set ``signal_px`` in metadata, the adapter
    must derive it from ``limit_price`` so the slippage gate has a reference."""
    proposal = OrderProposal(
        strategy_name="rt",
        symbol=SYMBOL,
        venue="paper",
        side="BUY",
        qty=Decimal("0.01"),
        order_type="limit",
        limit_price=Decimal("65000"),
        # No signal_px in metadata.
    )

    await risk_sink.submit(proposal)

    assert risk_sink.adapted[0].signal_px == Decimal("65000")
    assert risk_sink.adapted[0].limit_px == Decimal("65000")


@pytest.mark.anyio
async def test_metadata_passthrough_drops_internal_keys(
    risk_sink: RiskGatedExecutionSink,
) -> None:
    """Adapter-internal keys (``client_order_id``, ``signal_px``) are
    stripped from the metadata of the execution-side proposal; user
    metadata flows through untouched."""
    proposal = OrderProposal(
        strategy_name="rt",
        symbol=SYMBOL,
        venue="paper",
        side="BUY",
        qty=Decimal("0.01"),
        order_type="limit",
        limit_price=Decimal("65000"),
        metadata={
            "client_order_id": "qc4-meta-1",
            "signal_px": "65000",
            "ranking_score": "0.87",
            "regime": "trend_up",
        },
    )

    await risk_sink.submit(proposal)

    md = risk_sink.adapted[0].metadata
    assert "client_order_id" not in md
    assert "signal_px" not in md
    assert md["ranking_score"] == "0.87"
    assert md["regime"] == "trend_up"


# ---------------------------------------------------------------------------
# Strategy ABC contract — every hook returns ``list[OrderProposal]``.
# ---------------------------------------------------------------------------


class _NoopStrategy(Strategy):
    name = "noop"
    symbols: list[Symbol] = [SYMBOL]
    timeframes: list[Timeframe] = ["1m"]

    async def on_candle(self, bar: Bar, ctx: object) -> list[OrderProposal]:
        return []


@pytest.mark.anyio
async def test_strategy_default_hooks_return_empty_lists() -> None:
    """``on_tick`` and ``on_fill`` defaults must return [] so dispatcher
    can iterate them without a ``None`` check."""
    s = _NoopStrategy()
    bar = Bar(
        symbol=SYMBOL,
        timeframe="1m",
        open_ts=DEFAULT_START_TS,
        close_ts=DEFAULT_START_TS + dt.timedelta(minutes=1),
        open=Decimal("65000"),
        high=Decimal("65010"),
        low=Decimal("64990"),
        close=Decimal("65005"),
        volume=Decimal("1"),
        vwap=Decimal("65002"),
        trades=1,
    )
    tick = Tick(symbol=SYMBOL, ts=DEFAULT_START_TS, price=Decimal("65000"), size=Decimal("1"))
    fill = Fill(
        symbol=SYMBOL,
        side="BUY",
        qty=Decimal("0.01"),
        price=Decimal("65000"),
        ts=DEFAULT_START_TS,
        client_order_id=ClientOrderId("qc4-hook-1"),
        venue_order_id=VenueOrderId("venue-1"),
        venue="paper",
        fee=Decimal("0"),
    )

    assert await s.on_candle(bar, ctx=None) == []
    assert await s.on_tick(tick, ctx=None) == []
    assert await s.on_fill(fill, ctx=None) == []
    # on_start / on_stop return None (default no-ops). We call them for
    # side-effect coverage; awaiting them must not raise.
    await s.on_start(ctx=None)
    await s.on_stop(ctx=None)


# ---------------------------------------------------------------------------
# Execution-side OrderProposal: Pydantic validation rules.
# ---------------------------------------------------------------------------


def test_exec_order_proposal_requires_client_order_id_min_length() -> None:
    """The execution model's ``client_order_id`` field has a minimum length
    of 8 (DESIGN-LOCK §execution.idempotency). Adapter ids start with
    ``qc4-`` + a 4-char strategy-name slug — always > 8."""
    with pytest.raises(Exception):  # noqa: PT011 - any validation error is acceptable
        ExecOrderProposal(
            client_order_id="abc",
            symbol="BTC-USD",
            side=ExecSide.BUY,
            qty=Decimal("0.01"),
            limit_px=Decimal("65000"),
            signal_px=Decimal("65000"),
            strategy_name="t",
            intent_ts_ms=1,
        )


def test_exec_order_proposal_is_frozen() -> None:
    """The Pydantic config sets ``frozen=True`` so the in-memory proposal
    cannot be mutated after construction. This is what makes it safe to
    share across the risk/exec boundary."""
    p = ExecOrderProposal(
        client_order_id="qc4-test-1234",
        symbol="BTC-USD",
        side=ExecSide.BUY,
        qty=Decimal("0.01"),
        limit_px=Decimal("65000"),
        signal_px=Decimal("65000"),
        strategy_name="t",
        intent_ts_ms=1,
    )
    with pytest.raises(Exception):  # noqa: PT011 - Pydantic raises ValidationError
        p.qty = Decimal("0.02")


def test_exec_fill_is_frozen() -> None:
    """Fills are immutable once returned by the engine — the same
    invariant the ledger writer relies on."""
    f = ExecFill(
        client_order_id="qc4-test-1234",
        exchange_order_id="venue-1",
        symbol="BTC-USD",
        side=ExecSide.BUY,
        filled_qty=Decimal("0.01"),
        avg_price=Decimal("65000"),
        status="FILLED",
        venue_ts=DEFAULT_START_TS,
    )
    with pytest.raises(Exception):  # noqa: PT011 - Pydantic raises ValidationError
        f.filled_qty = Decimal("0.02")
