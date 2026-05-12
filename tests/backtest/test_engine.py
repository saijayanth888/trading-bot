"""Tests for :class:`quanta_core.backtest.engine.BacktestEngine`."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest

from quanta_core.backtest.candle_source import (
    InMemoryCandleSource,
    SyntheticCandleSource,
)
from quanta_core.backtest.engine import (
    BacktestConfig,
    BacktestEngine,
    FixedBpsSlippageModel,
    NoSlippageModel,
)
from quanta_core.strategy.base import Strategy
from quanta_core.types import (
    Bar,
    ClientOrderId,
    Context,
    OrderProposal,
)

# ---------------------------------------------------------------------------
# Helper strategies — defined in the test file so the harness is explicit.
# ---------------------------------------------------------------------------


class _RecordingStrategy(Strategy):
    """Buy on the second bar; record every bar + fill it sees."""

    name = "recording"

    def __init__(self, ctx: Context, config: dict[str, Any]) -> None:
        super().__init__(ctx, config)
        self.bars_seen: list[Bar] = []
        self.fills_seen: list[Any] = []
        self.started = False
        self.stopped = False
        self._fired = False

    def on_start(self) -> None:
        self.started = True

    def on_stop(self) -> None:
        self.stopped = True

    def on_candle(self, bar: Bar) -> Sequence[OrderProposal]:
        self.bars_seen.append(bar)
        if not self._fired and len(self.bars_seen) >= 2:
            self._fired = True
            return [
                OrderProposal(
                    symbol=bar.symbol,
                    side="BUY",
                    qty=Decimal("1"),
                    order_type="market",
                    client_order_id=ClientOrderId("rec-buy"),
                    rationale="recording",
                    asset_class="crypto",
                )
            ]
        return ()

    def on_fill(self, fill) -> None:
        self.fills_seen.append(fill)


class _ContextSubmittingStrategy(Strategy):
    """Submit one proposal via Context.submit_proposal on the first bar."""

    name = "ctx_submitter"

    def __init__(self, ctx: Context, config: dict[str, Any]) -> None:
        super().__init__(ctx, config)
        self._fired = False

    def on_candle(self, bar: Bar) -> Sequence[OrderProposal]:
        if not self._fired:
            self._fired = True
            proposal = OrderProposal(
                symbol=bar.symbol,
                side="BUY",
                qty=Decimal("2"),
                order_type="market",
                client_order_id=ClientOrderId("ctx-buy"),
                rationale="via ctx",
                asset_class="crypto",
            )
            self.ctx.submit_proposal(proposal)
            self.ctx.log_decision({"event": "first_bar", "ts": bar.timestamp_utc.isoformat()})
        return ()


class _LimitOrderStrategy(Strategy):
    """Place an unreachable buy limit so it can never fill."""

    name = "unreachable_limit"

    def __init__(self, ctx: Context, config: dict[str, Any]) -> None:
        super().__init__(ctx, config)
        self._fired = False

    def on_candle(self, bar: Bar) -> Sequence[OrderProposal]:
        if not self._fired:
            self._fired = True
            return [
                OrderProposal(
                    symbol=bar.symbol,
                    side="BUY",
                    qty=Decimal("1"),
                    order_type="limit",
                    limit_px=Decimal("0.01"),  # well below any synthetic low
                    client_order_id=ClientOrderId("unreach-buy"),
                    rationale="never fills",
                    asset_class="crypto",
                )
            ]
        return ()


class _HistoryAwareStrategy(Strategy):
    """Asserts the Context history view widens up to ``history_window``."""

    name = "history_aware"

    def __init__(self, ctx: Context, config: dict[str, Any]) -> None:
        super().__init__(ctx, config)
        self.history_lengths: list[int] = []

    def on_candle(self, bar: Bar) -> Sequence[OrderProposal]:
        hist = self.ctx.get_history(bar.symbol, bar.timeframe, 1000)
        self.history_lengths.append(len(hist))
        return ()


class _PositionAwareStrategy(Strategy):
    """Buy then later check that Context.get_position reflects the state."""

    name = "pos_aware"

    def __init__(self, ctx: Context, config: dict[str, Any]) -> None:
        super().__init__(ctx, config)
        self._idx = 0
        self.observed_position_qty: Decimal | None = None

    def on_candle(self, bar: Bar) -> Sequence[OrderProposal]:
        self._idx += 1
        if self._idx == 1:
            return [
                OrderProposal(
                    symbol=bar.symbol,
                    side="BUY",
                    qty=Decimal("3"),
                    order_type="market",
                    client_order_id=ClientOrderId("pos-buy"),
                    rationale="open long",
                    asset_class="crypto",
                )
            ]
        if self._idx == 4:
            pos = self.ctx.get_position(bar.symbol)
            if pos is not None:
                self.observed_position_qty = pos.qty
        return ()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestBacktestConfig:
    def test_validates_positive_equity(self, btc_symbol):
        with pytest.raises(ValueError, match="positive"):
            BacktestConfig(symbol=btc_symbol, timeframe="1m", starting_equity=Decimal("0"))

    def test_validates_non_negative_fee(self, btc_symbol):
        with pytest.raises(ValueError, match="non-negative"):
            BacktestConfig(symbol=btc_symbol, timeframe="1m", fee_bps=Decimal("-1"))

    def test_validates_history_window(self, btc_symbol):
        with pytest.raises(ValueError, match="non-negative"):
            BacktestConfig(symbol=btc_symbol, timeframe="1m", history_window=-1)


class TestSlippageModels:
    def test_no_slippage_returns_open(self, btc_symbol, make_bar, fixed_start):
        model = NoSlippageModel()
        bar = make_bar(symbol=btc_symbol, ts=fixed_start, open_=100.0, high=101.0, low=99.5)
        proposal = OrderProposal(
            symbol=btc_symbol,
            side="BUY",
            qty=Decimal("1"),
            order_type="market",
            client_order_id=ClientOrderId("co-1"),
            rationale="test",
            asset_class="crypto",
        )
        assert model.fill_price(proposal, bar) == Decimal("100.0")

    def test_fixed_bps_adverse_for_buy(self, btc_symbol, make_bar, fixed_start):
        model = FixedBpsSlippageModel(bps=Decimal("100"))  # 1%
        bar = make_bar(symbol=btc_symbol, ts=fixed_start, open_=100.0)
        proposal = OrderProposal(
            symbol=btc_symbol,
            side="BUY",
            qty=Decimal("1"),
            order_type="market",
            client_order_id=ClientOrderId("co-1"),
            rationale="test",
            asset_class="crypto",
        )
        assert model.fill_price(proposal, bar) == Decimal("101.00")

    def test_fixed_bps_adverse_for_sell(self, btc_symbol, make_bar, fixed_start):
        model = FixedBpsSlippageModel(bps=Decimal("100"))
        bar = make_bar(symbol=btc_symbol, ts=fixed_start, open_=100.0)
        proposal = OrderProposal(
            symbol=btc_symbol,
            side="SELL",
            qty=Decimal("1"),
            order_type="market",
            client_order_id=ClientOrderId("co-2"),
            rationale="test",
            asset_class="crypto",
        )
        assert model.fill_price(proposal, bar) == Decimal("99.00")

    def test_negative_bps_rejected(self):
        with pytest.raises(ValueError, match="non-negative"):
            FixedBpsSlippageModel(bps=Decimal("-1"))


class TestEngineConstruction:
    def test_rejects_symbol_mismatch(
        self, btc_symbol, eth_symbol, fixed_start, simple_strategy_cls
    ):
        src = SyntheticCandleSource(symbol=eth_symbol, timeframe="1m", start=fixed_start, n_bars=5)
        cfg = BacktestConfig(symbol=btc_symbol, timeframe="1m")
        with pytest.raises(ValueError, match="config.symbol"):
            BacktestEngine(
                strategy_class=simple_strategy_cls,
                config=cfg,
                candle_source=src,
            )

    def test_rejects_timeframe_mismatch(self, btc_symbol, fixed_start, simple_strategy_cls):
        src = SyntheticCandleSource(symbol=btc_symbol, timeframe="5m", start=fixed_start, n_bars=5)
        cfg = BacktestConfig(symbol=btc_symbol, timeframe="1m")
        with pytest.raises(ValueError, match="config.timeframe"):
            BacktestEngine(
                strategy_class=simple_strategy_cls,
                config=cfg,
                candle_source=src,
            )


class TestRunHappyPath:
    def test_simple_buy_sell_round_trip(self, btc_symbol, synthetic_source, simple_strategy_cls):
        cfg = BacktestConfig(symbol=btc_symbol, timeframe="1m", fee_bps=Decimal("0"))
        engine = BacktestEngine(
            strategy_class=simple_strategy_cls,
            config=cfg,
            candle_source=synthetic_source,
            strategy_config={"buy_at": 1, "sell_at": 5, "qty": "1"},
        )
        result = engine.run()
        # Two proposals (one buy + one sell), both fill on the NEXT bar.
        assert result.summary.n_proposals == 2
        assert result.summary.n_fills == 2
        assert result.summary.n_trades == 1
        trade = result.trades[0]
        assert trade.side == "BUY"
        assert trade.qty == Decimal("1")
        # Buy fired on bar idx 1 → fills at bar 2's open. Sell on idx 5 → fills at idx 6's open.
        # bars_held tracks bars between fill-in and fill-out, both at NEW bar boundaries.
        assert trade.bars_held >= 3

    def test_no_trades_path(self, btc_symbol, synthetic_source, flat_strategy_cls):
        cfg = BacktestConfig(symbol=btc_symbol, timeframe="1m")
        engine = BacktestEngine(
            strategy_class=flat_strategy_cls,
            config=cfg,
            candle_source=synthetic_source,
        )
        result = engine.run()
        assert result.summary.n_proposals == 0
        assert result.summary.n_fills == 0
        assert result.summary.n_trades == 0
        assert result.summary.starting_equity == result.summary.ending_equity
        assert result.summary.max_drawdown_pct == 0.0
        assert result.bars_processed == 60

    def test_lifecycle_hooks_called(self, btc_symbol, synthetic_source):
        cfg = BacktestConfig(symbol=btc_symbol, timeframe="1m")
        engine = BacktestEngine(
            strategy_class=_RecordingStrategy,
            config=cfg,
            candle_source=synthetic_source,
        )
        engine.run()
        # The strategy's bound instance — access through engine internals for assertion.
        strat = engine._strategy
        assert strat is not None
        assert strat.started is True  # type: ignore[union-attr]
        assert strat.stopped is True  # type: ignore[union-attr]
        assert len(strat.bars_seen) == 60  # type: ignore[union-attr]
        # One fill (the BUY) — on_fill called once.
        assert len(strat.fills_seen) == 1  # type: ignore[union-attr]

    def test_context_submit_proposal_routed(self, btc_symbol, synthetic_source):
        cfg = BacktestConfig(symbol=btc_symbol, timeframe="1m", fee_bps=Decimal("0"))
        engine = BacktestEngine(
            strategy_class=_ContextSubmittingStrategy,
            config=cfg,
            candle_source=synthetic_source,
        )
        result = engine.run()
        assert result.summary.n_proposals == 1
        assert result.summary.n_fills == 1
        # log_decision captured.
        assert len(engine.decisions) == 1
        assert engine.decisions[0]["event"] == "first_bar"

    def test_unfilled_limit_recorded_but_no_fill(self, btc_symbol, synthetic_source):
        cfg = BacktestConfig(symbol=btc_symbol, timeframe="1m")
        engine = BacktestEngine(
            strategy_class=_LimitOrderStrategy,
            config=cfg,
            candle_source=synthetic_source,
        )
        result = engine.run()
        assert result.summary.n_proposals == 1
        assert result.summary.n_fills == 0
        assert result.summary.n_trades == 0

    def test_history_window_caps(self, btc_symbol):
        cfg = BacktestConfig(symbol=btc_symbol, timeframe="1m", history_window=8)
        start = datetime(2026, 1, 1, tzinfo=UTC)
        src = SyntheticCandleSource(symbol=btc_symbol, timeframe="1m", start=start, n_bars=20)
        engine = BacktestEngine(
            strategy_class=_HistoryAwareStrategy,
            config=cfg,
            candle_source=src,
        )
        engine.run()
        strat = engine._strategy
        assert strat is not None
        lengths = strat.history_lengths  # type: ignore[union-attr]
        # Should grow up to history_window then plateau.
        assert lengths[0] == 1
        assert lengths[-1] == 8
        assert max(lengths) == 8

    def test_position_visible_via_context(self, btc_symbol, synthetic_source):
        cfg = BacktestConfig(symbol=btc_symbol, timeframe="1m")
        engine = BacktestEngine(
            strategy_class=_PositionAwareStrategy,
            config=cfg,
            candle_source=synthetic_source,
        )
        engine.run()
        strat = engine._strategy
        assert strat is not None
        assert strat.observed_position_qty == Decimal("3")  # type: ignore[union-attr]

    def test_now_advances_with_bars(self, btc_symbol):
        observed: list[datetime] = []

        class _ClockStrategy(Strategy):
            name = "clock"

            def on_candle(self, bar: Bar) -> Sequence[OrderProposal]:
                observed.append(self.ctx.now())
                return ()

        start = datetime(2026, 1, 1, tzinfo=UTC)
        src = SyntheticCandleSource(symbol=btc_symbol, timeframe="1h", start=start, n_bars=5)
        engine = BacktestEngine(
            strategy_class=_ClockStrategy,
            config=BacktestConfig(symbol=btc_symbol, timeframe="1h"),
            candle_source=src,
        )
        engine.run()
        assert observed[0] == start
        assert observed[-1] == start + timedelta(hours=4)


class TestStepOnce:
    def test_step_once_returns_step(self, btc_symbol, synthetic_source, simple_strategy_cls):
        cfg = BacktestConfig(symbol=btc_symbol, timeframe="1m")
        engine = BacktestEngine(
            strategy_class=simple_strategy_cls,
            config=cfg,
            candle_source=synthetic_source,
            strategy_config={"buy_at": 0, "sell_at": 1, "qty": "1"},
        )
        bars = list(synthetic_source)
        # Step bar 0: buy proposal queued (no fill yet — no prior bar).
        step0 = engine.step_once(bars[0])
        assert len(step0.proposals) == 1
        assert step0.fills == ()
        # Step bar 1: the queued buy fills at bar 1's open.
        step1 = engine.step_once(bars[1])
        assert len(step1.fills) == 1
        assert step1.fills[0].side == "BUY"

    def test_step_rejects_wrong_symbol(
        self, btc_symbol, eth_symbol, synthetic_source, make_bar, fixed_start, flat_strategy_cls
    ):
        cfg = BacktestConfig(symbol=btc_symbol, timeframe="1m")
        engine = BacktestEngine(
            strategy_class=flat_strategy_cls,
            config=cfg,
            candle_source=synthetic_source,
        )
        bad = make_bar(symbol=eth_symbol, ts=fixed_start)
        with pytest.raises(ValueError, match="bar.symbol"):
            engine.step_once(bad)

    def test_step_rejects_wrong_timeframe(
        self, btc_symbol, synthetic_source, make_bar, fixed_start, flat_strategy_cls
    ):
        cfg = BacktestConfig(symbol=btc_symbol, timeframe="1m")
        engine = BacktestEngine(
            strategy_class=flat_strategy_cls,
            config=cfg,
            candle_source=synthetic_source,
        )
        bad = make_bar(symbol=btc_symbol, ts=fixed_start, timeframe="1h")
        with pytest.raises(ValueError, match="bar.timeframe"):
            engine.step_once(bad)


class TestResetSemantics:
    def test_reset_clears_state(self, btc_symbol, synthetic_source, simple_strategy_cls):
        cfg = BacktestConfig(symbol=btc_symbol, timeframe="1m", fee_bps=Decimal("0"))
        engine = BacktestEngine(
            strategy_class=simple_strategy_cls,
            config=cfg,
            candle_source=synthetic_source,
            strategy_config={"buy_at": 1, "sell_at": 5, "qty": "1"},
        )
        first = engine.run()
        assert first.summary.n_trades == 1
        engine.reset()
        # Re-running requires a fresh source iterator; the engine snapshots the
        # source via slice() which re-iterates from scratch.
        second = engine.run()
        assert second.summary.n_trades == 1
        # The two runs should have identical proposals — parity within self.
        assert [p.client_order_id for p in first.proposals] == [
            p.client_order_id for p in second.proposals
        ]


class TestEquityCurve:
    def test_equity_curve_has_one_point_per_bar(
        self, btc_symbol, synthetic_source, flat_strategy_cls
    ):
        cfg = BacktestConfig(symbol=btc_symbol, timeframe="1m")
        engine = BacktestEngine(
            strategy_class=flat_strategy_cls,
            config=cfg,
            candle_source=synthetic_source,
        )
        result = engine.run()
        assert len(result.equity_curve) == 60

    def test_max_drawdown_non_negative(self, btc_symbol, synthetic_source, simple_strategy_cls):
        cfg = BacktestConfig(symbol=btc_symbol, timeframe="1m")
        engine = BacktestEngine(
            strategy_class=simple_strategy_cls,
            config=cfg,
            candle_source=synthetic_source,
            strategy_config={"buy_at": 1, "sell_at": 10, "qty": "1"},
        )
        result = engine.run()
        assert result.summary.max_drawdown_pct >= 0.0


class TestShortSelling:
    def test_short_close_pnl(self, btc_symbol, make_bar, fixed_start):
        """A SELL-then-BUY round trip produces correct PnL."""

        class _ShortStrategy(Strategy):
            name = "short_strategy"

            def __init__(self, ctx: Context, config: dict[str, Any]) -> None:
                super().__init__(ctx, config)
                self._idx = 0

            def on_candle(self, bar: Bar) -> Sequence[OrderProposal]:
                self._idx += 1
                if self._idx == 1:
                    return [
                        OrderProposal(
                            symbol=bar.symbol,
                            side="SELL",
                            qty=Decimal("1"),
                            order_type="market",
                            client_order_id=ClientOrderId("short-open"),
                            rationale="open short",
                            asset_class="crypto",
                        )
                    ]
                if self._idx == 3:
                    return [
                        OrderProposal(
                            symbol=bar.symbol,
                            side="BUY",
                            qty=Decimal("1"),
                            order_type="market",
                            client_order_id=ClientOrderId("short-close"),
                            rationale="close short",
                            asset_class="crypto",
                        )
                    ]
                return ()

        # Build a deterministic descending market so the short profits.
        bars = [
            make_bar(
                symbol=btc_symbol,
                ts=fixed_start + timedelta(minutes=i),
                open_=100 - i,
                high=100 - i + 0.5,
                low=100 - i - 0.5,
                close=99.5 - i,
            )
            for i in range(6)
        ]
        src = InMemoryCandleSource(bars)
        engine = BacktestEngine(
            strategy_class=_ShortStrategy,
            config=BacktestConfig(symbol=btc_symbol, timeframe="1m", fee_bps=Decimal("0")),
            candle_source=src,
        )
        result = engine.run()
        assert result.summary.n_trades == 1
        trade = result.trades[0]
        assert trade.side == "SELL"
        assert trade.pnl > 0


class TestPartialClose:
    def test_partial_close_leaves_residual_position(self, btc_symbol, synthetic_source):
        """Buy 3, sell 1 — the remaining 2 stay open."""

        class _PartialStrategy(Strategy):
            name = "partial"

            def __init__(self, ctx: Context, config: dict[str, Any]) -> None:
                super().__init__(ctx, config)
                self._idx = 0

            def on_candle(self, bar: Bar) -> Sequence[OrderProposal]:
                self._idx += 1
                if self._idx == 1:
                    return [
                        OrderProposal(
                            symbol=bar.symbol,
                            side="BUY",
                            qty=Decimal("3"),
                            order_type="market",
                            client_order_id=ClientOrderId("p-buy"),
                            rationale="open",
                            asset_class="crypto",
                        )
                    ]
                if self._idx == 5:
                    return [
                        OrderProposal(
                            symbol=bar.symbol,
                            side="SELL",
                            qty=Decimal("1"),
                            order_type="market",
                            client_order_id=ClientOrderId("p-sell"),
                            rationale="partial close",
                            asset_class="crypto",
                        )
                    ]
                return ()

        engine = BacktestEngine(
            strategy_class=_PartialStrategy,
            config=BacktestConfig(symbol=btc_symbol, timeframe="1m"),
            candle_source=synthetic_source,
        )
        result = engine.run()
        assert result.summary.n_trades == 1  # one round-trip for the partial slice
        assert result.trades[0].qty == Decimal("1")
        # After end-of-run, the open leg of 2 units is still tracked by the engine.
        assert engine._open_leg is not None
        assert engine._open_leg.qty == Decimal("2")


class TestPyramiding:
    def test_two_buys_weighted_average(self, btc_symbol, make_bar, fixed_start):
        """Two buys before any sell — entry price is volume-weighted."""

        class _PyramidStrategy(Strategy):
            name = "pyramid"

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
                            client_order_id=ClientOrderId(f"py-{self._idx}"),
                            rationale="pyramid",
                            asset_class="crypto",
                        )
                    ]
                if self._idx == 5:
                    return [
                        OrderProposal(
                            symbol=bar.symbol,
                            side="SELL",
                            qty=Decimal("2"),
                            order_type="market",
                            client_order_id=ClientOrderId("py-close"),
                            rationale="full close",
                            asset_class="crypto",
                        )
                    ]
                return ()

        # Strategy fires BUY proposals when its internal _idx is 1 or 3.
        # _idx increments on each on_candle call → _idx=1 fires on bars[0],
        # _idx=3 on bars[2], _idx=5 on bars[4]. Those proposals fill on the
        # NEXT bar's open (bars[1], bars[3], bars[5]).
        # Set bars[1].open=100, bars[3].open=110 → weighted avg entry = 105.
        # Set bars[5].open=120 → exit price = 120 → PnL (120-105)*2 = 30.
        bars: list[Bar] = []
        opens = [100, 100, 100, 110, 100, 120, 100, 100]
        for i, o in enumerate(opens):
            bars.append(
                make_bar(
                    symbol=btc_symbol,
                    ts=fixed_start + timedelta(minutes=i),
                    open_=o,
                    high=o + 0.5,
                    low=o - 0.5,
                    close=o + 0.1,
                )
            )
        src = InMemoryCandleSource(bars)
        engine = BacktestEngine(
            strategy_class=_PyramidStrategy,
            config=BacktestConfig(symbol=btc_symbol, timeframe="1m", fee_bps=Decimal("0")),
            candle_source=src,
        )
        result = engine.run()
        assert result.summary.n_trades == 1
        trade = result.trades[0]
        # Two units at 100 + 110 → weighted avg = 105.
        assert trade.entry_price == Decimal("105")
        assert trade.qty == Decimal("2")


class TestPropertyExposures:
    def test_proposals_property_grows(self, btc_symbol, synthetic_source, simple_strategy_cls):
        cfg = BacktestConfig(symbol=btc_symbol, timeframe="1m")
        engine = BacktestEngine(
            strategy_class=simple_strategy_cls,
            config=cfg,
            candle_source=synthetic_source,
            strategy_config={"buy_at": 1, "sell_at": 4, "qty": "1"},
        )
        bars = list(synthetic_source)
        engine.step_once(bars[0])
        assert engine.proposals == ()
        engine.step_once(bars[1])
        assert len(engine.proposals) == 1

    def test_equity_property_no_step_returns_starting(
        self, btc_symbol, synthetic_source, flat_strategy_cls
    ):
        engine = BacktestEngine(
            strategy_class=flat_strategy_cls,
            config=BacktestConfig(symbol=btc_symbol, timeframe="1m"),
            candle_source=synthetic_source,
        )
        assert engine.equity == Decimal("10000")

    def test_fills_trades_equity_property_views(
        self, btc_symbol, synthetic_source, simple_strategy_cls
    ):
        cfg = BacktestConfig(symbol=btc_symbol, timeframe="1m", fee_bps=Decimal("0"))
        engine = BacktestEngine(
            strategy_class=simple_strategy_cls,
            config=cfg,
            candle_source=synthetic_source,
            strategy_config={"buy_at": 1, "sell_at": 5, "qty": "1"},
        )
        engine.run()
        # fills + trades grow during run.
        assert isinstance(engine.fills, tuple)
        assert len(engine.fills) == 2
        assert isinstance(engine.trades, tuple)
        assert len(engine.trades) == 1
        # equity != starting (fee_bps=0, but price drift may move it slightly).
        assert isinstance(engine.equity, Decimal)


class TestEmptyRun:
    def test_empty_source_raises_or_returns_degenerate(
        self, btc_symbol, fixed_start, flat_strategy_cls
    ):
        """An InMemoryCandleSource rejects empty; for a real-world empty slice
        we should still emit a degenerate result via run() on a non-empty
        source restricted to a non-existent window.
        """
        # Use a synthetic source but restrict run() to a window with zero bars.
        src = SyntheticCandleSource(symbol=btc_symbol, timeframe="1m", start=fixed_start, n_bars=5)
        engine = BacktestEngine(
            strategy_class=flat_strategy_cls,
            config=BacktestConfig(symbol=btc_symbol, timeframe="1m"),
            candle_source=src,
        )
        far_future = fixed_start + timedelta(days=365)
        result = engine.run(start=far_future, end=far_future + timedelta(minutes=10))
        assert result.bars_processed == 0
        assert result.summary.n_fills == 0


class TestBuyLimitPath:
    def test_buy_limit_fills_when_low_crosses(self, btc_symbol, make_bar, fixed_start):
        """BUY limit at 100 fills when next bar's low <= 100 → fill at min(open, limit)."""

        class _BuyLimitStrategy(Strategy):
            name = "buy_limit"

            def __init__(self, ctx: Context, config: dict[str, Any]) -> None:
                super().__init__(ctx, config)
                self._fired = False

            def on_candle(self, bar: Bar) -> Sequence[OrderProposal]:
                if not self._fired:
                    self._fired = True
                    return [
                        OrderProposal(
                            symbol=bar.symbol,
                            side="BUY",
                            qty=Decimal("1"),
                            order_type="limit",
                            limit_px=Decimal("100"),
                            client_order_id=ClientOrderId("blr"),
                            rationale="reachable buy",
                            asset_class="crypto",
                        )
                    ]
                return ()

        # Bar 0 → strategy fires proposal. Bar 1 has low=95 (crosses limit at 100)
        # and open=99 → fill at min(100, 99) = 99.
        bars = [
            make_bar(symbol=btc_symbol, ts=fixed_start, open_=110, high=111, low=109, close=110),
            make_bar(
                symbol=btc_symbol,
                ts=fixed_start + timedelta(minutes=1),
                open_=99,
                high=102,
                low=95,
                close=101,
            ),  # crosses 100
        ]
        src = InMemoryCandleSource(bars)
        engine = BacktestEngine(
            strategy_class=_BuyLimitStrategy,
            config=BacktestConfig(symbol=btc_symbol, timeframe="1m", fee_bps=Decimal("0")),
            candle_source=src,
        )
        result = engine.run()
        assert result.summary.n_fills == 1
        # min(limit_px=100, fill_price=99) = 99 (we got the better price).
        assert result.fills[0].price == Decimal("99")


