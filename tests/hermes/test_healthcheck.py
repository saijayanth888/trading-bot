"""Tests for ``quanta_core.hermes.healthcheck``."""

from __future__ import annotations

import json

from quanta_core.hermes._common import load_config
from quanta_core.hermes.healthcheck import (
    ProbeResult,
    aggregate,
    maybe_post_alert,
    probe_alpaca,
    probe_coinbase,
    probe_mf_api,
    probe_ollama,
    probe_postgres,
)
from tests.hermes.conftest import FakeNotifier


def test_probe_ollama_uses_client(monkeypatch, clean_env):
    cfg = load_config()
    # Patch OllamaClient.ping to return predictable values
    import quanta_core.hermes._ollama as o

    monkeypatch.setattr(o.OllamaClient, "ping", lambda self: (True, 12.5, ["hermes3:8b"]))
    result = probe_ollama(cfg)
    assert result.ok is True
    assert result.detail["resident_models"] == ["hermes3:8b"]


def test_probe_postgres_no_dsn(clean_env):
    cfg = load_config()
    assert cfg.postgres_dsn is None
    result = probe_postgres(cfg)
    assert result.ok is False


def test_probe_alpaca_missing_credentials(clean_env):
    cfg = load_config()
    result = probe_alpaca(cfg)
    assert result.ok is False
    assert result.error == "missing_credentials"


def test_probe_alpaca_with_keys(monkeypatch, clean_env):
    monkeypatch.setenv("ALPACA_KEY_ID", "k")
    monkeypatch.setenv("ALPACA_SECRET_KEY", "s")
    cfg = load_config()

    class FakeResp:
        status_code = 200

        def json(self):
            return {"status": "ACTIVE"}

    import quanta_core.hermes.healthcheck as h

    monkeypatch.setattr(h.httpx, "get", lambda *a, **k: FakeResp())
    result = probe_alpaca(cfg)
    assert result.ok is True
    assert result.detail["account_status"] == "ACTIVE"


def test_probe_alpaca_non_200(monkeypatch, clean_env):
    monkeypatch.setenv("ALPACA_KEY_ID", "k")
    monkeypatch.setenv("ALPACA_SECRET_KEY", "s")
    cfg = load_config()

    class FakeResp:
        status_code = 403

    import quanta_core.hermes.healthcheck as h

    monkeypatch.setattr(h.httpx, "get", lambda *a, **k: FakeResp())
    result = probe_alpaca(cfg)
    assert result.ok is False
    assert result.error == "http_403"


def test_probe_alpaca_exception(monkeypatch, clean_env):
    monkeypatch.setenv("ALPACA_KEY_ID", "k")
    monkeypatch.setenv("ALPACA_SECRET_KEY", "s")
    cfg = load_config()
    import quanta_core.hermes.healthcheck as h

    def raises(*a, **k):
        raise OSError("nope")

    monkeypatch.setattr(h.httpx, "get", raises)
    result = probe_alpaca(cfg)
    assert result.ok is False


def test_probe_coinbase_missing(clean_env):
    cfg = load_config()
    assert probe_coinbase(cfg).ok is False


def test_probe_coinbase_ok(monkeypatch, clean_env):
    monkeypatch.setenv("COINBASE_API_KEY", "k")
    cfg = load_config()

    class FakeResp:
        status_code = 200

    import quanta_core.hermes.healthcheck as h

    monkeypatch.setattr(h.httpx, "get", lambda *a, **k: FakeResp())
    assert probe_coinbase(cfg).ok is True


def test_probe_mf_api_ok(monkeypatch, clean_env):
    cfg = load_config()

    class FakeResp:
        status_code = 200

    import quanta_core.hermes.healthcheck as h

    monkeypatch.setattr(h.httpx, "get", lambda *a, **k: FakeResp())
    assert probe_mf_api(cfg).ok is True


def test_probe_mf_api_non_200(monkeypatch, clean_env):
    cfg = load_config()

    class FakeResp:
        status_code = 500

    import quanta_core.hermes.healthcheck as h

    monkeypatch.setattr(h.httpx, "get", lambda *a, **k: FakeResp())
    assert probe_mf_api(cfg).ok is False


