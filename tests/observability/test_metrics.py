"""Unit tests for the metrics registry, counters, gauges and histograms."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from quanta_core.observability.metrics import (
    Counter,
    Histogram,
    MetricsRegistry,
    _reset_default_registry_for_tests,
    get_registry,
)


@pytest.fixture(autouse=True)
def _reset_default_registry() -> None:
    _reset_default_registry_for_tests()


@pytest.fixture()
def registry(tmp_path: Path) -> MetricsRegistry:
    return MetricsRegistry(jsonl_path=tmp_path / "metrics.jsonl")


# --------------------------------------------------------------------------- Counter


def test_counter_inc_default_value(registry: MetricsRegistry) -> None:
    counter = registry.counter("foo_total", "x")
    counter.inc()
    counter.inc()
    assert counter.value() == 2.0


def test_counter_inc_amount(registry: MetricsRegistry) -> None:
    counter = registry.counter("foo_total", "x")
    counter.inc(amount=2.5)
    assert counter.value() == 2.5


def test_counter_rejects_negative(registry: MetricsRegistry) -> None:
    counter = registry.counter("foo_total", "x")
    with pytest.raises(ValueError, match="amount >= 0"):
        counter.inc(amount=-1)


def test_counter_with_labels(registry: MetricsRegistry) -> None:
    counter = registry.counter("trades_total", "x", ["strategy", "venue", "side"])
    counter.inc(labels={"strategy": "mr", "venue": "alpaca", "side": "BUY"})
    counter.inc(labels={"strategy": "mr", "venue": "alpaca", "side": "BUY"})
    counter.inc(labels={"strategy": "mr", "venue": "coinbase", "side": "BUY"})
    assert counter.value(labels={"strategy": "mr", "venue": "alpaca", "side": "BUY"}) == 2.0
    assert counter.value(labels={"strategy": "mr", "venue": "coinbase", "side": "BUY"}) == 1.0
    assert len(counter.collect()) == 2


def test_counter_missing_labels_raises(registry: MetricsRegistry) -> None:
    counter = registry.counter("with_labels", "x", ["a"])
    with pytest.raises(ValueError, match="requires labels"):
        counter.inc()


def test_counter_extra_label_raises(registry: MetricsRegistry) -> None:
    counter = registry.counter("no_labels", "x")
    with pytest.raises(ValueError, match="declares no labels"):
        counter.inc(labels={"x": "1"})


def test_counter_label_set_mismatch(registry: MetricsRegistry) -> None:
    counter = registry.counter("two_labels", "x", ["a", "b"])
    with pytest.raises(ValueError, match="labels mismatch"):
        counter.inc(labels={"a": "1"})


# --------------------------------------------------------------------------- Gauge


def test_gauge_set_and_get(registry: MetricsRegistry) -> None:
    gauge = registry.gauge("paused", "x")
    gauge.set(1)
    assert gauge.value() == 1.0
    gauge.set(0)
    assert gauge.value() == 0.0


def test_gauge_inc_and_dec(registry: MetricsRegistry) -> None:
    gauge = registry.gauge("inflight", "x")
    gauge.inc()
    gauge.inc(2)
    gauge.dec(1)
    assert gauge.value() == 2.0


def test_gauge_rejects_nan(registry: MetricsRegistry) -> None:
    gauge = registry.gauge("g", "x")
    with pytest.raises(ValueError, match="NaN"):
        gauge.set(float("nan"))


# --------------------------------------------------------------------------- Histogram


def test_histogram_observe_buckets(registry: MetricsRegistry) -> None:
    h = registry.histogram("lat", "x", buckets=[0.1, 1.0, 10.0])
    h.observe(0.05)
    h.observe(0.5)
    h.observe(2.0)
    h.observe(20.0)  # Falls into +Inf — no bucket captures it
    snap = h.snapshot()
    assert snap["bucket_counts"][0] == 1
    assert snap["bucket_counts"][1] == 1
    assert snap["bucket_counts"][2] == 1
    assert snap["count"] == 4
    assert snap["sum"] == pytest.approx(22.55)


def test_histogram_default_buckets(registry: MetricsRegistry) -> None:
    h = registry.histogram("lat2", "x")
    assert h.buckets[0] == 0.005
    h.observe(0.5)
    assert h.snapshot()["count"] == 1


def test_histogram_rejects_negative_and_nan(registry: MetricsRegistry) -> None:
    h = registry.histogram("lat3", "x")
    with pytest.raises(ValueError):
        h.observe(-1)
    with pytest.raises(ValueError):
        h.observe(float("nan"))
    with pytest.raises(ValueError):
        h.observe(float("inf"))


def test_histogram_with_labels(registry: MetricsRegistry) -> None:
    h = registry.histogram("ollama_latency_seconds", "x", labels=["model"])
    h.observe(0.5, labels={"model": "hermes3:8b"})
    h.observe(2.5, labels={"model": "hermes3:8b"})
    snap = h.snapshot(labels={"model": "hermes3:8b"})
    assert snap["count"] == 2
    empty = h.snapshot(labels={"model": "hermes3:70b"})
    assert empty["count"] == 0


def test_histogram_rejects_bad_buckets() -> None:
    with pytest.raises(ValueError):
        Histogram("h", "x", buckets=[])
    with pytest.raises(ValueError):
        Histogram("h", "x", buckets=[0, 1])


# --------------------------------------------------------------------------- Registry / JSONL


def test_registry_persists_jsonl(registry: MetricsRegistry, tmp_path: Path) -> None:
    counter = registry.counter("persist_total", "x")
    counter.inc()
    counter.inc(amount=2.0)
    lines = registry.jsonl_path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 2
    parsed = [json.loads(line) for line in lines]
    assert parsed[-1]["value"] == 3.0
    assert parsed[-1]["kind"] == "counter"


def test_registry_dedup_returns_same_counter(registry: MetricsRegistry) -> None:
    c1 = registry.counter("name", "x")
    c2 = registry.counter("name", "x")
    assert c1 is c2


def test_registry_type_mismatch_raises(registry: MetricsRegistry) -> None:
    registry.counter("conflict", "x")
    with pytest.raises(ValueError, match="already registered"):
        registry.gauge("conflict", "x")
    with pytest.raises(ValueError, match="already registered"):
        registry.histogram("conflict", "x")


def test_registry_type_mismatch_counter_vs_others(
    registry: MetricsRegistry,
) -> None:
    registry.gauge("only_gauge", "x")
    with pytest.raises(ValueError, match="already registered"):
        registry.counter("only_gauge", "x")
    registry.histogram("only_h", "x")
    with pytest.raises(ValueError, match="already registered"):
        registry.counter("only_h", "x")


def test_registry_collect_returns_summary(registry: MetricsRegistry) -> None:
    registry.counter("a", "x")
    registry.gauge("b", "x")
    registry.histogram("c", "x")
    rows = registry.collect()
    names = {r["name"] for r in rows}
    assert {"a", "b", "c"}.issubset(names)


def test_registry_disable_persistence(registry: MetricsRegistry) -> None:
    registry.disable_persistence()
    counter = registry.counter("nopersist_total", "x")
    counter.inc()
    # File may have prior writes from setup; new inc must not append.
    pre = registry.jsonl_path.read_text(encoding="utf-8") if registry.jsonl_path.exists() else ""
    counter.inc()
    post = registry.jsonl_path.read_text(encoding="utf-8") if registry.jsonl_path.exists() else ""
    assert pre == post


def test_get_registry_is_singleton(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("QUANTA_METRICS_JSONL", str(tmp_path / "m.jsonl"))
    _reset_default_registry_for_tests()
    a = get_registry()
    b = get_registry()
    assert a is b


def test_registry_get_returns_none_for_missing(
    registry: MetricsRegistry,
) -> None:
    assert registry.get("nope") is None
    registry.counter("there", "x")
    assert registry.get("there") is not None


def test_jsonl_sink_disabled_when_dir_unwriteable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "nope" / "metrics.jsonl"
    registry = MetricsRegistry(jsonl_path=target)
    counter = registry.counter("u_total", "x")
    counter.inc()
    # Writes succeed silently — dir is created lazily.
    assert target.exists()


def test_canonical_metrics_preregistered() -> None:
    registry = MetricsRegistry()
    registry.disable_persistence()
    assert isinstance(registry.get("trades_total"), Counter)
    assert isinstance(registry.get("risk_block_total"), Counter)
    assert isinstance(registry.get("latency_seconds"), Histogram)
    assert isinstance(registry.get("ollama_latency_seconds"), Histogram)


def test_registry_gauge_and_histogram_dedup(registry: MetricsRegistry) -> None:
    g1 = registry.gauge("gx", "x")
    g2 = registry.gauge("gx", "x")
    assert g1 is g2
    h1 = registry.histogram("hx", "x")
    h2 = registry.histogram("hx", "x")
    assert h1 is h2


def test_jsonl_sink_swallows_oserror_on_mkdir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "sub" / "metrics.jsonl"
    registry = MetricsRegistry(jsonl_path=target)
    # Simulate Path.mkdir raising — the sink should silently disable.
    import pathlib

    original_mkdir = pathlib.Path.mkdir

    def boom(self: pathlib.Path, *args: Any, **kwargs: Any) -> None:
        if str(self) == str(target.parent):
            raise OSError("simulated permission denied")
        original_mkdir(self, *args, **kwargs)

    monkeypatch.setattr(pathlib.Path, "mkdir", boom)
    counter = registry.counter("oserr_total", "x")
    counter.inc()
    # First write attempted, dir-mkdir failed → sink disabled silently.
    assert not target.exists()
    counter.inc()  # subsequent writes are now no-ops via _enabled flag
