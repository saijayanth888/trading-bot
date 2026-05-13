"""Tests for the V4 ``TrendFollow`` long-only trend-following strategy.

This strategy complements :mod:`quanta_core.strategy.mean_rev_bb` — instead
of fading deviations, it rides them. Like ``MeanRevBB`` it speaks the V4
``Strategy`` ABC's vocabulary (sync ``on_candle`` -> ``Sequence[OrderProposal]``)
and reads regime via ``self.state["regime"]``.

Spec contract (LONG-only; Coinbase Spot has no shorting):

* Indicators: short SMA (default 8) and long SMA (default 21) of close.
* Entry (BUY): regime == ``trending_up`` AND close > short_ma AND
  short_ma > long_ma AND no existing long.
* Exit (SELL full qty): currently long AND (close < short_ma OR regime in
  ``{trending_down, high_volatility}``).
* Anything else -> FLAT (``()``).
* BUY conviction = ``(close - short_ma) / short_ma`` clamped to
  ``[0.4, 0.95]``, surfaced as ``self.last_conviction`` and folded into
  ``qty = base_qty * conviction``.
* SELL conviction = 1.0 (full exit).

All tests are synchronous per DESIGN-LOCK §5.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest

from quanta_core.strategy.trend_follow import TrendFollow
from quanta_core.types import (
    Bar,
    OrderProposal,
    Position,
    Symbol,
    Timeframe,
)

# ---------------------------------------------------------------------------
# Constants & helpers
# ---------------------------------------------------------------------------

SYMBOL = Symbol("BTC/USD")
TIMEFRAME: Timeframe = "5m"
UTC_NOW = datetime(2026, 5, 13, 12, 0, tzinfo=UTC)


def _bar(
    close: Decimal,
    *,
    idx: int = 0,
    low: Decimal | None = None,
    high: Decimal | None = None,
) -> Bar:
    """Build a Bar with sane OHLC consistency around ``close``."""
    low = low if low is not None else min(close, Decimal("1"))
    high = high if high is not None else max(close, close + Decimal("1"))
    return Bar(
        symbol=SYMBOL,
        open=close,
        high=high,
        low=low,
        close=close,
        volume=Decimal(10),
        timestamp_utc=UTC_NOW + timedelta(minutes=5 * idx),
        timeframe=TIMEFRAME,
    )


def _history(closes: Sequence[Decimal]) -> list[Bar]:
    """Materialise a list of Bars chronologically from a closes sequence."""
    return [_bar(c, idx=i) for i, c in enumerate(closes)]


def _rising_closes(n: int = 21, start: float = 100.0, step: float = 1.0) -> list[Decimal]:
    """Monotonically rising closes ensuring short_ma > long_ma."""
    return [Decimal(str(start + step * i)) for i in range(n)]


def _falling_closes(n: int = 21, start: float = 120.0, step: float = 1.0) -> list[Decimal]:
    """Monotonically falling closes ensuring short_ma < long_ma."""
    return [Decimal(str(start - step * i)) for i in range(n)]


class _FakeContext:
    """In-process Context double — returns whatever history we hand it."""

    def __init__(
        self,
        history: Sequence[Bar],
        position: Position | None = None,
    ) -> None:
        self._history = list(history)
        self._position = position
        self.submitted: list[OrderProposal] = []
        self.decisions: list[dict[str, Any]] = []

    def now(self) -> datetime:
        return UTC_NOW

    def get_position(self, symbol: Symbol) -> Position | None:
        del symbol
        return self._position

    def get_history(
        self,
        symbol: Symbol,
        timeframe: Timeframe,
        n: int,
    ) -> Sequence[Bar]:
        del symbol, timeframe
        return self._history[-n:]

    def submit_proposal(self, proposal: OrderProposal) -> None:
        self.submitted.append(proposal)

    def log_decision(self, decision: dict[str, Any]) -> None:
        self.decisions.append(decision)


def _make_long_position(qty: Decimal = Decimal("1")) -> Position:
    return Position(
        symbol=SYMBOL,
        qty=qty,
        avg_entry=Decimal("95"),
        mark=Decimal("100"),
        unrealized_pnl=Decimal("5"),
        side="BUY",
        asset_class="crypto",
        opened_at=UTC_NOW - timedelta(hours=1),
        subsystem_tag="trend_follow",
    )


# ---------------------------------------------------------------------------
# Test cases
# ---------------------------------------------------------------------------


def test_long_entry_on_breakout() -> None:
    """regime=trending_up, short_ma > long_ma, close > short_ma, flat -> BUY."""
    closes = _rising_closes(n=21)  # 100..120
    ctx = _FakeContext(_history(closes))
    strat = TrendFollow(ctx, {"symbol": str(SYMBOL), "timeframe": TIMEFRAME})
    strat.state["regime"] = "trending_up"

    # Current bar close = 125 — comfortably above the short MA (~116.5)
    # of the rising window.
    bar = _bar(Decimal("125"), idx=22, high=Decimal("130"))
    proposals = list(strat.on_candle(bar))

    assert len(proposals) == 1, f"expected 1 BUY proposal, got {proposals}"
    p = proposals[0]
    assert p.side == "BUY"
    assert p.symbol == SYMBOL
    assert p.qty > 0
    assert 0.4 <= strat.last_conviction <= 0.95


def test_no_entry_in_wrong_regime() -> None:
    """Same MA/close setup but regime=mean_reverting -> no proposals."""
    closes = _rising_closes(n=21)
    ctx = _FakeContext(_history(closes))
    strat = TrendFollow(ctx, {"symbol": str(SYMBOL), "timeframe": TIMEFRAME})
    strat.state["regime"] = "mean_reverting"

    bar = _bar(Decimal("125"), idx=22, high=Decimal("130"))
    proposals = list(strat.on_candle(bar))

    assert proposals == []


def test_no_entry_when_ma_relation_wrong() -> None:
    """regime=trending_up but short_ma < long_ma -> no entry."""
    # Falling closes: recent values are LOWER than older ones, so the
    # short SMA (last 8) is BELOW the long SMA (last 21).
    closes = _falling_closes(n=21)  # 120..100
    ctx = _FakeContext(_history(closes))
    strat = TrendFollow(ctx, {"symbol": str(SYMBOL), "timeframe": TIMEFRAME})
    strat.state["regime"] = "trending_up"

    # Close above the short MA, but short_ma < long_ma so entry must be denied.
    bar = _bar(Decimal("150"), idx=22, high=Decimal("160"))
    proposals = list(strat.on_candle(bar))

    assert proposals == []


def test_exit_on_momentum_break() -> None:
    """Currently long, close < short_ma -> SELL full qty."""
    closes = _rising_closes(n=21)  # short_ma ~ 116.5
    qty = Decimal("1.23")
    ctx = _FakeContext(_history(closes), position=_make_long_position(qty))
    strat = TrendFollow(ctx, {"symbol": str(SYMBOL), "timeframe": TIMEFRAME})
    # Regime still healthy — only the momentum break should trigger exit.
    strat.state["regime"] = "trending_up"

    # close=100 is well below the short MA -> momentum break exit.
    bar = _bar(Decimal("100"), idx=22, high=Decimal("101"))
    proposals = list(strat.on_candle(bar))

    assert len(proposals) == 1, f"expected 1 SELL exit, got {proposals}"
    p = proposals[0]
    assert p.side == "SELL"
    assert p.symbol == SYMBOL
    assert p.qty == qty


def test_exit_on_regime_degrade() -> None:
    """Currently long, close > short_ma, regime flips to trending_down -> SELL."""
    closes = _rising_closes(n=21)
    qty = Decimal("0.5")
    ctx = _FakeContext(_history(closes), position=_make_long_position(qty))
    strat = TrendFollow(ctx, {"symbol": str(SYMBOL), "timeframe": TIMEFRAME})
    strat.state["regime"] = "trending_down"

    # close=125 keeps us above short_ma — only the regime flip should fire exit.
    bar = _bar(Decimal("125"), idx=22, high=Decimal("130"))
    proposals = list(strat.on_candle(bar))

    assert len(proposals) == 1, f"expected SELL on regime degrade, got {proposals}"
    p = proposals[0]
    assert p.side == "SELL"
    assert p.qty == qty


def test_insufficient_history_returns_flat() -> None:
    """Only 5 bars in history when long_window=21 -> no proposals."""
    closes = _rising_closes(n=5)
    ctx = _FakeContext(_history(closes))
    strat = TrendFollow(ctx, {"symbol": str(SYMBOL), "timeframe": TIMEFRAME})
    strat.state["regime"] = "trending_up"

    bar = _bar(Decimal("125"), idx=6, high=Decimal("130"))
    proposals = list(strat.on_candle(bar))

    assert proposals == []


def test_conviction_clamped() -> None:
    """Extreme breakout -> conviction caps at 0.95."""
    closes = _rising_closes(n=21)  # short_ma ~ 116.5
    ctx = _FakeContext(_history(closes))
    strat = TrendFollow(ctx, {"symbol": str(SYMBOL), "timeframe": TIMEFRAME})
    strat.state["regime"] = "trending_up"

    # Absurd breakout — close 10x the short MA — must still clamp at 0.95.
    bar = _bar(Decimal("10000"), idx=22, high=Decimal("10001"))
    proposals = list(strat.on_candle(bar))

    assert len(proposals) == 1
    assert proposals[0].side == "BUY"
    assert strat.last_conviction == pytest.approx(0.95)
