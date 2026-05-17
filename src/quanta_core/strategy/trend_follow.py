"""TrendFollow — minimum-viable dual-SMA trend-following strategy (V4).

Pairs with :class:`quanta_core.strategy.mean_rev_bb.MeanRevBB`: where
``MeanRevBB`` fades deviations, this strategy rides them. LONG-only
because Coinbase Spot does not support shorting; the SELL side here is
purely an inventory exit, never a fresh short.

Signal logic
------------
* Indicators: short SMA (default 8) and long SMA (default 21) of close.
* Enter LONG when ALL of:
    - ``state["regime"] == "trending_up"`` (strict — ``mean_reverting`` does not qualify),
    - ``close > short_ma``,
    - ``short_ma > long_ma``,
    - no existing long position.
* Exit (SELL the full inventory qty) when we hold a long AND either:
    - ``close < short_ma`` (momentum break), OR
    - ``state["regime"] in {"trending_down", "high_volatility"}``.
* Everything else -> FLAT (empty sequence).

Conviction
----------
V4 ``OrderProposal`` carries no ``conviction`` field, so we mirror
``MeanRevBB``:

1. Expose ``self.last_conviction`` (handy for tests / logs).
2. Fold conviction into ``qty = base_qty * conviction`` for BUYs.

For BUYs, conviction scales by ``(close - short_ma) / short_ma`` clamped
to ``[0.4, 0.95]`` — larger breakouts get higher conviction. For SELLs we
use ``1.0`` (full exit; size is the inventory qty, not ``base_qty``).

Regime input
------------
Same pattern as ``MeanRevBB``: regime arrives via the mutable
``self.state`` dict seeded from ``config["state"]``. The live engine
pokes it once per bar; tests inject directly.

References
----------
* DESIGN-LOCK §5 — Strategy ABC is sync, ``__init__(ctx, config)``.
* ``quanta_core.strategy.base.Strategy`` — the ABC we extend.
* ``quanta_core.strategy.mean_rev_bb`` — the sibling strategy whose API
  shape we deliberately mirror.
"""

from __future__ import annotations

import uuid
from decimal import Decimal
from statistics import fmean
from typing import TYPE_CHECKING, Any

from quanta_core.strategy.base import Strategy
from quanta_core.types import ClientOrderId, OrderProposal, Symbol

if TYPE_CHECKING:  # pragma: no cover — typing only
    from collections.abc import Sequence

    from quanta_core.types import Bar, Context, Timeframe


# ---------------------------------------------------------------------------
# Tunables (overridable via config)
# ---------------------------------------------------------------------------

_DEFAULT_SHORT_WINDOW = 8
_DEFAULT_LONG_WINDOW = 21
_DEFAULT_BASE_QTY = Decimal("1")
_DEFAULT_ASSET_CLASS = "crypto"

# Conviction is clamped to this band per the spec.
_CONV_MIN = 0.4
_CONV_MAX = 0.95

# Strict — only trending_up gates an entry. mean_reverting belongs to MeanRevBB.
_ENTRY_REGIME = "trending_up"
# Regimes that force-exit an open long even when momentum is intact.
_EXIT_REGIMES = frozenset({"trending_down", "high_volatility"})
# Minimum HMM posterior probability required to permit entry. Must match
# MeanRevBB._MIN_ENTRY_PROBABILITY so both strategies apply the same low-
# confidence cut-off. Was referenced at on_candle() but never declared here —
# every cycle silently NameError'd inside the dispatcher's hook_exceptions
# guard, so TrendFollow produced zero entries until this declaration landed.
_MIN_ENTRY_PROBABILITY = 0.85


