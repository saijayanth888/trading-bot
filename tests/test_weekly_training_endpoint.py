"""Unit tests for GET /api/ops/weekly_training — the WeeklyTrainingLive card.

Run from the repo root:
    pytest tests/test_weekly_training_endpoint.py -v

Covers:
  * envelope shape contract ({status, data, error, checked_at})
  * "happy path" — model-forge returns 3 tracks; endpoint enriches to 6 rows
  * "model-forge offline" — httpx ConnectError → status="degraded" + local fields populated
  * "no champion yet" — model-forge reachable but tracks have no scores → eligibility="no-data"
  * reflection count from decisions.md (today / earlier-this-week / before-Monday)
  * 6 canonical tracks are always returned, in canonical order
  * lessons_injected = None when llm-calls.jsonl is absent
  * endpoint has no auth dep (read-only)

Mocking strategy: same pattern as the existing test_ops_dashboard.py — patch
``ops_routes.httpx.AsyncClient`` with a tiny stub that returns a synthetic
JSON body, and patch the path constants to point at tmp_path fixtures so we
don't touch real files.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "user_data"))

from dashboard import ops_routes  # noqa: E402


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


def _assert_envelope(env: dict):
    """Every /api/ops/* endpoint must return this 4-key envelope shape.

    weekly_training emits a 4th status value ``ready`` for the "registered
    but no adapter trained yet" case (build-up week before the first Sunday
    training cycle). It's distinct from ``degraded`` so the card can render
    a "training pipeline starting up" badge instead of an error chip.
    """
    assert isinstance(env, dict), f"envelope not a dict: {type(env)}"
    assert env.get("status") in ("ok", "ready", "degraded", "down"), \
        f"unexpected status: {env.get('status')!r}"
    assert "data" in env
    assert "error" in env
    assert "checked_at" in env


def _stub_modelforge_response(monkeypatch, *, body, status_code: int = 200,
                              raise_exc: Exception | None = None):
    """Replace ops_routes.httpx.AsyncClient with a stub that returns ``body``.

    If ``raise_exc`` is set, the ``get()`` call raises it instead — used to
    simulate "model-forge unreachable" (ConnectError) and similar failure
    modes the endpoint must degrade-soft over.
    """
    class _Resp:
        def __init__(self, sc, b):
            self.status_code = sc
            self._b = b

        def json(self):
            return self._b

    class _Client:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def get(self, *a, **kw):
            if raise_exc is not None:
                raise raise_exc
            return _Resp(status_code, body)

        async def post(self, *a, **kw):
            return _Resp(status_code, body)

    monkeypatch.setattr(ops_routes.httpx, "AsyncClient", _Client)


def _seed_decisions(tmp_path: Path, lines: list[str]) -> Path:
    """Write a decisions.md with the supplied lines (no trailing newline mgmt
    — caller controls the exact bytes)."""
    p = tmp_path / "decisions.md"
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return p


def _today_iso(offset_days: int = 0) -> str:
    """Format today (or today + offset) as YYYY-MM-DD for decisions.md blocks."""
    return (datetime.now(timezone.utc) + timedelta(days=offset_days)).strftime("%Y-%m-%d")


def _point_paths_at(monkeypatch, *, decisions: Path | None, llm_calls: Path | None):
    """Point the endpoint's path-lookup at the test fixtures."""
    monkeypatch.setattr(
        ops_routes, "_DECISIONS_PATHS",
        [decisions] if decisions else [Path("/tmp/__nope__/decisions.md")],
    )
    monkeypatch.setattr(
        ops_routes, "_LLM_CALLS_PATHS",
        [llm_calls] if llm_calls else [Path("/tmp/__nope__/llm-calls.jsonl")],
    )


# ---------------------------------------------------------------------------
# 1 — envelope shape + canonical 6-track skeleton
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_envelope_shape_when_modelforge_returns_empty(monkeypatch, tmp_path):
    """No model-forge data yet → envelope still has the 4 keys + 6 tracks."""
    _stub_modelforge_response(monkeypatch, body={"tracks": []})
    _point_paths_at(monkeypatch, decisions=None, llm_calls=None)

    env = await ops_routes.weekly_training()

    _assert_envelope(env)
    assert env["data"]["summary"]["n_tracks_registered"] == 6
    assert env["data"]["summary"]["n_tracks_trained"] == 0
    assert len(env["data"]["tracks"]) == 6
    # Canonical order — important for stable screenshots.
    ids = [t["track_id"] for t in env["data"]["tracks"]]
    assert ids == [
        "trading-reflector",
        "trading-bull",
        "trading-bear",
        "trading-arbiter",
        "trading-regime-tagger",
        "trading-indicator-selector",
    ]


@pytest.mark.asyncio
async def test_status_ready_when_no_tracks_trained(monkeypatch):
    """Model-forge reachable but no champions yet → status='ready' with a
    descriptive error noting tracks are registered + awaiting first cycle.
    'ready' is distinct from 'degraded' so the card can render a build-up
    badge instead of an error chip during the pre-first-Sunday window."""
    _stub_modelforge_response(monkeypatch, body={"tracks": []})
    _point_paths_at(monkeypatch, decisions=None, llm_calls=None)

    env = await ops_routes.weekly_training()
    assert env["status"] == "ready"
    assert "tracks registered" in (env["error"] or "")
    assert "awaiting first training cycle" in (env["error"] or "")
    assert env["data"]["model_forge_reachable"] is True


# ---------------------------------------------------------------------------
# 2 — happy path: model-forge returns real tracks
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_modelforge_tracks_enriched_correctly(monkeypatch):
    """A champion adapter + scores should produce a fully-populated row."""
    now_iso = datetime.now(timezone.utc).isoformat()
    body = {
        "tracks": [
            {
                "track_id": "trading-reflector",
                "champion_adapter_path": "data/adapters/run-abc/gen-3",
                "champion_run_id": "run-abc__gen3",
                "champion_promoted_at": now_iso,
                "champion_scores": {
                    "faithfulness_regex": 0.81,
                    "predictive_hit_rate_30d": 0.62,
                    "judge_score": 0.74,
                },
                "last_train_num_samples": 47,
            },
            {
                "track_id": "trading-bull",
                "champion_adapter_path": "data/adapters/run-xyz/gen-1",
                "champion_promoted_at": now_iso,
                "champion_scores": {"judge_preference_pct": 0.58},
                "max_samples": 30,
            },
        ]
    }
    _stub_modelforge_response(monkeypatch, body=body)
    _point_paths_at(monkeypatch, decisions=None, llm_calls=None)

    env = await ops_routes.weekly_training()
    _assert_envelope(env)
    assert env["status"] == "ok"
    assert env["data"]["model_forge_reachable"] is True

    by_id = {t["track_id"]: t for t in env["data"]["tracks"]}
    refl = by_id["trading-reflector"]
    assert refl["eligibility"] == "promoted"
    assert refl["headline_metric"] == "predictive_hit_rate_30d"
    assert refl["headline_score"] == pytest.approx(0.62)
    assert refl["examples_trained_this_week"] == 47
    assert refl["current_adapter_version"] is not None
    assert refl["current_adapter_version"].startswith("v")

    bull = by_id["trading-bull"]
    assert bull["eligibility"] == "promoted"
    assert bull["headline_score"] == pytest.approx(0.58)

    # Untrained tracks still appear with no-data eligibility.
    bear = by_id["trading-bear"]
    assert bear["eligibility"] == "no-data"
    assert bear["current_adapter"] is None

    # Summary counts trained tracks correctly.
    assert env["data"]["summary"]["n_tracks_trained"] == 2
    assert env["data"]["summary"]["n_promoted_this_week"] == 2


# ---------------------------------------------------------------------------
# 3 — model-forge unreachable → degraded with local-only fields
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_modelforge_unreachable_degrades_soft(monkeypatch, tmp_path):
    """Connection refused must not 500 — endpoint returns degraded with
    local fields populated so the card still renders something useful."""
    _stub_modelforge_response(
        monkeypatch, body={}, raise_exc=httpx.ConnectError("Connection refused"),
    )

    today = _today_iso(0)
    decisions = _seed_decisions(tmp_path, [
        "# Decisions log — append-only", "",
        f"[{today} | NVDA | BUY | +1.5% | +0.5% alpha | 2d]",
        "DECISION: momentum continuation",
        "REFLECTION: trade closed on schedule, regime held BULL",
        "---",
    ])
    _point_paths_at(monkeypatch, decisions=decisions, llm_calls=None)

    env = await ops_routes.weekly_training()
    _assert_envelope(env)
    assert env["status"] == "degraded"
    assert env["data"]["model_forge_reachable"] is False
    assert "unreachable" in (env["data"]["model_forge_error"] or "")
    # Local-only signal must still be there.
    assert env["data"]["reflections_this_week"] == 1
    # 6 skeleton rows always returned.
    assert len(env["data"]["tracks"]) == 6
    for t in env["data"]["tracks"]:
        assert t["eligibility"] == "no-data"
        assert t["current_adapter"] is None


@pytest.mark.asyncio
async def test_modelforge_timeout_treated_as_unreachable(monkeypatch):
    """ConnectTimeout also degrades soft (not just ConnectError)."""
    _stub_modelforge_response(
        monkeypatch, body={}, raise_exc=httpx.ConnectTimeout("timeout"),
    )
    _point_paths_at(monkeypatch, decisions=None, llm_calls=None)

    env = await ops_routes.weekly_training()
    assert env["status"] == "degraded"
    assert env["data"]["model_forge_reachable"] is False


@pytest.mark.asyncio
async def test_modelforge_500_treated_as_unreachable(monkeypatch):
    """HTTP 500 from model-forge → degraded with HTTP-code error string."""
    _stub_modelforge_response(monkeypatch, body={}, status_code=500)
    _point_paths_at(monkeypatch, decisions=None, llm_calls=None)

    env = await ops_routes.weekly_training()
    assert env["status"] == "degraded"
    assert env["data"]["model_forge_reachable"] is False
    assert "HTTP 500" in (env["data"]["model_forge_error"] or "")


# ---------------------------------------------------------------------------
# 4 — reflection counter
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reflections_this_week_zero_when_file_empty(monkeypatch, tmp_path):
    """Empty decisions.md (header only) → 0 reflections."""
    _stub_modelforge_response(monkeypatch, body={"tracks": []})
    p = _seed_decisions(tmp_path, [
        "# Decisions log — append-only", "",
        "Format:",
        "[date | ticker | rating | pending]",
        "---",
    ])
    _point_paths_at(monkeypatch, decisions=p, llm_calls=None)

    env = await ops_routes.weekly_training()
    assert env["data"]["reflections_this_week"] == 0


@pytest.mark.asyncio
async def test_reflections_counts_only_this_week_entries(monkeypatch, tmp_path):
    """Entries dated before this week's Monday must NOT count."""
    _stub_modelforge_response(monkeypatch, body={"tracks": []})
    # 10 days ago — definitely before this week's Monday.
    old = (datetime.now(timezone.utc) - timedelta(days=10)).strftime("%Y-%m-%d")
    today = _today_iso(0)
    p = _seed_decisions(tmp_path, [
        "# Decisions log — append-only", "",
        f"[{old} | AAPL | SELL | -1.0% | -0.2% alpha | 1d]",
        "DECISION: technical breakdown",
        "REFLECTION: thesis broke at -7% stop, exited cleanly",
        "---",
        f"[{today} | NVDA | BUY | +1.5% | +0.5% alpha | 2d]",
        "DECISION: momentum continuation",
        "REFLECTION: trade closed on schedule",
        "---",
        f"[{today} | AMD  | BUY | +0.2% | +0.0% alpha | 1d]",
        "DECISION: breakout test",
        "REFLECTION: flat day, regime stayed neutral",
        "---",
    ])
    _point_paths_at(monkeypatch, decisions=p, llm_calls=None)

    env = await ops_routes.weekly_training()
    # Old entry excluded → 2 reflections counted, not 3.
    assert env["data"]["reflections_this_week"] == 2


@pytest.mark.asyncio
async def test_reflections_skips_empty_reflection_lines(monkeypatch, tmp_path):
    """``REFLECTION:`` with no body must NOT count (still-pending trade)."""
    _stub_modelforge_response(monkeypatch, body={"tracks": []})
    today = _today_iso(0)
    p = _seed_decisions(tmp_path, [
        "# Decisions log — append-only", "",
        f"[{today} | TSLA | BUY | pending]",
        "DECISION: catalyst on earnings",
        "REFLECTION:",
        "---",
        f"[{today} | NVDA | BUY | +1.5% | +0.5% alpha | 2d]",
        "DECISION: momentum continuation",
        "REFLECTION: a real reflection sentence here",
        "---",
    ])
    _point_paths_at(monkeypatch, decisions=p, llm_calls=None)

    env = await ops_routes.weekly_training()
    assert env["data"]["reflections_this_week"] == 1


@pytest.mark.asyncio
async def test_lessons_injected_none_when_log_absent(monkeypatch, tmp_path):
    """No llm-calls.jsonl on disk → lessons_injected is None (not 0)."""
    _stub_modelforge_response(monkeypatch, body={"tracks": []})
    _point_paths_at(monkeypatch, decisions=None, llm_calls=None)

    env = await ops_routes.weekly_training()
    assert env["data"]["lessons_injected"] is None


@pytest.mark.asyncio
async def test_lessons_injected_counts_tool_calls(monkeypatch, tmp_path):
    """get_past_context() invocations this week are counted."""
    _stub_modelforge_response(monkeypatch, body={"tracks": []})
    now_iso = datetime.now(timezone.utc).isoformat()
    old_iso = (datetime.now(timezone.utc) - timedelta(days=14)).isoformat()
    log = tmp_path / "llm-calls.jsonl"
    log.write_text(
        "\n".join([
            json.dumps({"ts": now_iso, "tool": "get_past_context", "agent": "reflector"}),
            json.dumps({"ts": now_iso, "tool": "get_past_context", "agent": "bull"}),
            json.dumps({"ts": old_iso, "tool": "get_past_context", "agent": "bear"}),
            json.dumps({"ts": now_iso, "tool": "some_other_tool"}),
            "{not valid json",  # tolerated, skipped
        ]) + "\n",
        encoding="utf-8",
    )
    _point_paths_at(monkeypatch, decisions=None, llm_calls=log)

    env = await ops_routes.weekly_training()
    # 2 this-week entries with get_past_context, 1 old excluded, garbage skipped.
    assert env["data"]["lessons_injected"] == 2


# ---------------------------------------------------------------------------
# 5 — eligibility mapping (rolled-back, shadow)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_regressed_track_maps_to_red(monkeypatch):
    """A track whose last run rolled back must surface eligibility='regressed'
    so the card paints it red."""
    body = {"tracks": [{
        "track_id": "trading-arbiter",
        "champion_adapter_path": "data/adapters/run-foo/gen-2",
        "champion_run_id": "run-foo__gen2",
        "last_run_status": "regressed_rollback",
        "champion_scores": {"decision_consistency": 0.40},
        "champion_promoted_at": "2026-05-01T07:00:00+00:00",
    }]}
    _stub_modelforge_response(monkeypatch, body=body)
    _point_paths_at(monkeypatch, decisions=None, llm_calls=None)

    env = await ops_routes.weekly_training()
    arbiter = [t for t in env["data"]["tracks"] if t["track_id"] == "trading-arbiter"][0]
    assert arbiter["eligibility"] == "regressed"


@pytest.mark.asyncio
async def test_shadow_track_maps_to_yellow(monkeypatch):
    """last_run_status containing 'shadow' → eligibility='shadow'."""
    body = {"tracks": [{
        "track_id": "trading-bear",
        "champion_adapter_path": "data/adapters/run-bar/gen-1",
        "last_run_status": "shadow_promoted",
        "champion_scores": {"judge_preference_pct": 0.51},
    }]}
    _stub_modelforge_response(monkeypatch, body=body)
    _point_paths_at(monkeypatch, decisions=None, llm_calls=None)

    env = await ops_routes.weekly_training()
    bear = [t for t in env["data"]["tracks"] if t["track_id"] == "trading-bear"][0]
    assert bear["eligibility"] == "shadow"


# ---------------------------------------------------------------------------
# 6 — model-forge response shape tolerance
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bare_list_response_accepted(monkeypatch):
    """ModelForge versions that return a bare list (no `tracks` envelope)
    must still be parsed correctly."""
    body = [
        {
            "track_id": "trading-reflector",
            "champion_adapter_path": "data/adapters/run-z/gen-1",
            "champion_scores": {"predictive_hit_rate_30d": 0.55},
        },
    ]
    _stub_modelforge_response(monkeypatch, body=body)
    _point_paths_at(monkeypatch, decisions=None, llm_calls=None)

    env = await ops_routes.weekly_training()
    refl = [t for t in env["data"]["tracks"] if t["track_id"] == "trading-reflector"][0]
    assert refl["eligibility"] == "promoted"
    assert refl["headline_score"] == pytest.approx(0.55)


# ---------------------------------------------------------------------------
# 7 — auth surface — endpoint is read-only, no Authorization required
# ---------------------------------------------------------------------------


def test_endpoint_has_no_auth_dependency():
    """GET /api/ops/weekly_training must NOT have an auth dep —
    operator's dashboard polls every 10s without sending an auth header.
    Mirrors the read-only contract used by backtest_gates + shark_override_health.
    """
    # Find the route in the router's routes table.
    weekly_routes = [
        r for r in ops_routes.router.routes
        if getattr(r, "path", "") == "/api/ops/weekly_training"
    ]
    assert len(weekly_routes) == 1, "expected exactly one route registered"
    route = weekly_routes[0]
    # No dependencies = no auth. (The mutating endpoints register a
    # Depends(require_mcp_key) here.)
    deps = getattr(route, "dependant", None)
    if deps is not None:
        # FastAPI Dependant exposes .dependencies — must be empty for read-only.
        assert not deps.dependencies, (
            "weekly_training must be read-only — no Depends() dependencies allowed"
        )


# ---------------------------------------------------------------------------
# 8 — next_training_ts is a future UTC instant
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_next_training_ts_is_future(monkeypatch):
    """Countdown target must be strictly in the future and parseable."""
    _stub_modelforge_response(monkeypatch, body={"tracks": []})
    _point_paths_at(monkeypatch, decisions=None, llm_calls=None)

    env = await ops_routes.weekly_training()
    nt = env["data"]["next_training_ts"]
    assert isinstance(nt, str)
    dt = datetime.fromisoformat(nt.replace("Z", "+00:00"))
    assert dt > datetime.now(timezone.utc)
    # Must be within the next 7 days (Sunday 02:00 ET roll-over rule).
    assert dt < datetime.now(timezone.utc) + timedelta(days=8)
