"""Tests for /api/ops/llm_calls and /api/ops/llm_calls/{call_id}.

Covers:
  - Empty file → degraded envelope with empty calls list
  - 100-entry mock → returns correct slice + summary numbers
  - agent / model / since / regex / latency-range filters
  - include_text=0 strips heavy fields; include_text=1 keeps them
  - Detail endpoint: 200 on hit, 404 on miss, 410 + archive_path
  - Tail-reader handles a multi-megabyte file without OOM

Run from the repo root:
    pytest tests/test_llm_calls_endpoint.py -v
"""

from __future__ import annotations

import asyncio
import gzip
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi import HTTPException

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "user_data"))
sys.path.insert(0, str(ROOT / "stocks"))

from dashboard import ops_routes  # noqa: E402


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def isolated_log(tmp_path, monkeypatch):
    """Point the endpoint at a tmp_path log instead of the production file."""
    log_path = tmp_path / "llm-calls.jsonl"

    def _fake_log_paths():
        return [log_path]

    monkeypatch.setattr(ops_routes, "_llm_log_paths", _fake_log_paths)
    return log_path


def _rec(ts: datetime, *, agent: str = "reflector", model: str = "qwen3:30b",
         lat: float = 1.0, p_tok: int = 100, c_tok: int = 50,
         prompt: str | None = None, response_text: str | None = None,
         provider: str = "ollama") -> dict:
    return {
        "agent": agent,
        "model": model,
        "provider": provider,
        "tier": "fast",
        "role": "default",
        "latency_seconds": lat,
        "prompt_tokens": p_tok,
        "completion_tokens": c_tok,
        "timestamp": ts.isoformat(),
        "prompt": prompt,
        "system_message": None,
        "response_text": response_text,
        "messages": None,
        "redacted_count": 0,
    }


