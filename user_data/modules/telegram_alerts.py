"""
Production-grade Telegram alerter — mirrors SlackAlerter's API.

Why Telegram in addition to Slack
  - Push notifications to phone (Slack mobile is buggy; Telegram is rock-solid)
  - Operator gets paged when asleep / commuting / in meetings
  - Telegram MarkdownV2 has tighter formatting for at-a-glance reading
  - Used for HIGH-PRIORITY only — phones shouldn't buzz on routine daily P&L

Severity routing
  CRITICAL → Telegram + Slack (drawdown breach, kill switch, flash crash)
  WARNING  → Telegram + Slack (drawdown approaching, Ollama failover active)
  TRADE    → Slack only (entries/exits — too noisy for Telegram)
  REPORT   → Slack only (daily/weekly summaries)
  INFO     → Slack only (skill creation, evolution updates)

Hermes already polls the same bot for cron-summary delivery. To avoid
the getUpdates conflict that bit us yesterday: this module ONLY sends
(POST /sendMessage). It never polls.
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass

logger = logging.getLogger(__name__)

# MarkdownV2 escape set — Telegram's most strict mode but the cleanest
# rendering. Each char must be backslash-escaped if it appears in text
# we don't want interpreted as formatting.
_MD2_RESERVED = set(r"_*[]()~`>#+-=|{}.!\\")


def _escape_md2(text: object) -> str:
    """Escape a value for safe insertion into a MarkdownV2 message body."""
    out = []
    for c in str(text):
        if c in _MD2_RESERVED:
            out.append("\\")
        out.append(c)
    return "".join(out)


@dataclass(frozen=True)
class TelegramConfig:
    bot_token: str
    chat_id: str
    enabled: bool = True
    parse_mode: str = "MarkdownV2"
    timeout_s: float = 10.0

    @classmethod
    def from_env(cls) -> TelegramConfig:
        token = (os.environ.get("TELEGRAM_BOT_TOKEN") or "").strip()
        chat_id = (os.environ.get("TELEGRAM_CHAT_ID") or "").strip()
        # Treat an obvious placeholder as "not configured"
        is_placeholder = token.startswith(("your_", "${", "TELEGRAM"))
        return cls(
            bot_token=token,
            chat_id=chat_id,
            enabled=bool(token and chat_id and not is_placeholder),
        )


class TelegramAlerter:
    """Mirrors SlackAlerter's public surface. Methods that aren't critical
    or warning are intentional no-ops — the operator's phone shouldn't
    buzz on every trade fill."""

    def __init__(self, cfg: TelegramConfig | None = None):
        self.cfg = cfg or TelegramConfig.from_env()
        self._dedup_cache: dict[str, float] = {}
        self._dedup_window_s = 60

    @classmethod
    def from_env(cls) -> TelegramAlerter:
        return cls(TelegramConfig.from_env())

    # ── Public API — only critical / warning trigger Telegram ────────

    def notify_risk_warning(self, metric: str, value: float, threshold: float) -> bool:
        text = (
            f"⚠️ *Risk warning*\n"
            f"`{_escape_md2(metric)}` at *{_escape_md2(f'{value*100:.2f}%')}* "
            f"\\(threshold {_escape_md2(f'{threshold*100:.1f}%')}\\)\n"
            f"_Action_: monitor next 1h"
        )
        return self._send(text, dedup_key=f"warn:{metric}", priority=False)

    def notify_risk_critical(self, metric: str, value: float, threshold: float) -> bool:
        text = (
            f"🚨 *RISK CRITICAL — circuit breaker*\n"
            f"`{_escape_md2(metric)}` at *{_escape_md2(f'{value*100:.2f}%')}* "
            f"\\(threshold {_escape_md2(f'{threshold*100:.1f}%')}\\)\n"
            f"_Action_: bot paused — investigate now"
        )
        return self._send(text, dedup_key=f"crit:{metric}", priority=True)

    def notify_kill_switch(self, reason: str, actions: dict) -> bool:
        crypto = actions.get("crypto_paused", False)
        stocks = actions.get("stocks_kill_flag", False)
        # Monospaced code block for table; values stripped of ` to avoid
        # mid-block markup. MarkdownV2 doesn't need escapes inside ```...```.
        table = (
            "```\n"
            f"Reason       {str(reason)[:50]}\n"
            f"Crypto       {'paused (OK)' if crypto else 'NOT paused'}\n"
            f"Stocks       {'halted (OK)' if stocks else 'NOT halted'}\n"
            "```"
        )
        text = (
            f"🛑 *KILL SWITCH TRIPPED*\n"
            f"{table}\n"
            f"*Action*: investigate dashboard, manual reset required"
        )
        return self._send(text, dedup_key="kill_switch", priority=True)

    def notify_ollama_down(self, consecutive_failures: int) -> bool:
        text = (
            f"⚠️ *Ollama unhealthy*\n"
            f"`{_escape_md2(consecutive_failures)}` consecutive failures\n"
            f"Failover to Anthropic active — paying API costs\n"
            f"_Action_: SSH to Spark, run `systemctl status ollama`"
        )
        return self._send(text, dedup_key="ollama_down", priority=False)

    def notify_flash_crash(self, pair: str, magnitude_pct: float, timeframe: str) -> bool:
        text = (
            f"⚡ *FLASH CRASH detected*\n"
            f"`{_escape_md2(pair)}` dropped *{_escape_md2(f'{magnitude_pct:.2f}%')}* "
            f"in {_escape_md2(timeframe)}\n"
            f"_Action_: positions auto-protected — verify manually"
        )
        return self._send(text, dedup_key=f"flash:{pair}", priority=True)

    def notify_provider_failover(self, from_provider: str, to_provider: str, reason: str) -> bool:
        text = (
            f"🔄 *LLM failover*\n"
            f"`{_escape_md2(from_provider)}` → `{_escape_md2(to_provider)}`\n"
            f"Reason: {_escape_md2(reason)}"
        )
        return self._send(text, dedup_key=f"failover:{to_provider}", priority=False)

    def notify_system_alert(self, title: str, message: str) -> bool:
        text = f"⚠️ *{_escape_md2(title)}*\n{_escape_md2(message)}"
        return self._send(text, dedup_key=f"sys:{title}", priority=False)

    # ── Trade entries / exits / daily summaries — intentional no-ops ───
    def notify_trade_entry(self, *args, **kwargs) -> bool:
        return False

    def notify_trade_exit(self, *args, **kwargs) -> bool:
        return False

    def notify_daily_summary(self, *args, **kwargs) -> bool:
        return False

    def notify_weekly_evolution(self, *args, **kwargs) -> bool:
        return False

    def notify_info(self, *args, **kwargs) -> bool:
        return False

    def notify_error(self, *args, **kwargs) -> bool:
        # Errors go to Slack, not Telegram — too noisy and operator
        # already gets a Slack channel notification.
        return False

    # ── Internals ────────────────────────────────────────────────────

    def _send(
        self,
        text: str,
        *,
        dedup_key: str | None = None,
        priority: bool = False,
    ) -> bool:
        if not self.cfg.enabled:
            logger.debug("[telegram] not configured — skipping: %s", text[:80])
            return False

        # Dedup so a check that runs every 15s and stays in alarm doesn't spam
        if dedup_key:
            now = time.time()
            last = self._dedup_cache.get(dedup_key, 0.0)
            if now - last < self._dedup_window_s:
                logger.debug("[telegram] deduped: %s", dedup_key)
                return False
            self._dedup_cache[dedup_key] = now

        try:
            import requests
        except ImportError:
            logger.warning("[telegram] requests not installed — skipping")
            return False

        url = f"https://api.telegram.org/bot{self.cfg.bot_token}/sendMessage"
        payload = {
            "chat_id": self.cfg.chat_id,
            "text": text,
            "parse_mode": self.cfg.parse_mode,
            "disable_notification": not priority,  # priority=True → sound + vibrate
        }

        try:
            r = requests.post(url, json=payload, timeout=self.cfg.timeout_s)
        except Exception as exc:
            logger.warning("[telegram] send failed: %s", exc)
            return False

        if r.status_code != 200:
            # MarkdownV2 escape errors are common — log the response text
            logger.warning(
                "[telegram] HTTP %d: %s (payload-len=%d)",
                r.status_code, r.text[:200], len(text),
            )
            return False
        return True


_alerter: TelegramAlerter | None = None


def get_alerter() -> TelegramAlerter:
    global _alerter
    if _alerter is None:
        _alerter = TelegramAlerter.from_env()
    return _alerter
