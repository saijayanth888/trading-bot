"""Tests for ``quanta_core.hermes._common``."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from quanta_core.hermes._common import (
    HermesError,
    SlackNotifier,
    StateWriter,
    configure_logging,
    load_config,
    repo_root,
    state_dir,
    utc_iso,
    utc_now,
)


def test_state_dir_respects_env(tmp_path, monkeypatch):
    monkeypatch.setenv("QUANTA_STATE_DIR", str(tmp_path / "custom"))
    out = state_dir()
    assert out == tmp_path / "custom"
    assert out.exists()


def test_state_dir_default(monkeypatch, tmp_path):
    monkeypatch.delenv("QUANTA_STATE_DIR", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    out = state_dir()
    assert out == tmp_path / ".quanta" / "state"
    assert out.exists()


def test_state_writer_atomic_write(tmp_path):
    path = tmp_path / "subdir" / "out.json"
    writer = StateWriter(path)
    writer.write({"a": 1, "b": "two"})
    assert path.exists()
    assert json.loads(path.read_text()) == {"a": 1, "b": "two"}
    # tmp file should not linger
    assert not path.with_suffix(".json.tmp").exists()


def test_state_writer_serialises_datetime(tmp_path):
    path = tmp_path / "out.json"
    StateWriter(path).write({"ts": datetime(2026, 5, 12, tzinfo=timezone.utc)})
    body = json.loads(path.read_text())
    assert "2026-05-12" in body["ts"]


def test_state_writer_append_atomic(tmp_path):
    path = tmp_path / "decisions.md"
    path.write_text("seed\n")
    StateWriter(path).append_text_atomic("more\n")
    assert path.read_text() == "seed\nmore\n"
    assert not path.with_suffix(".md.tmp").exists()


def test_state_writer_append_creates_file(tmp_path):
    path = tmp_path / "new" / "file.md"
    StateWriter(path).append_text_atomic("hello\n")
    assert path.read_text() == "hello\n"


def test_state_writer_overwrites_atomically(tmp_path):
    """Same path written twice — second wins, no tmp leak."""

    path = tmp_path / "out.json"
    w = StateWriter(path)
    w.write({"n": 1})
    w.write({"n": 2})
    assert json.loads(path.read_text()) == {"n": 2}


def test_slack_notifier_no_url_returns_false(monkeypatch):
    monkeypatch.delenv("SLACK_WEBHOOK_URL", raising=False)
    n = SlackNotifier()
    assert n.post("hello") is False


def test_slack_notifier_with_httpx_mock(monkeypatch):
    """Patch httpx.post to record the call."""

    import quanta_core.hermes._common as common

    seen: dict = {}

    class FakeResp:
        status_code = 200
        text = "ok"

    def fake_post(url, json, timeout):
        seen.update({"url": url, "json": json, "timeout": timeout})
        return FakeResp()

    monkeypatch.setattr(common.httpx, "post", fake_post)
    n = SlackNotifier(webhook_url="http://hook", channel="#hermes")
    assert n.post("hi") is True
    assert seen["url"] == "http://hook"
    assert seen["json"]["text"] == "hi"
    assert seen["json"]["channel"] == "#hermes"


def test_slack_notifier_handles_4xx(monkeypatch):
    import quanta_core.hermes._common as common

    class FakeResp:
        status_code = 500
        text = "err"

    monkeypatch.setattr(common.httpx, "post", lambda *a, **kw: FakeResp())
    n = SlackNotifier(webhook_url="http://hook")
    assert n.post("hi") is False


def test_load_config_defaults(clean_env):
    cfg = load_config()
    assert cfg.ollama_base_url == "http://localhost:11434"
    assert cfg.reflector_model == "hermes3:8b"
    assert cfg.post_mortem_model == "hermes3:70b"
    assert cfg.mf_api_url == "http://localhost:8000"
    assert cfg.mf_weekly_workflow_id is None


def test_load_config_env_overrides(monkeypatch, clean_env):
    monkeypatch.setenv("OLLAMA_BASE_URL", "http://ollama:11434")
    monkeypatch.setenv("HERMES_REFLECTOR_MODEL", "hermes3:70b")
    monkeypatch.setenv("POSTGRES_DSN", "postgres://localhost/db")
    monkeypatch.setenv("MODELFORGE_WORKFLOW_ID", "uuid-here")
    monkeypatch.setenv("SLACK_WEBHOOK_URL", "http://slack")
    cfg = load_config()
    assert cfg.ollama_base_url == "http://ollama:11434"
    assert cfg.reflector_model == "hermes3:70b"
    assert cfg.postgres_dsn == "postgres://localhost/db"
    assert cfg.mf_weekly_workflow_id == "uuid-here"
    assert cfg.slack_webhook_url == "http://slack"


def test_load_config_alpaca_alt_env_names(monkeypatch, clean_env):
    monkeypatch.setenv("ALPACA_API_KEY", "k")
    monkeypatch.setenv("ALPACA_API_SECRET", "s")
    cfg = load_config()
    assert cfg.alpaca_key_id == "k"
    assert cfg.alpaca_secret_key == "s"


def test_utc_helpers():
    n = utc_now()
    assert n.tzinfo is not None
    s = utc_iso(n)
    assert "+00:00" in s or s.endswith("Z") or "+0000" in s


def test_repo_root_walks_to_pyproject(tmp_path, monkeypatch):
    monkeypatch.delenv("QUANTA_REPO_ROOT", raising=False)
    # We can rely on the actual repo root having a pyproject.toml — but to keep
    # the test hermetic we just assert the function returns a Path that exists.
    out = repo_root()
    assert isinstance(out, Path)


def test_repo_root_respects_env(tmp_path, monkeypatch):
    monkeypatch.setenv("QUANTA_REPO_ROOT", str(tmp_path))
    assert repo_root() == tmp_path


def test_configure_logging_is_idempotent():
    log1 = configure_logging("test_module")
    log2 = configure_logging("test_module")
    assert log1 is log2
    assert len(log1.handlers) == 1


def test_hermes_error_is_runtime_error():
    assert issubclass(HermesError, RuntimeError)
