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


# --------------------------------------------------------------------------
# Auth — require_mcp_key same-origin exemption + defense-in-depth (B-17)
# --------------------------------------------------------------------------


class _FakeClient:
    def __init__(self, host: str):
        self.host = host


class _FakeAuthReq:
    """Minimal stand-in for fastapi.Request matching what require_mcp_key reads."""

    def __init__(self, headers: dict, client_host: str = "127.0.0.1"):
        # FastAPI normalises header lookup to lowercase; mirror that.
        self.headers = {k.lower(): v for k, v in headers.items()}
        self.client = _FakeClient(client_host)


def test_pause_requires_auth_from_foreign_origin(monkeypatch):
    """A request whose Origin doesn't match Host must NOT bypass auth even
    if the peer is loopback (e.g. a reverse proxy injecting a spoofed
    Origin). With no Bearer header, this must 401."""
    from fastapi import HTTPException
    monkeypatch.setattr(ops_routes, "_DASHBOARD_MCP_KEY", "test-key-for-b17")
    req = _FakeAuthReq(
        headers={"Origin": "http://evil.example.com", "Host": "localhost:8081"},
        client_host="127.0.0.1",
    )
    with pytest.raises(HTTPException) as exc:
        ops_routes.require_mcp_key(request=req, authorization=None)
    assert exc.value.status_code == 401


def test_pause_allows_same_origin_from_localhost(monkeypatch):
    """Same Origin + Host AND loopback peer → bypass returns None (allowed)."""
    monkeypatch.setattr(ops_routes, "_DASHBOARD_MCP_KEY", "test-key-for-b17")
    req = _FakeAuthReq(
        headers={"Origin": "http://localhost:8081", "Host": "localhost:8081"},
        client_host="127.0.0.1",
    )
    assert ops_routes.require_mcp_key(request=req, authorization=None) is None


def test_same_origin_from_public_peer_requires_auth(monkeypatch):
    """Reverse-proxy attack scenario: Origin and Host match (proxy rewrites
    them) but the peer is a public address (e.g. 93.184.216.34). The
    same-origin bypass must refuse — bearer becomes required."""
    from fastapi import HTTPException
    monkeypatch.setattr(ops_routes, "_DASHBOARD_MCP_KEY", "test-key-for-b17")
    req = _FakeAuthReq(
        headers={"Origin": "http://localhost:8081", "Host": "localhost:8081"},
        client_host="93.184.216.34",
    )
    with pytest.raises(HTTPException) as exc:
        ops_routes.require_mcp_key(request=req, authorization=None)
    assert exc.value.status_code == 401


def test_same_origin_from_docker_bridge_peer_allowed(monkeypatch):
    """Docker port-forwarding scenario: host binds 127.0.0.1:8081 (P0-V),
    but inside the container the connection's peer is the docker bridge
    gateway IP (e.g. 172.19.0.1). P0-V already refused any traffic that
    wasn't 127.0.0.1 on the host, so an RFC1918 peer here is safe to trust."""
    monkeypatch.setattr(ops_routes, "_DASHBOARD_MCP_KEY", "test-key-for-b17")
    req = _FakeAuthReq(
        headers={"Origin": "http://localhost:8081", "Host": "localhost:8081"},
        client_host="172.19.0.1",
    )
    assert ops_routes.require_mcp_key(request=req, authorization=None) is None


def test_bearer_token_still_works_from_any_peer(monkeypatch):
    """Cron jobs / MCP tools authenticate via Authorization: Bearer header
    even from non-loopback peers. That path must remain unbroken."""
    monkeypatch.setattr(ops_routes, "_DASHBOARD_MCP_KEY", "test-key-for-b17")
    req = _FakeAuthReq(
        headers={"Host": "localhost:8081"},  # no Origin -- bypass path skipped
        client_host="93.184.216.34",
    )
    assert ops_routes.require_mcp_key(
        request=req, authorization="Bearer test-key-for-b17"
    ) is None


# --------------------------------------------------------------------------
# Regime config editor — POST /api/ops/regime_config
# --------------------------------------------------------------------------


class _FakeRegimeReq:
    """Minimal stand-in for fastapi.Request used by regime_config_post."""

    def __init__(self, body: dict):
        self._body = body
        self.headers = {"content-length": str(len(json.dumps(body)))}

    async def json(self):
        return self._body


