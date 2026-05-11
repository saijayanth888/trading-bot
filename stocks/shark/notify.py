"""Stocks-side adapter for the unified notifier.

The crypto side already has `user_data/modules/notifier.py` with a `notify`
singleton wrapping Slack + Telegram + Email. The stocks pipeline ran without
any operator pings on phase events — `market_open.py`, `daily_summary.py`,
and `execution/orders.py` had zero notifier imports.

This module:
  1. Adds `user_data/` to sys.path lazily on first import.
  2. Re-exposes the same `notify` singleton.
  3. Falls back to a deterministic stub that logs to stdout if the real
     notifier can't be loaded (so a missing webhook doesn't crash a phase).

Usage from any stocks module:
    from shark.notify import notify
    notify.trade_entry(pair="NVDA", signal="long", entry_price=219.04, ...)
    notify.daily_summary(...)
"""
from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

logger = logging.getLogger(__name__)

_REPO_ROOT = Path(__file__).resolve().parents[2]
_USER_DATA = _REPO_ROOT / "user_data"


class _StubNotifier:
    """Last-resort no-op singleton — logs instead of pinging.

    Returned by `_load_notifier()` when the real notifier import fails
    (e.g. user_data path missing on a stripped install). The interface
    matches `UnifiedNotifier` so callers don't have to handle two shapes.
    """

    def _log(self, kind: str, **kwargs) -> dict[str, bool]:
        logger.info("[NOTIFY-STUB] %s %s", kind, kwargs)
        return {"slack": False, "telegram": False}

    def critical(self, kind: str, **kwargs):
        return self._log("CRITICAL " + kind, **kwargs)

    def warning(self, kind: str, **kwargs):
        return self._log("WARNING " + kind, **kwargs)

    def trade_entry(self, **kwargs):
        return self._log("TRADE_ENTRY", **kwargs)

    def trade_exit(self, **kwargs):
        return self._log("TRADE_EXIT", **kwargs)

    def daily_summary(self, **kwargs):
        return self._log("DAILY_SUMMARY", **kwargs)

    def weekly_evolution(self, **kwargs):
        return self._log("WEEKLY", **kwargs)

    def info(self, title: str, message: str):
        return self._log("INFO " + title, message=message)

    def error(self, component: str, exc: Exception, context=None):
        return self._log("ERROR " + component, error=str(exc), context=context or {})


def _load_notifier():
    if str(_USER_DATA) not in sys.path:
        sys.path.insert(0, str(_USER_DATA))
    try:
        from modules.notifier import notify as _real
        return _real
    except Exception as exc:
        logger.warning("shark.notify: real notifier unavailable (%s) — using stub", exc)
        return _StubNotifier()


notify = _load_notifier()
