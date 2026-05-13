#!/usr/bin/env python3
"""
ModelForge ingest — Stage 1 of the trading-bot ↔ ModelForge data pipeline.

Reads the trading-bot's two reflection log sources and emits *raw* per-role
JSONL training examples to ``~/.dgx-train/raw/<role>/<YYYYMMDD>.jsonl``. Stage 2
(``modelforge_curate.py``) consumes these raws and writes the HF Arrow shards
ModelForge actually trains on.

Sources
-------
1. ``stocks/memory/decisions.md`` -> ``trading-reflector`` role. One example per
   block whose realized ``closed_at`` (date stamp on the realized tag line)
   matches yesterday. Pending blocks are also emitted with ``pending_outcome=
   True`` so we don't lose calls that haven't realized yet -- Stage 2 will
   gate on the flag.
2. ``stocks/memory/llm-calls.jsonl`` -> the 5 non-reflector roles. Filters by
   the ``agent`` field per :mod:`shark.llm.tracker` and only keeps lines whose
   ``timestamp`` falls inside yesterday (UTC).

Scheduling
----------
Target cron slot: nightly 21:00 ET via Hermes. This branch does NOT install
the cron entry -- that's a separate staging step.

Failure mode
------------
Fail-soft. Any unhandled exception writes a line to
``stocks/memory/cron-modelforge-ingest.log`` and the process exits 0 so cron
doesn't alarm. Per-role accept counts go to stdout for log scraping.

CLI
---
::

    python scripts/modelforge_ingest.py             # ingest yesterday
    python scripts/modelforge_ingest.py 2026-05-11  # ingest a specific date

See ``docs/MODELFORGE_DATA_PIPELINE.md`` for the full schema + flow diagram.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import os
import re
import sys
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Iterator

# --------------------------------------------------------------------------- #
# Module layout: project root is two parents up from this file.
# We add stocks/ to sys.path so the in-repo helper module imports cleanly.
# --------------------------------------------------------------------------- #

_HERE = Path(__file__).resolve()
_REPO_ROOT = _HERE.parents[1]
_STOCKS_ROOT = _REPO_ROOT / "stocks"
if str(_STOCKS_ROOT) not in sys.path:
    sys.path.insert(0, str(_STOCKS_ROOT))

# Imported lazily inside helpers below so the script still runs end-to-end in
# tests where the shark package isn't on sys.path.

logger = logging.getLogger("modelforge_ingest")


# --------------------------------------------------------------------------- #
# Constants — the agent->role mapping is the contract between the bot's
# tracker.py and ModelForge's evolution_tracks.track_id. Keep in lock-step
# with §2 of docs/MODELFORGE_INTEGRATION_PLAN.md.
# --------------------------------------------------------------------------- #

#: ``agent=`` values written by :class:`shark.llm.tracker.LLMTracker` mapped to
#: the ModelForge track_id we want to ingest into. The reflector role is
#: handled separately because it reads ``decisions.md``, not the JSONL log.
AGENT_TO_ROLE: dict[str, str] = {
    "bull_analyst":       "trading-bull",
    "analyst_bull":       "trading-bull",           # legacy alias
    "bear_analyst":       "trading-bear",
    "analyst_bear":       "trading-bear",           # legacy alias
    "research_manager":   "trading-arbiter",
    "risk_manager":       "trading-arbiter",
    "regime_tagger":      "trading-regime-tagger",
    "indicator_selector": "trading-indicator-selector",
    # 2026-05-13: also pick up Shark's actually-running risk-debate roles
    # (these are the agents the operator's stack invokes via Hermes 3 on
    # Ollama; the legacy `bull_analyst`/`bear_analyst` names map to V4's
    # DebateOrchestrator which is currently dead-code, so without this
    # extension the ingest finds 0 rows per role per day).
    "risk_debate.aggressive":  "trading-bull",      # bullish view = aggressive risk
    "risk_debate.conservative": "trading-bear",     # bearish view = conservative risk
    "risk_debate.neutral":     "trading-arbiter",   # neutral = arbiter
    "trade_reviewer":          "trading-reflector", # post-trade review
    "debate_orchestrator":     "trading-arbiter",
    "combined_analyst":        "trading-arbiter",
}

#: Roles whose Stage-2 curator should treat the call as a JSON/structured
#: output (validity-rate gate, not response-length gate).
STRUCTURED_OUTPUT_ROLES: frozenset[str] = frozenset({
    "trading-arbiter", "trading-regime-tagger", "trading-indicator-selector",
})

#: Canonical list of every role this pipeline knows about. Order is stable so
#: stdout summaries diff cleanly day over day.
ALL_ROLES: tuple[str, ...] = (
    "trading-reflector",
    "trading-bull",
    "trading-bear",
    "trading-arbiter",
    "trading-regime-tagger",
    "trading-indicator-selector",
)


# --------------------------------------------------------------------------- #
# Output layout
# --------------------------------------------------------------------------- #

def _raw_root() -> Path:
    """Resolve the on-disk root for raw ingested JSONL files.

    Override with ``DGX_TRAIN_ROOT`` for tests; defaults to
    ``~/.dgx-train/raw`` which is the operator's shared training data volume.
    """
    override = os.environ.get("DGX_TRAIN_ROOT", "").strip()
    if override:
        return Path(override) / "raw"
    return Path.home() / ".dgx-train" / "raw"


def _ingest_log_path() -> Path:
    """Where fail-soft errors are persisted so the cron job can `tail` them."""
    override = os.environ.get("MODELFORGE_INGEST_LOG", "").strip()
    if override:
        return Path(override)
    return _REPO_ROOT / "stocks" / "memory" / "cron-modelforge-ingest.log"


# --------------------------------------------------------------------------- #
# Date helpers — `yesterday` semantics live in one place so tests can pin a
# fixed date without monkey-patching datetime.
# --------------------------------------------------------------------------- #

def parse_target_date(arg: str | None, *, now: dt.datetime | None = None) -> dt.date:
    """Parse a CLI ``YYYY-MM-DD`` arg or fall back to yesterday (UTC).

    Args:
        arg: CLI string. ``None`` or ``"yesterday"`` resolves to UTC-yesterday.
        now: Injection point for tests — defaults to ``datetime.utcnow()``.

    Raises:
        ValueError: arg is non-empty and not parseable as ISO date.
    """
    if arg and arg.lower() != "yesterday":
        return dt.date.fromisoformat(arg)
    base = (now or dt.datetime.now(dt.timezone.utc))
    return (base - dt.timedelta(days=1)).date()


# --------------------------------------------------------------------------- #
# Source 1 — decisions.md (trading-reflector role)
# --------------------------------------------------------------------------- #

_REALIZED_TAG_RE = re.compile(
    # [YYYY-MM-DD | TICKER | RATING | +X.X% | +Y.Y% alpha | Nd]
    r"^\s*\[\s*(?P<open_date>\d{4}-\d{2}-\d{2})\s*\|\s*"
    r"(?P<ticker>[A-Za-z0-9._/-]+)\s*\|\s*"
    r"(?P<rating>[A-Za-z_]+)\s*\|\s*"
    r"(?P<raw_pct>[+\-]?\d+(?:\.\d+)?%)\s*\|\s*"
    r"(?P<alpha_pct>[+\-]?\d+(?:\.\d+)?% alpha)\s*\|\s*"
    r"(?P<holding>\d+d)\s*\]\s*$",
    re.MULTILINE,
)


@dataclass
class ReflectorEntry:
    """Parsed view of one realized-or-pending block in decisions.md.

    ``closed_at`` is the *realized* date (i.e. when the trade actually closed),
    not the open date. For pending blocks ``closed_at`` is ``None`` and the
    Stage-2 curator will leave the example in the pending bucket until a later
    ingest pass picks it up after the trade closes.
    """

    open_date: str          # YYYY-MM-DD as written in the tag line
    closed_at: str | None   # YYYY-MM-DD if realized, else None
    ticker: str
    rating: str
    pending: bool
    raw_pct: str | None
    alpha_pct: str | None
    holding: str | None
    decision: str           # The DECISION: prose
    reflection: str         # The REFLECTION: prose ("" if pending)


def iter_reflector_entries(decisions_md: Path) -> Iterator[ReflectorEntry]:
    """Stream every block in ``decisions.md`` as a :class:`ReflectorEntry`.

    Uses the canonical ``stocks.shark.memory.decisions`` parser so this script
    can't drift from the writer's format. If that module is unimportable
    (e.g. running outside the repo) we fall back to a minimal in-line parser.
    """
    if not decisions_md.exists():
        return

    try:
        from shark.memory.decisions import _iter_entries  # type: ignore
        for raw in _iter_entries(decisions_md):
            # `closed_at` is *not* in the parser's output today — we have to
            # infer it. The reflection cron writes the realized tag on the
            # day of close, so the file's mtime is a poor proxy. Instead we
            # treat `open_date + holding` as the closed_at; for pending we
            # leave it None.
            closed_at: str | None = None
            if not raw["pending"] and raw["holding"]:
                m = re.match(r"(\d+)d", raw["holding"])
                if m:
                    days = int(m.group(1))
                    try:
                        closed_at = (
                            dt.date.fromisoformat(raw["date"]) + dt.timedelta(days=days)
                        ).isoformat()
                    except ValueError:
                        closed_at = None
            yield ReflectorEntry(
                open_date=raw["date"],
                closed_at=closed_at,
                ticker=raw["ticker"],
                rating=raw["rating"],
                pending=bool(raw["pending"]),
                raw_pct=raw["raw_pct"],
                alpha_pct=raw["alpha_pct"],
                holding=raw["holding"],
                decision=raw["decision"],
                reflection=raw["reflection"],
            )
    except ImportError:
        # Standalone fallback parser used in test envs without the shark pkg.
        yield from _parse_decisions_inline(decisions_md)


def _parse_decisions_inline(decisions_md: Path) -> Iterator[ReflectorEntry]:
    """Minimal stand-alone decisions.md parser for test/CI environments.

    Mirrors :func:`shark.memory.decisions._iter_entries` semantics but without
    the dependency on the wider shark package. Block boundary is the markdown
    ``---`` separator; tag line is the first non-blank line that matches
    ``[ ... ]``.
    """
    text = decisions_md.read_text(encoding="utf-8")
    blocks = text.split("\n---")
    pending_re = re.compile(
        r"^\s*\[\s*(?P<open_date>\d{4}-\d{2}-\d{2})\s*\|\s*"
        r"(?P<ticker>[A-Za-z0-9._/-]+)\s*\|\s*"
        r"(?P<rating>[A-Za-z_]+)\s*\|\s*pending\s*\]\s*$",
        re.MULTILINE,
    )

    for block in blocks:
        m = _REALIZED_TAG_RE.search(block)
        realized = True
        if not m:
            m = pending_re.search(block)
            realized = False
            if not m:
                continue
        gd = m.groupdict()
        decision = _extract_after_tag(block, "DECISION:")
        reflection = _extract_after_tag(block, "REFLECTION:") if realized else ""
        closed_at: str | None = None
        if realized:
            try:
                days = int(re.match(r"(\d+)d", gd["holding"]).group(1))  # type: ignore[union-attr]
                closed_at = (
                    dt.date.fromisoformat(gd["open_date"]) + dt.timedelta(days=days)
                ).isoformat()
            except (ValueError, AttributeError):
                closed_at = None
        yield ReflectorEntry(
            open_date=gd["open_date"],
            closed_at=closed_at,
            ticker=gd["ticker"],
            rating=gd["rating"],
            pending=not realized,
            raw_pct=gd.get("raw_pct"),
            alpha_pct=gd.get("alpha_pct"),
            holding=gd.get("holding"),
            decision=decision,
            reflection=reflection,
        )


def _extract_after_tag(block: str, tag: str) -> str:
    """Pull the prose that follows ``DECISION:`` / ``REFLECTION:`` in a block.

    Stops at the next prefixed line (the only other prefixed line in our
    schema is the sibling tag) or end of block.
    """
    lines = block.splitlines()
    out: list[str] = []
    state = False
    for ln in lines:
        if ln.startswith(tag):
            state = True
            tail = ln[len(tag):].lstrip()
            if tail:
                out.append(tail)
            continue
        if state and (ln.startswith("DECISION:") or ln.startswith("REFLECTION:")):
            break
        if state:
            out.append(ln)
    return "\n".join(out).strip()


def reflector_example(entry: ReflectorEntry, *, target_date: dt.date) -> dict[str, Any] | None:
    """Build a raw training example for one reflector block.

    Yields ``None`` if the entry's ``closed_at`` doesn't match ``target_date``
    AND the entry isn't pending. Pending entries are always emitted (with
    ``pending_outcome=True``) so the operator has visibility into open trades
    via Stage 2's reject-reason histogram. Stage 2 itself filters pending out.
    """
    if entry.pending:
        if entry.open_date != target_date.isoformat():
            return None
    else:
        if entry.closed_at != target_date.isoformat():
            return None

    ledger = {
        "open_date": entry.open_date,
        "closed_at": entry.closed_at,
        "ticker": entry.ticker,
        "rating": entry.rating,
        "raw_pct": entry.raw_pct,
        "alpha_pct": entry.alpha_pct,
        "holding": entry.holding,
    }
    outcome_key = f"{entry.closed_at or entry.open_date}|{entry.ticker}"

    # The "instruction" for a reflector is the original DECISION thesis plus
    # the realized ledger. The "response" is the REFLECTION prose. For pending
    # entries the response is empty and Stage 2 will reject; we still write
    # the row so the next ingest pass can re-process when the trade closes.
    return {
        "ts": entry.closed_at or entry.open_date,
        "ticker": entry.ticker,
        "system_message": (
            "You are Shark's nightly reflector. Given the trade thesis and the "
            "realized outcome, write 2-4 sentences naming what worked, what "
            "missed, and one lesson to carry forward. Cite numeric values from "
            "the ledger."
        ),
        "user_message": (
            f"## Thesis ({entry.open_date} {entry.ticker} {entry.rating})\n"
            f"{entry.decision}\n\n"
            f"## Outcome\n"
            f"raw_pct={entry.raw_pct or 'n/a'} "
            f"alpha_pct={entry.alpha_pct or 'n/a'} "
            f"holding={entry.holding or 'n/a'}"
        ),
        "response": entry.reflection,
        "pending_outcome": entry.pending,
        "outcome_key": outcome_key,
        "ledger": ledger,
    }


# --------------------------------------------------------------------------- #
# Source 2 — llm-calls.jsonl (the other 5 roles)
# --------------------------------------------------------------------------- #

def iter_llm_calls(llm_calls_jsonl: Path) -> Iterator[dict[str, Any]]:
    """Stream JSONL records from the LLM tracker log.

    Bad lines are skipped silently; the upstream writer fsyncs each line so
    partial records are rare and not actionable here.
    """
    if not llm_calls_jsonl.exists():
        return
    with llm_calls_jsonl.open("r", encoding="utf-8") as fh:
        for line in fh:
            if not line.strip():
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue


def _record_date(record: dict[str, Any]) -> dt.date | None:
    """Extract UTC date from a tracker JSONL record's ``timestamp`` field."""
    ts = record.get("timestamp")
    if not isinstance(ts, str):
        return None
    try:
        return dt.datetime.fromisoformat(ts.replace("Z", "+00:00")).date()
    except ValueError:
        return None


