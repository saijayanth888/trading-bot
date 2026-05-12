"""Tests for the Pydantic event models in ``quanta_core.types``."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal

import pytest
from pydantic import ValidationError

from quanta_core.types import (
    Bar,
    ClientOrderId,
    Context,
    Fill,
    OrderProposal,
    Position,
    Symbol,
    Tick,
)

UTC_NOW = datetime(2026, 5, 12, 18, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# Bar
# ---------------------------------------------------------------------------


def _good_bar_kwargs() -> dict[str, object]:
    return {
        "symbol": Symbol("BTC/USD"),
        "open": Decimal(100),
        "high": Decimal(110),
        "low": Decimal(95),
        "close": Decimal(105),
        "volume": Decimal(10),
        "timestamp_utc": UTC_NOW,
        "timeframe": "5m",
    }


def test_bar_accepts_good_input() -> None:
    bar = Bar(**_good_bar_kwargs())  # type: ignore[arg-type]
    assert bar.symbol == "BTC/USD"
    assert bar.close == Decimal(105)
    assert bar.timestamp_utc.tzinfo is UTC


def test_bar_rejects_naive_timestamp() -> None:
    kwargs = _good_bar_kwargs()
    kwargs["timestamp_utc"] = datetime(2026, 5, 12, 18, 0)  # naive
    with pytest.raises(ValidationError, match="timezone-aware"):
        Bar(**kwargs)  # type: ignore[arg-type]


def test_bar_converts_non_utc_timestamp_to_utc() -> None:
    kwargs = _good_bar_kwargs()
    kwargs["timestamp_utc"] = datetime(
        2026,
        5,
        12,
        13,
        0,
        tzinfo=timezone(timedelta(hours=-5)),
    )
    bar = Bar(**kwargs)  # type: ignore[arg-type]
    assert bar.timestamp_utc.utcoffset() == timedelta(0)
    assert bar.timestamp_utc.hour == 18


def test_bar_rejects_high_below_low() -> None:
    kwargs = _good_bar_kwargs()
    kwargs["high"] = Decimal(90)
    kwargs["low"] = Decimal(95)
    with pytest.raises(ValidationError, match="high"):
        Bar(**kwargs)  # type: ignore[arg-type]


def test_bar_rejects_open_outside_range() -> None:
    kwargs = _good_bar_kwargs()
    kwargs["open"] = Decimal(80)
    with pytest.raises(ValidationError, match="open"):
        Bar(**kwargs)  # type: ignore[arg-type]


def test_bar_rejects_close_outside_range() -> None:
    kwargs = _good_bar_kwargs()
    kwargs["close"] = Decimal(200)
    with pytest.raises(ValidationError, match="close"):
        Bar(**kwargs)  # type: ignore[arg-type]


def test_bar_rejects_negative_volume() -> None:
    kwargs = _good_bar_kwargs()
    kwargs["volume"] = Decimal(-1)
    with pytest.raises(ValidationError):
        Bar(**kwargs)  # type: ignore[arg-type]


def test_bar_is_frozen() -> None:
    bar = Bar(**_good_bar_kwargs())  # type: ignore[arg-type]
    with pytest.raises(ValidationError):
        bar.close = Decimal(999)  # mutating a frozen model raises


def test_bar_rejects_extra_fields() -> None:
    kwargs = _good_bar_kwargs()
    kwargs["surprise"] = "boom"
    with pytest.raises(ValidationError):
        Bar(**kwargs)  # type: ignore[arg-type]


def test_bar_rejects_unknown_timeframe() -> None:
    kwargs = _good_bar_kwargs()
    kwargs["timeframe"] = "7m"
    with pytest.raises(ValidationError):
        Bar(**kwargs)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Tick
# ---------------------------------------------------------------------------


def test_tick_accepts_good_input() -> None:
    tick = Tick(
        symbol=Symbol("AAPL"),
        price=Decimal("190.5"),
        size=Decimal(100),
        timestamp_utc=UTC_NOW,
        side="BUY",
    )
    assert tick.side == "BUY"


def test_tick_optional_side() -> None:
    tick = Tick(
        symbol=Symbol("AAPL"),
        price=Decimal("190.5"),
        size=Decimal(100),
        timestamp_utc=UTC_NOW,
    )
    assert tick.side is None


def test_tick_rejects_zero_price() -> None:
    with pytest.raises(ValidationError):
        Tick(
            symbol=Symbol("AAPL"),
            price=Decimal(0),
            size=Decimal(1),
            timestamp_utc=UTC_NOW,
        )


def test_tick_rejects_zero_size() -> None:
    with pytest.raises(ValidationError):
        Tick(
            symbol=Symbol("AAPL"),
            price=Decimal(1),
            size=Decimal(0),
            timestamp_utc=UTC_NOW,
        )


def test_tick_rejects_naive_timestamp() -> None:
    with pytest.raises(ValidationError, match="timezone-aware"):
        Tick(
            symbol=Symbol("AAPL"),
            price=Decimal(1),
            size=Decimal(1),
            timestamp_utc=datetime(2026, 5, 12, 18, 0),
        )


# ---------------------------------------------------------------------------
# Fill
# ---------------------------------------------------------------------------


def _good_fill_kwargs() -> dict[str, object]:
    return {
        "order_id": "abc123",
        "client_order_id": ClientOrderId("00000000-0000-0000-0000-000000000001"),
        "symbol": Symbol("ETH/USD"),
        "side": "BUY",
        "qty": Decimal("0.5"),
        "price": Decimal(3000),
        "fee": Decimal("1.5"),
        "timestamp_utc": UTC_NOW,
        "venue": "coinbase",
    }


def test_fill_accepts_good_input() -> None:
    fill = Fill(**_good_fill_kwargs())  # type: ignore[arg-type]
    assert fill.venue == "coinbase"


def test_fill_rejects_negative_fee() -> None:
    kwargs = _good_fill_kwargs()
    kwargs["fee"] = Decimal("-0.01")
    with pytest.raises(ValidationError):
        Fill(**kwargs)  # type: ignore[arg-type]


def test_fill_rejects_naive_timestamp() -> None:
    kwargs = _good_fill_kwargs()
    kwargs["timestamp_utc"] = datetime(2026, 5, 12, 18, 0)
    with pytest.raises(ValidationError, match="timezone-aware"):
        Fill(**kwargs)  # type: ignore[arg-type]


def test_fill_rejects_unknown_venue() -> None:
    kwargs = _good_fill_kwargs()
    kwargs["venue"] = "binance"
    with pytest.raises(ValidationError):
        Fill(**kwargs)  # type: ignore[arg-type]


def test_fill_rejects_zero_qty() -> None:
    kwargs = _good_fill_kwargs()
    kwargs["qty"] = Decimal(0)
    with pytest.raises(ValidationError):
        Fill(**kwargs)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Position
# ---------------------------------------------------------------------------


def _good_position_kwargs(*, side: str = "BUY", qty: str = "10") -> dict[str, object]:
    return {
        "symbol": Symbol("AAPL"),
        "qty": Decimal(qty),
        "avg_entry": Decimal(180),
        "mark": Decimal(185),
        "unrealized_pnl": Decimal(50),
        "side": side,
        "asset_class": "equity",
        "opened_at": UTC_NOW,
        "subsystem_tag": "mean_rev_tft",
    }


def test_position_long_accepts_positive_qty() -> None:
    pos = Position(**_good_position_kwargs(side="BUY", qty="10"))  # type: ignore[arg-type]
    assert pos.qty == 10


def test_position_short_accepts_negative_qty() -> None:
    pos = Position(**_good_position_kwargs(side="SELL", qty="-5"))  # type: ignore[arg-type]
    assert pos.qty == -5


def test_position_long_rejects_negative_qty() -> None:
    with pytest.raises(ValidationError, match="BUY"):
        Position(**_good_position_kwargs(side="BUY", qty="-1"))  # type: ignore[arg-type]


def test_position_short_rejects_positive_qty() -> None:
    with pytest.raises(ValidationError, match="SELL"):
        Position(**_good_position_kwargs(side="SELL", qty="1"))  # type: ignore[arg-type]


def test_position_rejects_empty_subsystem_tag() -> None:
    kwargs = _good_position_kwargs()
    kwargs["subsystem_tag"] = ""
    with pytest.raises(ValidationError):
        Position(**kwargs)  # type: ignore[arg-type]


def test_position_rejects_naive_opened_at() -> None:
    kwargs = _good_position_kwargs()
    kwargs["opened_at"] = datetime(2026, 5, 12, 18, 0)
    with pytest.raises(ValidationError, match="timezone-aware"):
        Position(**kwargs)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# OrderProposal
# ---------------------------------------------------------------------------


def _good_proposal_kwargs() -> dict[str, object]:
    return {
        "symbol": Symbol("SOL/USD"),
        "side": "BUY",
        "qty": Decimal(1),
        "order_type": "limit",
        "limit_px": Decimal(150),
        "tif": "day",
        "client_order_id": ClientOrderId("00000000-0000-0000-0000-000000000002"),
        "rationale": "RSI oversold + bb_lower revert",
        "asset_class": "crypto",
    }


def test_proposal_accepts_limit_order() -> None:
    prop = OrderProposal(**_good_proposal_kwargs())  # type: ignore[arg-type]
    assert prop.limit_px == Decimal(150)
    assert prop.tif == "day"


def test_proposal_market_order_no_prices() -> None:
    kwargs = _good_proposal_kwargs()
    kwargs["order_type"] = "market"
    kwargs["limit_px"] = None
    prop = OrderProposal(**kwargs)  # type: ignore[arg-type]
    assert prop.limit_px is None


def test_proposal_market_rejects_limit_px() -> None:
    kwargs = _good_proposal_kwargs()
    kwargs["order_type"] = "market"
    with pytest.raises(ValidationError, match="market"):
        OrderProposal(**kwargs)  # type: ignore[arg-type]


def test_proposal_limit_requires_limit_px() -> None:
    kwargs = _good_proposal_kwargs()
    kwargs["limit_px"] = None
    with pytest.raises(ValidationError, match="limit_px required"):
        OrderProposal(**kwargs)  # type: ignore[arg-type]


def test_proposal_stop_requires_stop_px() -> None:
    kwargs = _good_proposal_kwargs()
    kwargs["order_type"] = "stop"
    kwargs["limit_px"] = None
    with pytest.raises(ValidationError, match="stop_px required"):
        OrderProposal(**kwargs)  # type: ignore[arg-type]


def test_proposal_stop_limit_requires_both() -> None:
    kwargs = _good_proposal_kwargs()
    kwargs["order_type"] = "stop_limit"
    kwargs["limit_px"] = Decimal(150)
    kwargs["stop_px"] = Decimal(145)
    prop = OrderProposal(**kwargs)  # type: ignore[arg-type]
    assert prop.stop_px == Decimal(145)


def test_proposal_rejects_zero_qty() -> None:
    kwargs = _good_proposal_kwargs()
    kwargs["qty"] = Decimal(0)
    with pytest.raises(ValidationError):
        OrderProposal(**kwargs)  # type: ignore[arg-type]


def test_proposal_rejects_empty_rationale() -> None:
    kwargs = _good_proposal_kwargs()
    kwargs["rationale"] = ""
    with pytest.raises(ValidationError):
        OrderProposal(**kwargs)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Context — protocol smoke test (runtime_checkable)
# ---------------------------------------------------------------------------


class _FakeContext:
    """Minimal implementation that satisfies the Context protocol."""

    def __init__(self) -> None:
        self.decisions: list[dict[str, object]] = []
        self.proposals: list[OrderProposal] = []

    def now(self) -> datetime:
        return UTC_NOW

    def get_position(self, symbol: Symbol) -> Position | None:
        return None

    def get_history(
        self,
        symbol: Symbol,
        timeframe: str,
        n: int,
    ) -> list[Bar]:
        return []

    def submit_proposal(self, proposal: OrderProposal) -> None:
        self.proposals.append(proposal)

    def log_decision(self, decision: dict[str, object]) -> None:
        self.decisions.append(decision)


def test_context_protocol_runtime_check() -> None:
    ctx = _FakeContext()
    assert isinstance(ctx, Context)


def test_context_can_be_used() -> None:
    ctx = _FakeContext()
    proposal = OrderProposal(**_good_proposal_kwargs())  # type: ignore[arg-type]
    ctx.submit_proposal(proposal)
    ctx.log_decision({"event": "test_decision"})
    assert ctx.proposals == [proposal]
    assert ctx.decisions == [{"event": "test_decision"}]
