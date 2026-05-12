"""Backtest engine — the parity oracle for V4.

A :class:`BacktestEngine` replays historical candles through the exact same
:class:`quanta_core.strategy.base.Strategy` class the live engine uses, with
two narrow swaps:

* the venue is a synthetic ``next-bar-open`` paper-fill simulator;
* the clock is the bar clock (no wall-clock drift).

The parity invariant — backtest must produce identical ``OrderProposal``
lists as live for the same candle inputs — is enforced by
``tests/backtest/test_live_backtest_parity.py``. Drift in this invariant is
the single hardest signal that the design has regressed.

The engine is intentionally synchronous. The foundation branch locked the
Strategy ABC as sync; making the engine async would either (a) require every
strategy to be async too, or (b) require an executor pool to bridge — both
choices are worse than letting both engines stay in one thread.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from quanta_core.backtest.candle_source import CandleSource, timeframe_to_timedelta
from quanta_core.backtest.result import (
    BacktestResult,
    EquityPoint,
    SimFill,
    SummaryMetrics,
    TradeRecord,
)
from quanta_core.types import (
    Bar,
    ClientOrderId,
    OrderProposal,
    Position,
    Side,
    Symbol,
    Timeframe,
)

if TYPE_CHECKING:  # pragma: no cover — typing only
    from collections.abc import Iterator, Sequence

    from quanta_core.strategy.base import Strategy


_ZERO = Decimal("0")
_ONE = Decimal("1")


# ---------------------------------------------------------------------------
# Slippage models
# ---------------------------------------------------------------------------


@runtime_checkable
class SlippageModel(Protocol):
    """Pluggable fill price adjustment for the next-bar-open simulator."""

    def fill_price(self, proposal: OrderProposal, next_bar: Bar) -> Decimal:
        """Return the price at which ``proposal`` fills on ``next_bar``."""
        ...


class NoSlippageModel:
    """Fills at the next bar's open. Useful for the parity invariant."""

    def fill_price(self, proposal: OrderProposal, next_bar: Bar) -> Decimal:
        """Return ``next_bar.open`` unchanged."""
        return next_bar.open


class FixedBpsSlippageModel:
    """Adds a configurable signed slippage in basis points to the open."""

    def __init__(self, bps: Decimal = Decimal("5")) -> None:
        """Configure with positive ``bps`` (5 = 0.05% adverse slippage)."""
        if bps < 0:
            msg = f"bps must be non-negative, got {bps}"
            raise ValueError(msg)
        self.bps = Decimal(bps)

    def fill_price(self, proposal: OrderProposal, next_bar: Bar) -> Decimal:
        """Push the open against the trader by ``bps`` basis points."""
        adj = next_bar.open * (self.bps / Decimal("10000"))
        if proposal.side == "BUY":
            return next_bar.open + adj
        return next_bar.open - adj


# ---------------------------------------------------------------------------
# Engine configuration
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BacktestConfig:
    """Lightweight DTO for engine configuration.

    Why a dataclass rather than pydantic: this struct is constructed many
    times per walk-forward run; the validation cost of pydantic isn't worth
    the gain when every field has an obvious type.
    """

    symbol: Symbol
    timeframe: Timeframe
    starting_equity: Decimal = Decimal("10000")
    fee_bps: Decimal = Decimal("10")  # 0.10% maker/taker per side
    seed: int = 0
    history_window: int = 256  # how many closed bars Context.get_history exposes

    def __post_init__(self) -> None:
        """Reject non-positive equity + negative fees."""
        if self.starting_equity <= 0:
            msg = f"starting_equity must be positive, got {self.starting_equity}"
            raise ValueError(msg)
        if self.fee_bps < 0:
            msg = f"fee_bps must be non-negative, got {self.fee_bps}"
            raise ValueError(msg)
        if self.history_window < 0:
            msg = f"history_window must be non-negative, got {self.history_window}"
            raise ValueError(msg)


# ---------------------------------------------------------------------------
# Open position state — internal bookkeeping for round-trip pairing.
# ---------------------------------------------------------------------------


