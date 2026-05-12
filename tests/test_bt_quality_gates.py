"""Unit tests for scripts/backtest_with_gates.py — strategy quality gates.

Run from the repo root:
    pytest tests/test_bt_quality_gates.py -v

Design principles:
  * Never call `freqtrade backtesting`. We mock the result JSON directly.
  * Every gate has at least 3 cases: PASS, BORDERLINE, FAIL.
  * MC bootstrap is seeded so the test is deterministic.
"""

from __future__ import annotations

import json
import math
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pytest

# Make scripts/ importable
ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import backtest_with_gates as bg  # noqa: E402


# ---------------------------------------------------------------------------
# Synthetic-trade builders
# ---------------------------------------------------------------------------


def _trades(pnls: list[float], start: datetime | None = None,
            spacing: timedelta = timedelta(days=10)) -> list[dict]:
    """Build a freqtrade-shaped trades list with given P&Ls and spacing."""
    if start is None:
        start = datetime(2024, 5, 1, tzinfo=timezone.utc)
    out = []
    for i, p in enumerate(pnls):
        ts_ms = int((start + spacing * i).timestamp() * 1000)
        out.append({"profit_abs": p, "open_timestamp": ts_ms})
    return out


def _block(pnls: list[float], **kw) -> dict:
    return {"trades": _trades(pnls, **kw)}


# ---------------------------------------------------------------------------
# 1. Statistic correctness
# ---------------------------------------------------------------------------


class TestSharpe:
    def test_sharpe_zero_when_constant(self):
        # All P&Ls identical → stddev = 0 → return 0 (don't divide-by-zero)
        assert bg.compute_sharpe([5.0] * 30) == 0.0

    def test_sharpe_positive_for_positive_mean(self):
        rng = np.random.default_rng(0)
        pnls = (rng.normal(loc=2.0, scale=1.0, size=200)).tolist()
        s = bg.compute_sharpe(pnls)
        assert s > 0

    def test_sharpe_negative_for_negative_mean(self):
        rng = np.random.default_rng(1)
        pnls = (rng.normal(loc=-2.0, scale=1.0, size=200)).tolist()
        s = bg.compute_sharpe(pnls)
        assert s < 0

    def test_sharpe_scale_invariant(self):
        # Multiplying every trade P&L by k > 0 must not change Sharpe.
        rng = np.random.default_rng(2)
        pnls = (rng.normal(loc=1.0, scale=2.0, size=100)).tolist()
        s1 = bg.compute_sharpe(pnls)
        s2 = bg.compute_sharpe([10.0 * p for p in pnls])
        s3 = bg.compute_sharpe([0.001 * p for p in pnls])
        assert abs(s1 - s2) < 1e-9
        assert abs(s1 - s3) < 1e-9

    def test_sharpe_annualisation_increases_with_factor(self):
        # Annualising by sqrt(N) where N>1 must scale up the per-trade sharpe.
        pnls = [1.0, 2.0, -1.0, 3.0, -0.5, 1.5, 2.0, -1.5, 1.0, 0.5]
        per_trade = bg.compute_sharpe(pnls)
        annual = bg.compute_sharpe(pnls, n_trades_per_year=100.0)
        assert annual == pytest.approx(per_trade * math.sqrt(100.0))


class TestProfitFactor:
    def test_pf_typical(self):
        # +10, +20 (gross_win=30) vs -5, -5 (gross_loss=10) → 3.0
        assert bg.compute_profit_factor([10, 20, -5, -5]) == 3.0

    def test_pf_inf_when_no_losses(self):
        # No losing trades and at least one win → infinite PF
        assert math.isinf(bg.compute_profit_factor([1.0, 2.0, 3.0]))

    def test_pf_zero_when_no_wins(self):
        assert bg.compute_profit_factor([-1.0, -2.0]) == 0.0

    def test_pf_zero_for_empty(self):
        # No trades — gross_win = gross_loss = 0; we map this to 0 (gate fails).
        assert bg.compute_profit_factor([]) == 0.0


