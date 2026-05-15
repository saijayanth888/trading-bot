"""
MonitoringMixin — Slack alerts + trade journal + InfluxDB metrics for the
FreqAI strategy.

The strategy used to carry ~250 lines of monitoring + alerting plumbing
(SlackAlerter / TradeJournal / MetricsWriter wiring, dedup, risk-threshold
latching, hourly snapshots, daily summary scheduling). This mixin holds all
of that so the strategy can stay focused on signal logic.

Mix into an IStrategy subclass and call ``self._init_monitoring(self.config)``
from ``bot_start()``. The mixin degrades gracefully — if any of the optional
modules (slack_alerts / trade_journal / metrics_writer) fail to import or
initialise, every public method is a safe no-op.

Public surface (the only methods the strategy should call):

    _init_monitoring(config)
        Instantiate SlackAlerter / TradeJournal / MetricsWriter.

    _send_risk_alert(gov)
        Fire warning at 5% drawdown, critical at 8%; reset latches at <3%.

    _record_trade_entry(*, pair, side, rate, stake, confidence, latest, entry_tag)
        Slack entry alert + journal log_entry + Influx regime/sentiment.

    _record_trade_exit(t, *, gov=None) -> bool
        Idempotent: returns True the first time it sees a trade id, False on
        subsequent calls. Drives Slack exit alert + journal log_exit + Influx
        write_trade. If ``gov`` is given, also calls gov.record_trade_close()
        — risk and monitoring share the same once-per-trade gate.

    _maybe_write_hourly_snapshot(now, equity, gov)
        Write the once-per-hour metrics snapshot.

    _maybe_send_daily_summary(now, gov)
        Send the once-per-UTC-day summary at the 00:00–01:00 hour.

All public methods catch exceptions internally — monitoring failures never
crash the trading loop.

Convention: every drawdown / pnl ``_pct`` value flowing through this mixin
is a **fraction** (e.g. ``0.05`` = 5%). ``risk_governor.status()`` returns
``drawdown_pct`` as ``1.0 - current/peak`` (fractional); the Slack
formatter ``slack_alerts._fmt_pct`` consumes fractions via ``f"{x:.2%}"``.
The 5% / 8% / 3% thresholds in ``_send_risk_alert`` are likewise
fractional. Do not multiply by 100 anywhere in this chain.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

try:
    from modules.metrics_writer import MetricsWriter
    from modules.slack_alerts import SlackAlerter
    from modules.trade_journal import TradeJournal
    _MONITOR_AVAILABLE = True
except Exception as exc:
    logger.warning("monitoring modules unavailable: %s", exc)
    SlackAlerter = None
    TradeJournal = None
    MetricsWriter = None
    _MONITOR_AVAILABLE = False


class MonitoringMixin:
    """See module docstring."""

    # All state owned by the mixin. Class-level defaults are fine for the
    # freqtrade single-instance-per-process pattern; _init_monitoring binds
    # them as instance attrs for safety.
    _slack: Any = None
    _journal: Any = None
    _metrics: Any = None
    _recorded_closed_trades: set = set()
    _journal_id_by_trade: dict = {}
    _last_daily_summary_date: str | None = None
    _risk_alert_state: dict = {}
    _last_metric_hour: str | None = None

    # ------------------------------------------------------------------
    # Initialisation — called from bot_start()
    # ------------------------------------------------------------------

    def _init_monitoring(self, config: dict) -> None:
        # Reset to instance scope so multiple strategy instances in the same
        # interpreter (rare, but happens in tests) don't share dedup state.
        self._slack = None
        self._journal = None
        self._metrics = None
        self._recorded_closed_trades = set()
        self._journal_id_by_trade = {}
        self._last_daily_summary_date = None
        self._risk_alert_state = {}
        self._last_metric_hour = None

        if not _MONITOR_AVAILABLE:
            return

        # SlackAlerter (+ a one-shot boot ping so the operator sees the wiring
        # is alive before the first trade arrives).
        try:
            self._slack = SlackAlerter.from_env()
            if self._slack and self._slack.enabled:
                logger.info("[monitoring] slack alerts enabled")
                try:
                    mode = "DRY-RUN (paper)" if config.get("dry_run", True) else "LIVE"
                    ratio = config.get("tradable_balance_ratio", 1.0)
                    self._slack.notify_info(
                        component="bot_start",
                        message=f"FreqAIMeanRevV1 booted — mode={mode}, "
                                f"tradable_balance_ratio={ratio}, "
                                f"strategy={type(self).__name__}",
                        context={"timeframe": getattr(self, "timeframe", "?"),
                                 "max_open_trades": config.get("max_open_trades")},
                    )
                except Exception:
                    pass
        except Exception as exc:
            logger.warning("[monitoring] slack init failed: %s", exc)
            self._slack = None

        try:
            self._journal = TradeJournal()
            logger.info("[monitoring] trade journal opened")
        except Exception as exc:
            logger.warning("[monitoring] trade journal init failed: %s", exc)
            self._journal = None

        try:
            self._metrics = MetricsWriter()
            if self._metrics and self._metrics.enabled:
                # DEPRECATED 2026-05-13: influxdb retired; this branch only
                # fires when an operator explicitly sets INFLUX_ENABLED=1.
                logger.info("[monitoring] influx metrics writer enabled")
        except Exception as exc:
            logger.warning("[monitoring] metrics writer init failed: %s", exc)
            self._metrics = None

    # ------------------------------------------------------------------
    # Risk threshold alerts
    # ------------------------------------------------------------------

    def _send_risk_alert(self, gov) -> None:
        """Drawdown threshold alerts, latched so we don't spam.

        Warning at 5%, critical at 8%; both latches reset once drawdown
        recovers below 3%. ``gov`` is the strategy's risk_governor instance;
        if it's None or Slack is unconfigured, this is a no-op.
        """
        if gov is None or self._slack is None:
            return
        try:
            st = gov.status()
            dd = float(st.get("drawdown_pct", 0.0) or 0.0)
            if dd >= 0.08 and self._risk_alert_state.get("dd_critical") is not True:
                self._slack.notify_risk_critical("portfolio_drawdown", dd, 0.08)
                self._risk_alert_state["dd_critical"] = True
            elif dd >= 0.05 and self._risk_alert_state.get("dd_warning") is not True:
                self._slack.notify_risk_warning("portfolio_drawdown", dd, 0.05)
                self._risk_alert_state["dd_warning"] = True
            if dd < 0.03:
                self._risk_alert_state.pop("dd_warning", None)
                self._risk_alert_state.pop("dd_critical", None)
        except Exception as exc:
            logger.debug("risk alert check failed: %s", exc)

    # ------------------------------------------------------------------
    # Trade entry / exit recording
    # ------------------------------------------------------------------

    def _record_trade_entry(
        self,
        *,
        pair: str,
        side: str,
        rate: float,
        stake: float,
        confidence: float | None,
        latest: dict,
        entry_tag: str | None,
    ) -> None:
        """Slack entry alert + journal log_entry + Influx regime/sentiment write.

        ``latest`` is the dict the strategy builds via ``_latest_signals_for(pair)``
        with keys: tft_probs, drl_votes, regime, sentiment_score,
        sentiment_confidence, features_used, reasoning.
        """
        if self._slack is not None:
            try:
                self._slack.notify_trade_entry(
                    pair=pair, signal=str(side or "long"),
                    entry_price=float(rate), stake=stake,
                    confidence=float(confidence or 0.0),
                    tft_probs=latest.get("tft_probs"),
                    drl_votes=latest.get("drl_votes"),
                    regime=latest.get("regime"),
                    entry_tag=str(entry_tag or ""),
                )
            except Exception as exc:
                logger.debug("slack entry notify failed: %s", exc)

        if self._journal is not None:
            try:
                jid = self._journal.log_entry(
                    pair=pair, direction=str(side or "long"),
                    entry_price=float(rate), stake=stake,
                    confidence=confidence,
                    tft_probs=latest.get("tft_probs"),
                    drl_votes=latest.get("drl_votes"),
                    sentiment_score=latest.get("sentiment_score"),
                    sentiment_confidence=latest.get("sentiment_confidence"),
                    regime=latest.get("regime"),
                    features_used=latest.get("features_used"),
                    reasoning=latest.get("reasoning"),
                    external_id=None,   # set on close via pair+price match
                )
                # Stash on a marker the exit-side code can correlate with.
                self._journal_id_by_trade[f"{pair}@{float(rate):.10g}"] = jid
            except Exception as exc:
                logger.debug("journal entry failed: %s", exc)

        if self._metrics is not None:
            try:
                rg = latest.get("regime")
                if rg:
                    self._metrics.write_regime(pair=pair, label=rg)
                if latest.get("sentiment_score") is not None:
                    self._metrics.write_sentiment(
                        pair=pair,
                        score=float(latest.get("sentiment_score") or 0.0),
                        confidence=float(latest.get("sentiment_confidence") or 0.0),
                        price=float(rate),
                    )
            except Exception as exc:
                logger.debug("metrics on entry failed: %s", exc)

    def _record_trade_exit(self, t, *, gov: Any = None) -> bool:
        """Once-per-trade exit recorder.

        Returns True the first time it sees ``t.id``, False thereafter (the
        caller can use this to skip the rest of its per-trade loop body).

        Drives:
          - gov.record_trade_close() if ``gov`` is given (risk shares the
            once-per-trade gate)
          - SlackAlerter.notify_trade_exit
          - TradeJournal.log_exit (matches via the marker stashed by
            _record_trade_entry, falls back to find_open_by_external_id)
          - MetricsWriter.write_trade
        """
        tid = getattr(t, "id", None)
        if tid is None or tid in self._recorded_closed_trades:
            return False
        self._recorded_closed_trades.add(tid)

        pair = str(getattr(t, "pair", ""))
        pnl_quote = float(getattr(t, "close_profit_abs", 0.0) or 0.0)
        pnl_pct = float(getattr(t, "close_profit", 0.0) or 0.0)
        close_date = getattr(t, "close_date_utc", None) or getattr(t, "close_date", None)
        entry_price = float(getattr(t, "open_rate", 0.0) or 0.0)
        exit_price = float(getattr(t, "close_rate", 0.0) or 0.0)
        exit_reason = str(getattr(t, "exit_reason", "") or "")
        duration_min = 0.0
        try:
            td = getattr(t, "trade_duration", None)
            if td is not None:
                duration_min = float(td) / 60.0
        except Exception:
            pass

        if gov is not None:
            try:
                gov.record_trade_close(pair, pnl_quote, pnl_pct, close_date)
            except Exception as exc:
                logger.debug("record_trade_close failed for %s: %s", pair, exc)

        if self._slack is not None:
            try:
                self._slack.notify_trade_exit(
                    pair=pair, entry_price=entry_price, exit_price=exit_price,
                    pnl=pnl_quote, pnl_pct=pnl_pct,
                    exit_reason=exit_reason, duration_minutes=duration_min,
                )
            except Exception as exc:
                logger.debug("slack exit notify failed: %s", exc)

        if self._journal is not None:
            try:
                # Correlation key resolution, in priority order:
                #   1. pair@rate marker stashed by _record_trade_entry — works
                #      within a single freqtrade lifetime.
                #   2. find_open_by_pair_and_price — restart-safe DB lookup,
                #      matches the latest still-open journal row by pair + a
                #      0.1% entry-price band. This is the path that fires
                #      after a restart (in-memory dict is empty).
                #   3. external_id fallback — legacy path; entries currently
                #      don't set external_id so this rarely matches.
                jid = self._journal_id_by_trade.pop(
                    f"{pair}@{float(entry_price):.10g}", None,
                )
                if jid is None:
                    jid = self._journal_id_by_trade.pop(str(tid), None)
                if jid is None:
                    jid = self._journal.find_open_by_pair_and_price(pair, entry_price)
                if jid is None:
                    jid = self._journal.find_open_by_external_id(str(tid))
                if jid is not None:
                    self._journal.log_exit(
                        jid, exit_price=exit_price, pnl=pnl_quote, pnl_pct=pnl_pct,
                        exit_reason=exit_reason, duration_min=duration_min,
                        closed_at=close_date,
                    )
                else:
                    logger.warning(
                        "[journal] no matching open row for trade %s %s @ %.4f — "
                        "close-side update skipped",
                        tid, pair, entry_price,
                    )
            except Exception as exc:
                logger.debug("journal exit failed: %s", exc)

        if self._metrics is not None:
            try:
                self._metrics.write_trade(
                    pair=pair, side="long",
                    pnl=pnl_quote, pnl_pct=pnl_pct,
                    duration_min=duration_min, ts=close_date,
                )
            except Exception as exc:
                logger.debug("metrics trade failed: %s", exc)

        return True

    # ------------------------------------------------------------------
    # Periodic snapshots / summaries
    # ------------------------------------------------------------------

    def _maybe_write_hourly_snapshot(self, now, equity: float, gov) -> None:
        """Once-per-hour Influx snapshot of equity / DD / cumulative P&L."""
        if self._metrics is None or not getattr(self._metrics, "enabled", False):
            return
        try:
            hour_key = now.strftime("%Y-%m-%dT%H")
        except Exception:
            return
        if self._last_metric_hour == hour_key:
            return
        self._last_metric_hour = hour_key
        try:
            stats = self._journal.stats() if self._journal is not None else {}
            cumulative = float(stats.get("total_pnl", 0.0))
            n = int(stats.get("trades", 0))
            win_rate = float(stats.get("win_rate", 0.0)) if n > 0 else None
            st = gov.status() if gov is not None else {}
            self._metrics.write_hourly_snapshot(
                equity=float(equity),
                peak_equity=float(st.get("peak_equity", equity)),
                drawdown=float(st.get("drawdown_pct", 0.0)),
                daily_pnl=float(st.get("daily_realized_pnl", 0.0)),
                cumulative_pnl=cumulative,
                win_rate_30d=win_rate, win_rate_n=n,
                ts=now,
            )
        except Exception as exc:
            logger.debug("hourly snapshot failed: %s", exc)

    def _maybe_send_daily_summary(self, now, gov) -> None:
        """Send once per UTC day, only between 00:00 and 01:00 UTC."""
        if self._slack is None or self._journal is None:
            return
        try:
            today = now.strftime("%Y-%m-%d")
        except Exception:
            return
        if self._last_daily_summary_date == today:
            return
        # Wait until at least one minute past midnight UTC so the previous
        # day's last trade has time to settle in the journal.
        if not (0 <= getattr(now, "hour", 0) <= 1):
            return
        from datetime import timedelta
        try:
            day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
            yesterday_start = day_start - timedelta(days=1)
            stats = self._journal.stats(start=yesterday_start, end=day_start)
            if stats.get("trades", 0) == 0:
                # Nothing to summarise; mark as sent so we don't keep checking.
                self._last_daily_summary_date = today
                return
            st = gov.status() if gov is not None else {}
            equity = float(st.get("current_equity", 0.0) or 0.0)
            self._slack.notify_daily_summary(
                date_utc=yesterday_start.strftime("%Y-%m-%d"),
                starting_equity=float(st.get("peak_equity", equity)),
                ending_equity=equity,
                total_pnl=float(stats.get("total_pnl", 0.0)),
                num_trades=int(stats.get("trades", 0)),
                wins=int(stats.get("wins", 0)),
                losses=int(stats.get("losses", 0)),
                max_drawdown=float(st.get("drawdown_pct", 0.0) or 0.0),
            )
            self._last_daily_summary_date = today
        except Exception as exc:
            logger.debug("daily summary failed: %s", exc)
