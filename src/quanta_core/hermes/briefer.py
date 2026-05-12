"""``quanta_core.hermes.briefer`` — Monday pre-market briefing.

Cadence
-------
Cron ``30 8 * * 1`` (Mon 08:30 ET).

Run
---
1. Pull the regime snapshot (``GET /api/regime`` if available, else read
   ``~/.quanta/state/regime.json``).
2. Pull the sentiment composite (``GET /api/sentiment`` if available).
3. Pull the upcoming-week economic calendar (``GET /api/calendar?week=next``
   or read ``stocks/kb/economic_calendar.json``).
4. Pull open positions from the ledger.
5. Write ``~/.quanta/state/briefing.json`` and (optionally) post to Slack.

This module is a *consumer* of regime + sentiment + calendar — it does not
compute any of them.  Consistent with the Layer 8 contract.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

try:
    import httpx
except Exception:  # pragma: no cover
    httpx = None  # type: ignore[assignment]

from quanta_core.hermes._common import (
    HermesConfig,
    SlackNotifier,
    StateWriter,
    configure_logging,
    load_config,
    utc_iso,
    utc_now,
)
from quanta_core.hermes._ledger import LedgerClient


@dataclass
class BriefingInputs:
    """Dataclass so tests can drive the renderer with fixtures."""

    regime: Mapping[str, Any]
    sentiment: Mapping[str, Any]
    calendar: Sequence[Mapping[str, Any]]
    open_positions: Sequence[Mapping[str, Any]]


def _http_get_json(url: str, timeout: float) -> Mapping[str, Any] | None:
    if httpx is None:  # pragma: no cover
        return None
    try:
        resp = httpx.get(url, timeout=timeout)
        if resp.status_code != 200:
            return None
        data = resp.json()
        if isinstance(data, dict):
            return data
        if isinstance(data, list):
            return {"items": data}
    except Exception:
        return None
    return None


def fetch_regime(cfg: HermesConfig) -> Mapping[str, Any]:
    api = _http_get_json(f"{cfg.mf_api_url}/api/regime", timeout=cfg.postgres_timeout_seconds)
    if api:
        return api
    p = cfg.state_root / "regime.json"
    if p.exists():
        try:
            data = json.loads(p.read_text())
            if isinstance(data, dict):
                return data
        except Exception:
            pass
    return {"regime": "unknown", "source": "missing"}


def fetch_sentiment(cfg: HermesConfig) -> Mapping[str, Any]:
    api = _http_get_json(f"{cfg.mf_api_url}/api/sentiment", timeout=cfg.postgres_timeout_seconds)
    if api:
        return api
    p = cfg.state_root / "sentiment.json"
    if p.exists():
        try:
            data = json.loads(p.read_text())
            if isinstance(data, dict):
                return data
        except Exception:
            pass
    return {"score": None, "source": "missing"}


def fetch_calendar(cfg: HermesConfig) -> Sequence[Mapping[str, Any]]:
    api = _http_get_json(
        f"{cfg.mf_api_url}/api/calendar?week=next",
        timeout=cfg.postgres_timeout_seconds,
    )
    if api:
        items = api.get("items", api.get("events", []))
        if isinstance(items, list):
            return [i for i in items if isinstance(i, Mapping)]
    p = cfg.repo_root_path / "stocks" / "kb" / "economic_calendar.json"
    if p.exists():
        try:
            data = json.loads(p.read_text())
            if isinstance(data, list):
                return [i for i in data if isinstance(i, Mapping)]
            if isinstance(data, dict):
                items = data.get("events", [])
                if isinstance(items, list):
                    return [i for i in items if isinstance(i, Mapping)]
        except Exception:
            pass
    return []


def render_briefing(inputs: BriefingInputs, today: date) -> dict[str, Any]:
    """Build the dashboard banner JSON + Slack-ready summary."""

    next_monday = today + timedelta(days=(7 - today.weekday()) % 7 or 7)
    return {
        "ts": utc_iso(),
        "for_week_starting": next_monday.isoformat(),
        "regime": dict(inputs.regime),
        "sentiment": dict(inputs.sentiment),
        "calendar": [dict(e) for e in inputs.calendar],
        "open_positions": [dict(p) for p in inputs.open_positions],
        "summary": _summarize_briefing(inputs),
    }


def _summarize_briefing(inputs: BriefingInputs) -> str:
    regime = inputs.regime.get("regime", "unknown")
    sent = inputs.sentiment.get("score")
    sent_str = f"{sent:+.2f}" if isinstance(sent, (int, float)) else "n/a"
    n_events = len(inputs.calendar)
    n_open = len(inputs.open_positions)
    return (
        f"regime={regime} · sentiment={sent_str} · "
        f"{n_events} upcoming event(s) · {n_open} open position(s)"
    )


def _open_positions_view(ledger: LedgerClient) -> Sequence[Mapping[str, Any]]:
    rows = list(ledger.open_positions())
    return [
        {
            "trade_id": r.trade_id,
            "pair": r.pair,
            "side": r.side,
            "entry_ts": r.entry_ts.isoformat() if r.entry_ts else None,
            "strategy": r.strategy,
            "regime": r.regime,
        }
        for r in rows
    ]


def _parse_args(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="quanta_core.hermes.briefer",
        description="Monday pre-market briefing → state + Slack",
    )
    parser.add_argument(
        "--no-slack",
        action="store_true",
        help="skip Slack post; just write the state file",
    )
    return parser.parse_args(list(argv))


def run(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv if argv is not None else sys.argv[1:])
    cfg = load_config()
    log = configure_logging("briefer")
    notifier = SlackNotifier(cfg.slack_webhook_url, cfg.slack_channel)
    ledger = LedgerClient(cfg.postgres_dsn, cfg.postgres_timeout_seconds)

    inputs = BriefingInputs(
        regime=fetch_regime(cfg),
        sentiment=fetch_sentiment(cfg),
        calendar=fetch_calendar(cfg),
        open_positions=_open_positions_view(ledger),
    )
    today = utc_now().date()
    briefing = render_briefing(inputs, today)

    state_path = cfg.state_root / "briefing.json"
    StateWriter(state_path).write(briefing)
    log.info("wrote %s — %s", state_path, briefing["summary"])

    if not args.no_slack:
        notifier.post(f":sun_with_face: pre-market briefing · {briefing['summary']}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(run())
