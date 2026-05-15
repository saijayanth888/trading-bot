"""
Append-only markdown decision log — single source of truth for trade decisions.

This module owns `stocks/memory/decisions.md`. Pattern lifted from
TradingAgents `agents/utils/memory.py` (Apache-2.0): one block per decision,
"pending" tag flips to a realized tag (with raw / alpha / holding) after the
trade closes.

The log is markdown so an operator can read/grep/diff it; the parser is
line-streaming so it stays cheap as the file grows over many months.

Layered on top of `shark.memory.atomic`:
  - `atomic_write_text` ensures a crash never leaves a half-written file.
  - `file_lock` (advisory fcntl flock) ensures two crons writing at the same
    second don't interleave appends.

Public API
----------
- `append_decision(date, ticker, rating, thesis, *, log_path=None)`
- `update_with_outcome(date, ticker, pnl_pct, alpha_pct, holding_days,
       reflection, *, log_path=None)`
- `get_past_context(ticker, k_same_symbol=5, k_cross_symbol=3, *,
       log_path=None) -> str`
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Iterator

from shark.memory.atomic import atomic_write_text, file_lock

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Constants — keep at module level so they're easy to grep
# ---------------------------------------------------------------------------

# Default location of the log relative to the repo root. Resolved lazily so
# the module is importable from arbitrary cwds (cron runs, tests, REPL).
_DEFAULT_REL_PATH = Path("memory") / "decisions.md"

# Block separator. A markdown HR (`---`) is used because:
#   1. it can't appear inside a tag line, DECISION line, or REFLECTION line
#      (those start with `[`, `DECISION:`, `REFLECTION:`),
#   2. it renders as a horizontal rule in any markdown viewer, so the file
#      is still pleasant for an operator to scroll through.
_SEPARATOR = "---"

# Header lines we always preserve at the top of the file. The file may also
# contain operator-edited prose between the header and the first entry; we
# preserve everything up to the first decision tag.
_HEADER_TEMPLATE = (
    "# Decisions log — append-only\n"
    "\n"
    "One line per decision. Status flips from `pending` → realized after the "
    "trade closes (handled by stage/12-reflector cron).\n"
    "\n"
    "Format:\n"
    "[date | ticker | rating | pending]\n"
    "[date | ticker | rating | +X.X% | +Y.Y% alpha | <holding>]\n"
    "DECISION: <thesis 1-2 lines>\n"
    "REFLECTION: <2-4 sentences, filled in after close>\n"
    "---\n"
)

# Tag line: `[date | ticker | rating | pending]` or
#           `[date | ticker | rating | +X.X% | +Y.Y% alpha | Nd]`
# Captured fields are trimmed downstream.
_TAG_RE = re.compile(r"^\[(?P<inner>[^\[\]]+)\]\s*$")


# ---------------------------------------------------------------------------
# Path / lock helpers
# ---------------------------------------------------------------------------

def _resolve_log_path(log_path: Path | None) -> Path:
    """Return the absolute path to decisions.md, falling back to repo default.

    Resolution rules (first match wins):
      1. Explicit `log_path` arg.
      2. Walk up from this module's location looking for a `stocks/memory/`
         dir — handles installed packages and worktrees.
      3. Walk up from cwd looking for `stocks/memory/`.
      4. CWD-relative `memory/decisions.md` (matches CLAUDE.md convention).
    """
    if log_path is not None:
        return Path(log_path)

    # Walk up from this file
    here = Path(__file__).resolve()
    for parent in here.parents:
        candidate = parent / "memory" / "decisions.md"
        if candidate.parent.is_dir():
            return candidate
        candidate = parent / "stocks" / "memory" / "decisions.md"
        if candidate.parent.is_dir():
            return candidate

    # Fall back to cwd
    return Path.cwd() / _DEFAULT_REL_PATH


def _lock_path_for(log_path: Path) -> Path:
    """Sibling lock file: `.decisions.md.lock` next to the log."""
    return log_path.parent / f".{log_path.name}.lock"


# ---------------------------------------------------------------------------
# Streaming parser
# ---------------------------------------------------------------------------

def _iter_entries(log_path: Path) -> Iterator[dict]:
    """Yield one dict per decision block — line-streaming, O(1) memory.

    Block boundaries are blank lines or `---` separators. Each yielded dict has:
        date:        str
        ticker:      str
        rating:      str        ("BUY", "SELL", "WAIT", etc.)
        pending:     bool
        raw_pct:     str | None
        alpha_pct:   str | None
        holding:     str | None ("3d", etc.)
        decision:    str        (the DECISION: prose)
        reflection:  str        ("" if still pending)
        tag_line:    str        (raw, for in-place rewrite)
        block_lines: list[str]  (raw lines including tag line)
    """
    if not log_path.exists():
        return

    with log_path.open("r", encoding="utf-8") as f:
        block: list[str] = []
        for line in f:
            stripped = line.rstrip("\n")
            if stripped.strip() == _SEPARATOR:
                if block:
                    parsed = _parse_block(block)
                    if parsed:
                        yield parsed
                    block = []
                continue
            block.append(stripped)
        if block:
            parsed = _parse_block(block)
            if parsed:
                yield parsed


def _parse_block(lines: list[str]) -> dict | None:
    """Parse one block of lines into an entry dict, or None for non-entries
    (e.g. the file header)."""
    # Find the tag line: first non-blank line that matches [ ... ]
    tag_idx = None
    for i, ln in enumerate(lines):
        if not ln.strip():
            continue
        m = _TAG_RE.match(ln)
        if m:
            tag_idx = i
            break
        # First non-blank, non-tag line — this is header prose, not an entry
        return None

    if tag_idx is None:
        return None

    tag_line = lines[tag_idx]
    inner = _TAG_RE.match(tag_line).group("inner")
    fields = [f.strip() for f in inner.split("|")]
    if len(fields) < 4:
        return None

    pending = fields[3].lower() == "pending"
    entry: dict = {
        "date": fields[0],
        "ticker": fields[1],
        "rating": fields[2],
        "pending": pending,
        "raw_pct": None if pending else fields[3],
        # alpha is suffixed with " alpha" in the realized form
        "alpha_pct": (fields[4] if not pending and len(fields) > 4 else None),
        "holding": (fields[5] if not pending and len(fields) > 5 else None),
        "tag_line": tag_line,
    }

    decision_parts: list[str] = []
    reflection_parts: list[str] = []
    state = None
    for ln in lines[tag_idx + 1 :]:
        if ln.startswith("DECISION:"):
            state = "decision"
            tail = ln[len("DECISION:") :].lstrip()
            if tail:
                decision_parts.append(tail)
            continue
        if ln.startswith("REFLECTION:"):
            state = "reflection"
            tail = ln[len("REFLECTION:") :].lstrip()
            if tail:
                reflection_parts.append(tail)
            continue
        if state == "decision":
            decision_parts.append(ln)
        elif state == "reflection":
            reflection_parts.append(ln)

    entry["decision"] = "\n".join(decision_parts).strip()
    entry["reflection"] = "\n".join(reflection_parts).strip()
    entry["block_lines"] = lines
    return entry


# ---------------------------------------------------------------------------
# Append: pending decision
# ---------------------------------------------------------------------------

def _ensure_header(log_path: Path) -> None:
    """Create the file (with header) if it does not exist."""
    if log_path.exists():
        return
    atomic_write_text(log_path, _HEADER_TEMPLATE)


def append_decision(
    date: str,
    ticker: str,
    rating: str,
    thesis: str,
    *,
    log_path: Path | None = None,
) -> None:
    """Append a `pending` decision block to decisions.md.

    Idempotent: if a `pending` block for (date, ticker) already exists, this
    is a no-op so a re-run cron doesn't double-log.

    Concurrency: holds an exclusive flock on `<log>.lock` for the read+append
    window so simultaneous crons cannot interleave their writes.
    """
    path = _resolve_log_path(log_path)
    path.parent.mkdir(parents=True, exist_ok=True)

    rating_clean = rating.strip().upper() or "WAIT"
    thesis_clean = thesis.strip().replace("\r\n", "\n")

    with file_lock(_lock_path_for(path)):
        _ensure_header(path)

        # Idempotency check — line-streaming scan, no full parse
        pending_marker = f"[{date} | {ticker} | "
        with path.open("r", encoding="utf-8") as f:
            for line in f:
                s = line.strip()
                if s.startswith(pending_marker) and s.endswith("| pending]"):
                    logger.debug(
                        "decisions.md already has pending entry for %s %s — skip",
                        date, ticker,
                    )
                    return

        block = (
            f"\n[{date} | {ticker} | {rating_clean} | pending]\n"
            f"DECISION: {thesis_clean}\n"
            f"REFLECTION: \n"
            f"---\n"
        )
        # Append by reading + atomic-rewriting. Cheap because the lock guarantees
        # we're the sole writer; a true O(1) append would lose crash safety.
        existing = path.read_text(encoding="utf-8")
        if not existing.endswith("\n"):
            existing += "\n"
        atomic_write_text(path, existing + block)


# ---------------------------------------------------------------------------
# Update: realize a pending decision with outcome
# ---------------------------------------------------------------------------

def _format_pct(value: float) -> str:
    """`+1.2%` / `-0.4%` — always signed, one decimal."""
    return f"{value:+.1f}%"


def update_with_outcome(
    date: str,
    ticker: str,
    pnl_pct: float,
    alpha_pct: float,
    holding_days: int,
    reflection: str,
    *,
    log_path: Path | None = None,
) -> bool:
    """Find the `pending` block for (date, ticker) and rewrite it with the
    realized outcome + reflection.

    Returns:
        True  — the pending block was found and rewritten.
        False — no matching pending block (already realized, or never logged).

    Idempotent: refuses to rewrite a block that is already realized.
    Atomic: uses temp-file + os.replace via `atomic_write_text`.
    """
    path = _resolve_log_path(log_path)
    if not path.exists():
        return False

    reflection_clean = reflection.strip().replace("\r\n", "\n")
    raw_str = _format_pct(pnl_pct)
    alpha_str = f"{_format_pct(alpha_pct)} alpha"
    holding_str = f"{int(holding_days)}d"

    with file_lock(_lock_path_for(path)):
        # Re-read inside the lock so we never act on stale bytes
        text = path.read_text(encoding="utf-8")

        # Split by `---` lines while preserving the rest of the file structure.
        # We rebuild the file as `header + entries`, where the header is
        # everything before the first decision tag.
        lines = text.split("\n")

        # Walk blocks: every chunk between `---` lines is one block. The
        # header is the first chunk that contains no tag line.
        blocks: list[list[str]] = []
        current: list[str] = []
        for ln in lines:
            if ln.strip() == _SEPARATOR:
                blocks.append(current)
                current = []
                blocks.append([_SEPARATOR])  # preserve the separator
            else:
                current.append(ln)
        if current:
            blocks.append(current)

        rewrote = False
        for idx, block in enumerate(blocks):
            if block == [_SEPARATOR]:
                continue
            entry = _parse_block(block)
            if entry is None:
                continue
            if entry["date"] != date or entry["ticker"] != ticker:
                continue
            if not entry["pending"]:
                # Idempotency: already realized — refuse to clobber
                logger.info(
                    "decisions.md: %s %s already realized — refusing to rewrite",
                    date, ticker,
                )
                return False

            # Rewrite this block with realized tag + REFLECTION
            new_tag = (
                f"[{date} | {ticker} | {entry['rating']} | "
                f"{raw_str} | {alpha_str} | {holding_str}]"
            )
            new_block = [
                new_tag,
                f"DECISION: {entry['decision']}" if entry["decision"] else "DECISION:",
                f"REFLECTION: {reflection_clean}",
            ]
            blocks[idx] = new_block
            rewrote = True
            break

        if not rewrote:
            return False

        # Reassemble. `blocks` already contains [_SEPARATOR] entries in the
        # right places, so we just join blocks with "\n" and lines within a
        # block with "\n".
        out_lines: list[str] = []
        for b in blocks:
            out_lines.extend(b)
        new_text = "\n".join(out_lines)
        if not new_text.endswith("\n"):
            new_text += "\n"
        atomic_write_text(path, new_text)
        return True


# ---------------------------------------------------------------------------
# Read: format past lessons for prompt injection
# ---------------------------------------------------------------------------

def get_past_context(
    ticker: str,
    k_same_symbol: int = 5,
    k_cross_symbol: int = 3,
    *,
    log_path: Path | None = None,
) -> str:
    """Return a prompt-injectable markdown block of past *realized* lessons.

    Layout:
        ## Past lessons for NVDA
        [2026-04-15] +0.8% +1.2% alpha | 3d
          REFLECTION: ...

        ## Past cross-symbol lessons
        [2026-04-22 AMD] +2.1% +0.3% alpha | 5d
          REFLECTION: ...

    Pending entries and entries with empty reflections are skipped.
    Returns "" when the log is empty or no realized entries exist.
    """
    path = _resolve_log_path(log_path)
    if not path.exists():
        return ""

    same: list[dict] = []
    cross: list[dict] = []

    # Stream the whole file; we need to keep at most (k_same + k_cross) entries
    # per symbol bucket. Memory is bounded by k_*, not by file size.
    for entry in _iter_entries(path):
        if entry["pending"] or not entry["reflection"]:
            continue
        if entry["ticker"] == ticker:
            same.append(entry)
            if len(same) > k_same_symbol:
                same.pop(0)
        else:
            cross.append(entry)
            if len(cross) > k_cross_symbol:
                cross.pop(0)

    if not same and not cross:
        return ""

    parts: list[str] = []

    if same:
        parts.append(f"## Past lessons for {ticker}")
        # Most recent first
        for e in reversed(same):
            tag = (
                f"[{e['date']}] "
                f"{e['raw_pct'] or 'n/a'} {e['alpha_pct'] or 'n/a alpha'} "
                f"| {e['holding'] or 'n/a'}"
            )
            parts.append(tag)
            parts.append(f"  REFLECTION: {e['reflection']}")
            parts.append("")  # blank between entries

    if cross:
        parts.append("## Past cross-symbol lessons")
        for e in reversed(cross):
            tag = (
                f"[{e['date']} {e['ticker']}] "
                f"{e['raw_pct'] or 'n/a'} {e['alpha_pct'] or 'n/a alpha'} "
                f"| {e['holding'] or 'n/a'}"
            )
            parts.append(tag)
            parts.append(f"  REFLECTION: {e['reflection']}")
            parts.append("")

    return "\n".join(parts).rstrip() + "\n"


__all__ = [
    "append_decision",
    "update_with_outcome",
    "get_past_context",
]
