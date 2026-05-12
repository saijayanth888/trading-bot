#!/usr/bin/env python3
"""Resample Coinbase 1h candles → 4h candles, write JSON files Freqtrade can read.

Coinbase Advanced REST exposes 5m / 15m / 1h / 6h / 1d — no native 4h. NFI X6
hard-requires 4h informative candles. This script bridges the gap by fetching
1h candles via ccxt (or reading cached feathers if present) and writing the
resampled 4h series to JSON files in Freqtrade's `JsonDataHandler` format:

    [[ts_ms, open, high, low, close, volume], ...]   ← orient="values"

File layout (matches `JsonDataHandler._pair_data_filename` for SPOT):

    <datadir>/<EXCHANGE>/<BASE>_<QUOTE>-4h.json        # SPOT

For futures the file lives under <datadir>/<EXCHANGE>/futures/, but NFI X6 is
spot-only per nfi_x6_config.json, so SPOT path is what we write.

Anchor: 4h bars are anchored to UTC 00:00 (00/04/08/12/16/20) using
`origin='epoch'` — this is what NFI X6 expects (matches what Binance / Kraken
serve natively and what the offline backtest used). Each 4h bar is timestamped
at its OPEN (left edge): the 04:00 bar covers the closed interval [04:00, 08:00).

Idempotent: if the output JSON already contains all the bars the resample
produces (and the latest bar matches), the file is left untouched.

Fail-soft: any per-pair error is logged and skipped; the script exits 0 so cron
does not alarm. Critical errors at startup (missing config, missing ccxt) DO
exit non-zero so the installer's gate can catch them.

Usage:
    python3 scripts/resample_1h_to_4h.py                # all NFI X6 pairs
    python3 scripts/resample_1h_to_4h.py BTC/USD ETH/USD
    python3 scripts/resample_1h_to_4h.py --days 30      # broader history
    python3 scripts/resample_1h_to_4h.py --seed         # one-time historical seed (90 days)
    python3 scripts/resample_1h_to_4h.py --dry-run      # don't write files
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from pathlib import Path
from typing import Iterable, Sequence

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG = REPO_ROOT / "user_data" / "strategies" / "nfi_x6_config.json"
DEFAULT_DATADIR = REPO_ROOT / "user_data" / "data" / "coinbase"

logger = logging.getLogger("resample_1h_to_4h")


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%SZ",
    )
    logging.Formatter.converter = time.gmtime


def load_pairs(config_path: Path) -> list[str]:
    """Read pair whitelist from nfi_x6_config.json."""
    cfg = json.loads(config_path.read_text())
    pairs = cfg.get("exchange", {}).get("pair_whitelist") or []
    if not pairs:
        raise RuntimeError(f"empty pair_whitelist in {config_path}")
    return list(pairs)


def pair_to_filename(pair: str) -> str:
    """Match freqtrade.misc.pair_to_filename — slash to underscore."""
    return pair.replace("/", "_")


def fetch_1h_via_ccxt(pair: str, days: int) -> "pandas.DataFrame":  # type: ignore[name-defined]
    """Fetch the last N days of 1h candles from Coinbase via ccxt.

    Coinbase Advanced's REST OHLCV endpoint returns at most ~300 candles per
    request, starting at `since`. Crucially: it does NOT return the most
    recent N candles when `since` is provided — it returns a window starting
    at `since` and capped at ~300 bars, even if you ask for `limit=1000`.

    So we paginate by advancing `since` past the last returned bar. We can
    only stop when (a) the call returns empty OR (b) the cursor is already
    beyond `now`. A short batch (len < limit) does NOT mean we are done.

    To guard against very-fresh-bar edge cases (Coinbase sometimes lags the
    last bar by a few hours), we ALSO do a final unbounded `limit=300`
    fetch with NO since — that returns the most-recent bars and we merge.
    """
    import ccxt  # type: ignore
    import pandas as pd  # type: ignore

    ex = ccxt.coinbase({"enableRateLimit": True})
    if not ex.has.get("fetchOHLCV"):
        raise RuntimeError("coinbase exchange does not advertise fetchOHLCV")

    now_ms = ex.milliseconds()
    one_hour_ms = 3600 * 1000
    since = now_ms - days * 24 * 3600 * 1000

    all_rows: list[list] = []
    cursor = since
    limit = 300
    # Cap pages defensively: 90 days @ 1h = 2160 bars / 300 per page = ~8 pages.
    # 1000 is room enough for >100 days even after retries.
    for _page in range(1000):
        batch = ex.fetch_ohlcv(pair, timeframe="1h", since=cursor, limit=limit)
        if not batch:
            break
        all_rows.extend(batch)
        last_ts = batch[-1][0]
        # Forward progress check — guard against infinite loop on identical batches.
        if last_ts <= cursor:
            break
        # Advance to the bar right after the last one returned.
        cursor = last_ts + one_hour_ms
        # Stop once cursor is past `now` (no future bars possible).
        if cursor >= now_ms:
            break
        time.sleep(ex.rateLimit / 1000.0)

    # Final top-up fetch (no `since`): returns the most-recent ~300 bars.
    # This catches any tail-end bars the paginated loop missed.
    try:
        tail = ex.fetch_ohlcv(pair, timeframe="1h", limit=limit)
        if tail:
            all_rows.extend(tail)
    except Exception:
        pass  # tail is a best-effort top-up; don't fail on it

    if not all_rows:
        return pd.DataFrame(columns=["date", "open", "high", "low", "close", "volume"])
    df = pd.DataFrame(all_rows, columns=["date_ms", "open", "high", "low", "close", "volume"])
    df = df.drop_duplicates("date_ms").sort_values("date_ms").reset_index(drop=True)
    df["date"] = pd.to_datetime(df["date_ms"], unit="ms", utc=True)
    return df[["date", "open", "high", "low", "close", "volume"]]


def load_1h_from_feather(datadir: Path, pair: str) -> "pandas.DataFrame":  # type: ignore[name-defined]
    """If a feather cache exists from a prior freqtrade download-data, prefer it."""
    import pandas as pd  # type: ignore

    src = datadir / f"{pair_to_filename(pair)}-1h.feather"
    if not src.exists():
        return pd.DataFrame(columns=["date", "open", "high", "low", "close", "volume"])
    df = pd.read_feather(src)
    if "date" not in df.columns:
        return pd.DataFrame(columns=["date", "open", "high", "low", "close", "volume"])
    df["date"] = pd.to_datetime(df["date"], utc=True)
    return df[["date", "open", "high", "low", "close", "volume"]].copy()


def resample_1h_to_4h(df_1h):
    """Resample 1h OHLCV → 4h, anchored to UTC 00:00 (00/04/08/12/16/20).

    Returns a DataFrame with columns [date, open, high, low, close, volume],
    where `date` is the bar's OPEN timestamp (left edge of the interval).
    Rows with all-NaN OHLC are dropped (gap handling).
    """
    import pandas as pd  # type: ignore

    if df_1h is None or len(df_1h) == 0:
        return pd.DataFrame(columns=["date", "open", "high", "low", "close", "volume"])

    df = df_1h.copy()
    df["date"] = pd.to_datetime(df["date"], utc=True)
    df = df.sort_values("date").drop_duplicates("date")
    df = df.set_index("date")

    # `origin='epoch'` + label='left' anchors bars on UTC 00:00,04:00,08:00,...
    # closed='left' means the bar at 04:00 includes [04:00, 08:00).
    agg = df.resample(
        "4h", label="left", closed="left", origin="epoch"
    ).agg(
        {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}
    )
    # Drop bars where there were zero 1h candles (gap) — keeping them would
    # produce all-NaN rows that confuse downstream indicators.
    agg = agg.dropna(subset=["open", "high", "low", "close"])

    out = agg.reset_index()
    out["date"] = out["date"].dt.tz_convert("UTC")
    return out[["date", "open", "high", "low", "close", "volume"]]


def df_to_freqtrade_json_rows(df_4h) -> list[list]:
    """Convert a 4h DataFrame to Freqtrade JsonDataHandler's `orient="values"` rows.

    Format matches `JsonDataHandler.ohlcv_store`:
        date column is converted to UTC int64 milliseconds-since-epoch.
    """
    if df_4h is None or len(df_4h) == 0:
        return []
    import pandas as pd  # type: ignore

    dates = pd.to_datetime(df_4h["date"], utc=True).dt.as_unit("ms").astype("int64")
    rows: list[list] = []
    for ts, o, h, l, c, v in zip(
        dates, df_4h["open"], df_4h["high"], df_4h["low"], df_4h["close"], df_4h["volume"],
    ):
        rows.append([int(ts), float(o), float(h), float(l), float(c), float(v)])
    return rows


def write_json_atomic(path: Path, rows: list[list]) -> None:
    """Write rows to JSON in freqtrade format, atomically (tmpfile + rename)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    # Match what `DataFrame.to_json(orient="values")` produces:
    # a single line: [[ts,o,h,l,c,v],[ts,o,h,l,c,v],...]
    tmp.write_text(json.dumps(rows, separators=(",", ":")))
    tmp.replace(path)


