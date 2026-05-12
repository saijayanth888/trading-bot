"""
Unit tests for ``scripts/modelforge_register_tracks.py``.

Covers:
  * Request body for each of the 6 tracks matches the ModelForge
    ``evolution_tracks`` row schema (per ``lineage_db.py:820-848``).
  * Idempotency:
      - existing track (GET 200)     → no POST, status "already_registered"
      - missing track  (GET 404)     → POST issued,  status "created"
      - per-id route missing (405)   → falls back to list-scan
  * ``--dry-run`` path: zero POSTs, status "dry_run".
  * ``--force`` path: POST issued even when the track exists.
  * ``--delete`` path: DELETE issued; 404 returns "skipped"; 200 returns
    "deleted".
  * API-key resolution prefers CLI > env > ``~/.env-modelforge`` > ``./.env``.

ModelForge is never actually contacted — httpx is monkey-patched with a
recorder that captures requests and returns canned responses.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import httpx
import pytest

# --------------------------------------------------------------------------- #
# Import the script under test.
# --------------------------------------------------------------------------- #

_REPO_ROOT = Path(__file__).resolve().parents[1]
_SCRIPT_PATH = _REPO_ROOT / "scripts" / "modelforge_register_tracks.py"


def _load_module():
    spec = importlib.util.spec_from_file_location(
        "modelforge_register_tracks", _SCRIPT_PATH
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules["modelforge_register_tracks"] = module
    spec.loader.exec_module(module)
    return module


mod = _load_module()


# --------------------------------------------------------------------------- #
# Fake httpx.Client — records every call and returns scripted responses.
# --------------------------------------------------------------------------- #


@dataclass
class FakeRequest:
    method: str
    url: str
    json_body: Any | None = None


@dataclass
class FakeResponse:
    status_code: int
    body: Any = field(default_factory=dict)
    text_override: str | None = None

    def json(self) -> Any:
        if isinstance(self.body, (dict, list)):
            return self.body
        raise ValueError("non-JSON body")

    @property
    def text(self) -> str:
        if self.text_override is not None:
            return self.text_override
        try:
            return json.dumps(self.body)
        except (TypeError, ValueError):
            return str(self.body)

    def raise_for_status(self) -> None:
        if 400 <= self.status_code < 600:
            raise httpx.HTTPStatusError(
                f"HTTP {self.status_code}",
                request=httpx.Request("GET", "http://test/"),
                response=httpx.Response(self.status_code),
            )


class FakeClient:
    """Drop-in for httpx.Client used inside ``register_one`` / ``delete_one``."""

    def __init__(
        self,
        *,
        responder: Callable[[str, str], FakeResponse],
        headers: dict[str, str] | None = None,
        timeout: float | None = None,
    ) -> None:
        self.requests: list[FakeRequest] = []
        self._responder = responder
        self.headers = dict(headers or {})
        self.timeout = timeout

    # Context manager protocol so ``with httpx.Client(...) as c:`` works.
    def __enter__(self) -> "FakeClient":
        return self

    def __exit__(self, *exc: Any) -> None:  # noqa: D401
        return None

    def get(self, url: str, **kwargs: Any) -> FakeResponse:
        self.requests.append(FakeRequest("GET", url))
        return self._responder("GET", url)

    def post(self, url: str, json: Any | None = None, **kwargs: Any) -> FakeResponse:
        self.requests.append(FakeRequest("POST", url, json_body=json))
        return self._responder("POST", url)

    def delete(self, url: str, **kwargs: Any) -> FakeResponse:
        self.requests.append(FakeRequest("DELETE", url))
        return self._responder("DELETE", url)


def _patch_httpx(monkeypatch: pytest.MonkeyPatch, fake: FakeClient) -> None:
    """Replace ``httpx.Client`` so the script gets our fake under ``with``."""

    def _factory(*args: Any, **kwargs: Any) -> FakeClient:
        # Mirror the script's call: ``httpx.Client(timeout=..., headers=...)``.
        fake.headers.update(dict(kwargs.get("headers") or {}))
        fake.timeout = kwargs.get("timeout", fake.timeout)
        return fake

    monkeypatch.setattr(mod.httpx, "Client", _factory)


# --------------------------------------------------------------------------- #
# Schema / track-list sanity
# --------------------------------------------------------------------------- #


def test_six_tracks_with_expected_ids():
    ids = mod.track_ids()
    assert ids == [
        "trading-reflector",
        "trading-bull",
        "trading-bear",
        "trading-arbiter",
        "trading-regime-tagger",
        "trading-indicator-selector",
    ]


@pytest.mark.parametrize("track", mod.TRACKS, ids=lambda t: t["id"])
def test_post_body_schema(track: dict[str, Any]):
    """Every track produces a body conforming to evolution_tracks schema."""
    body = mod.build_post_body(track)
    # Required columns (lineage_db.py:820-848)
    for col in (
        "track_id",
        "name",
        "description",
        "base_model",
        "target_benchmarks",
        "lora_rank",
        "lora_alpha",
        "learning_rate",
        "max_samples",
        "enabled",
    ):
        assert col in body, f"{track['id']} missing column {col}"
    assert isinstance(body["track_id"], str) and body["track_id"]
    assert isinstance(body["name"], str) and body["name"]
    assert isinstance(body["description"], str) and body["description"]
    assert isinstance(body["base_model"], str) and body["base_model"]
    assert isinstance(body["target_benchmarks"], list)
    assert all(isinstance(b, str) for b in body["target_benchmarks"])
    assert body["target_benchmarks"], "target_benchmarks must not be empty"
    assert isinstance(body["lora_rank"], int) and body["lora_rank"] >= 1
    assert isinstance(body["lora_alpha"], int) and body["lora_alpha"] >= 1
    assert isinstance(body["learning_rate"], float) and body["learning_rate"] > 0
    assert isinstance(body["max_samples"], int) and body["max_samples"] > 0
    assert body["enabled"] is True
    # Spec extras present (operator may want them once ModelForge schema bumps).
    assert "schedule" in body
    assert "expected_data_path" in body


def test_all_tracks_use_qwen3_30b_base():
    """Locked decision: qwen3:30b base for 6+ months. Don't drift accidentally."""
    for t in mod.TRACKS:
        assert t["base_model"] == "qwen3:30b", t["id"]