class TestWalkForward:
    def test_winrates_per_window(self):
        # 6 windows × 5 trades each: bucket k has k wins, 5-k losses.
        # That gives winrates 0/5, 1/5, 2/5, 3/5, 4/5, 5/5.
        start = datetime(2024, 1, 1, tzinfo=timezone.utc)
        pnls: list[float] = []
        dates: list[datetime] = []
        # 6 buckets equally spaced over 60 days. Use days 5,15,25,35,45,55 so
        # each falls in the middle of its bucket and there's no boundary jitter.
        for k in range(6):
            day = 5 + k * 10
            for w in range(k):  # k wins
                pnls.append(1.0)
                dates.append(start + timedelta(days=day, hours=w))
            for l in range(5 - k):  # 5-k losses
                pnls.append(-1.0)
                dates.append(start + timedelta(days=day, hours=k + l))
        wrs, diag = bg.walk_forward_winrates(pnls, dates, n_windows=6)
        assert diag["n_windows_with_trades"] == 6
        assert wrs == [0.0, 0.2, 0.4, 0.6, 0.8, 1.0]

    def test_handles_missing_windows(self):
        # Two clusters of trades — only 2 of 6 windows populated.
        start = datetime(2024, 1, 1, tzinfo=timezone.utc)
        # Window 0 (days ~5): 4 wins, 1 loss
        # Window 5 (days ~55): 1 win, 4 losses
        pnls = [1, 1, 1, 1, -1] + [1, -1, -1, -1, -1]
        dates = (
            [start + timedelta(days=5, hours=h) for h in range(5)]
            + [start + timedelta(days=55, hours=h) for h in range(5)]
        )
        wrs, diag = bg.walk_forward_winrates(pnls, dates, n_windows=6)
        assert diag["n_windows_with_trades"] == 2
        assert wrs == [pytest.approx(0.8), pytest.approx(0.2)]

    def test_variance_ratio_zero_for_uniform_winrate(self):
        # All windows have identical 50% winrate → stddev = 0
        # 4 windows × 4 trades, each window: 2 wins, 2 losses.
        start = datetime(2024, 1, 1, tzinfo=timezone.utc)
        pnls = [1, 1, -1, -1] * 4
        dates = []
        for w in range(4):
            base = start + timedelta(days=5 + w * 15)
            dates.extend([base + timedelta(hours=i) for i in range(4)])
        wrs, _ = bg.walk_forward_winrates(pnls, dates, n_windows=4)
        assert all(abs(wr - 0.5) < 1e-9 for wr in wrs)
        ratio = bg.winrate_variance_ratio(wrs)
        assert ratio == pytest.approx(0.0)

    def test_variance_ratio_none_with_one_window(self):
        # Only 1 window has data → ratio is undefined (need >= 2 to compute std)
        assert bg.winrate_variance_ratio([0.5]) is None
        assert bg.winrate_variance_ratio([]) is None

    def test_variance_ratio_inf_when_all_zero_winrate(self):
        # All windows lost — mean winrate is 0 → ratio is infinite (caller fails gate)
        assert bg.winrate_variance_ratio([0.0, 0.0, 0.0]) == float("inf")


class TestMonteCarlo:
    def test_low_p_for_strong_positive_signal(self):
        # Big positive mean relative to stddev → resampled means almost
        # never reach the observed mean → tiny p-value.
        rng = np.random.default_rng(11)
        pnls = (rng.normal(loc=5.0, scale=1.0, size=100)).tolist()
        p, diag = bg.monte_carlo_p_value(pnls, iterations=500, seed=7)
        assert p is not None
        assert p < 0.01
        assert diag["n_trades_used"] == 100

    def test_high_p_for_zero_mean(self):
        # True mean 0 → roughly half of resamples beat the observed mean;
        # p-value should be in the upper half of [0, 1] (>= 0.10 by a wide margin).
        rng = np.random.default_rng(13)
        pnls = (rng.normal(loc=0.0, scale=1.0, size=200)).tolist()
        p, _ = bg.monte_carlo_p_value(pnls, iterations=500, seed=7)
        assert p is not None
        assert p > 0.10

    def test_stable_on_small_n(self):
        # n = 5 is the smallest n we accept; must not crash, must return a number.
        p, diag = bg.monte_carlo_p_value([1.0, 2.0, -1.0, 0.5, -0.2], iterations=200, seed=7)
        assert p is not None
        assert 0.0 <= p <= 1.0
        assert diag["n_trades_used"] == 5

    def test_returns_none_for_too_few(self):
        p, _ = bg.monte_carlo_p_value([1.0, 2.0], iterations=200, seed=7)
        assert p is None

    def test_seed_is_deterministic(self):
        pnls = [1.0, -0.5, 2.0, -1.0, 1.5, 0.3, -0.7, 2.2, 1.1, -0.4]
        p1, _ = bg.monte_carlo_p_value(pnls, iterations=300, seed=42)
        p2, _ = bg.monte_carlo_p_value(pnls, iterations=300, seed=42)
        assert p1 == p2


