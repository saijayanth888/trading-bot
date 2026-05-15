"""
Tests for the shark BEAR_VOLATILE paper-mode override verifier.

The verifier is a deterministic bash + python script at
.hermes/scripts/shark_override_verify.sh that:
  1. Reads the latest shark_market_open cron output
  2. Parses regime, candidates, override-applied, trades-placed
  3. Writes stocks/memory/override_verify.json
  4. Tracks stalled_runs across invocations via a state file

The dashboard endpoint /api/ops/shark_override_health reads that JSON.

These tests:
  - Build synthetic cron output files
  - Run the verifier script with env vars overriding all paths
  - Assert the output JSON matches expectations
  - Verify stalled_runs accumulates across consecutive runs
  - Test the dashboard endpoint reads the JSON correctly
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / ".hermes" / "scripts" / "shark_override_verify.sh"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _write_cron_output(cron_dir: Path, name: str, body: str) -> Path:
    """Write a synthetic cron output file mimicking the Hermes format."""
    cron_dir.mkdir(parents=True, exist_ok=True)
    f = cron_dir / name
    f.write_text(body)
    return f


def _run_verifier(tmp_path: Path, suppress_slack: bool = True) -> dict:
    """
    Invoke the verifier script with all paths redirected to tmp_path.
    Returns the parsed JSON payload from override_verify.json.
    """
    cron_dir = tmp_path / "cron_output"
    state_file = tmp_path / "state.json"
    out_file = tmp_path / "override_verify.json"
    log_dir = tmp_path / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)

    env = dict(os.environ)
    env["SHARK_OVERRIDE_VERIFY_CRON_OUT_DIR"] = str(cron_dir)
    env["SHARK_OVERRIDE_VERIFY_STATE_FILE"] = str(state_file)
    env["SHARK_OVERRIDE_VERIFY_OUT_FILE"] = str(out_file)
    env["SHARK_OVERRIDE_VERIFY_SUPPRESS_SLACK"] = "1" if suppress_slack else "0"
    # Don't pollute the real ~/.hermes log
    env.pop("SLACK_WEBHOOK_URL", None)

    res = subprocess.run(
        ["bash", str(SCRIPT)],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert res.returncode == 0, (
        f"verifier failed rc={res.returncode}\n"
        f"stdout: {res.stdout}\nstderr: {res.stderr}"
    )
    if not out_file.is_file():
        raise AssertionError(
            f"verifier did not write {out_file}\n"
            f"stdout: {res.stdout}\nstderr: {res.stderr}"
        )
    return json.loads(out_file.read_text())


# ---------------------------------------------------------------------------
# Sanity: the script exists and is executable
# ---------------------------------------------------------------------------

def test_script_exists_and_is_executable():
    assert SCRIPT.is_file(), f"verifier script missing at {SCRIPT}"
    assert os.access(SCRIPT, os.X_OK), f"verifier script not executable: {SCRIPT}"


# ---------------------------------------------------------------------------
# Test 1: override-fired line → status=healthy
# ---------------------------------------------------------------------------

OVERRIDE_FIRED_OUTPUT = """\
# Cron Job: shark_market_open
**Job ID:** da38c6eb6673
**Run Time:** 2026-05-12 09:35:42

