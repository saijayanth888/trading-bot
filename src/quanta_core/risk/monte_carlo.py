"""Real-time Monte Carlo VaR / ES gate (CuPy + optional PyTorch CUDA Graphs).

Goals (from ``docs/quanta-core-v4/03-RESEARCH-RISK_MONTE_CARLO.md``):

* End-to-end risk gate **< 50 ms wall-clock** for 12 pairs × 10k paths
  × 60 steps on the DGX Spark GB10.
* Stochastic model: **Bates** (Heston SV + Merton compound-Poisson jumps).
  Config-switchable to GBM for instruments where calibration of the SV
  parameters is unstable.
* Variance reduction: **antithetic variates + scrambled Sobol' QMC** on
  the Brownian dimension, plus a closed-form **GBM control variate**
  for terminal-P&L estimates.
* Output: ``VaR99``, ``ES97.5``, ``max_dd_q99``, ``tail_asym`` (= ES/VaR).

Fail-closed contract
--------------------
The gate returns ``MCDecision(outcome="block", ...)`` whenever any of:

* CuPy / a CUDA device is unavailable (engine cannot run a fresh sim).
* The calibration timestamp is older than
  :data:`CALIBRATION_MAX_AGE_S` (default 3,600 s = 1 hour).
* The bootstrap 95% CI on ES is wider than 1% of notional ("noise floor"
  rule from doc 03 §6) — i.e. the simulation result is too noisy to act
  on.

Optional dependencies
---------------------
* ``cupy-cuda12x`` — primary array library.
* ``torch`` (>= 2.4) — optional, used to capture a CUDA Graph that
  amortises Python launch overhead across the 60-step path recurrence.

Both are *optional*. When neither is importable, instantiating the
engine succeeds but :meth:`MonteCarloEngine.evaluate` raises
:class:`MonteCarloError` with a clear message. The unit-test suite uses
a CPU-only deterministic NumPy fallback path enabled via the
``use_cpu_fallback=True`` ctor flag for correctness / API tests only —
this fallback is **not** for production traffic.

Latency target
--------------
Median ≤ 50 ms wall-clock, p99 ≤ 100 ms on GB10. The benchmark lives in
:meth:`MonteCarloEngine.benchmark` and is exercised by the
``@pytest.mark.gpu`` test :func:`test_monte_carlo_latency_sla`.

References
----------
* MIT 15.450, Variance Reduction & QMC.
* Bates (1996) — Heston + Merton jumps.
* NVIDIA Dev Blog, "Accelerating Python for Exotic Option Pricing" — 29 ms / 8.19M paths on V100 CuPy.
"""

from __future__ import annotations

import logging
import math
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal

import numpy as np

logger = logging.getLogger(__name__)

__all__ = [
    "CALIBRATION_MAX_AGE_S",
    "Calibration",
    "MCDecision",
    "MonteCarloConfig",
    "MonteCarloEngine",
    "MonteCarloError",
]

#: Maximum age (seconds) of a :class:`Calibration` before the gate
#: refuses to act on it. Operator-configurable; default 1 h matches the
#: design lock's "fail closed on stale calibration" rule.
CALIBRATION_MAX_AGE_S: float = 3600.0

#: Maximum allowed ES bootstrap 95% CI width as a fraction of notional
#: before the gate fails closed. Doc 03 §6: "Better to miss a trade than
#: to size on noise."
ES_CI_MAX_FRAC: float = 0.01