@dataclass
class _OpenLeg:
    """Tracks the open side of a round-trip until the exit fill arrives."""

    side: Side
    qty: Decimal
    entry_price: Decimal
    entry_ts: datetime
    fee_in: Decimal
    bars_held: int = 0


# ---------------------------------------------------------------------------
# Context implementation handed to the strategy each bar.
# ---------------------------------------------------------------------------


class _BacktestContext:
    """In-process Context implementation backed by the engine's state.

    The class is intentionally not part of the public API — strategies see
    it through the :class:`Context` protocol only. Tests can build one
    directly if they need to assert protocol conformance.
    """

    __slots__ = ("_engine",)

    def __init__(self, engine: BacktestEngine) -> None:
        """Bind to the owning engine; engine state is the source of truth."""
        self._engine = engine

    def now(self) -> datetime:
        """Return the bar clock — the close timestamp of the last closed bar."""
        return self._engine._clock

    def get_position(self, symbol: Symbol) -> Position | None:
        """Return the engine's current position for ``symbol``, if any."""
        return self._engine._position_for(symbol)

    def get_history(
        self,
        symbol: Symbol,
        timeframe: Timeframe,
        n: int,
    ) -> Sequence[Bar]:
        """Return the last ``n`` closed bars for ``(symbol, timeframe)``."""
        return self._engine._history_view(symbol, timeframe, n)

    def submit_proposal(self, proposal: OrderProposal) -> None:
        """Capture a proposal submitted via the Context surface."""
        self._engine._context_proposals.append(proposal)

    def log_decision(self, decision: dict[str, Any]) -> None:
        """Capture a freeform decision payload."""
        self._engine._decisions.append(decision)


# ---------------------------------------------------------------------------
# BacktestEngine
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BacktestStep:
    """The artefact one bar of :meth:`BacktestEngine.step_once` produces."""

    bar: Bar
    proposals: tuple[OrderProposal, ...]
    fills: tuple[SimFill, ...]
    equity: EquityPoint


