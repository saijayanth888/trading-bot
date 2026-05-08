"""
End-to-end smoke test for risk_governor + execution_engine.

Risk governor — every limit independently:
  1. Drawdown auto-pause + recovery
  2. Daily loss limit + UTC reset
  3. Max concurrent positions
  4. Max position size cap
  5. Pearson correlation block
  6. Circuit breaker + cooldown expiry
  7. Kelly Criterion math + safety scaling

Execution engine:
  8. Slippage gate aborts when drift > limit
  9. Retry with exponential backoff on transient errors
 10. Order timeout cancels + records cancel
 11. Partial fill tracking through monitor()
 12. Filled-success path through dry-run
 13. Audit log lines written to execution.log
"""

from __future__ import annotations

import json
import logging
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "user_data"))

from modules.execution_engine import (   # noqa: E402
    DryRunExecutionEngine,
    ExecutionConfig,
    ExecutionEngine,
    OrderReport,
    SlippageError,
    pair_to_product_id,
)
from modules.risk_governor import (      # noqa: E402
    RiskConfig,
    RiskGovernor,
)


def _ok(msg: str) -> None: print(f"  [✓] {msg}")
def _info(msg: str) -> None: print(f"  [i] {msg}")
def _hr() -> None: print("=" * 64)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class FakeClock:
    def __init__(self, t: datetime):
        self.t = t
    def __call__(self) -> datetime:
        return self.t
    def advance(self, **kwargs) -> None:
        self.t = self.t + timedelta(**kwargs)


def _gov(**overrides) -> tuple[RiskGovernor, FakeClock]:
    cfg = RiskConfig(**overrides)
    clk = FakeClock(datetime(2026, 5, 8, 12, 0, tzinfo=timezone.utc))
    return RiskGovernor(cfg, now_fn=clk), clk


def _approve(gov, **kwargs):
    defaults = {
        "pair": "BTC/USD",
        "signal_price": 65000.0,
        "base_stake": 1000.0,
        "equity": 10_000.0,
        "model_confidence": 0.6,
        "open_positions": [],
        "pair_returns": None,
    }
    defaults.update(kwargs)
    return gov.approve_entry(**defaults)


# ---------------------------------------------------------------------------
# Risk governor tests
# ---------------------------------------------------------------------------


def test_drawdown_pause_resume() -> None:
    print("\n[1/13] Drawdown auto-pause + recovery (hysteresis)")
    gov, _ = _gov(max_portfolio_drawdown_pct=0.08)
    gov.update_equity(10_000)        # peak
    gov.update_equity(9_500)         # -5% drawdown — still OK
    assert _approve(gov, equity=9_500).approved
    gov.update_equity(9_100)         # -9% drawdown → trip
    d = _approve(gov, equity=9_100)
    assert not d.approved and d.blocking_constraint == "max_drawdown_paused"
    # Climb back to within half the limit (5% off peak * 50% = 4%)
    gov.update_equity(9_700)         # -3% off peak — under 4% trigger → resume
    d2 = _approve(gov, equity=9_700)
    assert d2.approved
    _ok(f"trip at -9% → resume at -3% (hysteresis)")


def test_daily_loss_limit() -> None:
    print("\n[2/13] Daily loss limit + UTC reset")
    gov, clk = _gov(daily_loss_limit_pct=0.03)
    gov.update_equity(10_000)
    # Two losing trades: -1% and -2.5%, total -3.5% → trip
    gov.record_trade_close("BTC/USD", -100, -0.01, clk())
    gov.record_trade_close("ETH/USD", -250, -0.025, clk())
    d = _approve(gov)
    assert not d.approved and d.blocking_constraint == "daily_loss_limit"
    # Advance 6h — same UTC day (12:00 → 18:00), still blocked
    clk.advance(hours=6)
    gov.update_equity(9_650)
    d2 = _approve(gov, equity=9_650)
    assert not d2.approved, f"should still block at 18:00 same UTC day: {d2.reason}"
    # Advance another 7h — crosses into next UTC day, anchor resets
    clk.advance(hours=7)
    gov.update_equity(9_650)
    d3 = _approve(gov, equity=9_650)
    assert d3.approved, f"should unblock next UTC day: {d3.reason}"
    _ok("triggered at -3.5% loss; cleared at next UTC midnight")


