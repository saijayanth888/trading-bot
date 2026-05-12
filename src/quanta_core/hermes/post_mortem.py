"""``quanta_core.hermes.post_mortem`` — Saturday weekly post-mortem.

Cadence
-------
Cron ``0 10 * * 6`` (Saturday 10:00 ET).

Run
---
1. Pull the previous 7 days of closed trades from the ledger.
2. Cluster losses by ``(regime, exit_reason)``.
3. Ask ``hermes3:70b`` to summarise the top-3 loss buckets + write a
   manual-review recommendation per bucket.
4. Append the result to ``stocks/memory/decisions.md`` atomically.
5. Optional Slack post.

Per doc §7.5 this module is informational only — no state mutation, no
auto-apply.  Failure → quiet bell + no rollback.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, timedelta

from quanta_core.hermes._common import (
    SlackNotifier,
    StateWriter,
    configure_logging,
    load_config,
    utc_iso,
    utc_now,
)
from quanta_core.hermes._ledger import LedgerClient, TradeRow
from quanta_core.hermes._ollama import OllamaClient

POST_MORTEM_SYSTEM = (
    "You are a senior trading-desk analyst summarising the week's losses for "
    "the next-Monday review.\n\n"
    "Input is the top-3 loss buckets clustered by (regime, exit_reason). "
    "For each bucket, write:\n"
    "- A single sentence of what likely failed.\n"
    "- A single sentence of recommended manual review.\n\n"
    "Constraints: no bullet points, no headings, plain prose paragraphs."
)


@dataclass
class LossBucket:
    regime: str
    exit_reason: str
    count: int
    total_pnl: float
    sample_trade_ids: list[str]


def cluster_losses(trades: Sequence[TradeRow]) -> list[LossBucket]:
    """Group losing trades by ``(regime, exit_reason)``, sorted desc by loss."""

    buckets: dict[tuple[str, str], LossBucket] = {}
    for t in trades:
        if (t.pnl or 0.0) >= 0:
            continue
        regime = t.regime or "unknown"
        exit_reason = str(t.raw.get("exit_reason") or "unknown")
        key = (regime, exit_reason)
        b = buckets.get(key)
        if b is None:
            b = LossBucket(
                regime=regime,
                exit_reason=exit_reason,
                count=0,
                total_pnl=0.0,
                sample_trade_ids=[],
            )
            buckets[key] = b
        b.count += 1
        b.total_pnl += t.pnl or 0.0
        if len(b.sample_trade_ids) < 3:
            b.sample_trade_ids.append(t.trade_id)
    return sorted(buckets.values(), key=lambda x: x.total_pnl)


def buckets_to_prompt(buckets: Sequence[LossBucket]) -> str:
    if not buckets:
        return "No losses this week."
    lines = ["Top loss buckets for the week:"]
    for i, b in enumerate(buckets[:3], start=1):
        lines.append(
            f"{i}. ({b.regime} / {b.exit_reason}) — "
            f"{b.count} trades · total ${b.total_pnl:.2f}"
        )
    return "\n".join(lines)


def render_post_mortem_md(
    week_start: date,
    week_end: date,
    buckets: Sequence[LossBucket],
    llm_text: str | None,
) -> str:
    header = f"\n## Weekly Post-mortem · {week_start.isoformat()} → {week_end.isoformat()}\n"
    body_lines: list[str] = []
    if not buckets:
        body_lines.append("_No losing trades to cluster._\n")
    else:
        body_lines.append("**Top-3 loss buckets**\n")
        for i, b in enumerate(buckets[:3], start=1):
            body_lines.append(
                f"{i}. ({b.regime} / {b.exit_reason}) · "
                f"{b.count} trade(s) · total ${b.total_pnl:.2f}\n"
            )
    if llm_text:
        body_lines.append("\n" + llm_text.strip() + "\n")
    return header + "\n".join(body_lines) + "\n"


def _parse_args(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="quanta_core.hermes.post_mortem",
        description="Saturday weekly post-mortem cluster + review note",
    )
    parser.add_argument(
        "--end",
        type=lambda s: date.fromisoformat(s),
        default=None,
        help="end-of-window date (default: today UTC)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="compute + log only; skip decisions.md write",
    )
    return parser.parse_args(list(argv))


def run(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv if argv is not None else sys.argv[1:])
    cfg = load_config()
    log = configure_logging("post_mortem")
    notifier = SlackNotifier(cfg.slack_webhook_url, cfg.slack_channel)

    today = args.end or utc_now().date()
    start = today - timedelta(days=6)
    ledger = LedgerClient(cfg.postgres_dsn, cfg.postgres_timeout_seconds)
    trades = list(ledger.closed_trades_for_range(start, today))
    log.info("post_mortem window %s → %s: %d trades", start, today, len(trades))

    buckets = cluster_losses(trades)
    ollama = OllamaClient(cfg.ollama_base_url, cfg.llm_timeout_seconds)
    llm_text = (
        ollama.generate(
            model=cfg.post_mortem_model,
            prompt=buckets_to_prompt(buckets),
            system=POST_MORTEM_SYSTEM,
        )
        if buckets
        else None
    )

    md = render_post_mortem_md(start, today, buckets, llm_text)

    if not args.dry_run:
        decisions_path = (
            cfg.repo_root_path / "stocks" / "memory" / "decisions.md"
        )
        StateWriter(decisions_path).append_text_atomic(md)
        log.info("appended post-mortem to %s", decisions_path)

    payload = {
        "ts": utc_iso(),
        "window_start": start.isoformat(),
        "window_end": today.isoformat(),
        "trade_count": len(trades),
        "bucket_count": len(buckets),
        "top_buckets": [
            {
                "regime": b.regime,
                "exit_reason": b.exit_reason,
                "count": b.count,
                "total_pnl": round(b.total_pnl, 2),
            }
            for b in buckets[:3]
        ],
        "llm_used": llm_text is not None,
        "dry_run": args.dry_run,
    }
    StateWriter(cfg.state_root / "last_post_mortem.json").write(payload)
    notifier.post(
        ":mag: weekly post-mortem · "
        f"{len(trades)} trades · {len(buckets)} loss bucket(s)"
    )
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(run())
