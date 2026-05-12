"""Tests for ``quanta_core.logging_setup`` — JSONL emission + redaction."""

from __future__ import annotations

import json
import logging
import sys
from typing import TYPE_CHECKING

import pytest
import structlog

from quanta_core.logging_setup import configure, get_logger

if TYPE_CHECKING:
    from collections.abc import Callable


def _capture_log(
    capsys: pytest.CaptureFixture[str],
    emit_fn: Callable[[], None],
) -> list[dict[str, object]]:
    """Run ``emit_fn`` and return parsed JSON lines emitted to stdout.

    structlog's stdlib handler writes to ``sys.stdout``; capsys swaps in a
    capture sink for the duration of the test. We force-flush the root
    logger's handler so any buffered output lands before we read.
    """
    emit_fn()
    for handler in logging.getLogger().handlers:
        handler.flush()
    sys.stdout.flush()
    captured = capsys.readouterr()
    lines = [line for line in captured.out.splitlines() if line.strip()]
    return [json.loads(line) for line in lines]


def test_basic_jsonl_output(capsys: pytest.CaptureFixture[str]) -> None:
    configure(level="INFO", json_output=True)

    def emit() -> None:
        log = get_logger("quanta_core.tests")
        log.info("trade_submitted", symbol="BTC/USD", qty=1)

    events = _capture_log(capsys, emit)
    assert len(events) == 1
    e = events[0]
    assert e["event"] == "trade_submitted"
    assert e["symbol"] == "BTC/USD"
    assert e["qty"] == 1
    assert e["level"] == "info"
    assert "timestamp" in e
    timestamp = e["timestamp"]
    assert isinstance(timestamp, str)
    assert timestamp.endswith("Z")


def test_logger_name_attached(capsys: pytest.CaptureFixture[str]) -> None:
    configure(level="DEBUG", json_output=True)

    def emit() -> None:
        log = get_logger("quanta_core.risk.governor")
        log.debug("anchor_resolved", path="/tmp/x")

    events = _capture_log(capsys, emit)
    assert events[0]["logger"] == "quanta_core.risk.governor"


def test_get_logger_without_name(capsys: pytest.CaptureFixture[str]) -> None:
    configure(level="INFO", json_output=True)

    def emit() -> None:
        log = get_logger()
        log.info("anonymous_event")

    events = _capture_log(capsys, emit)
    assert events[0]["event"] == "anonymous_event"


def test_redaction_of_known_secret_keys(capsys: pytest.CaptureFixture[str]) -> None:
    configure(level="INFO", json_output=True)

    def emit() -> None:
        log = get_logger("quanta_core.exchanges.alpaca")
        log.info(
            "venue_connected",
            api_key="SUPER_SECRET_VALUE",
            api_secret="OTHER_SECRET",
            symbol="AAPL",
        )

    events = _capture_log(capsys, emit)
    e = events[0]
    assert e["api_key"] == "***REDACTED***"
    assert e["api_secret"] == "***REDACTED***"
    assert e["symbol"] == "AAPL"
    # Confirm the secret literal is nowhere in the rendered output.
    assert "SUPER_SECRET_VALUE" not in json.dumps(e)


def test_redaction_case_insensitive(capsys: pytest.CaptureFixture[str]) -> None:
    configure(level="INFO", json_output=True)

    def emit() -> None:
        log = get_logger()
        log.info(
            "creds",
            API_KEY="x",  # uppercase
            Password="y",  # mixed case
            normal_field="ok",
        )

    events = _capture_log(capsys, emit)
    e = events[0]
    assert e["API_KEY"] == "***REDACTED***"
    assert e["Password"] == "***REDACTED***"
    assert e["normal_field"] == "ok"


def test_level_filter_drops_below(capsys: pytest.CaptureFixture[str]) -> None:
    configure(level="WARNING", json_output=True)

    def emit() -> None:
        log = get_logger()
        log.debug("debug_dropped")
        log.info("info_dropped")
        log.warning("warning_kept")

    events = _capture_log(capsys, emit)
    assert len(events) == 1
    assert events[0]["event"] == "warning_kept"


def test_configure_is_idempotent() -> None:
    configure(level="INFO", json_output=True)
    configure(level="DEBUG", json_output=True)
    # After second call we should be at DEBUG.
    root = logging.getLogger()
    assert root.level == logging.DEBUG
    # And only one stdout handler attached (no duplicates).
    assert len(root.handlers) == 1


def test_console_renderer_mode(capsys: pytest.CaptureFixture[str]) -> None:
    configure(level="INFO", json_output=False)

    def emit() -> None:
        log = get_logger()
        log.info("console_event", k="v")

    emit()
    for handler in logging.getLogger().handlers:
        handler.flush()
    captured = capsys.readouterr()
    # Console renderer emits human-readable text containing the event name
    # and the kwargs; it is NOT JSON.
    assert "console_event" in captured.out
    assert "k=" in captured.out
    # And it's not parseable as JSON.
    with pytest.raises(json.JSONDecodeError):
        json.loads(captured.out.splitlines()[0])


def test_structlog_is_configured_after_call() -> None:
    configure(level="INFO", json_output=True)
    # is_configured() returns True after configure() runs.
    assert structlog.is_configured()