shark market-open:
2026-05-12 09:35:41,424 INFO shark.data.market_regime: PAPER MODE: overriding BEAR_VOLATILE rules — 1 trade/day at 0.5x size (confidence ≥ 0.85)
2026-05-12 09:35:41,424 INFO shark.data.market_regime: Market regime: BEAR_VOLATILE | trend_score=-3 atr_pct=1.52% atr_pctl=74%
2026-05-12 09:35:41,424 INFO shark.phases.market_open: Market regime: BEAR_VOLATILE — PAPER MODE
2026-05-12 09:36:01,000 INFO shark.phases.market_open: NVDA EXECUTE qty=4 confidence=0.88 rr=2.50 stop=$120.00 target=$135.00
2026-05-12 09:36:02,000 INFO shark.execution.alpaca_client: Bracket order placed for NVDA: order_id=abc123
── exit=0 ──
"""


def test_override_fired_status_healthy(tmp_path):
    cron_dir = tmp_path / "cron_output"
    _write_cron_output(cron_dir, "2026-05-12_09-35-42.md", OVERRIDE_FIRED_OUTPUT)

    payload = _run_verifier(tmp_path)

    assert payload["regime"] == "BEAR_VOLATILE"
    assert payload["override_expected"] is True
    assert payload["override_applied"] is True
    assert payload["candidates_evaluated"] == 1
    assert payload["candidates_passing_override"] == 1
    assert payload["trades_placed"] == 1
    assert payload["status"] == "healthy"
    assert payload["stalled_runs"] == 0
    assert payload["last_trade_at"] is not None


# ---------------------------------------------------------------------------
# Test 2: BEAR_VOLATILE with no candidates → status=healthy (nothing to evaluate)
# ---------------------------------------------------------------------------

NO_CANDIDATES_OUTPUT = """\
# Cron Job: shark_market_open
shark market-open:
2026-05-12 09:35:41,424 INFO shark.data.market_regime: PAPER MODE: overriding BEAR_VOLATILE rules — 1 trade/day at 0.5x size (confidence ≥ 0.85)
2026-05-12 09:35:41,424 INFO shark.data.market_regime: Market regime: BEAR_VOLATILE | trend_score=-3
2026-05-12 09:35:41,500 INFO shark.phases.market_open: No candidates for 2026-05-12
── exit=0 ──
"""


def test_no_candidates_status_healthy(tmp_path):
    cron_dir = tmp_path / "cron_output"
    _write_cron_output(cron_dir, "2026-05-12_09-35-42.md", NO_CANDIDATES_OUTPUT)

    payload = _run_verifier(tmp_path)

    assert payload["regime"] == "BEAR_VOLATILE"
    assert payload["override_expected"] is True
    assert payload["candidates_evaluated"] == 0
    assert payload["trades_placed"] == 0
    # Nothing to evaluate is not a failure — verifier should report healthy.
    assert payload["status"] == "healthy"
    assert payload["stalled_runs"] == 0


# ---------------------------------------------------------------------------
# Test 3: BEAR_VOLATILE + override expected + candidates evaluated + no trade
#         → stalled_runs increments across consecutive runs
# ---------------------------------------------------------------------------

CANDIDATES_BUT_NO_TRADE_OUTPUT = """\
# Cron Job: shark_market_open
shark market-open:
2026-05-12 09:35:41,424 INFO shark.data.market_regime: PAPER MODE: overriding BEAR_VOLATILE rules — 1 trade/day at 0.5x size (confidence ≥ 0.85)
2026-05-12 09:35:41,424 INFO shark.data.market_regime: Market regime: BEAR_VOLATILE | trend_score=-3
2026-05-12 09:36:00,000 INFO shark.phases.market_open: NVDA rejected — confidence 0.72 < 0.85 floor
2026-05-12 09:36:01,000 INFO shark.phases.market_open: AMD rejected — confidence 0.80 < 0.85 floor
2026-05-12 09:36:02,000 INFO shark.phases.market_open: TSLA rejected — derived R:R 1.20 < 1.50 tolerance (LLM claimed 2.50; entry=200.00 stop=190.00 target=212.00)
── exit=0 ──
"""


def test_candidates_no_trade_increments_stalled_runs(tmp_path):
    cron_dir = tmp_path / "cron_output"
    f = _write_cron_output(cron_dir, "2026-05-12_09-35-42.md", CANDIDATES_BUT_NO_TRADE_OUTPUT)

    # Run 1 — first stall
    p1 = _run_verifier(tmp_path)
    assert p1["regime"] == "BEAR_VOLATILE"
    assert p1["override_expected"] is True
    assert p1["candidates_evaluated"] == 3
    assert p1["candidates_passing_override"] == 0
    assert p1["trades_placed"] == 0
    assert p1["stalled_runs"] == 1
    assert p1["status"] == "degraded"

    # Run 2 — same output, second stall
    f.write_text(CANDIDATES_BUT_NO_TRADE_OUTPUT)
    p2 = _run_verifier(tmp_path)
    assert p2["stalled_runs"] == 2
    assert p2["status"] == "degraded"

    # Run 3 — third stall flips to "stalled"
    f.write_text(CANDIDATES_BUT_NO_TRADE_OUTPUT)
    p3 = _run_verifier(tmp_path)
    assert p3["stalled_runs"] == 3
    assert p3["status"] == "stalled"


# ---------------------------------------------------------------------------
# Test 4: a successful trade resets stalled_runs to 0
# ---------------------------------------------------------------------------

def test_trade_resets_stalled_runs(tmp_path):
    cron_dir = tmp_path / "cron_output"
    f = _write_cron_output(cron_dir, "2026-05-12_09-35-42.md", CANDIDATES_BUT_NO_TRADE_OUTPUT)

    # Stall twice
    _run_verifier(tmp_path)
    _run_verifier(tmp_path)

    # Then a successful run
    f.write_text(OVERRIDE_FIRED_OUTPUT)
    p = _run_verifier(tmp_path)
    assert p["stalled_runs"] == 0
    assert p["status"] == "healthy"
    assert p["trades_placed"] == 1


# ---------------------------------------------------------------------------
# Test 5: BULL regime → override_expected=False, status=healthy
# ---------------------------------------------------------------------------

BULL_OUTPUT = """\
# Cron Job: shark_market_open
shark market-open:
2026-05-12 09:35:41,424 INFO shark.data.market_regime: Market regime: BULL_QUIET | trend_score=2 atr_pct=0.85%
2026-05-12 09:36:00,000 INFO shark.phases.market_open: NVDA — Claude decided NO_TRADE
── exit=0 ──
"""


def test_bull_regime_override_not_expected(tmp_path):
    cron_dir = tmp_path / "cron_output"
    _write_cron_output(cron_dir, "2026-05-12_09-35-42.md", BULL_OUTPUT)
    payload = _run_verifier(tmp_path)
    assert payload["regime"] == "BULL_QUIET"
    assert payload["override_expected"] is False
    assert payload["status"] == "healthy"
    assert payload["stalled_runs"] == 0


# ---------------------------------------------------------------------------
# Test 6: no cron output files at all → status=unknown
# ---------------------------------------------------------------------------

def test_no_cron_output_status_unknown(tmp_path):
    # Don't create any files — but cron_dir needs to exist as parent
    payload = _run_verifier(tmp_path)
    assert payload["status"] == "unknown"
    assert payload["regime"] is None
    assert payload["candidates_evaluated"] == 0


# ---------------------------------------------------------------------------
# Test 7: dashboard endpoint reads the JSON correctly
# ---------------------------------------------------------------------------

def test_dashboard_endpoint_reads_verifier_json(tmp_path, monkeypatch):
    """
    /api/ops/shark_override_health should:
      - return envelope status="ok" when verifier reports healthy
      - return envelope status="degraded" when stalled_runs >= 1
      - return envelope status="down" when stalled_runs >= 3
      - include the verifier payload under data
    """
    # Set up an isolated override_verify.json via the verifier
    cron_dir = tmp_path / "cron_output"
    _write_cron_output(cron_dir, "2026-05-12_09-35-42.md", OVERRIDE_FIRED_OUTPUT)
    payload = _run_verifier(tmp_path)
    out_file = tmp_path / "override_verify.json"
    assert out_file.is_file()

    # Patch the candidate path list inside ops_routes to point to our tmp file.
    # Lazy import keeps test discovery cheap if FastAPI deps aren't installed.
    fastapi = pytest.importorskip("fastapi")
    sys.path.insert(0, str(REPO_ROOT))
    from user_data.dashboard import ops_routes  # type: ignore

    monkeypatch.setattr(ops_routes, "_OVERRIDE_VERIFY_PATHS", [out_file])

    import asyncio
    env = asyncio.run(ops_routes.shark_override_health())

    assert env["status"] == "ok"
    assert env["error"] is None
    assert env["data"]["regime"] == "BEAR_VOLATILE"
    assert env["data"]["trades_placed"] == 1
    assert env["data"]["status"] == "healthy"
    assert "age_s" in env["data"]


def test_dashboard_endpoint_reports_stalled(tmp_path, monkeypatch):
    """When verifier status='stalled' or stalled_runs >= 3, envelope status='down'."""
    out_file = tmp_path / "override_verify.json"
    out_file.write_text(json.dumps({
        "date": "2026-05-12",
        "regime": "BEAR_VOLATILE",
        "override_expected": True,
        "override_applied": True,
        "candidates_evaluated": 3,
        "candidates_passing_override": 0,
        "trades_placed": 0,
        "status": "stalled",
        "stalled_runs": 4,
        "last_trade_at": None,
        "reason": "4 consecutive stalled runs",
        "checked_at": "2026-05-12T01:00:00+00:00",
        "source_file": "/tmp/x.md",
    }))

    pytest.importorskip("fastapi")
    sys.path.insert(0, str(REPO_ROOT))
    from user_data.dashboard import ops_routes  # type: ignore

    monkeypatch.setattr(ops_routes, "_OVERRIDE_VERIFY_PATHS", [out_file])

    import asyncio
    env = asyncio.run(ops_routes.shark_override_health())

    assert env["status"] == "down"
    assert env["error"] is not None
    assert "stalled" in env["error"].lower()
    assert env["data"]["stalled_runs"] == 4


def test_dashboard_endpoint_reports_degraded(tmp_path, monkeypatch):
    """stalled_runs == 1 → envelope status='degraded'."""
    out_file = tmp_path / "override_verify.json"
    out_file.write_text(json.dumps({
        "date": "2026-05-12",
        "regime": "BEAR_VOLATILE",
        "override_expected": True,
        "override_applied": True,
        "candidates_evaluated": 2,
        "candidates_passing_override": 0,
        "trades_placed": 0,
        "status": "degraded",
        "stalled_runs": 1,
        "last_trade_at": None,
        "reason": "1 stalled run",
        "checked_at": "2026-05-12T01:00:00+00:00",
        "source_file": "/tmp/x.md",
    }))

    pytest.importorskip("fastapi")
    sys.path.insert(0, str(REPO_ROOT))
    from user_data.dashboard import ops_routes  # type: ignore

    monkeypatch.setattr(ops_routes, "_OVERRIDE_VERIFY_PATHS", [out_file])

    import asyncio
    env = asyncio.run(ops_routes.shark_override_health())

    assert env["status"] == "degraded"
    assert env["data"]["status"] == "degraded"
    assert env["data"]["stalled_runs"] == 1


def test_dashboard_endpoint_missing_file(tmp_path, monkeypatch):
    """Verifier JSON missing → envelope status='down' with helpful error."""
    pytest.importorskip("fastapi")
    sys.path.insert(0, str(REPO_ROOT))
    from user_data.dashboard import ops_routes  # type: ignore

    monkeypatch.setattr(ops_routes, "_OVERRIDE_VERIFY_PATHS", [tmp_path / "missing.json"])

    import asyncio
    env = asyncio.run(ops_routes.shark_override_health())

    assert env["status"] == "down"
    assert env["data"] is None
    assert "verifier" in env["error"].lower() or "not found" in env["error"].lower()