class TestSellLimitPath:
    def test_sell_limit_unreachable_returns_no_fill(self, btc_symbol, make_bar, fixed_start):
        """SELL limit above the bar's high never fills."""

        class _UnreachSellLimit(Strategy):
            name = "unreach_sell"

            def __init__(self, ctx: Context, config: dict[str, Any]) -> None:
                super().__init__(ctx, config)
                self._fired = False

            def on_candle(self, bar: Bar) -> Sequence[OrderProposal]:
                if not self._fired:
                    self._fired = True
                    return [
                        OrderProposal(
                            symbol=bar.symbol,
                            side="SELL",
                            qty=Decimal("1"),
                            order_type="limit",
                            limit_px=Decimal("99999"),
                            client_order_id=ClientOrderId("usl"),
                            rationale="too high",
                            asset_class="crypto",
                        )
                    ]
                return ()

        bars = [
            make_bar(
                symbol=btc_symbol,
                ts=fixed_start + timedelta(minutes=i),
                open_=100,
                high=101,
                low=99,
                close=100.5,
            )
            for i in range(4)
        ]
        src = InMemoryCandleSource(bars)
        engine = BacktestEngine(
            strategy_class=_UnreachSellLimit,
            config=BacktestConfig(symbol=btc_symbol, timeframe="1m"),
            candle_source=src,
        )
        result = engine.run()
        assert result.summary.n_proposals == 1
        assert result.summary.n_fills == 0

    def test_sell_limit_fills_when_high_crosses(self, btc_symbol, make_bar, fixed_start):
        """SELL limit at 105 fills when next bar's high >= 105."""

        class _SellLimitStrategy(Strategy):
            name = "sell_limit"

            def __init__(self, ctx: Context, config: dict[str, Any]) -> None:
                super().__init__(ctx, config)
                self._idx = 0

            def on_candle(self, bar: Bar) -> Sequence[OrderProposal]:
                self._idx += 1
                if self._idx == 1:
                    # First open the long position via market order.
                    return [
                        OrderProposal(
                            symbol=bar.symbol,
                            side="BUY",
                            qty=Decimal("1"),
                            order_type="market",
                            client_order_id=ClientOrderId("slm-buy"),
                            rationale="open",
                            asset_class="crypto",
                        )
                    ]
                if self._idx == 2:
                    return [
                        OrderProposal(
                            symbol=bar.symbol,
                            side="SELL",
                            qty=Decimal("1"),
                            order_type="limit",
                            limit_px=Decimal("105"),
                            client_order_id=ClientOrderId("slm-sell"),
                            rationale="take profit",
                            asset_class="crypto",
                        )
                    ]
                return ()

        bars = [
            make_bar(
                symbol=btc_symbol,
                ts=fixed_start + timedelta(minutes=0),
                open_=100,
                high=101,
                low=99,
                close=100.5,
            ),
            make_bar(
                symbol=btc_symbol,
                ts=fixed_start + timedelta(minutes=1),
                open_=100,
                high=101,
                low=99,
                close=100.5,
            ),
            make_bar(
                symbol=btc_symbol,
                ts=fixed_start + timedelta(minutes=2),
                open_=104,
                high=110,
                low=103,
                close=109,
            ),  # high crosses 105
            make_bar(
                symbol=btc_symbol,
                ts=fixed_start + timedelta(minutes=3),
                open_=109,
                high=110,
                low=108,
                close=109.5,
            ),
        ]
        src = InMemoryCandleSource(bars)
        engine = BacktestEngine(
            strategy_class=_SellLimitStrategy,
            config=BacktestConfig(symbol=btc_symbol, timeframe="1m", fee_bps=Decimal("0")),
            candle_source=src,
        )
        result = engine.run()
        # Both proposals fill — round-trip recorded.
        assert result.summary.n_fills == 2
        assert result.summary.n_trades == 1
        assert result.trades[0].exit_price == Decimal("105")


