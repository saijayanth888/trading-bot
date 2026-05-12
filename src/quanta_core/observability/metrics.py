"""Prometheus-style counters / histograms / gauges with a JSONL audit sink.

This module is deliberately self-contained: it does NOT pull in
``prometheus_client`` because the live engine is allowed to run in
constrained environments (e.g. backtest containers) where the prometheus
HTTP exporter is not wanted. The shapes mirror the prometheus client API
closely enough that swapping in the real client later is a one-line change.

Every metric mutation appends one line to ``~/.quanta/state/metrics.jsonl``
(append-only). A separate dashboard agent scrapes the JSONL — see
``docs/quanta-core-v4/06-ARCHITECTURE.md`` §3.18.
"""

from __future__ import annotations

import json
import math
import os
import threading
import time
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final

# Default histogram buckets in seconds (Prometheus default + tighter sub-second).
_DEFAULT_BUCKETS: Final[tuple[float, ...]] = (
    0.005,
    0.01,
    0.025,
    0.05,
    0.1,
    0.25,
    0.5,
    1.0,
    2.5,
    5.0,
    10.0,
    30.0,
    60.0,
)


def _default_jsonl_path() -> Path:
    """Resolve the JSONL sink path with env override."""
    override = os.environ.get("QUANTA_METRICS_JSONL")
    if override:
        return Path(override)
    return Path.home() / ".quanta" / "state" / "metrics.jsonl"


def _label_key(labels: Mapping[str, str] | None) -> tuple[tuple[str, str], ...]:
    """Return a hashable, order-stable key for a label set."""
    if not labels:
        return ()
    return tuple(sorted((str(k), str(v)) for k, v in labels.items()))


@dataclass(frozen=True, slots=True)
class _Sample:
    """One row in the JSONL sink."""

    ts: float
    metric: str
    kind: str
    value: float
    labels: dict[str, str] = field(default_factory=dict)
    note: str | None = None


