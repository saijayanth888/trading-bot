"""
LLM-call log rotation.

Source of truth: ``stocks/memory/llm-calls.jsonl`` (produced by
``shark.llm.tracker.LLMTracker._append_jsonl``).

Why a rotator at all?
---------------------
With ``SHARK_LLM_LOG_FULL_TEXT=1`` each line balloons from ~250 bytes to
~1 KB (full prompt + response). At ~200 calls/day that's ~6 MB/month —
fine in steady state, but operator wants a hard cap so a runaway burst
(reflector going chatty, debate looping) can't fill the disk.

Two rotation triggers — either fires:
  1. Size > 50 MB
  2. Age > 30 days since the first record in the file

After rotation we keep the last 90 days of ``llm-calls.YYYY-MM-DD.jsonl.gz``
archives and delete anything older.

This module is callable both as a library (the dashboard's
``/api/ops/llm_calls/<id>`` endpoint imports ``find_record_in_archives``
when a non-current record_id is requested) AND as a CLI shim that the
Hermes cron at 03:00 ET nightly invokes:

    python -m shark.llm.rotate

Exit code is always 0 on success — failures log a warning and return
without trying to roll back; rotation is best-effort and the next nightly
run will retry.
"""

from __future__ import annotations

import gzip
import json
import logging
import os
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Thresholds — overridable via env so tests can use tiny limits
# ---------------------------------------------------------------------------
DEFAULT_SIZE_LIMIT_BYTES = 50 * 1024 * 1024  # 50 MB
DEFAULT_AGE_LIMIT_DAYS = 30
DEFAULT_RETENTION_DAYS = 90


def _resolve_log_path() -> Path:
    """Mirror tracker._resolve_log_path so the rotator and writer agree."""
    override = os.environ.get("SHARK_TRACKER_LOG", "").strip()
    if override:
        return Path(override)
    return Path(__file__).resolve().parents[2] / "memory" / "llm-calls.jsonl"


# ---------------------------------------------------------------------------
# Predicates
# ---------------------------------------------------------------------------


def file_size_bytes(path: Path) -> int:
    try:
        return path.stat().st_size if path.is_file() else 0
    except OSError:
        return 0


def first_record_timestamp(path: Path) -> datetime | None:
    """Read the FIRST valid JSON line and return its timestamp (UTC).

    Used to decide age-based rotation. Reading just the first line means
    we don't have to scan a multi-megabyte file just to answer "is this
    older than 30 days?".
    """
    if not path.is_file():
        return None
    try:
        with path.open("r", encoding="utf-8") as fh:
            for line in fh:
                if not line.strip():
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                ts_str = rec.get("timestamp")
                if not ts_str:
                    return None
                try:
                    return datetime.fromisoformat(str(ts_str).replace("Z", "+00:00"))
                except (ValueError, TypeError):
                    return None
    except OSError:
        return None
    return None


def should_rotate(
    path: Path,
    *,
    size_limit_bytes: int = DEFAULT_SIZE_LIMIT_BYTES,
    age_limit_days: int = DEFAULT_AGE_LIMIT_DAYS,
    now: datetime | None = None,
) -> tuple[bool, str]:
    """Return ``(yes/no, reason)``. The reason string is logged so the
    operator can see WHICH trigger fired in the cron log."""
    if not path.is_file():
        return False, "no file"
    size = file_size_bytes(path)
    if size > size_limit_bytes:
        return True, f"size {size} > {size_limit_bytes}"
    first_ts = first_record_timestamp(path)
    if first_ts is not None:
        cur = now or datetime.now(timezone.utc)
        age = cur - first_ts
        if age > timedelta(days=age_limit_days):
            return True, f"age {age.days}d > {age_limit_days}d"
    return False, f"size {size}B, first_ts {first_ts}"


# ---------------------------------------------------------------------------
# Rotation
# ---------------------------------------------------------------------------


def archive_path_for(path: Path, when: datetime | None = None) -> Path:
    """Build the rotated archive path: ``llm-calls.YYYY-MM-DD.jsonl.gz``.

    If a same-day archive already exists (e.g. two rotations in one
    day) we suffix ``.N`` to avoid clobbering.
    """
    stamp = (when or datetime.now(timezone.utc)).strftime("%Y-%m-%d")
    base = path.with_name(f"llm-calls.{stamp}.jsonl.gz")
    if not base.exists():
        return base
    i = 1
    while True:
        cand = path.with_name(f"llm-calls.{stamp}.{i}.jsonl.gz")
        if not cand.exists():
            return cand
        i += 1


