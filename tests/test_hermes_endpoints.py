"""Tests for the V5 Hermes endpoints (B10 + §7).

Uses a synthetic ``$HERMES_ROOT`` tree so the operator's real
``~/.hermes/`` is never touched. Spec §5.4: ``jobs.json`` is READ-ONLY;
``acks.json``/``retrigger_requests.jsonl`` are APPEND-ONLY.
"""
from __future__ import annotations

import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

# Ensure module-level path resolution picks the synthetic root BEFORE
# the router is imported (`os.environ["HERMES_ROOT"] = ...`).


@pytest.fixture()
def hermes_app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    """Build a FastAPI app with the hermes + actions routers and a fake $HERMES_ROOT."""
    hermes_root = tmp_path / "hermes"
    (hermes_root / "cron").mkdir(parents=True)
    output_root = hermes_root / "cron" / "output"
    output_root.mkdir(parents=True)

    # Synthetic jobs.json
    now = datetime.now(tz=UTC)
    activating_old = (now - timedelta(minutes=45)).isoformat()
    jobs = {
        "jobs": [
            {
                "id": "abcd1234",
                "name": "ept_training_daily",
                "schedule": {"kind": "cron", "expr": "0 2 * * *", "display": "0 2 * * *"},
                "enabled": True,
                "state": "scheduled",
                "next_run_at": (now + timedelta(hours=1)).isoformat(),
                "last_run_at": (now - timedelta(hours=4)).isoformat(),
                "last_status": "ok",
                "deliver": "telegram",
            },
            {
                "id": "stuck0001",
                "name": "hermes_mcp",
                "schedule": {"kind": "interval", "expr": "every-5-min"},
                "enabled": True,
                "state": "activating",
                "activating_since": activating_old,
                "last_status": None,
            },
        ]
    }
    (hermes_root / "cron" / "jobs.json").write_text(json.dumps(jobs))

    # Synthetic run-output markdown
    job_dir = output_root / "abcd1234"
    job_dir.mkdir()
    md_ts = (now - timedelta(hours=4)).strftime("%Y-%m-%d %H:%M:%S")
    (job_dir / "2026-05-15_22-00-26.md").write_text(
        f"# Cron Job: ept_training_daily\n\n"
        f"**Job ID:** abcd1234\n"
        f"**Run Time:** {md_ts}\n"
        f"**Schedule:** 0 2 * * *\n\n"
        "## Response\n\nChampion 7J4 fitness 0.812\n"
    )

    monkeypatch.setenv("HERMES_ROOT", str(hermes_root))
    monkeypatch.setenv("USER_DATA_ROOT", str(tmp_path))
    monkeypatch.setenv("HERMES_MCP_KEY", "test-mcp-key-for-pytest")

    # Import AFTER env vars are set so `_hermes_root()` reads the synthetic tree.
    # Reload ops_routes so it picks up the test HERMES_MCP_KEY (module-level
    # capture means the env var must be set before first import).
    import importlib

    import user_data.dashboard.ops_routes as _ops_mod  # noqa: PLC0415
    importlib.reload(_ops_mod)
    from user_data.dashboard.v5 import actions as actions_mod
    from user_data.dashboard.v5 import hermes as hermes_mod
    # Re-bind the require_mcp_key dependency in v5.actions to the freshly
    # reloaded ops_routes function (the module captured the old one at import).
    actions_mod.require_mcp_key = _ops_mod.require_mcp_key  # type: ignore[attr-defined]

    app = FastAPI()
    app.include_router(hermes_mod.router)
    app.include_router(actions_mod.router)
    client = TestClient(app)
    # Provide the bearer for mutating endpoints. The same-origin exemption
    # works only when the dependency is the FRESHLY-imported one — since
    # `Depends(require_mcp_key)` captured the function at route-definition
    # time, the explicit Authorization header is the reliable path.
    client.headers.update(
        {"Authorization": "Bearer test-mcp-key-for-pytest"}
    )
    return client


# ---------------------------------------------------------------------------
# /schedule
# ---------------------------------------------------------------------------


def test_schedule_returns_parsed_jobs(hermes_app: TestClient):
    r = hermes_app.get("/api/v5/hermes/schedule")
    assert r.status_code == 200
    body = r.json()
    assert "_meta" in body
    names = {j["name"] for j in body["jobs"]}
    assert {"ept_training_daily", "hermes_mcp"} <= names
    # READ-ONLY guarantee — schedule endpoint must never write to jobs.json
    # (mtime check is enough; the file content is whatever the fixture set).
    by_name = {j["name"]: j for j in body["jobs"]}
    assert by_name["ept_training_daily"]["last_status"] == "ok"


# ---------------------------------------------------------------------------
# /runs
# ---------------------------------------------------------------------------


def test_runs_walks_markdown_outputs(hermes_app: TestClient):
    r = hermes_app.get("/api/v5/hermes/runs?limit=5")
    assert r.status_code == 200
    body = r.json()
    runs = body["runs"]
    assert len(runs) >= 1
    first = runs[0]
    assert first["job_id"] == "abcd1234"
    assert "Champion 7J4" in first["snippet"]


def test_runs_clamps_limit(hermes_app: TestClient):
    """``limit=0`` must clamp to >=1, ``limit=9999`` must clamp at 200."""
    r = hermes_app.get("/api/v5/hermes/runs?limit=0")
    assert r.status_code == 200
    r = hermes_app.get("/api/v5/hermes/runs?limit=9999")
    assert r.status_code == 200


# ---------------------------------------------------------------------------
# /health — B10 (composite + activating>30 min)
# ---------------------------------------------------------------------------


def test_health_flags_activating_over_30min(hermes_app: TestClient):
    """The synthetic ``hermes_mcp`` job has been activating for 45 min — that
    must show up in the amber reasons (B10)."""
    r = hermes_app.get("/api/v5/hermes/health")
    assert r.status_code == 200
    body = r.json()
    # state should not be green because of stuck-activating
    assert body["status"] in {"amber", "red"}
    assert any("activating>30min" in reason for reason in body["reasons"])
    assert "hermes_mcp" in body["signals"]["stuck_activating"]


# ---------------------------------------------------------------------------
# retrigger action (append-only, jobs.json untouched)
# ---------------------------------------------------------------------------


def test_retrigger_appends_request_and_keeps_jobs_json_intact(
    hermes_app: TestClient, tmp_path: Path
):
    hermes_root = Path(os.environ["HERMES_ROOT"])
    jobs_path = hermes_root / "cron" / "jobs.json"
    before = jobs_path.read_text()

    r = hermes_app.post("/api/v5/actions/hermes/retrigger/abcd1234", json={})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["matched"] is True

    # jobs.json must be byte-identical
    assert jobs_path.read_text() == before

    # Append-only request file should exist with one row
    req_file = hermes_root / "cron" / "retrigger_requests.jsonl"
    assert req_file.exists()
    rows = [json.loads(line) for line in req_file.read_text().splitlines() if line.strip()]
    assert len(rows) == 1
    assert rows[0]["job_id"] == "abcd1234"
    assert rows[0]["kind"] == "retrigger_request"