def test_indicator_selector_role_string_mentions_eight_indicators():
    isel = next(t for t in mod.TRACKS if t["id"] == "trading-indicator-selector")
    assert "8" in isel["role"]


# --------------------------------------------------------------------------- #
# Idempotency
# --------------------------------------------------------------------------- #


def _responder_per_id_known(status_map: dict[str, int]):
    """Responder that maps the URL's track_id suffix to a GET status code.

    POST always returns 201. DELETE returns 204 unless mapped to 404.
    """

    def _r(method: str, url: str) -> FakeResponse:
        if method == "GET":
            # If listing endpoint (no id suffix)
            if url.endswith("/api/forge/tracks"):
                # Build payload from the keys with status 200.
                rows = [
                    {"track_id": tid, "name": tid}
                    for tid, code in status_map.items()
                    if code == 200
                ]
                return FakeResponse(200, {"tracks": rows})
            tid = url.rsplit("/", 1)[-1]
            code = status_map.get(tid, 404)
            return FakeResponse(code, {"track_id": tid} if code == 200 else {})
        if method == "POST":
            return FakeResponse(201, {"ok": True})
        if method == "DELETE":
            tid = url.rsplit("/", 1)[-1]
            code = status_map.get(tid, 404)
            return FakeResponse(204 if code == 200 else 404, {})
        return FakeResponse(500)

    return _r


def test_register_all_when_none_exist(monkeypatch: pytest.MonkeyPatch, capsys):
    """All 6 GETs are 404 → 6 POSTs issued, all marked ``created``."""
    fake = FakeClient(responder=_responder_per_id_known({}))
    _patch_httpx(monkeypatch, fake)
    rc = mod.main([])
    assert rc == 0
    posts = [r for r in fake.requests if r.method == "POST"]
    assert len(posts) == 6
    posted_ids = sorted(r.json_body["track_id"] for r in posts)
    assert posted_ids == sorted(mod.track_ids())


def test_idempotent_when_all_exist(monkeypatch: pytest.MonkeyPatch, capsys):
    """All 6 GETs are 200 → 0 POSTs, all marked ``already_registered``."""
    status_map = {tid: 200 for tid in mod.track_ids()}
    fake = FakeClient(responder=_responder_per_id_known(status_map))
    _patch_httpx(monkeypatch, fake)
    rc = mod.main([])
    assert rc == 0
    posts = [r for r in fake.requests if r.method == "POST"]
    assert len(posts) == 0
    out = capsys.readouterr().out
    assert "already_registered" in out
    assert "6/6 OK" in out


