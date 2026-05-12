"""Tests for the Strategy ABC stub shipped alongside the backtest module."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest

from quanta_core.strategy.base import Strategy
from quanta_core.types import (
    Bar,
    ClientOrderId,
    Context,
    Fill,
    OrderProposal,
    Symbol,
    Tick,
)


class _MinimalStrategy(Strategy):
    """The smallest possible concrete strategy — only on_candle defined."""

    name = "minimal"

    def on_candle(self, bar: Bar) -> Sequence[OrderProposal]:
        return ()


def _stub_ctx() -> Any:
    class _C:
        def now(self) -> datetime:  # pragma: no cover
            return datetime(2026, 1, 1, tzinfo=UTC)

        def get_position(self, symbol):  # pragma: no cover
            return None

        def get_history(self, symbol, tf, n):  # pragma: no cover
            return ()

        def submit_proposal(self, proposal):  # pragma: no cover
            pass

        def log_decision(self, decision):  # pragma: no cover
            pass

    return _C()


def test_abc_cannot_instantiate_without_on_candle():
    class _Incomplete(Strategy):
        name = "incomplete"

    with pytest.raises(TypeError, match="on_candle"):
        _Incomplete(_stub_ctx(), {})  # type: ignore[abstract]


def test_default_on_tick_returns_empty():
    s = _MinimalStrategy(_stub_ctx(), {})
    tick = Tick(
        symbol=Symbol("BTC/USD"),
        price=Decimal("100"),
        size=Decimal("1"),
        timestamp_utc=datetime(2026, 1, 1, tzinfo=UTC),
    )
    assert s.on_tick(tick) == ()


def test_default_on_fill_returns_none():
    s = _MinimalStrategy(_stub_ctx(), {})
    fill = Fill(
        order_id="o1",
        client_order_id=ClientOrderId("co-1"),
        symbol=Symbol("BTC/USD"),
        side="BUY",
        qty=Decimal("1"),
        price=Decimal("100"),
        fee=Decimal("0"),
        timestamp_utc=datetime(2026, 1, 1, tzinfo=UTC),
        venue="paper",
    )
    assert s.on_fill(fill) is None


def test_default_lifecycle_hooks_return_none():
    s = _MinimalStrategy(_stub_ctx(), {})
    assert s.on_start() is None
    assert s.on_stop() is None


def test_default_train_hook_returns_none():
    s = _MinimalStrategy(_stub_ctx(), {})
    assert s.train_hook([]) is None


def test_repr_format():
    s = _MinimalStrategy(_stub_ctx(), {})
    assert repr(s) == "_MinimalStrategy(name='minimal')"


def test_ctx_and_config_stored():
    ctx = _stub_ctx()
    cfg = {"foo": 1, "bar": "baz"}
    s = _MinimalStrategy(ctx, cfg)
    assert s.ctx is ctx
    assert s.config == cfg
    # config is copied, not shared.
    cfg["foo"] = 999
    assert s.config["foo"] == 1


def test_context_protocol_runtime_checkable():
    ctx = _stub_ctx()
    assert isinstance(ctx, Context)