def _stub_httpx_async_client(monkeypatch):
    """Neutralise the best-effort freqtrade /reload_config POST inside
    regime_config_post so the test doesn't need a live freqtrade. The handler
    swallows exceptions and records them in reload_status, so we just need
    the call to not block forever / not network out."""

    class _Resp:
        status_code = 200

    class _Client:
        def __init__(self, *a, **kw): ...
        async def __aenter__(self): return self
        async def __aexit__(self, *exc): return False
        async def post(self, *a, **kw): return _Resp()
        async def get(self, *a, **kw): return _Resp()

    monkeypatch.setattr(ops_routes.httpx, "AsyncClient", _Client)

    async def _fake_jwt(client, force_refresh: bool = False):
        return "fake-jwt-token"

    monkeypatch.setattr(ops_routes, "_ensure_jwt", _fake_jwt)


@pytest.mark.asyncio
async def test_regime_min_stable_hours_roundtrips(tmp_path, monkeypatch):
    """`regime_min_stable_hours` must be accepted by the dashboard validator
    and round-trip via the GET handler. Regression: before this fix, POST
    rejected the param with 'unknown param: regime_min_stable_hours'."""
    # Seed a config.json with an existing regime_gating block (matching the
    # shape the strategy reads — including a stale value we will overwrite).
    cfg_path = tmp_path / "config.json"
    initial_cfg = {
        "regime_gating": {
            "_doc": "operator-tunable",
            "entry_delta": {"trending_up": 0.0, "trending_down": None},
            "exit_delta": {"trending_up": 0.0},
            "high_vol_stake_factor": 0.5,
            "high_vol_min_confidence": 0.6,
            "mean_rev_take_profit": 0.02,
            "trending_up_trail_trigger": 0.01,
            "trending_up_trail_distance": -0.005,
            "tft_min_confidence": 0.5,
            "meta_min_confidence": 0.5,
            "regime_min_stable_hours": 2.0,
        }
    }
    cfg_path.write_text(json.dumps(initial_cfg, indent=4))

    monkeypatch.setattr(ops_routes, "CONFIG_PATH", cfg_path)
    monkeypatch.setattr(ops_routes, "USER_DATA_ROOT_FOR_BACKUPS", tmp_path)
    _stub_httpx_async_client(monkeypatch)

    # POST a new value for regime_min_stable_hours alongside another known param.
    new_value = 3.5
    req = _FakeRegimeReq({"regime_gating": {
        "regime_min_stable_hours": new_value,
        "mean_rev_take_profit": 0.03,
    }})
    env = await ops_routes.regime_config_post(req)

    # 200 OK envelope, status='ok', and the change must be in the diff list.
    _assert_envelope(env)
    assert env["status"] == "ok"
    changes = env["data"]["changes"]
    assert any("regime_min_stable_hours" in c for c in changes), (
        f"expected regime_min_stable_hours in changes, got {changes!r}"
    )

    # On-disk config now reflects the new value.
    written = json.loads(cfg_path.read_text())
    assert written["regime_gating"]["regime_min_stable_hours"] == new_value

    # And GET surfaces it back through the handler (round-trip).
    got = ops_routes.regime_config_get()
    _assert_envelope(got)
    assert got["status"] == "ok"
    assert got["data"]["regime_gating"]["regime_min_stable_hours"] == new_value
    # Schema exposes the range so the UI can render bounds.
    assert "regime_min_stable_hours" in got["data"]["schema"]["scalar_ranges"]
    lo, hi = got["data"]["schema"]["scalar_ranges"]["regime_min_stable_hours"]
    assert lo == 0.0 and hi == 24.0


@pytest.mark.asyncio
async def test_regime_min_stable_hours_out_of_range_rejected(tmp_path, monkeypatch):
    """Values outside [0, 24] must 400 — sanity-range enforcement."""
    from fastapi import HTTPException

    cfg_path = tmp_path / "config.json"
    cfg_path.write_text(json.dumps({"regime_gating": {"regime_min_stable_hours": 2.0}}))
    monkeypatch.setattr(ops_routes, "CONFIG_PATH", cfg_path)
    monkeypatch.setattr(ops_routes, "USER_DATA_ROOT_FOR_BACKUPS", tmp_path)
    _stub_httpx_async_client(monkeypatch)

    req = _FakeRegimeReq({"regime_gating": {"regime_min_stable_hours": 48.0}})
    with pytest.raises(HTTPException) as exc:
        await ops_routes.regime_config_post(req)
    assert exc.value.status_code == 400
    assert "regime_min_stable_hours" in str(exc.value.detail)
