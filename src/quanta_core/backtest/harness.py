"""harness.py — V4 backtest harness with 5-gate quality reporter.

Replays MeanRevBB and TrendFollow over the 12 paper-mode crypto pairs
using the existing FeatherCandleSource (user_data/data/coinbase/*.feather).
Falls back to Coinbase public REST if a feather file is missing, caching
results to user_data/backtest_results/_ohlcv_cache/.

Output matches the schema the dashboard endpoint /api/ops/backtest_gates
reads (gates_report_<strategy>_<timestamp>.json + gates_report_<strategy>_latest.json).

Gate thresholds and statistical helpers are imported verbatim from the
dead scripts/backtest_with_gates.py predecessor — those are the canonical
numbers.

Usage
-----
    python -m quanta_core.backtest.harness --strategy mean_rev_bb --days 90
    python -m quanta_core.backtest.harness --strategy trend_follow --days 90
    python -m quanta_core.backtest.harness --all --days 90

Cron (installed Sunday 04:00 ET)
---------------------------------
    0 4 * * 0 flock -n /tmp/backtest_harness.lock \\
        /path/to/python3 -m quanta_core.backtest.harness --all \\
        >> /path/to/logs/cron-backtest-gates.log 2>&1
"""

from __future__ import annotations

import argparse
import ast
import json
import logging
import math
import shutil
import sys
import time
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import numpy as np

# ---------------------------------------------------------------------------
# Path constants
# ---------------------------------------------------------------------------

_REPO_ROOT = Path(__file__).resolve().parents[3]  # trading-bot/
_DATA_DIR = _REPO_ROOT / "user_data" / "data" / "coinbase"
_RESULTS_DIR = _REPO_ROOT / "user_data" / "backtest_results"
_CACHE_DIR = _RESULTS_DIR / "_ohlcv_cache"
_LOG_DIR = _REPO_ROOT / "user_data" / "logs"

# ---------------------------------------------------------------------------
# Paper-mode pairs (12 crypto USD pairs)
# ---------------------------------------------------------------------------

PAPER_PAIRS: list[str] = [
    "BTC_USD",
    "ETH_USD",
    "SOL_USD",
    "ADA_USD",
    "XRP_USD",
    "DOGE_USD",
    "AVAX_USD",
    "LINK_USD",
    "DOT_USD",
    "ATOM_USD",
    "LTC_USD",
    "BCH_USD",
]

# Coinbase REST uses "BTC-USD" product IDs.
_COINBASE_REST_BASE = "https://api.exchange.coinbase.com/products"
_GRANULARITY_5M = 300  # seconds
_MAX_BARS_PER_REQUEST = 300  # Coinbase hard cap

# ---------------------------------------------------------------------------
# Fee model
# Coinbase taker = 0.60% one-way for retail; round-trip = 0.30% = 30 bps.
# BacktestEngine.fee_bps is applied per side: 15 bps × 2 sides = 30 bps RT.
# ---------------------------------------------------------------------------

_FEE_BPS_PER_SIDE = Decimal("15")  # 0.15% per side = 0.30% round-trip

# Large starting equity so max_drawdown never exceeds 100% (BacktestEngine
# has a pydantic validator max_drawdown_pct <= 1.0; hitting it on a bad run
# would crash the harness rather than surface a gate failure).
_STARTING_EQUITY = Decimal("1000000")

# ---------------------------------------------------------------------------
# Gate constants — verbatim from scripts/backtest_with_gates.py
# ---------------------------------------------------------------------------

GATE_MIN_TRADES = 30
GATE_WALK_FORWARD_MAX_VARIANCE = 0.15
GATE_MC_P_VALUE = 0.05
GATE_MIN_SHARPE = 1.0
GATE_MIN_PROFIT_FACTOR = 1.5

DEFAULT_BOOTSTRAP_ITERS = 1000
DEFAULT_WALK_FORWARD_WINDOWS = 6
SECONDS_PER_YEAR = 365.25 * 24 * 3600

# ---------------------------------------------------------------------------
# scipy optional
# ---------------------------------------------------------------------------

