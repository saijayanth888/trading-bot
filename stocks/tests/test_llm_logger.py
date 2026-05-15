"""Tests for the LLM-call logger's full-text + redaction extension.

Covers:
  - Backward compat: with SHARK_LLM_LOG_FULL_TEXT unset, schema unchanged
  - Flag ON: prompt / system / response / messages written through
  - Each redaction pattern catches its target
  - Negative cases: no false positives on similar-looking strings
  - Concurrent appends from two threads don't interleave (one JSON-per-line)
  - redacted_count equals total substitutions

Run from stocks/:
    pytest tests/test_llm_logger.py -v
"""

from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest
from shark.llm import tracker as tracker_module
from shark.llm.redaction import redact, redact_messages

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def isolated_log(tmp_path, monkeypatch):
    """Each test gets its own JSONL file + a fresh tracker singleton.

    The production tracker is a module-level singleton; we reset it so
    `_log_disabled` from a previous test doesn't leak through.
    """
    log_path = tmp_path / "llm-calls.jsonl"
    monkeypatch.setattr(tracker_module, "_LOG_PATH", log_path)
    monkeypatch.setattr(tracker_module, "_singleton", None)
    # Default: flag OFF unless a test sets it
    monkeypatch.delenv("SHARK_LLM_LOG_FULL_TEXT", raising=False)
    yield log_path


def _read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


# ---------------------------------------------------------------------------
# Backward-compatible schema (flag OFF)
# ---------------------------------------------------------------------------


class TestBackwardCompat:
    def test_flag_off_omits_text_fields(self, isolated_log):
        """Without the flag, prompt/system/response are written as null and
        the existing schema keys must all still be present."""
        tracker_module.get_tracker().record(
            agent="t1", model="hermes3:8b", provider="ollama",
            latency_seconds=0.5, prompt_tokens=12, completion_tokens=8,
            tier="fast", role="default",
            system_message="you are an analyst",
            user_message="should not be logged",
            response_text="should not be logged",
        )
        rows = _read_jsonl(isolated_log)
        assert len(rows) == 1
        row = rows[0]
        # Legacy keys present
        for k in ("agent", "model", "provider", "tier", "role",
                  "latency_seconds", "prompt_tokens", "completion_tokens",
                  "timestamp"):
            assert k in row, f"missing legacy key: {k}"
        # Text fields exist as keys (schema-stable) but are null
        assert row.get("prompt") is None
        assert row.get("system_message") is None
        assert row.get("response_text") is None
        assert row.get("redacted_count") is None

    def test_existing_summariser_still_works(self, isolated_log):
        """The dashboard's summarise_window must not regress when the
        flag is off — it reads the same metadata keys it always did."""
        tracker_module.get_tracker().record(
            agent="t1", model="m", provider="ollama",
            latency_seconds=1.0, prompt_tokens=100, completion_tokens=50,
        )
        summary = tracker_module.summarise_window(log_path=isolated_log)
        assert summary["total_calls"] == 1
        assert summary["total_prompt_tokens"] == 100


# ---------------------------------------------------------------------------
# Flag ON — full-text persisted
# ---------------------------------------------------------------------------


class TestFullTextEnabled:
    def test_flag_on_writes_prompt_response(self, isolated_log, monkeypatch):
        monkeypatch.setenv("SHARK_LLM_LOG_FULL_TEXT", "1")
        tracker_module.get_tracker().record(
            agent="t2", model="hermes3:8b", provider="ollama",
            latency_seconds=0.3,
            system_message="trading analyst",
            user_message="What is AAPL doing?",
            response_text="AAPL is trending up.",
            messages=[
                {"role": "system", "content": "trading analyst"},
                {"role": "user", "content": "What is AAPL doing?"},
                {"role": "assistant", "content": "AAPL is trending up."},
            ],
        )
        rows = _read_jsonl(isolated_log)
        assert len(rows) == 1
        row = rows[0]
        assert row["system_message"] == "trading analyst"
        assert row["prompt"] == "What is AAPL doing?"
        assert row["response_text"] == "AAPL is trending up."
        assert isinstance(row["messages"], list) and len(row["messages"]) == 3
        assert row["redacted_count"] == 0

    @pytest.mark.parametrize("flag", ["1", "true", "yes", "ON"])
    def test_flag_truthy_values(self, isolated_log, monkeypatch, flag):
        """Common truthy spellings all enable the flag."""
        monkeypatch.setenv("SHARK_LLM_LOG_FULL_TEXT", flag)
        tracker_module.get_tracker().record(
            agent="t", model="m", provider="ollama",
            latency_seconds=0,
            user_message="hello",
            response_text="hi",
        )
        rows = _read_jsonl(isolated_log)
        assert rows[0]["prompt"] == "hello"


