"""Tests for ``quanta_core.hermes.lora_promoter``."""

from __future__ import annotations

import json
from datetime import date

from quanta_core.hermes import lora_promoter
from quanta_core.hermes.lora_promoter import (
    MfApiClient,
    _read_champions_json,
    _records_from_champions,
    _training_window,
)
from tests.hermes.conftest import FakeNotifier


def test_training_window_returns_mon_sun():
    # 2026-05-12 is a Tuesday, last completed week is Mon 5-04 → Sun 5-10
    monday, sunday = _training_window(date(2026, 5, 12))
    assert monday == "2026-05-04"
    assert sunday == "2026-05-10"


def test_records_from_promotions_list():
    data = {
        "promotions": [
            {"role": "arbiter", "pareto_pass": True, "from": "v16", "to": "v17", "metrics": {"hit_rate": 0.6}},
            {"role": "reflector", "pareto_pass": False, "kept_champion": "v9"},
        ]
    }
    recs = _records_from_champions(data)
    assert len(recs) == 2
    assert recs[0].role == "arbiter"
    assert recs[0].pareto_pass is True
    assert recs[0].metrics["hit_rate"] == 0.6
    assert recs[1].kept_champion == "v9"


def test_records_from_tracks_dict():
    data = {
        "tracks": {
            "trading-bull": {"promoted": True, "generation": "v3"},
            "trading-bear": {"promoted": False},
        }
    }
    recs = _records_from_champions(data)
    assert {r.role for r in recs} == {"trading-bull", "trading-bear"}


def test_records_from_bare_list():
    data = [{"role": "x", "pareto_pass": True, "generation": "v2"}]
    recs = _records_from_champions(data)
    assert len(recs) == 1
    assert recs[0].role == "x"


