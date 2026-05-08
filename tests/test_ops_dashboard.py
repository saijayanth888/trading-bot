"""
Smoke + unit tests for the Ops tab.

Covers:
  - the typed envelope shape returned by every /api/ops/* endpoint
  - probe primitives (TCP / HTTP / heartbeat) on synthetic inputs
  - degraded-mode handling when underlying sources fail
  - pause/resume guards (confirm flag, drawdown gate)

Run from the project root:
    pytest tests/test_ops_dashboard.py -v
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from pathlib import Path
from unittest.mock import patch

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "user_data"))

from dashboard import ops_probes, ops_db, ops_routes  # noqa: E402


# --------------------------------------------------------------------------
# Probes
# --------------------------------------------------------------------------


def test_heartbeat_missing_file(tmp_path):
    """Missing heartbeat file → up=False with 'missing' error."""
    out = ops_probes.heartbeat_probe(tmp_path / "no-such-file")
    assert out["up"] is False
    assert "missing" in out["error"]


def test_heartbeat_fresh_active(tmp_path):
    """Fresh file with content 'active' → up=True."""
    p = tmp_path / "alive"
    p.write_text("active\n")
    out = ops_probes.heartbeat_probe(p, max_age_s=120)
    assert out["up"] is True
    assert out["content"] == "active"
    assert out["age_s"] >= 0


def test_heartbeat_stale(tmp_path):
    """File present but mtime old → up=False."""
    p = tmp_path / "alive"
    p.write_text("active\n")
    old = time.time() - 600
    os.utime(p, (old, old))
    out = ops_probes.heartbeat_probe(p, max_age_s=60)
    assert out["up"] is False


def test_heartbeat_inactive_content(tmp_path):
    """Fresh file with content 'inactive' → up=False."""
    p = tmp_path / "alive"
    p.write_text("inactive\n")
    out = ops_probes.heartbeat_probe(p, max_age_s=120)
    assert out["up"] is False
    assert out["content"] == "inactive"


def test_tft_log_parse():
    """The regex correctly extracts epoch / max / val_sharpe / loss."""
    line = "2026-05-08 18:42:53,186 - TFTModel - INFO - epoch 4/25  loss=1.1098 (ce=1.0530 q=0.1895)  val_sharpe=0.910  lr=9.67e-04  step=2624"
    fake_path = Path("/tmp/_does_not_matter")
    # _parse_tft_line touches log_path for mtime — patch so it doesn't.
    with patch.object(Path, "stat", lambda self: type("S", (), {"st_mtime": time.time()})()):
        out = ops_probes._parse_tft_line(line, fake_path)
    assert out["epoch"] == 4
    assert out["max_epoch"] == 25
    assert abs(out["val_sharpe"] - 0.910) < 1e-6
    assert abs(out["loss"] - 1.1098) < 1e-4


# --------------------------------------------------------------------------
# Envelope shape — every endpoint must return {status, data, error, checked_at}
# --------------------------------------------------------------------------


def _assert_envelope(env: dict):
    assert isinstance(env, dict)
    assert env.get("status") in ("ok", "degraded", "down")
    assert "data" in env
    assert "error" in env
    assert "checked_at" in env


@pytest.mark.asyncio
async def test_services_envelope_when_all_down(monkeypatch):
    """If every probe returns up=False, status must be 'down'."""
    async def fake_summary():
        return {k: {"up": False, "via": "tcp", "endpoint": "x", "error": "refused"}
                for k in ("ollama", "hermes_mcp", "hermes_gateway", "hermes_dashboard",
                          "freqtrade", "postgres", "influxdb", "grafana")}
    monkeypatch.setattr(ops_probes, "services_summary", fake_summary)
    env = await ops_routes.services()
    _assert_envelope(env)
    assert env["status"] == "down"


@pytest.mark.asyncio
async def test_services_envelope_partial(monkeypatch):
    """One probe down → status='degraded', error names the offender."""
    async def fake_summary():
        out = {k: {"up": True, "via": "tcp", "endpoint": "x"} for k in
               ("ollama", "hermes_mcp", "hermes_gateway", "hermes_dashboard",
                "freqtrade", "postgres", "influxdb", "grafana")}
        out["postgres"] = {"up": False, "via": "tcp", "endpoint": "postgres:5432", "error": "refused"}
        return out
    monkeypatch.setattr(ops_probes, "services_summary", fake_summary)
    env = await ops_routes.services()
    _assert_envelope(env)
    assert env["status"] == "degraded"
    assert "postgres" in (env["error"] or "")


@pytest.mark.asyncio
async def test_training_envelope_no_signals(monkeypatch):
    """Empty result → degraded with 'no training signals' error."""
    monkeypatch.setattr(ops_probes, "training_state", lambda: {"tft": None, "drl": None, "ept": None})
    env = await ops_routes.training()
    _assert_envelope(env)
    assert env["status"] == "degraded"


@pytest.mark.asyncio
async def test_regime_envelope_empty_table(monkeypatch):
    """If regime_log returns nothing → degraded."""
    monkeypatch.setattr(ops_db, "regime_latest", lambda: None)
    monkeypatch.setattr(ops_db, "regime_transitions_24h", lambda limit=10: [])
    env = await ops_routes.regime()
    _assert_envelope(env)
    assert env["status"] == "degraded"


@pytest.mark.asyncio
async def test_sentiment_envelope_empty(monkeypatch):
    monkeypatch.setattr(ops_db, "sentiment_latest", lambda: None)
    monkeypatch.setattr(ops_db, "sentiment_hourly_24h", lambda: [])
    env = await ops_routes.sentiment()
    _assert_envelope(env)
    assert env["status"] == "degraded"


# --------------------------------------------------------------------------
# Pause / Resume guards
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resume_requires_confirm():
    """POST /resume with confirm=false must 400."""
    from fastapi import HTTPException

    class FakeReq:
        headers = {"content-length": "10"}
        async def json(self): return {"confirm": False}

    with pytest.raises(HTTPException) as exc:
        await ops_routes.resume(FakeReq())
    assert exc.value.status_code == 400


@pytest.mark.asyncio
async def test_resume_refuses_high_drawdown(monkeypatch):
    """If 30d drawdown < -6%, resume must 409 even with confirm=true."""
    from fastapi import HTTPException
    monkeypatch.setattr(ops_db, "trades_risk_summary", lambda: {
        "drawdown_pct_30d": -7.5,
        "circuit_breaker": {"active": False, "cooldown_remaining_min": 0},
    })

    class FakeReq:
        headers = {"content-length": "20"}
        async def json(self): return {"confirm": True}

    with pytest.raises(HTTPException) as exc:
        await ops_routes.resume(FakeReq())
    assert exc.value.status_code == 409
    assert "drawdown" in str(exc.value.detail).lower()


@pytest.mark.asyncio
async def test_resume_refuses_active_breaker(monkeypatch):
    from fastapi import HTTPException
    monkeypatch.setattr(ops_db, "trades_risk_summary", lambda: {
        "drawdown_pct_30d": 0,
        "circuit_breaker": {"active": True, "cooldown_remaining_min": 90},
    })

    class FakeReq:
        headers = {"content-length": "20"}
        async def json(self): return {"confirm": True}

    with pytest.raises(HTTPException) as exc:
        await ops_routes.resume(FakeReq())
    assert exc.value.status_code == 409
    assert "breaker" in str(exc.value.detail).lower()
