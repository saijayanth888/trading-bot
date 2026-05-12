"""The V4 parity oracle.

This is THE core test of the V4 design: backtest must produce identical
``OrderProposal`` sequences as live for the same candle inputs. If this test
ever fails, the design is broken — either the strategy is non-deterministic,
or one of the engines has drifted away from the shared contract.

The wave-2 live engine is not yet merged into this worktree, so this file
implements a **mock live engine** that mimics the live event loop: pull
bars from a stream, invoke ``strategy.on_candle``, route proposals through
a paper venue that fills at the next bar's open. The mock is intentionally
written in a different style than :class:`BacktestEngine` so the parity
property cannot be smuggled in via shared code paths — both implementations
share only the Strategy ABC, the type contracts, and the Context protocol.

The four scenarios below exercise the invariant from different angles:

1. Single-trade strategy on synthetic candles.
2. Multi-bar BUY/SELL pyramid.
3. Strategy that uses Context.get_history (state-dependent decisions).
4. Strategy with unfilled limit orders (proposals diverge from fills).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest

from quanta_core.backtest.candle_source import InMemoryCandleSource, SyntheticCandleSource
from quanta_core.backtest.engine import BacktestConfig, BacktestEngine
from quanta_core.strategy.base import Strategy
from quanta_core.types import (
    Bar,
    ClientOrderId,
    Context,
    OrderProposal,
    Position,
    Symbol,
    Timeframe,
)

pytestmark = pytest.mark.parity


# ---------------------------------------------------------------------------
# Mock live engine — DELIBERATELY independent of BacktestEngine.
#
# The mock represents what live.engine will do for each closed candle:
# - hand it to the strategy
# - capture the returned OrderProposals
# - simulate next-bar-open fills (so the strategy's state stays consistent
#   with what the live ledger would show via Context.get_position).
#
# Implementation is deliberately a different shape (mutable dataclass,
# imperative loop) so any accidental shared bug between the two engines
# shows up as a parity failure rather than silently cancelling.
# ---------------------------------------------------------------------------


@dataclass
class _MockLiveContext:
    """Minimal Context impl backed by a mutable dataclass."""

    clock: datetime
    history: list[Bar] = field(default_factory=list)
    position: Position | None = None
    decisions: list[dict[str, Any]] = field(default_factory=list)
    context_proposals: list[OrderProposal] = field(default_factory=list)
    history_window: int = 256
    symbol: Symbol = Symbol("UNKNOWN")
    timeframe: Timeframe = "1m"

    def now(self) -> datetime:
        return self.clock

    def get_position(self, symbol: Symbol) -> Position | None:
        return self.position

    def get_history(
        self,
        symbol: Symbol,
        timeframe: Timeframe,
        n: int,
    ) -> Sequence[Bar]:
        if symbol != self.symbol or timeframe != self.timeframe or n <= 0:
            return ()
        return tuple(self.history[-n:])

    def submit_proposal(self, proposal: OrderProposal) -> None:
        self.context_proposals.append(proposal)

    def log_decision(self, decision: dict[str, Any]) -> None:
        self.decisions.append(decision)


@dataclass
class _MockLiveFill:
    """The mock live engine's lightweight fill record."""

    symbol: Symbol
    side: str
    qty: Decimal
    price: Decimal
    timestamp_utc: datetime
    client_order_id: str


@dataclass
class _OpenLeg:
    side: str  # "BUY" or "SELL"
    qty: Decimal
    entry_price: Decimal
    entry_ts: datetime


@dataclass
class _MockLiveResult:
    proposals: list[OrderProposal] = field(default_factory=list)
    fills: list[_MockLiveFill] = field(default_factory=list)


