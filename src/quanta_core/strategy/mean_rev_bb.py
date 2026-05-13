"""MeanRevBB — minimum-viable Bollinger-Band mean-reversion strategy (V4).

This is the V4 cutover replacement for the FreqAI/TFT-heavy
``FreqAIMeanRevV1`` Freqtrade strategy. We intentionally ship the dumbest
possible signal that still respects the regime gate, because runtime hour
zero is not the time to debug the ML stack. FreqAI + TFT re-port lands
post-cutover.

Signal logic
------------
* Enter LONG when ``close < lower_bb`` AND ``regime`` is in the
  permissive set ``{"trending_up", "mean_reverting"}``.
* Exit LONG when a position is open AND ``close > middle_bb`` (the SMA).
* All other states -> FLAT (empty sequence).
* No short side. We will add it after we've watched the long side
  behave for at least one trading day.

Conviction
----------
The spec calls for a scalar in ``[0.4, 0.95]`` derived from how far the
close has plunged below the lower band. The V4 ``OrderProposal`` has no
``conviction`` field, so we surface it two ways:

1. As an instance attribute ``last_conviction`` (handy for tests / logs).
2. Folded into ``qty`` via ``base_qty * conviction`` so position sizing
   honours conviction without inventing a new wire type.

Regime input
------------
The V4 ``Context`` protocol does NOT expose regime. To keep the change
surface tiny tonight, the strategy reads regime from ``self.state``,
which is a mutable ``dict`` seeded from ``config["state"]`` (defaults to
``{}``). The live engine wires regime into ``state`` once per bar from
the ModelForge regime feed; tests inject directly via
``strat.state["regime"] = "..."``.

References
----------
* DESIGN-LOCK §5 — Strategy ABC is sync, ``__init__(ctx, config)``.
* ``quanta_core.strategy.base.Strategy`` — the ABC we extend.
* ``user_data/strategies/FreqAIMeanRevV1.py`` — the freqtrade ancestor
  whose BB signal we are reproducing minus all the ML scaffolding.
"""

from __future__ import annotations

import uuid
from decimal import Decimal
from statistics import fmean, pstdev
from typing import TYPE_CHECKING, Any

from quanta_core.strategy.base import Strategy
from quanta_core.types import ClientOrderId, OrderProposal, Symbol

if TYPE_CHECKING:  # pragma: no cover — typing only
    from collections.abc import Sequence

    from quanta_core.types import Bar, Context, Timeframe


# ---------------------------------------------------------------------------
# Tunables (overridable via config)
# ---------------------------------------------------------------------------

_DEFAULT_WINDOW = 20
_DEFAULT_STD_MULT = 2.0
_DEFAULT_BASE_QTY = Decimal("1")
_DEFAULT_ASSET_CLASS = "crypto"

# Conviction is clamped to this band per the spec.
_CONV_MIN = 0.4
_CONV_MAX = 0.95

# Regimes that permit a fresh long entry.
_PERMISSIVE_REGIMES = frozenset({"trending_up", "mean_reverting"})


class MeanRevBB(Strategy):
    """Bollinger-Band mean-reversion strategy (long-only, regime-gated)."""

    name = "mean_rev_bb"

    def __init__(self, ctx: Context, config: dict[str, Any]) -> None:
        super().__init__(ctx, config)
        self.window: int = int(self.config.get("window", _DEFAULT_WINDOW))
        self.std_mult: float = float(self.config.get("std_mult", _DEFAULT_STD_MULT))
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
        """Process one closed bar; emit BUY/SELL/empty per the spec."""
        symbol = self.symbol or bar.symbol

        history = self.ctx.get_history(symbol, self.timeframe, self.window)
        if len(history) < self.window:
            # Warm-up: not enough closes for a stable band.
            return ()

        closes = [float(b.close) for b in history]
        mean = fmean(closes)
        # Population std matches the canonical BB convention used by talib.
        std = pstdev(closes, mu=mean)
        lower = mean - self.std_mult * std
        middle = mean
        close = float(bar.close)

        position = self.ctx.get_position(symbol)
        regime = self.state.get("regime", "unknown")

        # ----- Exit first: if long and price has reverted to the mean,
        # close the position regardless of regime (risk-managed exit).
        if position is not None and position.side == "BUY" and position.qty > 0:
            if close > middle:
                return (self._build_proposal(symbol, "SELL", position.qty, close, mean, lower),)
            return ()

        # ----- Entry: long only, regime-gated.
        if regime not in _PERMISSIVE_REGIMES:
            return ()

        if close < lower:
            conviction = self._conviction(close=close, lower=lower, std=std)
            self.last_conviction = conviction
            qty = self._size(conviction)
            if qty <= 0:
                return ()
            return (self._build_proposal(symbol, "BUY", qty, close, mean, lower),)

        return ()

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _conviction(self, *, close: float, lower: float, std: float) -> float:
        """Scale conviction by how far below the lower band the close sits.

        Distance is measured in std units below ``lower``. 0 stds below
        the band maps to ``_CONV_MIN``; saturating at ``1`` std below the
        band maps to ``_CONV_MAX``. The output is clamped to
        ``[_CONV_MIN, _CONV_MAX]``.
        """
        if std <= 0:
            # Degenerate flat market — the band collapsed; any close
            # below the (equal-to-mean) lower band is mildly convicted.
            return _CONV_MIN
        depth = (lower - close) / std  # >= 0 when close < lower
        depth = max(0.0, depth)
        scaled = _CONV_MIN + (_CONV_MAX - _CONV_MIN) * min(1.0, depth)
        return max(_CONV_MIN, min(_CONV_MAX, scaled))

    def _size(self, conviction: float) -> Decimal:
        """Translate conviction into a Decimal qty (base_qty * conviction)."""
        raw = self.base_qty * Decimal(str(conviction))
        # Round to 8dp to keep the wire format predictable for crypto.
        return raw.quantize(Decimal("0.00000001"))

    def _build_proposal(
        self,
        symbol: Symbol,
        side: str,
        qty: Decimal,
        close: float,
        mean: float,
        lower: float,
    ) -> OrderProposal:
        """Construct an OrderProposal with a JSON-friendly rationale."""
        rationale = (
            f"mean_rev_bb side={side} close={close:.6f} mean={mean:.6f} "
            f"lower={lower:.6f} conviction={self.last_conviction:.4f} "
            f"regime={self.state.get('regime', 'unknown')}"
        )
        return OrderProposal(
            symbol=symbol,
            side=side,  # type: ignore[arg-type]  # Literal["BUY","SELL"]
            qty=qty,
            order_type="market",
            client_order_id=ClientOrderId(str(uuid.uuid4())),
            rationale=rationale,
            asset_class=self.asset_class,  # type: ignore[arg-type]
        )
