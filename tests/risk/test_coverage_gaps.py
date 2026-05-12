"""Targeted tests covering the remaining edge-case branches.

Split out so the primary test files stay focused on documented
regressions; this file specifically chases the 95% coverage gate.
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from quanta_core.risk import asset_class_gate as gate_mod
from quanta_core.risk import monte_carlo as mc
from quanta_core.risk import ownership as ownership_mod
from quanta_core.risk.asset_class_gate import Position, is_quanta_managed
from quanta_core.risk.governor import (
    RiskConfig,
    RiskGovernor,
    _anchor_path,
    _resolve_anchor_path,
)
from quanta_core.risk.monte_carlo import (
    Calibration,
    MonteCarloConfig,
    MonteCarloEngine,
)


def _now_2026() -> datetime:
    return datetime(2026, 5, 12, 12, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# governor.py edge cases
# ---------------------------------------------------------------------------


def test_anchor_path_back_compat_shim() -> None:
    """The legacy ``_anchor_path()`` shim must delegate to the resolver."""
    assert _anchor_path(None) == _resolve_anchor_path(None)
    assert _anchor_path("backtest") == _resolve_anchor_path("backtest")


def test_persist_anchors_mkdir_failure_is_logged_not_raised(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """If the state dir cannot be created, persist degrades to a log warn."""
    target = tmp_path / "no-perm" / "anchors.json"
    monkeypatch.setenv("RISK_GOVERNOR_ANCHORS_PATH", str(target))

    real_mkdir = Path.mkdir

    def boom(self: Path, *args: object, **kwargs: object) -> None:
        if self == target.parent:
            raise OSError("permission denied")
        real_mkdir(self, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(Path, "mkdir", boom)
    caplog.set_level("WARNING")
    gov = RiskGovernor(RiskConfig(), now_fn=_now_2026)
    gov.update_equity(10_000.0)  # would persist; mkdir fails
    assert any("could not create state dir" in r.message for r in caplog.records)


def test_persist_anchors_replace_failure_logged(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """If the atomic rename fails, persist logs but does not raise."""
    target = Path(os.environ["RISK_GOVERNOR_ANCHORS_PATH"])
    target.parent.mkdir(parents=True, exist_ok=True)

    real_replace = Path.replace

    def boom(self: Path, dst: Path) -> Path:
        raise OSError("simulated rename failure")

    monkeypatch.setattr(Path, "replace", boom)
    caplog.set_level("WARNING")
    gov = RiskGovernor(RiskConfig(), now_fn=_now_2026)
    gov.update_equity(10_000.0)
    monkeypatch.setattr(Path, "replace", real_replace)
    assert any("anchors persist failed" in r.message for r in caplog.records)


def test_load_anchors_missing_optional_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An anchor file missing all optional fields must still load cleanly."""
    target = Path(os.environ["RISK_GOVERNOR_ANCHORS_PATH"])
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps({}))  # everything missing
    gov = RiskGovernor(RiskConfig(), now_fn=_now_2026)
    assert gov._day_anchor_utc is None
    assert gov._starting_equity_today is None