def llm_call_example(record: dict[str, Any]) -> dict[str, Any] | None:
    """Convert one tracker JSONL record to a raw training example.

    Returns ``None`` when the record is missing the optional full-text payload
    (which only lands when ``SHARK_LLM_LOG_FULL_TEXT=1`` is set on the bot
    side). Without prompt/response the example has no training value.
    """
    prompt = record.get("prompt")
    response = record.get("response_text")
    if not isinstance(prompt, str) or not prompt.strip():
        return None
    if not isinstance(response, str) or not response.strip():
        return None

    system_message = record.get("system_message") or ""
    ts = record.get("timestamp") or ""

    # Best-effort ticker extraction. The tracker doesn't carry a ticker field,
    # so we rely on a regex over the prompt. Misses are fine — `ticker` is
    # informational only and Stage 2 doesn't gate on it.
    ticker = _guess_ticker(prompt) or _guess_ticker(system_message) or ""

    return {
        "ts": ts,
        "ticker": ticker,
        "system_message": system_message,
        "user_message": prompt,
        "response": response,
        "pending_outcome": False,  # bull/bear/arbiter responses are realized at emit time
        "outcome_key": f"{ts}|{record.get('agent', '?')}",
        "ledger": {
            "agent":              record.get("agent"),
            "model":              record.get("model"),
            "provider":           record.get("provider"),
            "tier":               record.get("tier"),
            "role":               record.get("role"),
            "latency_seconds":    record.get("latency_seconds"),
            "prompt_tokens":      record.get("prompt_tokens"),
            "completion_tokens":  record.get("completion_tokens"),
            "redacted_count":     record.get("redacted_count"),
            "valid":              record.get("valid", True),  # set by upstream pydantic-validation; default True for legacy rows
        },
    }