try:
    from scipy import stats as _scipy_stats  # noqa: F401
    _HAVE_SCIPY = True
except ImportError:
    _HAVE_SCIPY = False

LOGGER = logging.getLogger("backtest.harness")


# ---------------------------------------------------------------------------
# Gate math — copied verbatim from scripts/backtest_with_gates.py
# (all five functions: compute_sharpe, compute_profit_factor,
# walk_forward_winrates, winrate_variance_ratio, monte_carlo_p_value)
# ---------------------------------------------------------------------------


def _json_safe(v: Any) -> Any:
    """Make a value safe for json.dumps."""
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


def _gate_result(name: str, passed: bool, value: Any, threshold: Any, detail: str) -> dict[str, Any]:
    return {
        "gate": name,
        "pass": bool(passed),
        "value": _json_safe(value),
        "threshold": _json_safe(threshold),
        "detail": detail,
    }


def compute_sharpe(pnls: list[float], n_trades_per_year: float | None = None) -> float:
    """Annualised Sharpe on per-trade returns (from backtest_with_gates.py)."""
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
    """Gross win / gross loss (from backtest_with_gates.py)."""
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
    """Split trades into N equal-time windows (from backtest_with_gates.py)."""
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
    """stddev / mean of the per-window winrates (from backtest_with_gates.py)."""
    if len(winrates) < 2:
        return None
    arr = np.asarray(winrates, dtype=float)
    mu = float(arr.mean())
    if mu <= 0:
        return float("inf")
    sd = float(arr.std(ddof=1))
    return sd / mu


def monte_carlo_p_value(
    pnls: list[float],
    iterations: int = DEFAULT_BOOTSTRAP_ITERS,
    seed: int = 7,
) -> tuple[float | None, dict[str, Any]]:
    """Bootstrap p-value (from backtest_with_gates.py)."""
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
        return 1.0, diagnostics

    centred = arr - m_obs
    n = len(arr)
    idx = rng.integers(0, n, size=(iterations, n))
    resample_means = centred[idx].mean(axis=1)
    if m_obs > 0:
        p = float((resample_means >= m_obs).mean())
    else:
        p = float((resample_means <= m_obs).mean())
    return p, diagnostics


def evaluate_gates(
    pnls: list[float],
    open_dates: list[datetime],
    bootstrap_iters: int = DEFAULT_BOOTSTRAP_ITERS,
    walk_forward_windows: int = DEFAULT_WALK_FORWARD_WINDOWS,
    seed: int = 7,
) -> dict[str, Any]:
    """Run all 5 gates. Adapted from scripts/backtest_with_gates.py.

    Unlike the original, this accepts pre-extracted pnls + open_dates
    directly (the harness builds them from TradeRecord objects).
    """
    n = len(pnls)

    trades_per_year: float | None = None
    if open_dates and len(open_dates) >= 2:
        sorted_dates = sorted(open_dates)
        span_s = (sorted_dates[-1] - sorted_dates[0]).total_seconds()
        if span_s > 0:
            trades_per_year = n * (SECONDS_PER_YEAR / span_s)

    # Gate 1 — minimum trade count
    g_trades = _gate_result(
        "min_trades",
        n >= GATE_MIN_TRADES,
        n,
        GATE_MIN_TRADES,
        f"observed {n} trades, need >= {GATE_MIN_TRADES}",
    )

    # Gate 2 — walk-forward win-rate variance
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

    # Gate 3 — Monte Carlo p-value
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

    # Gate 4 — Sharpe > 1.0
    sharpe = compute_sharpe(pnls, n_trades_per_year=trades_per_year)
    if trades_per_year:
        sharpe_detail = f"annualised sharpe {sharpe:.4f} (trades/yr approx {trades_per_year:.0f})"
    else:
        sharpe_detail = f"per-trade sharpe {sharpe:.4f} (insufficient timerange to annualise)"
    g_sharpe = _gate_result(
        "sharpe",
        sharpe > GATE_MIN_SHARPE,
        sharpe,
        GATE_MIN_SHARPE,
        sharpe_detail,
    )

    # Gate 5 — profit factor > 1.5
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
# OHLCV acquisition
# ---------------------------------------------------------------------------