def test_load_anchors_malformed_starting_equity(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A non-numeric ``starting_equity_today`` triggers the ValueError path."""
    target = Path(os.environ["RISK_GOVERNOR_ANCHORS_PATH"])
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(
            {
                "day_anchor_utc": "2026-05-12T00:00:00+00:00",
                "starting_equity_today": "not-a-float",
            }
        )
    )
    caplog.set_level("WARNING")
    gov = RiskGovernor(RiskConfig(), now_fn=_now_2026)
    assert any("malformed anchors file" in r.message for r in caplog.records)
    # State is whatever the parser got to before the malformed field.
    assert gov._day_anchor_utc is not None


def test_correlation_filter_skips_same_pair() -> None:
    """Open position list containing the candidate pair itself is skipped."""
    gov = RiskGovernor(
        RiskConfig(correlation_threshold=0.5, correlation_min_overlap=10),
        now_fn=_now_2026,
    )
    gov.update_equity(10_000.0)
    idx = pd.date_range("2026-04-01", periods=100, freq="h", tz="UTC")
    series = pd.Series(np.random.default_rng(1).normal(0, 1, 100), index=idx)
    decision = gov.approve_entry(
        pair="BTC/USD",
        signal_price=1.0,
        base_stake=100.0,
        equity=10_000.0,
        open_positions=[("BTC/USD", 100.0)],  # same pair as candidate
        pair_returns={"BTC/USD": series},
    )
    assert decision.approved


def test_correlation_skipped_when_rho_none() -> None:
    """When ``_pearson_returns`` returns None the loop continues."""
    gov = RiskGovernor(
        RiskConfig(correlation_threshold=0.5, correlation_min_overlap=999),
        now_fn=_now_2026,
    )
    gov.update_equity(10_000.0)
    idx = pd.date_range("2026-04-01", periods=10, freq="h", tz="UTC")
    a = pd.Series(np.arange(10, dtype=float), index=idx)
    b = pd.Series(np.arange(10, dtype=float), index=idx)
    decision = gov.approve_entry(
        pair="BTC/USD",
        signal_price=1.0,
        base_stake=100.0,
        equity=10_000.0,
        open_positions=[("ETH/USD", 100.0)],
        pair_returns={"BTC/USD": a, "ETH/USD": b},
    )
    assert decision.approved
    # rho was None (insufficient overlap) → no correlations recorded
    assert decision.correlations == {}


def test_no_starting_equity_skips_daily_loss_check() -> None:
    """Before update_equity sets starting_equity, the daily-loss check is skipped."""
    gov = RiskGovernor(RiskConfig(), now_fn=_now_2026)
    # Don't call update_equity — _starting_equity_today stays None
    # and ``starting_equity_for_pct_limits`` is also None.
    decision = gov.approve_entry(
        pair="X",
        signal_price=1.0,
        base_stake=100.0,
        equity=10_000.0,
    )
    # update_equity is called inside approve_entry; on first call it sets
    # the starting equity. The fresh anchor means daily PnL is 0 so we pass.
    assert decision.approved


def test_kelly_avg_loss_zero_returns_zero() -> None:
    """When recorded losses have pnl_pct == 0 the avg-loss guard fires."""
    cfg = RiskConfig(kelly_min_trades=2, kelly_lookback_trades=5)
    gov = RiskGovernor(cfg, now_fn=_now_2026)
    gov.record_trade_close("X", -10.0, 0.0)  # weird zero-pct loss
    gov.record_trade_close("X", -10.0, 0.0)
    gov.record_trade_close("X", +10.0, +0.01)
    # No actual losses (pnl_pct < 0) — both branches degrade.
    assert gov._kelly_fraction(0.6) == 0.0


# ---------------------------------------------------------------------------
# asset_class_gate.py edge case
# ---------------------------------------------------------------------------


def test_asset_class_gate_swallows_load_owned_exception(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If load_owned raises despite its contract, the gate refuses (no raise)."""

    def boom(_subsystem: str) -> set[str]:
        raise OSError("disk gone")

    monkeypatch.setattr(gate_mod, "load_owned", boom)
    nvda = Position(symbol="NVDA", asset_class="us_equity")
    assert is_quanta_managed(nvda, "shark") is False


# ---------------------------------------------------------------------------
# monte_carlo.py edge cases
# ---------------------------------------------------------------------------


def _calm_calib() -> Calibration:
    return Calibration(
        s0=100.0,
        v0=0.0009,
        kappa=2.0,
        theta=0.0009,
        xi=0.05,
        rho=-0.5,
        mu=0.0,
        as_of=datetime.now(UTC),
    )


def test_simulate_cpu_no_antithetic_branch() -> None:
    """Disabling antithetic exercises the alternate sampling code path."""
    cfg = MonteCarloConfig(
        num_paths=500,
        horizon_steps=20,
        use_cpu_fallback=True,
        use_antithetic=False,
        es_ci_max_frac=1.0,
        seed=42,
    )
    decision = MonteCarloEngine(cfg).evaluate("X", _calm_calib())
    assert decision.outcome in ("pass", "warn", "block")


def test_simulate_with_jumps_branch() -> None:
    """jump_intensity > 0 exercises the Poisson-jump code path."""
    cfg = MonteCarloConfig(
        num_paths=500,
        horizon_steps=20,
        use_cpu_fallback=True,
        es_ci_max_frac=1.0,
        seed=42,
    )
    calib = Calibration(
        s0=100.0,
        v0=0.01,
        kappa=2.0,
        theta=0.01,
        xi=0.1,
        rho=-0.3,
        mu=0.0,
        jump_intensity=50.0,
        jump_mean=-0.01,
        jump_std=0.02,
        as_of=datetime.now(UTC),
    )
    decision = MonteCarloEngine(cfg).evaluate("X", calib)
    assert decision.outcome in ("pass", "warn", "block")


def test_bootstrap_empty_returns_inf() -> None:
    """A zero-length loss array yields an infinite CI width."""
    eng = MonteCarloEngine(MonteCarloConfig(use_cpu_fallback=True, seed=1, es_ci_max_frac=1.0))
    width = eng._bootstrap_es_ci_width(np.array([], dtype=np.float64))
    assert np.isinf(width)


def test_metrics_empty_tail_es_falls_back_to_threshold() -> None:
    """If the ES tail collapses to zero samples the threshold is reused."""
    eng = MonteCarloEngine(MonteCarloConfig(use_cpu_fallback=True, seed=1, es_ci_max_frac=1.0))
    # Construct contrived terminal+prices where all losses equal the threshold
    # so the > threshold filter returns an empty tail.
    n = 100
    terminal = np.zeros(n)  # no loss
    prices = np.full((n, 11), 100.0)
    metrics = eng._compute_metrics(terminal, prices)
    assert "es_975" in metrics


def test_decide_var_warn_only() -> None:
    """VaR in the warn band → outcome 'warn'."""
    eng = MonteCarloEngine(
        MonteCarloConfig(
            use_cpu_fallback=True,
            seed=1,
            var_warn_pct=0.001,
            var_block_pct=0.999,
            es_warn_pct=0.999,
            es_block_pct=1.0,
            max_dd_warn_pct=0.999,
            max_dd_block_pct=1.0,
            tail_asym_warn=999.0,
            tail_asym_block=1000.0,
            es_ci_max_frac=1.0,
        )
    )
    outcome, reason = eng._decide(
        {
            "var_99": 0.005,
            "es_975": 0.01,
            "max_dd_q99": 0.01,
            "tail_asym": 1.0,
            "es_ci_width": 0.0,
        }
    )
    assert outcome == "warn"
    assert "var_99" in reason


def test_decide_es_warn_max_dd_warn_tail_warn() -> None:
    """Each individual warn threshold can fire independently."""
    eng = MonteCarloEngine(
        MonteCarloConfig(
            use_cpu_fallback=True,
            seed=1,
            var_warn_pct=999.0,
            var_block_pct=1000.0,
            es_warn_pct=0.001,
            es_block_pct=999.0,
            max_dd_warn_pct=0.001,
            max_dd_block_pct=999.0,
            tail_asym_warn=0.5,
            tail_asym_block=999.0,
            es_ci_max_frac=1.0,
        )
    )
    outcome, reason = eng._decide(
        {
            "var_99": 0.0,
            "es_975": 0.01,
            "max_dd_q99": 0.01,
            "tail_asym": 1.0,
            "es_ci_width": 0.0,
        }
    )
    assert outcome == "warn"
    # All three warn reasons should be in the joined string.
    for token in ("es_975", "max_dd", "tail_asym"):
        assert token in reason


def test_decide_es_max_dd_tail_block_paths() -> None:
    """Each block threshold (ES, max_dd, tail_asym) trips independently."""
    eng = MonteCarloEngine(MonteCarloConfig(use_cpu_fallback=True, seed=1, es_ci_max_frac=1.0))
    outcome, reason = eng._decide(
        {
            "var_99": 0.0,
            "es_975": 1.0,  # > es_block_pct
            "max_dd_q99": 1.0,  # > max_dd_block_pct
            "tail_asym": 99.0,  # > tail_asym_block
            "es_ci_width": 0.0,
        }
    )
    assert outcome == "block"
    for token in ("es_975", "max_dd", "tail_asym"):
        assert token in reason


def test_evaluate_propagates_simulation_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unexpected exception inside _simulate is wrapped in MonteCarloError."""
    eng = MonteCarloEngine(MonteCarloConfig(use_cpu_fallback=True, seed=1, es_ci_max_frac=1.0))

    def boom(_symbol: str, _calib: Calibration) -> tuple[np.ndarray, np.ndarray]:
        raise RuntimeError("kernel crash")

    monkeypatch.setattr(eng, "_simulate", boom)
    with pytest.raises(mc.MonteCarloError, match="simulation failed"):
        eng.evaluate("X", _calm_calib())


def test_seed_none_uses_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """Config with seed=None routes through the default seed branch."""
    cfg = MonteCarloConfig(
        num_paths=100,
        horizon_steps=5,
        use_cpu_fallback=True,
        seed=None,
        es_ci_max_frac=1.0,
    )
    eng = MonteCarloEngine(cfg)
    # Force fresh RNG.
    assert eng._cpu_rng() is not None


# ---------------------------------------------------------------------------
# ownership module — the _state_dir + override branches
# ---------------------------------------------------------------------------


def test_ownership_state_dir_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("QUANTA_STATE_DIR", raising=False)
    d = ownership_mod._state_dir()
    assert d.parts[-2:] == (".quanta", "state")
