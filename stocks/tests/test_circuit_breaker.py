"""Unit tests for shark.llm.circuit_breaker.

Run from stocks/:
    pytest tests/test_circuit_breaker.py -v
"""

from __future__ import annotations

import time

import pytest

from shark.llm import circuit_breaker as cb_module
from shark.llm.circuit_breaker import CircuitBreaker, State


@pytest.fixture(autouse=True)
def isolated_state_dir(tmp_path, monkeypatch):
    """Every test gets a fresh state dir so breakers don't leak across tests."""
    monkeypatch.setattr(cb_module, "_STATE_DIR", tmp_path)
    # Clear any module-level singleton breakers from prior test modules
    monkeypatch.setattr(cb_module, "_breakers", {})
    yield


# ---------------------------------------------------------------------------
# State machine: CLOSED → OPEN → HALF_OPEN → CLOSED
# ---------------------------------------------------------------------------


class TestStateMachine:
    def test_starts_closed(self):
        cb = CircuitBreaker("t1", tier="fast")
        ok, reason = cb.can_execute()
        assert ok is True
        assert reason == "closed"

    def test_failure_under_threshold_stays_closed(self, monkeypatch):
        monkeypatch.setattr(cb_module, "FAILURE_THRESHOLD", 5)
        cb = CircuitBreaker("t2", tier="fast")
        for _ in range(4):
            cb.record_failure("connection refused")
        ok, _ = cb.can_execute()
        assert ok is True

    def test_trips_after_threshold_failures(self, monkeypatch):
        monkeypatch.setattr(cb_module, "FAILURE_THRESHOLD", 5)
        cb = CircuitBreaker("t3", tier="fast")
        for _ in range(5):
            cb.record_failure("timeout")
        ok, reason = cb.can_execute()
        assert ok is False
        assert reason == "open"

    def test_recovery_timeout_transitions_to_half_open(self, monkeypatch):
        monkeypatch.setattr(cb_module, "FAILURE_THRESHOLD", 3)
        monkeypatch.setattr(cb_module, "RECOVERY_TIMEOUT_S", 0.1)
        cb = CircuitBreaker("t4", tier="fast")
        for _ in range(3):
            cb.record_failure("oops")
        # Immediately after trip — still OPEN
        ok, reason = cb.can_execute()
        assert ok is False and reason == "open"
        # After recovery timeout — HALF_OPEN probe
        time.sleep(0.15)
        ok, reason = cb.can_execute()
        assert ok is True
        assert reason == "half_open_probe"

    def test_half_open_success_closes_breaker(self, monkeypatch):
        monkeypatch.setattr(cb_module, "FAILURE_THRESHOLD", 3)
        monkeypatch.setattr(cb_module, "RECOVERY_TIMEOUT_S", 0.1)
        cb = CircuitBreaker("t5", tier="fast")
        for _ in range(3):
            cb.record_failure("err")
        time.sleep(0.15)
        cb.can_execute()  # transitions OPEN → HALF_OPEN
        cb.record_success(1.0)
        ok, reason = cb.can_execute()
        assert ok is True and reason == "closed"

    def test_half_open_failure_reopens(self, monkeypatch):
        monkeypatch.setattr(cb_module, "FAILURE_THRESHOLD", 3)
        monkeypatch.setattr(cb_module, "RECOVERY_TIMEOUT_S", 0.1)
        cb = CircuitBreaker("t6", tier="fast")
        for _ in range(3):
            cb.record_failure("e")
        time.sleep(0.15)
        cb.can_execute()  # → HALF_OPEN
        cb.record_failure("probe failed")
        ok, reason = cb.can_execute()
        assert ok is False
        assert reason == "open"


# ---------------------------------------------------------------------------
# Latency-based trip
# ---------------------------------------------------------------------------


