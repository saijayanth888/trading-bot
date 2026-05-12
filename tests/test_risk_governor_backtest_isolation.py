"""
Regression test for Bug 2 (2026-05-12).

Symptom: running ``freqtrade backtesting`` while the live bot was paused
for drawdown read the live anchor file
    user_data/state/risk_governor_anchors.json
and started the backtest with ``paused_for_drawdown: True``. Every entry
was then blocked by the max_drawdown_paused gate, making the backtest a
silent no-op while the operator believed they were measuring strategy
performance.

Root cause: ``_anchor_path()`` returned the same on-disk path regardless
of runmode. The persistence logic was added for live restart safety
(P0-G) but never excluded the simulator runmodes.

Fix: ``_resolve_anchor_path(runmode)`` routes backtest / hyperopt / edge
to a per-PID transient file under ``/tmp``; live / dry / None keep the
existing path. ``RISK_GOVERNOR_ANCHORS_PATH`` still overrides BOTH
(needed by the test fixture in conftest.py).

Test plan:
  1. RiskGovernor constructed with ``runmode="backtest"`` does not read
     or write the live anchor file even when one exists with
     ``paused_for_drawdown: True``.
  2. The transient file path lives under ``/tmp`` and carries the PID.
  3. RiskGovernor constructed without a runmode (live / dry) uses the
     env-overridden path (auto-isolated by conftest fixture).
  4. RiskGovernor.from_config() correctly extracts the freqtrade RunMode
     enum's .value and propagates to the path resolver.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "user_data"))

from modules.risk_governor import (  # noqa: E402
    _BACKTEST_RUNMODES,
    _resolve_anchor_path,
    RiskConfig,
    RiskGovernor,
)


def test_backtest_runmode_uses_transient_anchor_path(monkeypatch) -> None:
    """Path resolver returns /tmp/risk_governor_backtest_<pid>.json."""
    # Clear the env override the conftest fixture set so we observe the
    # mode-based path, not the test-isolation path.
    monkeypatch.delenv("RISK_GOVERNOR_ANCHORS_PATH", raising=False)
    for mode in ("backtest", "hyperopt", "edge"):
        p = _resolve_anchor_path(mode)
        assert p.parent == Path(tempfile.gettempdir()), (mode, p)
        assert str(os.getpid()) in p.name, (mode, p)
        assert p.name.endswith(".json")


def test_live_runmode_uses_default_persistent_path(monkeypatch) -> None:
    """Live / dry / None resolve to the persistent state file."""
    monkeypatch.delenv("RISK_GOVERNOR_ANCHORS_PATH", raising=False)
    for mode in (None, "live", "dry_run"):
        p = _resolve_anchor_path(mode)
        assert p.parent != Path(tempfile.gettempdir()), (mode, p)
        assert p.name == "risk_governor_anchors.json", (mode, p)


def test_env_override_wins_in_every_mode(tmp_path: Path, monkeypatch) -> None:
    """RISK_GOVERNOR_ANCHORS_PATH must short-circuit every runmode."""
    override = tmp_path / "custom_anchor.json"
    monkeypatch.setenv("RISK_GOVERNOR_ANCHORS_PATH", str(override))
    for mode in (None, "live", "dry_run", "backtest", "hyperopt", "edge"):
        assert _resolve_anchor_path(mode) == override, mode


def test_backtest_governor_does_not_read_live_anchor(
    tmp_path: Path, monkeypatch
) -> None:
    """The smoking-gun test: a poisoned 'live' anchor must NOT bleed into
    a backtest-runmode governor."""
    # The conftest fixture has already isolated the env var to a per-test
    # path. Plant a poison anchor there with paused_for_drawdown=True so
    # any code path that honours the env var would import the pause flag.
    poisoned = Path(os.environ["RISK_GOVERNOR_ANCHORS_PATH"])
    poisoned.parent.mkdir(parents=True, exist_ok=True)
    poisoned.write_text(json.dumps({
        "day_anchor_utc": "2026-05-12T00:00:00+00:00",
        "starting_equity_today": 10_000.0,
        "daily_realized_pnl": 0.0,
        "peak_equity": 10_000.0,
        "paused_for_drawdown": True,
        "updated_at": "2026-05-12T18:00:00+00:00",
    }))

    # NOW clear the env var so the runmode-based resolver kicks in. With
    # the fix in place, the backtest governor must compute a /tmp path
    # rather than read the poison file we just wrote.
    monkeypatch.delenv("RISK_GOVERNOR_ANCHORS_PATH", raising=False)

    gov = RiskGovernor(RiskConfig(), runmode="backtest")
    assert gov._paused_for_drawdown is False, (
        "backtest governor inherited paused_for_drawdown from the live "
        "anchor — bug 2 has regressed."
    )
    # And no anchor file should exist at the transient path until we
    # explicitly persist.
    transient = _resolve_anchor_path("backtest")
    if transient.exists():
        transient.unlink()


def test_backtest_governor_persists_to_transient_path(monkeypatch) -> None:
    """update_equity() must write to /tmp, NOT the live state file."""
    monkeypatch.delenv("RISK_GOVERNOR_ANCHORS_PATH", raising=False)
    gov = RiskGovernor(
        RiskConfig(),
        runmode="backtest",
        now_fn=lambda: datetime(2026, 5, 12, 12, 0, tzinfo=timezone.utc),
    )
    gov.update_equity(10_000.0)
    transient = _resolve_anchor_path("backtest")
    assert transient.exists(), "backtest anchor should be written to /tmp"
    assert transient.parent == Path(tempfile.gettempdir())
    # Clean up — also exercised by the atexit hook, but explicit here.
    transient.unlink()


def test_from_config_extracts_runmode_enum() -> None:
    """A fake freqtrade-style config with an Enum runmode must propagate."""
    class FakeRunMode:
        # Mimics freqtrade.enums.RunMode shape.
        value = "backtest"

    cfg = {"risk_management": {}, "runmode": FakeRunMode()}
    gov = RiskGovernor.from_config(cfg)
    assert gov._runmode == "backtest"


def test_from_config_handles_string_runmode() -> None:
    """Plain-string runmode (test configs) also works."""
    cfg = {"risk_management": {}, "runmode": "BACKTEST"}
    gov = RiskGovernor.from_config(cfg)
    assert gov._runmode == "backtest"


def test_from_config_no_runmode_defaults_to_live() -> None:
    """Missing runmode = treat as live (use default anchor)."""
    gov = RiskGovernor.from_config({"risk_management": {}})
    assert gov._runmode is None


def test_backtest_runmodes_constant_includes_all_three() -> None:
    """Guard against accidental drift in the runmode-set definition."""
    assert _BACKTEST_RUNMODES == frozenset({"backtest", "hyperopt", "edge"})


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
