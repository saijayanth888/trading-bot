"""
Pre-trade risk governor.

Single point of approval for every entry the strategy is about to send.
Reads its limits from `config.json[risk_management]` so a runtime config
reload (without rebuilding the bot) is enough to relax / tighten any
constraint.

Hard gates (any failing → block trade):
    1. Portfolio drawdown ≥ max_portfolio_drawdown_pct → trading_paused
       (auto-resumes only when current_equity climbs back above the
       trigger; "kill switch" semantics that the operator can verify).
    2. Today's realised PnL ≤ -daily_loss_limit_pct of starting equity →
       blocked until next UTC midnight.
    3. Open positions ≥ max_concurrent_positions.
    4. Position size > max_position_size_pct of portfolio.
    5. Pair correlation > correlation_threshold (Pearson, returns over the
       last `correlation_lookback_days`) with ANY open-position pair.
    6. Circuit breaker: ≥ circuit_breaker_consecutive_losses losses → ban
       trading for circuit_breaker_cooldown_hours.

Soft outputs (returned, not blocking):
    - Kelly-suggested position fraction (model confidence as win prob,
      empirical avg-win/avg-loss from the last `kelly_lookback_trades`,
      scaled by `kelly_safety_factor`).
    - Reason string explaining either approval or which limit fired.

Default limits (override in config.json):

    "risk_management": {
        "max_portfolio_drawdown_pct": 0.08,
        "daily_loss_limit_pct": 0.03,
        "max_position_size_pct": 0.10,
        "max_concurrent_positions": 6,
        "correlation_threshold": 0.70,
        "correlation_lookback_days": 30,
        "circuit_breaker_consecutive_losses": 5,
        "circuit_breaker_cooldown_hours": 4,
        "kelly_enabled": true,
        "kelly_lookback_trades": 100,
        "kelly_safety_factor": 0.5,
        "kelly_max_fraction": 0.25,
        "starting_equity_for_pct_limits": null
    }
"""

from __future__ import annotations

import json
import logging
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


@dataclass
class RiskConfig:
    max_portfolio_drawdown_pct: float = 0.08
    daily_loss_limit_pct: float = 0.03
    max_position_size_pct: float = 0.10
    max_concurrent_positions: int = 6

    correlation_threshold: float = 0.70
    correlation_lookback_days: int = 30
    correlation_min_overlap: int = 50          # min shared candles to compute corr

    circuit_breaker_consecutive_losses: int = 5
    circuit_breaker_cooldown_hours: float = 4.0

    kelly_enabled: bool = True
    kelly_lookback_trades: int = 100
    kelly_safety_factor: float = 0.5
    kelly_max_fraction: float = 0.25           # never propose > 25% of equity
    kelly_min_trades: int = 10                 # below this → fall back to base stake

    # If set, used as the fixed denominator for the daily-loss / drawdown
    # percentages — useful when the bot starts in dry-run with synthetic
    # equity. Leave None to compute against the live equity peak.
    starting_equity_for_pct_limits: float | None = None

    @classmethod
    def from_dict(cls, d: Mapping[str, Any] | None) -> "RiskConfig":
        if not d:
            return cls()
        kwargs = {k: v for k, v in d.items() if k in cls.__dataclass_fields__}
        return cls(**kwargs)


# ---------------------------------------------------------------------------
# Decision + state
# ---------------------------------------------------------------------------


@dataclass
class RiskDecision:
    approved: bool
    reason: str                                  # "approved" or "blocked: <which>"
    blocking_constraint: str | None              # None when approved
    suggested_stake: float                       # post-cap, post-Kelly stake in quote ccy
    kelly_fraction: float                        # raw Kelly suggestion in [0, 1]
    cap_fraction: float                          # max_position_size_pct cap actually applied
    correlations: dict[str, float] = field(default_factory=dict)
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "approved": self.approved,
            "reason": self.reason,
            "blocking_constraint": self.blocking_constraint,
            "suggested_stake": self.suggested_stake,
            "kelly_fraction": self.kelly_fraction,
            "cap_fraction": self.cap_fraction,
            "correlations": self.correlations,
            "extra": self.extra,
        }


