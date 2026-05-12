"""Tests for the Hermes-state-file healthcheck publisher."""

from __future__ import annotations

import json
import time
import urllib.request
from pathlib import Path
from typing import Any

import pytest

from quanta_core.observability.healthcheck_publisher import (
    HealthcheckPublisher,
    HealthStatus,
)


def _write(path: Path, data: dict[str, Any]) -> None:
    path.write_text(json.dumps(data), encoding="utf-8")


def test_evaluate_ok_when_all_files_healthy(tmp_path: Path) -> None:
    _write(tmp_path / "live_engine.json", {"status": "ok"})
    _write(tmp_path / "risk.json", {"status": "running"})
    publisher = HealthcheckPublisher(
        state_dir=tmp_path,
        required_components=["live_engine", "risk"],
    )
    snap = publisher.evaluate()
    assert snap.status == "ok"
    assert snap.http_status == 200


def test_evaluate_degraded_when_required_missing(tmp_path: Path) -> None:
    _write(tmp_path / "risk.json", {"status": "ok"})
    publisher = HealthcheckPublisher(
        state_dir=tmp_path,
        required_components=["live_engine"],
    )
    snap = publisher.evaluate()
    assert snap.status == "degraded"
    assert snap.http_status == 503
    assert snap.components["live_engine"]["status"] == "degraded"


def test_evaluate_down_when_state_reports_critical(tmp_path: Path) -> None:
    _write(tmp_path / "live_engine.json", {"status": "critical"})
    publisher = HealthcheckPublisher(state_dir=tmp_path)
    snap = publisher.evaluate()
    assert snap.status == "down"


def test_evaluate_degraded_when_stale(tmp_path: Path) -> None:
    path = tmp_path / "live_engine.json"
    _write(path, {"status": "ok"})
    old = time.time() - 600
    import os

    os.utime(path, (old, old))
    publisher = HealthcheckPublisher(state_dir=tmp_path, freshness_s=60)
    snap = publisher.evaluate()
    assert snap.status == "degraded"
    assert "stale" in snap.components["live_engine"]["detail"]


def test_evaluate_degraded_when_unparseable(tmp_path: Path) -> None:
    (tmp_path / "live_engine.json").write_text("not json")
    publisher = HealthcheckPublisher(state_dir=tmp_path)
    snap = publisher.evaluate()
    assert snap.status == "degraded"


def test_evaluate_paused_marks_degraded(tmp_path: Path) -> None:
    _write(tmp_path / "live_engine.json", {"paused": True})
    publisher = HealthcheckPublisher(state_dir=tmp_path)
    snap = publisher.evaluate()
    assert snap.status == "degraded"


def test_evaluate_unknown_status_falls_back_to_ok(tmp_path: Path) -> None:
    _write(tmp_path / "live_engine.json", {"status": "ENTANGLED"})
    publisher = HealthcheckPublisher(state_dir=tmp_path)
    snap = publisher.evaluate()
    assert snap.status == "ok"


def test_evaluate_nonexistent_dir(tmp_path: Path) -> None:
    publisher = HealthcheckPublisher(
        state_dir=tmp_path / "missing",
        required_components=["live_engine"],
    )
    snap = publisher.evaluate()
    assert snap.status == "degraded"


def test_evaluate_payload_not_dict(tmp_path: Path) -> None:
    (tmp_path / "live_engine.json").write_text(json.dumps([1, 2]))
    publisher = HealthcheckPublisher(state_dir=tmp_path)
    snap = publisher.evaluate()
    assert snap.components["live_engine"]["status"] == "degraded"


def test_health_status_to_dict(tmp_path: Path) -> None:
    _write(tmp_path / "live_engine.json", {"status": "ok"})
    publisher = HealthcheckPublisher(state_dir=tmp_path)
    snap = publisher.evaluate()
    d = snap.to_dict()
    assert d["status"] == snap.status
    assert d["components"] == snap.components
    assert d["ts"] == snap.ts


def test_constructor_rejects_bad_freshness() -> None:
    with pytest.raises(ValueError):
        HealthcheckPublisher(freshness_s=0)


def test_http_endpoint_returns_health(tmp_path: Path) -> None:
    _write(tmp_path / "live_engine.json", {"status": "ok"})
    publisher = HealthcheckPublisher(state_dir=tmp_path)
    port = publisher.start(host="127.0.0.1", port=0)
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=2.0) as resp:
            assert resp.status == 200
            body = json.loads(resp.read().decode("utf-8"))
        assert body["status"] == "ok"
    finally:
        publisher.stop()


def test_http_404_on_unknown_path(tmp_path: Path) -> None:
    _write(tmp_path / "live_engine.json", {"status": "ok"})
    publisher = HealthcheckPublisher(state_dir=tmp_path)
    port = publisher.start(host="127.0.0.1", port=0)
    try:
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/other", timeout=2.0)
        except urllib.error.HTTPError as exc:
            assert exc.code == 404
    finally:
        publisher.stop()


def test_http_503_when_degraded(tmp_path: Path) -> None:
    publisher = HealthcheckPublisher(
        state_dir=tmp_path,
        required_components=["live_engine"],
    )
    port = publisher.start(host="127.0.0.1", port=0)
    try:
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=2.0)
            raise AssertionError("expected HTTPError")
        except urllib.error.HTTPError as exc:
            assert exc.code == 503
            body = json.loads(exc.read().decode("utf-8"))
            assert body["status"] == "degraded"
    finally:
        publisher.stop()


def test_http_double_start_raises(tmp_path: Path) -> None:
    publisher = HealthcheckPublisher(state_dir=tmp_path)
    publisher.start(host="127.0.0.1", port=0)
    try:
        with pytest.raises(RuntimeError):
            publisher.start(host="127.0.0.1", port=0)
    finally:
        publisher.stop()


def test_http_stop_when_not_started(tmp_path: Path) -> None:
    publisher = HealthcheckPublisher(state_dir=tmp_path)
    publisher.stop()  # no-op


def test_status_dataclass_http_status() -> None:
    hs_ok = HealthStatus(status="ok", components={}, ts=0.0)
    hs_down = HealthStatus(status="down", components={}, ts=0.0)
    assert hs_ok.http_status == 200
    assert hs_down.http_status == 503


def test_state_dir_property(tmp_path: Path) -> None:
    publisher = HealthcheckPublisher(state_dir=tmp_path)
    assert publisher.state_dir == tmp_path


def test_evaluate_handles_stat_oserror(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "live_engine.json"
    _write(path, {"status": "ok"})
    original_stat = Path.stat

    def boom(self: Path, *args: Any, **kwargs: Any) -> Any:
        if self == path:
            raise OSError("simulated")
        return original_stat(self, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", boom)
    publisher = HealthcheckPublisher(state_dir=tmp_path)
    snap = publisher.evaluate()
    assert snap.components["live_engine"]["status"] == "degraded"
    assert "stat failed" in snap.components["live_engine"]["detail"]


def test_evaluate_recognises_warning_status(tmp_path: Path) -> None:
    _write(tmp_path / "live_engine.json", {"status": "warning"})
    publisher = HealthcheckPublisher(state_dir=tmp_path)
    snap = publisher.evaluate()
    assert snap.status == "degraded"


def test_evaluate_recognises_error_status(tmp_path: Path) -> None:
    _write(tmp_path / "live_engine.json", {"status": "error"})
    publisher = HealthcheckPublisher(state_dir=tmp_path)
    snap = publisher.evaluate()
    assert snap.status == "down"