def _feather_path(pair: str) -> Path:
    """Return the path to the feather file for a pair (e.g. BTC_USD-5m.feather)."""
    return _DATA_DIR / f"{pair}-5m.feather"


def _fetch_from_coinbase_rest(
    pair: str,
    start_dt: datetime,
    end_dt: datetime,
) -> list[dict[str, Any]]:
    """Fetch 5m OHLCV bars from Coinbase REST, chunked into 300-bar slices.

    Returns a list of raw bar dicts with keys:
        timestamp_utc, open, high, low, close, volume
    sorted chronologically.

    Rate limit: Coinbase allows 5 req/s. We sleep 0.25s between requests.
    """
    import urllib.request

    product_id = pair.replace("_", "-")  # BTC_USD -> BTC-USD
    url_base = f"{_COINBASE_REST_BASE}/{product_id}/candles"
    chunk_seconds = _MAX_BARS_PER_REQUEST * _GRANULARITY_5M  # 25 hours

    all_bars: list[dict[str, Any]] = []
    chunk_start = start_dt

    while chunk_start < end_dt:
        chunk_end = min(chunk_start + timedelta(seconds=chunk_seconds), end_dt)
        params = (
            f"?granularity={_GRANULARITY_5M}"
            f"&start={chunk_start.strftime('%Y-%m-%dT%H:%M:%SZ')}"
            f"&end={chunk_end.strftime('%Y-%m-%dT%H:%M:%SZ')}"
        )
        url = url_base + params
        try:
            with urllib.request.urlopen(url, timeout=30) as resp:
                raw = json.loads(resp.read().decode())
        except Exception as exc:  # noqa: BLE001
            LOGGER.warning("Coinbase REST error for %s %s-%s: %s", pair, chunk_start, chunk_end, exc)
            chunk_start = chunk_end
            time.sleep(0.25)
            continue

        # Coinbase returns [[time, low, high, open, close, volume], ...]
        # in reverse chronological order.
        for row in raw:
            if len(row) < 6:
                continue
            ts, low, high, open_, close, volume = row[0], row[1], row[2], row[3], row[4], row[5]
            all_bars.append({
                "timestamp_utc": datetime.fromtimestamp(ts, tz=UTC),
                "open": float(open_),
                "high": float(high),
                "low": float(low),
                "close": float(close),
                "volume": float(volume),
            })
        time.sleep(0.25)
        chunk_start = chunk_end

    # Sort chronologically and deduplicate.
    all_bars.sort(key=lambda b: b["timestamp_utc"])
    seen: set[datetime] = set()
    deduped: list[dict[str, Any]] = []
    for b in all_bars:
        if b["timestamp_utc"] not in seen:
            seen.add(b["timestamp_utc"])
            deduped.append(b)
    return deduped


def _bars_from_feather(pair: str, start_dt: datetime, end_dt: datetime) -> list[dict[str, Any]]:
    """Load bars from the feather file, filtered to [start_dt, end_dt)."""
    import pandas as pd

    path = _feather_path(pair)
    df = pd.read_feather(path)
    # Ensure UTC-aware timestamps.
    dates = pd.to_datetime(df["date"], utc=True)
    mask = (dates >= start_dt) & (dates < end_dt)
    df = df[mask].copy()
    df["date"] = pd.to_datetime(df["date"], utc=True)
    df = df.sort_values("date").drop_duplicates(subset=["date"], keep="last")

    bars: list[dict[str, Any]] = []
    for row in df.itertuples(index=False):
        bars.append({
            "timestamp_utc": row.date.to_pydatetime().astimezone(UTC),
            "open": float(row.open),
            "high": float(row.high),
            "low": float(row.low),
            "close": float(row.close),
            "volume": float(row.volume),
        })
    return bars


