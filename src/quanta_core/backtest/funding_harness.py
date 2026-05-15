"""funding_harness.py — backtest scaffold for delta-neutral funding-rate harvest.

Sister to ``backtest/harness.py``. Same gates, same JSON shape, so it lands
in the dashboard's Backtest Quality Gates card alongside the other strategies.

Data source (read-only, free, no auth): OKX
``GET https://www.okx.com/api/v5/public/funding-rate-history?instId=<SYM>-USDT-SWAP``.
OKX is the empirical proxy for what the operator's eventual venue (Bybit
or dYdX) experiences — funding rates across major perp venues correlate
> 0.97 (ScienceDirect 2025 §4.2) so the result generalises.

CLI
---
    python -m quanta_core.backtest.funding_harness --symbol BTC --days 30
    python -m quanta_core.backtest.funding_harness --all --days 90

NOT installed in cron — see design doc §9. Operator opens dYdX account in
Week 2-3, then we add a cron line and switch the data source to dYdX
indexer for live signal.

Design doc: ``audit/2026-05-15-funding-rate-design.md``.
"""
from __future__ import annotations

import argparse
import json
import logging
import math
import shutil
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Sequence

import numpy as np

from quanta_core.backtest.harness import (  # canonical gate math
    DEFAULT_BOOTSTRAP_ITERS,
    DEFAULT_WALK_FORWARD_WINDOWS,
    GATE_MC_P_VALUE,
    GATE_MIN_PROFIT_FACTOR,
    GATE_MIN_SHARPE,
    GATE_MIN_TRADES,
    GATE_WALK_FORWARD_MAX_VARIANCE,
    SECONDS_PER_YEAR,
    evaluate_gates,
)
from quanta_core.strategies.funding_rate_harvest import (
    ENTER_THRESHOLD,
    EXIT_THRESHOLD,
    FUNDING_PERIODS_PER_YEAR,
    FUNDING_PERIOD_HOURS,
    HARVEST_REGIMES,
    MIN_HOLD_PERIODS,
    ROUND_TRIP_FEE_BPS,
    TAKER_FEE_BPS_PER_LEG,
    FundingRateHarvest,
    FundingTick,
    HarvestTrade,
    simulate_harvest,
    synthetic_regimes_from_spot,
)

UTC = timezone.utc

# ---------------------------------------------------------------------------
# Path constants — mirror harness.py
# ---------------------------------------------------------------------------

_REPO_ROOT = Path(__file__).resolve().parents[3]
_DATA_DIR = _REPO_ROOT / "user_data" / "data" / "coinbase"
_RESULTS_DIR = _REPO_ROOT / "user_data" / "backtest_results"
_CACHE_DIR = _RESULTS_DIR / "_funding_cache"

# ---------------------------------------------------------------------------
# Symbol mapping. Operator's spot data uses BTC_USD; OKX perp uses
# BTC-USDT-SWAP. We backtest the perp's funding history paired with the
# operator's spot bars (OKX USDT and Coinbase USD diverge by < 0.05% after
# the USDC/USDT spread, which is well below our fee floor).
# ---------------------------------------------------------------------------

# top-5 by volume from the operator's existing 12-pair paper-mode set.
DEFAULT_SYMBOLS: list[str] = ["BTC", "ETH", "SOL", "XRP", "DOGE"]

# Per-cycle notional used for PnL accounting. 10k matches the operator's
# planned per-pair cap (5% of ~$200k portfolio = $10k). Constant per cycle
# so the gates math reflects strategy quality, not position sizing.
DEFAULT_NOTIONAL_USD = 10_000.0

OKX_FUNDING_URL = "https://www.okx.com/api/v5/public/funding-rate-history"
HTTP_TIMEOUT_S = 20
HTTP_RETRIES = 3
HTTP_BACKOFF_S = 1.5

LOGGER = logging.getLogger("backtest.funding_harness")


# ---------------------------------------------------------------------------
# OKX funding-rate fetcher (no auth, free)
# ---------------------------------------------------------------------------