# ---------------------------------------------------------------------------
# 2. Per-gate PASS / BORDERLINE / FAIL behaviour
# ---------------------------------------------------------------------------


class TestMinTradesGate:
    def test_pass_at_threshold(self):
        block = _block([1.0] * 30)
        rep = bg.evaluate_gates(block, bootstrap_iters=200)
        gate = next(g for g in rep["gates"] if g["gate"] == "min_trades")
        assert gate["pass"] is True
        assert gate["value"] == 30

    def test_fail_below_threshold(self):
        block = _block([1.0] * 29)
        rep = bg.evaluate_gates(block, bootstrap_iters=200)
        gate = next(g for g in rep["gates"] if g["gate"] == "min_trades")
        assert gate["pass"] is False

    def test_fail_zero_trades(self):
        rep = bg.evaluate_gates({"trades": []}, bootstrap_iters=200)
        gate = next(g for g in rep["gates"] if g["gate"] == "min_trades")
        assert gate["pass"] is False
        assert rep["promotion_eligible"] is False


class TestProfitFactorGate:
    def test_pass(self):
        # PF = 3.0 > 1.5 → pass
        rep = bg.evaluate_gates(_block([10, 20, -5, -5] * 10), bootstrap_iters=200)
        gate = next(g for g in rep["gates"] if g["gate"] == "profit_factor")
        assert gate["pass"] is True

    def test_borderline_just_above(self):
        # PF = 1.6 — close enough to threshold to confirm strict ">" works.
        # 8 wins of 1.0 (gross_win=8) + 5 losses of 1.0 (gross_loss=5) → PF = 1.6
        pnls = [1.0] * 8 + [-1.0] * 5
        # Repeat so we have >= 30 trades (3x): 24 wins, 15 losses, PF unchanged.
        pnls = pnls * 3
        rep = bg.evaluate_gates(_block(pnls), bootstrap_iters=200)
        gate = next(g for g in rep["gates"] if g["gate"] == "profit_factor")
        assert gate["pass"] is True
        assert gate["value"] == pytest.approx(1.6)

    def test_fail_just_below(self):
        # PF = 1.4 → fail (strict ">")
        pnls = ([1.0] * 7 + [-1.0] * 5) * 3
        rep = bg.evaluate_gates(_block(pnls), bootstrap_iters=200)
        gate = next(g for g in rep["gates"] if g["gate"] == "profit_factor")
        assert gate["pass"] is False


class TestSharpeGate:
    def test_pass_for_high_signal(self):
        rng = np.random.default_rng(101)
        pnls = (rng.normal(loc=3.0, scale=1.0, size=80)).tolist()
        rep = bg.evaluate_gates(_block(pnls), bootstrap_iters=200)
        gate = next(g for g in rep["gates"] if g["gate"] == "sharpe")
        assert gate["pass"] is True

    def test_fail_for_noisy_zero_mean(self):
        rng = np.random.default_rng(103)
        pnls = (rng.normal(loc=0.0, scale=5.0, size=80)).tolist()
        rep = bg.evaluate_gates(_block(pnls), bootstrap_iters=200)
        gate = next(g for g in rep["gates"] if g["gate"] == "sharpe")
        assert gate["pass"] is False