def _seed(path: Path, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for r in records:
            fh.write(json.dumps(r) + "\n")


def _run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# Index endpoint — /api/ops/llm_calls
# ---------------------------------------------------------------------------


class TestIndex:
    def test_missing_log_returns_degraded(self, isolated_log):
        # Log file doesn't exist yet
        env = _run(ops_routes.llm_calls())
        assert env["status"] == "degraded"
        assert env["data"]["calls"] == []
        assert env["data"]["summary"]["total_calls"] == 0
        # Hint includes the path so operator can see where it WOULD have written.
        assert "log_path" in env["data"]

    def test_100_records_correct_slice_and_summary(self, isolated_log):
        now = datetime.now(timezone.utc)
        records = []
        for i in range(100):
            agent = "reflector" if i % 2 == 0 else "analyst_bull"
            records.append(_rec(
                now - timedelta(minutes=i),
                agent=agent,
                lat=0.5 + (i % 10) * 0.3,
                p_tok=200 + i,
                c_tok=80 + i,
            ))
        _seed(isolated_log, records)

        env = _run(ops_routes.llm_calls(limit=50))
        assert env["status"] == "ok"
        data = env["data"]
        # Default page size
        assert len(data["calls"]) == 50
        assert data["total_in_window"] == 100  # before pagination, all match
        assert data["total_24h"] == 100
        # Summary aggregates
        s = data["summary"]
        assert s["total_calls"] == 100
        assert s["total_prompt_tokens"] == sum(200 + i for i in range(100))
        assert s["total_completion_tokens"] == sum(80 + i for i in range(100))
        assert s["by_agent"]["reflector"] == 50
        assert s["by_agent"]["analyst_bull"] == 50
        assert s["ollama_pct"] == 100.0  # all records are ollama in our fixture

    def test_agent_filter(self, isolated_log):
        now = datetime.now(timezone.utc)
        records = [
            _rec(now - timedelta(seconds=1), agent="reflector"),
            _rec(now - timedelta(seconds=2), agent="analyst_bull"),
            _rec(now - timedelta(seconds=3), agent="reflector"),
        ]
        _seed(isolated_log, records)
        env = _run(ops_routes.llm_calls(agent="reflector"))
        assert env["status"] == "ok"
        calls = env["data"]["calls"]
        assert len(calls) == 2
        assert all(c["agent"] == "reflector" for c in calls)

    def test_model_filter(self, isolated_log):
        now = datetime.now(timezone.utc)
        records = [
            _rec(now - timedelta(seconds=1), model="hermes3:8b"),
            _rec(now - timedelta(seconds=2), model="qwen3:30b"),
        ]
        _seed(isolated_log, records)
        env = _run(ops_routes.llm_calls(model="hermes"))
        assert env["status"] == "ok"
        assert len(env["data"]["calls"]) == 1
        assert env["data"]["calls"][0]["model"] == "hermes3:8b"

    def test_latency_range(self, isolated_log):
        now = datetime.now(timezone.utc)
        records = [
            _rec(now - timedelta(seconds=1), lat=0.5),
            _rec(now - timedelta(seconds=2), lat=3.0),
            _rec(now - timedelta(seconds=3), lat=12.0),
            _rec(now - timedelta(seconds=4), lat=25.0),
        ]
        _seed(isolated_log, records)
        env = _run(ops_routes.llm_calls(min_latency=2.0, max_latency=15.0))
        kept = env["data"]["calls"]
        assert len(kept) == 2
        assert sorted([c["latency_seconds"] for c in kept]) == [3.0, 12.0]

    def test_since_filter(self, isolated_log):
        now = datetime.now(timezone.utc)
        records = [
            _rec(now - timedelta(minutes=5)),
            _rec(now - timedelta(hours=2)),
        ]
        _seed(isolated_log, records)
        since = (now - timedelta(minutes=30)).isoformat()
        env = _run(ops_routes.llm_calls(since=since))
        # Only the 5-minute-old record should be returned
        assert env["data"]["total_in_window"] == 1

    def test_regex_filter_matches_agent(self, isolated_log):
        now = datetime.now(timezone.utc)
        records = [
            _rec(now - timedelta(seconds=1), agent="analyst_bull"),
            _rec(now - timedelta(seconds=2), agent="analyst_bear"),
            _rec(now - timedelta(seconds=3), agent="reflector"),
        ]
        _seed(isolated_log, records)
        env = _run(ops_routes.llm_calls(q=r"^analyst"))
        assert env["data"]["total_in_window"] == 2

    def test_regex_bad_pattern_returns_down(self, isolated_log):
        _seed(isolated_log, [_rec(datetime.now(timezone.utc))])
        env = _run(ops_routes.llm_calls(q="["))  # invalid regex
        assert env["status"] == "down"
        assert "regex" in env["error"]

    def test_include_text_strips_heavy_by_default(self, isolated_log):
        now = datetime.now(timezone.utc)
        records = [
            _rec(now, prompt="big prompt", response_text="big response"),
        ]
        _seed(isolated_log, records)
        env = _run(ops_routes.llm_calls(include_text=0))
        assert env["data"]["calls"][0].get("prompt") is None
        assert env["data"]["calls"][0].get("response_text") is None
        # Token / latency metadata still present
        assert env["data"]["calls"][0]["latency_seconds"] is not None

    def test_include_text_returns_full(self, isolated_log):
        now = datetime.now(timezone.utc)
        _seed(isolated_log, [_rec(now, prompt="big prompt", response_text="big response")])
        env = _run(ops_routes.llm_calls(include_text=1))
        c = env["data"]["calls"][0]
        assert c["prompt"] == "big prompt"
        assert c["response_text"] == "big response"

    def test_regex_against_prompt_only_when_include_text(self, isolated_log):
        now = datetime.now(timezone.utc)
        records = [
            _rec(now - timedelta(seconds=1), agent="a", prompt="needle_in_haystack"),
            _rec(now - timedelta(seconds=2), agent="b", prompt="other"),
        ]
        _seed(isolated_log, records)
        # Without include_text=1, regex doesn't touch prompt → 0 matches
        env_off = _run(ops_routes.llm_calls(q="needle", include_text=0))
        assert env_off["data"]["total_in_window"] == 0
        # With include_text=1, the regex matches the prompt text → 1 match
        env_on = _run(ops_routes.llm_calls(q="needle", include_text=1))
        assert env_on["data"]["total_in_window"] == 1

    def test_limit_clamped_to_500(self, isolated_log):
        now = datetime.now(timezone.utc)
        _seed(isolated_log, [_rec(now - timedelta(seconds=i)) for i in range(10)])
        env = _run(ops_routes.llm_calls(limit=9999))
        # Limit clamped to 500 but we only have 10 records
        assert len(env["data"]["calls"]) == 10

    def test_ollama_vs_anthropic_pct(self, isolated_log):
        now = datetime.now(timezone.utc)
        records = [
            _rec(now - timedelta(seconds=1), provider="ollama"),
            _rec(now - timedelta(seconds=2), provider="ollama"),
            _rec(now - timedelta(seconds=3), provider="ollama"),
            _rec(now - timedelta(seconds=4), provider="anthropic"),
        ]
        _seed(isolated_log, records)
        env = _run(ops_routes.llm_calls())
        s = env["data"]["summary"]
        assert s["ollama_pct"] == 75.0
        assert s["anthropic_pct"] == 25.0


# ---------------------------------------------------------------------------
# Tail reader — must handle large files
# ---------------------------------------------------------------------------


class TestTailReader:
    def test_tail_returns_newest_first(self, isolated_log):
        now = datetime.now(timezone.utc)
        records = [
            _rec(now - timedelta(seconds=100), agent="oldest"),
            _rec(now - timedelta(seconds=50), agent="middle"),
            _rec(now - timedelta(seconds=1), agent="newest"),
        ]
        _seed(isolated_log, records)
        out = ops_routes._read_jsonl_tail(isolated_log, max_records=3)
        assert out[0]["agent"] == "newest"
        assert out[2]["agent"] == "oldest"

    def test_tail_caps_at_max_records(self, isolated_log):
        now = datetime.now(timezone.utc)
        _seed(isolated_log, [_rec(now - timedelta(seconds=i)) for i in range(50)])
        out = ops_routes._read_jsonl_tail(isolated_log, max_records=10)
        assert len(out) == 10

    def test_tail_skips_malformed_lines(self, isolated_log):
        # Mix valid + garbage lines
        good = _rec(datetime.now(timezone.utc), agent="good")
        with isolated_log.open("w") as fh:
            fh.write("garbage line\n")
            fh.write(json.dumps(good) + "\n")
            fh.write("{partial json\n")
        out = ops_routes._read_jsonl_tail(isolated_log, max_records=100)
        assert len(out) == 1
        assert out[0]["agent"] == "good"

    def test_tail_handles_large_file(self, tmp_path, monkeypatch):
        """Sanity check: a 2 MB file with 2000 records doesn't OOM."""
        log = tmp_path / "big.jsonl"
        now = datetime.now(timezone.utc)
        with log.open("w") as fh:
            for i in range(2000):
                fh.write(json.dumps(_rec(now - timedelta(seconds=i), agent=f"a{i}")) + "\n")
        out = ops_routes._read_jsonl_tail(log, max_records=50)
        assert len(out) == 50
        # Tail is newest-in-FILE-ORDER first. We wrote a0..a1999 in append
        # order, so the last line is a1999 → it shows up first.
        assert out[0]["agent"] == "a1999"
        assert out[49]["agent"] == "a1950"


# ---------------------------------------------------------------------------
# Detail endpoint — /api/ops/llm_calls/{call_id}
# ---------------------------------------------------------------------------


class TestDetail:
    def test_hit_returns_full_record(self, isolated_log):
        target = _rec(datetime.now(timezone.utc), agent="target", prompt="hello world")
        _seed(isolated_log, [target])
        env = _run(ops_routes.llm_call_detail(target["timestamp"]))
        assert env["status"] == "ok"
        assert env["data"]["call"]["agent"] == "target"
        assert env["data"]["call"]["prompt"] == "hello world"
        assert env["data"]["source"] == "live"

    def test_url_encoded_timestamp(self, isolated_log):
        # Timestamps contain "+" which becomes %2B when URL-encoded; FastAPI
        # decodes the path on our behalf, but the handler also calls
        # urllib.parse.unquote defensively. Verify either form works.
        target = _rec(datetime.now(timezone.utc))
        _seed(isolated_log, [target])
        from urllib.parse import quote
        env = _run(ops_routes.llm_call_detail(quote(target["timestamp"])))
        assert env["status"] == "ok"

    def test_404_when_not_found_and_no_archives(self, isolated_log):
        # Empty log + no archive sibling → 404
        isolated_log.touch()
        with pytest.raises(HTTPException) as exc:
            _run(ops_routes.llm_call_detail("2099-01-01T00:00:00+00:00"))
        assert exc.value.status_code == 404

    def test_410_when_record_in_archive(self, isolated_log, monkeypatch):
        """If the live file misses but an archive contains it, return 410
        with the archive path so operator can grep manually."""
        target_ts = "2026-04-15T10:00:00.000000+00:00"
        target = {**_rec(datetime.now(timezone.utc)), "timestamp": target_ts,
                  "agent": "archived_target", "prompt": "old prompt"}
        # Live file empty
        isolated_log.touch()
        # Archive sibling with the target record
        arch = isolated_log.parent / "llm-calls.2026-04-15.jsonl.gz"
        with gzip.open(arch, "wt") as fh:
            fh.write(json.dumps(target) + "\n")

        with pytest.raises(HTTPException) as exc:
            _run(ops_routes.llm_call_detail(target_ts))
        assert exc.value.status_code == 410
        detail = exc.value.detail
        assert isinstance(detail, dict)
        assert detail["call"]["agent"] == "archived_target"
        assert str(arch) in detail["archive_path"]

    def test_410_when_not_in_live_but_archives_exist(self, isolated_log):
        """If the live file misses and archives exist but none contain
        the target, we still 410 (the record is somewhere we can't
        reach quickly) and surface the newest archive as a grep hint."""
        # Live empty
        isolated_log.touch()
        # One archive that doesn't contain our target
        arch = isolated_log.parent / "llm-calls.2026-04-01.jsonl.gz"
        with gzip.open(arch, "wt") as fh:
            fh.write(json.dumps(_rec(datetime.now(timezone.utc))) + "\n")
        with pytest.raises(HTTPException) as exc:
            _run(ops_routes.llm_call_detail("2099-01-01T00:00:00+00:00"))
        assert exc.value.status_code == 410
        detail = exc.value.detail
        assert isinstance(detail, dict)
        assert "newest_archive" in detail

    def test_empty_call_id_rejected(self, isolated_log):
        with pytest.raises(HTTPException) as exc:
            _run(ops_routes.llm_call_detail("   "))
        assert exc.value.status_code == 400
