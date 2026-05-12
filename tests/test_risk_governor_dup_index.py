"""
Regression test for Bug 1 (2026-05-12).

Symptom: live freqtrade log emitted
    pandas.errors.InvalidIndexError: Reindexing only valid with uniquely
    valued Index objects
from ``RiskGovernor._pearson_returns`` when two trades closed at the same
candle timestamp (e.g. trailing-stop fill + immediate re-entry stamped at
the same minute). pd.concat(..., join="inner") on non-unique indices and
any subsequent .reindex() raise.

Root cause: the per-pair returns Series can carry duplicate timestamps.

Fix: dedupe the index (keep="last") in ``_pearson_returns`` before joining.

This test constructs two Series each with 3 duplicate timestamps and
verifies the governor returns a finite correlation, not raises.
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "user_data"))

from modules.risk_governor import RiskConfig, RiskGovernor  # noqa: E402


def _gov() -> RiskGovernor:
    cfg = RiskConfig(correlation_threshold=0.70, correlation_min_overlap=20)
    clk = lambda: datetime(2026, 5, 12, 12, 0, tzinfo=timezone.utc)
    return RiskGovernor(cfg, now_fn=clk)


def test_pearson_returns_handles_duplicate_index() -> None:
    """Two pair-returns Series with duplicate timestamps must NOT raise."""
    gov = _gov()
    base_idx = pd.date_range("2026-04-12", periods=100, freq="h", tz="UTC")
    # Inject 3 duplicate timestamps at the start (same candle filled twice).
    dup_idx = pd.DatetimeIndex(
        [base_idx[0]] * 3 + list(base_idx[1:])
    )
    rng = np.random.default_rng(7)
    a_vals = rng.normal(0, 1, 102)
    b_vals = a_vals * 0.9 + rng.normal(0, 0.1, 102)
    a = pd.Series(a_vals, index=dup_idx)
    b = pd.Series(b_vals, index=dup_idx)
    assert not a.index.is_unique, "test setup: a.index must have duplicates"
    assert not b.index.is_unique, "test setup: b.index must have duplicates"

    rho = gov._pearson_returns(a, b)
    assert rho is not None, "correlation must be computable after dedup"
    assert np.isfinite(rho), f"correlation must be finite, got {rho}"
    assert 0.5 < rho < 1.0, f"strongly-correlated pair should be highly +, got {rho}"


def test_pearson_returns_handles_one_sided_duplicates() -> None:
    """Only one of the two Series has duplicates — still must not raise."""
    gov = _gov()
    a_idx = pd.date_range("2026-04-12", periods=100, freq="h", tz="UTC")
    b_idx = pd.DatetimeIndex([a_idx[0]] * 2 + list(a_idx[1:]))
    rng = np.random.default_rng(11)
    a = pd.Series(rng.normal(0, 1, 100), index=a_idx)
    b = pd.Series(rng.normal(0, 1, 101), index=b_idx)
    assert a.index.is_unique
    assert not b.index.is_unique

    rho = gov._pearson_returns(a, b)
    # Result may be None (insufficient overlap or 0-std), but must not raise
    # and must be either None or a finite float.
    assert rho is None or np.isfinite(rho)


def test_pearson_returns_approve_entry_path_with_dup_index() -> None:
    """End-to-end: approve_entry() must not crash on duplicate timestamps."""
    gov = _gov()
    gov.update_equity(10_000)
    base_idx = pd.date_range("2026-04-12", periods=200, freq="h", tz="UTC")
    # 5 duplicate stamps at the head of each Series.
    dup_idx = pd.DatetimeIndex([base_idx[0]] * 5 + list(base_idx[1:]))
    rng = np.random.default_rng(42)
    btc = pd.Series(rng.normal(0, 1, 204), index=dup_idx)
    eth = btc * 0.95 + rng.normal(0, 0.1, 204)
    sol = pd.Series(rng.normal(0, 1, 204), index=dup_idx)

    # Should NOT raise — the gate either blocks or approves cleanly.
    decision = gov.approve_entry(
        pair="BTC/USD",
        signal_price=65000.0,
        base_stake=100.0,
        equity=10_000.0,
        open_positions=[("ETH/USD", 100.0)],
        pair_returns={"BTC/USD": btc, "ETH/USD": eth, "SOL/USD": sol},
    )
    # BTC and ETH are highly correlated → correlation_filter should fire.
    assert decision.blocking_constraint == "correlation_filter", (
        f"expected correlation block, got {decision.blocking_constraint}"
    )


if __name__ == "__main__":
    test_pearson_returns_handles_duplicate_index()
    test_pearson_returns_handles_one_sided_duplicates()
    test_pearson_returns_approve_entry_path_with_dup_index()
    print("OK — all 3 duplicate-index regression tests pass.")