_HTTP_HEADERS = {
    # OKX (and several CDNs) reject the default Python urllib UA. A neutral
    # browser-like UA fixes the 403 without misrepresenting intent — the
    # endpoint is public and rate-limited.
    "User-Agent": "Mozilla/5.0 (compatible; quanta-funding-harness/1.0)",
    "Accept": "application/json",
}


def _http_get_json(url: str, params: dict[str, Any]) -> dict[str, Any]:
    """GET ``url`` with retry. Returns parsed JSON or raises after retries."""
    qs = urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
    full = f"{url}?{qs}"
    last_exc: Exception | None = None
    for attempt in range(HTTP_RETRIES):
        try:
            req = urllib.request.Request(full, headers=_HTTP_HEADERS)
            with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_S) as resp:
                body = resp.read().decode("utf-8")
                return json.loads(body)
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            wait = HTTP_BACKOFF_S * (2 ** attempt)
            LOGGER.warning(
                "HTTP attempt %d/%d failed for %s: %s — sleeping %.1fs",
                attempt + 1, HTTP_RETRIES, full, exc, wait,
            )
            time.sleep(wait)
    raise RuntimeError(f"HTTP failure after {HTTP_RETRIES} attempts: {last_exc}")


def fetch_funding_history_okx(
    okx_symbol: str,
    days: int,
    *,
    page_limit: int = 100,
) -> list[FundingTick]:
    """Fetch ``days`` of funding-rate history for one OKX SWAP instrument.

    OKX paginates backward via the ``after`` parameter (= "older than this
    timestamp ms"). Each page is up to 100 entries × 8h = ~33 days. We keep
    paging until the oldest entry is before ``cutoff`` or the API stops
    returning data.

    Parameters
    ----------
    okx_symbol : e.g. "BTC-USDT-SWAP".
    days       : how far back to fetch.
    page_limit : OKX max is 100.

    Returns
    -------
    list[FundingTick] sorted chronologically (oldest first).
    """
    cutoff_dt = datetime.now(UTC) - timedelta(days=days)
    cutoff_ms = int(cutoff_dt.timestamp() * 1000)

    collected: dict[int, float] = {}
    after: int | None = None
    pages_fetched = 0
    MAX_PAGES = 30  # safety: 30 × 100 × 8h ≈ 1000 days

    while pages_fetched < MAX_PAGES:
        payload = _http_get_json(
            OKX_FUNDING_URL,
            {
                "instId": okx_symbol,
                "limit": page_limit,
                "after": after,
            },
        )
        if str(payload.get("code")) != "0":
            raise RuntimeError(f"OKX error for {okx_symbol}: {payload.get('msg')}")
        rows = payload.get("data") or []
        if not rows:
            break
        # Each row: {"fundingTime": "<ms>", "fundingRate": "<decimal>", ...}
        oldest_ms_this_page: int | None = None
        for row in rows:
            try:
                t_ms = int(row["fundingTime"])
                rate = float(row["fundingRate"])
            except (KeyError, TypeError, ValueError):
                continue
            collected[t_ms] = rate
            if oldest_ms_this_page is None or t_ms < oldest_ms_this_page:
                oldest_ms_this_page = t_ms
        pages_fetched += 1
        if oldest_ms_this_page is None:
            break
        if oldest_ms_this_page <= cutoff_ms:
            break
        after = oldest_ms_this_page
        # Be polite to OKX (public limit is 6 req/s per endpoint per IP).
        time.sleep(0.3)

    LOGGER.info(
        "OKX %s: fetched %d funding entries across %d page(s)",
        okx_symbol, len(collected), pages_fetched,
    )

    # Filter to the requested window and sort ascending.
    ticks: list[FundingTick] = []
    for t_ms in sorted(collected):
        if t_ms < cutoff_ms:
            continue
        dt = datetime.fromtimestamp(t_ms / 1000.0, tz=UTC)
        ticks.append(FundingTick(funding_time=dt, rate=collected[t_ms]))
    return ticks