class _JsonlSink:
    """Thread-safe append-only JSONL writer."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._lock = threading.Lock()
        self._enabled = True

    @property
    def path(self) -> Path:
        return self._path

    def disable(self) -> None:
        """Disable on-disk persistence (used in tests where the path is unwriteable)."""
        self._enabled = False

    def write(self, sample: _Sample) -> None:
        if not self._enabled:
            return
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
        except OSError:
            self._enabled = False
            return
        line = json.dumps(
            {
                "ts": sample.ts,
                "metric": sample.metric,
                "kind": sample.kind,
                "value": sample.value,
                "labels": sample.labels,
                "note": sample.note,
            }
        )
        with self._lock:
            with self._path.open("a", encoding="utf-8") as fh:
                fh.write(line + "\n")


class _Metric:
    """Base class — labels + thread-safe value table."""

    kind: str = "metric"

    def __init__(
        self,
        name: str,
        description: str,
        label_names: Iterable[str] | None = None,
        sink: _JsonlSink | None = None,
    ) -> None:
        self.name = name
        self.description = description
        self.label_names = tuple(label_names or ())
        self._lock = threading.Lock()
        self._sink = sink

    def _validate_labels(self, labels: Mapping[str, str] | None) -> dict[str, str]:
        if not self.label_names:
            if labels:
                raise ValueError(f"metric {self.name!r} declares no labels; got {dict(labels)!r}")
            return {}
        if labels is None:
            raise ValueError(f"metric {self.name!r} requires labels {self.label_names!r}")
        missing = set(self.label_names) - set(labels)
        extra = set(labels) - set(self.label_names)
        if missing or extra:
            raise ValueError(
                f"metric {self.name!r} labels mismatch — "
                f"missing={sorted(missing)} extra={sorted(extra)}"
            )
        return {k: str(labels[k]) for k in self.label_names}

    def _emit(self, value: float, labels: dict[str, str], note: str | None = None) -> None:
        if self._sink is not None:
            self._sink.write(
                _Sample(
                    ts=time.time(),
                    metric=self.name,
                    kind=self.kind,
                    value=value,
                    labels=labels,
                    note=note,
                )
            )


class Counter(_Metric):
    """Monotonic counter — only ever increases.

    Examples
    --------
    >>> trades = Counter("trades_total", "Trades opened", ["strategy"])
    >>> trades.inc(labels={"strategy": "mean_rev"})
    """

    kind = "counter"

    def __init__(
        self,
        name: str,
        description: str,
        label_names: Iterable[str] | None = None,
        sink: _JsonlSink | None = None,
    ) -> None:
        super().__init__(name, description, label_names, sink)
        self._values: dict[tuple[tuple[str, str], ...], float] = {}

    def inc(
        self,
        amount: float = 1.0,
        labels: Mapping[str, str] | None = None,
    ) -> None:
        """Increment the counter by ``amount`` (must be >= 0)."""
        if amount < 0:
            raise ValueError(f"Counter.inc requires amount >= 0, got {amount!r}")
        normalised = self._validate_labels(labels)
        key = _label_key(normalised)
        with self._lock:
            self._values[key] = self._values.get(key, 0.0) + amount
            current = self._values[key]
        self._emit(current, normalised)

    def value(self, labels: Mapping[str, str] | None = None) -> float:
        """Return the current value for ``labels`` (0.0 if never set)."""
        normalised = self._validate_labels(labels)
        key = _label_key(normalised)
        with self._lock:
            return self._values.get(key, 0.0)

    def collect(self) -> list[tuple[dict[str, str], float]]:
        """Snapshot every label combination + its current value."""
        with self._lock:
            return [(dict(k), v) for k, v in sorted(self._values.items())]


class Gauge(_Metric):
    """Bidirectional gauge — can be set to any value at any time."""

    kind = "gauge"

    def __init__(
        self,
        name: str,
        description: str,
        label_names: Iterable[str] | None = None,
        sink: _JsonlSink | None = None,
    ) -> None:
        super().__init__(name, description, label_names, sink)
        self._values: dict[tuple[tuple[str, str], ...], float] = {}

    def set(self, value: float, labels: Mapping[str, str] | None = None) -> None:
        """Replace the current value."""
        if math.isnan(value):
            raise ValueError("Gauge.set rejects NaN")
        normalised = self._validate_labels(labels)
        key = _label_key(normalised)
        with self._lock:
            self._values[key] = float(value)
        self._emit(float(value), normalised)

    def inc(
        self,
        amount: float = 1.0,
        labels: Mapping[str, str] | None = None,
    ) -> None:
        normalised = self._validate_labels(labels)
        key = _label_key(normalised)
        with self._lock:
            self._values[key] = self._values.get(key, 0.0) + amount
            current = self._values[key]
        self._emit(current, normalised)

    def dec(
        self,
        amount: float = 1.0,
        labels: Mapping[str, str] | None = None,
    ) -> None:
        self.inc(-amount, labels)

    def value(self, labels: Mapping[str, str] | None = None) -> float:
        normalised = self._validate_labels(labels)
        key = _label_key(normalised)
        with self._lock:
            return self._values.get(key, 0.0)

    def collect(self) -> list[tuple[dict[str, str], float]]:
        with self._lock:
            return [(dict(k), v) for k, v in sorted(self._values.items())]


@dataclass(slots=True)
class _HistogramState:
    bucket_counts: list[int]
    total_sum: float = 0.0
    total_count: int = 0


class Histogram(_Metric):
    """Bucketed histogram — sum + count + per-bucket counters.

    Buckets are upper-inclusive (Prometheus convention). The implicit
    ``+Inf`` bucket is always present.
    """

    kind = "histogram"

    def __init__(
        self,
        name: str,
        description: str,
        buckets: Iterable[float] | None = None,
        label_names: Iterable[str] | None = None,
        sink: _JsonlSink | None = None,
    ) -> None:
        super().__init__(name, description, label_names, sink)
        if buckets is None:
            bucket_list = list(_DEFAULT_BUCKETS)
        else:
            bucket_list = sorted(buckets)
        if not bucket_list:
            raise ValueError("Histogram requires at least one bucket")
        if any(b <= 0 for b in bucket_list):
            raise ValueError("Histogram buckets must all be > 0")
        self._buckets: tuple[float, ...] = tuple(bucket_list)
        self._states: dict[tuple[tuple[str, str], ...], _HistogramState] = {}

    @property
    def buckets(self) -> tuple[float, ...]:
        return self._buckets

    def observe(
        self,
        value: float,
        labels: Mapping[str, str] | None = None,
    ) -> None:
        if math.isnan(value) or math.isinf(value):
            raise ValueError(f"Histogram.observe rejects NaN / inf, got {value!r}")
        if value < 0:
            raise ValueError(f"Histogram.observe requires value >= 0, got {value!r}")
        normalised = self._validate_labels(labels)
        key = _label_key(normalised)
        with self._lock:
            state = self._states.get(key)
            if state is None:
                state = _HistogramState(bucket_counts=[0] * len(self._buckets))
                self._states[key] = state
            for i, upper in enumerate(self._buckets):
                if value <= upper:
                    state.bucket_counts[i] += 1
                    break
            state.total_sum += float(value)
            state.total_count += 1
            running_count = state.total_count
            running_sum = state.total_sum
        self._emit(
            float(value),
            normalised,
            note=f"count={running_count} sum={running_sum:.6f}",
        )

    def snapshot(self, labels: Mapping[str, str] | None = None) -> dict[str, Any]:
        normalised = self._validate_labels(labels)
        key = _label_key(normalised)
        with self._lock:
            state = self._states.get(key)
            buckets = list(self._buckets)
            if state is None:
                return {
                    "buckets": buckets,
                    "bucket_counts": [0] * len(buckets),
                    "sum": 0.0,
                    "count": 0,
                }
            return {
                "buckets": buckets,
                "bucket_counts": list(state.bucket_counts),
                "sum": state.total_sum,
                "count": state.total_count,
            }


class MetricsRegistry:
    """Process-wide registry of named metrics with a shared JSONL sink.

    Use :func:`get_registry` to obtain the lazily-instantiated default
    registry. Tests instantiate a private registry against ``tmp_path``.
    """

    def __init__(self, jsonl_path: Path | None = None) -> None:
        self._sink = _JsonlSink(jsonl_path or _default_jsonl_path())
        self._metrics: dict[str, _Metric] = {}
        self._lock = threading.Lock()
        # Pre-register the canonical V4 trading metrics so callers do not
        # have to pass label-name tuples at every call-site.
        self.counter("trades_total", "Trades opened", ["strategy", "venue", "side"])
        self.counter("risk_block_total", "Trades blocked by a risk gate", ["gate"])
        self.histogram("latency_seconds", "Generic operation latency in seconds")
        self.histogram(
            "ollama_latency_seconds",
            "Ollama HTTP round-trip latency in seconds",
            labels=["model"],
        )

    @property
    def jsonl_path(self) -> Path:
        return self._sink.path

    def disable_persistence(self) -> None:
        """Drop the JSONL sink (tests with read-only filesystems)."""
        self._sink.disable()

    # ------------------------------------------------------------------ factories

    def counter(
        self,
        name: str,
        description: str,
        labels: Iterable[str] | None = None,
    ) -> Counter:
        with self._lock:
            existing = self._metrics.get(name)
            if existing is not None:
                if not isinstance(existing, Counter):
                    raise ValueError(
                        f"metric {name!r} is already registered as "
                        f"{type(existing).__name__}, not Counter"
                    )
                return existing
            metric = Counter(name, description, labels, self._sink)
            self._metrics[name] = metric
            return metric

    def gauge(
        self,
        name: str,
        description: str,
        labels: Iterable[str] | None = None,
    ) -> Gauge:
        with self._lock:
            existing = self._metrics.get(name)
            if existing is not None:
                if not isinstance(existing, Gauge):
                    raise ValueError(
                        f"metric {name!r} is already registered as "
                        f"{type(existing).__name__}, not Gauge"
                    )
                return existing
            metric = Gauge(name, description, labels, self._sink)
            self._metrics[name] = metric
            return metric

    def histogram(
        self,
        name: str,
        description: str,
        buckets: Iterable[float] | None = None,
        labels: Iterable[str] | None = None,
    ) -> Histogram:
        with self._lock:
            existing = self._metrics.get(name)
            if existing is not None:
                if not isinstance(existing, Histogram):
                    raise ValueError(
                        f"metric {name!r} is already registered as "
                        f"{type(existing).__name__}, not Histogram"
                    )
                return existing
            metric = Histogram(name, description, buckets, labels, self._sink)
            self._metrics[name] = metric
            return metric

    # ------------------------------------------------------------------ lookup

    def get(self, name: str) -> _Metric | None:
        with self._lock:
            return self._metrics.get(name)

    def collect(self) -> list[dict[str, Any]]:
        """Return a flat dump of every metric — useful for ``/metrics`` HTTP."""
        out: list[dict[str, Any]] = []
        with self._lock:
            metrics = list(self._metrics.values())
        for metric in metrics:
            row: dict[str, Any] = {
                "name": metric.name,
                "kind": metric.kind,
                "description": metric.description,
                "label_names": list(metric.label_names),
            }
            if isinstance(metric, Counter | Gauge):
                row["values"] = [
                    {"labels": labels, "value": value} for labels, value in metric.collect()
                ]
            elif isinstance(metric, Histogram):
                row["buckets"] = list(metric.buckets)
            out.append(row)
        return out


_DEFAULT_REGISTRY: MetricsRegistry | None = None
_DEFAULT_REGISTRY_LOCK = threading.Lock()


def get_registry() -> MetricsRegistry:
    """Return (and lazily create) the process-wide default registry."""
    global _DEFAULT_REGISTRY
    with _DEFAULT_REGISTRY_LOCK:
        if _DEFAULT_REGISTRY is None:
            _DEFAULT_REGISTRY = MetricsRegistry()
        return _DEFAULT_REGISTRY


def _reset_default_registry_for_tests() -> None:
    """Drop the default registry. Tests call this between cases."""
    global _DEFAULT_REGISTRY
    with _DEFAULT_REGISTRY_LOCK:
        _DEFAULT_REGISTRY = None