def test_max_concurrent_positions() -> None:
    print("\n[3/13] Max concurrent positions")
    gov, _ = _gov(max_concurrent_positions=3)
    gov.update_equity(10_000)
    open_pos = [("BTC/USD", 100), ("ETH/USD", 100), ("SOL/USD", 100)]
    d = _approve(gov, open_positions=open_pos)
    assert not d.approved and d.blocking_constraint == "max_concurrent_positions"
    d2 = _approve(gov, open_positions=open_pos[:2])
    assert d2.approved
    _ok("blocks at 3 open, allows at 2")


def test_max_position_size_cap() -> None:
    print("\n[4/13] Max position size cap")
    gov, _ = _gov(max_position_size_pct=0.10, kelly_enabled=False)
    gov.update_equity(10_000)
    # base stake 5000 (50% of equity) → must cap at 1000 (10%)
    d = _approve(gov, base_stake=5000)
    assert d.approved
    assert abs(d.suggested_stake - 1000.0) < 1e-6, d.suggested_stake
    _ok(f"5000 → {d.suggested_stake} (10% of 10k)")


def test_correlation_filter() -> None:
    print("\n[5/13] Correlation filter (Pearson > 0.7)")
    gov, _ = _gov(correlation_threshold=0.70, correlation_min_overlap=20)
    gov.update_equity(10_000)
    idx = pd.date_range("2026-04-08", periods=200, freq="h", tz="UTC")
    rng = np.random.default_rng(42)
    btc = pd.Series(rng.normal(0, 1, 200), index=idx)
    eth = btc * 0.95 + rng.normal(0, 0.1, 200)        # heavily correlated
    sol = pd.Series(rng.normal(0, 1, 200), index=idx) # uncorrelated
    open_pos = [("ETH/USD", 100)]                     # already long ETH
    d_block = _approve(
        gov, pair="BTC/USD",
        open_positions=open_pos,
        pair_returns={"BTC/USD": btc, "ETH/USD": eth},
    )
    assert not d_block.approved and d_block.blocking_constraint == "correlation_filter", d_block
    # Same setup but candidate uncorrelated
    d_ok = _approve(
        gov, pair="SOL/USD",
        open_positions=open_pos,
        pair_returns={"SOL/USD": sol, "ETH/USD": eth},
    )
    assert d_ok.approved, f"uncorrelated should be allowed: {d_ok}"
    _ok(f"BTC↔ETH ρ={d_block.correlations.get('ETH/USD', 0):.2f} blocked; SOL allowed")


def test_circuit_breaker() -> None:
    print("\n[6/13] Circuit breaker + cooldown expiry")
    gov, clk = _gov(
        circuit_breaker_consecutive_losses=3,
        circuit_breaker_cooldown_hours=2.0,
    )
    gov.update_equity(10_000)
    for _ in range(3):
        gov.record_trade_close("BTC/USD", -50, -0.005, clk())
    d = _approve(gov)
    assert not d.approved and d.blocking_constraint == "circuit_breaker_cooldown"
    # Still blocked 1h later
    clk.advance(hours=1)
    gov.update_equity(10_000)
    assert not _approve(gov).approved
    # 2h later → cooldown expired
    clk.advance(hours=1, minutes=5)
    gov.update_equity(10_000)
    assert _approve(gov).approved
    _ok("3 losses → 2h cooldown → expires correctly")