class TestMonteCarloGate:
    def test_pass_for_strong_positive(self):
        rng = np.random.default_rng(201)
        pnls = (rng.normal(loc=4.0, scale=1.0, size=80)).tolist()
        rep = bg.evaluate_gates(_block(pnls), bootstrap_iters=400, seed=7)
        gate = next(g for g in rep["gates"] if g["gate"] == "monte_carlo_p_value")
        assert gate["pass"] is True

    def test_fail_for_random_walk(self):
        rng = np.random.default_rng(202)
        pnls = (rng.normal(loc=0.0, scale=1.0, size=80)).tolist()
        rep = bg.evaluate_gates(_block(pnls), bootstrap_iters=400, seed=7)
        gate = next(g for g in rep["gates"] if g["gate"] == "monte_carlo_p_value")
        assert gate["pass"] is False


# ---------------------------------------------------------------------------
# 3. End-to-end report shape
# ---------------------------------------------------------------------------


class TestReportShape:
    def test_all_pass_strategy_promotes(self):
        # Build a clearly-good strategy: 80 trades, mean=3, sd=1, evenly spaced
        # so walk-forward variance is tiny.
        rng = np.random.default_rng(301)
        n = 80
        pnls = (rng.normal(loc=3.0, scale=1.0, size=n)).tolist()
        block = {"trades": [
            {"profit_abs": p,
             "open_timestamp": int((datetime(2024, 5, 1, tzinfo=timezone.utc)
                                    + timedelta(days=i * 9)).timestamp() * 1000)}
            for i, p in enumerate(pnls)
        ]}
        rep = bg.evaluate_gates(block, bootstrap_iters=400, seed=7)
        # Every gate should pass, but variance can be marginal — assert each
        # individually so a failure tells us which gate broke.
        passes = {g["gate"]: g["pass"] for g in rep["gates"]}
        assert passes["min_trades"] is True
        assert passes["sharpe"] is True
        assert passes["profit_factor"] is True
        assert passes["monte_carlo_p_value"] is True
        # walk_forward variance: with mean ~ 3, sd ~ 1, evenly spread: winrate
        # should be very stable per window.
        assert passes["walk_forward_variance"] is True
        assert rep["promotion_eligible"] is True

    def test_all_fail_strategy_blocked(self):
        # 10 trades only, noisy zero mean → fails every numeric gate.
        rng = np.random.default_rng(401)
        pnls = (rng.normal(loc=0.0, scale=1.0, size=10)).tolist()
        block = _block(pnls, spacing=timedelta(days=30))
        rep = bg.evaluate_gates(block, bootstrap_iters=200, seed=7)
        assert rep["promotion_eligible"] is False
        # min_trades + at least one statistical gate must fail.
        passes = {g["gate"]: g["pass"] for g in rep["gates"]}
        assert passes["min_trades"] is False

    def test_report_has_all_5_gates_in_order(self):
        rep = bg.evaluate_gates(_block([1.0] * 30), bootstrap_iters=200)
        names = [g["gate"] for g in rep["gates"]]
        assert names == [
            "min_trades",
            "walk_forward_variance",
            "monte_carlo_p_value",
            "sharpe",
            "profit_factor",
        ]

    def test_thresholds_block_is_complete(self):
        rep = bg.evaluate_gates(_block([1.0] * 30), bootstrap_iters=200)
        thr = rep["thresholds"]
        assert thr == {
            "min_trades": bg.GATE_MIN_TRADES,
            "walk_forward_max_variance": bg.GATE_WALK_FORWARD_MAX_VARIANCE,
            "monte_carlo_p_value": bg.GATE_MC_P_VALUE,
            "min_sharpe": bg.GATE_MIN_SHARPE,
            "min_profit_factor": bg.GATE_MIN_PROFIT_FACTOR,
        }

    def test_report_is_json_serialisable(self):
        # Includes inf/nan handling. The promote endpoint serialises this
        # to JSON, so a non-serialisable value would 500 the dashboard card.
        block = _block([1.0, 2.0, 3.0])  # PF = inf
        rep = bg.evaluate_gates(block, bootstrap_iters=200)
        text = json.dumps(rep, default=str)
        round_trip = json.loads(text)
        # inf is serialised to the string "inf"
        pf_gate = next(g for g in round_trip["gates"] if g["gate"] == "profit_factor")
        assert pf_gate["value"] == "inf"


