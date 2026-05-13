#!/usr/bin/env python3
"""sentiment_refresh — invoke user_data.modules.sentiment_engine._poll_once()

Re-homes the sentiment pipeline post-V4 cutover. Pre-cutover this ran
inside freqtrade's strategy hook (FreqAIMeanRevV1 imported sentiment_engine
and the engine's thread-based poller fired every 15 min). With freqtrade
stopped, sentiment_log went silent. This wrapper is the new entry point:
Hermes cron fires it on a 15-min cadence, the script runs one full poll
(Perplexity + 6-source aggregator → Hermes 3 fast + deep scoring →
trust-the-majority → INSERT into sentiment_log), then exits.

Always exits 0 — sentiment_engine handles its own error paths and posts
to Slack on failure; cron should not alarm.

Usage:
    python3 scripts/sentiment_refresh.py
"""
from __future__ import annotations

import asyncio
import logging
import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
USER_DATA = REPO / "user_data"

# Make `from modules.sentiment_engine import _poll_once` resolve.
if str(USER_DATA) not in sys.path:
    sys.path.insert(0, str(USER_DATA))

# Make sure POSTGRES_HOST defaults to the host-port-forwarded TimescaleDB
# (sentiment_log lives there). The user_data/modules/db.py reads these.
os.environ.setdefault("POSTGRES_HOST", "localhost")
os.environ.setdefault("POSTGRES_PORT", "5434")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("sentiment_refresh")


async def _main() -> int:
    try:
        from modules.sentiment_engine import _poll_once
    except Exception as exc:
        log.exception("failed to import sentiment_engine: %s", exc)
        return 0  # cron-safe non-fail

    log.info("sentiment_refresh starting — invoking _poll_once()")
    try:
        result = await _poll_once()
    except Exception as exc:
        log.exception("_poll_once raised: %s", exc)
        return 0

    if result is None:
        log.warning("_poll_once returned None (no items scored)")
    else:
        log.info(
            "_poll_once complete — score=%s confidence=%s n_headlines=%s",
            result.get("sentiment_score"),
            result.get("confidence"),
            result.get("n_headlines"),
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_main()))