_TICKER_RE = re.compile(r"\b([A-Z]{2,5})(?:/USD)?\b")


def _guess_ticker(text: str) -> str | None:
    """Heuristic ticker extractor from a prompt/system message.

    Looks for the first 2-5 uppercase token after a ``Ticker:`` /
    ``Symbol:`` label, then falls back to any uppercase token between word
    boundaries (skipping common English words and known noise tokens).
    """
    if not text:
        return None
    labelled = re.search(r"(?:Ticker|Symbol|Pair)\s*[:=]\s*([A-Z]{2,5})", text)
    if labelled:
        return labelled.group(1)
    noise = {"YOU", "GIVEN", "THE", "AND", "OR", "FOR", "NOT", "USD", "API", "USE"}
    for match in _TICKER_RE.finditer(text):
        sym = match.group(1)
        if sym in noise:
            continue
        return sym
    return None


# --------------------------------------------------------------------------- #
# Idempotent writer
# --------------------------------------------------------------------------- #

def write_raw_jsonl(
    role: str,
    target_date: dt.date,
    examples: Iterable[dict[str, Any]],
    *,
    raw_root: Path,
) -> tuple[int, bool]:
    """Write ``examples`` to ``<raw_root>/<role>/<YYYYMMDD>.jsonl``.

    Returns:
        (count, was_skipped). ``was_skipped`` is ``True`` when the per-day
        file already exists (idempotent re-run). ``count`` is the number of
        examples written this run -- 0 on skip.
    """
    role_dir = raw_root / role
    role_dir.mkdir(parents=True, exist_ok=True)
    out_path = role_dir / f"{target_date.strftime('%Y%m%d')}.jsonl"

    if out_path.exists():
        return 0, True

    count = 0
    tmp = out_path.with_suffix(".jsonl.partial")
    try:
        with tmp.open("w", encoding="utf-8") as fh:
            for ex in examples:
                fh.write(json.dumps(ex, ensure_ascii=False) + "\n")
                count += 1
        # Atomic publish — `os.replace` is atomic on POSIX and Windows. Even
        # if the cron is killed mid-write the consumer never sees a half file.
        os.replace(tmp, out_path)
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass
    return count, False