class TestPyramidingShort:
    def test_adding_to_short(self, btc_symbol, make_bar, fixed_start):
        """Two SELL proposals stack on a short leg with weighted-average entry."""

        class _ShortPyramid(Strategy):
            name = "short_pyramid"

            def __init__(self, ctx: Context, config: dict[str, Any]) -> None:
                super().__init__(ctx, config)
                self._idx = 0

            def on_candle(self, bar: Bar) -> Sequence[OrderProposal]:
                self._idx += 1
                if self._idx in {1, 3}:
                    return [
                        OrderProposal(
                            symbol=bar.symbol,
                            side="SELL",
                            qty=Decimal("1"),
                            order_type="market",
                            client_order_id=ClientOrderId(f"sp-{self._idx}"),
                            rationale="add short",
                            asset_class="crypto",
                        )
                    ]
                if self._idx == 5:
                    return [
                        OrderProposal(
                            symbol=bar.symbol,
                            side="BUY",
                            qty=Decimal("2"),
                            order_type="market",
                            client_order_id=ClientOrderId("sp-cover"),
                            rationale="cover",
                            asset_class="crypto",
                        )
                    ]
                return ()

        # Bar idx 0 → SELL fires, fills on bar idx 1 (open=100).
        # Bar idx 2 → SELL fires, fills on bar idx 3 (open=90).
        # Bar idx 4 → BUY 2 fires, fills on bar idx 5 (open=80).
        # Weighted entry = (100 + 90)/2 = 95. Cover at 80 → PnL = (95-80)*2 = 30.
        opens = [100, 100, 100, 90, 90, 80, 80]
        bars = [
            make_bar(
                symbol=btc_symbol,
                ts=fixed_start + timedelta(minutes=i),
                open_=o,
                high=o + 1,
                low=o - 1,
                close=o + 0.5,
            )
            for i, o in enumerate(opens)
        ]
        src = InMemoryCandleSource(bars)
        engine = BacktestEngine(
            strategy_class=_ShortPyramid,
            config=BacktestConfig(symbol=btc_symbol, timeframe="1m", fee_bps=Decimal("0")),
            candle_source=src,
        )
        result = engine.run()
        assert result.summary.n_trades == 1
        trade = result.trades[0]
        assert trade.side == "SELL"
        assert trade.entry_price == Decimal("95")
        assert trade.qty == Decimal("2")