def test_kelly_math() -> None:
    print("\n[7/13] Kelly Criterion sizing")
    gov, _ = _gov(
        kelly_enabled=True,
        kelly_safety_factor=0.5,
        kelly_max_fraction=0.25,
        kelly_min_trades=10,
        max_position_size_pct=0.30,            # don't let cap dominate the test
        circuit_breaker_consecutive_losses=999, # don't trip CB while priming history
    )
    gov.update_equity(10_000)
    # Below min_trades → kelly_fraction = 0
    d_cold = _approve(gov, model_confidence=0.65, base_stake=5000)
    assert d_cold.kelly_fraction == 0.0
    # Seed history: 60% wins of 2%, 40% losses of 1% → b = 2.0
    # f* = (0.65 * 2 - 0.35) / 2 = 0.475 → safety 0.5 → 0.2375 → cap 0.25 → 0.2375
    for _ in range(60):
        gov.record_trade_close("BTC/USD", 200, 0.02)
    for _ in range(40):
        gov.record_trade_close("BTC/USD", -100, -0.01)
    d = _approve(gov, model_confidence=0.65, base_stake=5000)
    assert abs(d.kelly_fraction - 0.2375) < 1e-3, d.kelly_fraction
    # At p=0.65 b=2: kelly_stake = 0.2375 * 10k = 2375 < base 5000 → use kelly
    assert abs(d.suggested_stake - 2375.0) < 1.0, d.suggested_stake
    # Below break-even (p < 1/(1+b) = 1/3) → kelly clamped to 0
    d_low = _approve(gov, model_confidence=0.25, base_stake=5000)
    assert d_low.kelly_fraction == 0.0, f"p<break-even → 0; got {d_low.kelly_fraction}"
    _ok(f"p=0.65 b=2 → f*=0.2375 (cap 0.25); p=0.25 → 0 (below break-even)")


# ---------------------------------------------------------------------------
# Execution engine tests
# ---------------------------------------------------------------------------


def test_slippage_gate() -> None:
    print("\n[8/13] Slippage gate")
    eng = DryRunExecutionEngine(
        config=ExecutionConfig(slippage_pct=0.003, dry_run=True, poll_interval_sec=0.0),
        mock_drift_pct=0.005,        # 0.5% drift > 0.3% limit
    )
    rep = eng.place_limit("BTC-USD", "BUY", 0.001, 65000.0, signal_price=65000.0)
    assert rep.status == "REJECTED" and "drift" in (rep.cancelled_reason or "")
    _ok(f"0.5% drift → REJECTED: {rep.cancelled_reason}")
    # Drift within limit → allowed
    eng2 = DryRunExecutionEngine(
        config=ExecutionConfig(slippage_pct=0.003, dry_run=True, poll_interval_sec=0.0),
        mock_drift_pct=0.001,
        fill_after_polls=1,
    )
    rep2 = eng2.place_limit("BTC-USD", "BUY", 0.001, 65000.0, signal_price=65000.0)
    assert rep2.status == "FILLED", rep2
    _ok(f"0.1% drift → FILLED")


def test_retry_backoff() -> None:
    print("\n[9/13] Retry with exponential backoff on transient errors")
    cfg = ExecutionConfig(
        retry_attempts=3,
        retry_backoff_initial_sec=0.001,    # fast for tests
        retry_backoff_factor=2.0,
        order_timeout_sec=2.0,
        poll_interval_sec=0.001,
        dry_run=True,
    )

    class FlakyEngine(DryRunExecutionEngine):
        attempts = 0
        def _submit_order(self, **kwargs):
            FlakyEngine.attempts += 1
            if FlakyEngine.attempts < 3:
                raise RuntimeError("transient")
            return super()._submit_order(**kwargs)

    eng = FlakyEngine(config=cfg, fill_after_polls=1)
    t0 = time.perf_counter()
    rep = eng.place_limit("BTC-USD", "BUY", 0.001, 65000.0, signal_price=65000.0)
    elapsed = time.perf_counter() - t0
    assert rep.status == "FILLED" and rep.attempts == 3
    # 0.001 + 0.002 = 0.003s of backoff at minimum
    _ok(f"fail/fail/succeed → attempts={rep.attempts}, elapsed={elapsed:.3f}s")