# --------------------------------------------------------------------------- #
# Orchestration
# --------------------------------------------------------------------------- #

@dataclass
class IngestStats:
    """Per-run counts surfaced to stdout for cron log scraping."""

    target_date: dt.date
    accepted: dict[str, int] = field(default_factory=dict)
    skipped_existing: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def render(self) -> str:
        """One-line human-readable summary; safe for stdout."""
        parts = [f"modelforge-ingest date={self.target_date.isoformat()}"]
        for role in ALL_ROLES:
            n = self.accepted.get(role, 0)
            tag = f"{role}={n}"
            if role in self.skipped_existing:
                tag += "(skip)"
            parts.append(tag)
        if self.errors:
            parts.append(f"errors={len(self.errors)}")
        return " ".join(parts)


def ingest(
    target_date: dt.date,
    *,
    decisions_md: Path,
    llm_calls_jsonl: Path,
    raw_root: Path,
) -> IngestStats:
    """Run the full ingest for ``target_date``.

    Pure I/O and parsing -- never raises; per-role failures are recorded in
    the returned :class:`IngestStats` so the caller can choose to alert.
    """
    stats = IngestStats(target_date=target_date)

    # All six roles share one per_role bucket. trading-reflector is special —
    # it draws from BOTH decisions.md (deliberation log) and the JSONL log
    # (Shark's trade_reviewer post-mortems). Without merging, the JSONL
    # branch would KeyError on trade_reviewer rows and swallow as errors=1.
    per_role: dict[str, list[dict[str, Any]]] = {r: [] for r in ALL_ROLES}

    # --- Source 1: decisions.md → trading-reflector --------------------------
    try:
        for entry in iter_reflector_entries(decisions_md):
            ex = reflector_example(entry, target_date=target_date)
            if ex is not None:
                per_role["trading-reflector"].append(ex)
    except Exception as exc:  # pragma: no cover - defensive
        stats.errors.append(f"trading-reflector decisions.md: {exc}")

    # --- Source 2: llm-calls.jsonl → all six roles ---------------------------
    try:
        for record in iter_llm_calls(llm_calls_jsonl):
            if _record_date(record) != target_date:
                continue
            role = AGENT_TO_ROLE.get(str(record.get("agent") or "").strip())
            if role is None:
                continue
            ex = llm_call_example(record)
            if ex is None:
                continue
            per_role[role].append(ex)
    except Exception as exc:  # pragma: no cover - defensive
        stats.errors.append(f"llm-calls.jsonl read: {exc}")

    for role, examples in per_role.items():
        try:
            count, skipped = write_raw_jsonl(role, target_date, examples, raw_root=raw_root)
            stats.accepted[role] = count
            if skipped:
                stats.skipped_existing.append(role)
        except Exception as exc:  # pragma: no cover - defensive
            stats.errors.append(f"{role}: {exc}")

    return stats