def is_up_to_date(existing_path: Path, new_rows: list[list]) -> bool:
    """Return True iff `existing_path` already holds the exact same rows."""
    if not existing_path.exists() or not new_rows:
        return False
    try:
        existing = json.loads(existing_path.read_text())
    except Exception:
        return False
    if not isinstance(existing, list) or len(existing) != len(new_rows):
        return False
    # Compare just the last bar's open ts + close — cheap proxy that catches
    # the common "no new bar since last run" case.
    e_last = existing[-1]
    n_last = new_rows[-1]
    return bool(
        len(e_last) == len(n_last)
        and e_last[0] == n_last[0]
        and abs(float(e_last[4]) - float(n_last[4])) < 1e-9
    )


def process_pair(
    pair: str,
    datadir: Path,
    days: int,
    dry_run: bool,
    prefer_feather: bool,
) -> tuple[str, int, str]:
    """Resample one pair. Returns (pair, bars_written, status)."""
    import pandas as pd  # type: ignore

    # 1. Source 1h candles. Prefer the on-disk feather cache (faster, no API
    #    burn), fall back to ccxt fetch.
    df_1h = pd.DataFrame()
    if prefer_feather:
        df_1h = load_1h_from_feather(datadir, pair)
    src = "feather"
    if len(df_1h) == 0:
        df_1h = fetch_1h_via_ccxt(pair, days)
        src = "ccxt"
    if len(df_1h) == 0:
        return (pair, 0, f"NO_DATA src={src}")

    # 2. Resample.
    df_4h = resample_1h_to_4h(df_1h)
    if len(df_4h) == 0:
        return (pair, 0, f"RESAMPLE_EMPTY src={src} 1h_rows={len(df_1h)}")

    # 3. Convert + write.
    rows = df_to_freqtrade_json_rows(df_4h)
    out = datadir / f"{pair_to_filename(pair)}-4h.json"
    if dry_run:
        return (pair, len(rows), f"DRY_RUN src={src} would_write={out}")
    if is_up_to_date(out, rows):
        return (pair, len(rows), f"UNCHANGED src={src} {out.name}")
    write_json_atomic(out, rows)
    return (pair, len(rows), f"WROTE src={src} {out.name}")


