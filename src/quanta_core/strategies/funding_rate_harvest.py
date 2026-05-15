"""funding_rate_harvest.py — delta-neutral perp funding-rate carry.

Pure decision module. No exchange clients, no order placement, no DB writes.
The backtest harness (``quanta_core.backtest.funding_harness``) calls into
``simulate_harvest`` to produce trade lists; the live wires (Week-3, deferred)
will call ``should_enter`` / ``should_exit`` against live funding rates.

Design doc: ``audit/2026-05-15-funding-rate-design.md``.
Evidence:    ``audit/2026-05-15-strategy-research.md`` §2.

Strategy in 1 sentence: short the perp + long the same notional spot →
delta-neutral on price; collect every positive funding payment minus
double-leg taker fees.

Numbers (locked):
- Bybit-style 8h funding cadence (operator's likely live venue uses dYdX
  hourly, but the literature and backtest are calibrated to 8h).
- Bybit/dYdX taker fee assumption: 0.055% per leg, 2 legs, 2 sides
  (entry + exit) = 0.22% round-trip per harvest cycle.
- Break-even funding rate per 8h: 0.022% (≈ 24% APY pre-fee), assuming a
  single funding period held. Holding for N periods reduces the per-period
  break-even to (0.22% / N).
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Iterable, Sequence

# ---------------------------------------------------------------------------
# Fee + threshold constants (Bybit-style; see design doc §7)
# ---------------------------------------------------------------------------

# Per-leg taker fee in basis points. 0.055% = 5.5 bps. Two legs (spot + perp).
TAKER_FEE_BPS_PER_LEG = 5.5
LEGS_PER_SIDE = 2  # spot + perp
SIDES_PER_CYCLE = 2  # entry + exit
ROUND_TRIP_FEE_BPS = TAKER_FEE_BPS_PER_LEG * LEGS_PER_SIDE * SIDES_PER_CYCLE  # = 22.0 bps = 0.22%
ROUND_TRIP_FEE_PCT = ROUND_TRIP_FEE_BPS / 10_000.0  # 0.0022

# Funding-rate thresholds (decimals; 0.00022 = 0.022% per 8h).
ENTER_THRESHOLD = 0.00022   # ≈ 24% APY pre-fee, just above break-even
EXIT_THRESHOLD = 0.00005    # ≈ 5.5% APY — exit when carry has decayed
MIN_HOLD_PERIODS = 3        # avoid 1-period whipsaws

# Funding cadence (Bybit/OKX/Binance perp default).
FUNDING_PERIOD_HOURS = 8
FUNDING_PERIODS_PER_YEAR = 365.25 * 24 / FUNDING_PERIOD_HOURS  # ≈ 1095.75
FUNDING_PERIODS_PER_DAY = 24 / FUNDING_PERIOD_HOURS            # = 3.0

# Regimes the strategy is allowed to harvest in (design doc §6).
HARVEST_REGIMES: frozenset[str] = frozenset({"trending_up", "high_volatility"})

# Tighter threshold during high_volatility — funding is noisier so demand
# more carry to cover the wider basis whip-saw.
HIGH_VOL_THRESHOLD_MULT = 1.5


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FundingTick:
    """One funding-rate observation from an exchange."""
    funding_time: datetime    # the moment funding accrues (UTC)
    rate: float               # decimal, e.g. 0.0001 == 0.01% per 8h
    spot_price: float | None = None  # mid-price at funding_time, optional


@dataclass(frozen=True)
class HarvestPosition:
    """An open delta-neutral cycle."""
    entry_time: datetime
    entry_spot_price: float
    notional_usd: float
    periods_held: int = 0


@dataclass(frozen=True)
class HarvestTrade:
    """A closed harvest cycle (= one round-trip)."""
    symbol: str
    entry_time: datetime
    exit_time: datetime
    notional_usd: float
    periods_held: int
    funding_pnl_usd: float          # sum of funding payments collected
    fee_usd: float                  # entry + exit fee
    pnl_usd: float                  # funding_pnl_usd - fee_usd
    pnl_pct: float                  # pnl_usd / notional_usd
    exit_reason: str                # "funding_decayed" | "regime_exit" | "end_of_data"
    avg_funding_rate: float         # mean rate over the holding window
    regime_at_entry: str

    def as_dict(self) -> dict[str, object]:
        return {
            "pair": self.symbol,
            "entry_ts": self.entry_time.isoformat(),
            "exit_ts": self.exit_time.isoformat(),
            "entry_price": float(self.notional_usd),  # legacy field name; harness ignores
            "exit_price": float(self.notional_usd),
            "side": "neutral_short_perp",
            "notional_usd": float(self.notional_usd),
            "periods_held": int(self.periods_held),
            "funding_pnl_usd": float(self.funding_pnl_usd),
            "fee_usd": float(self.fee_usd),
            "pnl_usd": float(self.pnl_usd),
            "pnl_pct": float(self.pnl_pct),
            "exit_reason": self.exit_reason,
            "avg_funding_rate": float(self.avg_funding_rate),
            "regime_at_entry": self.regime_at_entry,
            "fee_total": float(self.fee_usd),
            "bars_held": int(self.periods_held),
        }


# ---------------------------------------------------------------------------
# Decision API
# ---------------------------------------------------------------------------


def _threshold_for_regime(regime: str) -> float:
    if regime == "high_volatility":
        return ENTER_THRESHOLD * HIGH_VOL_THRESHOLD_MULT
    return ENTER_THRESHOLD


class FundingRateHarvest:
    """Decision-only strategy. The backtest and (future) live runner both
    drive this class one funding tick at a time.

    The class does not hold position state — that is the caller's job. This
    keeps it equally reusable from a backtest loop and a live event handler.
    """

    name = "funding_rate_harvest"

    def __init__(
        self,
        enter_threshold: float = ENTER_THRESHOLD,
        exit_threshold: float = EXIT_THRESHOLD,
        min_hold_periods: int = MIN_HOLD_PERIODS,
        harvest_regimes: Iterable[str] = HARVEST_REGIMES,
        high_vol_mult: float = HIGH_VOL_THRESHOLD_MULT,
    ) -> None:
        if enter_threshold <= exit_threshold:
            raise ValueError("enter_threshold must exceed exit_threshold")
        if min_hold_periods < 1:
            raise ValueError("min_hold_periods must be >= 1")
        self.enter_threshold = float(enter_threshold)
        self.exit_threshold = float(exit_threshold)
        self.min_hold_periods = int(min_hold_periods)
        self.harvest_regimes = frozenset(harvest_regimes)
        self.high_vol_mult = float(high_vol_mult)

    # -- decision hooks -----------------------------------------------------

    def should_enter(self, funding_rate: float, regime: str) -> bool:
        """Open a delta-neutral cycle iff regime is in the harvest set AND
        the current funding rate clears the (regime-adjusted) threshold."""
        if regime not in self.harvest_regimes:
            return False
        threshold = self.enter_threshold
        if regime == "high_volatility":
            threshold *= self.high_vol_mult
        return funding_rate >= threshold

    def should_exit(
        self,
        funding_rate: float,
        regime: str,
        position: HarvestPosition,
    ) -> tuple[bool, str]:
        """Decide whether to close an open cycle. Returns (should_exit, reason).

        Exit if:
          - regime has rotated out of the harvest set, OR
          - funding_rate has decayed below ``exit_threshold`` AND we have
            satisfied the minimum hold,
          - funding_rate has gone *negative* (we'd be paying instead of
            receiving) regardless of min_hold (the carry is now negative).
        """
        if funding_rate < 0:
            # Hard exit: we'd start paying on the next funding tick.
            return True, "funding_negative"
        if regime not in self.harvest_regimes:
            # Regime no longer favourable.
            if position.periods_held >= self.min_hold_periods:
                return True, "regime_exit"
            # Soft hold: regime flipped but we haven't earned enough to
            # cover the round-trip fee yet — wait one more period.
            return False, "regime_exit_pending_min_hold"
        if (
            funding_rate < self.exit_threshold
            and position.periods_held >= self.min_hold_periods
        ):
            return True, "funding_decayed"
        return False, ""


# ---------------------------------------------------------------------------
# Backtest simulation
# ---------------------------------------------------------------------------


def simulate_harvest(
    symbol: str,
    ticks: Sequence[FundingTick],
    regimes: Sequence[str],
    notional_usd: float = 10_000.0,
    strategy: FundingRateHarvest | None = None,
    fee_bps_per_leg: float = TAKER_FEE_BPS_PER_LEG,
) -> list[HarvestTrade]:
    """Replay one symbol's funding-rate history into a list of closed trades.

    Parameters
    ----------
    symbol : trading symbol, e.g. "BTC-USDT-SWAP" or "BTC".
    ticks  : chronologically sorted FundingTick sequence.
    regimes: regime label per tick (same length as ``ticks``). Caller is
             responsible for alignment.
    notional_usd : simulated notional per cycle. Sized constant per cycle
             — position-sizing logic lives in the live wiring (design doc §5).
    strategy : decision instance (default: ``FundingRateHarvest()``).
    fee_bps_per_leg : taker fee per leg in basis points. Default = 5.5
             (Bybit / dYdX baseline); the harness can override for venues
             with a different fee schedule.

    Returns
    -------
    list[HarvestTrade] — one entry per closed harvest cycle.

    Notes
    -----
    The funding payment received in period ``i`` while holding is computed as
    ``notional_usd * funding_rate[i]``. We treat the holder as short-perp +
    long-spot; positive funding flows TO the short.
    """
    if len(ticks) != len(regimes):
        raise ValueError(
            f"ticks and regimes must align ({len(ticks)} vs {len(regimes)})"
        )

    strat = strategy or FundingRateHarvest()
    fee_per_leg_pct = fee_bps_per_leg / 10_000.0
    cycle_fee_usd = notional_usd * fee_per_leg_pct * LEGS_PER_SIDE * SIDES_PER_CYCLE

    trades: list[HarvestTrade] = []
    open_pos: HarvestPosition | None = None
    accrued_funding_usd: float = 0.0
    accrued_rates: list[float] = []
    regime_at_entry: str = ""

    for i, (tick, regime) in enumerate(zip(ticks, regimes)):
        if open_pos is None:
            # Look for an entry signal.
            if strat.should_enter(tick.rate, regime):
                open_pos = HarvestPosition(
                    entry_time=tick.funding_time,
                    entry_spot_price=tick.spot_price or 0.0,
                    notional_usd=notional_usd,
                    periods_held=0,
                )
                regime_at_entry = regime
                accrued_funding_usd = 0.0
                accrued_rates = []
            continue

        # Position is open — accrue this period's funding payment, then
        # check whether to exit.
        accrued_funding_usd += notional_usd * tick.rate
        accrued_rates.append(tick.rate)
        open_pos = HarvestPosition(
            entry_time=open_pos.entry_time,
            entry_spot_price=open_pos.entry_spot_price,
            notional_usd=open_pos.notional_usd,
            periods_held=open_pos.periods_held + 1,
        )

        exit_now, reason = strat.should_exit(tick.rate, regime, open_pos)
        is_last = (i == len(ticks) - 1)
        if exit_now or is_last:
            if is_last and not exit_now:
                reason = "end_of_data"
            avg_rate = sum(accrued_rates) / len(accrued_rates) if accrued_rates else 0.0
            trade = HarvestTrade(
                symbol=symbol,
                entry_time=open_pos.entry_time,
                exit_time=tick.funding_time,
                notional_usd=open_pos.notional_usd,
                periods_held=open_pos.periods_held,
                funding_pnl_usd=accrued_funding_usd,
                fee_usd=cycle_fee_usd,
                pnl_usd=accrued_funding_usd - cycle_fee_usd,
                pnl_pct=(accrued_funding_usd - cycle_fee_usd) / open_pos.notional_usd,
                exit_reason=reason,
                avg_funding_rate=avg_rate,
                regime_at_entry=regime_at_entry,
            )
            trades.append(trade)
            open_pos = None
            accrued_funding_usd = 0.0
            accrued_rates = []
            regime_at_entry = ""

    return trades


# ---------------------------------------------------------------------------
# Lightweight regime stub used by the backtest harness when the live HMM
# does not (yet) cover the historical window. See design doc §9.
# ---------------------------------------------------------------------------


def synthetic_regimes_from_spot(
    funding_times: Sequence[datetime],
    spot_bars: Sequence[tuple[datetime, float]],
    high_vol_quantile: float = 0.75,
) -> list[str]:
    """Classify each funding tick into one of:
    ``trending_up`` / ``trending_down`` / ``high_volatility`` /
    ``mean_reverting``.

    Heuristic (NOT the production HMM — see design doc §9):
        - 24h spot return > +0.5%  → trending_up
        - 24h spot return < -0.5%  → trending_down
        - 24h hourly-σ in top 25%  → high_volatility (overrides direction
          calls to honour "vol > trend" stacking)
        - otherwise                → mean_reverting

    Parameters
    ----------
    funding_times : sorted UTC timestamps where funding accrued.
    spot_bars     : (timestamp_utc, close_price) hourly bars covering the
                    same window. Need not be the same length as funding_times.
    high_vol_quantile : top quantile of rolling-24h vol that triggers the
                    high_vol label.
    """
    if not spot_bars:
        return ["mean_reverting"] * len(funding_times)

    bars = sorted(spot_bars, key=lambda b: b[0])
    times = [b[0] for b in bars]
    prices = [b[1] for b in bars]

    # Rolling 24h log-returns and 24h volatility, indexed off the bars.
    n = len(bars)
    returns_24h: list[float] = [0.0] * n
    vol_24h: list[float] = [0.0] * n
    # Bars are hourly → 24 bars per 24h window.
    LOOKBACK = 24
    for i in range(n):
        lo = max(0, i - LOOKBACK)
        if i - lo < 2 or prices[lo] <= 0:
            continue
        # 24h cumulative return.
        returns_24h[i] = (prices[i] / prices[lo]) - 1.0
        # 24h rolling volatility = stddev of 1h log returns.
        log_rets: list[float] = []
        for j in range(lo + 1, i + 1):
            if prices[j - 1] > 0 and prices[j] > 0:
                log_rets.append(math.log(prices[j] / prices[j - 1]))
        if len(log_rets) >= 2:
            mu = sum(log_rets) / len(log_rets)
            var = sum((r - mu) ** 2 for r in log_rets) / (len(log_rets) - 1)
            vol_24h[i] = math.sqrt(var)

    # Compute high-vol cutoff (top quantile across the whole window).
    vols_nonzero = sorted(v for v in vol_24h if v > 0)
    if vols_nonzero:
        cutoff_idx = int(high_vol_quantile * len(vols_nonzero))
        cutoff_idx = min(max(cutoff_idx, 0), len(vols_nonzero) - 1)
        vol_cutoff = vols_nonzero[cutoff_idx]
    else:
        vol_cutoff = float("inf")

    def _bar_idx_for(ft: datetime) -> int:
        # bisect-right on times for the latest bar at-or-before ft.
        lo, hi = 0, len(times)
        while lo < hi:
            mid = (lo + hi) // 2
            if times[mid] <= ft:
                lo = mid + 1
            else:
                hi = mid
        return max(0, lo - 1)

    out: list[str] = []
    for ft in funding_times:
        idx = _bar_idx_for(ft)
        v = vol_24h[idx]
        ret = returns_24h[idx]
        if v >= vol_cutoff and v > 0:
            out.append("high_volatility")
        elif ret > 0.005:
            out.append("trending_up")
        elif ret < -0.005:
            out.append("trending_down")
        else:
            out.append("mean_reverting")
    return out


__all__ = [
    "ENTER_THRESHOLD",
    "EXIT_THRESHOLD",
    "FUNDING_PERIODS_PER_DAY",
    "FUNDING_PERIODS_PER_YEAR",
    "FUNDING_PERIOD_HOURS",
    "FundingRateHarvest",
    "FundingTick",
    "HARVEST_REGIMES",
    "HarvestPosition",
    "HarvestTrade",
    "MIN_HOLD_PERIODS",
    "ROUND_TRIP_FEE_BPS",
    "ROUND_TRIP_FEE_PCT",
    "TAKER_FEE_BPS_PER_LEG",
    "simulate_harvest",
    "synthetic_regimes_from_spot",
]