# ---------------------------------------------------------------------------
# Redaction patterns
# ---------------------------------------------------------------------------


class TestRedactionPatterns:
    def test_api_key_sk_prefix(self):
        out, n = redact("Authorization: sk-abcdef0123456789ABCDEF")
        assert "<REDACTED:api_key>" in out
        assert n == 1

    def test_api_key_xoxb_prefix(self):
        out, n = redact("token = xoxb-1234567890-abcdefghijklm")
        assert "<REDACTED:api_key>" in out
        assert n == 1

    def test_api_key_assignment_form(self):
        out, n = redact('api_key = "deadbeefcafebabe1234"')
        assert "<REDACTED:api_key>" in out
        assert n == 1

    def test_account_number_after_keyword(self):
        out, n = redact("account: 1234567890")
        assert "<REDACTED:account>" in out
        assert n == 1

    def test_account_number_before_keyword(self):
        out, n = redact("9876543210 wallet")
        assert "<REDACTED:account>" in out
        assert n == 1

    def test_email_redaction(self):
        out, n = redact("Contact: saijayanth532@gmail.com please")
        assert "<REDACTED:email>" in out
        assert "saijayanth532" not in out
        assert n == 1

    def test_operator_path_redaction(self):
        out, n = redact("Logs at /home/saijayanthai/Documents/trading-bot/log.txt today")
        assert "<REDACTED:path>" in out
        assert "saijayanthai" not in out
        assert n == 1

    def test_slack_webhook_redaction(self):
        out, n = redact("Hook: https://hooks.slack.com/services/T0/B0/abcDEFghi")
        assert "<REDACTED:webhook>" in out
        assert n == 1

    def test_multiple_patterns_one_string(self):
        text = (
            "key=sk-1234567890abcdef1234 reach me at me@x.io "
            "account 1111222233334 logs in /home/saijayanthai/foo"
        )
        out, n = redact(text)
        assert "<REDACTED:api_key>" in out
        assert "<REDACTED:email>" in out
        assert "<REDACTED:account>" in out
        assert "<REDACTED:path>" in out
        assert n >= 4

    def test_redact_handles_none(self):
        out, n = redact(None)
        assert out == ""
        assert n == 0


# ---------------------------------------------------------------------------
# Negative cases — must NOT over-redact
# ---------------------------------------------------------------------------


class TestNoFalsePositives:
    def test_session_number_not_redacted(self):
        """'session' near a number is not the 'account' keyword."""
        out, n = redact("session 1234567890 expired")
        assert "<REDACTED:account>" not in out
        assert n == 0

    def test_timestamp_alone_not_redacted(self):
        """A bare unix-ms timestamp with no account/wallet/id keyword stays."""
        out, n = redact("ts=1715000000000 latency=1.2")
        assert "<REDACTED:account>" not in out
        assert n == 0

    def test_normal_url_not_path_redacted(self):
        """Non-operator paths stay (e.g. /home/runner under CI, /opt, /usr)."""
        out, n = redact("Binary at /opt/trading-bot/bin/shark")
        assert "<REDACTED:path>" not in out
        assert n == 0
        out, n = redact("Config at /home/someoneelse/bot.toml")
        assert "<REDACTED:path>" not in out
        assert n == 0

    def test_non_slack_url_not_webhook(self):
        out, n = redact("Docs at https://example.com/api/foo")
        assert "<REDACTED:webhook>" not in out
        assert n == 0

    def test_short_sk_not_redacted(self):
        """An 'sk-' followed by fewer than 16 chars is not flagged."""
        out, n = redact("She said sk-short")
        assert "<REDACTED:api_key>" not in out
        assert n == 0

    def test_trading_prose_unchanged(self):
        """A realistic trading-style sentence with no secrets is untouched."""
        text = (
            "AAPL closed at 195.32 on 2026-05-11; SPY +0.4%. "
            "Volume 38,123,456 shares. Catalyst: earnings beat."
        )
        out, n = redact(text)
        assert out == text
        assert n == 0