def main(argv: Sequence[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("pairs", nargs="*", help="optional pairs to process (default: NFI X6 whitelist)")
    p.add_argument("--config", type=Path, default=DEFAULT_CONFIG,
                   help="path to nfi_x6_config.json (default: %(default)s)")
    p.add_argument("--datadir", type=Path, default=DEFAULT_DATADIR,
                   help="freqtrade data dir for coinbase (default: %(default)s)")
    p.add_argument("--days", type=int, default=14,
                   help="how many days of 1h history to fetch (default: 14; cron loops keep recent bars fresh)")
    p.add_argument("--seed", action="store_true",
                   help="one-time historical seed: 90 days (operator runs this once)")
    p.add_argument("--no-feather", action="store_true",
                   help="skip feather cache; always fetch from ccxt")
    p.add_argument("--dry-run", action="store_true",
                   help="resample but don't write JSON files")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args(argv)

    _setup_logging(args.verbose)

    if args.seed:
        args.days = 90

    # Fail loudly on missing dependencies — these are install-time errors.
    try:
        import pandas  # noqa: F401
    except ImportError:
        logger.error("pandas not installed; install with `pip install pandas`")
        return 2
    if not args.config.exists():
        logger.error("config not found: %s", args.config)
        return 2

    pairs = args.pairs or load_pairs(args.config)
    args.datadir.mkdir(parents=True, exist_ok=True)
    logger.info("resampling %d pair(s) into %s (days=%d, dry_run=%s)",
                len(pairs), args.datadir, args.days, args.dry_run)

    n_wrote = n_unchanged = n_err = 0
    for pair in pairs:
        try:
            _, bars, status = process_pair(
                pair=pair,
                datadir=args.datadir,
                days=args.days,
                dry_run=args.dry_run,
                prefer_feather=not args.no_feather,
            )
            logger.info("[%s] bars=%d %s", pair, bars, status)
            if status.startswith("WROTE"):
                n_wrote += 1
            elif status.startswith("UNCHANGED") or status.startswith("DRY_RUN"):
                n_unchanged += 1
            else:
                n_err += 1
        except Exception as exc:  # fail-soft
            logger.error("[%s] FAIL %s", pair, exc, exc_info=args.verbose)
            n_err += 1

    logger.info("summary: wrote=%d unchanged=%d errors=%d", n_wrote, n_unchanged, n_err)
    # Cron-friendly: never alarm. Exit 1 only when EVERY pair failed, which is
    # almost certainly a config bug worth waking up for.
    if n_err > 0 and n_wrote == 0 and n_unchanged == 0:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
