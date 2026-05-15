#!/usr/bin/env python3
"""archive_shark_memory — move stale entries from shark memory files to *-ARCHIVE.md.

Why:
    Shark's CONTEXT-BRIEFING auto-generator targets ~4000 tokens. With each
    phase appending to TRADE-LOG.md / RESEARCH-LOG.md / SIGNAL-LOG.md the
    pool drifts past 12000 tokens within ~2 weeks, triggering the
    "memory files exceed safe threshold" warning on every phase. Bloated
    context degrades LLM decision quality. The shark/CLAUDE.md anti-bloat
    rules call for:

      - TRADE-LOG.md         older than 30 days → TRADE-LOG-ARCHIVE.md
      - RESEARCH-LOG.md      older than  7 days → RESEARCH-LOG-ARCHIVE.md
      - LESSONS-LEARNED.md   keep only last 20  → LESSONS-LEARNED-ARCHIVE.md

    SIGNAL-LOG.md isn't in the doc but is the biggest single file
    (38KB at audit time, mostly HTML email bodies). We archive entries
    older than 7 days there too.

Behaviour:
    - Idempotent: safe to run repeatedly. Re-running with no stale
      entries is a no-op.
    - Append-only on archive files (never truncate them).
    - Atomic on the source file (write to .tmp then rename).
    - Returns exit 0 on success, 1 if any file failed to parse (caller
      should alert).

Cron schedule (Hermes):
    0 21 * * *   — every night at 21:00 UTC (after shark daily-summary)
"""
from __future__ import annotations

import re
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
MEM_DIR = REPO / "stocks" / "memory"

# (source, archive, retention_days, header_regex_picking_block_dates).
# header_regex must yield a YYYY-MM-DD on each entry-start line. We split
# on those lines and keep blocks whose date is within retention_days of
# `now`. None = no date-based archiving (count-based for LESSONS-LEARNED).
ARCHIVE_CONFIGS: list[tuple[str, str, int | None, re.Pattern | None]] = [
    ("TRADE-LOG.md",       "TRADE-LOG-ARCHIVE.md",       30,
        re.compile(r"^\s*##\s+.*?(\d{4}-\d{2}-\d{2})", re.MULTILINE)),
    ("RESEARCH-LOG.md",    "RESEARCH-LOG-ARCHIVE.md",     7,
        re.compile(r"^\s*##\s+.*?(\d{4}-\d{2}-\d{2})", re.MULTILINE)),
    ("SIGNAL-LOG.md",      "SIGNAL-LOG-ARCHIVE.md",       7,
        re.compile(r"^\s*##\s+Shark\s+.*?(\d{4}-\d{2}-\d{2})", re.MULTILINE)),
    ("LESSONS-LEARNED.md", "LESSONS-LEARNED-ARCHIVE.md",  None,
        re.compile(r"^\s*##\s+", re.MULTILINE)),  # count-based: last 20
]
LESSONS_KEEP_LAST_N = 20


def _split_into_blocks(text: str, header_re: re.Pattern) -> list[tuple[str | None, str]]:
    """Split text on lines matching header_re. Each block carries its date.

    Returns [(date_str_or_None, block_text), ...] in original order.
    The first block (preamble before any header match) gets date=None.
    """
    matches = list(header_re.finditer(text))
    if not matches:
        return [(None, text)] if text.strip() else []
    blocks: list[tuple[str | None, str]] = []
    # Preamble before first header.
    if matches[0].start() > 0:
        preamble = text[: matches[0].start()]
        if preamble.strip():
            blocks.append((None, preamble))
    for i, m in enumerate(matches):
        start = m.start()
        end = matches[i + 1].start() if (i + 1) < len(matches) else len(text)
        block_text = text[start:end]
        # The pattern's capture group is the date (if any).
        date_str = m.group(1) if m.groups() else None
        blocks.append((date_str, block_text))
    return blocks