def cached_funding_history(
    okx_symbol: str,
    days: int,
    *,
    cache: bool = True,
) -> list[FundingTick]:
    """Wrapper that caches the funding-history fetch to a JSON file under
    ``user_data/backtest_results/_funding_cache/`` (one file per symbol per
    day-bucket). Cache TTL: 6 hours (funding accrues every 8h, so a 6h cache
    is at most 1 period stale).
    """
    if not cache:
        return fetch_funding_history_okx(okx_symbol, days)
    _CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache_path = _CACHE_DIR / f"{okx_symbol}_{days}d.json"
    if cache_path.is_file():
        age_s = time.time() - cache_path.stat().st_mtime
        if age_s < 6 * 3600:
            try:
                rows = json.loads(cache_path.read_text())
                return [
                    FundingTick(
                        funding_time=datetime.fromisoformat(r["t"]),
                        rate=float(r["r"]),
                    )
                    for r in rows
                ]
            except Exception:  # noqa: BLE001 — fall through to refetch
                LOGGER.warning("Cache parse failed for %s, refetching", cache_path)
    ticks = fetch_funding_history_okx(okx_symbol, days)
    try:
        cache_path.write_text(json.dumps(
            [{"t": t.funding_time.isoformat(), "r": t.rate} for t in ticks]
        ))
    except Exception as exc:  # noqa: BLE001
        LOGGER.warning("Cache write failed for %s: %s", cache_path, exc)
    return ticks


# ---------------------------------------------------------------------------
# Spot-bar loader (re-uses operator's existing Coinbase feathers)
# ---------------------------------------------------------------------------


def load_spot_bars(symbol: str, days: int) -> list[tuple[datetime, float]]:
    """Load ``days`` of 1h spot bars from the operator's coinbase feathers.

    Returns a list of (timestamp_utc, close_price) tuples. Empty list if
    the feather is missing — the harness then falls back to a single-regime
    synthesis (everything is "trending_up") which biases the result optimistic.
    The harness logs this fallback prominently in the gate report.
    """
    pair_id = f"{symbol}_USD"
    candidates = [
        _DATA_DIR / f"{pair_id}-1h.feather",
        _DATA_DIR / f"{pair_id}-4h.feather",
    ]
    path = next((p for p in candidates if p.is_file()), None)
    if path is None:
        LOGGER.warning("No spot feather for %s — regime stub will be permissive", symbol)
        return []
    try:
        import pandas as pd
    except ImportError:
        LOGGER.warning("pandas missing — cannot load spot bars; regime stub permissive")
        return []
    df = pd.read_feather(path)
    df["date"] = pd.to_datetime(df["date"], utc=True)
    cutoff = datetime.now(UTC) - timedelta(days=days + 2)
    df = df[df["date"] >= cutoff].sort_values("date").drop_duplicates(subset=["date"])
    return [(row.date.to_pydatetime().astimezone(UTC), float(row.close))
            for row in df.itertuples(index=False)]


# ---------------------------------------------------------------------------
# Per-symbol runner
# ---------------------------------------------------------------------------


def _trades_to_dicts(trades: Sequence[HarvestTrade]) -> list[dict[str, Any]]:
    return [t.as_dict() for t in trades]


