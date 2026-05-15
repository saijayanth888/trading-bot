"""
wheel.config — typed, validated settings for the wheel strategy.

All thresholds are operator-tunable via env vars (WHEEL_*) so a paper-mode
pilot can run different params from a live deployment without touching code.
Defaults are aligned with the alpacahq/options-wheel reference + research
findings (30-delta CSPs, 7-10 DTE, 50% profit-take).

Env vars (all optional; defaults below are the pilot values):
    WHEEL_SYMBOLS                  comma-separated tickers, default "SOFI"
    WHEEL_DELTA_MIN                short put min |delta|, default 0.25
    WHEEL_DELTA_MAX                short put max |delta|, default 0.35
    WHEEL_DTE_MIN                  min days-to-expiry, default 7
    WHEEL_DTE_MAX                  max days-to-expiry, default 10
    WHEEL_MIN_OI                   min open interest, default 500
    WHEEL_MIN_YIELD_PCT_WEEK       min weekly bid/strike, default 0.008
    WHEEL_MAX_RISK_PER_TICKER      max collateral USD per ticker, default 1700
    WHEEL_MAX_TOTAL_COLLATERAL     max total CSP collateral USD, default 5000
    WHEEL_PROFIT_TAKE_PCT          buy-to-close at this fraction of premium
                                   collected, default 0.50
    WHEEL_DELTA_ROLL_TRIGGER       roll the put when |delta| crosses, default 0.50
    WHEEL_KILL_LOSS_PER_CYCLE      cycle-level dollar loss to kill bot, default 500
    WHEEL_EARNINGS_BLACKOUT_DAYS   skip new CSPs within N days of earnings,
                                   default 3
    WHEEL_PAPER                    "true" / "false", default true (read from
                                   trading-bot/.env's TRADING_MODE)
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field


@dataclass(frozen=True)
class WheelConfig:
    # Universe — start single-ticker; expand only after 30-day pilot success.
    symbols: tuple[str, ...] = ("SOFI",)

    # Strike-selection knobs.
    delta_min: float = 0.25
    delta_max: float = 0.35
    dte_min: int = 7
    dte_max: int = 10
    min_open_interest: int = 500
    min_yield_per_week: float = 0.008  # bid/strike, e.g. 0.008 = 0.8%/wk

    # Capital limits.
    max_risk_per_ticker_usd: float = 1700.0  # 1 contract = 100 sh × $17 = $1700
    max_total_collateral_usd: float = 5000.0  # pilot cap

    # Lifecycle.
    profit_take_fraction: float = 0.50  # close at 50% of credit collected
    delta_roll_trigger: float = 0.50  # roll if short put delta exceeds this
    earnings_blackout_days: int = 3  # skip CSPs within N days of earnings

    # Risk killers.
    kill_loss_per_cycle_usd: float = 500.0  # walk away from a ticker for 90d

    # Mode.
    paper: bool = True

    # ── Regime gating ───────────────────────────────────────────────────
    # Per-SPY-regime tuning of CSP entry. Mirrors the crypto strategy's
    # regime_gating block (config.json) — keeps the wheel risk-aware
    # without per-trade discretionary decisions.
    #
    #   delta_max_shift   added to cfg.delta_max for this regime
    #                     (negative = tighter, further OTM, safer)
    #   block             hard-block new CSP entries in this regime
    #
    # Operator can override via WHEEL_REGIME_GATING env var (JSON string).
    # Default policy: skip CSPs in trending_down + high_volatility; loosen
    # slightly in trending_up; default in mean_reverting / unknown.
    regime_gating: dict = field(default_factory=lambda: {
        "trending_up":     {"delta_max_shift": +0.05, "block": False},
        "trending_down":   {"delta_max_shift":  0.00, "block": True},
        "high_volatility": {"delta_max_shift":  0.00, "block": True},
        "mean_reverting":  {"delta_max_shift":  0.00, "block": False},
        "unknown":         {"delta_max_shift":  0.00, "block": False},
    })

    def assert_valid(self) -> None:
        if not self.symbols:
            raise ValueError("WHEEL_SYMBOLS must contain at least one ticker")
        if not 0 < self.delta_min < self.delta_max < 1:
            raise ValueError(f"delta band invalid: [{self.delta_min}, {self.delta_max}]")
        if not 1 <= self.dte_min <= self.dte_max <= 60:
            raise ValueError(f"DTE band invalid: [{self.dte_min}, {self.dte_max}]")
        if self.min_yield_per_week <= 0:
            raise ValueError("min_yield_per_week must be > 0")
        if self.max_risk_per_ticker_usd <= 0 or self.max_total_collateral_usd <= 0:
            raise ValueError("collateral caps must be > 0")
        if not 0 < self.profit_take_fraction < 1:
            raise ValueError(
                f"profit_take_fraction must be in (0, 1): {self.profit_take_fraction}"
            )


def _env_str(key: str, default: str) -> str:
    return (os.environ.get(key) or default).strip()


def _env_float(key: str, default: float) -> float:
    raw = os.environ.get(key)
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _env_int(key: str, default: int) -> int:
    raw = os.environ.get(key)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _env_bool(key: str, default: bool) -> bool:
    raw = (os.environ.get(key) or "").strip().lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes", "y")


def _env_regime_gating() -> dict | None:
    """Optional JSON override for the regime_gating defaults.

        WHEEL_REGIME_GATING='{"trending_down": {"block": false}}'
    """
    import json as _json
    raw = (os.environ.get("WHEEL_REGIME_GATING") or "").strip()
    if not raw:
        return None
    try:
        parsed = _json.loads(raw)
        return parsed if isinstance(parsed, dict) else None
    except _json.JSONDecodeError:
        return None


def load_config() -> WheelConfig:
    """Build a WheelConfig from env vars + sane defaults."""
    symbols_raw = _env_str("WHEEL_SYMBOLS", "SOFI")
    symbols = tuple(s.strip().upper() for s in symbols_raw.split(",") if s.strip())

    # Merge env override (if any) on top of the dataclass default. Build a
    # WheelConfig with no override first to capture the field's factory
    # defaults, then deep-merge the override.
    rg_override = _env_regime_gating()
    rg_kwargs: dict = {}
    if rg_override is not None:
        merged = dict(WheelConfig().regime_gating)  # start from defaults
        for regime, policy in rg_override.items():
            base = dict(merged.get(regime, {}))
            base.update(policy if isinstance(policy, dict) else {})
            merged[regime] = base
        rg_kwargs = {"regime_gating": merged}

    cfg = WheelConfig(
        symbols=symbols,
        delta_min=_env_float("WHEEL_DELTA_MIN", 0.25),
        delta_max=_env_float("WHEEL_DELTA_MAX", 0.35),
        dte_min=_env_int("WHEEL_DTE_MIN", 7),
        dte_max=_env_int("WHEEL_DTE_MAX", 10),
        min_open_interest=_env_int("WHEEL_MIN_OI", 500),
        min_yield_per_week=_env_float("WHEEL_MIN_YIELD_PCT_WEEK", 0.008),
        max_risk_per_ticker_usd=_env_float("WHEEL_MAX_RISK_PER_TICKER", 1700.0),
        max_total_collateral_usd=_env_float("WHEEL_MAX_TOTAL_COLLATERAL", 5000.0),
        profit_take_fraction=_env_float("WHEEL_PROFIT_TAKE_PCT", 0.50),
        delta_roll_trigger=_env_float("WHEEL_DELTA_ROLL_TRIGGER", 0.50),
        earnings_blackout_days=_env_int("WHEEL_EARNINGS_BLACKOUT_DAYS", 3),
        kill_loss_per_cycle_usd=_env_float("WHEEL_KILL_LOSS_PER_CYCLE", 500.0),
        paper=_env_bool("WHEEL_PAPER", _env_str("TRADING_MODE", "paper") == "paper"),
        **rg_kwargs,
    )
    cfg.assert_valid()
    return cfg
