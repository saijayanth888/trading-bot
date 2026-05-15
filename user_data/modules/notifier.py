"""
Unified notifier — single interface that routes messages to the right
channel(s) based on severity. Used by every part of the system (crypto,
stocks, Hermes scripts).

Severity routing
  CRITICAL → Telegram + Slack (operator's phone buzzes immediately)
  WARNING  → Telegram + Slack (phone buzzes silently)
  TRADE    → Slack only
  REPORT   → Slack only
  INFO     → Slack only

Usage
    from modules.notifier import notify

    notify.critical("kill_switch", reason="DD>10%", actions={...})
    notify.warning("risk", metric="drawdown", value=0.06, threshold=0.05)
    notify.trade_entry(pair="BTC/USD", signal="long", entry_price=68000, ...)
    notify.daily_summary(...)
    notify.error("execution_engine", exc, context={"order_id": "..."})

Both alerters fail open — a Slack outage doesn't take down a trade. If
neither channel is configured, calls return {"slack": False, "telegram": False}
so the caller can log without crashing.
"""

from __future__ import annotations

import logging

from .slack_alerts import SlackAlerter
from .telegram_alerts import TelegramAlerter

logger = logging.getLogger(__name__)


class UnifiedNotifier:
    """One object to rule them all. Pure orchestration over Slack + Telegram."""

    def __init__(self):
        self.slack = SlackAlerter.from_env()
        self.telegram = TelegramAlerter.from_env()

    # ── CRITICAL — both channels, priority=True on Telegram ──────────

    def critical(self, kind: str, **kwargs) -> dict[str, bool]:
        results: dict[str, bool] = {}
        try:
            if kind == "kill_switch":
                results["slack"] = self.slack.notify_risk_critical(
                    metric=kwargs.get("reason", "kill_switch"),
                    value=float(kwargs.get("value") or 0),
                    threshold=float(kwargs.get("threshold") or 0),
                )
                results["telegram"] = self.telegram.notify_kill_switch(
                    reason=str(kwargs.get("reason", "unknown")),
                    actions=dict(kwargs.get("actions") or {}),
                )
            elif kind == "risk":
                results["slack"] = self.slack.notify_risk_critical(
                    metric=str(kwargs.get("metric", "?")),
                    value=float(kwargs.get("value") or 0),
                    threshold=float(kwargs.get("threshold") or 0),
                )
                results["telegram"] = self.telegram.notify_risk_critical(
                    metric=str(kwargs.get("metric", "?")),
                    value=float(kwargs.get("value") or 0),
                    threshold=float(kwargs.get("threshold") or 0),
                )
            elif kind == "ollama_down":
                fails = int(kwargs.get("consecutive_failures") or 0)
                results["slack"] = self.slack.notify_info(
                    component="ollama_unreachable",
                    message=str(kwargs.get("message")
                                or f"{fails} consecutive failures — Anthropic fallback active"),
                )
                results["telegram"] = self.telegram.notify_ollama_down(
                    consecutive_failures=fails,
                )
            elif kind == "flash_crash":
                # Slack via the generic info channel — flash crash isn't part
                # of the SlackAlerter built-in surface.
                pair = str(kwargs.get("pair", "?"))
                mag = float(kwargs.get("magnitude_pct") or 0)
                tf = str(kwargs.get("timeframe", "?"))
                results["slack"] = self.slack.notify_info(
                    component=f"flash_crash:{pair}",
                    message=f"Dropped {mag:.2f}% in {tf}. Positions auto-protected.",
                )
                results["telegram"] = self.telegram.notify_flash_crash(
                    pair=pair, magnitude_pct=mag, timeframe=tf,
                )
            else:
                # Unknown critical kind — fall through with title/message
                title = kwargs.get("title", kind)
                message = kwargs.get("message", str(kwargs))
                results["slack"] = self.slack.notify_info(component=title, message=message)
                results["telegram"] = self.telegram.notify_system_alert(title, message)
        except Exception as exc:
            logger.exception("notifier.critical(%s) failed: %s", kind, exc)
            results.setdefault("slack", False)
            results.setdefault("telegram", False)
        return results

    # ── WARNING — both channels, priority=False on Telegram ──────────

    def warning(self, kind: str, **kwargs) -> dict[str, bool]:
        results: dict[str, bool] = {}
        try:
            if kind == "risk":
                results["slack"] = self.slack.notify_risk_warning(
                    metric=str(kwargs.get("metric", "?")),
                    value=float(kwargs.get("value") or 0),
                    threshold=float(kwargs.get("threshold") or 0),
                )
                results["telegram"] = self.telegram.notify_risk_warning(
                    metric=str(kwargs.get("metric", "?")),
                    value=float(kwargs.get("value") or 0),
                    threshold=float(kwargs.get("threshold") or 0),
                )
            elif kind == "ollama_down":
                fails = int(kwargs.get("consecutive_failures") or 0)
                results["slack"] = self.slack.notify_info(
                    component="ollama_unhealthy",
                    message=f"{fails} consecutive failures. Failover to Anthropic active.",
                )
                results["telegram"] = self.telegram.notify_ollama_down(
                    consecutive_failures=fails,
                )
            elif kind == "failover":
                frm = str(kwargs.get("from_provider", "?"))
                to = str(kwargs.get("to_provider", "?"))
                reason = str(kwargs.get("reason", ""))
                results["slack"] = self.slack.notify_info(
                    component=f"llm_failover_{frm}_to_{to}",
                    message=reason,
                )
                results["telegram"] = self.telegram.notify_provider_failover(
                    from_provider=frm, to_provider=to, reason=reason,
                )
            else:
                title = kwargs.get("title", f"Warning: {kind}")
                message = kwargs.get("message", str(kwargs))
                results["slack"] = self.slack.notify_info(component=title, message=message)
                results["telegram"] = self.telegram.notify_system_alert(title, message)
        except Exception as exc:
            logger.exception("notifier.warning(%s) failed: %s", kind, exc)
            results.setdefault("slack", False)
            results.setdefault("telegram", False)
        return results

    # ── TRADE / REPORT / INFO — Slack only ───────────────────────────

    def trade_entry(self, **kwargs) -> bool:
        try:
            return self.slack.notify_trade_entry(**kwargs)
        except Exception as exc:
            logger.warning("notifier.trade_entry failed: %s", exc)
            return False

    def trade_exit(self, **kwargs) -> bool:
        try:
            return self.slack.notify_trade_exit(**kwargs)
        except Exception as exc:
            logger.warning("notifier.trade_exit failed: %s", exc)
            return False

    def daily_summary(self, **kwargs) -> bool:
        # Translate shark's daily_summary call shape (date / equity /
        # day_pnl_dollars / day_pnl_pct / open_positions / weekly_trades /
        # circuit_breaker) into SlackAlerter.notify_daily_summary's signature
        # (date_utc / starting_equity / ending_equity / total_pnl / num_trades /
        # wins / losses / sharpe_30d / max_drawdown). The freqtrade-era caller
        # in monitoring_mixin.py already uses the SlackAlerter signature
        # natively, so we only translate when shark-shape keys are present.
        try:
            if "date" in kwargs or "day_pnl_dollars" in kwargs:
                equity = float(kwargs.get("equity") or 0.0)
                day_pnl = float(kwargs.get("day_pnl_dollars") or 0.0)
                weekly_trades = int(kwargs.get("weekly_trades") or 0)
                kwargs = {
                    "date_utc": str(kwargs.get("date") or kwargs.get("date_utc") or ""),
                    "starting_equity": max(equity - day_pnl, 0.0),
                    "ending_equity": equity,
                    "total_pnl": day_pnl,
                    "num_trades": weekly_trades,
                    "wins": 0,  # shark tracks weekly trades, not W/L split here
                    "losses": 0,
                }
            return self.slack.notify_daily_summary(**kwargs)
        except Exception as exc:
            logger.warning("notifier.daily_summary failed: %s", exc)
            return False

    def weekly_evolution(self, **kwargs) -> bool:
        try:
            return self.slack.notify_weekly_evolution(**kwargs)
        except Exception as exc:
            logger.warning("notifier.weekly_evolution failed: %s", exc)
            return False

    def info(self, title: str, message: str) -> bool:
        try:
            return self.slack.notify_info(component=title, message=message)
        except Exception as exc:
            logger.warning("notifier.info failed: %s", exc)
            return False

    def error(self, component: str, exc: Exception, context: dict | None = None) -> bool:
        try:
            return self.slack.notify_error(component, exc, context)
        except Exception as inner:
            logger.warning("notifier.error failed: %s", inner)
            return False


# Singleton — import-and-use pattern
notify = UnifiedNotifier()
