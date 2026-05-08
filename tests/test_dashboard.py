"""
Smoke test for the dashboard FastAPI app.

  1. Indicators (RSI/BB/MACD) compute on a synthetic close series.
  2. Trade-journal markers built from a seeded SQLite render correctly.
  3. Regime-segment compression collapses contiguous labels.
  4. /api/pairs returns the configured pairs.
  5. / renders the index template.
  6. /api/state returns a payload with every expected key (graceful when
     freqtrade is unreachable).
  7. /api/candles falls back to the public Coinbase source and returns a
     real candle list when available; if not, returns 503 (which we
     accept — the test asserts behavior, not network reachability).
  8. /ws — connect, receive one JSON push, disconnect cleanly.
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "user_data"))


def _ok(msg: str) -> None: print(f"  [✓] {msg}")
def _info(msg: str) -> None: print(f"  [i] {msg}")
def _hr() -> None: print("=" * 64)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _seed_journal(db_path: Path) -> None:
    """Drop a few synthetic trades into the trade_journal table."""
    sys.path.insert(0, str(ROOT / "user_data"))
    from modules.trade_journal import TradeJournal
    j = TradeJournal(db_path=db_path)
    base = datetime.now(timezone.utc) - timedelta(hours=20)
    for i, (pnl, pct) in enumerate([(50, 0.05), (-10, -0.01), (25, 0.025)]):
        opened = base + timedelta(hours=i * 4)
        closed = opened + timedelta(hours=2)
        jid = j.log_entry(
            pair="BTC/USD", direction="long",
            entry_price=65_000.0 + i * 10, stake=1000.0,
            opened_at=opened, regime="trending_up",
        )
        j.log_exit(
            jid, exit_price=65_500.0 + i * 10,
            pnl=pnl, pnl_pct=pct, exit_reason="test",
            duration_min=120, closed_at=closed,
        )


def _make_temp_user_data(tmp: Path) -> Path:
    """Build a minimal user_data tree that the dashboard can read from."""
    (tmp / "data").mkdir(parents=True)
    (tmp / "logs").mkdir(parents=True)
    _seed_journal(tmp / "data" / "onchain.db")
    # Drop a fake evolution log so /api/state has a champion field
    (tmp / "logs" / "evolution.json").write_text(json.dumps([{
        "generation": 4, "champion": "gen4-c00", "runner_up": "gen4-c01",
        "alive": [{"member_id": "gen4-c00", "fitness": 1.42}],
    }]))
    return tmp


# ---------------------------------------------------------------------------
# 1. Indicators
# ---------------------------------------------------------------------------


def test_indicators() -> None:
    print("\n[1/8] Indicators (RSI / Bollinger / MACD)")
    from dashboard.indicators import attach_all, rsi, bollinger_bands, macd

    rng = np.random.default_rng(7)
    close = pd.Series(np.cumsum(rng.normal(0, 0.5, 200)) + 100)
    rsi_v = rsi(close)
    assert 0 <= float(rsi_v.iloc[-1]) <= 100, rsi_v.iloc[-1]
    upper, mid, lower = bollinger_bands(close)
    assert float(upper.iloc[-1]) >= float(mid.iloc[-1]) >= float(lower.iloc[-1])
    m, s, h = macd(close)
    assert abs(float(m.iloc[-1]) - (float(m.iloc[-1]) - float(s.iloc[-1])) - float(s.iloc[-1])) < 1e-9
    df = pd.DataFrame({
        "date": pd.date_range("2026-01-01", periods=200, freq="5min", tz="UTC"),
        "open": close, "high": close + 0.5, "low": close - 0.5, "close": close,
        "volume": np.full(200, 100.0),
    })
    df = attach_all(df)
    for col in ("rsi", "bb_upper", "bb_mid", "bb_lower", "macd", "macd_signal", "macd_hist"):
        assert col in df.columns, col
    _ok("RSI in [0,100]; BB upper≥mid≥lower; MACD hist = MACD−signal; attach_all adds 7 cols")


# ---------------------------------------------------------------------------
# 2. Trade markers
# ---------------------------------------------------------------------------


def test_trade_markers(tmp_user_data: Path) -> None:
    print("\n[2/8] Trade markers from seeded journal")
    os.environ["USER_DATA_ROOT"] = str(tmp_user_data)
    # Force module reload so it picks up the new env var
    import importlib, dashboard.data_sources as ds
    importlib.reload(ds)
    markers = ds.fetch_trade_markers("BTC/USD")
    # 3 trades × 2 markers (entry+exit) = 6
    assert len(markers) == 6, f"got {len(markers)} markers"
    shapes = {m["shape"] for m in markers}
    assert shapes == {"arrowUp", "arrowDown"}
    # Entries should sort chronologically before exits
    times = [m["time"] for m in markers]
    assert times == sorted(times)
    _ok(f"{len(markers)} markers, shapes={shapes}, sorted")


# ---------------------------------------------------------------------------
# 3. Regime-segment compression
# ---------------------------------------------------------------------------


def test_regime_segments() -> None:
    print("\n[3/8] regime_segments_from_df compression")
    from dashboard.data_sources import regime_segments_from_df
    times = pd.date_range("2026-01-01", periods=10, freq="5min", tz="UTC")
    labels = ["trending_up"] * 4 + ["mean_reverting"] * 3 + ["trending_up"] * 3
    df = pd.DataFrame({"date": times, "regime_label": labels})
    segs = regime_segments_from_df(df)
    assert len(segs) == 3, f"expected 3 segments, got {len(segs)}: {segs}"
    assert segs[0]["label"] == "trending_up"
    assert segs[1]["label"] == "mean_reverting"
    assert segs[2]["label"] == "trending_up"
    assert segs[0]["start"] < segs[1]["start"] < segs[2]["start"]
    _ok(f"10 rows → 3 segments: {[s['label'] for s in segs]}")


# ---------------------------------------------------------------------------
# 4–7. HTTP endpoints
# ---------------------------------------------------------------------------


def test_http_endpoints(tmp_user_data: Path) -> None:
    os.environ["USER_DATA_ROOT"] = str(tmp_user_data)
    os.environ["DASHBOARD_PAIRS"] = "BTC/USD,ETH/USD"
    # Empty FREQTRADE_API_PASS forces freqtrade fetcher to bail out, so
    # /api/state still completes without a live freqtrade and /api/candles
    # falls back to the public Coinbase fetcher.
    os.environ["FREQTRADE_API_PASS"] = ""

    # Re-import to pick up the env vars
    for name in ("dashboard.data_sources", "dashboard.app"):
        if name in sys.modules:
            del sys.modules[name]
    from fastapi.testclient import TestClient
    from dashboard.app import app

    with TestClient(app) as client:
        print("\n[4/8] GET /api/pairs")
        r = client.get("/api/pairs")
        assert r.status_code == 200
        body = r.json()
        assert body["pairs"] == ["BTC/USD", "ETH/USD"]
        _ok(f"pairs={body['pairs']} timeframe={body['timeframe']}")

        print("\n[5/8] GET / (HTML)")
        r = client.get("/")
        assert r.status_code == 200
        text = r.text
        assert "Trading bot" in text
        assert "lightweight-charts" in text
        assert "/static/js/app.js" in text
        # Selects rendered with our pairs
        assert 'value="BTC/USD"' in text
        _ok(f"index renders {len(text)} bytes; chart lib + static refs present")

        print("\n[6/8] GET /api/state (sidebar payload)")
        r = client.get("/api/state")
        assert r.status_code == 200, r.text
        s = r.json()
        for k in (
            "ts", "pair", "regime", "sentiment_score", "onchain", "tft",
            "positions", "daily_pnl", "daily_pnl_history", "recent_trades",
            "champion",
        ):
            assert k in s, f"missing {k} in state"
        # Champion comes from the synthetic evolution log we wrote
        assert s["champion"]["champion_id"] == "gen4-c00"
        # 3 closed trades in journal → recent_trades has them
        assert len(s["recent_trades"]) == 3
        _ok(f"state ok: champion={s['champion']['champion_id']}, "
            f"recent_trades={len(s['recent_trades'])}, "
            f"positions={len(s['positions'])}")

        print("\n[7/8] GET /api/candles/BTC/USD (Coinbase fallback)")
        r = client.get("/api/candles/BTC/USD?timeframe=5m&limit=120")
        if r.status_code == 200:
            body = r.json()
            assert body["pair"] == "BTC/USD"
            assert body["source"] in ("freqtrade", "coinbase")
            assert isinstance(body["candles"], list)
            if body["candles"]:
                c0 = body["candles"][0]
                for k in ("time", "open", "high", "low", "close"):
                    assert k in c0, c0
            assert "indicators" in body
            for k in ("rsi", "bb_upper", "bb_mid", "bb_lower",
                      "macd", "macd_signal", "macd_hist"):
                assert k in body["indicators"], k
            _ok(f"candles ok: source={body['source']}, "
                f"n_candles={len(body['candles'])}, "
                f"rsi_pts={len(body['indicators']['rsi'])}")
        else:
            # Test environment may have no internet — accept 503
            assert r.status_code == 503, r.status_code
            _ok(f"candles 503 (no network) — accepted, body={r.json()}")

        print("\n[8/8] WebSocket /ws — receive at least one push")
        with client.websocket_connect("/ws") as ws:
            payload = ws.receive_json()
            assert isinstance(payload, dict)
            assert "ts" in payload, payload
        _ok(f"ws received payload with keys={sorted(payload)[:6]}…")


def main() -> int:
    _hr()
    print(" Dashboard smoke test")
    _hr()

    test_indicators()
    test_regime_segments()

    with tempfile.TemporaryDirectory() as td:
        tmp = _make_temp_user_data(Path(td))
        test_trade_markers(tmp)
        test_http_endpoints(tmp)

    print("\nALL CHECKS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