def test_aggregate_resets_consec_on_success():
    probes = [ProbeResult("ollama", True, 1.0), ProbeResult("postgres", True, 2.0)]
    prev = {"consecutive_failures": 3, "any_failure": True}
    state = aggregate(probes, prev)
    assert state["any_failure"] is False
    assert state["consecutive_failures"] == 0


def test_aggregate_increments_on_failure():
    probes = [ProbeResult("ollama", False, 0.0)]
    prev = {"consecutive_failures": 2, "any_failure": True}
    state = aggregate(probes, prev)
    assert state["consecutive_failures"] == 3
    assert state["any_failure"] is True


def test_aggregate_handles_no_prev_state():
    probes = [ProbeResult("ollama", False, 0.0)]
    state = aggregate(probes, None)
    assert state["consecutive_failures"] == 1


def test_maybe_post_alert_fires_above_threshold():
    notifier = FakeNotifier()
    state = {
        "any_failure": True,
        "consecutive_failures": 3,
        "ollama": {"ok": False},
        "postgres": {"ok": True},
    }
    posted = maybe_post_alert(state, threshold=3, notifier=notifier)
    assert posted is True
    assert "ollama" in notifier.posts[0]


def test_maybe_post_alert_silent_below_threshold():
    notifier = FakeNotifier()
    state = {"any_failure": True, "consecutive_failures": 1}
    assert maybe_post_alert(state, threshold=3, notifier=notifier) is False


def test_maybe_post_alert_silent_when_ok():
    notifier = FakeNotifier()
    state = {"any_failure": False, "consecutive_failures": 0}
    assert maybe_post_alert(state, threshold=3, notifier=notifier) is False


def test_run_writes_state(monkeypatch, state_root, clean_env):
    """End-to-end run with all probes patched to fast-fail."""

    import quanta_core.hermes.healthcheck as h

    monkeypatch.setattr(h, "probe_ollama", lambda cfg: ProbeResult("ollama", True, 1.0))
    monkeypatch.setattr(h, "probe_postgres", lambda cfg: ProbeResult("postgres", True, 1.0))
    monkeypatch.setattr(h, "probe_alpaca", lambda cfg: ProbeResult("alpaca", True, 1.0))
    monkeypatch.setattr(h, "probe_coinbase", lambda cfg: ProbeResult("coinbase", True, 1.0))
    monkeypatch.setattr(h, "probe_mf_api", lambda cfg: ProbeResult("mf_api", True, 1.0))
    monkeypatch.setattr(h, "SlackNotifier", lambda *a, **k: FakeNotifier())

    code = h.run([])
    assert code == 0
    state = json.loads((state_root / "healthcheck_last.json").read_text())
    assert state["any_failure"] is False
    assert state["ollama"]["ok"] is True


def test_run_persists_consec_across_runs(monkeypatch, state_root, clean_env):
    """Second run with failures must read prev state and increment."""

    import quanta_core.hermes.healthcheck as h

    notifier = FakeNotifier()
    monkeypatch.setattr(h, "probe_ollama", lambda cfg: ProbeResult("ollama", False, 1.0))
    monkeypatch.setattr(h, "probe_postgres", lambda cfg: ProbeResult("postgres", True, 1.0))
    monkeypatch.setattr(h, "probe_alpaca", lambda cfg: ProbeResult("alpaca", True, 1.0))
    monkeypatch.setattr(h, "probe_coinbase", lambda cfg: ProbeResult("coinbase", True, 1.0))
    monkeypatch.setattr(h, "probe_mf_api", lambda cfg: ProbeResult("mf_api", True, 1.0))
    monkeypatch.setattr(h, "SlackNotifier", lambda *a, **k: notifier)

    # threshold high so no slack fires
    import os

    os.environ["HERMES_HEALTH_FAIL_THRESHOLD"] = "5"
    for _ in range(3):
        assert h.run([]) == 0
    state = json.loads((state_root / "healthcheck_last.json").read_text())
    assert state["consecutive_failures"] == 3
    os.environ.pop("HERMES_HEALTH_FAIL_THRESHOLD")