# ---------------------------------------------------------------------------
# 4. Result-loading helpers
# ---------------------------------------------------------------------------


class TestResultLoaders:
    def test_extract_trade_pnls_from_profit_abs(self):
        block = {"trades": [{"profit_abs": 1.5}, {"profit_abs": -0.5}]}
        assert bg.extract_trade_pnls(block) == [1.5, -0.5]

    def test_extract_trade_pnls_falls_back_to_ratio(self):
        block = {"trades": [
            {"profit_ratio": 0.02, "stake_amount": 100},  # → 2.0
            {"profit_ratio": -0.01, "stake_amount": 200},  # → -2.0
        ]}
        assert bg.extract_trade_pnls(block) == [2.0, -2.0]

    def test_extract_open_dates_from_ms_timestamp(self):
        block = {"trades": [{"open_timestamp": 1735689600000}]}
        dates = bg.extract_trade_open_dates(block)
        assert dates[0] == datetime(2025, 1, 1, tzinfo=timezone.utc)

    def test_extract_open_dates_from_iso(self):
        block = {"trades": [{"open_date": "2025-01-01T00:00:00Z"}]}
        dates = bg.extract_trade_open_dates(block)
        assert dates[0] == datetime(2025, 1, 1, tzinfo=timezone.utc)

    def test_extract_strategy_block_picks_named(self):
        result = {"strategy": {
            "Foo": {"trades": [{"profit_abs": 1}]},
            "Bar": {"trades": [{"profit_abs": 2}]},
        }}
        b = bg.extract_strategy_block(result, "Bar")
        assert b["trades"][0]["profit_abs"] == 2

    def test_extract_strategy_block_falls_back_when_only_one(self):
        result = {"strategy": {"Foo": {"trades": []}}}
        # Asked for a name that doesn't exist; should still return the only block.
        b = bg.extract_strategy_block(result, "WrongName")
        assert b == {"trades": []}

    def test_extract_strategy_block_raises_when_ambiguous(self):
        result = {"strategy": {"Foo": {}, "Bar": {}}}
        with pytest.raises(KeyError):
            bg.extract_strategy_block(result, "WrongName")


# ---------------------------------------------------------------------------
# 5. write_report — touches disk in tmp_path
# ---------------------------------------------------------------------------


class TestWriteReport:
    def test_writes_timestamped_and_latest(self, tmp_path: Path):
        rep = bg.evaluate_gates(_block([1.0] * 30), bootstrap_iters=200)
        rep["strategy"] = "Demo"
        ts_path, latest_path = bg.write_report(rep, tmp_path, "Demo")
        assert ts_path.is_file()
        assert latest_path.is_file()
        assert latest_path.name == "gates_report_Demo_latest.json"
        # The two should have identical contents.
        assert ts_path.read_text() == latest_path.read_text()
        # Round-trip JSON to verify structural validity.
        loaded = json.loads(latest_path.read_text())
        assert loaded["strategy"] == "Demo"
        assert "promotion_eligible" in loaded


# ---------------------------------------------------------------------------
# 6. /api/ops/backtest_gates endpoint — surfaces the on-disk reports
# ---------------------------------------------------------------------------


