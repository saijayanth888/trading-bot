"""Unit tests for scripts/resample_1h_to_4h.py — Coinbase 1h → 4h pipeline.

Run from the repo root:
    pytest tests/test_resample_4h.py -v

Design principles:
  * No exchange calls. Every test feeds a synthetic 1h DataFrame.
  * Math is hand-verified: open=first1h.open, high=max, low=min, close=last1h.close,
    volume=sum (per pandas convention with `agg`).
  * Anchor: 4h bars are anchored to UTC 00:00 (00/04/08/12/16/20) via
    `origin='epoch'` + `label='left'` + `closed='left'` — verified explicitly.
  * Idempotency: writing identical rows twice produces identical files.
  * Gap-handling: 1h gaps don't crash; the resampler drops all-NaN 4h bars.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import resample_1h_to_4h as rs  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_1h_df(start: str, n_bars: int, base_price: float = 100.0) -> pd.DataFrame:
    """Build a synthetic 1h OHLCV DataFrame with predictable values.

    Bar i has:
        open  = base + i
        close = base + i + 0.5
        high  = base + i + 1
        low   = base + i - 1
        volume = 10 + i
    so we can hand-verify resampling math.
    """
    ts = pd.date_range(start=start, periods=n_bars, freq="1h", tz="UTC")
    df = pd.DataFrame({
        "date": ts,
        "open":   [base_price + i for i in range(n_bars)],
        "high":   [base_price + i + 1 for i in range(n_bars)],
        "low":    [base_price + i - 1 for i in range(n_bars)],
        "close":  [base_price + i + 0.5 for i in range(n_bars)],
        "volume": [10.0 + i for i in range(n_bars)],
    })
    return df


# ---------------------------------------------------------------------------
# 1. OHLCV math correctness
# ---------------------------------------------------------------------------


def test_resample_math_first_high_low_last_sum():
    """A clean 7-day 1h series resamples to 4h with mathematically correct OHLCV."""
    df_1h = make_1h_df("2026-04-01T00:00:00Z", n_bars=7 * 24)  # 168 bars
    out = rs.resample_1h_to_4h(df_1h)

    # 168 bars / 4 = 42 4h bars
    assert len(out) == 42

    # First 4h bar covers 1h bars 0..3:
    #   open=base+0=100, high=max(101,102,103,104)=104, low=min(99,100,101,102)=99,
    #   close=base+3+0.5=103.5, volume=10+11+12+13=46
    first = out.iloc[0]
    assert first["date"] == pd.Timestamp("2026-04-01T00:00:00Z")
    assert first["open"] == pytest.approx(100.0)
    assert first["high"] == pytest.approx(104.0)
    assert first["low"] == pytest.approx(99.0)
    assert first["close"] == pytest.approx(103.5)
    assert first["volume"] == pytest.approx(10 + 11 + 12 + 13)

    # Last 4h bar covers 1h bars 164..167:
    #   open=100+164=264, close=100+167+0.5=267.5, high=100+167+1=268, low=263, vol=174+175+176+177
    last = out.iloc[-1]
    assert last["open"] == pytest.approx(264.0)
    assert last["high"] == pytest.approx(268.0)
    assert last["low"] == pytest.approx(263.0)
    assert last["close"] == pytest.approx(267.5)
    assert last["volume"] == pytest.approx(174 + 175 + 176 + 177)


# ---------------------------------------------------------------------------
# 2. Anchor alignment
# ---------------------------------------------------------------------------


def test_anchor_aligned_to_utc_00_04_08_12_16_20():
    """Every 4h bar must be timestamped at a UTC hour in {0,4,8,12,16,20}."""
    df_1h = make_1h_df("2026-04-01T00:00:00Z", n_bars=7 * 24)
    out = rs.resample_1h_to_4h(df_1h)
    hours = {ts.hour for ts in out["date"]}
    assert hours == {0, 4, 8, 12, 16, 20}


def test_anchor_when_input_starts_mid_4h_window():
    """If 1h data starts at 03:00 (mid-4h-window), the FIRST 4h bar is at 00:00.

    1h bars at 03:00, 04:00, 05:00, 06:00, 07:00:
        - bar at 03:00 belongs to the 4h window [00:00, 04:00) → labeled "00:00"
        - bars at 04:00..07:00 belong to the 4h window [04:00, 08:00) → "04:00"

    With closed='left', label='left', origin='epoch':
        First produced bar is timestamped 2026-04-01T00:00:00Z (covers just the
        03:00 1h bar) — open=close=that single bar's values.
        Second bar is timestamped 04:00, covers four 1h bars.
    """
    # 5 bars starting at 03:00
    ts = pd.date_range(start="2026-04-01T03:00:00Z", periods=5, freq="1h", tz="UTC")
    df = pd.DataFrame({
        "date": ts,
        "open":   [10, 20, 30, 40, 50],
        "high":   [11, 21, 31, 41, 51],
        "low":    [ 9, 19, 29, 39, 49],
        "close":  [10.5, 20.5, 30.5, 40.5, 50.5],
        "volume": [1, 2, 3, 4, 5],
    })
    out = rs.resample_1h_to_4h(df)

    # Expected: two 4h bars: 00:00 (just the 03:00 1h bar) + 04:00 (4 bars).
    assert len(out) == 2
    bar0 = out.iloc[0]
    assert bar0["date"] == pd.Timestamp("2026-04-01T00:00:00Z")
    # First (and only) source bar of bucket [00:00, 04:00) is at 03:00 with values 10,11,9,10.5,1.
    assert bar0["open"] == 10
    assert bar0["high"] == 11
    assert bar0["low"] == 9
    assert bar0["close"] == 10.5
    assert bar0["volume"] == 1
    bar1 = out.iloc[1]
    assert bar1["date"] == pd.Timestamp("2026-04-01T04:00:00Z")
    assert bar1["open"] == 20      # 1h@04
    assert bar1["close"] == 50.5   # 1h@07
    assert bar1["high"] == 51
    assert bar1["low"] == 19
    assert bar1["volume"] == 2 + 3 + 4 + 5


# ---------------------------------------------------------------------------
# 3. Idempotency
# ---------------------------------------------------------------------------


def test_write_idempotent(tmp_path):
    """Writing the same rows twice produces byte-identical files."""
    df_1h = make_1h_df("2026-04-01T00:00:00Z", n_bars=4 * 24)
    out = rs.resample_1h_to_4h(df_1h)
    rows = rs.df_to_freqtrade_json_rows(out)
    p = tmp_path / "BTC_USD-4h.json"
    rs.write_json_atomic(p, rows)
    first_bytes = p.read_bytes()
    rs.write_json_atomic(p, rows)
    second_bytes = p.read_bytes()
    assert first_bytes == second_bytes


def test_is_up_to_date_detects_match(tmp_path):
    df_1h = make_1h_df("2026-04-01T00:00:00Z", n_bars=4 * 24)
    out = rs.resample_1h_to_4h(df_1h)
    rows = rs.df_to_freqtrade_json_rows(out)
    p = tmp_path / "BTC_USD-4h.json"
    rs.write_json_atomic(p, rows)
    assert rs.is_up_to_date(p, rows) is True


def test_is_up_to_date_detects_new_bar(tmp_path):
    df_1h = make_1h_df("2026-04-01T00:00:00Z", n_bars=4 * 24)
    out = rs.resample_1h_to_4h(df_1h)
    rows = rs.df_to_freqtrade_json_rows(out)
    p = tmp_path / "BTC_USD-4h.json"
    rs.write_json_atomic(p, rows[:-1])  # write all but last bar
    assert rs.is_up_to_date(p, rows) is False  # rows has one more bar


# ---------------------------------------------------------------------------
# 4. Gap handling
# ---------------------------------------------------------------------------


def test_gap_in_1h_does_not_crash():
    """If 1h candles have a gap, the resampler drops the all-NaN 4h bar and continues."""
    # Build 48 1h bars but DELETE bars 16-19 (one full 4h window) to simulate downtime
    df_1h = make_1h_df("2026-04-01T00:00:00Z", n_bars=48)
    # Drop bars 16,17,18,19 (the [16:00, 20:00) 4h window)
    keep = df_1h[~df_1h["date"].isin([
        pd.Timestamp("2026-04-01T16:00:00Z"),
        pd.Timestamp("2026-04-01T17:00:00Z"),
        pd.Timestamp("2026-04-01T18:00:00Z"),
        pd.Timestamp("2026-04-01T19:00:00Z"),
    ])].reset_index(drop=True)
    out = rs.resample_1h_to_4h(keep)
    # Expected: 12 4h bars (48/4) minus the one empty bucket = 11
    assert len(out) == 11
    # And the 16:00 bar must NOT be in the output
    assert pd.Timestamp("2026-04-01T16:00:00Z") not in set(out["date"])
    # 20:00 bar should still be present (data resumes at bar 20)
    assert pd.Timestamp("2026-04-01T20:00:00Z") in set(out["date"])


def test_empty_input_returns_empty():
    df = pd.DataFrame(columns=["date", "open", "high", "low", "close", "volume"])
    out = rs.resample_1h_to_4h(df)
    assert len(out) == 0
    assert list(out.columns) == ["date", "open", "high", "low", "close", "volume"]


# ---------------------------------------------------------------------------
# 5. Output format → Freqtrade JsonDataHandler shape
# ---------------------------------------------------------------------------


def test_freqtrade_json_rows_shape():
    """Rows are [[ms_int, o, h, l, c, v], ...] with the timestamp at the bar OPEN."""
    df_1h = make_1h_df("2026-04-01T00:00:00Z", n_bars=8)
    out = rs.resample_1h_to_4h(df_1h)
    rows = rs.df_to_freqtrade_json_rows(out)
    assert len(rows) == 2
    for row in rows:
        assert len(row) == 6
        assert isinstance(row[0], int)
        for v in row[1:]:
            assert isinstance(v, float)
    # First bar OPEN at 2026-04-01T00:00 UTC = 1775347200000 ms
    expected_ms = int(pd.Timestamp("2026-04-01T00:00:00Z").value // 1_000_000)
    assert rows[0][0] == expected_ms


def test_freqtrade_json_rows_match_dataframe_to_json():
    """Our hand-written serializer matches what `DataFrame.to_json(orient='values')`
    would produce — that's the format JsonDataHandler reads on the other side."""
    df_1h = make_1h_df("2026-04-01T00:00:00Z", n_bars=12)
    out = rs.resample_1h_to_4h(df_1h)
    rows_ours = rs.df_to_freqtrade_json_rows(out)

    # Mirror what JsonDataHandler.ohlcv_store does:
    df_for_json = out.copy()
    df_for_json["date"] = df_for_json["date"].dt.as_unit("ms").astype("int64")
    rows_ref_str = df_for_json[["date","open","high","low","close","volume"]] \
        .reset_index(drop=True).to_json(orient="values")
    rows_ref = json.loads(rows_ref_str)

    # Compare with float tolerance
    assert len(rows_ours) == len(rows_ref)
    for a, b in zip(rows_ours, rows_ref):
        assert a[0] == b[0]
        for x, y in zip(a[1:], b[1:]):
            assert x == pytest.approx(y)


# ---------------------------------------------------------------------------
# 6. pair_to_filename helper
# ---------------------------------------------------------------------------


def test_pair_to_filename():
    assert rs.pair_to_filename("BTC/USD") == "BTC_USD"
    assert rs.pair_to_filename("SOL/USDT:USDT") == "SOL_USDT:USDT"
    assert rs.pair_to_filename("LINK/USD") == "LINK_USD"


# ---------------------------------------------------------------------------
# 7. load_pairs → reads NFI X6 whitelist
# ---------------------------------------------------------------------------


def test_load_pairs_reads_whitelist(tmp_path):
    cfg = tmp_path / "nfi.json"
    cfg.write_text(json.dumps({
        "exchange": {"name": "coinbase", "pair_whitelist": ["BTC/USD", "ETH/USD"]}
    }))
    pairs = rs.load_pairs(cfg)
    assert pairs == ["BTC/USD", "ETH/USD"]


def test_load_pairs_raises_on_empty_whitelist(tmp_path):
    cfg = tmp_path / "nfi.json"
    cfg.write_text(json.dumps({"exchange": {"pair_whitelist": []}}))
    with pytest.raises(RuntimeError, match="empty pair_whitelist"):
        rs.load_pairs(cfg)
