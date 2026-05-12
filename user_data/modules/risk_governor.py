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

import atexit
import json
import logging
import os
import tempfile
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Persistence (P0-G): daily anchor + drawdown-pause flag survive restart.
#
# Without this, a process restart mid-loss reset the anchor and re-armed
# the 3% daily-loss budget — compounding ~5.5% in a single UTC day was
# possible. The anchor file is a tiny JSON dict written atomically via
# tempfile + rename.
#
# Path can be overridden by RISK_GOVERNOR_ANCHORS_PATH for tests.
#
# Bug 2 (2026-05-12): backtest/hyperopt processes USED to read the live
# anchor file. A stale ``paused_for_drawdown: true`` would block every
# trade in the backtest, making the simulation a no-op while the live
# bot kept trading normally. Fix: backtest-class runmodes use a
# per-process transient anchor under /tmp that is auto-deleted on
# normal exit. Live / dry-run continue to use the persistent anchor.
# RISK_GOVERNOR_ANCHORS_PATH overrides BOTH for tests.
# ---------------------------------------------------------------------------

_DEFAULT_ANCHOR_PATH = (
    Path(__file__).resolve().parents[1] / "state" / "risk_governor_anchors.json"
)

# Freqtrade runmodes that should NOT touch the live anchor file. ``edge``
# is included because it walks the historical book the same way backtest
# does and would similarly pollute the live state if it persisted.
_BACKTEST_RUNMODES = frozenset({"backtest", "hyperopt", "edge"})


def _resolve_anchor_path(runmode: str | None = None) -> Path:
    """Resolve the anchor file path for the current run.

    Priority:
      1. ``RISK_GOVERNOR_ANCHORS_PATH`` env var (test-grade override; honoured
         in every mode so test fixtures keep working).
      2. ``/tmp/risk_governor_backtest_<pid>.json`` for backtest / hyperopt
         / edge runmodes — transient, per-process, auto-deleted on exit.
      3. ``user_data/state/risk_governor_anchors.json`` (default; live + dry).
    """
    override = os.environ.get("RISK_GOVERNOR_ANCHORS_PATH", "").strip()
    if override:
        return Path(override)
    if runmode and runmode.lower() in _BACKTEST_RUNMODES:
        return Path(tempfile.gettempdir()) / f"risk_governor_backtest_{os.getpid()}.json"
    return _DEFAULT_ANCHOR_PATH


