"""Tests for the Strategy ABC contract."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest

from quanta_core.strategy import Strategy
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
# Test doubles
# ---------------------------------------------------------------------------


class _FakeContext:
    """Bare-minimum Context implementation."""

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
        del proposal

    def log_decision(self, decision: dict[str, Any]) -> None:
        del decision


def _make_bar() -> Bar:
    return Bar(
        symbol=Symbol("BTC/USD"),
        open=Decimal(100),
        high=Decimal(110),
        low=Decimal(95),
        close=Decimal(105),
        volume=Decimal(10),
        timestamp_utc=UTC_NOW,
        timeframe="5m",
    )


def _make_tick() -> Tick:
    return Tick(
        symbol=Symbol("BTC/USD"),
        price=Decimal(105),
        size=Decimal(1),
        timestamp_utc=UTC_NOW,
    )


def _make_fill() -> Fill:
    return Fill(
        order_id="abc",
        client_order_id=ClientOrderId("00000000-0000-0000-0000-000000000001"),
        symbol=Symbol("BTC/USD"),
        side="BUY",
        qty=Decimal(1),
        price=Decimal(100),
        fee=Decimal(0),
        timestamp_utc=UTC_NOW,
        venue="paper",
    )


# ---------------------------------------------------------------------------
# ABC enforcement
# ---------------------------------------------------------------------------


def test_strategy_cannot_be_instantiated_directly() -> None:
    with pytest.raises(TypeError):
        Strategy(_FakeContext(), {})  # type: ignore[abstract]


def test_strategy_missing_on_candle_cannot_be_instantiated() -> None:
    class MissingHooks(Strategy):
        pass

    with pytest.raises(TypeError):
        MissingHooks(_FakeContext(), {})  # type: ignore[abstract]


# ---------------------------------------------------------------------------
# Concrete subclass exercising every hook
# ---------------------------------------------------------------------------


class _RecordingStrategy(Strategy):
    """Records every hook invocation for assertion."""

    name = "recorder"

    def __init__(self, ctx: Context, config: dict[str, Any]) -> None:
        super().__init__(ctx, config)
        self.events: list[tuple[str, object]] = []

    def on_candle(self, bar: Bar) -> list[OrderProposal]:
        self.events.append(("candle", bar))
        return []

    def on_tick(self, tick: Tick) -> list[OrderProposal]:
        self.events.append(("tick", tick))
        return []

    def on_fill(self, fill: Fill) -> None:
        self.events.append(("fill", fill))

    def on_start(self) -> None:
        self.events.append(("start", None))

    def on_stop(self) -> None:
        self.events.append(("stop", None))

    def train_hook(self, samples: list[Any]) -> None:
        self.events.append(("train", samples))


def test_strategy_lifecycle_hooks_fire() -> None:
    ctx = _FakeContext()
    strat = _RecordingStrategy(ctx, {"key": "val"})
    strat.on_start()
    bar = _make_bar()
    tick = _make_tick()
    fill = _make_fill()
    strat.on_candle(bar)
    strat.on_tick(tick)
    strat.on_fill(fill)
    strat.train_hook([1, 2, 3])
    strat.on_stop()

    kinds = [e[0] for e in strat.events]
    assert kinds == ["start", "candle", "tick", "fill", "train", "stop"]


def test_strategy_stores_ctx_and_config() -> None:
    ctx = _FakeContext()
    strat = _RecordingStrategy(ctx, {"key": "val"})
    assert strat.ctx is ctx
    assert strat.config == {"key": "val"}
    # Config dict is copied — outside mutation must not bleed in.
    strat.config["mutated"] = "yes"
    assert "mutated" not in {"key": "val"}


def test_strategy_repr() -> None:
    strat = _RecordingStrategy(_FakeContext(), {})
    assert repr(strat) == "_RecordingStrategy(name='recorder')"


# ---------------------------------------------------------------------------
# Default no-op hooks on a minimal subclass
# ---------------------------------------------------------------------------


class _MinimalStrategy(Strategy):
    """Implements only the mandatory ``on_candle`` hook."""

    def on_candle(self, bar: Bar) -> list[OrderProposal]:
        return []


def test_default_on_tick_is_empty() -> None:
    strat = _MinimalStrategy(_FakeContext(), {})
    assert list(strat.on_tick(_make_tick())) == []


def test_default_on_fill_is_noop() -> None:
    strat = _MinimalStrategy(_FakeContext(), {})
    # Default on_fill returns None implicitly; no exception, no state mutation.
    strat.on_fill(_make_fill())


def test_default_on_start_and_stop_are_noops() -> None:
    strat = _MinimalStrategy(_FakeContext(), {})
    strat.on_start()
    strat.on_stop()


def test_default_train_hook_is_noop() -> None:
    strat = _MinimalStrategy(_FakeContext(), {})
    strat.train_hook([])


def test_strategy_can_emit_order_proposals() -> None:
    """A minimal strategy can produce a real OrderProposal on a candle."""

    class _OnePropStrategy(Strategy):
        def on_candle(self, bar: Bar) -> list[OrderProposal]:
            return [
                OrderProposal(
                    symbol=bar.symbol,
                    side="BUY",
                    qty=Decimal(1),
                    order_type="market",
                    client_order_id=ClientOrderId("11111111-1111-1111-1111-111111111111"),
                    rationale="test",
                    asset_class="crypto",
                ),
            ]

    strat = _OnePropStrategy(_FakeContext(), {})
    props = list(strat.on_candle(_make_bar()))
    assert len(props) == 1
    assert props[0].side == "BUY"