class TestLatencyTrip:
    def test_does_not_trip_with_few_samples(self, monkeypatch):
        monkeypatch.setattr(cb_module, "LATENCY_P95_THRESHOLD_S", {"fast": 5.0})
        monkeypatch.setattr(cb_module, "LATENCY_MIN_SAMPLES", 10)
        cb = CircuitBreaker("lat-1", tier="fast")
        # Only 5 samples — below the min — even if all are slow
        for _ in range(5):
            cb.record_success(20.0)
        ok, _ = cb.can_execute()
        assert ok is True  # not tripped yet

    def test_trips_when_p95_exceeds_threshold(self, monkeypatch):
        monkeypatch.setattr(cb_module, "LATENCY_P95_THRESHOLD_S", {"fast": 5.0})
        monkeypatch.setattr(cb_module, "LATENCY_MIN_SAMPLES", 10)
        cb = CircuitBreaker("lat-2", tier="fast")
        # 10 fast samples then 5 slow ones — p95 would be ≥ 20s
        for _ in range(10):
            cb.record_success(1.0)
        for _ in range(5):
            cb.record_success(20.0)
        ok, reason = cb.can_execute()
        assert ok is False
        assert reason == "open"

    def test_tier_specific_thresholds(self, monkeypatch):
        monkeypatch.setattr(cb_module, "LATENCY_P95_THRESHOLD_S",
                            {"fast": 5.0, "deep": 60.0})
        monkeypatch.setattr(cb_module, "LATENCY_MIN_SAMPLES", 10)
        # Fast breaker — 30s response trips it
        fast = CircuitBreaker("lat-fast", tier="fast")
        for _ in range(15):
            fast.record_success(30.0)
        ok_fast, _ = fast.can_execute()
        assert ok_fast is False  # tripped

        # Deep breaker — same 30s response is FINE under 60s threshold
        deep = CircuitBreaker("lat-deep", tier="deep")
        for _ in range(15):
            deep.record_success(30.0)
        ok_deep, _ = deep.can_execute()
        assert ok_deep is True  # still closed


# ---------------------------------------------------------------------------
# Persistence — state survives instance recreation
# ---------------------------------------------------------------------------


class TestPersistence:
    def test_state_survives_instance_recreation(self, monkeypatch):
        monkeypatch.setattr(cb_module, "FAILURE_THRESHOLD", 3)
        cb1 = CircuitBreaker("persist-1", tier="fast")
        for _ in range(3):
            cb1.record_failure("x")

        cb2 = CircuitBreaker("persist-1", tier="fast")  # fresh instance, same name
        ok, reason = cb2.can_execute()
        assert ok is False
        assert reason == "open"

    def test_corrupt_file_falls_back_to_fresh_state(self, tmp_path):
        cb = CircuitBreaker("corrupt-1", tier="fast")
        cb.state_file.write_text("not valid json {")
        ok, reason = cb.can_execute()
        assert ok is True
        assert reason == "closed"

    def test_colon_in_name_normalised_to_underscore(self):
        cb = CircuitBreaker("ollama:fast", tier="fast")
        assert ":" not in cb.state_file.name
        assert "ollama_fast" in cb.state_file.name


# ---------------------------------------------------------------------------
# Status snapshot — used by /api/ops/circuit_breakers
# ---------------------------------------------------------------------------


class TestStatusSnapshot:
    def test_status_keys(self):
        cb = CircuitBreaker("status-1", tier="fast")
        s = cb.get_status()
        for k in ("name", "tier", "state", "failure_count",
                  "in_state_seconds", "p50_latency_s", "p95_latency_s",
                  "threshold_s", "samples_in_window"):
            assert k in s, f"missing key: {k}"
        assert s["state"] == "closed"

    def test_status_reports_p50_p95(self):
        cb = CircuitBreaker("status-2", tier="fast")
        for v in [1.0, 1.0, 2.0, 2.0, 3.0]:
            cb.record_success(v)
        s = cb.get_status()
        assert s["p50_latency_s"] is not None
        assert s["p95_latency_s"] is not None
        assert s["samples_in_window"] == 5