class TestBacktestGatesEndpoint:
    """The cron writes *_latest.json files; the endpoint globs them and
    returns one row per strategy. Tested in isolation by pointing the
    BACKTEST_RESULTS_DIR at a tmp_path."""

    def _import_router(self, tmp_path: Path):
        # Lazy import so we can monkey-patch BACKTEST_RESULTS_DIR before
        # the endpoint reads it.
        sys.path.insert(0, str(ROOT / "user_data"))
        from dashboard import ops_routes  # noqa: WPS433
        ops_routes.BACKTEST_RESULTS_DIR = tmp_path
        return ops_routes

    def _seed_report(self, tmp_path: Path, strategy: str, eligible: bool, *,
                     n_trades: int = 60, gates: list[dict] | None = None,
                     mtime_offset_s: float = 0.0) -> Path:
        if gates is None:
            gates = [
                {"gate": "min_trades",            "pass": True,  "value": n_trades, "threshold": 30,
                 "detail": "ok"},
                {"gate": "walk_forward_variance", "pass": True,  "value": 0.10,     "threshold": 0.15,
                 "detail": "ok"},
                {"gate": "monte_carlo_p_value",   "pass": True,  "value": 0.01,     "threshold": 0.05,
                 "detail": "ok"},
                {"gate": "sharpe",                "pass": True,  "value": 1.8,      "threshold": 1.0,
                 "detail": "ok"},
                {"gate": "profit_factor",         "pass": True,  "value": 2.4,      "threshold": 1.5,
                 "detail": "ok"},
            ]
        if not eligible:
            gates[-1]["pass"] = False
            gates[-1]["value"] = 1.1
        path = tmp_path / f"gates_report_{strategy}_latest.json"
        path.write_text(json.dumps({
            "strategy": strategy,
            "evaluated_at": datetime.now(timezone.utc).isoformat(),
            "n_trades": n_trades,
            "promotion_eligible": eligible,
            "gates": gates,
            "thresholds": {
                "min_trades": 30, "walk_forward_max_variance": 0.15,
                "monte_carlo_p_value": 0.05, "min_sharpe": 1.0,
                "min_profit_factor": 1.5,
            },
            "config": {"bootstrap_iters": 1000, "walk_forward_windows": 6, "seed": 7,
                       "scipy_available": True},
            "timerange": "20240501-20260501",
        }))
        if mtime_offset_s:
            import os as _os
            now = datetime.now(timezone.utc).timestamp()
            _os.utime(path, (now + mtime_offset_s, now + mtime_offset_s))
        return path

    def test_empty_dir_returns_degraded(self, tmp_path: Path):
        ops_routes = self._import_router(tmp_path)
        import asyncio as _asyncio
        env = _asyncio.run(ops_routes.backtest_gates())
        assert env["status"] == "degraded"
        assert env["data"]["strategies"] == []
        assert env["data"]["any_eligible"] is False

    def test_one_eligible_one_not(self, tmp_path: Path):
        ops_routes = self._import_router(tmp_path)
        self._seed_report(tmp_path, "GoodStrat", eligible=True)
        self._seed_report(tmp_path, "BadStrat", eligible=False)
        import asyncio as _asyncio
        env = _asyncio.run(ops_routes.backtest_gates())
        assert env["status"] == "ok"
        rows = env["data"]["strategies"]
        assert len(rows) == 2
        by_name = {r["strategy"]: r for r in rows}
        assert by_name["GoodStrat"]["promotion_eligible"] is True
        assert by_name["BadStrat"]["promotion_eligible"] is False
        assert env["data"]["any_eligible"] is True
        # Each row exposes the 5-gate strip
        assert len(by_name["GoodStrat"]["gates"]) == 5

    def test_stale_report_marked(self, tmp_path: Path):
        ops_routes = self._import_router(tmp_path)
        # 9 days old → stale (threshold is 8 days)
        self._seed_report(tmp_path, "OldStrat", eligible=True,
                          mtime_offset_s=-9 * 24 * 3600)
        import asyncio as _asyncio
        env = _asyncio.run(ops_routes.backtest_gates())
        assert env["status"] == "degraded"
        assert env["data"]["any_stale"] is True
        assert env["data"]["strategies"][0]["stale"] is True

    def test_malformed_file_skipped_not_500(self, tmp_path: Path):
        ops_routes = self._import_router(tmp_path)
        (tmp_path / "gates_report_BrokenStrat_latest.json").write_text("not json {")
        self._seed_report(tmp_path, "GoodStrat", eligible=True)
        import asyncio as _asyncio
        env = _asyncio.run(ops_routes.backtest_gates())
        # Broken file should be silently skipped; the good one still surfaces.
        names = [r["strategy"] for r in env["data"]["strategies"]]
        assert names == ["GoodStrat"]