def acquire_bars(pair: str, start_dt: datetime, end_dt: datetime) -> list[dict[str, Any]]:
    """Return 5m OHLCV bars for `pair` in [start_dt, end_dt).

    Priority:
      (a) Feather file if present and covers the window.
      (b) Cached parquet under _CACHE_DIR.
      (c) Coinbase REST (writes to cache).
    """
    feather = _feather_path(pair)
    if feather.is_file():
        LOGGER.debug("Loading %s from feather %s", pair, feather)
        bars = _bars_from_feather(pair, start_dt, end_dt)
        if bars:
            LOGGER.info("%s: loaded %d bars from feather", pair, len(bars))
            return bars
        LOGGER.warning("%s: feather exists but returned 0 bars for window; falling back to REST", pair)

    # Try disk cache.
    start_tag = start_dt.strftime("%Y%m%d")
    end_tag = end_dt.strftime("%Y%m%d")
    cache_path = _CACHE_DIR / f"{pair}_5m_{start_tag}_{end_tag}.parquet"
    if cache_path.is_file():
        import pandas as pd
        LOGGER.info("%s: loading from cache %s", pair, cache_path)
        df = pd.read_parquet(cache_path)
        bars = df.to_dict("records")
        return bars

    # Fetch from REST and cache.
    LOGGER.info("%s: fetching from Coinbase REST %s to %s", pair, start_dt.date(), end_dt.date())
    bars = _fetch_from_coinbase_rest(pair, start_dt, end_dt)
    if bars:
        import pandas as pd
        _CACHE_DIR.mkdir(parents=True, exist_ok=True)
        df = pd.DataFrame(bars)
        df.to_parquet(cache_path, index=False)
        LOGGER.info("%s: cached %d bars to %s", pair, len(bars), cache_path)
    return bars


# ---------------------------------------------------------------------------
# Per-pair backtest runner
# ---------------------------------------------------------------------------


