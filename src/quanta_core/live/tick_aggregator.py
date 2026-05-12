"""Tick -> Bar aggregator.

Buffers ticks per (symbol, timeframe) and emits a closed Bar the first tick
*after* the boundary. Supports the timeframes locked by the design contract:
``1m``, ``5m``, ``15m``, ``1h``, ``4h``, ``1d``.

VWAP is computed as ``Σ(price * size) / Σ(size)``; on a zero-volume bar
(no trades within the window) the aggregator does not emit anything — bars
are only forged once at least one tick has been observed.

Late ticks
----------
A late tick is one whose ``ts`` is strictly less than the open boundary of
the currently-open bar at that timeframe. Late ticks are logged and dropped
(returns from ``on_tick`` exclude their effect). The drop count is exposed
through ``late_tick_count`` for observability.

Naive datetimes are rejected at the boundary — every ``Tick.ts`` must carry
a non-None tzinfo. We never silently coerce.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING

from quanta_core.util.types import Bar

if TYPE_CHECKING:
    from quanta_core.util.types import Symbol, Tick, Timeframe

_log = logging.getLogger(__name__)


_TIMEFRAME_SECONDS: dict[str, int] = {
    "1m": 60,
    "5m": 300,
    "15m": 900,
    "1h": 3600,
    "4h": 14_400,
    "1d": 86_400,
}


@dataclass
class _OpenBar:
    """Mutable in-progress bar state for one (symbol, timeframe)."""

    symbol: Symbol
    timeframe: Timeframe
    open_ts: datetime
    close_ts: datetime
    open_price: Decimal
    high: Decimal
    low: Decimal
    last_price: Decimal
    volume: Decimal = Decimal("0")
    price_volume_sum: Decimal = Decimal("0")
    trades: int = 0


@dataclass
class TickAggregator:
    """Per-symbol, multi-timeframe tick aggregator.

    Parameters
    ----------
    symbol
        Symbol this aggregator is bound to.
    timeframes
        List of timeframes to maintain. Must each be one of
        ``1m``, ``5m``, ``15m``, ``1h``, ``4h``, ``1d``.

    Notes
    -----
    Bar boundaries are aligned to the UTC epoch. A 5m bar covers
    ``[unix_ts - unix_ts % 300, unix_ts - unix_ts % 300 + 300)``.
    """

    symbol: Symbol
    timeframes: list[Timeframe]
    late_tick_count: int = 0
    _open_bars: dict[str, _OpenBar] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for tf in self.timeframes:
            if tf not in _TIMEFRAME_SECONDS:
                raise ValueError(f"unsupported timeframe: {tf}")

    def on_tick(self, tick: Tick) -> list[Bar]:
        """Ingest one tick; return any bars closed by this tick.

        Returns
        -------
        list[Bar]
            Zero or more newly-closed Bars, one per timeframe whose
            boundary fell between the previous tick and this one.

        Raises
        ------
        ValueError
            If ``tick.ts`` is naive (no tzinfo) or if the tick is for a
            different symbol than the aggregator was constructed for.
        """

        if tick.ts.tzinfo is None:
            raise ValueError("tick.ts must be tz-aware (UTC)")
        if tick.symbol != self.symbol:
            raise ValueError(
                f"tick.symbol {tick.symbol!r} != aggregator {self.symbol!r}",
            )

        closed: list[Bar] = []
        for tf in self.timeframes:
            bar = self._step_one(tf, tick)
            if bar is not None:
                closed.append(bar)
        return closed

    # ----- private helpers -----

    def _step_one(self, tf: Timeframe, tick: Tick) -> Bar | None:
        """Update one timeframe; return the closed Bar if a boundary crossed."""

        seconds = _TIMEFRAME_SECONDS[tf]
        boundary_open = self._floor_to_boundary(tick.ts, seconds)
        boundary_close = boundary_open + timedelta(seconds=seconds)

        open_bar = self._open_bars.get(tf)
        emitted: Bar | None = None

        if open_bar is None:
            self._open_bars[tf] = self._new_open_bar(
                tf,
                tick,
                boundary_open,
                boundary_close,
            )
            return None

        if tick.ts < open_bar.open_ts:
            self.late_tick_count += 1
            _log.warning(
                "tick_aggregator.late_tick",
                extra={
                    "symbol": str(self.symbol),
                    "timeframe": tf,
                    "tick_ts": tick.ts.isoformat(),
                    "bar_open_ts": open_bar.open_ts.isoformat(),
                },
            )
            return None

        if tick.ts >= open_bar.close_ts:
            emitted = self._close(open_bar)
            self._open_bars[tf] = self._new_open_bar(
                tf,
                tick,
                boundary_open,
                boundary_close,
            )
            return emitted

        # Tick belongs to the currently-open bar — accumulate.
        self._accumulate(open_bar, tick)
        return None

    @staticmethod
    def _floor_to_boundary(ts: datetime, seconds: int) -> datetime:
        epoch = int(ts.replace(tzinfo=UTC).timestamp())
        floored = epoch - (epoch % seconds)
        return datetime.fromtimestamp(floored, tz=UTC)

    def _new_open_bar(
        self,
        tf: Timeframe,
        tick: Tick,
        open_ts: datetime,
        close_ts: datetime,
    ) -> _OpenBar:
        bar = _OpenBar(
            symbol=self.symbol,
            timeframe=tf,
            open_ts=open_ts,
            close_ts=close_ts,
            open_price=tick.price,
            high=tick.price,
            low=tick.price,
            last_price=tick.price,
        )
        self._accumulate(bar, tick)
        return bar

    @staticmethod
    def _accumulate(bar: _OpenBar, tick: Tick) -> None:
        if tick.price > bar.high:
            bar.high = tick.price
        if tick.price < bar.low:
            bar.low = tick.price
        bar.last_price = tick.price
        bar.volume += tick.size
        bar.price_volume_sum += tick.price * tick.size
        bar.trades += 1

    @staticmethod
    def _close(bar: _OpenBar) -> Bar:
        vwap = bar.price_volume_sum / bar.volume if bar.volume > 0 else bar.last_price
        return Bar(
            symbol=bar.symbol,
            timeframe=bar.timeframe,
            open_ts=bar.open_ts,
            close_ts=bar.close_ts,
            open=bar.open_price,
            high=bar.high,
            low=bar.low,
            close=bar.last_price,
            volume=bar.volume,
            vwap=vwap,
            trades=bar.trades,
        )


__all__ = ["TickAggregator"]