class BacktestEngine:
    """Replay historical candles through a Strategy class.

    Parameters
    ----------
    strategy_class
        Subclass of :class:`quanta_core.strategy.base.Strategy`. Constructed
        once via ``strategy_class(ctx, strategy_config)``; ``ctx`` is a
        :class:`_BacktestContext` bound to this engine.
    config
        Backtest run config (symbol, timeframe, starting equity, fees, seed).
    candle_source
        Source of OHLCV bars; must yield in chronological order.
    strategy_config
        Optional config dict passed to the strategy constructor.
    slippage_model
        Optional slippage model; defaults to :class:`NoSlippageModel` so the
        parity invariant holds out of the box. Tests that exercise the
        slippage path pass :class:`FixedBpsSlippageModel`.

    Notes
    -----
    Proposals are routed through the **synthetic next-bar-open** fill
    simulator. A proposal emitted on bar ``t`` fills on bar ``t+1``'s open
    (possibly adjusted by the slippage model). If the candle source ends
    before the next bar arrives, the proposal is dropped and recorded as
    unfilled (visible in :attr:`BacktestResult.proposals` but absent from
    :attr:`BacktestResult.fills`).
    """

    def __init__(
        self,
        *,
        strategy_class: type[Strategy],
        config: BacktestConfig,
        candle_source: CandleSource,
        strategy_config: dict[str, Any] | None = None,
        slippage_model: SlippageModel | None = None,
    ) -> None:
        """Wire the engine to a strategy class and candle source."""
        if candle_source.symbol != config.symbol:
            msg = f"candle_source.symbol={candle_source.symbol} != config.symbol={config.symbol}"
            raise ValueError(msg)
        if candle_source.timeframe != config.timeframe:
            msg = (
                f"candle_source.timeframe={candle_source.timeframe} != "
                f"config.timeframe={config.timeframe}"
            )
            raise ValueError(msg)
        self._strategy_class = strategy_class
        self._config = config
        self._candle_source = candle_source
        self._strategy_config: dict[str, Any] = dict(strategy_config or {})
        self._slippage_model: SlippageModel = slippage_model or NoSlippageModel()
        self._tf_delta: timedelta = timeframe_to_timedelta(config.timeframe)

        # Mutable engine state (only valid mid-run).
        self._clock: datetime = datetime(1970, 1, 1, tzinfo=UTC)
        self._history: list[Bar] = []
        self._cash: Decimal = config.starting_equity
        self._holdings_qty: Decimal = _ZERO
        self._holdings_side: Side | None = None
        self._holdings_avg: Decimal = _ZERO
        self._holdings_opened_at: datetime | None = None
        self._open_leg: _OpenLeg | None = None

        # Pending proposal: at most one, fired at next bar open.
        self._pending_proposals: list[OrderProposal] = []
        # Outputs across the run.
        self._all_proposals: list[OrderProposal] = []
        self._all_fills: list[SimFill] = []
        self._all_trades: list[TradeRecord] = []
        self._equity_curve: list[EquityPoint] = []
        self._decisions: list[dict[str, Any]] = []
        # Buffer for proposals submitted via Context.submit_proposal.
        self._context_proposals: list[OrderProposal] = []
        # Order counter for deterministic client_order_id assignment.
        self._co_id_counter: int = 0

        # Constructed lazily by _ensure_strategy().
        self._strategy: Strategy | None = None
        self._started: bool = False
        self._stopped: bool = False

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def run(
        self,
        start: datetime | None = None,
        end: datetime | None = None,
    ) -> BacktestResult:
        """Replay the candle source end-to-end and return a result.

        Parameters
        ----------
        start
            Inclusive lower bound on bar timestamps; defaults to the source's
            first bar.
        end
            Exclusive upper bound; defaults to the source's last bar + 1 tf.
        """
        self._ensure_strategy()
        if not self._started:
            self._strategy.on_start()  # type: ignore[union-attr]
            self._started = True

        bars_iter = self._select_iter(start, end)
        first_ts: datetime | None = None
        last_ts: datetime | None = None
        n_bars = 0

        for bar in bars_iter:
            self.step_once(bar)
            n_bars += 1
            if first_ts is None:
                first_ts = bar.timestamp_utc
            last_ts = bar.timestamp_utc

        # Drain: at end of stream, fold any remaining open leg by marking it
        # to the last bar's close so the equity curve and summary reflect
        # latent exposure.
        if last_ts is None:
            # No bars at all; produce a degenerate result rooted on the
            # configured start (or epoch fallback).
            first_ts = start if start is not None else self._clock
            last_ts = first_ts

        if not self._stopped:
            self._strategy.on_stop()  # type: ignore[union-attr]
            self._stopped = True

        # Defensive: by this point both timestamps are set (see fallback
        # branch above) but mypy needs the assertion to narrow.
        assert first_ts is not None
        assert last_ts is not None
        end_clock = last_ts + self._tf_delta
        summary = self._summarise()
        return BacktestResult(
            strategy_name=self._strategy_class.name,
            symbol=self._config.symbol,
            start=first_ts,
            end=end_clock,
            bars_processed=n_bars,
            proposals=tuple(self._all_proposals),
            fills=tuple(self._all_fills),
            trades=tuple(self._all_trades),
            equity_curve=tuple(self._equity_curve),
            summary=summary,
        )

    def step_once(self, bar: Bar) -> BacktestStep:
        """Replay exactly one bar; useful for unit testing and walk-forward.

        The order of operations matches the live engine's per-bar pipeline:

        1. Fill any pending proposal at this bar's open (via the slippage
           model).
        2. Advance the clock to this bar's close.
        3. Append the bar to the closed-bar history.
        4. Mark-to-market the open holdings.
        5. Invoke ``strategy.on_candle(bar)``.
        6. Buffer proposals (both returned and Context-submitted) for the
           next bar.
        7. Record the equity snapshot.
        """
        if bar.symbol != self._config.symbol:
            msg = f"bar.symbol={bar.symbol} != config.symbol={self._config.symbol}"
            raise ValueError(msg)
        if bar.timeframe != self._config.timeframe:
            msg = f"bar.timeframe={bar.timeframe} != config.timeframe={self._config.timeframe}"
            raise ValueError(msg)

        self._ensure_strategy()
        if not self._started:
            self._strategy.on_start()  # type: ignore[union-attr]
            self._started = True

        # 1. Fill pending proposals on THIS bar's open.
        step_fills = self._fill_pending(bar)

        # 2. Advance clock to bar close.
        self._clock = bar.timestamp_utc

        # 3. Push bar onto history (cap to history_window so memory is bounded).
        self._history.append(bar)
        if len(self._history) > self._config.history_window:
            # Keep at most history_window bars (FIFO).
            del self._history[0 : len(self._history) - self._config.history_window]

        # 4. Mark-to-market open holdings (against this bar's close).
        if self._open_leg is not None:
            self._open_leg.bars_held += 1

        # 5. Run on_candle on the now-closed bar.
        assert self._strategy is not None
        # Returned proposals + Context.submit_proposal proposals are unioned.
        self._context_proposals.clear()
        returned = tuple(self._strategy.on_candle(bar))
        ctx_emitted = tuple(self._context_proposals)
        proposals_this_bar = returned + ctx_emitted

        # 6. Stash for next bar's fill simulation; track in the run log.
        self._pending_proposals.extend(proposals_this_bar)
        self._all_proposals.extend(proposals_this_bar)

        # 7. Equity snapshot.
        equity_point = self._mark_equity(bar)
        self._equity_curve.append(equity_point)

        return BacktestStep(
            bar=bar,
            proposals=proposals_this_bar,
            fills=tuple(step_fills),
            equity=equity_point,
        )

    # Convenience for the walk-forward driver.
    def reset(self) -> None:
        """Wipe per-run state without rebuilding the engine.

        Used by :class:`quanta_core.backtest.walk_forward.WalkForwardRunner`
        between folds. Keeps the strategy_class/config/candle_source binding;
        replaces every mutable field.
        """
        self._clock = datetime(1970, 1, 1, tzinfo=UTC)
        self._history.clear()
        self._cash = self._config.starting_equity
        self._holdings_qty = _ZERO
        self._holdings_side = None
        self._holdings_avg = _ZERO
        self._holdings_opened_at = None
        self._open_leg = None
        self._pending_proposals.clear()
        self._all_proposals.clear()
        self._all_fills.clear()
        self._all_trades.clear()
        self._equity_curve.clear()
        self._decisions.clear()
        self._context_proposals.clear()
        self._co_id_counter = 0
        self._strategy = None
        self._started = False
        self._stopped = False

    # ------------------------------------------------------------------
    # Read-only views — used by parity tests and the dashboard.
    # ------------------------------------------------------------------

    @property
    def proposals(self) -> Sequence[OrderProposal]:
        """All proposals seen so far in the current run."""
        return tuple(self._all_proposals)

    @property
    def fills(self) -> Sequence[SimFill]:
        """All simulated fills so far in the current run."""
        return tuple(self._all_fills)

    @property
    def trades(self) -> Sequence[TradeRecord]:
        """All closed round-trip trades so far in the current run."""
        return tuple(self._all_trades)

    @property
    def equity(self) -> Decimal:
        """Mark-to-market equity at the last processed bar."""
        if not self._equity_curve:
            return self._config.starting_equity
        return self._equity_curve[-1].equity

    @property
    def decisions(self) -> Sequence[dict[str, Any]]:
        """Captured ``ctx.log_decision`` payloads."""
        return tuple(self._decisions)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _ensure_strategy(self) -> None:
        """Construct the strategy on first use (lazy)."""
        if self._strategy is not None:
            return
        ctx = _BacktestContext(self)
        self._strategy = self._strategy_class(ctx, self._strategy_config)

    def _select_iter(
        self,
        start: datetime | None,
        end: datetime | None,
    ) -> Iterator[Bar]:
        """Return an iterator over the candle source clipped to [start, end)."""
        if start is None and end is None:
            return iter(self._candle_source)
        # Use far-past / far-future sentinels if either side is None.
        lo = start if start is not None else datetime.min.replace(tzinfo=UTC)
        hi = end if end is not None else datetime.max.replace(tzinfo=UTC)
        return self._candle_source.slice(lo, hi)

    def _fill_pending(self, bar: Bar) -> list[SimFill]:
        """Fill every pending proposal at this bar's open.

        Returns the list of fills (possibly empty). Drops the pending queue.
        """
        if not self._pending_proposals:
            return []
        fills: list[SimFill] = []
        for proposal in self._pending_proposals:
            fill = self._simulate_fill(proposal, bar)
            if fill is not None:
                fills.append(fill)
                self._all_fills.append(fill)
                self._apply_fill(fill)
        self._pending_proposals.clear()
        return fills

    def _simulate_fill(
        self,
        proposal: OrderProposal,
        bar: Bar,
    ) -> SimFill | None:
        """Convert one proposal into a simulated fill on ``bar``.

        For limit orders, fill only when the bar's range crossed the limit:
        BUY limits fill at ``min(limit_px, fill_price)``; SELL limits fill
        at ``max(limit_px, fill_price)``. Market orders always fill.
        """
        fill_px = self._slippage_model.fill_price(proposal, bar)
        # Limit-price feasibility check.
        if proposal.order_type in {"limit", "stop_limit"} and proposal.limit_px is not None:
            if proposal.side == "BUY":
                if bar.low > proposal.limit_px:
                    return None  # bar never traded through the limit
                fill_px = min(proposal.limit_px, fill_px)
            else:  # SELL
                if bar.high < proposal.limit_px:
                    return None
                fill_px = max(proposal.limit_px, fill_px)
        fee = (fill_px * proposal.qty * self._config.fee_bps / Decimal("10000")).quantize(
            Decimal("0.00000001"),
            rounding=ROUND_HALF_UP,
        )
        return SimFill(
            symbol=proposal.symbol,
            side=proposal.side,
            qty=proposal.qty,
            price=fill_px,
            fee=fee,
            timestamp_utc=bar.timestamp_utc,
            client_order_id=str(proposal.client_order_id),
        )

    def _apply_fill(self, fill: SimFill) -> None:
        """Update cash, holdings, and the round-trip ledger from a fill."""
        notional = fill.qty * fill.price
        if fill.side == "BUY":
            self._cash -= notional + fill.fee
            if self._open_leg is None:
                # New long leg.
                self._open_leg = _OpenLeg(
                    side="BUY",
                    qty=fill.qty,
                    entry_price=fill.price,
                    entry_ts=fill.timestamp_utc,
                    fee_in=fill.fee,
                )
                self._holdings_side = "BUY"
                self._holdings_qty = fill.qty
                self._holdings_avg = fill.price
                self._holdings_opened_at = fill.timestamp_utc
            elif self._open_leg.side == "BUY":
                # Adding to long — weighted-average the entry.
                total_qty = self._open_leg.qty + fill.qty
                self._open_leg.entry_price = (
                    self._open_leg.entry_price * self._open_leg.qty + fill.price * fill.qty
                ) / total_qty
                self._open_leg.qty = total_qty
                self._open_leg.fee_in += fill.fee
                self._holdings_qty = total_qty
                self._holdings_avg = self._open_leg.entry_price
            else:
                # Closing a short leg (or partial close).
                self._close_or_reduce(fill)
        else:  # SELL
            self._cash += notional - fill.fee
            if self._open_leg is None:
                # New short leg.
                self._open_leg = _OpenLeg(
                    side="SELL",
                    qty=fill.qty,
                    entry_price=fill.price,
                    entry_ts=fill.timestamp_utc,
                    fee_in=fill.fee,
                )
                self._holdings_side = "SELL"
                self._holdings_qty = -fill.qty
                self._holdings_avg = fill.price
                self._holdings_opened_at = fill.timestamp_utc
            elif self._open_leg.side == "SELL":
                # Adding to short.
                total_qty = self._open_leg.qty + fill.qty
                self._open_leg.entry_price = (
                    self._open_leg.entry_price * self._open_leg.qty + fill.price * fill.qty
                ) / total_qty
                self._open_leg.qty = total_qty
                self._open_leg.fee_in += fill.fee
                self._holdings_qty = -total_qty
                self._holdings_avg = self._open_leg.entry_price
            else:
                # Closing a long leg.
                self._close_or_reduce(fill)

        # Notify the strategy via on_fill (after ledger update, mirroring live).
        from quanta_core.types import Fill as _Fill

        ledger_fill = _Fill(
            order_id=f"sim-{self._co_id_counter}",
            client_order_id=ClientOrderId(fill.client_order_id),
            symbol=fill.symbol,
            side=fill.side,
            qty=fill.qty,
            price=fill.price,
            fee=fill.fee,
            timestamp_utc=fill.timestamp_utc,
            venue="paper",
        )
        self._co_id_counter += 1
        assert self._strategy is not None
        self._strategy.on_fill(ledger_fill)

    def _close_or_reduce(self, fill: SimFill) -> None:
        """Apply a closing fill against the open leg, recording a TradeRecord."""
        assert self._open_leg is not None
        leg = self._open_leg
        # Sign convention: closing qty is the min of leg.qty and fill.qty.
        close_qty = min(leg.qty, fill.qty)
        # PnL for the closed slice.
        if leg.side == "BUY":
            pnl = (fill.price - leg.entry_price) * close_qty
        else:
            pnl = (leg.entry_price - fill.price) * close_qty
        # Pro-rata the entry fee.
        fee_in_slice = (leg.fee_in * close_qty / leg.qty).quantize(
            Decimal("0.00000001"),
            rounding=ROUND_HALF_UP,
        )
        fee_total = fee_in_slice + fill.fee
        self._all_trades.append(
            TradeRecord(
                symbol=fill.symbol,
                side=leg.side,
                entry_price=leg.entry_price,
                exit_price=fill.price,
                qty=close_qty,
                entry_ts=leg.entry_ts,
                exit_ts=fill.timestamp_utc,
                pnl=(pnl - fee_total).quantize(Decimal("0.00000001"), rounding=ROUND_HALF_UP),
                fee_total=fee_total,
                bars_held=leg.bars_held,
            )
        )
        remaining = leg.qty - close_qty
        if remaining > 0:
            leg.qty = remaining
            leg.fee_in -= fee_in_slice
            self._holdings_qty = remaining if leg.side == "BUY" else -remaining
        else:
            # Leg fully closed.
            self._open_leg = None
            self._holdings_qty = _ZERO
            self._holdings_side = None
            self._holdings_avg = _ZERO
            self._holdings_opened_at = None

    def _mark_equity(self, bar: Bar) -> EquityPoint:
        """Mark holdings to ``bar.close`` and emit an :class:`EquityPoint`."""
        if self._open_leg is None:
            holdings_value = _ZERO
        elif self._open_leg.side == "BUY":
            holdings_value = self._open_leg.qty * bar.close
        else:
            # For a short, holdings_value is the *negative* mark — open
            # short equity goes up when price falls; the cash balance
            # already captured the proceeds at entry.
            holdings_value = -self._open_leg.qty * bar.close
        equity = self._cash + holdings_value
        return EquityPoint(
            timestamp_utc=bar.timestamp_utc,
            equity=equity,
            cash=self._cash,
            holdings_value=holdings_value,
        )

    def _position_for(self, symbol: Symbol) -> Position | None:
        """Build a :class:`Position` snapshot for the Context if open."""
        if self._open_leg is None or symbol != self._config.symbol:
            return None
        leg = self._open_leg
        last_close = self._history[-1].close if self._history else leg.entry_price
        # Use a strictly-positive mark; if last_close is zero (shouldn't happen
        # in practice), fall back to the entry price to keep the position
        # invariant valid.
        mark = last_close if last_close > 0 else leg.entry_price
        unrealised = (
            (mark - leg.entry_price) * leg.qty
            if leg.side == "BUY"
            else (leg.entry_price - mark) * leg.qty
        )
        return Position(
            symbol=symbol,
            qty=leg.qty if leg.side == "BUY" else -leg.qty,
            avg_entry=leg.entry_price,
            mark=mark,
            unrealized_pnl=unrealised,
            side=leg.side,
            asset_class="crypto",  # backtest treats every symbol as fungible
            opened_at=leg.entry_ts,
            subsystem_tag="backtest",
        )

    def _history_view(
        self,
        symbol: Symbol,
        timeframe: Timeframe,
        n: int,
    ) -> Sequence[Bar]:
        """Return the engine's tail-of-history for the Context."""
        if symbol != self._config.symbol or timeframe != self._config.timeframe:
            return ()
        if n <= 0:
            return ()
        if n >= len(self._history):
            return tuple(self._history)
        return tuple(self._history[-n:])

    def _summarise(self) -> SummaryMetrics:
        """Compute summary metrics from the equity curve + trade ledger."""
        starting = self._config.starting_equity
        ending = self._equity_curve[-1].equity if self._equity_curve else starting
        total_return_pct = ((ending / starting) - _ONE) if starting != _ZERO else _ZERO
        # Per-bar returns drive the Sharpe ratio. We use simple returns
        # rather than log-returns because the equity curve can dip below the
        # starting equity (drawdown), and log of a non-positive number is
        # undefined.
        rets: list[float] = []
        prev_eq = starting
        for pt in self._equity_curve:
            if prev_eq != _ZERO:
                rets.append(float((pt.equity - prev_eq) / prev_eq))
            prev_eq = pt.equity
        sharpe = _sharpe_ratio(rets)
        max_dd = _max_drawdown(self._equity_curve, starting)
        wins = sum(1 for t in self._all_trades if t.pnl > 0)
        n_trades = len(self._all_trades)
        win_rate = wins / n_trades if n_trades > 0 else 0.0
        total_fees = sum((f.fee for f in self._all_fills), _ZERO)
        return SummaryMetrics(
            n_trades=n_trades,
            n_proposals=len(self._all_proposals),
            n_fills=len(self._all_fills),
            starting_equity=starting,
            ending_equity=ending,
            total_return_pct=total_return_pct,
            win_rate=win_rate,
            sharpe=sharpe,
            max_drawdown_pct=max_dd,
            total_fees=total_fees,
        )