def rotate_file(path: Path, *, when: datetime | None = None) -> Path | None:
    """Gzip the live file into a dated archive, then truncate the original.

    Truncate (rather than delete + recreate) so the writer's open file
    descriptors (if any) don't end up pointing at a deleted inode. The
    writer is append-only so a truncated-to-zero file is just an empty
    log that gets new records on the next call.

    Returns the archive Path on success, or None if there was nothing to
    rotate.
    """
    if not path.is_file() or file_size_bytes(path) == 0:
        logger.info("rotate: nothing to rotate at %s", path)
        return None
    archive = archive_path_for(path, when=when)
    try:
        # Stream rather than read-all-then-write to keep peak RAM low.
        with path.open("rb") as src, gzip.open(archive, "wb") as dst:
            shutil.copyfileobj(src, dst)
        # Truncate the live file in-place so any open writer continues
        # appending to the same inode.
        with path.open("r+b") as fh:
            fh.truncate(0)
        logger.info("rotate: archived %s → %s", path, archive)
        return archive
    except OSError as exc:
        logger.warning("rotate: failed for %s: %s", path, exc)
        return None


def list_archives(path: Path) -> list[Path]:
    """All ``llm-calls.*.jsonl.gz`` siblings of the live file, oldest first.

    Sorting by name works because the date stamp is the prefix (YYYY-MM-DD).
    """
    parent = path.parent
    if not parent.is_dir():
        return []
    return sorted(parent.glob("llm-calls.*.jsonl.gz"))


def prune_archives(
    path: Path,
    *,
    retention_days: int = DEFAULT_RETENTION_DAYS,
    now: datetime | None = None,
) -> list[Path]:
    """Delete archives older than ``retention_days`` based on the date
    embedded in the filename. Returns the list of deleted paths."""
    cur = now or datetime.now(timezone.utc)
    cutoff = cur - timedelta(days=retention_days)
    deleted: list[Path] = []
    for arch in list_archives(path):
        # Filename is llm-calls.YYYY-MM-DD[.N].jsonl.gz
        stem = arch.name.removeprefix("llm-calls.")
        date_part = stem.split(".", 1)[0]  # "YYYY-MM-DD"
        try:
            ts = datetime.strptime(date_part, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        except ValueError:
            continue
        if ts < cutoff:
            try:
                arch.unlink()
                deleted.append(arch)
                logger.info("rotate: pruned old archive %s", arch)
            except OSError as exc:
                logger.warning("rotate: failed to prune %s: %s", arch, exc)
    return deleted


# ---------------------------------------------------------------------------
# Reading archives (for /api/ops/llm_calls/<id> when the record is gone
# from the live file)
# ---------------------------------------------------------------------------


def iter_archive_records(arch: Path) -> Iterable[dict]:
    """Yield one dict per line from a gzipped archive. Malformed lines
    are skipped silently — log files are append-only and may have a
    trailing partial line if the process was killed mid-write."""
    try:
        with gzip.open(arch, "rt", encoding="utf-8") as fh:
            for line in fh:
                if not line.strip():
                    continue
                try:
                    yield json.loads(line)
                except json.JSONDecodeError:
                    continue
    except OSError:
        return


def find_record_in_archives(path: Path, timestamp: str) -> tuple[dict | None, Path | None]:
    """Search the archives newest-first for the record whose ``timestamp``
    field equals the given string.

    Returns ``(record, archive_path)``. If the record isn't found the
    ``archive_path`` is the newest archive that *might* have contained
    it — so the dashboard's 410 response can hint where to grep.
    """
    archives = list_archives(path)
    if not archives:
        return None, None
    for arch in reversed(archives):  # newest first
        for rec in iter_archive_records(arch):
            if str(rec.get("timestamp")) == timestamp:
                return rec, arch
    return None, archives[-1]  # newest = best grep hint


# ---------------------------------------------------------------------------
# CLI entry
# ---------------------------------------------------------------------------


def run(
    *,
    log_path: Path | None = None,
    size_limit_bytes: int = DEFAULT_SIZE_LIMIT_BYTES,
    age_limit_days: int = DEFAULT_AGE_LIMIT_DAYS,
    retention_days: int = DEFAULT_RETENTION_DAYS,
    now: datetime | None = None,
) -> dict:
    """High-level rotate + prune. Returns a summary dict the caller
    (or the cron's stdout) can log."""
    path = log_path or _resolve_log_path()
    rotated = None
    do_rotate, reason = should_rotate(
        path, size_limit_bytes=size_limit_bytes,
        age_limit_days=age_limit_days, now=now,
    )
    if do_rotate:
        rotated = rotate_file(path, when=now)
    pruned = prune_archives(path, retention_days=retention_days, now=now)
    return {
        "log_path": str(path),
        "rotated_to": str(rotated) if rotated else None,
        "reason": reason,
        "rotated": bool(rotated),
        "pruned_count": len(pruned),
        "pruned": [str(p) for p in pruned],
    }


if __name__ == "__main__":  # pragma: no cover — exercised by the cron
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    summary = run()
    print(json.dumps(summary, indent=2))
