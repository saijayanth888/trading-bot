"""Tests for stocks.shark.llm.rotate.

Covers:
  - Size-based rotation: 51 MB triggers, 49 MB doesn't
  - Age-based rotation: 31-day-old file triggers regardless of size
  - Retention pruning: archives older than 90d are deleted
  - find_record_in_archives: locates a record across multiple gzipped archives
  - rotate_file truncates in place (preserves the inode)

Run from stocks/:
    pytest tests/test_llm_rotation.py -v
"""

from __future__ import annotations

import gzip
import json
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from shark.llm import rotate as rot


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r) + "\n")


def _rec(ts: datetime, agent: str = "t1", n: int = 1) -> dict:
    return {
        "agent": agent,
        "model": "hermes3:8b",
        "provider": "ollama",
        "tier": "fast",
        "role": "default",
        "latency_seconds": 1.0,
        "prompt_tokens": 100,
        "completion_tokens": 50,
        "timestamp": ts.isoformat(),
    }


# ---------------------------------------------------------------------------
# Size-based rotation
# ---------------------------------------------------------------------------


class TestSizeTrigger:
    def test_under_limit_does_not_rotate(self, tmp_path: Path):
        log = tmp_path / "llm-calls.jsonl"
        _write_jsonl(log, [_rec(datetime.now(timezone.utc))])
        do, reason = rot.should_rotate(log, size_limit_bytes=1024 * 1024)
        assert do is False
        assert "size" in reason

    def test_over_size_triggers(self, tmp_path: Path):
        log = tmp_path / "llm-calls.jsonl"
        # Write 51 KB of data with a 50 KB limit — same shape as 51 MB / 50 MB.
        rows = []
        now = datetime.now(timezone.utc)
        # Each record is ~200 bytes; we need ~260 to top 51 KB.
        for i in range(300):
            rows.append(_rec(now, agent="t" + str(i)))
        _write_jsonl(log, rows)
        # Choose a limit that's definitely smaller than the file we wrote.
        size = log.stat().st_size
        assert size > 50_000, f"test setup wrote only {size}B"
        do, reason = rot.should_rotate(log, size_limit_bytes=50_000)
        assert do is True
        assert "size" in reason

    def test_just_under_limit_no_rotate(self, tmp_path: Path):
        """Bytes-exact: a file of 1000 bytes with a 1500-byte limit should
        NOT trigger size rotation."""
        log = tmp_path / "llm-calls.jsonl"
        log.write_text("x" * 1000)
        # No JSON ⇒ no timestamp ⇒ no age trigger either.
        do, reason = rot.should_rotate(log, size_limit_bytes=1500)
        assert do is False


# ---------------------------------------------------------------------------
# Age-based rotation
# ---------------------------------------------------------------------------


class TestAgeTrigger:
    def test_31_day_old_triggers(self, tmp_path: Path):
        log = tmp_path / "llm-calls.jsonl"
        old = datetime.now(timezone.utc) - timedelta(days=31)
        _write_jsonl(log, [_rec(old, agent="ancient")])
        do, reason = rot.should_rotate(log, age_limit_days=30)
        assert do is True
        assert "age" in reason

    def test_29_day_old_no_rotate(self, tmp_path: Path):
        log = tmp_path / "llm-calls.jsonl"
        recent = datetime.now(timezone.utc) - timedelta(days=29)
        _write_jsonl(log, [_rec(recent, agent="fresh")])
        do, _reason = rot.should_rotate(log, age_limit_days=30)
        assert do is False

    def test_no_first_record_no_age_trigger(self, tmp_path: Path):
        log = tmp_path / "llm-calls.jsonl"
        # Garbage JSON-but-no-timestamp records
        log.write_text(json.dumps({"agent": "ok"}) + "\n")
        do, reason = rot.should_rotate(log, age_limit_days=1)
        assert do is False
        # File is also tiny so the size trigger is also off.
        assert "first_ts None" in reason


# ---------------------------------------------------------------------------
# rotate_file
# ---------------------------------------------------------------------------


class TestRotateFile:
    def test_rotates_and_truncates_in_place(self, tmp_path: Path):
        log = tmp_path / "llm-calls.jsonl"
        rows = [_rec(datetime.now(timezone.utc), agent="t" + str(i)) for i in range(5)]
        _write_jsonl(log, rows)
        inode_before = log.stat().st_ino

        archive = rot.rotate_file(log)
        assert archive is not None
        assert archive.exists()
        # Archive is a valid gzip with the same number of lines.
        with gzip.open(archive, "rt") as fh:
            archived_rows = [json.loads(l) for l in fh if l.strip()]
        assert len(archived_rows) == 5
        # Live file truncated to zero bytes but inode preserved.
        assert log.exists()
        assert log.stat().st_size == 0
        assert log.stat().st_ino == inode_before

    def test_rotate_no_op_when_empty(self, tmp_path: Path):
        log = tmp_path / "llm-calls.jsonl"
        log.touch()
        assert rot.rotate_file(log) is None

    def test_rotate_no_op_when_missing(self, tmp_path: Path):
        log = tmp_path / "llm-calls.jsonl"
        assert rot.rotate_file(log) is None

    def test_archive_path_uniqueness(self, tmp_path: Path):
        log = tmp_path / "llm-calls.jsonl"
        _write_jsonl(log, [_rec(datetime.now(timezone.utc))])
        archive_a = rot.rotate_file(log)
        # Second rotation in the same day — must not clobber.
        _write_jsonl(log, [_rec(datetime.now(timezone.utc), agent="round2")])
        archive_b = rot.rotate_file(log)
        assert archive_a is not None and archive_b is not None
        assert archive_a != archive_b