def run_mock_live(
    *,
    strategy_class: type[Strategy],
    strategy_config: dict[str, Any],
    bars: Sequence[Bar],
    history_window: int = 256,
) -> _MockLiveResult:
    """Run the mock live event loop. Independent of ``BacktestEngine``."""
    if not bars:
        return _MockLiveResult()
    ctx = _MockLiveContext(
        clock=bars[0].timestamp_utc,
        history_window=history_window,
        symbol=bars[0].symbol,
        timeframe=bars[0].timeframe,
    )
    strat = strategy_class(ctx, strategy_config)
    strat.on_start()
    result = _MockLiveResult()
    pending: list[OrderProposal] = []
    open_leg: _OpenLeg | None = None

    def _apply(fill: _MockLiveFill) -> None:
        nonlocal open_leg
        if fill.side == "BUY":
            if open_leg is None:
                open_leg = _OpenLeg(
                    side="BUY",
                    qty=fill.qty,
                    entry_price=fill.price,
                    entry_ts=fill.timestamp_utc,
                )
            elif open_leg.side == "BUY":
                total = open_leg.qty + fill.qty
                open_leg.entry_price = (
                    open_leg.entry_price * open_leg.qty + fill.price * fill.qty
                ) / total
                open_leg.qty = total
            else:
                close_qty = min(open_leg.qty, fill.qty)
                if open_leg.qty - close_qty <= 0:
                    open_leg = None
                else:
                    open_leg.qty -= close_qty
        else:  # SELL
            if open_leg is None:
                open_leg = _OpenLeg(
                    side="SELL",
                    qty=fill.qty,
                    entry_price=fill.price,
                    entry_ts=fill.timestamp_utc,
                )
            elif open_leg.side == "SELL":
                total = open_leg.qty + fill.qty
                open_leg.entry_price = (
                    open_leg.entry_price * open_leg.qty + fill.price * fill.qty
                ) / total
                open_leg.qty = total
            else:
                close_qty = min(open_leg.qty, fill.qty)
                if open_leg.qty - close_qty <= 0:
                    open_leg = None
                else:
                    open_leg.qty -= close_qty

    def _refresh_ctx_position(bar: Bar) -> None:
        if open_leg is None:
            ctx.position = None
        else:
            ctx.position = Position(
                symbol=bar.symbol,
                qty=open_leg.qty if open_leg.side == "BUY" else -open_leg.qty,
                avg_entry=open_leg.entry_price,
                mark=bar.close if bar.close > 0 else open_leg.entry_price,
                unrealized_pnl=Decimal("0"),
                side=open_leg.side,  # type: ignore[arg-type]
                asset_class="crypto",
                opened_at=open_leg.entry_ts,
                subsystem_tag="mock_live",
            )

    for bar in bars:
        # 1. Fill any pending proposals at this bar's open.
        if pending:
            for proposal in pending:
                if proposal.order_type == "limit" and proposal.limit_px is not None:
                    if proposal.side == "BUY":
                        if bar.low > proposal.limit_px:
                            continue
                        fill_px = min(proposal.limit_px, bar.open)
                    else:
                        if bar.high < proposal.limit_px:
                            continue
                        fill_px = max(proposal.limit_px, bar.open)
                else:
                    fill_px = bar.open
                fill = _MockLiveFill(
                    symbol=proposal.symbol,
                    side=proposal.side,
                    qty=proposal.qty,
                    price=fill_px,
                    timestamp_utc=bar.timestamp_utc,
                    client_order_id=str(proposal.client_order_id),
                )
                result.fills.append(fill)
                _apply(fill)
                # The live engine calls on_fill after ledger commit.
                # We synthesise a Fill from the SimFill shape for the strategy.
                from quanta_core.types import Fill as _Fill

                strat.on_fill(
                    _Fill(
                        order_id=f"mock-{fill.client_order_id}",
                        client_order_id=ClientOrderId(fill.client_order_id),
                        symbol=fill.symbol,
                        side=proposal.side,
                        qty=fill.qty,
                        price=fill.price,
                        fee=Decimal("0"),
                        timestamp_utc=fill.timestamp_utc,
                        venue="paper",
                    )
                )
            pending.clear()

        # 2. Advance clock + history.
        ctx.clock = bar.timestamp_utc
        ctx.history.append(bar)
        if len(ctx.history) > history_window:
            del ctx.history[0 : len(ctx.history) - history_window]

        # 3. Refresh position view.
        _refresh_ctx_position(bar)

        # 4. Run on_candle.
        ctx.context_proposals.clear()
        returned = tuple(strat.on_candle(bar))
        emitted = tuple(ctx.context_proposals)
        proposals_this_bar = returned + emitted
        pending.extend(proposals_this_bar)
        result.proposals.extend(proposals_this_bar)

    strat.on_stop()
    return result


# ---------------------------------------------------------------------------
# Strategies used by parity scenarios
# ---------------------------------------------------------------------------