def test_timeout_cancel() -> None:
    print("\n[10/13] Order timeout cancellation")
    cfg = ExecutionConfig(
        order_timeout_sec=0.05, poll_interval_sec=0.01, dry_run=True,
    )
    # Never fills (fill_after_polls = 1000) within the 50ms budget
    eng = DryRunExecutionEngine(config=cfg, fill_after_polls=10_000)
    rep = eng.place_limit("BTC-USD", "BUY", 0.001, 65000.0, signal_price=65000.0)
    assert rep.status == "CANCELLED" and rep.cancelled_reason == "timeout"
    _ok(f"never-fills → CANCELLED after {cfg.order_timeout_sec}s")


def test_partial_fill_tracking() -> None:
    print("\n[11/13] Partial fill tracking")
    cfg = ExecutionConfig(
        order_timeout_sec=0.5, poll_interval_sec=0.01, dry_run=True,
    )
    # Fill after a few polls so we accumulate partials
    eng = DryRunExecutionEngine(
        config=cfg, fill_after_polls=4, partial_fills=3,
    )
    rep = eng.place_limit("BTC-USD", "BUY", 1.0, 65000.0, signal_price=65000.0)
    assert rep.status == "FILLED", rep.status
    assert len(rep.fills) >= 2, f"expected ≥2 partial fill records, got {len(rep.fills)}"
    _ok(f"FILLED with {len(rep.fills)} fill events recorded")


def test_dry_run_filled_path() -> None:
    print("\n[12/13] Dry-run filled-success path")
    cfg = ExecutionConfig(
        order_timeout_sec=0.5, poll_interval_sec=0.0, dry_run=True,
    )
    eng = DryRunExecutionEngine(config=cfg, fill_after_polls=1)
    rep = eng.place_limit("BTC-USD", "BUY", 0.001, 65000.0, signal_price=65000.0)
    assert rep.status == "FILLED"
    assert rep.order_id and rep.order_id.startswith("dry-")
    assert rep.attempts == 1
    _ok(f"clean path → id={rep.order_id[:24]}…  status={rep.status}")


def test_audit_log() -> None:
    print("\n[13/13] Audit log lines written to execution.log")
    with tempfile.TemporaryDirectory() as td:
        log_path = Path(td) / "execution.log"
        cfg = ExecutionConfig(
            log_path=str(log_path), order_timeout_sec=0.5,
            poll_interval_sec=0.0, dry_run=True,
        )
        # Reset the audit logger so this run gets its own handler
        existing = logging.getLogger("execution_engine.audit")
        for h in list(existing.handlers):
            existing.removeHandler(h)
            try: h.close()
            except Exception: pass

        eng = DryRunExecutionEngine(config=cfg, fill_after_polls=1)
        eng.place_limit("BTC-USD", "BUY", 0.001, 65000.0, signal_price=65000.0)
        # Force flush
        for h in logging.getLogger("execution_engine.audit").handlers:
            h.flush()
        text = log_path.read_text()
    assert "INIT" in text and "PLACE" in text and "FILLED" in text, text
    _ok(f"audit log: {len(text.splitlines())} lines, INIT/PLACE/FILLED present")


def test_pair_conversion() -> None:
    assert pair_to_product_id("BTC/USD") == "BTC-USD"
    assert pair_to_product_id("eth/usd") == "ETH-USD"


def main() -> int:
    _hr()
    print(" Risk governor + execution engine smoke test")
    _hr()

    test_drawdown_pause_resume()
    test_daily_loss_limit()
    test_max_concurrent_positions()
    test_max_position_size_cap()
    test_correlation_filter()
    test_circuit_breaker()
    test_kelly_math()
    test_slippage_gate()
    test_retry_backoff()
    test_timeout_cancel()
    test_partial_fill_tracking()
    test_dry_run_filled_path()
    test_audit_log()
    test_pair_conversion()

    print("\nALL CHECKS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