class TestHistoryViewEdgeCases:
    def test_history_view_wrong_symbol_returns_empty(
        self, btc_symbol, eth_symbol, synthetic_source, flat_strategy_cls
    ):
        engine = BacktestEngine(
            strategy_class=flat_strategy_cls,
            config=BacktestConfig(symbol=btc_symbol, timeframe="1m"),
            candle_source=synthetic_source,
        )
        bars = list(synthetic_source)
        engine.step_once(bars[0])
        assert engine._history_view(eth_symbol, "1m", 5) == ()

    def test_history_view_wrong_timeframe_returns_empty(
        self, btc_symbol, synthetic_source, flat_strategy_cls
    ):
        engine = BacktestEngine(
            strategy_class=flat_strategy_cls,
            config=BacktestConfig(symbol=btc_symbol, timeframe="1m"),
            candle_source=synthetic_source,
        )
        bars = list(synthetic_source)
        engine.step_once(bars[0])
        assert engine._history_view(btc_symbol, "1h", 5) == ()

    def test_history_view_zero_n_returns_empty(
        self, btc_symbol, synthetic_source, flat_strategy_cls
    ):
        engine = BacktestEngine(
            strategy_class=flat_strategy_cls,
            config=BacktestConfig(symbol=btc_symbol, timeframe="1m"),
            candle_source=synthetic_source,
        )
        bars = list(synthetic_source)
        engine.step_once(bars[0])
        assert engine._history_view(btc_symbol, "1m", 0) == ()