def test_partial_existing(monkeypatch: pytest.MonkeyPatch):
    """3 exist, 3 don't → exactly 3 POSTs issued."""
    existing = ["trading-reflector", "trading-bull", "trading-arbiter"]
    status_map = {tid: 200 for tid in existing}
    fake = FakeClient(responder=_responder_per_id_known(status_map))
    _patch_httpx(monkeypatch, fake)
    rc = mod.main([])
    assert rc == 0
    posts = [r for r in fake.requests if r.method == "POST"]
    assert len(posts) == 3
    posted_ids = sorted(r.json_body["track_id"] for r in posts)
    assert posted_ids == sorted(
        tid for tid in mod.track_ids() if tid not in existing
    )


# --------------------------------------------------------------------------- #
# --dry-run / --force
# --------------------------------------------------------------------------- #


def test_dry_run_issues_no_writes(monkeypatch: pytest.MonkeyPatch, capsys):
    fake = FakeClient(responder=_responder_per_id_known({}))
    _patch_httpx(monkeypatch, fake)
    rc = mod.main(["--dry-run"])
    assert rc == 0
    # Dry-run short-circuits before the GET as well — zero HTTP calls.
    assert fake.requests == []
    out = capsys.readouterr().out
    assert "dry_run" in out


def test_force_reposts_existing(monkeypatch: pytest.MonkeyPatch, capsys):
    status_map = {tid: 200 for tid in mod.track_ids()}
    fake = FakeClient(responder=_responder_per_id_known(status_map))
    _patch_httpx(monkeypatch, fake)
    rc = mod.main(["--force"])
    assert rc == 0
    posts = [r for r in fake.requests if r.method == "POST"]
    assert len(posts) == 6
    out = capsys.readouterr().out
    assert "forced_update" in out


# --------------------------------------------------------------------------- #
# Single-track + unknown id
# --------------------------------------------------------------------------- #


def test_single_track_selection(monkeypatch: pytest.MonkeyPatch):
    fake = FakeClient(responder=_responder_per_id_known({}))
    _patch_httpx(monkeypatch, fake)
    rc = mod.main(["--track", "trading-bull"])
    assert rc == 0
    posts = [r for r in fake.requests if r.method == "POST"]
    assert len(posts) == 1
    assert posts[0].json_body["track_id"] == "trading-bull"


def test_unknown_track_raises(monkeypatch: pytest.MonkeyPatch):
    fake = FakeClient(responder=_responder_per_id_known({}))
    _patch_httpx(monkeypatch, fake)
    with pytest.raises(SystemExit):
        mod.main(["--track", "trading-nope"])


# --------------------------------------------------------------------------- #
# Fallback: per-id GET returns 405 → script scans the list endpoint.
# --------------------------------------------------------------------------- #


def test_per_id_405_falls_back_to_list(monkeypatch: pytest.MonkeyPatch):
    """If GET /api/forge/tracks/{id} returns 405, we list and scan."""
    calls = {"per_id_gets": 0, "list_gets": 0}

    def _r(method: str, url: str) -> FakeResponse:
        if method == "GET":
            if url.endswith("/api/forge/tracks"):
                calls["list_gets"] += 1
                # Pretend the reflector exists, nothing else.
                return FakeResponse(200, {"tracks": [{"track_id": "trading-reflector"}]})
            calls["per_id_gets"] += 1
            return FakeResponse(405, {})
        if method == "POST":
            return FakeResponse(201, {})
        return FakeResponse(500)

    fake = FakeClient(responder=_r)
    _patch_httpx(monkeypatch, fake)
    rc = mod.main([])
    assert rc == 0
    posts = [r for r in fake.requests if r.method == "POST"]
    # Reflector exists, so 5 should be posted.
    assert len(posts) == 5
    assert all(
        r.json_body["track_id"] != "trading-reflector" for r in posts
    )
    assert calls["list_gets"] >= 6  # one list-scan per per-id 405


# --------------------------------------------------------------------------- #
# --delete (rollback)
# --------------------------------------------------------------------------- #


def test_delete_existing_track(monkeypatch: pytest.MonkeyPatch):
    status_map = {"trading-reflector": 200}
    fake = FakeClient(responder=_responder_per_id_known(status_map))
    _patch_httpx(monkeypatch, fake)
    rc = mod.main(["--delete", "trading-reflector"])
    assert rc == 0
    deletes = [r for r in fake.requests if r.method == "DELETE"]
    assert len(deletes) == 1
    assert deletes[0].url.endswith("/trading-reflector")


