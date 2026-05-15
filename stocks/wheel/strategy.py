"""
wheel.strategy — pure-function helpers for filtering, scoring, and selecting
options contracts. Contains zero IO so it's trivially unit-testable.

Adapted from alpacahq/options-wheel/core/strategy.py with adjustments:
* delta band uses absolute value (puts are negative-delta, calls positive)
* yield filter is per-week instead of annualized — easier to reason about
  for a weekly cycle
* score still rewards higher premium, lower delta, shorter DTE
* added is_earnings_blackout() helper since the alpacahq template skips this
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Iterable

from .config import WheelConfig


@dataclass(frozen=True)
class OptionContract:
    """Subset of an Alpaca option-contract record we care about for scoring."""

    symbol: str  # the option symbol e.g. SOFI260516P00015000
    underlying: str  # e.g. SOFI
    strike: float
    expiry: date
    contract_type: str  # "put" or "call"
    delta: float  # negative for puts, positive for calls
    bid: float
    ask: float
    open_interest: int

    @property
    def mid(self) -> float:
        return (self.bid + self.ask) / 2.0 if self.bid and self.ask else 0.0

    @property
    def dte(self) -> int:
        return max(0, (self.expiry - date.today()).days)


def filter_puts(
    contracts: Iterable[OptionContract],
    cfg: WheelConfig,
    min_strike: float = 0.0,
) -> list[OptionContract]:
    """Apply delta-band, OI floor, weekly-yield floor, and DTE band filters."""
    out: list[OptionContract] = []
    for c in contracts:
        if c.contract_type != "put":
            continue
        if c.strike < min_strike:
            continue
        if not cfg.dte_min <= c.dte <= cfg.dte_max:
            continue
        delta_abs = abs(c.delta)
        if not cfg.delta_min <= delta_abs <= cfg.delta_max:
            continue
        if c.open_interest < cfg.min_open_interest:
            continue
        if c.bid <= 0 or c.strike <= 0:
            continue
        # Per-week yield = bid / strike  (bid because we're SELLING the put)
        # We extrapolate to weekly even on 7-DTE since DTE <= 10 is fine
        # to treat as one cycle.
        weekly_yield = c.bid / c.strike
        if weekly_yield < cfg.min_yield_per_week:
            continue
        out.append(c)
    return out


def filter_calls(
    contracts: Iterable[OptionContract],
    cfg: WheelConfig,
    cost_basis: float,
) -> list[OptionContract]:
    """Covered-call filter: short call must be ABOVE our cost basis (else we
    risk being called away at a loss)."""
    out: list[OptionContract] = []
    for c in contracts:
        if c.contract_type != "call":
            continue
        if c.strike < cost_basis:  # never sell a CC below your cost basis
            continue
        if not cfg.dte_min <= c.dte <= cfg.dte_max:
            continue
        if not cfg.delta_min <= abs(c.delta) <= cfg.delta_max:
            continue
        if c.open_interest < cfg.min_open_interest:
            continue
        if c.bid <= 0:
            continue
        out.append(c)
    return out


def score_contract(c: OptionContract) -> float:
    """Score for ranking: higher = better.

    Components:
        (1 - |delta|)        higher when assignment less likely
        (250 / (DTE + 5))    higher when shorter DTE — more capital churn
        (bid / strike)       higher when premium is fatter
    """
    return (
        (1 - abs(c.delta))
        * (250.0 / (c.dte + 5.0))
        * (c.bid / c.strike if c.strike else 0.0)
    )


def select_best(
    contracts: list[OptionContract],
    n: int | None = None,
) -> list[OptionContract]:
    """Sort by score descending, dedup by underlying, return top n (or all)."""
    if not contracts:
        return []
    scored: list[tuple[OptionContract, float]] = [
        (c, score_contract(c)) for c in contracts
    ]
    # Best per underlying
    best_per_underlying: dict[str, tuple[OptionContract, float]] = {}
    for c, s in scored:
        cur = best_per_underlying.get(c.underlying)
        if cur is None or s > cur[1]:
            best_per_underlying[c.underlying] = (c, s)
    ordered = sorted(best_per_underlying.values(), key=lambda x: x[1], reverse=True)
    if n is None:
        return [c for c, _ in ordered]
    return [c for c, _ in ordered[:n]]


def is_earnings_blackout(
    next_earnings: date | None,
    today: date | None = None,
    blackout_days: int = 3,
) -> bool:
    """True if next_earnings is within blackout_days of today.

    next_earnings == None → no earnings on file → not in blackout.
    """
    if next_earnings is None:
        return False
    today = today or date.today()
    delta = (next_earnings - today).days
    return 0 <= delta <= blackout_days


def profit_take_threshold(credit_received: float, cfg: WheelConfig) -> float:
    """The buy-to-close limit price at which we'd take profit.

    e.g. sold for $0.30 with 50% profit-take → close at $0.15.
    """
    return credit_received * (1.0 - cfg.profit_take_fraction)