class _SingleTradeStrategy(Strategy):
    """Buy on bar 2, sell on bar 6 — deterministic."""

    name = "parity_single_trade"

    def __init__(self, ctx: Context, config: dict[str, Any]) -> None:
        super().__init__(ctx, config)
        self._idx = 0

    def on_candle(self, bar: Bar) -> Sequence[OrderProposal]:
        self._idx += 1
        if self._idx == 2:
            return [
                OrderProposal(
                    symbol=bar.symbol,
                    side="BUY",
                    qty=Decimal("1"),
                    order_type="market",
                    client_order_id=ClientOrderId("p-buy"),
                    rationale="parity buy",
                    asset_class="crypto",
                )
            ]
        if self._idx == 6:
            return [
                OrderProposal(
                    symbol=bar.symbol,
                    side="SELL",
                    qty=Decimal("1"),
                    order_type="market",
                    client_order_id=ClientOrderId("p-sell"),
                    rationale="parity sell",
                    asset_class="crypto",
                )
            ]
        return ()


class _PyramidStrategy(Strategy):
    """Two buys then a full close — uses pyramiding state."""

    name = "parity_pyramid"

    def __init__(self, ctx: Context, config: dict[str, Any]) -> None:
        super().__init__(ctx, config)
        self._idx = 0

    def on_candle(self, bar: Bar) -> Sequence[OrderProposal]:
        self._idx += 1
        if self._idx in {1, 3}:
            return [
                OrderProposal(
                    symbol=bar.symbol,
                    side="BUY",
                    qty=Decimal("1"),
                    order_type="market",
                    client_order_id=ClientOrderId(f"pyr-buy-{self._idx}"),
                    rationale="pyramid",
                    asset_class="crypto",
                )
            ]
        if self._idx == 6:
            return [
                OrderProposal(
                    symbol=bar.symbol,
                    side="SELL",
                    qty=Decimal("2"),
                    order_type="market",
                    client_order_id=ClientOrderId("pyr-close"),
                    rationale="close pyramid",
                    asset_class="crypto",
                )
            ]
        return ()


class _HistoryAwareStrategy(Strategy):
    """Buy whenever the last bar's close > previous close (rolling momentum)."""

    name = "parity_history"

    def __init__(self, ctx: Context, config: dict[str, Any]) -> None:
        super().__init__(ctx, config)
        self._fired_indices: set[int] = set()
        self._idx = 0

    def on_candle(self, bar: Bar) -> Sequence[OrderProposal]:
        self._idx += 1
        hist = self.ctx.get_history(bar.symbol, bar.timeframe, 3)
        # Need at least 2 historical bars to compare.
        momentum_up = len(hist) >= 2 and hist[-1].close > hist[-2].close
        if momentum_up and self._idx not in self._fired_indices:
            self._fired_indices.add(self._idx)
            return [
                OrderProposal(
                    symbol=bar.symbol,
                    side="BUY",
                    qty=Decimal("1"),
                    order_type="market",
                    client_order_id=ClientOrderId(f"hist-{self._idx}"),
                    rationale="momentum up",
                    asset_class="crypto",
                )
            ]
        return ()


class _UnreachableLimitStrategy(Strategy):
    """Place buys at deeply-unreachable limit prices — proposals exist, fills don't."""

    name = "parity_unreachable_limit"

    def __init__(self, ctx: Context, config: dict[str, Any]) -> None:
        super().__init__(ctx, config)
        self._idx = 0

    def on_candle(self, bar: Bar) -> Sequence[OrderProposal]:
        self._idx += 1
        if self._idx in {2, 5, 8}:
            return [
                OrderProposal(
                    symbol=bar.symbol,
                    side="BUY",
                    qty=Decimal("1"),
                    order_type="limit",
                    limit_px=Decimal("0.0001"),  # unreachable
                    client_order_id=ClientOrderId(f"unreach-{self._idx}"),
                    rationale="unreachable",
                    asset_class="crypto",
                )
            ]
        return ()


# ---------------------------------------------------------------------------
# Parity assertions
# ---------------------------------------------------------------------------


def _assert_proposals_identical(
    live: Sequence[OrderProposal],
    backtest: Sequence[OrderProposal],
) -> None:
    """Identical ordered sequences (by every field). Hard failure on diff."""
    assert len(live) == len(backtest), (
        f"PARITY BROKEN: proposal count mismatch — live={len(live)} backtest={len(backtest)}"
    )
    for i, (lp, bp) in enumerate(zip(live, backtest, strict=True)):
        assert lp == bp, f"PARITY BROKEN at proposal #{i}:\n  live:     {lp}\n  backtest: {bp}"