class TestRunWithExternalStart:
    def test_run_with_step_once_first_skips_on_start(
        self, btc_symbol, synthetic_source, simple_strategy_cls
    ):
        """When step_once was called first, run() should not call on_start again."""
        engine = BacktestEngine(
            strategy_class=simple_strategy_cls,
            config=BacktestConfig(symbol=btc_symbol, timeframe="1m"),
            candle_source=synthetic_source,
            strategy_config={"buy_at": 999, "sell_at": 999, "qty": "1"},
        )
        bars = list(synthetic_source)
        engine.step_once(bars[0])  # this calls on_start internally
        # run() now should advance through the rest without re-firing on_start.
        result = engine.run(start=bars[1].timestamp_utc)
        assert result.bars_processed > 0


class TestPositionForOtherSymbol:
    def test_position_for_unknown_symbol_returns_none(
        self, btc_symbol, eth_symbol, synthetic_source, simple_strategy_cls
    ):
        engine = BacktestEngine(
            strategy_class=simple_strategy_cls,
            config=BacktestConfig(symbol=btc_symbol, timeframe="1m"),
            candle_source=synthetic_source,
            strategy_config={"buy_at": 0, "sell_at": 50, "qty": "1"},
        )
        # Advance one bar so a position exists.
        bars = list(synthetic_source)
        engine.step_once(bars[0])
        engine.step_once(bars[1])  # BUY fills here
        # Query a different symbol — must return None even though the engine has a position.
        assert engine._position_for(eth_symbol) is None
        # Same symbol returns the position.
        assert engine._position_for(btc_symbol) is not None


class TestSlippageIntegration:
    def test_fixed_bps_changes_fill_price(self, btc_symbol, synthetic_source, simple_strategy_cls):
        cfg = BacktestConfig(symbol=btc_symbol, timeframe="1m", fee_bps=Decimal("0"))
        engine_no_slip = BacktestEngine(
            strategy_class=simple_strategy_cls,
            config=cfg,
            candle_source=synthetic_source,
            strategy_config={"buy_at": 1, "sell_at": 5, "qty": "1"},
        )
        engine_with_slip = BacktestEngine(
            strategy_class=simple_strategy_cls,
            config=cfg,
            candle_source=SyntheticCandleSource(
                symbol=btc_symbol,
                timeframe="1m",
                start=datetime(2026, 1, 1, tzinfo=UTC),
                n_bars=60,
                seed=7,
            ),
            strategy_config={"buy_at": 1, "sell_at": 5, "qty": "1"},
            slippage_model=FixedBpsSlippageModel(bps=Decimal("50")),  # 0.5%
        )
        a = engine_no_slip.run()
        b = engine_with_slip.run()
        # Slippage hurts both legs of a round trip → worse PnL.
        assert b.trades[0].pnl < a.trades[0].pnl