# ---------------------------------------------------------------------------
# redact_messages — chat-format wrapper
# ---------------------------------------------------------------------------


class TestRedactMessages:
    def test_scrubs_each_message(self):
        msgs = [
            {"role": "system", "content": "you are a trader, email: x@y.io"},
            {"role": "user", "content": "What is the price?"},
            {"role": "assistant", "content": "sk-deadbeef1234567890abcd"},
        ]
        out, n = redact_messages(msgs)
        assert n == 2
        assert "<REDACTED:email>" in out[0]["content"]
        assert out[1]["content"] == "What is the price?"
        assert "<REDACTED:api_key>" in out[2]["content"]
        # Originals untouched
        assert "x@y.io" in msgs[0]["content"]

    def test_non_string_content_passes_through(self):
        msgs = [{"role": "user", "content": [{"type": "image", "url": "..."}]}]
        out, n = redact_messages(msgs)
        assert out == msgs
        assert n == 0


# ---------------------------------------------------------------------------
# Tracker integration with redaction
# ---------------------------------------------------------------------------


class TestRedactedCountField:
    def test_count_equals_substitutions(self, isolated_log, monkeypatch):
        monkeypatch.setenv("SHARK_LLM_LOG_FULL_TEXT", "1")
        tracker_module.get_tracker().record(
            agent="t", model="m", provider="ollama",
            latency_seconds=0,
            system_message="Slack: https://hooks.slack.com/services/T0/B0/xyz",
            user_message="email me at saijayanth532@gmail.com",
            response_text="key=sk-abcdef0123456789ABCDEF logged",
        )
        rows = _read_jsonl(isolated_log)
        row = rows[0]
        # 1 webhook + 1 email + 1 api_key
        assert row["redacted_count"] == 3
        assert "<REDACTED:webhook>" in row["system_message"]
        assert "<REDACTED:email>" in row["prompt"]
        assert "<REDACTED:api_key>" in row["response_text"]

    def test_zero_count_when_clean(self, isolated_log, monkeypatch):
        monkeypatch.setenv("SHARK_LLM_LOG_FULL_TEXT", "1")
        tracker_module.get_tracker().record(
            agent="t", model="m", provider="ollama",
            latency_seconds=0,
            system_message="be terse",
            user_message="What is AAPL doing today?",
            response_text="AAPL trending up.",
        )
        rows = _read_jsonl(isolated_log)
        assert rows[0]["redacted_count"] == 0


# ---------------------------------------------------------------------------
# Atomic append under concurrent writers
# ---------------------------------------------------------------------------


class TestConcurrentAppend:
    def test_two_threads_dont_interleave(self, isolated_log, monkeypatch):
        """Each thread writes 50 records; final file must have 100 lines
        and every line must parse as JSON (i.e. no torn writes)."""
        monkeypatch.setenv("SHARK_LLM_LOG_FULL_TEXT", "1")
        tracker = tracker_module.get_tracker()
        # A noticeable payload to stress the locking — make the line bigger
        # than typical so a non-atomic write would be obviously interleaved.
        big_payload = "x" * 4000

        def writer(label: str) -> None:
            for i in range(50):
                tracker.record(
                    agent=f"thread-{label}", model="m", provider="ollama",
                    latency_seconds=0,
                    user_message=f"{label}-{i}: {big_payload}",
                    response_text=f"resp-{label}-{i}",
                )

        t1 = threading.Thread(target=writer, args=("A",))
        t2 = threading.Thread(target=writer, args=("B",))
        t1.start(); t2.start()
        t1.join(); t2.join()

        # Every line must parse — that's the smoking gun for atomicity.
        lines = isolated_log.read_text().splitlines()
        assert len(lines) == 100, f"expected 100 lines, got {len(lines)}"
        parsed = [json.loads(line) for line in lines]  # would raise on torn line
        # Each writer made 50 calls
        from collections import Counter
        counts = Counter(r["agent"] for r in parsed)
        assert counts["thread-A"] == 50
        assert counts["thread-B"] == 50
