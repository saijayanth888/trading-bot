"""Unit tests for `user_data.modules.producers.positions`.

Covers **B6/B9** — UNION crypto fills + wheel state + shark.

The crypto-side test stubs `ops_db.open_positions` so we don't need a
live postgres in CI. The wheel + shark tests use tmp_path-pinned JSON.

Verifies the union shape: every row has {source, symbol, side, …}, the
`_meta.counts` reflects the per-source split, and the producer NEVER
writes to disk (spec §5.4).
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from user_data.modules.producers import positions as POS


@pytest.fixture
def stub_crypto_ops_db(monkeypatch):
    """Stub `ops_db.open_positions` so the test doesn't need postgres."""
    import sys
    fake = type(sys)("user_data.dashboard.ops_db")
    def open_positions(limit=50):
        return [
            {
                "trade_id": 1,
                "pair": "BTC/USD",
                "direction": "long",
                "open_rate": 80_540.33,
                "stake_amount": 1_900.0,
                "current_profit": -0.016,
                "mark_price": 79_252.0,
                "mark_ts": "2026-05-15T15:18:08+00:00",
                "open_date": "2026-05-15T12:12:59+00:00",
                "external_id": "v4-btc-001",
                "regime_at_entry": "trending_up",
            },
        ]
    fake.open_positions = open_positions  # type: ignore[attr-defined]
    # The `user_data.dashboard` package itself must be importable
    import user_data.dashboard  # noqa: F401
    monkeypatch.setitem(sys.modules, "user_data.dashboard.ops_db", fake)
    return fake


def test_positions_snapshot_unions_three_sources(monkeypatch, tmp_path, stub_crypto_ops_db):
    # Wheel positions.json — one short put
    wheel_dir = tmp_path / "wheel"
    wheel_dir.mkdir()
    (wheel_dir / "account_snapshot.json").write_text(json.dumps({
        "ts": "2026-05-15T20:59:06+00:00",
        "portfolio_value": 100_241.17,
        "wheel_open_positions": 1,
    }))
    (wheel_dir / "positions.json").write_text(json.dumps([
        {
            "kind": "short_put",
            "symbol": "NVDA260522P00220000",
            "underlying": "NVDA",
            "strike": 220.0,
            "qty": -1,
            "entry_credit": 6.16,
            "mark": 4.80,
            "opened_at": "2026-05-13T15:30:00+00:00",
            "order_id": "wheel-nvda-001",
        },
    ]))
    monkeypatch.setattr(POS, "_WHEEL_STATE_DIR", wheel_dir)
    monkeypatch.setattr(POS, "_WHEEL_SNAPSHOT_PATH", wheel_dir / "account_snapshot.json")
    monkeypatch.setattr(POS, "_WHEEL_POSITIONS_PATH", wheel_dir / "positions.json")

    # Shark data — one long stock
    shark_path = tmp_path / "data.json"
    shark_path.write_text(json.dumps({
        "generated_at": "2026-05-15T17:30:07",
        "open_trades": [
            {
                "symbol": "AMD",
                "entry_price": 145.0,
                "qty": 10,
                "mark": 150.0,
                "entry_date": "2026-05-14",
                "regime": "trending_up",
                "stop": 135.0,
                "target": 160.0,
                "setup_tag": "momentum",
            },
        ],
    }))
    monkeypatch.setattr(POS, "_SHARK_DATA_PATH", shark_path)

    out = POS.positions_snapshot()
    rows = out["positions"]
    sources = {r["source"] for r in rows}
    assert sources == {"crypto", "wheel", "shark"}

    counts = out["_meta"]["counts"]
    assert counts["crypto"] == 1
    assert counts["wheel"] == 1
    assert counts["shark"] == 1
    assert counts["total"] == 3

    # Wheel row should carry collateral (strike × 100 × |qty|)
    wheel_row = next(r for r in rows if r["source"] == "wheel")
    assert wheel_row["stake_usd"] == pytest.approx(22_000.0, abs=0.01)
    # PnL = (entry_credit − mark) × 100 × |qty| = (6.16 − 4.80) × 100 = 136.0
    assert wheel_row["pnl_usd"] == pytest.approx(136.0, abs=0.01)

    # Shark row should carry pnl_usd = (mark − entry) × qty = 50.0
    shark_row = next(r for r in rows if r["source"] == "shark")
    assert shark_row["pnl_usd"] == pytest.approx(50.0, abs=0.01)
    assert shark_row["side"] == "long"

    # Crypto row preserves stake + current_profit
    crypto_row = next(r for r in rows if r["source"] == "crypto")
    assert crypto_row["symbol"] == "BTC/USD"
    assert crypto_row["pnl_pct"] == pytest.approx(-0.016, abs=1e-6)


def test_positions_snapshot_handles_missing_files_gracefully(monkeypatch, tmp_path, stub_crypto_ops_db):
    """B9 — when wheel + shark JSON files are missing, producer still
    returns a shaped dict with just the crypto rows + degraded errors."""
    monkeypatch.setattr(POS, "_WHEEL_STATE_DIR", tmp_path / "nope")
    monkeypatch.setattr(POS, "_WHEEL_SNAPSHOT_PATH", tmp_path / "nope" / "account_snapshot.json")
    monkeypatch.setattr(POS, "_WHEEL_POSITIONS_PATH", tmp_path / "nope" / "positions.json")
    monkeypatch.setattr(POS, "_SHARK_DATA_PATH", tmp_path / "missing.json")

    out = POS.positions_snapshot()
    assert out["_meta"]["counts"]["crypto"] == 1
    assert out["_meta"]["counts"]["wheel"] == 0
    assert out["_meta"]["counts"]["shark"] == 0
    assert out["_meta"]["counts"]["total"] == 1


def test_positions_snapshot_wheel_count_only_fallback(monkeypatch, tmp_path):
    """When `positions.json` is absent but `account_snapshot.json` says
    `wheel_open_positions: N`, surface a single placeholder row so the
    operator at least sees "N wheel positions open" instead of 0."""
    wheel_dir = tmp_path / "wheel"
    wheel_dir.mkdir()
    (wheel_dir / "account_snapshot.json").write_text(json.dumps({
        "ts": "2026-05-15T20:59:06+00:00",
        "portfolio_value": 100_241.17,
        "wheel_open_positions": 3,   # 3 positions but no positions.json
        "wheel_cumulative_pnl": 669.05,
    }))
    monkeypatch.setattr(POS, "_WHEEL_STATE_DIR", wheel_dir)
    monkeypatch.setattr(POS, "_WHEEL_SNAPSHOT_PATH", wheel_dir / "account_snapshot.json")
    monkeypatch.setattr(POS, "_WHEEL_POSITIONS_PATH", wheel_dir / "positions.json")  # absent

    # Stub crypto + shark to zero
    import sys
    fake = type(sys)("user_data.dashboard.ops_db")
    fake.open_positions = lambda limit=50: []  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "user_data.dashboard.ops_db", fake)
    monkeypatch.setattr(POS, "_SHARK_DATA_PATH", tmp_path / "missing.json")

    out = POS.positions_snapshot()
    assert out["_meta"]["counts"]["wheel"] == 1   # placeholder row
    wheel_row = next(r for r in out["positions"] if r["source"] == "wheel")
    assert wheel_row["extra"]["summary_only"] is True
    assert wheel_row["extra"]["snapshot_count"] == 3
    # Error surface so the operator sees "positions.json missing"
    assert any("positions.json missing" in e for e in out["_meta"]["errors"])