def run_symbol(
    symbol: str,
    days: int,
    *,
    notional_usd: float = DEFAULT_NOTIONAL_USD,
    fee_bps_per_leg: float = TAKER_FEE_BPS_PER_LEG,
    cache: bool = True,
    strategy: FundingRateHarvest | None = None,
) -> dict[str, Any]:
    """Backtest one symbol and return a per-symbol summary dict.

    Returned shape:
        {"symbol": ..., "trades": [...], "summary": {sharpe, pf, total_pnl, ...}}
    """
    okx_symbol = f"{symbol}-USDT-SWAP"
    ticks = cached_funding_history(okx_symbol, days, cache=cache)
    if not ticks:
        return {
            "symbol": symbol,
            "trades": [],
            "summary": {
                "n_trades": 0,
                "sharpe": 0.0,
                "profit_factor": 0.0,
                "total_pnl_usd": 0.0,
                "avg_pnl_pct_per_trade": 0.0,
                "avg_funding_rate": 0.0,
                "fund_periods": 0,
                "regime_breakdown": {},
                "data_source": "okx",
                "spot_data_available": False,
                "warning": "no funding ticks fetched",
            },
        }

    spot_bars = load_spot_bars(symbol, days)
    spot_available = bool(spot_bars)
    if spot_available:
        regimes = synthetic_regimes_from_spot(
            [t.funding_time for t in ticks],
            spot_bars,
        )
    else:
        # Permissive fallback (see load_spot_bars docstring).
        regimes = ["trending_up"] * len(ticks)

    strat = strategy or FundingRateHarvest()
    trades = simulate_harvest(
        symbol=symbol,
        ticks=ticks,
        regimes=regimes,
        notional_usd=notional_usd,
        strategy=strat,
        fee_bps_per_leg=fee_bps_per_leg,
    )

    # Per-symbol summary.
    pnls = [t.pnl_usd for t in trades]
    avg_funding = float(np.mean([t.avg_funding_rate for t in trades])) if trades else 0.0
    win_rate = float(sum(1 for p in pnls if p > 0) / len(pnls)) if pnls else 0.0

    # Annualised Sharpe at the per-trade level (gates math expects
    # trades_per_year — match harness.py convention).
    if trades and len(trades) >= 2:
        ts_seconds = (trades[-1].exit_time - trades[0].entry_time).total_seconds()
        trades_per_year = (
            len(trades) * SECONDS_PER_YEAR / ts_seconds if ts_seconds > 0 else None
        )
    else:
        trades_per_year = None

    arr = np.asarray(pnls, dtype=float)
    if len(arr) >= 2 and arr.std(ddof=1) > 0:
        sharpe_per_trade = float(arr.mean() / arr.std(ddof=1))
        sharpe_ann = sharpe_per_trade * math.sqrt(trades_per_year or 1.0)
    else:
        sharpe_ann = 0.0
    gross_win = sum(p for p in pnls if p > 0)
    gross_loss = -sum(p for p in pnls if p < 0)
    if gross_loss <= 0:
        profit_factor = float("inf") if gross_win > 0 else 0.0
    else:
        profit_factor = gross_win / gross_loss

    regime_breakdown: dict[str, int] = {}
    for r in regimes:
        regime_breakdown[r] = regime_breakdown.get(r, 0) + 1

    summary = {
        "n_trades": len(trades),
        "n_funding_periods": len(ticks),
        "sharpe": sharpe_ann,
        "profit_factor": profit_factor if math.isfinite(profit_factor) else "inf",
        "win_rate": win_rate,
        "total_pnl_usd": float(sum(pnls)),
        "total_pnl_pct_of_notional": float(sum(pnls) / notional_usd) if notional_usd > 0 else 0.0,
        "avg_pnl_pct_per_trade": float(np.mean([t.pnl_pct for t in trades])) if trades else 0.0,
        "avg_funding_rate": avg_funding,
        "implied_apy_pre_fee": float(avg_funding * FUNDING_PERIODS_PER_YEAR),
        "regime_breakdown_funding_periods": regime_breakdown,
        "data_source": "okx",
        "spot_data_available": spot_available,
        "trades_per_year_estimate": trades_per_year,
    }

    return {
        "symbol": symbol,
        "okx_symbol": okx_symbol,
        "trades": _trades_to_dicts(trades),
        "summary": summary,
    }


# ---------------------------------------------------------------------------
# Main runner — gates report
# ---------------------------------------------------------------------------


