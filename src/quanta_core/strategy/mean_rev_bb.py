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
* Exit LONG (recovery) when a position is open AND ``close > middle_bb``
  (the SMA). ``exit_reason`` = ``"mean_reversion"``.
* Exit LONG (stop-loss) when ``close < entry_price * (1 - max_loss_pct)``.
  ``exit_reason`` = ``"stop_loss"``. This gate was added in response to
  audit/2026-05-14-night/07-architecture-review.md §P2 + Hard Requirements §2
  which identified that a lower-band entry in a trending-down market had NO
  exit path until recovery — a structurally unbounded-loss scenario. The
  ``max_loss_pct`` default (0.04) caps the loss at 4 % of entry price, wide
  enough to survive typical BB-channel noise but tight enough to cut a
  trending move before it becomes catastrophic. Operators may override it via
  config (key ``"max_loss_pct"``). Do NOT remove this gate without a
  replacement risk control in place.
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
# Maximum tolerated loss from entry before a forced stop-loss exit.
# 4 % is conservative enough to survive BB-channel noise yet short enough
# to cut a trending-down move.  Override via config key ``"max_loss_pct"``.
_DEFAULT_MAX_LOSS_PCT = 0.04

# Conviction is clamped to this band per the spec.
_CONV_MIN = 0.4
_CONV_MAX = 0.95

# Regimes that permit a fresh long entry.
_PERMISSIVE_REGIMES = frozenset({"trending_up", "mean_reverting"})
# Regimes that force an exit on any open BUY position (regime-flip exit).
# Match trend_follow's exit set so behavior is symmetric across strategies.
_EXIT_REGIMES = frozenset({"trending_down", "high_volatility"})
# Minimum HMM posterior probability required to permit entry. 2026-05-15
# tightening after today's -$1,010 loss on a BTC trade opened during a
# mean_reverting classification with p=0.65 that flipped to trending_down
# p=0.99 two hours later. 0.85 keeps us out of low-confidence regimes
# where the HMM is essentially guessing.
_MIN_ENTRY_PROBABILITY = 0.85


class MeanRevBB(Strategy):
    """Bollinger-Band mean-reversion strategy (long-only, regime-gated)."""

    name = "mean_rev_bb"

    def __init__(self, ctx: Context, config: dict[str, Any]) -> None:
        super().__init__(ctx, config)
        self.window: int = int(self.config.get("window", _DEFAULT_WINDOW))
        self.std_mult: float = float(self.config.get("std_mult", _DEFAULT_STD_MULT))
        self.base_qty: Decimal = Decimal(str(self.config.get("base_qty", _DEFAULT_BASE_QTY)))
        self.asset_class: str = str(self.config.get("asset_class", _DEFAULT_ASSET_CLASS))
        # Fractional stop-loss distance from entry price (e.g. 0.04 = 4 %).
        self.max_loss_pct: float = float(self.config.get("max_loss_pct", _DEFAULT_MAX_LOSS_PCT))
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
        regime_prob = float(self.state.get("regime_probability") or 0.0)

        # ----- Exit first: if long, check (a) regime flipped adversarial,
        # (b) stop-loss hit, or (c) recovery path. All paths emit SELL;
        # they differ only in the exit_reason embedded in the rationale.
        if position is not None and position.side == "BUY" and position.qty > 0:
            # Engine's _P shim exposes avg_price (run_v4_shadow.py:186);
            # legacy/test contexts may use avg_entry. Try both.
            entry_price = float(
                getattr(position, "avg_price", None)
                or getattr(position, "avg_entry", 0.0)
            )
            stop_level = entry_price * (1.0 - self.max_loss_pct)

            # (a) regime-flip exit — added 2026-05-15. Matches trend_follow's
            # behavior: when regime turns trending_down or high_volatility,
            # exit immediately. Don't wait for the BB mean to recover; the
            # strategy's edge has evaporated.
            if regime in _EXIT_REGIMES:
                return (
                    self._build_proposal(
                        symbol, "SELL", position.qty, close, mean, lower, bar,
                        exit_reason="regime_flip",
                        extra=f"entry={entry_price:.6f} regime={regime} p={regime_prob:.2f}",
                    ),
                )

            # (b) stop-loss — price fell more than max_loss_pct from entry.
            # exit_reason embedded in rationale so it round-trips into the
            # intent JSONB column written by write_proposal_and_order.
            if close <= stop_level:
                loss_pct = (entry_price - close) / entry_price * 100.0
                return (
                    self._build_proposal(
                        symbol, "SELL", position.qty, close, mean, lower, bar,
                        exit_reason="stop_loss",
                        extra=f"entry={entry_price:.6f} stop={stop_level:.6f} loss={loss_pct:.2f}%",
                    ),
                )

            # (c) recovery exit — close back above the middle band (SMA).
            if close > middle:
                return (
                    self._build_proposal(
                        symbol, "SELL", position.qty, close, mean, lower, bar,
                        exit_reason="mean_reversion",
                    ),
                )
            return ()

        # ----- Entry: long only, regime-gated AND confidence-gated.
        if regime not in _PERMISSIVE_REGIMES:
            return ()
        if regime_prob < _MIN_ENTRY_PROBABILITY:
            # Low-confidence regime — HMM is uncertain. Don't fire entries
            # we can't justify. Added 2026-05-15 after today's BTC trade
            # opened during mean_reverting p=0.65 and lost $1,056 when
            # regime flipped to trending_down p=0.99 within 2 hours.
            return ()

        if close < lower:
            conviction = self._conviction(close=close, lower=lower, std=std)
            self.last_conviction = conviction
            qty = self._size(conviction)
            if qty <= 0:
                return ()
            return (self._build_proposal(symbol, "BUY", qty, close, mean, lower, bar),)

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

    # Fixed namespace UUID for deterministic client_order_id derivation.
    # If this constant changes, prior idempotency reservations will not
    # collide with new proposals — only change it during a deliberate
    # ledger migration. See task: "Stable client_order_id for V4 idempotency".
    _COID_NAMESPACE = uuid.UUID("a8e9c46f-0e2e-4b4a-9d1a-3f5e6c0b4a7e")

    def _build_proposal(
        self,
        symbol: Symbol,
        side: str,
        qty: Decimal,
        close: float,
        mean: float,
        lower: float,
        bar: Bar,
        *,
        exit_reason: str | None = None,
        extra: str | None = None,
    ) -> OrderProposal:
        """Construct an OrderProposal with a JSON-friendly rationale.

        ``client_order_id`` is deterministically derived from
        (strategy, symbol, side, bar timestamp). A crashed-mid-cycle restart
        that re-evaluates the same bar will produce the same id, which the
        ``execution_idempotency`` unique constraint then rejects — preventing
        duplicate proposals and double-counted paper fills. Replaces the
        previous ``uuid.uuid4()`` per-call randomness.

        ``exit_reason`` (optional) is embedded verbatim into the rationale
        string so it round-trips through the ``intent`` JSONB column in
        ``write_proposal_and_order`` without requiring a schema change to
        ``OrderProposal``. Downstream readers can extract it with a simple
        string search or by parsing the rationale field.
        """
        rationale = (
            f"mean_rev_bb side={side} close={close:.6f} mean={mean:.6f} "
            f"lower={lower:.6f} conviction={self.last_conviction:.4f} "
            f"regime={self.state.get('regime', 'unknown')}"
        )
        if exit_reason is not None:
            rationale += f" exit_reason={exit_reason}"
        if extra is not None:
            rationale += f" {extra}"
        coid_seed = f"mean_rev_bb|{symbol}|{side}|{bar.timestamp_utc.isoformat()}"
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