# ---------------------------------------------------------------------------
# prune_archives
# ---------------------------------------------------------------------------


class TestPrune:
    def _make_archive(self, parent: Path, days_ago: int) -> Path:
        """Create a fake archive file with the given date in its name."""
        stamp = (datetime.now(timezone.utc) - timedelta(days=days_ago)).strftime("%Y-%m-%d")
        arch = parent / f"llm-calls.{stamp}.jsonl.gz"
        with gzip.open(arch, "wt") as fh:
            fh.write(json.dumps(_rec(datetime.now(timezone.utc))) + "\n")
        return arch

    def test_old_archives_deleted(self, tmp_path: Path):
        log = tmp_path / "llm-calls.jsonl"
        log.touch()
        keep = self._make_archive(tmp_path, days_ago=10)
        gone = self._make_archive(tmp_path, days_ago=120)
        deleted = rot.prune_archives(log, retention_days=90)
        assert gone in deleted
        assert keep not in deleted
        assert keep.exists()
        assert not gone.exists()

    def test_no_deletes_when_all_fresh(self, tmp_path: Path):
        log = tmp_path / "llm-calls.jsonl"
        log.touch()
        self._make_archive(tmp_path, days_ago=5)
        self._make_archive(tmp_path, days_ago=20)
        deleted = rot.prune_archives(log, retention_days=90)
        assert deleted == []


# ---------------------------------------------------------------------------
# Archive search
# ---------------------------------------------------------------------------


class TestArchiveSearch:
    def test_find_record_in_recent_archive(self, tmp_path: Path):
        log = tmp_path / "llm-calls.jsonl"
        log.touch()
        target_ts = "2026-04-15T10:00:00.000000+00:00"
        target = {**_rec(datetime.now(timezone.utc)), "timestamp": target_ts, "agent": "target"}
        stamp = "2026-04-15"
        arch = tmp_path / f"llm-calls.{stamp}.jsonl.gz"
        with gzip.open(arch, "wt") as fh:
            fh.write(json.dumps(target) + "\n")
            fh.write(json.dumps(_rec(datetime.now(timezone.utc))) + "\n")

        rec, found_arch = rot.find_record_in_archives(log, target_ts)
        assert rec is not None
        assert rec["agent"] == "target"
        assert found_arch == arch

    def test_missing_returns_none_plus_newest_hint(self, tmp_path: Path):
        log = tmp_path / "llm-calls.jsonl"
        log.touch()
        # One archive that doesn't contain our target
        arch = tmp_path / "llm-calls.2026-04-01.jsonl.gz"
        with gzip.open(arch, "wt") as fh:
            fh.write(json.dumps(_rec(datetime.now(timezone.utc))) + "\n")

        rec, hint = rot.find_record_in_archives(log, "2099-01-01T00:00:00+00:00")
        assert rec is None
        # Hint is the newest archive (here, the only one).
        assert hint == arch

    def test_no_archives_returns_none_none(self, tmp_path: Path):
        log = tmp_path / "llm-calls.jsonl"
        rec, hint = rot.find_record_in_archives(log, "whatever")
        assert rec is None
        assert hint is None


# ---------------------------------------------------------------------------
# CLI / run() top-level
# ---------------------------------------------------------------------------


class TestRunSummary:
    def test_run_does_nothing_when_below_thresholds(self, tmp_path: Path):
        log = tmp_path / "llm-calls.jsonl"
        _write_jsonl(log, [_rec(datetime.now(timezone.utc))])
        out = rot.run(log_path=log, size_limit_bytes=10 * 1024 * 1024)
        assert out["rotated"] is False
        assert out["rotated_to"] is None
        assert out["pruned_count"] == 0

    def test_run_rotates_when_above(self, tmp_path: Path):
        log = tmp_path / "llm-calls.jsonl"
        # Tiny size limit so any non-empty file triggers.
        _write_jsonl(log, [_rec(datetime.now(timezone.utc))])
        out = rot.run(log_path=log, size_limit_bytes=10)
        assert out["rotated"] is True
        assert out["rotated_to"] is not None
        assert Path(out["rotated_to"]).exists()
