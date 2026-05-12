"""``quanta_core.hermes.weekly_publisher`` — Friday Markdown drop.

Cadence
-------
Cron ``0 16 * * 5`` (Friday 16:00 ET post-close).

Run
---
1. Resolve the ISO week boundaries (Mon 00:00 → Sun 23:59 ET).
2. Pull closed trades, reflector lessons, adapter promotions, regime mix.
3. Run 3 advisory quality gates (per doc 12 §6) — failures **mutate** the
   post, they do not block it.
4. Render the Jinja2 template to ``docs/weekly/YYYY-WW.md`` atomically.
5. Append a run record to ``~/.quanta/state/weekly_publish_state.json``.

Doc 12 §5 anti-cherry-pick discipline is enforced here:

1. Mandatory file creation — module *always* writes a file, even if losing.
2. Losing weeks render through the same template, no apology branch.
3. No ``--skip-week`` flag exists.  ``--force`` only re-renders when a file
   already exists for the same week.
4. Missed-week detection — ``--audit`` mode scans ``docs/weekly/*.md``.
5. Tone parity is enforced by the template having no conditional adjectives.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader, StrictUndefined, select_autoescape

from quanta_core.hermes._common import (
    HermesConfig,
    HermesError,
    SlackNotifier,
    StateWriter,
    configure_logging,
    load_config,
    utc_iso,
    utc_now,
)
from quanta_core.hermes._ledger import LedgerClient, TradeRow

TEMPLATE_DIR = Path(__file__).parent / "templates"
TEMPLATE_NAME = "weekly_post.md.j2"


# ---------------------------------------------------------------------------
# Domain types
# ---------------------------------------------------------------------------


@dataclass
class GateResult:
    gate: str
    passed: bool
    message: str

    def as_banner(self) -> dict[str, str]:
        return {"gate": self.gate, "message": self.message}


@dataclass
class WeekContext:
    iso_year: int
    iso_week: int
    monday: date
    sunday: date
    trades: Sequence[TradeRow]
    open_positions: Sequence[TradeRow]
    lessons_added: int
    adapters_promoted: Sequence[str]
    regime_mix: Mapping[str, int]
    run_mode: str = "paper"


# ---------------------------------------------------------------------------
# Week-boundary math
# ---------------------------------------------------------------------------


def iso_week_bounds(reference: date) -> tuple[date, date, int, int]:
    """Return ``(monday, sunday, iso_year, iso_week)`` for the ISO week
    containing ``reference``."""

    iso_year, iso_week, iso_dow = reference.isocalendar()
    monday = reference - timedelta(days=iso_dow - 1)
    sunday = monday + timedelta(days=6)
    return monday, sunday, iso_year, iso_week


# ---------------------------------------------------------------------------
# Quality gates (advisory)
# ---------------------------------------------------------------------------


def gate_reconciliation(
    trades: Sequence[TradeRow], broker_delta: float | None
) -> GateResult:
    """Trade-sum vs broker delta within 1¢."""

    total = sum((t.pnl or 0.0) for t in trades)
    if broker_delta is None:
        return GateResult(
            "reconciliation",
            False,
            "broker_delta_unavailable — reconciliation skipped",
        )
    diff = abs(total - broker_delta)
    passed = diff <= 0.01
    return GateResult(
        "reconciliation",
        passed,
        f"sum_pnl=${total:.2f} broker_delta=${broker_delta:.2f} diff=${diff:.2f}",
    )


def gate_reflector_daily(
    reflector_days_seen: Sequence[date], monday: date, sunday: date
) -> GateResult:
    """At least one reflector run per weekday in window."""

    weekdays = {
        monday + timedelta(days=i)
        for i in range((sunday - monday).days + 1)
        if (monday + timedelta(days=i)).weekday() < 5
    }
    seen = set(reflector_days_seen)
    missing = sorted(weekdays - seen)
    if not missing:
        return GateResult("reflector_daily", True, "all weekdays present")
    return GateResult(
        "reflector_daily",
        False,
        f"Reflector missed {len(missing)} days: {[d.isoformat() for d in missing]}",
    )


def gate_risk_anchor(
    anchor_value: float | None, expected_anchor: float | None
) -> GateResult:
    """Risk-governor anchor matches expected starting equity."""

    if anchor_value is None or expected_anchor is None:
        return GateResult(
            "risk_anchor",
            False,
            "risk_anchor_unavailable — anchor check skipped",
        )
    drift = abs(anchor_value - expected_anchor)
    passed = drift < 0.01
    return GateResult(
        "risk_anchor",
        passed,
        f"anchor=${anchor_value:.2f} expected=${expected_anchor:.2f} drift=${drift:.2f}",
    )


# ---------------------------------------------------------------------------
# Adapter promotions (consume Hermes' own state file)
# ---------------------------------------------------------------------------


def read_adapters_promoted(state_root: Path) -> list[str]:
    """Read the most-recent ``last_lora_promotion.json`` and return role tags.

    Falls back to an empty list if the file is missing.
    """

    p = state_root / "last_lora_promotion.json"
    if not p.exists():
        return []
    try:
        data = json.loads(p.read_text())
    except Exception:
        return []
    promotions = data.get("promotions") or []
    out: list[str] = []
    for p_ in promotions:
        if not isinstance(p_, dict):
            continue
        if not p_.get("pareto_pass"):
            continue
        role = p_.get("role", "?")
        from_v = p_.get("from") or "?"
        to_v = p_.get("to") or "?"
        out.append(f"{role}: {from_v}→{to_v}")
    return out


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def _trade_view(trade: TradeRow) -> dict[str, Any]:
    held = None
    if trade.entry_ts and trade.exit_ts:
        held = (trade.exit_ts - trade.entry_ts).total_seconds() / 86400.0
    return {
        "pair": trade.pair,
        "side": trade.side,
        "entry_price": _fmt(trade.entry_price),
        "exit_price": _fmt(trade.exit_price),
        "entry_ts": trade.entry_ts.isoformat() if trade.entry_ts else "?",
        "exit_ts": trade.exit_ts.isoformat() if trade.exit_ts else "?",
        "pnl": _fmt(trade.pnl),
        "pnl_pct": _fmt(trade.pnl_pct),
        "hold_duration": f"{held:.2f}d" if held is not None else "?",
        "strategy": trade.strategy or "?",
        "regime": trade.regime or "?",
        "lessons": [],
    }


def _open_view(pos: TradeRow, today: date) -> dict[str, Any]:
    days_held: int | None = None
    if pos.entry_ts:
        days_held = (today - pos.entry_ts.date()).days
    return {
        "pair": pos.pair,
        "side": pos.side,
        "entry_ts": pos.entry_ts.isoformat() if pos.entry_ts else "?",
        "days_held": days_held,
    }


def _fmt(value: Any) -> str:
    if value is None:
        return "n/a"
    try:
        return f"{float(value):.2f}"
    except (TypeError, ValueError):
        return str(value)


def render_post(
    week: WeekContext,
    gates: Sequence[GateResult],
    privacy_footer: str = "Paper mode — all values shown as-is.",
) -> str:
    """Render the Markdown post.  Pure function — no I/O."""

    env = Environment(
        loader=FileSystemLoader(str(TEMPLATE_DIR)),
        autoescape=select_autoescape([]),  # markdown — autoescape off
        undefined=StrictUndefined,
        keep_trailing_newline=True,
    )
    template = env.get_template(TEMPLATE_NAME)

    failing = [g for g in gates if not g.passed]
    today = utc_now().date()

    net_pnl = sum((t.pnl or 0.0) for t in week.trades)
    pnl_pcts = [t.pnl_pct for t in week.trades if t.pnl_pct is not None]
    net_pnl_pct = sum(pnl_pcts) if pnl_pcts else 0.0

    return template.render(
        iso_year=week.iso_year,
        iso_week=f"{week.iso_week:02d}",
        monday_date=week.monday.isoformat(),
        sunday_date=week.sunday.isoformat(),
        net_pnl=f"{net_pnl:.2f}",
        net_pnl_pct=f"{net_pnl_pct:.2f}",
        drawdown_pct="n/a",
        open_count=len(week.open_positions),
        run_mode=week.run_mode,
        trade_count=len(week.trades),
        trades=[_trade_view(t) for t in week.trades],
        lessons_added=week.lessons_added,
        adapters_promoted=list(week.adapters_promoted),
        regime_mix=dict(week.regime_mix),
        open_positions=[_open_view(p, today) for p in week.open_positions],
        generated_ts=utc_iso(),
        privacy_footer=privacy_footer,
        data_integrity_warnings=[g.as_banner() for g in failing],
    )


# ---------------------------------------------------------------------------
# I/O helpers
# ---------------------------------------------------------------------------


def output_path(cfg: HermesConfig, week: WeekContext) -> Path:
    return (
        cfg.repo_root_path
        / "docs"
        / "weekly"
        / f"{week.iso_year}-W{week.iso_week:02d}.md"
    )


def write_post(path: Path, content: str, force: bool) -> None:
    """Atomic write.  Refuses to overwrite unless ``force=True``."""

    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and not force:
        raise HermesError(
            f"weekly publish refused: {path} already exists — pass --force to re-render"
        )
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(content)
    os.replace(tmp, path)


def missed_weeks(docs_root: Path, since: date, today: date) -> list[str]:
    """Return iso-week tags missing between ``since`` and ``today``.

    Used by the missed-week audit job (doc 12 §5.4).
    """

    weeks_dir = docs_root / "weekly"
    existing: set[str] = set()
    if weeks_dir.exists():
        for f in weeks_dir.glob("*.md"):
            existing.add(f.stem)

    cursor = since
    out: list[str] = []
    while cursor <= today:
        iso_year, iso_week, _ = cursor.isocalendar()
        tag = f"{iso_year}-W{iso_week:02d}"
        if tag not in existing:
            out.append(tag)
        cursor += timedelta(days=7)
    # Deduplicate while preserving order
    seen: set[str] = set()
    deduped: list[str] = []
    for tag in out:
        if tag not in seen:
            seen.add(tag)
            deduped.append(tag)
    return deduped


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------


def _parse_args(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="quanta_core.hermes.weekly_publisher",
        description="Render the Friday weekly Markdown drop",
    )
    parser.add_argument(
        "--week",
        default="current",
        help="'current' (default) | 'previous' | YYYY-WW iso tag",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="overwrite an existing weekly file",
    )
    parser.add_argument(
        "--audit",
        action="store_true",
        help="report missed weeks; do not render",
    )
    parser.add_argument(
        "--audit-since",
        default=None,
        help="ISO date floor for --audit (default: 8 weeks ago)",
    )
    return parser.parse_args(list(argv))


def _resolve_reference_date(spec: str) -> date:
    today = utc_now().date()
    if spec == "current":
        return today
    if spec == "previous":
        return today - timedelta(days=7)
    # YYYY-WW
    try:
        y, w = spec.split("-W")
        return date.fromisocalendar(int(y), int(w), 1)
    except Exception as exc:
        raise HermesError(f"bad --week value {spec!r}") from exc


def build_context(
    reference: date,
    ledger: LedgerClient,
    cfg: HermesConfig,
) -> WeekContext:
    monday, sunday, iso_year, iso_week = iso_week_bounds(reference)
    trades = list(ledger.closed_trades_for_range(monday, sunday))
    opens = list(ledger.open_positions())
    adapters = read_adapters_promoted(cfg.state_root)
    regime_mix: dict[str, int] = {}
    for t in trades:
        key = t.regime or "unknown"
        regime_mix[key] = regime_mix.get(key, 0) + 1
    return WeekContext(
        iso_year=iso_year,
        iso_week=iso_week,
        monday=monday,
        sunday=sunday,
        trades=trades,
        open_positions=opens,
        lessons_added=0,
        adapters_promoted=adapters,
        regime_mix=regime_mix,
        run_mode=os.environ.get("QUANTA_RUN_MODE", "paper"),
    )


def run(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv if argv is not None else sys.argv[1:])
    cfg = load_config()
    log = configure_logging("weekly_publisher")
    notifier = SlackNotifier(cfg.slack_webhook_url, cfg.slack_channel)
    state_path = cfg.state_root / "weekly_publish_state.json"

    if args.audit:
        since_str = args.audit_since
        since: date
        if since_str:
            since = date.fromisoformat(since_str)
        else:
            since = utc_now().date() - timedelta(weeks=8)
        missed = missed_weeks(
            cfg.repo_root_path / "docs", since, utc_now().date()
        )
        log.info("audit: missed weeks since %s: %s", since, missed)
        StateWriter(state_path).write(
            {
                "ts": utc_iso(),
                "audit": True,
                "since": since.isoformat(),
                "missed_weeks": missed,
            }
        )
        if missed:
            notifier.post(
                f":warning: weekly publish missed for {len(missed)} weeks: "
                + ", ".join(missed[:5])
            )
        return 0

    reference = _resolve_reference_date(args.week)
    ledger = LedgerClient(cfg.postgres_dsn, cfg.postgres_timeout_seconds)
    week = build_context(reference, ledger, cfg)

    # 3 advisory gates — failures mutate but do not block
    gates: list[GateResult] = [
        gate_reconciliation(week.trades, broker_delta=None),
        gate_reflector_daily(
            reflector_days_seen=_reflector_days_from_state(cfg, week.monday, week.sunday),
            monday=week.monday,
            sunday=week.sunday,
        ),
        gate_risk_anchor(anchor_value=None, expected_anchor=None),
    ]
    log.info("rendering week %d-W%02d", week.iso_year, week.iso_week)
    rendered = render_post(week, gates)
    out = output_path(cfg, week)

    try:
        write_post(out, rendered, force=args.force)
    except HermesError as exc:
        log.warning("publish skipped: %s", exc)
        return 1

    payload = {
        "ts": utc_iso(),
        "iso_week": f"{week.iso_year}-W{week.iso_week:02d}",
        "monday": week.monday.isoformat(),
        "sunday": week.sunday.isoformat(),
        "markdown_path": str(out.relative_to(cfg.repo_root_path))
        if out.is_absolute() and out.is_relative_to(cfg.repo_root_path)
        else str(out),
        "trade_count": len(week.trades),
        "open_count": len(week.open_positions),
        "lessons_added": week.lessons_added,
        "adapters_promoted": list(week.adapters_promoted),
        "regime_mix": dict(week.regime_mix),
        "gate_results": {g.gate: ("pass" if g.passed else "warn") for g in gates},
        "data_integrity_warning": any(not g.passed for g in gates),
        "run_mode": week.run_mode,
    }
    StateWriter(state_path).write(payload)
    notifier.post(
        f":bookmark_tabs: weekly publish · {payload['iso_week']} · "
        f"{len(week.trades)} trades · "
        f"{len([g for g in gates if not g.passed])} gate warning(s)"
    )
    return 0


def _reflector_days_from_state(
    cfg: HermesConfig, monday: date, sunday: date
) -> list[date]:
    """Best-effort read of recent reflector run dates.

    The reflector overwrites its single state file each night, so this is
    a conservative check: we report the *single* last-known run as the
    only "seen" day.  The gate will warn loudly until the publisher reads
    a history file (deferred — present as a TODO in HANDOFF.md).
    """

    p = cfg.state_root / "last_reflection.json"
    if not p.exists():
        return []
    try:
        data = json.loads(p.read_text())
        td = data.get("trading_day")
        if td:
            return [date.fromisoformat(td)]
    except Exception:
        pass
    return []


if __name__ == "__main__":  # pragma: no cover
    sys.exit(run())