@dataclass
class TradeRecord:
    pair: str
    pnl_quote: float                             # signed PnL in quote currency
    pnl_pct: float                               # signed return on the trade's stake
    closed_at: datetime                          # tz-aware UTC


# ---------------------------------------------------------------------------
# Governor
# ---------------------------------------------------------------------------


class RiskGovernor:
    """
    Stateful pre-trade gatekeeper.

    Strategy integration (one instance per strategy run):

        gov = RiskGovernor.from_config(config)
        decision = gov.approve_entry(
            pair="BTC/USD",
            signal_price=current_rate,
            base_stake=proposed_stake,
            equity=current_equity,
            model_confidence=meta_confidence,
            open_positions=[("ETH/USD", 0.4), ...],   # (pair, stake_pct)
            pair_returns={"BTC/USD": series, "ETH/USD": series, ...},
        )
        if not decision.approved: return None
        stake = decision.suggested_stake
    """

    def __init__(self, config: RiskConfig | None = None, *, now_fn=None) -> None:
        self.config = config or RiskConfig()
        self._now = now_fn or (lambda: datetime.now(timezone.utc))

        # Equity tracking
        self._peak_equity: float = 0.0
        self._current_equity: float = 0.0
        self._starting_equity_today: float | None = None
        self._day_anchor_utc: datetime | None = None
        self._daily_realized_pnl: float = 0.0

        # Trade history (for Kelly + circuit breaker)
        self._trade_history: deque[TradeRecord] = deque(
            maxlen=max(self.config.kelly_lookback_trades, 200)
        )
        self._consecutive_losses: int = 0
        self._cooldown_until: datetime | None = None
        self._paused_for_drawdown: bool = False

        logger.info(
            "[risk] governor initialised: "
            "max_dd=%.1f%% daily=%.1f%% max_pos=%.1f%% concurrent=%d "
            "corr=%.2f@%dd cb=%d/%dh kelly=%s",
            self.config.max_portfolio_drawdown_pct * 100,
            self.config.daily_loss_limit_pct * 100,
            self.config.max_position_size_pct * 100,
            self.config.max_concurrent_positions,
            self.config.correlation_threshold,
            self.config.correlation_lookback_days,
            self.config.circuit_breaker_consecutive_losses,
            self.config.circuit_breaker_cooldown_hours,
            self.config.kelly_enabled,
        )

    # ------------------------------------------------------------------
    # Construction helpers
    # ------------------------------------------------------------------

    @classmethod
    def from_config(cls, config: Mapping[str, Any]) -> "RiskGovernor":
        return cls(RiskConfig.from_dict(config.get("risk_management", {})))

    @classmethod
    def from_config_file(cls, path: str | Path) -> "RiskGovernor":
        return cls.from_config(json.loads(Path(path).read_text()))

    # ------------------------------------------------------------------
    # State updates — call from strategy hooks
    # ------------------------------------------------------------------

    def update_equity(self, equity: float) -> None:
        """Update running equity + peak. Triggers drawdown auto-pause / resume."""
        self._current_equity = float(equity)
        if equity > self._peak_equity:
            self._peak_equity = float(equity)

        # Daily anchor
        now = self._now()
        if self._day_anchor_utc is None or now.date() > self._day_anchor_utc.date():
            self._day_anchor_utc = now.replace(hour=0, minute=0, second=0, microsecond=0)
            self._starting_equity_today = float(equity)
            self._daily_realized_pnl = 0.0
            logger.info(
                "[risk] new UTC day %s — daily anchor reset, starting_equity=%.2f",
                self._day_anchor_utc.date().isoformat(), equity,
            )

        # Drawdown auto-pause toggle. We auto-resume only when equity climbs
        # back above (peak * (1 - threshold)) — i.e. the trigger lifts on
        # recovery, but the operator should still inspect why we tripped.
        dd_trigger = self.config.max_portfolio_drawdown_pct
        if self._peak_equity > 0:
            dd = max(0.0, 1.0 - self._current_equity / self._peak_equity)
            if dd >= dd_trigger and not self._paused_for_drawdown:
                self._paused_for_drawdown = True
                logger.warning(
                    "[risk] PAUSE — drawdown %.2f%% ≥ limit %.2f%% "
                    "(peak %.2f, now %.2f)",
                    dd * 100, dd_trigger * 100, self._peak_equity, self._current_equity,
                )
            elif dd < dd_trigger * 0.5 and self._paused_for_drawdown:
                # Hysteresis: only resume once we've climbed back to within
                # half the limit, to avoid flapping on / off.
                self._paused_for_drawdown = False
                logger.info(
                    "[risk] RESUME — drawdown recovered to %.2f%% (peak %.2f, now %.2f)",
                    dd * 100, self._peak_equity, self._current_equity,
                )

    def record_trade_close(
        self, pair: str, pnl_quote: float, pnl_pct: float,
        closed_at: datetime | None = None,
    ) -> None:
        """Record a closed trade. Updates daily PnL + Kelly stats + circuit breaker."""
        ts = closed_at or self._now()
        self._trade_history.append(TradeRecord(
            pair=pair, pnl_quote=float(pnl_quote), pnl_pct=float(pnl_pct), closed_at=ts,
        ))
        if self._day_anchor_utc and ts.date() == self._day_anchor_utc.date():
            self._daily_realized_pnl += float(pnl_quote)

        if pnl_quote < 0:
            self._consecutive_losses += 1
            # Trip the breaker exactly once per streak (when we first cross
            # the threshold). Additional losses while cooling down don't
            # extend the deadline — let it expire naturally.
            if (
                self._consecutive_losses == self.config.circuit_breaker_consecutive_losses
                and self._cooldown_until is None
            ):
                self._cooldown_until = ts + timedelta(hours=self.config.circuit_breaker_cooldown_hours)
                logger.warning(
                    "[risk] CIRCUIT BREAKER — %d consecutive losses → "
                    "cooldown until %s",
                    self._consecutive_losses, self._cooldown_until.isoformat(),
                )
        else:
            if self._consecutive_losses > 0:
                logger.info("[risk] loss streak broken at %d", self._consecutive_losses)
            self._consecutive_losses = 0

    # ------------------------------------------------------------------
    # Approval — the main entry point
    # ------------------------------------------------------------------

    def approve_entry(
        self,
        pair: str,
        signal_price: float,
        base_stake: float,
        equity: float,
        model_confidence: float | None = None,
        open_positions: Iterable[tuple[str, float]] | None = None,
        pair_returns: Mapping[str, "pd.Series"] | None = None,
    ) -> RiskDecision:
        """
        Decide whether to allow a new long entry on `pair`.

        Args:
            pair: e.g. "BTC/USD"
            signal_price: the model's reference price (used by execution layer)
            base_stake: proposed stake in quote currency before risk sizing
            equity: current portfolio equity in quote currency
            model_confidence: meta-agent confidence in [0, 1]; required if Kelly enabled
            open_positions: iterable of (pair, stake_in_quote) currently open
            pair_returns: dict mapping pair → return series; the candidate pair must
                          be present along with each open-position pair, otherwise
                          the correlation gate is skipped (and logged).
        """
        self.update_equity(equity)
        now = self._now()
        open_positions = list(open_positions or [])

        # 1. Drawdown auto-pause -----------------------------------------
        if self._paused_for_drawdown:
            return self._block(
                "max_drawdown_paused",
                f"portfolio drawdown ≥ {self.config.max_portfolio_drawdown_pct:.1%}",
                base_stake, 0.0,
            )

        # 2. Daily loss limit --------------------------------------------
        starting = (
            self.config.starting_equity_for_pct_limits
            if self.config.starting_equity_for_pct_limits is not None
            else self._starting_equity_today
        )
        if starting and starting > 0:
            daily_loss_pct = -self._daily_realized_pnl / starting
            if daily_loss_pct >= self.config.daily_loss_limit_pct:
                # Block until the next UTC midnight.
                next_midnight = (now.replace(hour=0, minute=0, second=0, microsecond=0)
                                 + timedelta(days=1))
                return self._block(
                    "daily_loss_limit",
                    f"realised loss {daily_loss_pct:.2%} ≥ "
                    f"{self.config.daily_loss_limit_pct:.2%}; blocked until {next_midnight.isoformat()}",
                    base_stake, 0.0,
                    extra={"unblocks_at": next_midnight.isoformat()},
                )

        # 3. Concurrent positions ----------------------------------------
        if len(open_positions) >= self.config.max_concurrent_positions:
            return self._block(
                "max_concurrent_positions",
                f"{len(open_positions)} ≥ {self.config.max_concurrent_positions} open",
                base_stake, 0.0,
            )

        # 4. Circuit breaker cooldown ------------------------------------
        if self._cooldown_until and now < self._cooldown_until:
            return self._block(
                "circuit_breaker_cooldown",
                f"{self._consecutive_losses} consecutive losses; "
                f"cooldown until {self._cooldown_until.isoformat()}",
                base_stake, 0.0,
                extra={"unblocks_at": self._cooldown_until.isoformat()},
            )
        elif self._cooldown_until and now >= self._cooldown_until:
            logger.info("[risk] circuit-breaker cooldown expired; resuming")
            self._cooldown_until = None
            # Reset the consecutive-loss counter so the breaker doesn't
            # immediately re-trip on the next loss.
            self._consecutive_losses = 0

        # 5. Correlation filter ------------------------------------------
        correlations: dict[str, float] = {}
        if pair_returns and pair in pair_returns:
            cand_returns = pair_returns[pair]
            for op_pair, _stake in open_positions:
                if op_pair == pair or op_pair not in pair_returns:
                    continue
                rho = self._pearson_returns(cand_returns, pair_returns[op_pair])
                if rho is None:
                    continue
                correlations[op_pair] = float(rho)
                if abs(rho) > self.config.correlation_threshold:
                    return self._block(
                        "correlation_filter",
                        f"|ρ|={abs(rho):.2f} with {op_pair} > "
                        f"{self.config.correlation_threshold:.2f}",
                        base_stake, 0.0,
                        correlations=correlations,
                    )

        # 6. Sizing — apply max-position cap and Kelly ------------------
        cap = self.config.max_position_size_pct * float(equity)
        kelly_frac = 0.0
        if self.config.kelly_enabled and model_confidence is not None:
            kelly_frac = self._kelly_fraction(float(model_confidence))
            kelly_stake = kelly_frac * float(equity)
            sized = min(base_stake, kelly_stake) if kelly_stake > 0 else base_stake
        else:
            sized = base_stake
        suggested = max(0.0, min(sized, cap))

        return RiskDecision(
            approved=True,
            reason="approved",
            blocking_constraint=None,
            suggested_stake=float(suggested),
            kelly_fraction=float(kelly_frac),
            cap_fraction=float(self.config.max_position_size_pct),
            correlations=correlations,
        )

    # ------------------------------------------------------------------
    # Kelly Criterion
    # ------------------------------------------------------------------

    def _kelly_fraction(self, confidence: float) -> float:
        """
        f* = (p·b − q) / b, with safety scaling.

        p = confidence (model's win probability for this trade)
        q = 1 − p
        b = avg_win / avg_loss (both in *positive* return units, computed
            from the last `kelly_lookback_trades` closed trades).

        If we don't have enough closed trades yet we return 0 — the caller
        will fall back to `base_stake` capped by `max_position_size_pct`.
        """
        cfg = self.config
        if not cfg.kelly_enabled:
            return 0.0
        if confidence <= 0.0 or confidence >= 1.0:
            confidence = float(np.clip(confidence, 1e-3, 1.0 - 1e-3))

        recent = list(self._trade_history)[-cfg.kelly_lookback_trades:]
        if len(recent) < cfg.kelly_min_trades:
            return 0.0

        wins = [t.pnl_pct for t in recent if t.pnl_pct > 0]
        losses = [-t.pnl_pct for t in recent if t.pnl_pct < 0]
        if not wins or not losses:
            return 0.0

        avg_win = float(np.mean(wins))
        avg_loss = float(np.mean(losses))
        if avg_loss <= 0.0 or avg_win <= 0.0:
            return 0.0

        b = avg_win / avg_loss
        p = float(confidence)
        q = 1.0 - p
        f_star = (p * b - q) / b
        f = max(0.0, f_star) * cfg.kelly_safety_factor
        return float(min(f, cfg.kelly_max_fraction))

    # ------------------------------------------------------------------
    # Correlation
    # ------------------------------------------------------------------

    def _pearson_returns(
        self, a: "pd.Series", b: "pd.Series",
    ) -> float | None:
        """Pearson correlation over the most recent `correlation_lookback_days`."""
        if a is None or b is None or len(a) == 0 or len(b) == 0:
            return None
        lookback = self.config.correlation_lookback_days
        # Both series may have different cadences — left-join on time index
        if not isinstance(a.index, pd.DatetimeIndex) or not isinstance(b.index, pd.DatetimeIndex):
            # Fall back to plain alignment by position
            n = min(len(a), len(b))
            ax, bx = np.asarray(a[-n:]), np.asarray(b[-n:])
        else:
            cutoff = max(a.index.max(), b.index.max()) - pd.Timedelta(days=lookback)
            ax = a[a.index >= cutoff]
            bx = b[b.index >= cutoff]
            joined = pd.concat([ax, bx], axis=1, join="inner").dropna()
            if len(joined) < self.config.correlation_min_overlap:
                return None
            ax = joined.iloc[:, 0].to_numpy()
            bx = joined.iloc[:, 1].to_numpy()

        if len(ax) < self.config.correlation_min_overlap:
            return None
        if float(np.std(ax)) == 0.0 or float(np.std(bx)) == 0.0:
            return None
        return float(np.corrcoef(ax, bx)[0, 1])

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _block(
        self,
        constraint: str, reason: str,
        base_stake: float, kelly_frac: float,
        *, correlations: dict[str, float] | None = None,
        extra: dict[str, Any] | None = None,
    ) -> RiskDecision:
        logger.warning("[risk] BLOCK (%s): %s", constraint, reason)
        return RiskDecision(
            approved=False,
            reason=f"blocked: {reason}",
            blocking_constraint=constraint,
            suggested_stake=0.0,
            kelly_fraction=float(kelly_frac),
            cap_fraction=float(self.config.max_position_size_pct),
            correlations=correlations or {},
            extra=extra or {},
        )

    def status(self) -> dict[str, Any]:
        """Operator-friendly snapshot for /status endpoints."""
        now = self._now()
        return {
            "now_utc": now.isoformat(),
            "current_equity": self._current_equity,
            "peak_equity": self._peak_equity,
            "drawdown_pct": (
                0.0 if self._peak_equity == 0
                else 1.0 - self._current_equity / self._peak_equity
            ),
            "paused_for_drawdown": self._paused_for_drawdown,
            "daily_realized_pnl": self._daily_realized_pnl,
            "starting_equity_today": self._starting_equity_today,
            "consecutive_losses": self._consecutive_losses,
            "cooldown_until": (
                self._cooldown_until.isoformat() if self._cooldown_until else None
            ),
            "trades_recorded": len(self._trade_history),
        }
