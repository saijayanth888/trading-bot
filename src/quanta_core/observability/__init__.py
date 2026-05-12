"""Quanta Core observability — metrics, notifications, healthcheck.

This package provides:

* :mod:`quanta_core.observability.metrics` — Prometheus-style counters /
  histograms / gauges with a JSONL audit sink.
* :mod:`quanta_core.observability.notifier` — fire-and-forget Slack notifier
  with severity routing + dedup; falls back to ``LogOnlyNotifier`` in paper
  mode.
* :mod:`quanta_core.observability.healthcheck_publisher` — a tiny HTTP
  server that exposes ``/health`` from Hermes state files.
"""

from __future__ import annotations

from quanta_core.observability.healthcheck_publisher import (
    HealthcheckPublisher,
    HealthStatus,
)
from quanta_core.observability.metrics import (
    Counter,
    Gauge,
    Histogram,
    MetricsRegistry,
    get_registry,
)
from quanta_core.observability.notifier import (
    LogOnlyNotifier,
    Notifier,
    NotifierError,
    Severity,
    SlackNotifier,
)

__all__ = [
    "Counter",
    "Gauge",
    "HealthStatus",
    "HealthcheckPublisher",
    "Histogram",
    "LogOnlyNotifier",
    "MetricsRegistry",
    "Notifier",
    "NotifierError",
    "Severity",
    "SlackNotifier",
    "get_registry",
]