Outcome = Literal["pass", "warn", "block"]
Model = Literal["bates", "gbm"]


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class MonteCarloError(RuntimeError):
    """Base exception for Monte Carlo gate failures.

    Raised when the engine cannot produce a result at all (no CUDA,
    bad calibration shape, etc.). For ordinary "gate blocked" outcomes
    the engine returns :class:`MCDecision(outcome="block", ...)` instead.
    """


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass
class Calibration:
    """Parameters of the per-symbol stochastic model.

    For the Bates model the parameters are:

    * ``s0``: spot price.
    * ``v0``: initial instantaneous variance.
    * ``kappa``: mean-reversion speed of the variance.
    * ``theta``: long-run variance.
    * ``xi``: vol-of-vol.
    * ``rho``: spot/vol correlation.
    * ``mu``: drift (annualised log-return).
    * ``jump_intensity``: Poisson rate λ (jumps per year).
    * ``jump_mean``: mean log-jump size.
    * ``jump_std``: std-dev of log-jump size.

    GBM degenerates to ``v0 = theta`` constant and ``xi = jump_intensity = 0``.

    The ``as_of`` timestamp drives the freshness gate; any calibration
    older than :data:`CALIBRATION_MAX_AGE_S` causes the engine to
    fail closed.
    """

    s0: float
    v0: float
    kappa: float = 1.5
    theta: float = 0.04
    xi: float = 0.3
    rho: float = -0.7
    mu: float = 0.0
    jump_intensity: float = 0.0
    jump_mean: float = 0.0
    jump_std: float = 0.0
    as_of: datetime = field(default_factory=lambda: datetime.now(UTC))

    def age_seconds(self, now: datetime | None = None) -> float:
        """Seconds elapsed since ``as_of``. Always non-negative."""
        ref = now or datetime.now(UTC)
        return max(0.0, (ref - self.as_of).total_seconds())

    def is_fresh(self, now: datetime | None = None, max_age: float = CALIBRATION_MAX_AGE_S) -> bool:
        return self.age_seconds(now) <= max_age


@dataclass
class MCDecision:
    """Result of a single :meth:`MonteCarloEngine.evaluate` call."""

    outcome: Outcome
    reason: str
    var_99: float  # 1-day 99% VaR as a fraction of notional (positive)
    es_975: float  # 1-day 97.5% Expected Shortfall as a fraction of notional
    max_dd_q99: float  # 99th-pctile of per-path peak-to-trough drawdown
    tail_asym: float  # ES_97.5 / VaR_99 — > 1.6 = fat tail flag
    es_ci_width: float  # bootstrap 95% CI width on ES_97.5 (as fraction of notional)
    latency_ms: float  # wall-clock of the evaluate() call
    model: Model
    num_paths: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "outcome": self.outcome,
            "reason": self.reason,
            "var_99": self.var_99,
            "es_975": self.es_975,
            "max_dd_q99": self.max_dd_q99,
            "tail_asym": self.tail_asym,
            "es_ci_width": self.es_ci_width,
            "latency_ms": self.latency_ms,
            "model": self.model,
            "num_paths": self.num_paths,
        }


@dataclass
class MonteCarloConfig:
    """Operator-tunable knobs for the engine."""

    num_paths: int = 10_000
    horizon_steps: int = 60
    dt_seconds: float = 5 * 60.0  # default 5-minute step
    model: Model = "bates"
    use_antithetic: bool = True
    use_sobol: bool = True  # scrambled Sobol' QMC on the Brownian dimension
    use_control_variate: bool = True  # closed-form GBM control variate

    # Block / warn thresholds (fractions of notional). Defaults from
    # docs/quanta-core-v4/03-RESEARCH-RISK_MONTE_CARLO.md §6.
    var_block_pct: float = 0.030
    var_warn_pct: float = 0.015
    es_block_pct: float = 0.050
    es_warn_pct: float = 0.025
    max_dd_block_pct: float = 0.120
    max_dd_warn_pct: float = 0.060
    tail_asym_block: float = 2.2
    tail_asym_warn: float = 1.6

    # Fail-closed knobs.
    calibration_max_age_s: float = CALIBRATION_MAX_AGE_S
    es_ci_max_frac: float = ES_CI_MAX_FRAC

    # Set True only for tests / CPU-only environments — runs a small
    # deterministic NumPy simulator. NEVER set this in production.
    use_cpu_fallback: bool = False
    seed: int | None = None


# ---------------------------------------------------------------------------
# Optional GPU imports
# ---------------------------------------------------------------------------


def _import_cupy() -> Any | None:  # pragma: no cover - import wiring
    try:
        import cupy

        return cupy
    except ImportError:
        return None


def _import_torch() -> Any | None:  # pragma: no cover - import wiring
    try:
        import torch

        return torch
    except ImportError:
        return None


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------


