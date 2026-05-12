"""Tests for ``quanta_core.hermes.gpu_yield_adapter``."""

from __future__ import annotations

import json

import pytest

from quanta_core.hermes import gpu_yield_adapter as gya
from tests.hermes.conftest import FakeNotifier


def test_run_script_missing_returns_127(tmp_path):
    out = gya._run_script(tmp_path / "missing.sh", timeout=1.0)
    code, _stdout, stderr = out
    assert code == 127
    assert "script not found" in stderr


def test_run_script_executes(tmp_path):
    script = tmp_path / "echo.sh"
    script.write_text("#!/usr/bin/env bash\necho hi\n")
    script.chmod(0o755)
    code, stdout, _stderr = gya._run_script(script, timeout=5.0)
    assert code == 0
    assert "hi" in stdout


def test_run_script_handles_nonzero(tmp_path):
    script = tmp_path / "fail.sh"
    script.write_text("#!/usr/bin/env bash\nexit 7\n")
    script.chmod(0o755)
    code, _, _ = gya._run_script(script, timeout=5.0)
    assert code == 7


def test_run_script_timeout(tmp_path):
    script = tmp_path / "slow.sh"
    script.write_text("#!/usr/bin/env bash\nsleep 5\n")
    script.chmod(0o755)
    code, _, stderr = gya._run_script(script, timeout=0.1)
    assert code == 124
    assert "timeout" in stderr


def test_yield_now_records_state(monkeypatch, state_root, clean_env, tmp_path):
    fake_script = tmp_path / "yield.sh"
    fake_script.write_text("#!/usr/bin/env bash\necho yielded\n")
    fake_script.chmod(0o755)

    monkeypatch.setattr(gya, "YIELD_SCRIPT", fake_script)
    notifier = FakeNotifier()
    monkeypatch.setattr(gya, "SlackNotifier", lambda *a, **k: notifier)

    from quanta_core.hermes._common import load_config

    cfg = load_config()
    code = gya.yield_now(cfg)
    assert code == 0
    state = json.loads((state_root / "last_gpu_yield.json").read_text())
    assert state["action"] == "yield_now"
    assert state["exit_code"] == 0
    assert "yielded" in state["stdout_tail"]
    # No slack post on success
    assert notifier.posts == []


def test_yield_now_failure_alerts(monkeypatch, state_root, clean_env, tmp_path):
    fake_script = tmp_path / "yield.sh"
    fake_script.write_text("#!/usr/bin/env bash\nexit 1\n")
    fake_script.chmod(0o755)

    monkeypatch.setattr(gya, "YIELD_SCRIPT", fake_script)
    notifier = FakeNotifier()
    monkeypatch.setattr(gya, "SlackNotifier", lambda *a, **k: notifier)

    from quanta_core.hermes._common import load_config

    cfg = load_config()
    code = gya.yield_now(cfg)
    assert code == 1
    assert any("gpu_yield" in p for p in notifier.posts)


def test_resume_records_state(monkeypatch, state_root, clean_env, tmp_path):
    fake_script = tmp_path / "resume.sh"
    fake_script.write_text("#!/usr/bin/env bash\necho resumed\n")
    fake_script.chmod(0o755)
    monkeypatch.setattr(gya, "RESUME_SCRIPT", fake_script)
    monkeypatch.setattr(gya, "SlackNotifier", lambda *a, **k: FakeNotifier())
    from quanta_core.hermes._common import load_config

    cfg = load_config()
    code = gya.resume(cfg)
    assert code == 0
    state = json.loads((state_root / "last_gpu_resume.json").read_text())
    assert state["action"] == "resume"
    assert "resumed" in state["stdout_tail"]


def test_entrypoint_yield_action(monkeypatch, state_root, clean_env, tmp_path):
    fake_script = tmp_path / "yield.sh"
    fake_script.write_text("#!/usr/bin/env bash\nexit 0\n")
    fake_script.chmod(0o755)
    monkeypatch.setattr(gya, "YIELD_SCRIPT", fake_script)
    monkeypatch.setattr(gya, "SlackNotifier", lambda *a, **k: FakeNotifier())
    assert gya.run(["yield"]) == 0


def test_entrypoint_resume_action(monkeypatch, state_root, clean_env, tmp_path):
    fake_script = tmp_path / "resume.sh"
    fake_script.write_text("#!/usr/bin/env bash\nexit 0\n")
    fake_script.chmod(0o755)
    monkeypatch.setattr(gya, "RESUME_SCRIPT", fake_script)
    monkeypatch.setattr(gya, "SlackNotifier", lambda *a, **k: FakeNotifier())
    assert gya.run(["resume"]) == 0


def test_entrypoint_bad_action():
    with pytest.raises(SystemExit):
        gya.run(["nonsense"])