def _run_one_pair(
    pair: str,
    strategy_name: str,
    bars_dicts: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Run one strategy over one pair's bars. Returns list of trade records.

    Each record has:
        pair, entry_ts, entry_price, exit_ts, exit_price, side,
        pnl_usd, pnl_pct, exit_reason (extracted from rationale)
    """
    from decimal import Decimal as D

    from quanta_core.backtest.candle_source import InMemoryCandleSource
    from quanta_core.backtest.engine import BacktestConfig, BacktestEngine
    from quanta_core.types import Bar, Symbol

    if not bars_dicts:
        LOGGER.warning("%s/%s: no bars — skipping", pair, strategy_name)
        return []

    # Build Bar objects.
    bars: list[Bar] = []
    for b in bars_dicts:
        try:
            bar = Bar(
                symbol=Symbol(pair),
                open=D(str(b["open"])),
                high=D(str(b["high"])),
                low=D(str(b["low"])),
                close=D(str(b["close"])),
                volume=D(str(b["volume"])),
                timestamp_utc=b["timestamp_utc"],
                timeframe="5m",
            )
            bars.append(bar)
        except Exception as exc:  # noqa: BLE001
            LOGGER.debug("Skipping malformed bar for %s: %s", pair, exc)

    if len(bars) < 30:
        LOGGER.warning("%s/%s: only %d valid bars — skipping", pair, strategy_name, len(bars))
        return []

    src = InMemoryCandleSource(bars)
    cfg = BacktestConfig(
        symbol=Symbol(pair),
        timeframe="5m",
        starting_equity=_STARTING_EQUITY,
        fee_bps=_FEE_BPS_PER_SIDE,
    )

    # Regime config: give each strategy its most-permissive regime so gates
    # reflect honest signal quality rather than regime availability.
    # MeanRevBB entries require: trending_up or mean_reverting.
    # TrendFollow entries require: trending_up only.
    if strategy_name == "mean_rev_bb":
        from quanta_core.strategy.mean_rev_bb import MeanRevBB as StratClass
        regime = "mean_reverting"
    else:
        from quanta_core.strategy.trend_follow import TrendFollow as StratClass
        regime = "trending_up"

    strategy_config = {"state": {"regime": regime}}

    try:
        engine = BacktestEngine(
            strategy_class=StratClass,
            config=cfg,
            candle_source=src,
            strategy_config=strategy_config,
        )
        result = engine.run()
    except Exception as exc:  # noqa: BLE001
        LOGGER.error("Engine error for %s/%s: %s", pair, strategy_name, exc)
        return []

    trades: list[dict[str, Any]] = []
    for t in result.trades:
        entry_price_f = float(t.entry_price)
        pnl_pct = float(t.pnl) / (entry_price_f * float(t.qty)) if entry_price_f > 0 else 0.0
        # Extract exit_reason from rationale if this is a sell. The engine
        # records all closed legs; the exit_reason is baked into the rationale
        # of the SELL proposal. We approximate: stop_loss if pnl is negative
        # and entry was near the stop level. A more precise extraction would
        # require storing exit_reason on TradeRecord — that's a schema change
        # deferred to post-audit sprint.
        trades.append({
            "pair": pair,
            "entry_ts": t.entry_ts.isoformat(),
            "entry_price": float(t.entry_price),
            "exit_ts": t.exit_ts.isoformat(),
            "exit_price": float(t.exit_price),
            "side": t.side,
            "pnl_usd": float(t.pnl),
            "pnl_pct": pnl_pct,
            "exit_reason": "stop_loss" if float(t.pnl) < 0 and abs(pnl_pct) > 0.035 else "mean_reversion",
            "fee_total": float(t.fee_total),
            "bars_held": t.bars_held,
        })

    LOGGER.info(
        "%s/%s: %d trades, win_rate=%.3f, sharpe=%.3f",
        pair, strategy_name, result.summary.n_trades,
        result.summary.win_rate, result.summary.sharpe,
    )
    return trades


# ---------------------------------------------------------------------------
# Report writing
# ---------------------------------------------------------------------------


def write_report(report: dict[str, Any], strategy: str) -> tuple[Path, Path]:
    """Write timestamped + stable *_latest.json. Returns (timestamped, latest)."""
    _RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    timestamped = _RESULTS_DIR / f"gates_report_{strategy}_{ts}.json"
    latest = _RESULTS_DIR / f"gates_report_{strategy}_latest.json"
    text = json.dumps(report, indent=2, default=str)
    timestamped.write_text(text)
    shutil.copyfile(timestamped, latest)
    return timestamped, latest


# ---------------------------------------------------------------------------
# Main backtest runner
# ---------------------------------------------------------------------------


def run_strategy(
    strategy_name: str,
    days: int = 90,
    bootstrap_iters: int = DEFAULT_BOOTSTRAP_ITERS,
    walk_forward_windows: int = DEFAULT_WALK_FORWARD_WINDOWS,
    seed: int = 7,
) -> dict[str, Any]:
    """Run `strategy_name` over all 12 pairs for the last `days` days.

    Returns the full gates report dict (ready to pass to write_report).
    """
    end_dt = datetime.now(UTC).replace(second=0, microsecond=0)
    start_dt = end_dt - timedelta(days=days)

    LOGGER.info(
        "Running %s over %d pairs, window %s to %s",
        strategy_name, len(PAPER_PAIRS), start_dt.date(), end_dt.date(),
    )

    t0 = time.monotonic()
    all_trades: list[dict[str, Any]] = []
    pairs_ok: int = 0
    pairs_err: int = 0

    for pair in PAPER_PAIRS:
        try:
            bars = acquire_bars(pair, start_dt, end_dt)
        except Exception as exc:  # noqa: BLE001
            LOGGER.error("Failed to acquire bars for %s: %s", pair, exc)
            pairs_err += 1
            continue

        if not bars:
            LOGGER.warning("%s: 0 bars returned — skipping", pair)
            pairs_err += 1
            continue

        pair_trades = _run_one_pair(pair, strategy_name, bars)
        all_trades.extend(pair_trades)
        pairs_ok += 1

    elapsed = time.monotonic() - t0
    LOGGER.info(
        "%s: finished %d pairs (%d errors) in %.1fs — %d total trades",
        strategy_name, pairs_ok, pairs_err, elapsed, len(all_trades),
    )

    # Extract pnl_usd and open_dates for gate calculation.
    pnls = [t["pnl_usd"] for t in all_trades]
    open_dates = [datetime.fromisoformat(t["entry_ts"]) for t in all_trades]

    report = evaluate_gates(
        pnls,
        open_dates,
        bootstrap_iters=bootstrap_iters,
        walk_forward_windows=walk_forward_windows,
        seed=seed,
    )
    report["strategy"] = strategy_name
    report["timerange"] = f"{start_dt.date().isoformat()} to {end_dt.date().isoformat()}"
    report["days_requested"] = days
    report["pairs_evaluated"] = PAPER_PAIRS
    report["pairs_ok"] = pairs_ok
    report["pairs_err"] = pairs_err
    report["elapsed_seconds"] = round(elapsed, 1)
    report["fee_bps_per_side"] = float(_FEE_BPS_PER_SIDE)
    report["fee_pct_roundtrip"] = float(_FEE_BPS_PER_SIDE) * 2 / 100
    report["regime_injected"] = "mean_reverting" if strategy_name == "mean_rev_bb" else "trending_up"
    report["trades"] = all_trades  # full trade log for audit

    return report


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _print_summary(report: dict[str, Any]) -> None:
    """Print a human-readable gate summary to stdout."""
    gates = report.get("gates", [])
    eligible = report.get("promotion_eligible", False)
    n = report.get("n_trades", 0)
    strat = report.get("strategy", "?")
    print(f"\n=== {strat} | {n} trades | promotion_eligible={eligible} ===")
    for g in gates:
        status = "PASS" if g.get("pass") else "FAIL"
        val = g.get("value")
        thr = g.get("threshold")
        detail = g.get("detail", "")
        print(f"  [{status}] {g.get('gate')}: value={val} threshold={thr} — {detail}")
    print()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="V4 backtest harness — runs MeanRevBB/TrendFollow quality gates",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument(
        "--strategy",
        choices=["mean_rev_bb", "trend_follow"],
        help="Strategy to run",
    )
    ap.add_argument(
        "--all",
        action="store_true",
        help="Run both strategies",
    )
    ap.add_argument(
        "--days",
        type=int,
        default=90,
        help="Lookback window in days (default 90)",
    )
    ap.add_argument(
        "--bootstrap-iters",
        type=int,
        default=DEFAULT_BOOTSTRAP_ITERS,
    )
    ap.add_argument(
        "--walk-forward-windows",
        type=int,
        default=DEFAULT_WALK_FORWARD_WINDOWS,
    )
    ap.add_argument(
        "--seed",
        type=int,
        default=7,
    )
    ap.add_argument(
        "--quiet",
        action="store_true",
        help="Suppress per-gate console summary",
    )
    args = ap.parse_args(argv)

    if not args.strategy and not args.all:
        ap.error("Provide --strategy <name> or --all")

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-8s %(name)s %(message)s",
        stream=sys.stderr,
    )

    strategies_to_run: list[str] = []
    if args.all:
        strategies_to_run = ["mean_rev_bb", "trend_follow"]
    else:
        strategies_to_run = [args.strategy]

    overall_exit = 0
    for strat in strategies_to_run:
        LOGGER.info("=== Starting %s ===", strat)
        try:
            report = run_strategy(
                strat,
                days=args.days,
                bootstrap_iters=args.bootstrap_iters,
                walk_forward_windows=args.walk_forward_windows,
                seed=args.seed,
            )
        except Exception as exc:  # noqa: BLE001
            LOGGER.error("Fatal error running %s: %s", strat, exc, exc_info=True)
            overall_exit = 2
            continue

        timestamped, latest = write_report(report, strat)
        LOGGER.info("Report: %s", timestamped)
        LOGGER.info("Latest: %s", latest)

        if not args.quiet:
            _print_summary(report)

        if not report.get("promotion_eligible"):
            overall_exit = max(overall_exit, 1)

    return overall_exit


if __name__ == "__main__":
    sys.exit(main())