def test_delete_missing_returns_skipped(monkeypatch: pytest.MonkeyPatch, capsys):
    fake = FakeClient(responder=_responder_per_id_known({}))
    _patch_httpx(monkeypatch, fake)
    rc = mod.main(["--delete", "trading-reflector"])
    # 404 maps to "skipped" which is OK.
    assert rc == 0
    out = capsys.readouterr().out
    assert "skipped" in out


def test_delete_dry_run_issues_no_call(monkeypatch: pytest.MonkeyPatch):
    fake = FakeClient(responder=_responder_per_id_known({}))
    _patch_httpx(monkeypatch, fake)
    rc = mod.main(["--delete", "trading-reflector", "--dry-run"])
    assert rc == 0
    assert fake.requests == []


# --------------------------------------------------------------------------- #
# HTTP error surfacing
# --------------------------------------------------------------------------- #


def test_500_post_returns_nonzero(monkeypatch: pytest.MonkeyPatch, capsys):
    def _r(method: str, url: str) -> FakeResponse:
        if method == "GET":
            # Per-id GET returns 404 (track not present).
            # List GET returns empty so the fallback existence-check passes.
            if url.endswith("/api/forge/tracks"):
                return FakeResponse(200, {"tracks": []})
            return FakeResponse(404, {})
        if method == "POST":
            return FakeResponse(
                500,
                text_override="internal error",
            )
        return FakeResponse(500)

    fake = FakeClient(responder=_r)
    _patch_httpx(monkeypatch, fake)
    rc = mod.main(["--track", "trading-reflector"])
    assert rc == 1
    out = capsys.readouterr().out
    assert "error" in out
    assert "500" in out


def test_network_error_returns_nonzero(monkeypatch: pytest.MonkeyPatch):
    def _r(method: str, url: str) -> FakeResponse:
        raise httpx.ConnectError("connection refused")

    fake = FakeClient(responder=_r)
    _patch_httpx(monkeypatch, fake)
    rc = mod.main(["--track", "trading-reflector"])
    assert rc == 1


# --------------------------------------------------------------------------- #
# Auth + base URL resolution
# --------------------------------------------------------------------------- #


def test_api_key_cli_wins(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("MODELFORGE_API_KEY", "from-env")
    assert mod.resolve_api_key("from-cli") == "from-cli"


def test_api_key_env_used(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("MODELFORGE_API_KEY", "from-env")
    assert mod.resolve_api_key(None) == "from-env"


def test_api_key_env_file(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    monkeypatch.delenv("MODELFORGE_API_KEY", raising=False)
    home = tmp_path / "home"
    home.mkdir()
    env_file = home / ".env-modelforge"
    env_file.write_text('MODELFORGE_API_KEY="from-file"\n')
    monkeypatch.setattr(mod.Path, "home", lambda: home)
    # ./.env must not exist either — point cwd at an empty dir.
    monkeypatch.chdir(tmp_path)
    assert mod.resolve_api_key(None) == "from-file"


def test_api_key_dotenv_fallback(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    monkeypatch.delenv("MODELFORGE_API_KEY", raising=False)
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(mod.Path, "home", lambda: home)
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text("MODELFORGE_API_KEY=dotenv-key\n")
    assert mod.resolve_api_key(None) == "dotenv-key"


def test_api_key_none_when_unset(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    monkeypatch.delenv("MODELFORGE_API_KEY", raising=False)
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(mod.Path, "home", lambda: home)
    monkeypatch.chdir(tmp_path)
    assert mod.resolve_api_key(None) is None


def test_base_url_resolution(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("MODELFORGE_BASE_URL", raising=False)
    assert mod.resolve_base_url(None) == "http://localhost:8000"
    monkeypatch.setenv("MODELFORGE_BASE_URL", "http://remote:9000/")
    assert mod.resolve_base_url(None) == "http://remote:9000"
    assert mod.resolve_base_url("http://override:1") == "http://override:1"


def test_header_x_api_key_sent_when_key_present(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("MODELFORGE_API_KEY", "sekret")
    fake = FakeClient(responder=_responder_per_id_known({}))
    _patch_httpx(monkeypatch, fake)
    mod.main(["--track", "trading-reflector"])
    assert fake.headers.get("X-API-Key") == "sekret"


def test_header_no_x_api_key_when_unset(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    monkeypatch.delenv("MODELFORGE_API_KEY", raising=False)
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(mod.Path, "home", lambda: home)
    monkeypatch.chdir(tmp_path)
    fake = FakeClient(responder=_responder_per_id_known({}))
    _patch_httpx(monkeypatch, fake)
    mod.main(["--track", "trading-reflector"])
    assert "X-API-Key" not in fake.headers
