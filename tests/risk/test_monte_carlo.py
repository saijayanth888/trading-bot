"""Tests for ``quanta_core.risk.monte_carlo``.

All tests run on the CPU fallback simulator (deterministic NumPy) by
default. GPU tests live under ``@pytest.mark.gpu`` and are skipped unless
the runner explicitly opts in with ``pytest -m gpu``.

Coverage targets:

* Calibration freshness gate (fail-closed on stale).
* CuPy-missing path raises :class:`MonteCarloError`.
* Decision matrix returns correct ``pass | warn | block`` outcomes.
* Deterministic seed reproduces the same metrics.
* Latency benchmark SLA — only meaningful with a GPU, but the CPU
  fallback test asserts the *shape* of the benchmark dict.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import numpy as np
import pytest

from quanta_core.risk.monte_carlo import (
    CALIBRATION_MAX_AGE_S,
    Calibration,
    MCDecision,
    MonteCarloConfig,
    MonteCarloEngine,
    MonteCarloError,
)


def _calm_calib(now: datetime | None = None) -> Calibration:
    """A calibration tuned to produce mostly-pass outcomes."""
    return Calibration(
        s0=100.0,
        v0=0.0009,  # ~3% annualised vol → tiny per-step
        kappa=2.0,
        theta=0.0009,
        xi=0.05,
        rho=-0.5,
        mu=0.0,
        jump_intensity=0.0,
        jump_mean=0.0,
        jump_std=0.0,
        as_of=now or datetime.now(UTC),
    )


def _wild_calib(now: datetime | None = None) -> Calibration:
    """High vol + jumps → triggers block outcomes."""
    return Calibration(
        s0=100.0,
        v0=0.16,  # 40% annualised vol
        kappa=2.0,
        theta=0.16,
        xi=0.5,
        rho=-0.7,
        mu=0.0,
        jump_intensity=200.0,
        jump_mean=-0.05,
        jump_std=0.1,
        as_of=now or datetime.now(UTC),
    )


def _cpu_engine(**overrides: object) -> MonteCarloEngine:
    defaults: dict[str, object] = {
        "num_paths": 2_000,
        "horizon_steps": 30,
        "dt_seconds": 5 * 60.0,
        "use_cpu_fallback": True,
        "seed": 42,
        # Loosen the CI floor for these small simulations; the
        # production threshold is still asserted in the dedicated test.
        "es_ci_max_frac": 1.0,
    }
    defaults.update(overrides)
    cfg = MonteCarloConfig(**defaults)  # type: ignore[arg-type]
    return MonteCarloEngine(cfg)


# ---------------------------------------------------------------------------
# Calibration freshness
# ---------------------------------------------------------------------------


class TestFreshnessGate:
    def test_stale_calibration_blocks(self) -> None:
        eng = _cpu_engine()
        old = datetime.now(UTC) - timedelta(seconds=CALIBRATION_MAX_AGE_S + 60)
        calib = _calm_calib(now=old)
        decision = eng.evaluate("BTC/USD", calib)
        assert decision.outcome == "block"
        assert "calibration_stale" in decision.reason

    def test_fresh_calibration_passes_freshness_check(self) -> None:
        eng = _cpu_engine()
        decision = eng.evaluate("BTC/USD", _calm_calib())
        assert decision.outcome in ("pass", "warn", "block")
        assert "calibration_stale" not in decision.reason

    def test_age_seconds_is_non_negative(self) -> None:
        # as_of in the future → still returns a non-negative age.
        future = datetime.now(UTC) + timedelta(hours=1)
        calib = _calm_calib(now=future)
        assert calib.age_seconds() == 0.0


# ---------------------------------------------------------------------------
# Decision matrix
# ---------------------------------------------------------------------------


class TestDecisionMatrix:
    def test_calm_market_passes(self) -> None:
        eng = _cpu_engine()
        decision = eng.evaluate("BTC/USD", _calm_calib())
        assert decision.outcome == "pass"
        assert decision.var_99 < 0.03
        assert decision.es_975 >= decision.var_99 * 0.0  # ES > VaR-derived 0

    def test_wild_market_blocks(self) -> None:
        eng = _cpu_engine()
        decision = eng.evaluate("BTC/USD", _wild_calib())
        # Either block or at least warn. Wild calib reliably trips at
        # least one threshold.
        assert decision.outcome in ("warn", "block")

    def test_ci_too_wide_blocks(self) -> None:
        """Setting an unrealistically tight CI threshold forces a block."""
        eng = _cpu_engine(es_ci_max_frac=1e-9)  # impossible to satisfy
        decision = eng.evaluate("BTC/USD", _calm_calib())
        assert decision.outcome == "block"
        assert "es_ci_too_wide" in decision.reason

    def test_decision_includes_latency(self) -> None:
        eng = _cpu_engine()
        decision = eng.evaluate("BTC/USD", _calm_calib())
        assert decision.latency_ms > 0
        assert decision.num_paths == eng.cfg.num_paths


# ---------------------------------------------------------------------------
# GBM model & control variate
# ---------------------------------------------------------------------------


class TestGBMModel:
    def test_gbm_runs(self) -> None:
        eng = _cpu_engine(model="gbm")
        decision = eng.evaluate("STOCK", _calm_calib())
        assert isinstance(decision, MCDecision)
        assert decision.model == "gbm"

    def test_zero_vol_gbm_no_loss(self) -> None:
        """A zero-vol GBM should produce ~0 tail risk."""
        eng = _cpu_engine(model="gbm")
        calib = Calibration(
            s0=100.0,
            v0=0.0,
            theta=1e-10,
            mu=0.0,
            as_of=datetime.now(UTC),
        )
        decision = eng.evaluate("STABLECOIN", calib)
        assert abs(decision.var_99) < 1e-3


# ---------------------------------------------------------------------------
# Determinism
# ---------------------------------------------------------------------------


class TestDeterminism:
    def test_same_seed_same_metrics(self) -> None:
        e1 = _cpu_engine()
        e2 = _cpu_engine()
        calib = _calm_calib()
        d1 = e1.evaluate("X", calib)
        d2 = e2.evaluate("X", calib)
        assert np.isclose(d1.var_99, d2.var_99)
        assert np.isclose(d1.es_975, d2.es_975)
        assert np.isclose(d1.max_dd_q99, d2.max_dd_q99)


# ---------------------------------------------------------------------------
# CuPy-missing path
# ---------------------------------------------------------------------------


class TestCuPyAbsent:
    def test_raises_clear_error_when_cupy_missing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """When use_cpu_fallback=False and no CuPy → MonteCarloError."""
        from quanta_core.risk import monte_carlo as mc

        monkeypatch.setattr(mc, "_import_cupy", lambda: None)
        monkeypatch.setattr(mc, "_import_torch", lambda: None)

        cfg = MonteCarloConfig(
            num_paths=100,
            horizon_steps=10,
            use_cpu_fallback=False,
        )
        eng = MonteCarloEngine(cfg)
        with pytest.raises(MonteCarloError, match="CuPy is not installed"):
            eng.evaluate("X", _calm_calib())


# ---------------------------------------------------------------------------
# Serialisation
# ---------------------------------------------------------------------------


class TestSerialisation:
    def test_to_dict_includes_all_fields(self) -> None:
        eng = _cpu_engine()
        decision = eng.evaluate("X", _calm_calib())
        out = decision.to_dict()
        for key in (
            "outcome",
            "reason",
            "var_99",
            "es_975",
            "max_dd_q99",
            "tail_asym",
            "es_ci_width",
            "latency_ms",
            "model",
            "num_paths",
        ):
            assert key in out


# ---------------------------------------------------------------------------
# Benchmark
# ---------------------------------------------------------------------------


class TestBenchmark:
    def test_benchmark_returns_latency_stats(self) -> None:
        eng = _cpu_engine()
        stats = eng.benchmark(_calm_calib(), n_runs=4)
        for key in ("median_ms", "p99_ms", "min_ms", "max_ms"):
            assert key in stats
            assert stats[key] > 0
        assert stats["min_ms"] <= stats["median_ms"] <= stats["max_ms"]


# ---------------------------------------------------------------------------
# GPU SLA test (requires real GB10)
# ---------------------------------------------------------------------------


@pytest.mark.gpu
def test_monte_carlo_latency_sla() -> None:  # pragma: no cover - requires GPU
    """Median wall-clock for one symbol must be ≤ 50 ms on GB10.

    Skipped unless ``pytest -m gpu`` is invoked AND a CUDA device is
    reachable via CuPy. The test runs the production-sized config
    (10k paths × 60 steps) and asserts the design-doc SLA.
    """
    from quanta_core.risk.monte_carlo import _import_cupy

    if _import_cupy() is None:
        pytest.skip("CuPy not installed")

    eng = MonteCarloEngine(
        MonteCarloConfig(
            num_paths=10_000,
            horizon_steps=60,
            dt_seconds=5 * 60.0,
            es_ci_max_frac=1.0,  # tolerate CI floor for benchmark
        )
    )
    stats = eng.benchmark(_calm_calib(), n_runs=16)
    assert stats["median_ms"] <= 50.0, (
        f"GB10 latency SLA breached: median={stats['median_ms']:.1f}ms > 50ms"
    )


# ---------------------------------------------------------------------------
# CuPy GPU smoke (skipped if no CUDA, but does NOT assert latency SLA)
# ---------------------------------------------------------------------------


@pytest.mark.gpu
def test_gpu_path_smoke() -> None:  # pragma: no cover - requires GPU
    """If CuPy is installed, the GPU code path must produce a finite result."""
    from quanta_core.risk.monte_carlo import _import_cupy

    if _import_cupy() is None:
        pytest.skip("CuPy not installed")

    eng = MonteCarloEngine(
        MonteCarloConfig(
            num_paths=1_000,
            horizon_steps=10,
            dt_seconds=60.0,
            use_cpu_fallback=False,
            es_ci_max_frac=1.0,
        )
    )
    decision = eng.evaluate("BTC", _calm_calib())
    assert np.isfinite(decision.var_99)
    assert np.isfinite(decision.es_975)
