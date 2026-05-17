"""Tests for the V4 ``MeanRevBB`` Bollinger mean-reversion strategy.

The V4 ``Strategy`` ABC only knows about ``OrderProposal`` — there is no
``Signal`` type with ``side=LONG/FLAT`` and a ``conviction`` field. We
therefore translate the spec's vocabulary into the ABC's contract:

* spec ``side=LONG``       -> ``OrderProposal(side="BUY", ...)``
* spec ``side=FLAT``       -> empty ``Sequence[OrderProposal]``
* spec ``side=EXIT_LONG``  -> ``OrderProposal(side="SELL", ...)`` when a
                              long position is currently open
* spec ``conviction``      -> stored as ``conviction`` on the returned
                              proposal's ``rationale`` JSON blob AND mapped
                              to qty via ``base_qty * conviction``. We also
                              expose the most-recent conviction on the
                              strategy instance as ``last_conviction`` so
                              tests can assert on it directly without
                              parsing JSON.

Regime is read from ``self.state["regime"]`` (the spec's literal wording);
``state`` is seeded from ``config["state"]`` and mutable by the engine /
tests. A missing or "unknown" regime is treated as FLAT.

All tests are synchronous per DESIGN-LOCK §5.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest

from quanta_core.strategy.mean_rev_bb import MeanRevBB
from quanta_core.types import (
    Bar,
    ClientOrderId,
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

# A 20-bar history with a clean, known mean and std so the BB bands are
# trivially computable: closes are 100, 101, 100, 99, 100, 101, ... -> mean=100.
# Using a tight band gives us a predictable lower / middle for the assertions.
_FLAT_CLOSES = [Decimal("100"), Decimal("101"), Decimal("100"), Decimal("99")] * 5
assert len(_FLAT_CLOSES) == 20


def _bar(close: Decimal, *, idx: int = 0, low: Decimal | None = None) -> Bar:
    """Build a Bar with sane OHLC consistency around ``close``."""
    low = low if low is not None else min(close, Decimal("90"))
    high = max(close, Decimal("110"))
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


def _make_long_position() -> Position:
    return Position(
        symbol=SYMBOL,
        qty=Decimal("1"),
        avg_entry=Decimal("95"),
        mark=Decimal("100"),
        unrealized_pnl=Decimal("5"),
        side="BUY",
        asset_class="crypto",
        opened_at=UTC_NOW - timedelta(hours=1),
        subsystem_tag="mean_rev_bb",
    )


# ---------------------------------------------------------------------------
# Test cases (all sync per DESIGN-LOCK §5)
# ---------------------------------------------------------------------------


def test_long_signal_at_lower_band() -> None:
    """Close at lower band + regime=mean_reverting -> BUY proposal."""
    # 20 closes at exactly 100 -> std=0, BB collapses. Add tiny noise.
    closes = list(_FLAT_CLOSES)
    ctx = _FakeContext(_history(closes))
    strat = MeanRevBB(ctx, {"symbol": str(SYMBOL), "timeframe": TIMEFRAME})
    strat.state["regime"] = "mean_reverting"
    # _MIN_ENTRY_PROBABILITY=0.85 gate (2026-05-15) requires explicit
    # regime_probability for any test that expects a BUY proposal.
    strat.state["regime_probability"] = 0.9

    # Build a current bar whose close is well below the lower band (mean=100,
    # std ~ 0.7, lower ~ 98.6). Close=95 is comfortably below.
    bar = _bar(Decimal("95"), idx=21)
    proposals = list(strat.on_candle(bar))

    assert len(proposals) == 1, f"expected 1 BUY proposal, got {proposals}"
    p = proposals[0]
    assert p.side == "BUY"
    assert p.symbol == SYMBOL
    assert p.qty > 0
    # Conviction (exposed on the strategy) must be in [0.4, 0.95].
    assert 0.4 <= strat.last_conviction <= 0.95


def test_long_signal_also_fires_in_trending_up() -> None:
    """Spec: regime in {trending_up, mean_reverting} permits entries."""
    closes = list(_FLAT_CLOSES)
    ctx = _FakeContext(_history(closes))
    strat = MeanRevBB(ctx, {"symbol": str(SYMBOL), "timeframe": TIMEFRAME})
    strat.state["regime"] = "trending_up"
    strat.state["regime_probability"] = 0.9

    bar = _bar(Decimal("95"), idx=21)
    proposals = list(strat.on_candle(bar))

    assert len(proposals) == 1
    assert proposals[0].side == "BUY"


def test_exit_signal_at_middle_band() -> None:
    """Currently long + close back at middle BB -> SELL (exit) proposal."""
    closes = list(_FLAT_CLOSES)
    ctx = _FakeContext(_history(closes), position=_make_long_position())
    strat = MeanRevBB(ctx, {"symbol": str(SYMBOL), "timeframe": TIMEFRAME})
    strat.state["regime"] = "mean_reverting"

    # Mean is 100; a close at 101 is above the middle band -> exit.
    bar = _bar(Decimal("101"), idx=21)
    proposals = list(strat.on_candle(bar))

    assert len(proposals) == 1, f"expected 1 SELL exit, got {proposals}"
    p = proposals[0]
    assert p.side == "SELL"
    assert p.symbol == SYMBOL
    assert p.qty == _make_long_position().qty


def test_wrong_regime_returns_flat() -> None:
    """Close at lower band + regime=high_volatility -> no proposals."""
    closes = list(_FLAT_CLOSES)
    ctx = _FakeContext(_history(closes))
    strat = MeanRevBB(ctx, {"symbol": str(SYMBOL), "timeframe": TIMEFRAME})
    strat.state["regime"] = "high_volatility"

    bar = _bar(Decimal("95"), idx=21)
    proposals = list(strat.on_candle(bar))

    assert proposals == []


def test_default_no_signal() -> None:
    """Close mid-band + regime=trending_up -> no proposals."""
    closes = list(_FLAT_CLOSES)
    ctx = _FakeContext(_history(closes))
    strat = MeanRevBB(ctx, {"symbol": str(SYMBOL), "timeframe": TIMEFRAME})
    strat.state["regime"] = "trending_up"

    # Close exactly at mean (100) -> neither below lower nor above middle when
    # flat -> no entry, no exit.
    bar = _bar(Decimal("100"), idx=21)
    proposals = list(strat.on_candle(bar))

    assert proposals == []


def test_unknown_regime_returns_flat() -> None:
    """Missing or unknown regime -> no proposals even with a perfect entry."""
    closes = list(_FLAT_CLOSES)
    ctx = _FakeContext(_history(closes))
    strat = MeanRevBB(ctx, {"symbol": str(SYMBOL), "timeframe": TIMEFRAME})
    # Don't set state["regime"] at all -> missing key.

    bar = _bar(Decimal("95"), idx=21)
    proposals = list(strat.on_candle(bar))
    assert proposals == []

    # Explicit "unknown" must also be FLAT.
    strat.state["regime"] = "unknown"
    proposals = list(strat.on_candle(bar))
    assert proposals == []


# ---------------------------------------------------------------------------
# Conviction clamp — bonus to anchor the [0.4, 0.95] spec contract.
# ---------------------------------------------------------------------------


def test_conviction_is_clamped_to_max() -> None:
    """A close miles below the lower band still clamps conviction to 0.95."""
    closes = list(_FLAT_CLOSES)
    ctx = _FakeContext(_history(closes))
    strat = MeanRevBB(ctx, {"symbol": str(SYMBOL), "timeframe": TIMEFRAME})
    strat.state["regime"] = "mean_reverting"
    strat.state["regime_probability"] = 0.9

    # Close way below band — provide a sufficiently low ``low`` so the Bar
    # validator is satisfied.
    bar = _bar(Decimal("50"), idx=21, low=Decimal("50"))
    proposals = list(strat.on_candle(bar))
    assert len(proposals) == 1
    assert strat.last_conviction == pytest.approx(0.95)


def test_insufficient_history_returns_flat() -> None:
    """Fewer than ``window`` bars in history -> warm-up; no proposals."""
    closes = list(_FLAT_CLOSES)[:5]  # only 5 bars, need 20
    ctx = _FakeContext(_history(closes))
    strat = MeanRevBB(ctx, {"symbol": str(SYMBOL), "timeframe": TIMEFRAME})
    strat.state["regime"] = "mean_reverting"

    bar = _bar(Decimal("95"), idx=6)
    proposals = list(strat.on_candle(bar))
    assert proposals == []
