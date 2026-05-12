"""``quanta_core.hermes.reflector`` — nightly lesson writer.

Cadence
-------
Cron ``0 23 * * *`` (23:00 ET nominal; doc 11 §3 also references 23:30
ET — the build agent's brief specifies **23:30 ET nightly**; either is
acceptable, the script does not assume a clock).

Run
---
1. Read closed trades for the just-ended UTC day from the ledger.
2. For each trade, ask the resident ``hermes3:8b`` model to produce a
   2-4 sentence post-mortem.
3. Append the rendered block to ``stocks/memory/decisions.md`` atomically
   (write-tmp + ``os.replace``).
4. Write ``~/.quanta/state/last_reflection.json`` per doc §5.2 schema.

Failure behaviour (doc §7.1)
----------------------------
* No trades for the day → exit 0 with a "no_trades" reason in state.
* LLM unavailable → write partial state with ``model_unavailable=True``
  and exit 0; downstream healthcheck will fire on staleness.
* Ledger unavailable → log + exit 1 (data fault, fail loud).

Backfill
--------
``python -m quanta_core.hermes.reflector --backfill 3`` retro-runs the
last N days.  Used when a missed cron needs to be recovered manually.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from quanta_core.hermes._common import (
    HermesConfig,
    SlackNotifier,
    StateWriter,
    configure_logging,
    load_config,
    utc_iso,
    utc_now,
)
from quanta_core.hermes._ledger import LedgerClient, TradeRow
from quanta_core.hermes._ollama import OllamaClient

# Verbatim prompt (port from TradingAgents, Apache-2.0).  Same shape as the
# existing ``scripts/nightly_reflector.py`` so adapters trained on the
# legacy data continue to score well on the new pipeline.
REFLECTOR_SYSTEM = (
    "You are a trading analyst reviewing your own past decision now that "
    "the outcome is known.\n\n"
    "Write exactly 2-4 sentences of plain prose answering, in this order:\n"
    "1. Was the directional call correct? Cite the realised P&L (+X.X% or -X.X%).\n"
    "2. Which part of the investment thesis held or failed?\n"
    "3. One concrete lesson to apply to the next similar analysis.\n\n"
    "Constraints: no bullet points, no headings, no apology language."
)


@dataclass
class ReflectionRecord:
    trade_id: str
    pair: str
    side: str
    pnl_pct: float | None
    text: str

    def render(self) -> str:
        pnl_str = f"{self.pnl_pct:+.2f}%" if self.pnl_pct is not None else "n/a"
        return (
            f"\n### {self.pair} · {self.side} · {self.trade_id}\n"
            f"- **P&L** {pnl_str}\n\n"
            f"{self.text.strip()}\n"
        )


def _trade_to_prompt(trade: TradeRow) -> str:
    pnl_str = f"{trade.pnl_pct:+.2f}%" if trade.pnl_pct is not None else "unknown"
    parts = [
        f"Trade {trade.trade_id} · {trade.pair} {trade.side}",
        f"Realised P&L: {pnl_str}",
    ]
    if trade.entry_price is not None and trade.exit_price is not None:
        parts.append(
            f"Entry ${trade.entry_price:.4f} → Exit ${trade.exit_price:.4f}"
        )
    if trade.entry_ts and trade.exit_ts:
        held = (trade.exit_ts - trade.entry_ts).total_seconds() / 86400.0
        parts.append(f"Held {held:.2f} days")
    if trade.strategy:
        parts.append(f"Strategy: {trade.strategy}")
    if trade.regime:
        parts.append(f"Regime at entry: {trade.regime}")
    return "\n".join(parts)


def reflect_one(
    trade: TradeRow, ollama: OllamaClient, model: str
) -> ReflectionRecord | None:
    """Call the LLM for a single trade.  Returns ``None`` on infra failure."""

    text = ollama.generate(
        model=model,
        prompt=_trade_to_prompt(trade),
        system=REFLECTOR_SYSTEM,
    )
    if text is None or not text.strip():
        return None
    return ReflectionRecord(
        trade_id=trade.trade_id,
        pair=trade.pair,
        side=trade.side,
        pnl_pct=trade.pnl_pct,
        text=text,
    )


def render_day_block(
    trading_day: date, records: Sequence[ReflectionRecord]
) -> str:
    """Render the markdown block to append to ``decisions.md``."""

    header = f"\n## Reflections · {trading_day.isoformat()}\n"
    body = "".join(r.render() for r in records)
    if not records:
        body = "\n_No trades closed today._\n"
    return header + body


def _resolve_decisions_path(cfg: HermesConfig) -> Path:
    return cfg.repo_root_path / "stocks" / "memory" / "decisions.md"


def _resolve_state_path(cfg: HermesConfig) -> Path:
    return cfg.state_root / "last_reflection.json"


def run_for_day(
    trading_day: date,
    cfg: HermesConfig,
    ledger: LedgerClient,
    ollama: OllamaClient,
    notifier: SlackNotifier,
    dry_run: bool = False,
) -> tuple[int, dict[str, Any]]:
    """Run reflector for a single trading day.  Returns ``(exit_code, payload)``."""

    log = configure_logging("reflector")
    started = utc_now()

    if not ledger.available:
        log.error("ledger unavailable — refusing to run (data fault, fail loud)")
        return 1, {
            "ts": utc_iso(),
            "trading_day": trading_day.isoformat(),
            "error": "ledger_unavailable",
        }

    trades = list(ledger.closed_trades_for_day(trading_day))
    log.info("found %d closed trades for %s", len(trades), trading_day)

    records: list[ReflectionRecord] = []
    model_unavailable = False
    for trade in trades:
        rec = reflect_one(trade, ollama, cfg.reflector_model)
        if rec is None:
            log.warning(
                "reflector LLM unavailable for %s — leaving placeholder",
                trade.trade_id,
            )
            model_unavailable = True
            records.append(
                ReflectionRecord(
                    trade_id=trade.trade_id,
                    pair=trade.pair,
                    side=trade.side,
                    pnl_pct=trade.pnl_pct,
                    text="_pending — LLM unavailable at reflection time_",
                )
            )
            continue
        records.append(rec)

    block = render_day_block(trading_day, records)

    if not dry_run:
        decisions_path = _resolve_decisions_path(cfg)
        StateWriter(decisions_path).append_text_atomic(block)
        log.info("appended %d lines to %s", block.count("\n"), decisions_path)

    duration_seconds = (utc_now() - started).total_seconds()
    payload = {
        "ts": utc_iso(started),
        "trading_day": trading_day.isoformat(),
        "trades_reviewed": len(trades),
        "summary": _summarize(records),
        "decisions_md_lines_appended": block.count("\n"),
        "model": cfg.reflector_model,
        "model_unavailable": model_unavailable,
        "duration_seconds": round(duration_seconds, 2),
        "dry_run": dry_run,
    }
    if not dry_run:
        StateWriter(_resolve_state_path(cfg)).write(payload)

    if trades:
        wl = _count_winners_losers(trades)
        notifier.post(
            f":memo: reflector · {trading_day.isoformat()} · "
            f"{wl[0]}W / {wl[1]}L · "
            f"{len(records)} lessons appended to decisions.md"
        )
    return 0, payload


def _summarize(records: Sequence[ReflectionRecord]) -> str:
    if not records:
        return "no closed trades"
    winners = sum(1 for r in records if (r.pnl_pct or 0) > 0)
    losers = len(records) - winners
    return (
        f"{winners} winner{'s' if winners != 1 else ''}, "
        f"{losers} loser{'s' if losers != 1 else ''}"
    )


def _count_winners_losers(trades: Sequence[TradeRow]) -> tuple[int, int]:
    w = sum(1 for t in trades if (t.pnl_pct or 0) > 0)
    return w, len(trades) - w


def _parse_args(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="quanta_core.hermes.reflector",
        description="Append nightly per-trade reflections to decisions.md",
    )
    parser.add_argument(
        "--day",
        type=lambda s: date.fromisoformat(s),
        default=None,
        help="trading day (default: yesterday UTC)",
    )
    parser.add_argument(
        "--backfill",
        type=int,
        default=0,
        help="re-run the last N days (inclusive of --day)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="compute + log only; skip decisions.md + state writes",
    )
    return parser.parse_args(list(argv))


def run(argv: Sequence[str] | None = None) -> int:
    """Module entrypoint.  ``python -m quanta_core.hermes.reflector``."""

    args = _parse_args(argv if argv is not None else sys.argv[1:])
    cfg = load_config()
    log = configure_logging("reflector")

    target_day = args.day or (utc_now().date() - timedelta(days=1))
    days: list[date]
    if args.backfill > 0:
        days = [
            target_day - timedelta(days=i)
            for i in range(args.backfill - 1, -1, -1)
        ]
    else:
        days = [target_day]

    ledger = LedgerClient(cfg.postgres_dsn, cfg.postgres_timeout_seconds)
    ollama = OllamaClient(cfg.ollama_base_url, cfg.llm_timeout_seconds)
    notifier = SlackNotifier(cfg.slack_webhook_url, cfg.slack_channel)

    log.info("running for days: %s (dry_run=%s)", [d.isoformat() for d in days], args.dry_run)
    final_code = 0
    for day in days:
        code, _payload = run_for_day(
            day, cfg, ledger, ollama, notifier, dry_run=args.dry_run
        )
        if code != 0:
            final_code = code
    return final_code


if __name__ == "__main__":  # pragma: no cover
    sys.exit(run())