def test_read_champions_json_finds_in_state(tmp_path, monkeypatch):
    from quanta_core.hermes._common import load_config

    monkeypatch.setenv("QUANTA_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    (tmp_path / "home").mkdir(parents=True, exist_ok=True)
    (tmp_path / "champions.json").write_text(json.dumps({"promotions": [{"role": "x", "pareto_pass": True}]}))
    cfg = load_config()
    data = _read_champions_json(cfg)
    assert data is not None
    assert data["promotions"][0]["role"] == "x"


def test_read_champions_json_missing_returns_none(tmp_path, monkeypatch):
    from quanta_core.hermes._common import load_config

    monkeypatch.setenv("QUANTA_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    (tmp_path / "home").mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("QUANTA_REPO_ROOT", str(tmp_path / "repo"))
    (tmp_path / "repo").mkdir(parents=True, exist_ok=True)
    cfg = load_config()
    assert _read_champions_json(cfg) is None


class _FakeResp:
    def __init__(self, status: int, body: dict | None = None):
        self.status_code = status
        self._body = body or {}
        self.text = json.dumps(self._body)

    def json(self):
        return self._body


def test_mfapi_trigger_workflow_ok(monkeypatch):
    import quanta_core.hermes.lora_promoter as lp

    captured: dict = {}

    def fake_post(url, headers, timeout):
        captured["url"] = url
        captured["headers"] = headers
        return _FakeResp(200, {"run_id": "r1"})

    monkeypatch.setattr(lp.httpx, "post", fake_post)
    client = MfApiClient("http://mf", api_key="k")
    out = client.trigger_workflow("uuid")
    assert out == {"run_id": "r1"}
    assert "uuid" in captured["url"]
    assert captured["headers"]["X-API-Key"] == "k"


def test_mfapi_trigger_workflow_4xx(monkeypatch):
    import quanta_core.hermes.lora_promoter as lp

    monkeypatch.setattr(lp.httpx, "post", lambda *a, **k: _FakeResp(403))
    assert MfApiClient("http://mf", api_key="k").trigger_workflow("uuid") is None


def test_mfapi_trigger_exception(monkeypatch):
    import quanta_core.hermes.lora_promoter as lp

    def raises(*a, **k):
        raise OSError("nope")

    monkeypatch.setattr(lp.httpx, "post", raises)
    assert MfApiClient("http://mf", None).trigger_workflow("u") is None


def test_mfapi_latest_run_extracts_first(monkeypatch):
    import quanta_core.hermes.lora_promoter as lp

    body = {"runs": [{"id": "r1", "status": "completed"}, {"id": "r2"}]}
    monkeypatch.setattr(lp.httpx, "get", lambda *a, **k: _FakeResp(200, body))
    run = MfApiClient("http://mf", None).latest_run("uuid")
    assert run == {"id": "r1", "status": "completed"}


def test_run_dry_run_emits_state(state_root, clean_env, monkeypatch):
    monkeypatch.setenv("MODELFORGE_WORKFLOW_ID", "wf-1")
    code = lora_promoter.run(["--dry-run"])
    assert code == 0
    state = json.loads((state_root / "last_lora_promotion.json").read_text())
    assert state["dry_run"] is True
    assert state["workflow_id"] == "wf-1"


def test_run_missing_workflow_id_returns_2(state_root, clean_env):
    code = lora_promoter.run([])
    assert code == 2
    state = json.loads((state_root / "last_lora_promotion.json").read_text())
    assert state["error"] == "no_workflow_id"


def test_run_trigger_failure_returns_1(state_root, clean_env, monkeypatch):
    monkeypatch.setenv("MODELFORGE_WORKFLOW_ID", "wf-1")
    import quanta_core.hermes.lora_promoter as lp

    monkeypatch.setattr(lp.MfApiClient, "trigger_workflow", lambda self, w: None)
    monkeypatch.setattr(lp, "SlackNotifier", lambda *a, **k: FakeNotifier())
    code = lp.run([])
    assert code == 1
    state = json.loads((state_root / "last_lora_promotion.json").read_text())
    assert state["error"] == "trigger_failed"


def test_run_happy_path(state_root, clean_env, monkeypatch, tmp_path):
    monkeypatch.setenv("MODELFORGE_WORKFLOW_ID", "wf-1")
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / ".dgx-train").mkdir(parents=True, exist_ok=True)
    (tmp_path / ".dgx-train" / "champions.json").write_text(
        json.dumps({"promotions": [{"role": "arbiter", "pareto_pass": True, "to": "v3"}]})
    )

    import quanta_core.hermes.lora_promoter as lp

    monkeypatch.setattr(lp.MfApiClient, "trigger_workflow", lambda self, w: {"ok": True})
    monkeypatch.setattr(
        lp.MfApiClient,
        "latest_run",
        lambda self, w: {"id": "r1", "status": "completed"},
    )

    notifier = FakeNotifier()
    monkeypatch.setattr(lp, "SlackNotifier", lambda *a, **k: notifier)
    monkeypatch.setattr(lp, "_poll_until_done", lambda *a, **k: {"id": "r1", "status": "completed"})

    code = lp.run([])
    assert code == 0
    state = json.loads((state_root / "last_lora_promotion.json").read_text())
    assert state["run_status"] == "completed"
    assert state["promotions"][0]["role"] == "arbiter"
    assert any("promoted=arbiter" in p for p in notifier.posts)


def test_poll_until_done_returns_terminal(monkeypatch):
    """Polling helper exits on the first terminal status."""

    import logging

    import quanta_core.hermes.lora_promoter as lp

    calls = []

    class FakeClient:
        def latest_run(self, w):
            calls.append(w)
            statuses = ["running", "completed"]
            idx = min(len(calls) - 1, 1)
            return {"id": "r1", "status": statuses[idx]}

    monkeypatch.setattr(lp.time, "sleep", lambda s: None)
    out = lp._poll_until_done(FakeClient(), "w", interval=0, max_seconds=10, log=logging.getLogger())
    assert out is not None
    assert out["status"] == "completed"


def test_poll_until_done_timeout(monkeypatch):
    import logging

    import quanta_core.hermes.lora_promoter as lp

    class FakeClient:
        def latest_run(self, w):
            return {"status": "running"}

    monkeypatch.setattr(lp.time, "sleep", lambda s: None)
    # set max_seconds=0 so the loop exits immediately
    out = lp._poll_until_done(FakeClient(), "w", interval=0, max_seconds=0, log=logging.getLogger())
    assert out is None or out["status"] == "running"