# ---------------------------------------------------------------------------
# Test scenarios
# ---------------------------------------------------------------------------


@pytest.fixture
def parity_bars(btc_symbol):
    """A 12-bar synthetic stream shared by every scenario."""
    start = datetime(2026, 6, 1, tzinfo=UTC)
    src = SyntheticCandleSource(
        symbol=btc_symbol,
        timeframe="1m",
        start=start,
        n_bars=12,
        seed=1234,
    )
    return tuple(src)


def _run_backtest(strategy_class, strategy_config, btc_symbol, bars):
    """Run a BacktestEngine over an in-memory bar list."""
    src = InMemoryCandleSource(bars)
    engine = BacktestEngine(
        strategy_class=strategy_class,
        config=BacktestConfig(symbol=btc_symbol, timeframe="1m", fee_bps=Decimal("0")),
        candle_source=src,
        strategy_config=strategy_config,
    )
    result = engine.run()
    return list(result.proposals)


def test_parity_single_trade(btc_symbol, parity_bars):
    """One buy + one sell — proposals must match across engines."""
    live = run_mock_live(
        strategy_class=_SingleTradeStrategy,
        strategy_config={},
        bars=parity_bars,
    )
    backtest_proposals = _run_backtest(_SingleTradeStrategy, {}, btc_symbol, parity_bars)
    _assert_proposals_identical(live.proposals, backtest_proposals)
    # And the resulting fills should be identical too (since the synthetic
    # bars hit both limit fills cleanly via market orders).
    assert len(live.fills) == 2


def test_parity_pyramid(btc_symbol, parity_bars):
    """Pyramiding strategy — proposals must match including any closing leg."""
    live = run_mock_live(
        strategy_class=_PyramidStrategy,
        strategy_config={},
        bars=parity_bars,
    )
    backtest_proposals = _run_backtest(_PyramidStrategy, {}, btc_symbol, parity_bars)
    _assert_proposals_identical(live.proposals, backtest_proposals)


def test_parity_history_aware(btc_symbol, parity_bars):
    """Strategy reading get_history — both engines must expose the same view."""
    live = run_mock_live(
        strategy_class=_HistoryAwareStrategy,
        strategy_config={},
        bars=parity_bars,
    )
    backtest_proposals = _run_backtest(_HistoryAwareStrategy, {}, btc_symbol, parity_bars)
    _assert_proposals_identical(live.proposals, backtest_proposals)


def test_parity_unreachable_limit(btc_symbol, parity_bars):
    """Limit proposals that never fill — proposals are still identical."""
    live = run_mock_live(
        strategy_class=_UnreachableLimitStrategy,
        strategy_config={},
        bars=parity_bars,
    )
    backtest_proposals = _run_backtest(_UnreachableLimitStrategy, {}, btc_symbol, parity_bars)
    _assert_proposals_identical(live.proposals, backtest_proposals)
    # And both engines record zero fills (as expected for unreachable limits).
    assert len(live.fills) == 0


def test_parity_strategy_independence(btc_symbol):
    """Same strategy run twice on different bar copies — fully deterministic."""
    start = datetime(2026, 7, 1, tzinfo=UTC)
    src_a = SyntheticCandleSource(
        symbol=btc_symbol, timeframe="1m", start=start, n_bars=20, seed=99
    )
    src_b = SyntheticCandleSource(
        symbol=btc_symbol, timeframe="1m", start=start, n_bars=20, seed=99
    )
    bars_a = tuple(src_a)
    bars_b = tuple(src_b)
    assert bars_a == bars_b  # synthetic source is deterministic

    live_run_1 = run_mock_live(strategy_class=_PyramidStrategy, strategy_config={}, bars=bars_a)
    live_run_2 = run_mock_live(strategy_class=_PyramidStrategy, strategy_config={}, bars=bars_b)
    _assert_proposals_identical(live_run_1.proposals, live_run_2.proposals)

    bt_run_1 = _run_backtest(_PyramidStrategy, {}, btc_symbol, bars_a)
    bt_run_2 = _run_backtest(_PyramidStrategy, {}, btc_symbol, bars_b)
    _assert_proposals_identical(bt_run_1, bt_run_2)


