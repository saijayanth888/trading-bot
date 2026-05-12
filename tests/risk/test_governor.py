"""Tests for ``quanta_core.risk.governor``.

Ports two regression test suites verbatim from the legacy bot:

* ``tests/test_risk_governor_dup_index.py`` — Bug 1 (2026-05-12).
  Duplicate timestamps in the per-pair returns Series must not raise.
* ``tests/test_risk_governor_backtest_isolation.py`` — Bug 2 (2026-05-12).
  Backtest / hyperopt / edge runmodes must NOT touch the live anchor.

Plus full coverage of the governor's hard gates: drawdown, daily loss,
concurrent positions, circuit breaker, correlation, Kelly sizing,
manual resume, status snapshot, and persistence round-trip.
"""

from __future__ import annotations

import json
import os
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from quanta_core.risk.governor import (
    _BACKTEST_RUNMODES,
    RiskConfig,
    RiskDecision,
    RiskGovernor,
    _resolve_anchor_path,
)


def _fixed_now(year: int = 2026, month: int = 5, day: int = 12) -> datetime:
    return datetime(year, month, day, 12, 0, tzinfo=UTC)


def _gov(cfg: RiskConfig | None = None, runmode: str | None = None) -> RiskGovernor:
    cfg = cfg or RiskConfig(correlation_threshold=0.70, correlation_min_overlap=20)
    return RiskGovernor(cfg, now_fn=_fixed_now, runmode=runmode)


# ---------------------------------------------------------------------------
# 1. Port of test_risk_governor_dup_index.py (Bug 1)
# ---------------------------------------------------------------------------


class TestDuplicateIndexCorrelation:
    def test_pearson_returns_handles_duplicate_index(self) -> None:
        """Two pair-returns Series with duplicate timestamps must NOT raise."""
        gov = _gov()
        base_idx = pd.date_range("2026-04-12", periods=100, freq="h", tz="UTC")
        dup_idx = pd.DatetimeIndex([base_idx[0]] * 3 + list(base_idx[1:]))
        rng = np.random.default_rng(7)
        a_vals = rng.normal(0, 1, 102)
        b_vals = a_vals * 0.9 + rng.normal(0, 0.1, 102)
        a = pd.Series(a_vals, index=dup_idx)
        b = pd.Series(b_vals, index=dup_idx)
        assert not a.index.is_unique
        assert not b.index.is_unique

        rho = gov._pearson_returns(a, b)
        assert rho is not None
        assert np.isfinite(rho)
        assert 0.5 < rho < 1.0

    def test_pearson_returns_handles_one_sided_duplicates(self) -> None:
        gov = _gov()
        a_idx = pd.date_range("2026-04-12", periods=100, freq="h", tz="UTC")
        b_idx = pd.DatetimeIndex([a_idx[0]] * 2 + list(a_idx[1:]))
        rng = np.random.default_rng(11)
        a = pd.Series(rng.normal(0, 1, 100), index=a_idx)
        b = pd.Series(rng.normal(0, 1, 101), index=b_idx)
        assert a.index.is_unique
        assert not b.index.is_unique

        rho = gov._pearson_returns(a, b)
        # Result may be None (insufficient overlap or 0-std) but must not raise.
        assert rho is None or np.isfinite(rho)

    def test_approve_entry_path_with_dup_index(self) -> None:
        gov = _gov()
        gov.update_equity(10_000)
        base_idx = pd.date_range("2026-04-12", periods=200, freq="h", tz="UTC")
        dup_idx = pd.DatetimeIndex([base_idx[0]] * 5 + list(base_idx[1:]))
        rng = np.random.default_rng(42)
        btc = pd.Series(rng.normal(0, 1, 204), index=dup_idx)
        eth = btc * 0.95 + rng.normal(0, 0.1, 204)
        sol = pd.Series(rng.normal(0, 1, 204), index=dup_idx)

        decision = gov.approve_entry(
            pair="BTC/USD",
            signal_price=65000.0,
            base_stake=100.0,
            equity=10_000.0,
            open_positions=[("ETH/USD", 100.0)],
            pair_returns={"BTC/USD": btc, "ETH/USD": eth, "SOL/USD": sol},
        )
        assert decision.blocking_constraint == "correlation_filter"

    def test_pearson_returns_handles_empty_series(self) -> None:
        gov = _gov()
        empty = pd.Series(dtype="float64")
        assert gov._pearson_returns(empty, empty) is None
        a = pd.Series([1.0, 2.0, 3.0])
        assert gov._pearson_returns(a, empty) is None

    def test_pearson_returns_non_datetime_index_fallback(self) -> None:
        """Series with non-datetime index falls back to position alignment."""
        gov = _gov(RiskConfig(correlation_min_overlap=5))
        rng = np.random.default_rng(1)
        a = pd.Series(rng.normal(0, 1, 50))  # int index
        b = pd.Series(a.values * 0.9 + rng.normal(0, 0.1, 50))
        rho = gov._pearson_returns(a, b)
        assert rho is not None and 0.5 < rho < 1.0

    def test_pearson_returns_zero_std_returns_none(self) -> None:
        gov = _gov(RiskConfig(correlation_min_overlap=5))
        a = pd.Series([1.0] * 50)
        b = pd.Series(np.random.default_rng(0).normal(0, 1, 50))
        assert gov._pearson_returns(a, b) is None