# Back-compat shim: existing callers (and the test fixture) call
# ``_anchor_path()`` with no arguments. Route to the resolver with
# ``runmode=None`` so live behaviour is unchanged.
def _anchor_path(runmode: str | None = None) -> Path:
    return _resolve_anchor_path(runmode)


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

    def __init__(
        self,
        config: RiskConfig | None = None,
        *,
        now_fn=None,
        runmode: str | None = None,
    ) -> None:
        self.config = config or RiskConfig()
        self._now = now_fn or (lambda: datetime.now(timezone.utc))
        # Bug 2 (2026-05-12): in backtest/hyperopt/edge we MUST NOT touch
        # the live anchor file. Stash the runmode so every _anchor_path()
        # call here resolves to the transient /tmp path for this PID.
        # Normalised to lowercase string; None means "live/dry" default.
        self._runmode: str | None = (runmode.lower() if isinstance(runmode, str) else None)

        # If we're in a backtest-class runmode, schedule the transient
        # anchor for cleanup on normal interpreter exit. Best-effort; if
        # the process is killed hard the file is still safely confined
        # to /tmp and a reboot reclaims it.
        if self._runmode in _BACKTEST_RUNMODES:
            transient = _resolve_anchor_path(self._runmode)

            def _cleanup_transient_anchor(_p: Path = transient) -> None:
                try:
                    _p.unlink(missing_ok=True)
                except OSError:
                    pass

            atexit.register(_cleanup_transient_anchor)

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

        # P0-G: rehydrate daily anchor + drawdown-pause flag from disk.
        # If the file doesn't exist (first ever boot) we keep the in-memory
        # defaults and the next update_equity() call will persist them.
        self._load_anchors()

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
    # Anchor persistence (P0-G)
    # ------------------------------------------------------------------

    def _load_anchors(self) -> None:
        """Restore daily anchor + pause flag from the state file, if any."""
        path = _resolve_anchor_path(self._runmode)
        if not path.exists():
            return
        try:
            data = json.loads(path.read_text())
        except Exception as exc:
            logger.warning("[risk] could not parse anchors file %s: %s", path, exc)
            return

        try:
            anchor_iso = data.get("day_anchor_utc")
            if anchor_iso:
                anchor = datetime.fromisoformat(anchor_iso)
                # Tolerate naive timestamps in legacy files
                if anchor.tzinfo is None:
                    anchor = anchor.replace(tzinfo=timezone.utc)
                self._day_anchor_utc = anchor

            starting = data.get("starting_equity_today")
            if starting is not None:
                self._starting_equity_today = float(starting)

            realised = data.get("daily_realized_pnl")
            if realised is not None:
                self._daily_realized_pnl = float(realised)

            peak = data.get("peak_equity")
            if peak is not None:
                self._peak_equity = float(peak)

            # P0-H: the drawdown-pause flag MUST survive restart. Auto-resume
            # is gone; only a manual /api/ops/resume call should clear it.
            if data.get("paused_for_drawdown"):
                self._paused_for_drawdown = True

            logger.info(
                "[risk] anchors restored from %s: "
                "anchor=%s starting=%.2f realised_today=%.2f peak=%.2f paused=%s",
                path,
                self._day_anchor_utc.isoformat() if self._day_anchor_utc else None,
                self._starting_equity_today or 0.0,
                self._daily_realized_pnl, self._peak_equity,
                self._paused_for_drawdown,
            )
        except (TypeError, ValueError) as exc:
            logger.warning("[risk] malformed anchors file %s: %s", path, exc)

    def _persist_anchors(self) -> None:
        """Atomic write of the anchor state (tempfile + rename)."""
        path = _resolve_anchor_path(self._runmode)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            logger.warning("[risk] could not create state dir %s: %s", path.parent, exc)
            return

        payload = {
            "day_anchor_utc": (
                self._day_anchor_utc.isoformat() if self._day_anchor_utc else None
            ),
            "starting_equity_today": self._starting_equity_today,
            "daily_realized_pnl": self._daily_realized_pnl,
            "peak_equity": self._peak_equity,
            "paused_for_drawdown": self._paused_for_drawdown,
            "updated_at": self._now().isoformat(),
        }
        try:
            tmp = path.with_suffix(path.suffix + ".tmp")
            tmp.write_text(json.dumps(payload, indent=2))
            tmp.replace(path)
        except OSError as exc:
            logger.warning("[risk] anchors persist failed: %s", exc)

    def resume_after_manual_review(self, reason: str = "manual_resume") -> bool:
        """Operator-facing hook: clear the drawdown-pause flag (P0-H).

        Auto-resume on equity recovery was removed because it silently
        re-enabled trading without any operator visibility. Use this from
        /api/ops/resume (or an MCP tool) after a human has reviewed the
        drawdown event.
        """
        if not self._paused_for_drawdown:
            return False
        self._paused_for_drawdown = False
        self._persist_anchors()
        logger.warning("[risk] MANUAL RESUME — drawdown pause cleared (reason=%s)", reason)
        return True

    # ------------------------------------------------------------------
    # Construction helpers
    # ------------------------------------------------------------------

    @classmethod
    def from_config(cls, config: Mapping[str, Any]) -> "RiskGovernor":
        # Bug 2 (2026-05-12): extract freqtrade's runmode so the governor
        # writes to a transient anchor when invoked under backtest /
        # hyperopt / edge. The config["runmode"] value is normally a
        # freqtrade.enums.RunMode (has .value attr); fall back to str()
        # when it's already a plain string (test configs).
        rm_obj = config.get("runmode")
        runmode: str | None = None
        if rm_obj is not None:
            runmode = getattr(rm_obj, "value", None) or str(rm_obj)
        return cls(
            RiskConfig.from_dict(config.get("risk_management", {})),
            runmode=runmode,
        )

    @classmethod
    def from_config_file(cls, path: str | Path) -> "RiskGovernor":
        return cls.from_config(json.loads(Path(path).read_text()))

    # ------------------------------------------------------------------
    # State updates — call from strategy hooks
    # ------------------------------------------------------------------

    def update_equity(self, equity: float) -> None:
        """Update running equity + peak; trip drawdown pause on threshold.

        P0-G: persist daily anchor + pause flag every call so a restart
        in the middle of a 3% loss can't re-arm the budget.
        P0-H: auto-resume removed. Once paused, only ``resume_after_manual_review``
        (called by /api/ops/resume) clears the flag.
        """
        self._current_equity = float(equity)
        if equity > self._peak_equity:
            self._peak_equity = float(equity)

        # Daily anchor — roll over on UTC date change.
        now = self._now()
        if self._day_anchor_utc is None or now.date() > self._day_anchor_utc.date():
            self._day_anchor_utc = now.replace(hour=0, minute=0, second=0, microsecond=0)
            self._starting_equity_today = float(equity)
            self._daily_realized_pnl = 0.0
            logger.info(
                "[risk] new UTC day %s — daily anchor reset, starting_equity=%.2f",
                self._day_anchor_utc.date().isoformat(), equity,
            )

        # Drawdown pause — trip-only. No auto-resume; operator inspects + resumes.
        dd_trigger = self.config.max_portfolio_drawdown_pct
        if self._peak_equity > 0:
            dd = max(0.0, 1.0 - self._current_equity / self._peak_equity)
            if dd >= dd_trigger and not self._paused_for_drawdown:
                self._paused_for_drawdown = True
                logger.warning(
                    "[risk] PAUSE — drawdown %.2f%% ≥ limit %.2f%% "
                    "(peak %.2f, now %.2f). Manual /api/ops/resume required.",
                    dd * 100, dd_trigger * 100, self._peak_equity, self._current_equity,
                )

        # Persist after every update — cheap (<1 KB write) and the only way to
        # survive a mid-day restart without losing the anchor.
        self._persist_anchors()

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

        # Persist updated daily realised PnL so the daily-loss check survives
        # a restart between trade close and the next entry attempt.
        self._persist_anchors()

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
        open_unrealised_pnl: float = 0.0,
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
            open_unrealised_pnl: signed mark-to-market P&L of currently open
                                 positions in quote currency. P0-I: included
                                 in the daily-loss check so the bot can't keep
                                 opening trades while sitting on a big paper
                                 loss that the realised-only number misses.
        """
        self.update_equity(equity)
        now = self._now()
        open_positions = list(open_positions or [])

        # 1. Drawdown pause (manual-resume only after P0-H) --------------
        if self._paused_for_drawdown:
            return self._block(
                "max_drawdown_paused",
                f"portfolio drawdown ≥ {self.config.max_portfolio_drawdown_pct:.1%}",
                base_stake, 0.0,
            )

        # 2. Daily loss limit --------------------------------------------
        # P0-I: include unrealised P&L so a bot underwater on opens can't
        # keep stacking new trades while the realised number sits near 0.
        starting = (
            self.config.starting_equity_for_pct_limits
            if self.config.starting_equity_for_pct_limits is not None
            else self._starting_equity_today
        )
        if starting and starting > 0:
            combined_daily_pnl = self._daily_realized_pnl + float(open_unrealised_pnl)
            daily_loss_pct = -combined_daily_pnl / starting
            if daily_loss_pct >= self.config.daily_loss_limit_pct:
                # Block until the next UTC midnight.
                next_midnight = (now.replace(hour=0, minute=0, second=0, microsecond=0)
                                 + timedelta(days=1))
                return self._block(
                    "daily_loss_limit",
                    f"daily loss {daily_loss_pct:.2%} "
                    f"(realised={self._daily_realized_pnl:.2f}, "
                    f"unrealised={float(open_unrealised_pnl):.2f}) ≥ "
                    f"{self.config.daily_loss_limit_pct:.2%}; "
                    f"blocked until {next_midnight.isoformat()}",
                    base_stake, 0.0,
                    extra={
                        "unblocks_at": next_midnight.isoformat(),
                        "daily_realised_pnl": self._daily_realized_pnl,
                        "open_unrealised_pnl": float(open_unrealised_pnl),
                    },
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
        """Pearson correlation over the most recent `correlation_lookback_days`.

        Duplicate-timestamp safety (Bug 1, 2026-05-12): one 5m candle can
        produce two trades against the same pair (e.g. trailing-stop close
        + immediate re-entry stamped at the same minute), which gives the
        returns Series a non-unique DatetimeIndex. ``pd.concat(..., join="inner")``
        and any subsequent ``.reindex`` operation then raise
        ``InvalidIndexError: Reindexing only valid with uniquely valued
        Index objects``. We collapse duplicates BEFORE the join by keeping
        the LAST observation per timestamp — that mirrors the actual book
        state at candle close, which is what the correlation gate cares
        about.
        """
        if a is None or b is None or len(a) == 0 or len(b) == 0:
            return None
        lookback = self.config.correlation_lookback_days
        # Both series may have different cadences — left-join on time index
        if not isinstance(a.index, pd.DatetimeIndex) or not isinstance(b.index, pd.DatetimeIndex):
            # Fall back to plain alignment by position
            n = min(len(a), len(b))
            ax, bx = np.asarray(a[-n:]), np.asarray(b[-n:])
        else:
            # Deduplicate the index (keep last per timestamp). Cheap O(n)
            # mask — safer than groupby().last() because it preserves the
            # original Series dtype and tz.
            if not a.index.is_unique:
                a = a[~a.index.duplicated(keep="last")]
            if not b.index.is_unique:
                b = b[~b.index.duplicated(keep="last")]
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