# ---------------------------------------------------------------------------
# Metric helpers — top-level so walk_forward can reuse them.
# ---------------------------------------------------------------------------


def _sharpe_ratio(rets: Sequence[float]) -> float:
    """Annualised Sharpe ratio over a sequence of per-bar simple returns.

    Annualisation factor defaults to 252 (trading days). For sub-daily
    backtests this slightly overstates the ratio; the parity oracle does
    not depend on the absolute number — only that backtest and live agree.
    """
    if len(rets) < 2:
        return 0.0
    mean: float = float(sum(rets)) / len(rets)
    sq: float = float(sum((r - mean) ** 2 for r in rets))
    var: float = sq / (len(rets) - 1)
    if var <= 0:
        return 0.0
    stdev: float = var**0.5
    sharpe: float = (mean / stdev) * (252**0.5)
    return sharpe


def _max_drawdown(
    curve: Sequence[EquityPoint],
    starting_equity: Decimal,
) -> float:
    """Return the largest peak-to-trough drawdown as a positive fraction."""
    if not curve:
        return 0.0
    peak = starting_equity
    max_dd = _ZERO
    for pt in curve:
        if pt.equity > peak:
            peak = pt.equity
        if peak > 0:
            dd = (peak - pt.equity) / peak
            if dd > max_dd:
                max_dd = dd
    return float(max_dd) if max_dd >= 0 else 0.0


# Re-export type used by the engine helpers' annotations.
__all__ = [
    "BacktestConfig",
    "BacktestEngine",
    "BacktestStep",
    "FixedBpsSlippageModel",
    "NoSlippageModel",
    "SlippageModel",
]