class TrendFollow(Strategy):
    """Dual-SMA trend-following strategy (long-only, regime-gated)."""

    name = "trend_follow"

    def __init__(self, ctx: Context, config: dict[str, Any]) -> None:
        super().__init__(ctx, config)
        self.short_window: int = int(self.config.get("short_window", _DEFAULT_SHORT_WINDOW))
        self.long_window: int = int(self.config.get("long_window", _DEFAULT_LONG_WINDOW))
        if self.short_window >= self.long_window:
            msg = (
                f"short_window ({self.short_window}) must be < "
                f"long_window ({self.long_window})"
            )
            raise ValueError(msg)
        self.base_qty: Decimal = Decimal(str(self.config.get("base_qty", _DEFAULT_BASE_QTY)))
        self.asset_class: str = str(self.config.get("asset_class", _DEFAULT_ASSET_CLASS))
        # Symbol the engine wires this instance to. Falls back to the bar's
        # own symbol when not present (single-symbol shortcut).
        symbol_cfg = self.config.get("symbol")
        self.symbol: Symbol | None = Symbol(symbol_cfg) if symbol_cfg else None
        timeframe_cfg = self.config.get("timeframe", "5m")
        self.timeframe: Timeframe = timeframe_cfg  # type: ignore[assignment]
        # Mutable per-bar state; engine pokes regime in here before on_candle.
        initial_state = self.config.get("state", {})
        self.state: dict[str, Any] = dict(initial_state) if initial_state else {}
        # Exposed for tests / logs.
        self.last_conviction: float = 0.0

    # ------------------------------------------------------------------
    # Mandatory hook
    # ------------------------------------------------------------------

    def on_candle(self, bar: Bar) -> Sequence[OrderProposal]:
        """Process one closed bar; emit BUY / SELL / empty per the spec."""
        symbol = self.symbol or bar.symbol

        # Pull enough history to compute the long SMA. The ``get_history``
        # contract returns the last n closed bars (chronological); we ask
        # for ``long_window`` because the long SMA dominates the warmup.
        history = self.ctx.get_history(symbol, self.timeframe, self.long_window)
        if len(history) < self.long_window:
            # Warm-up: not enough closes for a stable long SMA.
            return ()

        closes = [float(b.close) for b in history]
        long_ma = fmean(closes[-self.long_window :])
        short_ma = fmean(closes[-self.short_window :])
        close = float(bar.close)

        position = self.ctx.get_position(symbol)
        regime = self.state.get("regime", "unknown")
        regime_prob = float(self.state.get("regime_probability") or 0.0)
        has_long = (
            position is not None
            and position.side == "BUY"
            and position.qty > 0
        )

        # ----- Exit first: momentum break OR regime degrade.
        if has_long:
            assert position is not None  # for type-checkers
            if close < short_ma or regime in _EXIT_REGIMES:
                self.last_conviction = 1.0
                return (
                    self._build_proposal(
                        symbol,
                        "SELL",
                        position.qty,
                        close=close,
                        short_ma=short_ma,
                        long_ma=long_ma,
                        bar=bar,
                    ),
                )
            return ()

        # ----- Entry: strict trending_up + bullish MA cross + close above short
        # + high-confidence regime (2026-05-15 — match mean_rev_bb gate).
        if regime != _ENTRY_REGIME:
            return ()
        if regime_prob < _MIN_ENTRY_PROBABILITY:
            return ()
        if not (close > short_ma and short_ma > long_ma):
            return ()

        conviction = self._conviction(close=close, short_ma=short_ma)
        self.last_conviction = conviction
        qty = self._size(conviction)
        if qty <= 0:
            return ()
        return (
            self._build_proposal(
                symbol,
                "BUY",
                qty,
                close=close,
                short_ma=short_ma,
                long_ma=long_ma,
                bar=bar,
            ),
        )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _conviction(self, *, close: float, short_ma: float) -> float:
        """Scale conviction by relative breakout above the short MA.

        ``(close - short_ma) / short_ma`` is the percentage breakout. We
        clamp the result to ``[_CONV_MIN, _CONV_MAX]`` so wild candles
        cannot levitate position sizes into orbit.
        """
        if short_ma <= 0:
            # Degenerate (non-positive prices shouldn't happen for crypto,
            # but guard anyway). Floor at the minimum so we don't divide-by-zero.
            return _CONV_MIN
        raw = (close - short_ma) / short_ma
        return max(_CONV_MIN, min(_CONV_MAX, raw))

    def _size(self, conviction: float) -> Decimal:
        """Translate conviction into a Decimal qty (base_qty * conviction)."""
        raw = self.base_qty * Decimal(str(conviction))
        # Round to 8dp to keep the wire format predictable for crypto.
        return raw.quantize(Decimal("0.00000001"))

    # Fixed namespace UUID for deterministic client_order_id derivation.
    # MUST match MeanRevBB._COID_NAMESPACE so the (strategy, symbol, side, ts)
    # tuple gives a globally-unique-but-stable id across the V4 stack.
    _COID_NAMESPACE = uuid.UUID("a8e9c46f-0e2e-4b4a-9d1a-3f5e6c0b4a7e")

    def _build_proposal(
        self,
        symbol: Symbol,
        side: str,
        qty: Decimal,
        *,
        close: float,
        short_ma: float,
        long_ma: float,
        bar: Bar,
    ) -> OrderProposal:
        """Construct an OrderProposal with a JSON-friendly rationale.

        ``client_order_id`` is deterministically derived from
        (strategy, symbol, side, bar timestamp). A crashed-mid-cycle restart
        that re-evaluates the same bar produces the same id, which the
        ``execution_idempotency`` unique constraint then rejects — preventing
        duplicate proposals and double-counted paper fills.
        """
        rationale = (
            f"trend_follow side={side} close={close:.6f} "
            f"short_ma={short_ma:.6f} long_ma={long_ma:.6f} "
            f"conviction={self.last_conviction:.4f} "
            f"regime={self.state.get('regime', 'unknown')}"
        )
        coid_seed = f"trend_follow|{symbol}|{side}|{bar.timestamp_utc.isoformat()}"
        coid = uuid.uuid5(self._COID_NAMESPACE, coid_seed)
        return OrderProposal(
            symbol=symbol,
            side=side,  # type: ignore[arg-type]  # Literal["BUY","SELL"]
            qty=qty,
            order_type="market",
            client_order_id=ClientOrderId(str(coid)),
            rationale=rationale,
            asset_class=self.asset_class,  # type: ignore[arg-type]
        )
