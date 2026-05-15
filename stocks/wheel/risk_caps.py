"""
Runtime-derived wheel risk caps as % of portfolio equity.

Pre-2026-05-14 the wheel module used three static-dollar ceilings:

    max_total_collateral_usd  = $5,000   (10% of the pilot $50k account)
    max_risk_per_ticker_usd   = $1,700   ( 3.4% of $50k — one SOFI contract)
    kill_loss_per_cycle_usd   = $500     ( 1.0% of $50k)

Those dollar numbers were sized for a $50k pilot account. The stagnant-
config audit flagged them as classic anti-pattern: if the live account
grows to $100k the wheel keeps allocating the same $5k notional (now a
timid 5% of equity), and if the paper account is reset to $25k the wheel
happily risks the same $5k (now an aggressive 20% of equity). Either way
the operator has to remember to nudge env vars — exactly the kind of
manual book-keeping that drifts and rots.

This module replaces those static caps with runtime-derived values:

    derive_caps(portfolio_value, cfg)  →  EquityRiskCaps
      max_total_collateral_usd = min(0.100 * pv, cfg.max_total_collateral_usd)
      max_risk_per_ticker_usd  = min(0.034 * pv, cfg.max_risk_per_ticker_usd)
      kill_loss_per_cycle_usd  = min(0.010 * pv, cfg.kill_loss_per_cycle_usd)

The cfg dollar fields BECOME defensive ceilings (operator-pinned via
WHEEL_* env vars). The dynamic arm tightens the cap when equity shrinks
but never relaxes above the pinned ceiling — relaxing requires an
explicit env-var bump, which is the appropriate friction.

At the pilot $50k both arms tie and behavior is unchanged. At smaller
accounts the wheel auto-tightens; at larger accounts the operator must
opt in to a bigger ceiling.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass


def _pct_env(
    key: str,
    default: float,
    lo: float = 0.001,
    hi: float = 0.50,
) -> float:
    """Read an env override for a PCT_* constant; clamp to [lo, hi].

    Fail-safe: any unparseable, out-of-range, or empty value falls back to
    the compiled default with a WARNING log. Prevents fat-finger footguns
    like ``WHEEL_PCT_TOTAL_COLLATERAL=2.5`` (250 % deployment — would
    obliterate the wheel) from silently sticking.
    """
    raw = os.environ.get(key)
    if raw is None or not raw.strip():
        return default
    try:
        val = float(raw)
    except (TypeError, ValueError):
        logger.warning(
            "wheel.risk_caps: env %s=%r not a float — using compiled default %s",
            key, raw, default,
        )
        return default
    if not (lo <= val <= hi):
        logger.warning(
            "wheel.risk_caps: env %s=%s outside safe range [%s, %s] — using default %s",
            key, val, lo, hi, default,
        )
        return default
    return val


# Equity-fraction policy. Defaults updated 2026-05-15 to "Config A" per
# audit/2026-05-15-wheel-sizing-research.md after the discovery that the
# previous values (0.100 / 0.034) were $50k-pilot artifacts blocking every
# trade today on a $100k account. New defaults are literature-backed:
# - 25% total deployment (was 10%); spintwig 17-yr SPY backtest + tastytrade
#   target 70-80% but Config A is the conservative on-ramp.
# - 10% per-ticker (was 3.4%); arxiv 2508.16598 quarter-Kelly midpoint,
#   options.cafe + quantwheel.com practitioner consensus.
# - 1% kill_loss/cycle preserved — emergency stop, not a strategy knob.
#
# Each constant is env-overridable via WHEEL_PCT_* without a code change
# so future tuning (Config B after 4-week paper validation: 40% / 15%)
# is a .env edit, not a recompile.
PCT_TOTAL_COLLATERAL: float = _pct_env("WHEEL_PCT_TOTAL_COLLATERAL", 0.250)
PCT_RISK_PER_TICKER: float = _pct_env("WHEEL_PCT_RISK_PER_TICKER", 0.100)
PCT_KILL_LOSS_PER_CYCLE: float = _pct_env("WHEEL_PCT_KILL_LOSS_PER_CYCLE", 0.010)


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class EquityRiskCaps:
    """Per-cycle derived caps — computed once at the top of sell_csps()."""
    max_total_collateral_usd: float
    max_risk_per_ticker_usd: float
    kill_loss_per_cycle_usd: float
    portfolio_value: float          # the input pv (kept for logging)
    pinned: tuple[str, ...]         # which fields hit the cfg ceiling


def derive_caps(portfolio_value: float, cfg) -> EquityRiskCaps:
    """Compute equity-relative wheel caps.

    Each cap = min(equity_pct * portfolio_value, cfg_ceiling). The cfg
    ceiling holds when equity is large; the equity_pct arm tightens when
    equity is small.

    If ``portfolio_value <= 0`` (bad broker snapshot), all three caps
    fall back to the cfg ceilings — defensive, never blocks on a bad
    read; the existing buying-power gate will still refuse a real
    submit if the account is actually empty.
    """
    if portfolio_value is None or portfolio_value <= 0:
        logger.warning(
            "wheel: portfolio_value=%s — falling back to cfg ceilings for risk caps",
            portfolio_value,
        )
        return EquityRiskCaps(
            max_total_collateral_usd=float(cfg.max_total_collateral_usd),
            max_risk_per_ticker_usd=float(cfg.max_risk_per_ticker_usd),
            kill_loss_per_cycle_usd=float(cfg.kill_loss_per_cycle_usd),
            portfolio_value=float(portfolio_value or 0.0),
            pinned=("max_total_collateral_usd", "max_risk_per_ticker_usd",
                    "kill_loss_per_cycle_usd"),
        )

    pv = float(portfolio_value)
    # Round to cents before comparing so float-precision noise like
    # 0.034 * 50_000 = 1700.0000000000002 doesn't spuriously "pin" a
    # cap that mathematically equals the cfg ceiling.
    total_eq = round(PCT_TOTAL_COLLATERAL * pv, 2)
    ticker_eq = round(PCT_RISK_PER_TICKER * pv, 2)
    kill_eq = round(PCT_KILL_LOSS_PER_CYCLE * pv, 2)
    total_ceil = round(float(cfg.max_total_collateral_usd), 2)
    ticker_ceil = round(float(cfg.max_risk_per_ticker_usd), 2)
    kill_ceil = round(float(cfg.kill_loss_per_cycle_usd), 2)

    total_cap = min(total_eq, total_ceil)
    ticker_cap = min(ticker_eq, ticker_ceil)
    kill_cap = min(kill_eq, kill_ceil)

    # "Pinned" = the cfg ceiling strictly won (would have been larger
    # if not for the ceiling). Ties don't count.
    pinned: list[str] = []
    if total_ceil < total_eq:
        pinned.append("max_total_collateral_usd")
    if ticker_ceil < ticker_eq:
        pinned.append("max_risk_per_ticker_usd")
    if kill_ceil < kill_eq:
        pinned.append("kill_loss_per_cycle_usd")

    return EquityRiskCaps(
        max_total_collateral_usd=total_cap,
        max_risk_per_ticker_usd=ticker_cap,
        kill_loss_per_cycle_usd=kill_cap,
        portfolio_value=round(pv, 2),
        pinned=tuple(pinned),
    )


def caps_as_dict(caps: EquityRiskCaps) -> dict:
    """Plain-dict form for JSON serialization in summary/status output."""
    return {
        "portfolio_value": caps.portfolio_value,
        "max_total_collateral_usd": caps.max_total_collateral_usd,
        "max_risk_per_ticker_usd": caps.max_risk_per_ticker_usd,
        "kill_loss_per_cycle_usd": caps.kill_loss_per_cycle_usd,
        "pinned_to_cfg_ceiling": list(caps.pinned),
    }


__all__ = [
    "PCT_TOTAL_COLLATERAL",
    "PCT_RISK_PER_TICKER",
    "PCT_KILL_LOSS_PER_CYCLE",
    "EquityRiskCaps",
    "derive_caps",
    "caps_as_dict",
]