def run_strategy(
    symbols: Sequence[str],
    days: int,
    *,
    notional_usd: float = DEFAULT_NOTIONAL_USD,
    fee_bps_per_leg: float = TAKER_FEE_BPS_PER_LEG,
    bootstrap_iters: int = DEFAULT_BOOTSTRAP_ITERS,
    walk_forward_windows: int = DEFAULT_WALK_FORWARD_WINDOWS,
    seed: int = 7,
    cache: bool = True,
    enter_threshold: float = ENTER_THRESHOLD,
    exit_threshold: float = EXIT_THRESHOLD,
    min_hold_periods: int = MIN_HOLD_PERIODS,
) -> dict[str, Any]:
    """Run the harvest backtest across ``symbols`` for ``days`` of history.

    Produces the SAME schema as ``harness.run_strategy`` so the dashboard's
    /api/ops/backtest_gates endpoint can ingest it without changes.
    """
    LOGGER.info(
        "Funding-rate harvest backtest: %d symbols, %d days, notional=$%s, "
        "fee=%sbps/leg, enter=%.5f, exit=%.5f, min_hold=%d",
        len(symbols), days, notional_usd, fee_bps_per_leg,
        enter_threshold, exit_threshold, min_hold_periods,
    )
    t0 = time.monotonic()

    per_symbol: list[dict[str, Any]] = []
    all_trades: list[dict[str, Any]] = []
    symbols_ok = 0
    symbols_err = 0

    strat_override = FundingRateHarvest(
        enter_threshold=enter_threshold,
        exit_threshold=exit_threshold,
        min_hold_periods=min_hold_periods,
    )

    for sym in symbols:
        try:
            result = run_symbol(
                sym,
                days=days,
                notional_usd=notional_usd,
                fee_bps_per_leg=fee_bps_per_leg,
                cache=cache,
                strategy=strat_override,
            )
        except Exception as exc:  # noqa: BLE001
            LOGGER.error("Failed to backtest %s: %s", sym, exc, exc_info=True)
            symbols_err += 1
            per_symbol.append({"symbol": sym, "error": str(exc)})
            continue
        per_symbol.append(result)
        all_trades.extend(result.get("trades", []))
        if result.get("trades"):
            symbols_ok += 1
        else:
            symbols_err += 1
        LOGGER.info(
            "  %-5s n_trades=%d sharpe=%.3f pf=%s pnl=$%.2f",
            sym,
            result["summary"]["n_trades"],
            result["summary"]["sharpe"],
            result["summary"]["profit_factor"],
            result["summary"]["total_pnl_usd"],
        )

    elapsed = time.monotonic() - t0

    # Aggregate gates across all symbols' trades — same shape as harness.py.
    pnls = [t["pnl_usd"] for t in all_trades]
    open_dates: list[datetime] = []
    for t in all_trades:
        try:
            open_dates.append(datetime.fromisoformat(t["entry_ts"]))
        except Exception:  # noqa: BLE001
            pass

    report = evaluate_gates(
        pnls,
        open_dates,
        bootstrap_iters=bootstrap_iters,
        walk_forward_windows=walk_forward_windows,
        seed=seed,
    )
    report["strategy"] = "funding_rate_harvest"
    report["timerange"] = (
        f"{(datetime.now(UTC) - timedelta(days=days)).date().isoformat()} to "
        f"{datetime.now(UTC).date().isoformat()}"
    )
    report["days_requested"] = days
    report["pairs_evaluated"] = list(symbols)
    report["pairs_ok"] = symbols_ok
    report["pairs_err"] = symbols_err
    report["elapsed_seconds"] = round(elapsed, 1)
    report["fee_bps_per_side"] = float(fee_bps_per_leg) * 2  # 2 legs per side
    report["fee_pct_roundtrip"] = float(fee_bps_per_leg) * 4 / 100.0  # 2 legs × 2 sides
    report["regime_injected"] = "synthetic_from_spot_bars"
    report["data_source"] = "okx_funding_history"
    report["live_venue_planned"] = "dydx_v4_or_kraken_futures (deferred to Week 3)"
    report["per_symbol"] = per_symbol
    report["trades"] = all_trades
    report["strategy_config"] = {
        "enter_threshold": ENTER_THRESHOLD,
        "exit_threshold": EXIT_THRESHOLD,
        "min_hold_periods": MIN_HOLD_PERIODS,
        "harvest_regimes": sorted(HARVEST_REGIMES),
        "funding_period_hours": FUNDING_PERIOD_HOURS,
        "round_trip_fee_bps": ROUND_TRIP_FEE_BPS,
        "notional_per_cycle_usd": notional_usd,
    }
    return report


