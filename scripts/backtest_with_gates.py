#!/usr/bin/env python3
"""backtest_with_gates.py — pass/fail wrapper around `freqtrade backtesting`.

Enforces 5 quality gates before a strategy is "promotion eligible":

    1. min 30 trades
    2. walk-forward win-rate variance < 15% (stddev/mean across N windows)
    3. Monte-Carlo bootstrap p-value < 0.05  (strategy beats zero-mean random)
    4. Sharpe > 1.0  (annualised, scale-invariant)
    5. profit factor > 1.5  (gross_win / gross_loss)

Why these specific thresholds — see HANDOFF.md in the same commit.

Usage
-----
    # NOTE 2026-05-14: this script wraps freqtrade backtesting; the
    # freqtrade container + FreqAIMeanRevV1 strategy were retired on
    # 2026-05-14, so live invocation requires a port to quanta-core
    # backtesting. The script remains on disk for the port reference.
    # Historical usage:
    python scripts/backtest_with_gates.py \\
        --strategy FreqAIMeanRevV1 \\
        --timerange 20240501-20260501 \\
        --config /app/user_data/config.json

Output
------
A report JSON is always written to:

    user_data/backtest_results/gates_report_<strategy>_<timestamp>.json

…and a stable copy is also written to:

    user_data/backtest_results/gates_report_<strategy>_latest.json

(the dashboard endpoint /api/ops/backtest_gates reads the *_latest.json files).

Exit status
-----------
    0 → all 5 gates pass  ("promotion_eligible": true)
    1 → at least one gate failed
    2 → infrastructure error (backtest crash, missing result, …)

NO automatic promotion happens. The dashboard surfaces a recommendation; the
operator flips the live switch by hand.
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import shutil
import subprocess
import sys
import zipfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np

# scipy is part of FreqAI's transitive dep set; we only use it for the
# bootstrap critical-value calc and fall back to a numpy-only impl if it
# isn't importable in the runtime env.
try:
    from scipy import stats as _scipy_stats  # noqa: F401
    _HAVE_SCIPY = True
except ImportError:
    _HAVE_SCIPY = False


# ---------------------------------------------------------------------------
# Constants — change here, NOT in the gate functions.
# ---------------------------------------------------------------------------

GATE_MIN_TRADES = 30
GATE_WALK_FORWARD_MAX_VARIANCE = 0.15      # stddev(winrate) / mean(winrate)
GATE_MC_P_VALUE = 0.05
GATE_MIN_SHARPE = 1.0
GATE_MIN_PROFIT_FACTOR = 1.5

DEFAULT_BOOTSTRAP_ITERS = 1000
DEFAULT_WALK_FORWARD_WINDOWS = 6

# Trading-day annualisation factor for crypto (24/7 market).
# FreqAI strategies trade 5m bars but Sharpe is computed on per-trade returns,
# so we annualise by trades-per-year. We approximate trades-per-year from the
# observed timerange so the number is scale-invariant in the trade-count sense.
SECONDS_PER_YEAR = 365.25 * 24 * 3600

LOGGER = logging.getLogger("backtest_with_gates")


# ---------------------------------------------------------------------------
# 1. Result loading
# ---------------------------------------------------------------------------


def _find_latest_freqtrade_result(results_dir: Path) -> Path | None:
    """Find the freshest backtest-result-*.zip in results_dir.

    Freqtrade writes a `.last_result.json` pointer file on success but it
    isn't always present (older versions, partial runs). Falling back to the
    newest zip by mtime keeps us robust.
    """
    last_pointer = results_dir / ".last_result.json"
    if last_pointer.is_file():
        try:
            payload = json.loads(last_pointer.read_text())
            target = payload.get("latest_backtest")
            if target:
                p = results_dir / target
                if p.is_file():
                    return p
        except Exception:  # noqa: BLE001 — pointer is best-effort
            LOGGER.warning("Could not parse %s, falling back to mtime", last_pointer)
    zips = sorted(results_dir.glob("backtest-result-*.zip"),
                  key=lambda p: p.stat().st_mtime, reverse=True)
    return zips[0] if zips else None


def load_backtest_result(path: Path) -> dict[str, Any]:
    """Load a freqtrade backtest result. Accepts a .zip or a .json.

    Freqtrade zips contain one JSON file with the same stem; we extract it
    in-memory rather than dropping a temp file.
    """
    if path.suffix == ".zip":
        with zipfile.ZipFile(path) as zf:
            # The strategy json is the largest member (meta is tiny).
            members = [n for n in zf.namelist() if n.endswith(".json")]
            if not members:
                raise ValueError(f"no .json inside {path}")
            # Prefer the file whose name matches the zip stem; else largest.
            stem = path.stem
            best = next((m for m in members if Path(m).stem == stem), None)
            if best is None:
                best = max(members, key=lambda m: zf.getinfo(m).file_size)
            with zf.open(best) as fh:
                return json.load(fh)
    if path.suffix == ".json":
        return json.loads(path.read_text())
    raise ValueError(f"unrecognised result file: {path}")


def extract_strategy_block(result: dict[str, Any], strategy: str) -> dict[str, Any]:
    """Pull the per-strategy block out of a freqtrade result envelope.

    Freqtrade wraps results as ``{"strategy": {<name>: {...}}, "strategy_comparison": [...]}``.
    """
    strategies = result.get("strategy") or {}
    if strategy in strategies:
        return strategies[strategy]
    # Fallback: if there's only one strategy in the result, use it.
    if len(strategies) == 1:
        only = next(iter(strategies.values()))
        LOGGER.info("strategy=%r not found; falling back to the only block present", strategy)
        return only
    raise KeyError(f"strategy {strategy!r} not found in result (have {list(strategies)})")


# ---------------------------------------------------------------------------
# 2. Per-trade extraction
# ---------------------------------------------------------------------------


def extract_trade_pnls(strategy_block: dict[str, Any]) -> list[float]:
    """Return per-trade absolute P&Ls (USD) from a strategy block.

    Freqtrade uses ``profit_abs`` per trade; older versions used ``profit_pct``
    only — we accept either.
    """
    trades = strategy_block.get("trades") or []
    pnls: list[float] = []
    for t in trades:
        if "profit_abs" in t and t["profit_abs"] is not None:
            try:
                pnls.append(float(t["profit_abs"]))
                continue
            except (TypeError, ValueError):
                pass
        # Fallback to profit_ratio × stake_amount if absolute is missing.
        ratio = t.get("profit_ratio")
        stake = t.get("stake_amount")
        if ratio is not None and stake is not None:
            try:
                pnls.append(float(ratio) * float(stake))
            except (TypeError, ValueError):
                pass
    return pnls


def extract_trade_open_dates(strategy_block: dict[str, Any]) -> list[datetime]:
    """Per-trade open timestamps (UTC). Used by walk-forward windowing."""
    trades = strategy_block.get("trades") or []
    out: list[datetime] = []
    for t in trades:
        ts = t.get("open_date") or t.get("open_timestamp")
        if ts is None:
            continue
        if isinstance(ts, (int, float)):
            # freqtrade open_timestamp is ms-since-epoch
            out.append(datetime.fromtimestamp(ts / 1000.0, tz=UTC))
        else:
            try:
                # ISO8601, may have trailing Z
                out.append(datetime.fromisoformat(str(ts).replace("Z", "+00:00")))
            except ValueError:
                continue
    return out


# ---------------------------------------------------------------------------
# 3. Statistics — pure numpy/scipy, deterministic given a seed
# ---------------------------------------------------------------------------


def compute_sharpe(pnls: list[float], n_trades_per_year: float | None = None) -> float:
    """Annualised Sharpe on per-trade returns.

    Scale-invariant: multiplying every trade P&L by a positive constant
    leaves the Sharpe unchanged (mean and stddev scale by the same factor).
    """
    if len(pnls) < 2:
        return 0.0
    arr = np.asarray(pnls, dtype=float)
    mu = float(arr.mean())
    sd = float(arr.std(ddof=1))
    if sd <= 0:
        return 0.0
    base = mu / sd
    factor = math.sqrt(n_trades_per_year) if n_trades_per_year and n_trades_per_year > 0 else 1.0
    return base * factor


def compute_profit_factor(pnls: list[float]) -> float:
    """Gross win / gross loss. ``inf`` if no losses; 0 if no wins."""
    gross_win = sum(p for p in pnls if p > 0)
    gross_loss = -sum(p for p in pnls if p < 0)
    if gross_loss <= 0:
        return float("inf") if gross_win > 0 else 0.0
    return gross_win / gross_loss


def walk_forward_winrates(
    pnls: list[float],
    open_dates: list[datetime],
    n_windows: int = DEFAULT_WALK_FORWARD_WINDOWS,
) -> tuple[list[float], dict[str, Any]]:
    """Split trades into N equal-time windows; compute per-window win-rate.

    Returns (winrates, diagnostics). Empty windows are *skipped* (not zero-
    filled) so a strategy that simply doesn't trade in some month isn't
    falsely penalised — but we record `windows_with_trades` so the gate can
    fail cleanly if too few windows produced any data.
    """
    diagnostics: dict[str, Any] = {
        "n_windows_requested": n_windows,
        "n_windows_with_trades": 0,
        "trades_per_window": [],
        "winrates": [],
    }
    if not pnls or not open_dates or len(pnls) != len(open_dates):
        return [], diagnostics

    pairs = sorted(zip(open_dates, pnls), key=lambda x: x[0])
    start, end = pairs[0][0], pairs[-1][0]
    span = (end - start).total_seconds()
    if span <= 0:
        # All trades on one timestamp — single bucket is the only honest answer.
        wins = sum(1 for _, p in pairs if p > 0)
        wr = wins / len(pairs)
        diagnostics["n_windows_with_trades"] = 1
        diagnostics["trades_per_window"] = [len(pairs)]
        diagnostics["winrates"] = [wr]
        return [wr], diagnostics

    bucket_pnls: list[list[float]] = [[] for _ in range(n_windows)]
    for d, p in pairs:
        frac = (d - start).total_seconds() / span
        idx = min(int(frac * n_windows), n_windows - 1)
        bucket_pnls[idx].append(p)

    winrates: list[float] = []
    per_window: list[int] = []
    for bucket in bucket_pnls:
        per_window.append(len(bucket))
        if not bucket:
            continue
        wins = sum(1 for p in bucket if p > 0)
        winrates.append(wins / len(bucket))

    diagnostics["n_windows_with_trades"] = len(winrates)
    diagnostics["trades_per_window"] = per_window
    diagnostics["winrates"] = winrates
    return winrates, diagnostics


def winrate_variance_ratio(winrates: list[float]) -> float | None:
    """stddev / mean of the per-window winrates. None if not computable."""
    if len(winrates) < 2:
        return None
    arr = np.asarray(winrates, dtype=float)
    mu = float(arr.mean())
    if mu <= 0:
        # Pathological: every window had a 0% winrate. Treat as infinitely
        # high variance ratio so the gate fails.
        return float("inf")
    sd = float(arr.std(ddof=1))
    return sd / mu


def monte_carlo_p_value(
    pnls: list[float],
    iterations: int = DEFAULT_BOOTSTRAP_ITERS,
    seed: int = 7,
) -> tuple[float | None, dict[str, Any]]:
    """Bootstrap p-value: P(bootstrap_mean >= 0 | sample) under H0 mean=0.

    Method: standard one-sided bootstrap test.

      1. Compute observed mean ``m_obs``.
      2. Centre the sample: subtract m_obs (so the resampling distribution
         has zero mean — that's our null).
      3. Resample with replacement; compute mean of each resample.
      4. p = fraction of resample-means whose absolute value is >= |m_obs|
         (two-sided), then halved for one-sided (we care about "beats zero").

    If the strategy has too few trades (n < 5), we can't bootstrap meaningfully
    and return None — the calling gate fails cleanly with a "n/a" detail.
    """
    diagnostics: dict[str, Any] = {
        "iterations": iterations,
        "seed": seed,
        "n_trades_used": len(pnls),
        "observed_mean": None,
    }
    if len(pnls) < 5:
        return None, diagnostics

    rng = np.random.default_rng(seed)
    arr = np.asarray(pnls, dtype=float)
    m_obs = float(arr.mean())
    diagnostics["observed_mean"] = m_obs
    if m_obs == 0:
        return 1.0, diagnostics  # observed effect is exactly zero

    centred = arr - m_obs
    n = len(arr)
    # Resample (iterations × n) at once using vectorised numpy — fast enough
    # at 1000 × 1000 trades (~10ms on a laptop).
    idx = rng.integers(0, n, size=(iterations, n))
    resample_means = centred[idx].mean(axis=1)
    # one-sided test: how often do we see a mean as extreme (in the
    # observed direction) as m_obs under H0?
    if m_obs > 0:
        p = float((resample_means >= m_obs).mean())
    else:
        p = float((resample_means <= m_obs).mean())
    return p, diagnostics


# ---------------------------------------------------------------------------
# 4. Gate evaluation
# ---------------------------------------------------------------------------


def _gate_result(name: str, passed: bool, value: Any, threshold: Any, detail: str) -> dict[str, Any]:
    return {
        "gate": name,
        "pass": bool(passed),
        "value": _json_safe(value),
        "threshold": _json_safe(threshold),
        "detail": detail,
    }


def _json_safe(v: Any) -> Any:
    if v is None:
        return None
    if isinstance(v, float):
        if math.isinf(v):
            return "inf" if v > 0 else "-inf"
        if math.isnan(v):
            return "nan"
        return v
    if isinstance(v, (np.floating, np.integer)):
        return _json_safe(float(v))
    return v


def evaluate_gates(
    strategy_block: dict[str, Any],
    bootstrap_iters: int = DEFAULT_BOOTSTRAP_ITERS,
    walk_forward_windows: int = DEFAULT_WALK_FORWARD_WINDOWS,
    seed: int = 7,
) -> dict[str, Any]:
    """Run all 5 gates against a single strategy block. Returns the report."""
    pnls = extract_trade_pnls(strategy_block)
    open_dates = extract_trade_open_dates(strategy_block)
    n = len(pnls)

    # --- Annualisation factor for Sharpe ---
    # Use observed trades-per-year over the timerange covered by the trades.
    trades_per_year: float | None = None
    if open_dates and len(open_dates) >= 2:
        sorted_dates = sorted(open_dates)
        span_s = (sorted_dates[-1] - sorted_dates[0]).total_seconds()
        if span_s > 0:
            trades_per_year = n * (SECONDS_PER_YEAR / span_s)

    # --- Gate 1 ---
    g_trades = _gate_result(
        "min_trades",
        n >= GATE_MIN_TRADES,
        n,
        GATE_MIN_TRADES,
        f"observed {n} trades, need >= {GATE_MIN_TRADES}",
    )

    # --- Gate 2 ---
    winrates, wf_diag = walk_forward_winrates(pnls, open_dates, walk_forward_windows)
    var_ratio = winrate_variance_ratio(winrates)
    if var_ratio is None:
        g_walk = _gate_result(
            "walk_forward_variance",
            False,
            None,
            GATE_WALK_FORWARD_MAX_VARIANCE,
            "n/a — fewer than 2 windows had trades",
        )
    else:
        passed = var_ratio < GATE_WALK_FORWARD_MAX_VARIANCE and not math.isinf(var_ratio)
        g_walk = _gate_result(
            "walk_forward_variance",
            passed,
            var_ratio,
            GATE_WALK_FORWARD_MAX_VARIANCE,
            f"stddev/mean of {wf_diag['n_windows_with_trades']} windows = "
            f"{var_ratio:.4f}, need < {GATE_WALK_FORWARD_MAX_VARIANCE}",
        )
    g_walk["windows"] = wf_diag

    # --- Gate 3 ---
    p_value, mc_diag = monte_carlo_p_value(pnls, iterations=bootstrap_iters, seed=seed)
    if p_value is None:
        g_mc = _gate_result(
            "monte_carlo_p_value",
            False,
            None,
            GATE_MC_P_VALUE,
            f"n/a — need >= 5 trades, have {n}",
        )
    else:
        g_mc = _gate_result(
            "monte_carlo_p_value",
            p_value < GATE_MC_P_VALUE,
            p_value,
            GATE_MC_P_VALUE,
            f"bootstrap p={p_value:.4f} over {bootstrap_iters} iters, need < {GATE_MC_P_VALUE}",
        )
    g_mc["bootstrap_diag"] = mc_diag

    # --- Gate 4 ---
    sharpe = compute_sharpe(pnls, n_trades_per_year=trades_per_year)
    if trades_per_year:
        sharpe_detail = f"annualised sharpe {sharpe:.4f} (trades/yr ≈ {trades_per_year:.0f})"
    else:
        sharpe_detail = f"per-trade sharpe {sharpe:.4f} (insufficient timerange to annualise)"
    g_sharpe = _gate_result(
        "sharpe",
        sharpe > GATE_MIN_SHARPE,
        sharpe,
        GATE_MIN_SHARPE,
        sharpe_detail,
    )

    # --- Gate 5 ---
    pf = compute_profit_factor(pnls)
    g_pf = _gate_result(
        "profit_factor",
        pf > GATE_MIN_PROFIT_FACTOR and not math.isnan(pf),
        pf,
        GATE_MIN_PROFIT_FACTOR,
        f"gross_win / gross_loss = {pf}" if math.isinf(pf) else f"gross_win / gross_loss = {pf:.4f}",
    )

    gates = [g_trades, g_walk, g_mc, g_sharpe, g_pf]
    promotion_eligible = all(g["pass"] for g in gates)

    return {
        "strategy": None,  # filled in by caller
        "evaluated_at": datetime.now(UTC).isoformat(),
        "n_trades": n,
        "trades_per_year_estimate": _json_safe(trades_per_year),
        "gates": gates,
        "promotion_eligible": promotion_eligible,
        "thresholds": {
            "min_trades": GATE_MIN_TRADES,
            "walk_forward_max_variance": GATE_WALK_FORWARD_MAX_VARIANCE,
            "monte_carlo_p_value": GATE_MC_P_VALUE,
            "min_sharpe": GATE_MIN_SHARPE,
            "min_profit_factor": GATE_MIN_PROFIT_FACTOR,
        },
        "config": {
            "bootstrap_iters": bootstrap_iters,
            "walk_forward_windows": walk_forward_windows,
            "seed": seed,
            "scipy_available": _HAVE_SCIPY,
        },
    }


# ---------------------------------------------------------------------------
# 5. Backtest invocation
# ---------------------------------------------------------------------------


def run_freqtrade_backtest(
    strategy: str,
    timerange: str,
    config: Path,
    extra_args: list[str] | None = None,
    cwd: Path | None = None,
    timeout_s: int = 60 * 60,
) -> int:
    """Invoke `freqtrade backtesting` as a subprocess. Returns the exit code.

    We deliberately don't capture stdout/stderr — freqtrade's progress bars
    are useful in cron logs and we want the operator to be able to tail the
    log file.
    """
    cmd = [
        "freqtrade", "backtesting",
        "--config", str(config),
        "--strategy", strategy,
        "--timerange", timerange,
        "--export", "trades",
        "--export-filename", "user_data/backtest_results/.last_result.json",
    ]
    if extra_args:
        cmd.extend(extra_args)
    LOGGER.info("Running: %s (cwd=%s)", " ".join(cmd), cwd)
    proc = subprocess.run(cmd, cwd=cwd, timeout=timeout_s, check=False)
    return proc.returncode


# ---------------------------------------------------------------------------
# 6. CLI
# ---------------------------------------------------------------------------


def write_report(report: dict[str, Any], results_dir: Path, strategy: str) -> tuple[Path, Path]:
    """Drop a timestamped copy + a stable *_latest.json pointer."""
    results_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    timestamped = results_dir / f"gates_report_{strategy}_{ts}.json"
    latest = results_dir / f"gates_report_{strategy}_latest.json"
    timestamped.write_text(json.dumps(report, indent=2, default=str))
    # Use copy instead of write so a Slack-side `tail -f` keeps its inode.
    shutil.copyfile(timestamped, latest)
    return timestamped, latest


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--strategy", required=True, help="Strategy class name (e.g. FreqAIMeanRevV1)")
    p.add_argument("--timerange", default="20240501-20260501",
                   help="Freqtrade timerange (default: 2-year window)")
    p.add_argument("--config", default="user_data/config.json",
                   help="Path to freqtrade config.json")
    p.add_argument("--results-dir", default="user_data/backtest_results",
                   help="Where backtest result zips and gate reports live")
    p.add_argument("--result-json", default=None,
                   help="Skip the freqtrade subprocess; load this result file instead")
    p.add_argument("--bootstrap-iters", type=int, default=DEFAULT_BOOTSTRAP_ITERS)
    p.add_argument("--walk-forward-windows", type=int, default=DEFAULT_WALK_FORWARD_WINDOWS)
    p.add_argument("--seed", type=int, default=7)
    p.add_argument("--cwd", default=None, help="Working directory for the freqtrade subprocess")
    p.add_argument("--quiet", action="store_true")
    args = p.parse_args(argv)

    logging.basicConfig(
        level=logging.WARNING if args.quiet else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    results_dir = Path(args.results_dir).resolve()
    results_dir.mkdir(parents=True, exist_ok=True)

    # 1. Acquire a result file
    if args.result_json:
        result_path = Path(args.result_json).resolve()
        if not result_path.is_file():
            LOGGER.error("--result-json does not exist: %s", result_path)
            return 2
    else:
        cwd = Path(args.cwd).resolve() if args.cwd else None
        rc = run_freqtrade_backtest(
            args.strategy, args.timerange, Path(args.config).resolve(),
            cwd=cwd,
        )
        if rc != 0:
            LOGGER.error("freqtrade backtesting exited with status %d", rc)
            return 2
        result_path = _find_latest_freqtrade_result(results_dir)
        if not result_path:
            LOGGER.error("no backtest result file found in %s", results_dir)
            return 2

    LOGGER.info("Loading result from %s", result_path)
    try:
        result = load_backtest_result(result_path)
        block = extract_strategy_block(result, args.strategy)
    except Exception as exc:  # noqa: BLE001
        LOGGER.error("Could not load result: %s", exc)
        return 2

    # 2. Evaluate gates
    report = evaluate_gates(
        block,
        bootstrap_iters=args.bootstrap_iters,
        walk_forward_windows=args.walk_forward_windows,
        seed=args.seed,
    )
    report["strategy"] = args.strategy
    report["timerange"] = args.timerange
    report["result_source"] = str(result_path)

    # 3. Persist
    timestamped, latest = write_report(report, results_dir, args.strategy)
    LOGGER.info("Report: %s", timestamped)
    LOGGER.info("Latest: %s", latest)

    # 4. Console summary
    if not args.quiet:
        print(json.dumps(_summary(report), indent=2, default=str))

    return 0 if report["promotion_eligible"] else 1


def _summary(report: dict[str, Any]) -> dict[str, Any]:
    return {
        "strategy": report["strategy"],
        "n_trades": report["n_trades"],
        "promotion_eligible": report["promotion_eligible"],
        "gates": [
            {"gate": g["gate"], "pass": g["pass"], "value": g["value"], "threshold": g["threshold"]}
            for g in report["gates"]
        ],
    }


if __name__ == "__main__":
    sys.exit(main())