def _archive_by_date(
    src: Path, archive: Path, retention_days: int, header_re: re.Pattern,
) -> dict:
    """Move blocks older than `retention_days` from src into archive."""
    summary = {"src": src.name, "moved": 0, "kept": 0, "ok": True}
    if not src.exists():
        summary["ok"] = False
        summary["error"] = "source file missing"
        return summary

    text = src.read_text(encoding="utf-8", errors="replace")
    blocks = _split_into_blocks(text, header_re)
    cutoff = (datetime.now(UTC) - timedelta(days=retention_days)).date()

    kept: list[str] = []
    moved: list[str] = []
    for date_str, block in blocks:
        if date_str is None:
            kept.append(block)
            continue
        try:
            block_date = datetime.strptime(date_str, "%Y-%m-%d").date()
        except ValueError:
            kept.append(block)
            continue
        if block_date < cutoff:
            moved.append(block)
        else:
            kept.append(block)

    if not moved:
        summary["moved"] = 0
        summary["kept"] = len(blocks)
        return summary

    # Append-write archive.
    archive_body = "".join(moved)
    if archive.exists():
        with archive.open("a", encoding="utf-8") as fh:
            fh.write(archive_body)
    else:
        archive.write_text(archive_body, encoding="utf-8")

    # Atomic write of trimmed source.
    new_src = "".join(kept)
    tmp = src.with_suffix(src.suffix + ".tmp")
    tmp.write_text(new_src, encoding="utf-8")
    tmp.replace(src)

    summary["moved"] = len(moved)
    summary["kept"] = len(kept)
    return summary


def _archive_by_count(
    src: Path, archive: Path, keep_last_n: int, header_re: re.Pattern,
) -> dict:
    """Keep only the LAST keep_last_n blocks in src; move the rest to archive."""
    summary = {"src": src.name, "moved": 0, "kept": 0, "ok": True}
    if not src.exists():
        summary["ok"] = False
        summary["error"] = "source file missing"
        return summary

    text = src.read_text(encoding="utf-8", errors="replace")
    blocks = _split_into_blocks(text, header_re)
    if len(blocks) <= keep_last_n + 1:  # +1 for the optional preamble
        summary["kept"] = len(blocks)
        return summary

    # The preamble (date=None) always stays.
    preamble = [b for b in blocks if b[0] is None]
    dated = [b for b in blocks if b[0] is not None]

    moved = dated[:-keep_last_n] if len(dated) > keep_last_n else []
    kept = dated[-keep_last_n:] if len(dated) > keep_last_n else dated

    if not moved:
        summary["kept"] = len(blocks)
        return summary

    archive_body = "".join(b for _, b in moved)
    if archive.exists():
        with archive.open("a", encoding="utf-8") as fh:
            fh.write(archive_body)
    else:
        archive.write_text(archive_body, encoding="utf-8")

    new_src = "".join(b for _, b in preamble) + "".join(b for _, b in kept)
    tmp = src.with_suffix(src.suffix + ".tmp")
    tmp.write_text(new_src, encoding="utf-8")
    tmp.replace(src)

    summary["moved"] = len(moved)
    summary["kept"] = len(preamble) + len(kept)
    return summary


def main() -> int:
    results: list[dict] = []
    fatal = False
    for src_name, arch_name, retention_days, header_re in ARCHIVE_CONFIGS:
        src = MEM_DIR / src_name
        archive = MEM_DIR / arch_name
        try:
            if retention_days is None:
                r = _archive_by_count(src, archive, LESSONS_KEEP_LAST_N, header_re)
            else:
                r = _archive_by_date(src, archive, retention_days, header_re)
        except Exception as exc:  # noqa: BLE001
            r = {"src": src_name, "ok": False, "error": str(exc)}
        results.append(r)
        if not r.get("ok", True):
            fatal = True
        moved = r.get("moved", 0)
        kept = r.get("kept", 0)
        print(f"  {src_name:25s}  moved={moved}  kept={kept}  ok={r.get('ok', True)}")

    # Total bytes post-trim for the operator
    total_bytes = sum(
        (MEM_DIR / src).stat().st_size for src, *_ in ARCHIVE_CONFIGS
        if (MEM_DIR / src).exists()
    )
    print(f"\nTotal memory bytes after archive: {total_bytes} (target: <40KB)")
    return 1 if fatal else 0


if __name__ == "__main__":
    sys.exit(main())