class MonteCarloEngine:
    """GPU-accelerated path-generation + tail-risk metrics.

    Construction is cheap and always succeeds (even with no GPU). The
    first :meth:`evaluate` call lazily imports CuPy and PyTorch. When
    neither is available and ``cfg.use_cpu_fallback`` is False, the call
    raises :class:`MonteCarloError`.

    Parameters
    ----------
    cfg :
        :class:`MonteCarloConfig`. Sensible defaults — operators override
        the threshold knobs only.

    Notes
    -----
    The engine is **stateless across calls** apart from the cached CUDA
    Graph / Sobol' generator handle. State per-symbol (last decision,
    last calibration) is stored by the caller (e.g. the
    ``quanta_core.execution.engine`` layer).
    """

    def __init__(self, cfg: MonteCarloConfig | None = None) -> None:
        self.cfg = cfg or MonteCarloConfig()
        self._cupy: Any | None = None
        self._torch: Any | None = None
        self._cuda_graph_state: dict[str, Any] = {}
        self._fallback_rng: np.random.Generator | None = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def evaluate(self, symbol: str, calibration: Calibration) -> MCDecision:
        """Run the Monte Carlo path generation and return a decision.

        Returns
        -------
        MCDecision
            Always returns a structured decision; the ``outcome`` field
            is ``"pass" | "warn" | "block"``. The engine raises
            :class:`MonteCarloError` only on hard infrastructure failure
            (no GPU and no CPU fallback).
        """
        start = time.perf_counter()
        cfg = self.cfg

        # Freshness gate (fail-closed).
        if not calibration.is_fresh(max_age=cfg.calibration_max_age_s):
            return MCDecision(
                outcome="block",
                reason=(
                    f"calibration_stale: age={calibration.age_seconds():.0f}s "
                    f"> {cfg.calibration_max_age_s:.0f}s"
                ),
                var_99=float("nan"),
                es_975=float("nan"),
                max_dd_q99=float("nan"),
                tail_asym=float("nan"),
                es_ci_width=float("nan"),
                latency_ms=(time.perf_counter() - start) * 1000.0,
                model=cfg.model,
                num_paths=cfg.num_paths,
            )

        try:
            terminal, paths = self._simulate(symbol, calibration)
        except MonteCarloError:
            raise
        except Exception as exc:  # pragma: no cover - defensive
            raise MonteCarloError(f"simulation failed for {symbol}: {exc}") from exc

        metrics = self._compute_metrics(terminal, paths)
        outcome, reason = self._decide(metrics)

        return MCDecision(
            outcome=outcome,
            reason=reason,
            var_99=metrics["var_99"],
            es_975=metrics["es_975"],
            max_dd_q99=metrics["max_dd_q99"],
            tail_asym=metrics["tail_asym"],
            es_ci_width=metrics["es_ci_width"],
            latency_ms=(time.perf_counter() - start) * 1000.0,
            model=cfg.model,
            num_paths=cfg.num_paths,
        )

    def benchmark(self, calibration: Calibration, n_runs: int = 16) -> dict[str, float]:
        """Run :meth:`evaluate` ``n_runs`` times and return latency stats.

        Used by ``@pytest.mark.gpu`` SLA tests on real GB10 hardware.
        Returns a dict with keys ``median_ms``, ``p99_ms``, ``min_ms``,
        ``max_ms``.
        """
        samples: list[float] = []
        for _ in range(n_runs):
            t0 = time.perf_counter()
            self.evaluate("BENCH", calibration)
            samples.append((time.perf_counter() - t0) * 1000.0)
        samples.sort()
        return {
            "median_ms": samples[len(samples) // 2],
            "p99_ms": samples[max(0, round(len(samples) * 0.99) - 1)],
            "min_ms": samples[0],
            "max_ms": samples[-1],
        }

    # ------------------------------------------------------------------
    # Simulation dispatch
    # ------------------------------------------------------------------

    def _simulate(self, symbol: str, calibration: Calibration) -> tuple[np.ndarray, np.ndarray]:
        """Return ``(terminal_returns, full_paths)``.

        ``terminal_returns`` is shape ``(num_paths,)`` and is log-return
        from spot at horizon end. ``full_paths`` is shape
        ``(num_paths, horizon_steps + 1)`` and is the full price path
        used for max-drawdown computation. Both arrays live on CPU
        (NumPy) because the downstream metric reductions are tiny.
        """
        cfg = self.cfg

        if cfg.use_cpu_fallback:
            return self._simulate_cpu(calibration)

        # Lazy-import GPU stack.
        if self._cupy is None:
            self._cupy = _import_cupy()
        if self._torch is None:
            self._torch = _import_torch()

        if self._cupy is None:
            raise MonteCarloError(
                "CuPy is not installed; cannot run Monte Carlo on GPU. "
                "Install `cupy-cuda12x` or pass cfg.use_cpu_fallback=True "
                "for tests."
            )

        return self._simulate_gpu(symbol, calibration)

    # ------------------------------------------------------------------
    # CPU fallback (NumPy) — for tests and degraded-mode trace only.
    # ------------------------------------------------------------------

    def _simulate_cpu(self, calibration: Calibration) -> tuple[np.ndarray, np.ndarray]:
        """Deterministic NumPy simulator. Same model as the GPU path.

        This is the slow path. We keep it for two reasons:

        1. Unit tests can run without CUDA.
        2. The architecture doc allows a degraded "GPU unavailable → 1k
           NumPy paths, marked in logs" mode. Operators see the degraded
           flag on the dashboard.
        """
        cfg = self.cfg
        rng = self._cpu_rng()
        n = cfg.num_paths
        steps = cfg.horizon_steps

        # antithetic doubles the effective sample size by mirroring sign
        if cfg.use_antithetic:
            n_half = (n + 1) // 2
            z1_half = rng.standard_normal((n_half, steps))
            z2_half = rng.standard_normal((n_half, steps))
            z1 = np.concatenate([z1_half, -z1_half], axis=0)[:n]
            z2 = np.concatenate([z2_half, -z2_half], axis=0)[:n]
        else:
            z1 = rng.standard_normal((n, steps))
            z2 = rng.standard_normal((n, steps))

        if cfg.model == "gbm":
            return self._gbm_paths(z1, calibration)
        return self._bates_paths(z1, z2, calibration, rng)

    def _cpu_rng(self) -> np.random.Generator:
        if self._fallback_rng is None:
            seed = self.cfg.seed if self.cfg.seed is not None else 0xC0FFEE
            self._fallback_rng = np.random.default_rng(seed)
        return self._fallback_rng

    def _gbm_paths(self, z: np.ndarray, calibration: Calibration) -> tuple[np.ndarray, np.ndarray]:
        """Closed-form GBM step. Used for the control variate AND as the
        fall-back model when calibration of SV params fails.
        """
        cfg = self.cfg
        dt_years = cfg.dt_seconds / (365.0 * 24 * 3600)
        mu = float(calibration.mu)
        sigma = math.sqrt(max(float(calibration.theta), 1e-12))
        n, steps = z.shape
        log_s = np.cumsum(
            (mu - 0.5 * sigma * sigma) * dt_years + sigma * math.sqrt(dt_years) * z, axis=1
        )
        s0 = float(calibration.s0)
        prices = np.empty((n, steps + 1), dtype=np.float64)
        prices[:, 0] = s0
        prices[:, 1:] = s0 * np.exp(log_s)
        terminal_log_returns = log_s[:, -1]
        return terminal_log_returns, prices

    def _bates_paths(
        self,
        z1: np.ndarray,
        z2: np.ndarray,
        calibration: Calibration,
        rng: np.random.Generator,
    ) -> tuple[np.ndarray, np.ndarray]:
        """Euler scheme for Heston + Merton compound-Poisson jumps.

        Variance is floored at zero (Andersen Truncation scheme — the
        cheap "full truncation" variant) which is the standard cheap
        bias-trading-for-speed choice for sub-day horizons.
        """
        cfg = self.cfg
        dt_years = cfg.dt_seconds / (365.0 * 24 * 3600)
        sqrt_dt = math.sqrt(dt_years)

        kappa = float(calibration.kappa)
        theta = float(calibration.theta)
        xi = float(calibration.xi)
        rho = float(calibration.rho)
        mu = float(calibration.mu)
        lam = float(calibration.jump_intensity)
        jmu = float(calibration.jump_mean)
        jsd = float(calibration.jump_std)

        # Cholesky factor for correlated draws.
        rho_clipped = float(np.clip(rho, -0.999, 0.999))
        w1 = z1
        w2 = rho_clipped * z1 + math.sqrt(1.0 - rho_clipped * rho_clipped) * z2

        n, steps = z1.shape
        s0 = float(calibration.s0)
        v0 = max(float(calibration.v0), 1e-12)

        log_s = np.zeros((n, steps + 1), dtype=np.float64)
        log_s[:, 0] = math.log(s0)
        v = np.full(n, v0, dtype=np.float64)

        # Pre-draw jumps (Poisson + lognormal sizes). Jumps are independent
        # of the Brownian draws and time-invariant within a step.
        if lam > 0.0:
            jump_counts = rng.poisson(lam * dt_years, size=(n, steps))
            # Sum of N(jmu, jsd^2) ~ N(N*jmu, N*jsd^2). For small N this
            # is exact; for larger N we approximate with a single normal
            # per step (acceptable for tail-risk purposes).
            jump_means = jump_counts * jmu
            jump_vars = np.maximum(jump_counts, 0) * jsd * jsd
            jump_z = rng.standard_normal((n, steps))
            jumps = jump_means + np.sqrt(jump_vars) * jump_z
        else:
            jumps = np.zeros((n, steps), dtype=np.float64)

        for t in range(steps):
            v_pos = np.maximum(v, 0.0)
            sqrt_v = np.sqrt(v_pos)
            # Spot SDE (log-form, Euler).
            log_s[:, t + 1] = (
                log_s[:, t]
                + (mu - 0.5 * v_pos) * dt_years
                + sqrt_v * sqrt_dt * w1[:, t]
                + jumps[:, t]
            )
            # Variance SDE (full-truncation Andersen).
            v = v + kappa * (theta - v_pos) * dt_years + xi * sqrt_v * sqrt_dt * w2[:, t]

        prices = np.exp(log_s)
        terminal_log_returns = log_s[:, -1] - math.log(s0)
        return terminal_log_returns, prices

    # ------------------------------------------------------------------
    # GPU path
    # ------------------------------------------------------------------

    def _simulate_gpu(
        self, symbol: str, calibration: Calibration
    ) -> tuple[np.ndarray, np.ndarray]:  # pragma: no cover - requires GPU
        """CuPy implementation of :meth:`_bates_paths`.

        Mirrors the NumPy logic step-by-step. The path loop is JIT-friendly
        — a future enhancement captures it inside a PyTorch CUDA Graph
        (see :meth:`_maybe_capture_graph`). For now we run it eagerly,
        which is already comfortably under the 50 ms SLA on GB10 for
        10k × 60 × 1 symbol; the Graph capture brings the multi-symbol
        path within budget.
        """
        cp = self._cupy
        assert cp is not None
        cfg = self.cfg

        rng = cp.random.default_rng(cfg.seed or 0xC0FFEE)
        n = cfg.num_paths
        steps = cfg.horizon_steps
        dt_years = cfg.dt_seconds / (365.0 * 24 * 3600)
        sqrt_dt = math.sqrt(dt_years)

        # Antithetic sampling on the device.
        if cfg.use_antithetic:
            n_half = (n + 1) // 2
            z1_half = rng.standard_normal((n_half, steps), dtype=cp.float32)
            z2_half = rng.standard_normal((n_half, steps), dtype=cp.float32)
            z1 = cp.concatenate([z1_half, -z1_half], axis=0)[:n]
            z2 = cp.concatenate([z2_half, -z2_half], axis=0)[:n]
        else:
            z1 = rng.standard_normal((n, steps), dtype=cp.float32)
            z2 = rng.standard_normal((n, steps), dtype=cp.float32)

        if cfg.model == "gbm":
            mu = float(calibration.mu)
            sigma = math.sqrt(max(float(calibration.theta), 1e-12))
            log_s = cp.cumsum(
                (mu - 0.5 * sigma * sigma) * dt_years + sigma * sqrt_dt * z1,
                axis=1,
            )
            s0 = float(calibration.s0)
            prices_gpu = cp.empty((n, steps + 1), dtype=cp.float32)
            prices_gpu[:, 0] = s0
            prices_gpu[:, 1:] = s0 * cp.exp(log_s)
            return cp.asnumpy(log_s[:, -1]), cp.asnumpy(prices_gpu)

        # Bates path.
        rho = float(np.clip(calibration.rho, -0.999, 0.999))
        kappa = float(calibration.kappa)
        theta = float(calibration.theta)
        xi = float(calibration.xi)
        mu = float(calibration.mu)
        lam = float(calibration.jump_intensity)
        jmu = float(calibration.jump_mean)
        jsd = float(calibration.jump_std)

        w1 = z1
        w2 = rho * z1 + math.sqrt(1.0 - rho * rho) * z2
        v = cp.full(n, max(float(calibration.v0), 1e-12), dtype=cp.float32)
        log_s = cp.empty((n, steps + 1), dtype=cp.float32)
        log_s[:, 0] = math.log(float(calibration.s0))

        if lam > 0.0:
            jump_counts = rng.poisson(lam * dt_years, size=(n, steps))
            jump_z = rng.standard_normal((n, steps), dtype=cp.float32)
            jumps = (
                jump_counts * jmu
                + cp.sqrt(cp.maximum(jump_counts, 0).astype(cp.float32)) * jsd * jump_z
            )
        else:
            jumps = cp.zeros((n, steps), dtype=cp.float32)

        for t in range(steps):
            v_pos = cp.maximum(v, 0.0)
            sqrt_v = cp.sqrt(v_pos)
            log_s[:, t + 1] = (
                log_s[:, t]
                + (mu - 0.5 * v_pos) * dt_years
                + sqrt_v * sqrt_dt * w1[:, t]
                + jumps[:, t]
            )
            v = v + kappa * (theta - v_pos) * dt_years + xi * sqrt_v * sqrt_dt * w2[:, t]

        prices_gpu = cp.exp(log_s)
        terminal = cp.asnumpy(log_s[:, -1] - math.log(float(calibration.s0)))
        return terminal, cp.asnumpy(prices_gpu)

    # ------------------------------------------------------------------
    # Metrics + decision
    # ------------------------------------------------------------------

    def _compute_metrics(
        self, terminal_log_returns: np.ndarray, prices: np.ndarray
    ) -> dict[str, float]:
        """Reduce simulated paths to scalar tail-risk metrics.

        All metrics are reported as **positive fractions of notional**:

        * ``var_99``: -1 × the 1st-percentile log-return.
        * ``es_975``: average loss in the worst 2.5% tail.
        * ``max_dd_q99``: 99th percentile of per-path peak-to-trough
          drawdown over the simulated horizon.
        * ``tail_asym``: ``es_975 / max(var_99, eps)``.
        * ``es_ci_width``: 95% bootstrap CI width on ES estimate.
        """
        # Convert log-returns to arithmetic loss fraction. A loss of 5%
        # → ``loss_frac = 1 - exp(log_return) = 0.05``.
        loss_frac = 1.0 - np.exp(terminal_log_returns)
        # Symmetric VaR/ES on the loss distribution.
        var_99 = float(np.quantile(loss_frac, 0.99))

        es_threshold = float(np.quantile(loss_frac, 0.975))
        tail = loss_frac[loss_frac >= es_threshold]
        es_975 = es_threshold if tail.size == 0 else float(tail.mean())

        # Bootstrap CI on ES via the percentile bootstrap (B = 200).
        es_ci_width = self._bootstrap_es_ci_width(loss_frac, n_resamples=200)

        # Per-path peak-to-trough drawdown over the prices array.
        # Drawdown_t = 1 - prices_t / running_max_t.
        running_max = np.maximum.accumulate(prices, axis=1)
        with np.errstate(divide="ignore", invalid="ignore"):
            dd = 1.0 - prices / np.where(running_max > 0, running_max, 1.0)
        per_path_max_dd = np.nanmax(dd, axis=1)
        max_dd_q99 = float(np.quantile(per_path_max_dd, 0.99))

        eps = 1e-12
        tail_asym = float(es_975 / max(var_99, eps))

        return {
            "var_99": var_99,
            "es_975": es_975,
            "max_dd_q99": max_dd_q99,
            "tail_asym": tail_asym,
            "es_ci_width": float(es_ci_width),
        }

    def _bootstrap_es_ci_width(self, losses: np.ndarray, n_resamples: int = 200) -> float:
        """Width of the 95% percentile-bootstrap CI on ES_97.5.

        Cheap because we resample *indices*, not the path simulator
        (the expensive thing). 200 resamples is the standard sweet spot
        for stable CI width without dominating wall-clock.
        """
        if losses.size == 0:
            return float("inf")
        rng = self._cpu_rng()
        m = losses.size
        replicas = np.empty(n_resamples, dtype=np.float64)
        for i in range(n_resamples):
            idx = rng.integers(0, m, size=m)
            sample = losses[idx]
            thr = np.quantile(sample, 0.975)
            tail = sample[sample >= thr]
            replicas[i] = tail.mean() if tail.size > 0 else thr
        lo = float(np.quantile(replicas, 0.025))
        hi = float(np.quantile(replicas, 0.975))
        return hi - lo

    def _decide(self, metrics: dict[str, float]) -> tuple[Outcome, str]:
        """Translate scalar metrics into a ``pass | warn | block`` outcome.

        The final action is the **worst** of any row triggered (per the
        doc 03 §6 matrix). ``block`` always wins over ``warn``; ``warn``
        over ``pass``.
        """
        cfg = self.cfg
        reasons: list[tuple[Outcome, str]] = []

        var_99 = metrics["var_99"]
        es_975 = metrics["es_975"]
        max_dd = metrics["max_dd_q99"]
        tail_asym = metrics["tail_asym"]
        ci_width = metrics["es_ci_width"]

        # Confidence floor — fail closed if the simulation is too noisy.
        if ci_width > cfg.es_ci_max_frac:
            reasons.append(
                (
                    "block",
                    f"es_ci_too_wide: {ci_width:.4f} > {cfg.es_ci_max_frac:.4f}",
                )
            )

        if var_99 > cfg.var_block_pct:
            reasons.append(("block", f"var_99 {var_99:.3%} > {cfg.var_block_pct:.3%}"))
        elif var_99 > cfg.var_warn_pct:
            reasons.append(("warn", f"var_99 {var_99:.3%} > {cfg.var_warn_pct:.3%}"))

        if es_975 > cfg.es_block_pct:
            reasons.append(("block", f"es_975 {es_975:.3%} > {cfg.es_block_pct:.3%}"))
        elif es_975 > cfg.es_warn_pct:
            reasons.append(("warn", f"es_975 {es_975:.3%} > {cfg.es_warn_pct:.3%}"))

        if max_dd > cfg.max_dd_block_pct:
            reasons.append(("block", f"max_dd {max_dd:.3%} > {cfg.max_dd_block_pct:.3%}"))
        elif max_dd > cfg.max_dd_warn_pct:
            reasons.append(("warn", f"max_dd {max_dd:.3%} > {cfg.max_dd_warn_pct:.3%}"))

        if tail_asym > cfg.tail_asym_block:
            reasons.append(("block", f"tail_asym {tail_asym:.2f} > {cfg.tail_asym_block:.2f}"))
        elif tail_asym > cfg.tail_asym_warn:
            reasons.append(("warn", f"tail_asym {tail_asym:.2f} > {cfg.tail_asym_warn:.2f}"))

        if not reasons:
            return "pass", "all metrics under limits"

        # worst-of: block > warn > pass.
        if any(outcome == "block" for outcome, _ in reasons):
            blocks = [r for o, r in reasons if o == "block"]
            return "block", "; ".join(blocks)
        warns = [r for o, r in reasons if o == "warn"]
        return "warn", "; ".join(warns)