# --------------------------------------------------------------------------- #
# Fail-soft logging
# --------------------------------------------------------------------------- #

def _log_error(msg: str) -> None:
    """Append a timestamped error to the ingest log; never raises."""
    try:
        path = _ingest_log_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(f"{dt.datetime.now(dt.timezone.utc).isoformat()} {msg}\n")
    except OSError:
        pass


def _resolve_default_decisions_md() -> Path:
    """Default location for the reflection log when no env override is set."""
    override = os.environ.get("SHARK_DECISIONS_MD", "").strip()
    if override:
        return Path(override)
    return _REPO_ROOT / "stocks" / "memory" / "decisions.md"


def _resolve_default_llm_calls_jsonl() -> Path:
    """Default location for the tracker JSONL when no env override is set."""
    override = os.environ.get("SHARK_TRACKER_LOG", "").strip()
    if override:
        return Path(override)
    return _REPO_ROOT / "stocks" / "memory" / "llm-calls.jsonl"


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def main(argv: list[str] | None = None) -> int:
    """CLI entry point; returns POSIX exit code (always 0, fail-soft).

    Errors are recorded to the ingest log; the only thing main() does is
    print a one-line summary and exit cleanly so cron stays quiet.
    """
    parser = argparse.ArgumentParser(description="ModelForge Stage 1 ingest")
    parser.add_argument("date", nargs="?", default=None, help="YYYY-MM-DD; defaults to yesterday UTC")
    parser.add_argument("--decisions-md", default=None, help="Override decisions.md path")
    parser.add_argument("--llm-calls", default=None, help="Override llm-calls.jsonl path")
    parser.add_argument("--raw-root", default=None, help="Override ~/.dgx-train/raw")
    parser.add_argument("--quiet", action="store_true", help="Suppress stdout summary")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")

    try:
        target = parse_target_date(args.date)
    except ValueError as exc:
        _log_error(f"bad date arg: {exc}")
        print(f"modelforge-ingest ERROR bad-date {exc}", file=sys.stderr)
        return 0

    decisions_md = Path(args.decisions_md) if args.decisions_md else _resolve_default_decisions_md()
    llm_calls_jsonl = Path(args.llm_calls) if args.llm_calls else _resolve_default_llm_calls_jsonl()
    raw_root = Path(args.raw_root) if args.raw_root else _raw_root()

    try:
        stats = ingest(
            target,
            decisions_md=decisions_md,
            llm_calls_jsonl=llm_calls_jsonl,
            raw_root=raw_root,
        )
    except Exception:  # pragma: no cover - defensive top-level catch
        _log_error("ingest crashed:\n" + traceback.format_exc())
        return 0

    for err in stats.errors:
        _log_error(err)

    if not args.quiet:
        print(stats.render())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