# ---------------------------------------------------------------------------
# 2. Port of test_risk_governor_backtest_isolation.py (Bug 2)
# ---------------------------------------------------------------------------


class TestAnchorIsolation:
    def test_backtest_runmode_uses_transient_anchor_path(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("RISK_GOVERNOR_ANCHORS_PATH", raising=False)
        for mode in ("backtest", "hyperopt", "edge"):
            p = _resolve_anchor_path(mode)
            assert p.parent == Path(tempfile.gettempdir()), (mode, p)
            assert str(os.getpid()) in p.name
            assert p.name.endswith(".json")

    def test_live_runmode_uses_default_persistent_path(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("RISK_GOVERNOR_ANCHORS_PATH", raising=False)
        for mode in (None, "live", "dry_run"):
            p = _resolve_anchor_path(mode)
            assert p.parent != Path(tempfile.gettempdir())
            assert p.name == "risk_governor_anchors.json"

    def test_env_override_wins_in_every_mode(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        override = tmp_path / "custom_anchor.json"
        monkeypatch.setenv("RISK_GOVERNOR_ANCHORS_PATH", str(override))
        for mode in (None, "live", "dry_run", "backtest", "hyperopt", "edge"):
            assert _resolve_anchor_path(mode) == override, mode

    def test_backtest_governor_does_not_read_live_anchor(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A poisoned live anchor must NOT bleed into a backtest governor."""
        poisoned = Path(os.environ["RISK_GOVERNOR_ANCHORS_PATH"])
        poisoned.parent.mkdir(parents=True, exist_ok=True)
        poisoned.write_text(
            json.dumps(
                {
                    "day_anchor_utc": "2026-05-12T00:00:00+00:00",
                    "starting_equity_today": 10_000.0,
                    "daily_realized_pnl": 0.0,
                    "peak_equity": 10_000.0,
                    "paused_for_drawdown": True,
                    "updated_at": "2026-05-12T18:00:00+00:00",
                }
            )
        )
        monkeypatch.delenv("RISK_GOVERNOR_ANCHORS_PATH", raising=False)

        gov = RiskGovernor(RiskConfig(), runmode="backtest")
        assert gov._paused_for_drawdown is False, (
            "backtest governor inherited paused_for_drawdown from the live "
            "anchor — bug 2 has regressed."
        )
        # No anchor file should exist at the transient path until persist.
        transient = _resolve_anchor_path("backtest")
        if transient.exists():
            transient.unlink()

    def test_backtest_governor_persists_to_transient_path(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("RISK_GOVERNOR_ANCHORS_PATH", raising=False)
        gov = RiskGovernor(
            RiskConfig(),
            runmode="backtest",
            now_fn=_fixed_now,
        )
        gov.update_equity(10_000.0)
        transient = _resolve_anchor_path("backtest")
        assert transient.exists()
        assert transient.parent == Path(tempfile.gettempdir())
        transient.unlink()

    def test_from_config_extracts_runmode_enum(self) -> None:
        class FakeRunMode:
            value = "backtest"

        cfg = {"risk_management": {}, "runmode": FakeRunMode()}
        gov = RiskGovernor.from_config(cfg)
        assert gov._runmode == "backtest"

    def test_from_config_handles_string_runmode(self) -> None:
        cfg = {"risk_management": {}, "runmode": "BACKTEST"}
        gov = RiskGovernor.from_config(cfg)
        assert gov._runmode == "backtest"

    def test_from_config_no_runmode_defaults_to_live(self) -> None:
        gov = RiskGovernor.from_config({"risk_management": {}})
        assert gov._runmode is None

    def test_backtest_runmodes_constant_includes_all_three(self) -> None:
        assert frozenset({"backtest", "hyperopt", "edge"}) == _BACKTEST_RUNMODES

    def test_from_config_file_reads_json(self, tmp_path: Path) -> None:
        cfg_path = tmp_path / "config.json"
        cfg_path.write_text(json.dumps({"risk_management": {"max_concurrent_positions": 9}}))
        gov = RiskGovernor.from_config_file(cfg_path)
        assert gov.config.max_concurrent_positions == 9


# ---------------------------------------------------------------------------
# 3. Hard gates — happy + failure paths
# ---------------------------------------------------------------------------


class TestApproveEntryGates:
    def test_approves_clean_entry(self) -> None:
        gov = _gov()
        decision = gov.approve_entry(
            pair="BTC/USD",
            signal_price=65000.0,
            base_stake=500.0,
            equity=10_000.0,
        )
        assert decision.approved
        assert decision.blocking_constraint is None
        assert decision.suggested_stake > 0
        assert decision.outcome == "pass"

    def test_drawdown_pause_blocks(self) -> None:
        gov = _gov(RiskConfig(max_portfolio_drawdown_pct=0.05))
        gov.update_equity(10_000.0)
        # Push a deep equity drop (-10%): trips the 5% drawdown gate.
        decision = gov.approve_entry(
            pair="BTC/USD",
            signal_price=65000.0,
            base_stake=100.0,
            equity=9_000.0,
        )
        assert not decision.approved
        assert decision.blocking_constraint == "max_drawdown_paused"

    def test_daily_loss_limit_blocks(self) -> None:
        cfg = RiskConfig(daily_loss_limit_pct=0.02)
        gov = _gov(cfg)
        gov.update_equity(10_000.0)
        # Realised PnL hammered down 2.5%, just over the 2% limit.
        gov._daily_realized_pnl = -250.0
        decision = gov.approve_entry(
            pair="BTC/USD",
            signal_price=65000.0,
            base_stake=100.0,
            equity=10_000.0,
        )
        assert not decision.approved
        assert decision.blocking_constraint == "daily_loss_limit"
        assert "unblocks_at" in decision.extra

    def test_daily_loss_uses_unrealised_pnl(self) -> None:
        """P0-I: unrealised P&L counts toward the daily-loss budget."""
        cfg = RiskConfig(daily_loss_limit_pct=0.02)
        gov = _gov(cfg)
        gov.update_equity(10_000.0)
        gov._daily_realized_pnl = 0.0
        decision = gov.approve_entry(
            pair="BTC/USD",
            signal_price=65000.0,
            base_stake=100.0,
            equity=10_000.0,
            open_unrealised_pnl=-250.0,
        )
        assert decision.blocking_constraint == "daily_loss_limit"

    def test_concurrent_positions_blocks(self) -> None:
        cfg = RiskConfig(max_concurrent_positions=2)
        gov = _gov(cfg)
        decision = gov.approve_entry(
            pair="BTC/USD",
            signal_price=1.0,
            base_stake=100.0,
            equity=10_000.0,
            open_positions=[("ETH/USD", 100.0), ("SOL/USD", 100.0)],
        )
        assert decision.blocking_constraint == "max_concurrent_positions"

    def test_circuit_breaker_trips_after_n_losses(self) -> None:
        cfg = RiskConfig(
            circuit_breaker_consecutive_losses=3,
            circuit_breaker_cooldown_hours=1.0,
            # Loose daily-loss limit so the circuit-breaker fires first,
            # not the daily-loss gate.
            daily_loss_limit_pct=0.50,
        )
        gov = _gov(cfg)
        gov.update_equity(10_000.0)
        for _ in range(3):
            gov.record_trade_close("BTC/USD", -100.0, -0.01)
        decision = gov.approve_entry(
            pair="BTC/USD", signal_price=1.0, base_stake=100.0, equity=10_000.0
        )
        assert decision.blocking_constraint == "circuit_breaker_cooldown"

    def test_circuit_breaker_resets_on_win(self) -> None:
        cfg = RiskConfig(circuit_breaker_consecutive_losses=3)
        gov = _gov(cfg)
        gov.update_equity(10_000.0)
        for _ in range(2):
            gov.record_trade_close("BTC/USD", -50.0, -0.005)
        gov.record_trade_close("BTC/USD", +200.0, +0.02)
        assert gov._consecutive_losses == 0

    def test_circuit_breaker_cooldown_expires(self) -> None:
        cfg = RiskConfig(
            circuit_breaker_consecutive_losses=2,
            circuit_breaker_cooldown_hours=1.0,
            daily_loss_limit_pct=0.50,
        )
        now_ref = _fixed_now()
        # Build governor with mutable clock.
        clock = [now_ref]
        gov = RiskGovernor(cfg, now_fn=lambda: clock[0])
        gov.update_equity(10_000.0)
        gov.record_trade_close("BTC/USD", -100.0, -0.01)
        gov.record_trade_close("BTC/USD", -100.0, -0.01)
        assert gov._cooldown_until is not None
        # Advance clock past cooldown
        clock[0] = now_ref + timedelta(hours=2)
        decision = gov.approve_entry(
            pair="BTC/USD", signal_price=1.0, base_stake=100.0, equity=10_000.0
        )
        assert decision.approved
        assert gov._cooldown_until is None

    def test_correlation_filter_blocks(self) -> None:
        gov = _gov(RiskConfig(correlation_threshold=0.5, correlation_min_overlap=10))
        gov.update_equity(10_000.0)
        idx = pd.date_range("2026-04-01", periods=100, freq="h", tz="UTC")
        rng = np.random.default_rng(1)
        base = rng.normal(0, 1, 100)
        btc = pd.Series(base, index=idx)
        eth = pd.Series(base * 0.95 + rng.normal(0, 0.05, 100), index=idx)
        decision = gov.approve_entry(
            pair="BTC/USD",
            signal_price=1.0,
            base_stake=100.0,
            equity=10_000.0,
            open_positions=[("ETH/USD", 100.0)],
            pair_returns={"BTC/USD": btc, "ETH/USD": eth},
        )
        assert decision.blocking_constraint == "correlation_filter"

    def test_correlation_skipped_when_pair_missing(self) -> None:
        gov = _gov()
        gov.update_equity(10_000.0)
        decision = gov.approve_entry(
            pair="BTC/USD",
            signal_price=1.0,
            base_stake=100.0,
            equity=10_000.0,
            open_positions=[("ETH/USD", 100.0)],
            pair_returns={"ETH/USD": pd.Series([1.0, 2.0])},  # BTC missing
        )
        assert decision.approved


# ---------------------------------------------------------------------------
# 4. Kelly sizing
# ---------------------------------------------------------------------------


class TestKelly:
    def test_kelly_returns_zero_without_history(self) -> None:
        gov = _gov(RiskConfig(kelly_min_trades=10))
        assert gov._kelly_fraction(0.6) == 0.0

    def test_kelly_disabled_returns_zero(self) -> None:
        gov = _gov(RiskConfig(kelly_enabled=False))
        assert gov._kelly_fraction(0.6) == 0.0

    def test_kelly_clips_confidence_to_open_interval(self) -> None:
        gov = _gov(RiskConfig(kelly_min_trades=2, kelly_lookback_trades=10))
        gov.record_trade_close("X", +100.0, +0.05)
        gov.record_trade_close("X", -50.0, -0.02)
        gov.record_trade_close("X", +100.0, +0.05)
        # Extremes clip but don't crash.
        assert gov._kelly_fraction(0.0) >= 0.0
        assert gov._kelly_fraction(1.0) >= 0.0

    def test_kelly_caps_at_max_fraction(self) -> None:
        cfg = RiskConfig(
            kelly_min_trades=2,
            kelly_lookback_trades=5,
            kelly_max_fraction=0.10,
            kelly_safety_factor=1.0,
        )
        gov = _gov(cfg)
        # All wins → huge raw Kelly, but cap should clip to 0.10.
        for _ in range(5):
            gov.record_trade_close("X", +100.0, +0.50)
        # Add a single loss so b is well-defined.
        gov.record_trade_close("X", -10.0, -0.01)
        f = gov._kelly_fraction(0.9)
        assert f <= cfg.kelly_max_fraction

    def test_kelly_zero_when_only_wins(self) -> None:
        cfg = RiskConfig(kelly_min_trades=2, kelly_lookback_trades=5)
        gov = _gov(cfg)
        for _ in range(3):
            gov.record_trade_close("X", +100.0, +0.05)
        # b is undefined (no losses) → returns 0.
        assert gov._kelly_fraction(0.6) == 0.0

    def test_approve_entry_uses_kelly_when_confidence_provided(self) -> None:
        cfg = RiskConfig(
            kelly_min_trades=2,
            kelly_lookback_trades=10,
            kelly_safety_factor=1.0,
            kelly_max_fraction=0.5,
            max_position_size_pct=0.5,
        )
        gov = _gov(cfg)
        for _ in range(5):
            gov.record_trade_close("X", +100.0, +0.05)
        for _ in range(2):
            gov.record_trade_close("X", -50.0, -0.02)
        decision = gov.approve_entry(
            pair="BTC/USD",
            signal_price=1.0,
            base_stake=10_000.0,  # >> Kelly suggestion
            equity=10_000.0,
            model_confidence=0.7,
        )
        assert decision.approved
        assert decision.kelly_fraction > 0
        assert decision.suggested_stake < 10_000.0


# ---------------------------------------------------------------------------
# 5. Persistence + manual resume
# ---------------------------------------------------------------------------


class TestPersistence:
    def test_anchor_round_trip(self) -> None:
        gov = _gov()
        gov.update_equity(10_000.0)
        gov.record_trade_close("BTC/USD", -100.0, -0.01)
        anchor_path = Path(os.environ["RISK_GOVERNOR_ANCHORS_PATH"])
        assert anchor_path.exists()
        data = json.loads(anchor_path.read_text())
        assert data["peak_equity"] == 10_000.0
        assert data["daily_realized_pnl"] == -100.0

    def test_paused_flag_persists_across_restart(self) -> None:
        cfg = RiskConfig(max_portfolio_drawdown_pct=0.05)
        gov = _gov(cfg)
        gov.update_equity(10_000.0)
        gov.update_equity(9_000.0)  # 10% drawdown → pause
        assert gov._paused_for_drawdown

        # Simulate restart: instantiate a fresh governor that reads the
        # same anchor file (auto-isolated by conftest).
        gov2 = _gov(cfg)
        assert gov2._paused_for_drawdown

    def test_manual_resume_clears_pause(self) -> None:
        cfg = RiskConfig(max_portfolio_drawdown_pct=0.05)
        gov = _gov(cfg)
        gov.update_equity(10_000.0)
        gov.update_equity(9_000.0)
        assert gov._paused_for_drawdown
        assert gov.resume_after_manual_review("operator-ok") is True
        assert not gov._paused_for_drawdown
        # Second call is no-op.
        assert gov.resume_after_manual_review() is False

    def test_load_corrupt_anchor_does_not_crash(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        bad = tmp_path / "bad.json"
        bad.write_text("not json")
        monkeypatch.setenv("RISK_GOVERNOR_ANCHORS_PATH", str(bad))
        # Must not raise.
        gov = RiskGovernor(RiskConfig(), now_fn=_fixed_now)
        assert not gov._paused_for_drawdown

    def test_load_naive_anchor_timestamp_is_tolerated(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        legacy = tmp_path / "legacy.json"
        legacy.write_text(
            json.dumps(
                {
                    # Naive timestamp — older versions didn't write tzinfo.
                    "day_anchor_utc": "2026-05-12T00:00:00",
                    "starting_equity_today": 10_000.0,
                    "daily_realized_pnl": -100.0,
                    "peak_equity": 11_000.0,
                    "paused_for_drawdown": False,
                }
            )
        )
        monkeypatch.setenv("RISK_GOVERNOR_ANCHORS_PATH", str(legacy))
        gov = RiskGovernor(RiskConfig(), now_fn=_fixed_now)
        assert gov._day_anchor_utc is not None
        assert gov._day_anchor_utc.tzinfo is not None


# ---------------------------------------------------------------------------
# 6. Status snapshot + decision serialisation
# ---------------------------------------------------------------------------


class TestSnapshot:
    def test_status_contains_required_fields(self) -> None:
        gov = _gov()
        gov.update_equity(10_000.0)
        snap = gov.status()
        for key in (
            "now_utc",
            "current_equity",
            "peak_equity",
            "drawdown_pct",
            "paused_for_drawdown",
            "daily_realized_pnl",
            "starting_equity_today",
            "consecutive_losses",
            "cooldown_until",
            "trades_recorded",
        ):
            assert key in snap

    def test_decision_to_dict_round_trip(self) -> None:
        d = RiskDecision(
            approved=True,
            reason="approved",
            blocking_constraint=None,
            suggested_stake=500.0,
            kelly_fraction=0.1,
            cap_fraction=0.10,
        )
        out = d.to_dict()
        assert out["outcome"] == "pass"
        assert out["suggested_stake"] == 500.0

    def test_status_drawdown_with_zero_peak_is_zero(self) -> None:
        gov = _gov()
        # No update_equity yet → peak = 0.
        snap = gov.status()
        assert snap["drawdown_pct"] == 0.0


# ---------------------------------------------------------------------------
# 7. RiskConfig.from_dict
# ---------------------------------------------------------------------------


class TestRiskConfigFromDict:
    def test_empty_dict_returns_defaults(self) -> None:
        cfg = RiskConfig.from_dict({})
        assert cfg.max_concurrent_positions == 6

    def test_none_returns_defaults(self) -> None:
        cfg = RiskConfig.from_dict(None)
        assert cfg.max_concurrent_positions == 6

    def test_unknown_keys_ignored(self) -> None:
        cfg = RiskConfig.from_dict({"max_concurrent_positions": 12, "unknown_key": 42})
        assert cfg.max_concurrent_positions == 12


# ---------------------------------------------------------------------------
# 8. Daily anchor rollover
# ---------------------------------------------------------------------------


def test_daily_anchor_rolls_over_at_utc_midnight() -> None:
    clock = [datetime(2026, 5, 12, 23, 50, tzinfo=UTC)]
    gov = RiskGovernor(RiskConfig(), now_fn=lambda: clock[0])
    gov.update_equity(10_000.0)
    gov.record_trade_close("BTC/USD", -100.0, -0.01)
    assert gov._daily_realized_pnl == -100.0

    # Cross UTC midnight.
    clock[0] = datetime(2026, 5, 13, 0, 5, tzinfo=UTC)
    gov.update_equity(10_000.0)
    assert gov._daily_realized_pnl == 0.0
    assert gov._day_anchor_utc is not None
    assert gov._day_anchor_utc.date() == datetime(2026, 5, 13).date()