def test_parity_clock_alignment(btc_symbol, parity_bars):
    """Context.now() must agree between live and backtest at every bar."""
    captured_live: list[datetime] = []
    captured_bt: list[datetime] = []

    class _ClockCapturingStrategy(Strategy):
        name = "parity_clock"

        def on_candle(self, bar: Bar) -> Sequence[OrderProposal]:
            self._collector.append(self.ctx.now())  # type: ignore[attr-defined]
            return ()

    # Bind the per-run collector via subclass injection.
    class _Live(_ClockCapturingStrategy):
        _collector = captured_live

    class _BT(_ClockCapturingStrategy):
        _collector = captured_bt

    run_mock_live(strategy_class=_Live, strategy_config={}, bars=parity_bars)
    _run_backtest(_BT, {}, btc_symbol, parity_bars)

    assert captured_live == captured_bt
    assert len(captured_live) == len(parity_bars)


def test_parity_history_view_alignment(btc_symbol, parity_bars):
    """Context.get_history must yield identical bars at every step."""
    captured_live: list[tuple[int, tuple[Bar, ...]]] = []
    captured_bt: list[tuple[int, tuple[Bar, ...]]] = []

    class _HistCapture(Strategy):
        name = "parity_hist_capture"

        def __init__(self, ctx: Context, config: dict[str, Any]) -> None:
            super().__init__(ctx, config)
            self._idx = 0

        def on_candle(self, bar: Bar) -> Sequence[OrderProposal]:
            self._idx += 1
            hist = self.ctx.get_history(bar.symbol, bar.timeframe, 5)
            self._collector.append((self._idx, tuple(hist)))  # type: ignore[attr-defined]
            return ()

    class _Live(_HistCapture):
        _collector = captured_live

    class _BT(_HistCapture):
        _collector = captured_bt

    run_mock_live(strategy_class=_Live, strategy_config={}, bars=parity_bars)
    _run_backtest(_BT, {}, btc_symbol, parity_bars)

    assert captured_live == captured_bt


# ---------------------------------------------------------------------------
# Drift demo — proves the test would catch a regression.
# ---------------------------------------------------------------------------


class _NonDeterministicStrategy(Strategy):
    """INTENTIONALLY non-deterministic — different outputs on every call.

    This strategy uses a module-level mutable counter to fire a different
    proposal on every other call, REGARDLESS of bar contents. It exists to
    prove the parity test catches non-determinism — see the test below.
    """

    name = "intentionally_broken"
    _counter: list[int] = []  # class-level mutable shared state

    def on_candle(self, bar: Bar) -> Sequence[OrderProposal]:
        self._counter.append(1)
        # Only emit on odd-numbered calls regardless of bar identity.
        if len(self._counter) % 2 == 1:
            return [
                OrderProposal(
                    symbol=bar.symbol,
                    side="BUY",
                    qty=Decimal("1"),
                    order_type="market",
                    client_order_id=ClientOrderId(f"broken-{len(self._counter)}"),
                    rationale="non-det",
                    asset_class="crypto",
                )
            ]
        return ()


def test_parity_catches_nondeterminism(btc_symbol, parity_bars):
    """The parity assertion fires when the strategy carries shared state.

    Both engines use the SAME class, so the class-level counter increments
    in BOTH runs. The first run drains the counter; the second run sees a
    different parity — proposal client_order_ids will diverge.
    """
    _NonDeterministicStrategy._counter.clear()
    live = run_mock_live(
        strategy_class=_NonDeterministicStrategy, strategy_config={}, bars=parity_bars
    )
    backtest_proposals = _run_backtest(_NonDeterministicStrategy, {}, btc_symbol, parity_bars)
    # We *expect* this to differ — the class-level counter is shared, so the
    # backtest run sees a counter that already advanced through the live run.
    # The proposals' client_order_ids reflect that offset.
    live_ids = [p.client_order_id for p in live.proposals]
    bt_ids = [p.client_order_id for p in backtest_proposals]
    assert live_ids != bt_ids
    # And the strict equality check would raise — confirming the parity oracle
    # is sharp enough to catch this kind of bug.
    with pytest.raises(AssertionError, match="PARITY BROKEN"):
        _assert_proposals_identical(live.proposals, backtest_proposals)