# ---------------------------------------------------------------------------
# Report writing — mirror harness.py
# ---------------------------------------------------------------------------


def write_report(report: dict[str, Any]) -> tuple[Path, Path]:
    _RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    timestamped = _RESULTS_DIR / f"gates_report_funding_rate_harvest_{ts}.json"
    latest = _RESULTS_DIR / "gates_report_funding_rate_harvest_latest.json"
    text = json.dumps(report, indent=2, default=str)
    timestamped.write_text(text)
    shutil.copyfile(timestamped, latest)
    return timestamped, latest


def _print_summary(report: dict[str, Any]) -> None:
    print()
    print(f"=== funding_rate_harvest | {report.get('n_trades', 0)} trades | "
          f"promotion_eligible={report.get('promotion_eligible')} ===")
    for g in report.get("gates", []):
        status = "PASS" if g.get("pass") else "FAIL"
        print(f"  [{status}] {g.get('gate'):24s} value={g.get('value')!s:30s} "
              f"threshold={g.get('threshold')}")
    print()
    print("--- per-symbol ---")
    print(f"  {'sym':<6}{'trades':>7}{'sharpe':>10}{'pf':>10}"
          f"{'pnl_usd':>14}{'avg_apy_pre_fee':>18}")
    for ps in report.get("per_symbol", []):
        s = ps.get("summary") or {}
        sym = ps.get("symbol", "?")
        n = s.get("n_trades", 0)
        sh = s.get("sharpe", 0.0)
        pf = s.get("profit_factor", 0.0)
        pnl = s.get("total_pnl_usd", 0.0)
        apy = s.get("implied_apy_pre_fee", 0.0)
        print(f"  {sym:<6}{n:>7}{sh:>10.3f}{str(pf):>10}{pnl:>14.2f}{apy*100:>17.2f}%")
    print()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Funding-rate harvest backtest (read-only OKX history)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--symbol", type=str, help="Single symbol, e.g. BTC")
    g.add_argument("--all", action="store_true",
                   help=f"Run all default symbols: {','.join(DEFAULT_SYMBOLS)}")
    ap.add_argument("--days", type=int, default=90, help="Lookback window (default 90)")
    ap.add_argument("--notional-usd", type=float, default=DEFAULT_NOTIONAL_USD,
                    help="Per-cycle notional (default 10000)")
    ap.add_argument("--fee-bps-per-leg", type=float, default=TAKER_FEE_BPS_PER_LEG,
                    help=f"Taker fee per leg in bps (default {TAKER_FEE_BPS_PER_LEG})")
    ap.add_argument("--no-cache", action="store_true",
                    help="Bypass the funding-rate disk cache")
    ap.add_argument(
        "--enter-threshold", type=float, default=ENTER_THRESHOLD,
        help=f"Funding-rate entry threshold per period (default {ENTER_THRESHOLD})",
    )
    ap.add_argument(
        "--exit-threshold", type=float, default=EXIT_THRESHOLD,
        help=f"Funding-rate exit threshold per period (default {EXIT_THRESHOLD})",
    )
    ap.add_argument(
        "--min-hold-periods", type=int, default=MIN_HOLD_PERIODS,
        help=f"Min funding periods to hold before exit (default {MIN_HOLD_PERIODS})",
    )
    ap.add_argument(
        "--sweep", action="store_true",
        help="Run a 3-row threshold sweep (literature / calibrated / aggressive) "
             "and print per-row gate summaries. Persists only the calibrated row.",
    )
    ap.add_argument("--bootstrap-iters", type=int, default=DEFAULT_BOOTSTRAP_ITERS)
    ap.add_argument("--walk-forward-windows", type=int, default=DEFAULT_WALK_FORWARD_WINDOWS)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--quiet", action="store_true")
    ap.add_argument("--no-write", action="store_true",
                    help="Do not persist the gates report")
    args = ap.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-8s %(name)s %(message)s",
        stream=sys.stderr,
    )

    symbols: list[str] = list(DEFAULT_SYMBOLS) if args.all else [args.symbol.upper()]

    # Threshold rows for the optional sweep. The "calibrated" row is what
    # the OKX cap (1bp/8h) actually allows; "literature" matches the
    # ScienceDirect 2025 paper's table 4. "Aggressive" is exit-on-flip.
    sweep_rows = [
        {"label": "literature_default",
         "enter": ENTER_THRESHOLD, "exit": EXIT_THRESHOLD, "min_hold": MIN_HOLD_PERIODS},
        {"label": "venue_calibrated",
         "enter": 0.00005, "exit": 0.0, "min_hold": 1},  # 0.5bp/8h enter, exit on non-positive
        {"label": "aggressive_any_positive",
         "enter": 0.000001, "exit": 0.0, "min_hold": 1},  # any positive funding
    ]

    try:
        if args.sweep:
            sweep_results: list[dict[str, Any]] = []
            for row in sweep_rows:
                LOGGER.info("=== sweep row: %s ===", row["label"])
                rep = run_strategy(
                    symbols=symbols, days=args.days,
                    notional_usd=args.notional_usd,
                    fee_bps_per_leg=args.fee_bps_per_leg,
                    bootstrap_iters=args.bootstrap_iters,
                    walk_forward_windows=args.walk_forward_windows,
                    seed=args.seed, cache=not args.no_cache,
                    enter_threshold=row["enter"], exit_threshold=row["exit"],
                    min_hold_periods=row["min_hold"],
                )
                rep["sweep_label"] = row["label"]
                sweep_results.append(rep)
            # Persist only the venue_calibrated row as the canonical report.
            canonical = next(r for r in sweep_results if r["sweep_label"] == "venue_calibrated")
            canonical["sweep_results"] = [
                {"label": r["sweep_label"],
                 "n_trades": r["n_trades"],
                 "promotion_eligible": r["promotion_eligible"],
                 "gates": r["gates"],
                 "config": r["strategy_config"]}
                for r in sweep_results
            ]
            report = canonical
        else:
            report = run_strategy(
                symbols=symbols,
                days=args.days,
                notional_usd=args.notional_usd,
                fee_bps_per_leg=args.fee_bps_per_leg,
                bootstrap_iters=args.bootstrap_iters,
                walk_forward_windows=args.walk_forward_windows,
                seed=args.seed,
                cache=not args.no_cache,
                enter_threshold=args.enter_threshold,
                exit_threshold=args.exit_threshold,
                min_hold_periods=args.min_hold_periods,
            )
    except Exception as exc:  # noqa: BLE001
        LOGGER.error("Fatal: %s", exc, exc_info=True)
        return 2

    if not args.no_write:
        timestamped, latest = write_report(report)
        LOGGER.info("Report: %s", timestamped)
        LOGGER.info("Latest: %s", latest)

    if not args.quiet:
        _print_summary(report)
        if args.sweep and "sweep_results" in report:
            print("--- threshold sweep ---")
            print(f"  {'label':<28}{'n_trades':>10}{'eligible':>12}")
            for sr in report["sweep_results"]:
                print(f"  {sr['label']:<28}{sr['n_trades']:>10}{str(sr['promotion_eligible']):>12}")
            print()

    return 0 if report.get("promotion_eligible") else 1


if __name__ == "__main__":
    sys.exit(main())
