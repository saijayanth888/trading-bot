"""Tests for src.quanta_core.observability.v4_buffer.V4Buffer."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.quanta_core.observability.v4_buffer import V4Buffer


def test_append_then_read_recent(tmp_path: Path) -> None:
    buf = V4Buffer(jsonl_path=tmp_path / "debates.jsonl", capacity=4)
    buf.append({"kind": "debate", "session_id": "abc", "pair": "BTC/USD"})
    buf.append({"kind": "debate", "session_id": "def", "pair": "ETH/USD"})

    recent = buf.read_recent(limit=10)
    assert len(recent) == 2
    assert recent[0]["session_id"] == "abc"
    assert recent[1]["session_id"] == "def"

    # JSONL persisted to disk
    lines = (tmp_path / "debates.jsonl").read_text().strip().splitlines()
    assert json.loads(lines[0])["session_id"] == "abc"
    assert json.loads(lines[1])["session_id"] == "def"


def test_ring_buffer_capacity_bounded(tmp_path: Path) -> None:
    buf = V4Buffer(jsonl_path=tmp_path / "ring.jsonl", capacity=3)
    for i in range(5):
        buf.append({"i": i})

    recent = buf.read_recent(limit=10)
    # oldest two evicted from RAM ring; the remaining three are 2..4
    assert [r["i"] for r in recent] == [2, 3, 4]

    # All 5 appends still persisted to JSONL (durable record)
    lines = (tmp_path / "ring.jsonl").read_text().strip().splitlines()
    assert len(lines) == 5


def test_read_recent_with_limit(tmp_path: Path) -> None:
    buf = V4Buffer(jsonl_path=tmp_path / "lim.jsonl", capacity=10)
    for i in range(7):
        buf.append({"i": i})

    last_three = buf.read_recent(limit=3)
    assert [r["i"] for r in last_three] == [4, 5, 6]


def test_creates_parent_dir(tmp_path: Path) -> None:
    nested = tmp_path / "nested" / "dir" / "events.jsonl"
    buf = V4Buffer(jsonl_path=nested, capacity=4)
    buf.append({"hello": "world"})
    assert nested.exists()


def test_append_serializes_datetime(tmp_path: Path) -> None:
    from datetime import datetime, timezone
    buf = V4Buffer(jsonl_path=tmp_path / "dt.jsonl", capacity=4)
    ts = datetime(2026, 5, 13, 1, 30, 0, tzinfo=timezone.utc)
    buf.append({"ts": ts, "session_id": "live-001"})

    lines = (tmp_path / "dt.jsonl").read_text().strip().splitlines()
    parsed = json.loads(lines[0])
    # str(datetime) form persisted via default=str fallback
    assert "2026-05-13" in parsed["ts"]
