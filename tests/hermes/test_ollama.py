"""Tests for ``quanta_core.hermes._ollama``."""

from __future__ import annotations

from quanta_core.hermes._ollama import OllamaClient


class _FakeResp:
    def __init__(self, status: int, body: dict | None = None, text: str = ""):
        self.status_code = status
        self._body = body or {}
        self.text = text

    def json(self):
        return self._body


def test_generate_success(monkeypatch):
    import quanta_core.hermes._ollama as o

    def fake_post(url, json, timeout):
        assert "/api/generate" in url
        assert json["model"] == "hermes3:8b"
        return _FakeResp(200, {"response": "  lesson text  "})

    monkeypatch.setattr(o.httpx, "post", fake_post)
    client = OllamaClient(timeout_seconds=1.0)
    out = client.generate("hermes3:8b", "prompt", system="sys")
    assert out == "lesson text"


def test_generate_non_200(monkeypatch):
    import quanta_core.hermes._ollama as o

    monkeypatch.setattr(o.httpx, "post", lambda *a, **k: _FakeResp(500, text="boom"))
    assert OllamaClient().generate("m", "p") is None


def test_generate_exception(monkeypatch):
    import quanta_core.hermes._ollama as o

    def raises(*a, **k):
        raise OSError("conn refused")

    monkeypatch.setattr(o.httpx, "post", raises)
    assert OllamaClient().generate("m", "p") is None


def test_list_resident(monkeypatch):
    import quanta_core.hermes._ollama as o

    monkeypatch.setattr(
        o.httpx,
        "get",
        lambda *a, **k: _FakeResp(
            200, {"models": [{"name": "hermes3:8b"}, {"name": "qwen3:30b"}]}
        ),
    )
    assert OllamaClient().list_resident() == ["hermes3:8b", "qwen3:30b"]


def test_list_resident_non_200(monkeypatch):
    import quanta_core.hermes._ollama as o

    monkeypatch.setattr(o.httpx, "get", lambda *a, **k: _FakeResp(500))
    assert OllamaClient().list_resident() == []


def test_ping_ok(monkeypatch):
    import quanta_core.hermes._ollama as o

    monkeypatch.setattr(
        o.httpx,
        "get",
        lambda *a, **k: _FakeResp(200, {"models": [{"name": "hermes3:8b"}]}),
    )
    ok, lat, resident = OllamaClient().ping()
    assert ok is True
    assert lat >= 0
    assert resident == ["hermes3:8b"]


def test_ping_fail(monkeypatch):
    import quanta_core.hermes._ollama as o

    def raises(*a, **k):
        raise OSError("nope")

    monkeypatch.setattr(o.httpx, "get", raises)
    ok, _lat, resident = OllamaClient().ping()
    assert ok is False
    assert resident == []
