"""Slippage gate — boundary cases + stale-quote handling."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest

from quanta_core.execution.engine import OrderProposal, Side
from quanta_core.execution.slippage_gate import SlippageGateResult, passes

UTC = dt.UTC
NOW = dt.datetime(2026, 5, 12, 12, 0, 0, tzinfo=UTC)


def _proposal(limit_px: Decimal | None = Decimal("100")) -> OrderProposal:
    return OrderProposal(
        client_order_id="qc4-test-0001",
        symbol="BTC-USD",
        side=Side.BUY,
        qty=Decimal("1"),
        limit_px=limit_px,
        signal_px=Decimal("100"),
        strategy_name="test",
        intent_ts_ms=1_715_000_000_000,
    )


# ---------------------------------------------------------------------------
# Pass cases
# ---------------------------------------------------------------------------


def test_passes_when_drift_within_threshold() -> None:
    r = passes(
        _proposal(),
        current_mid=Decimal("100.4"),
        threshold_pct=Decimal("0.5"),
        quote_ts=NOW,
        now=NOW,
    )
    assert r.ok is True
    assert r.reason == "ok"
    assert r.drift_pct is not None
    assert r.drift_pct < Decimal("0.5")


def test_passes_on_exact_signal_price() -> None:
    r = passes(
        _proposal(),
        current_mid=Decimal("100"),
        threshold_pct=Decimal("0.5"),
        quote_ts=NOW,
        now=NOW,
    )
    assert r.ok is True
    assert r.drift_pct == Decimal("0")


def test_market_order_bypasses_gate() -> None:
    r = passes(
        _proposal(limit_px=None),
        current_mid=Decimal("1000000"),
        threshold_pct=Decimal("0.01"),
        quote_ts=NOW,
        now=NOW,
    )
    assert r.ok is True
    assert r.reason == "no_gate_market_order"


def test_threshold_zero_disables_gate_but_records_drift() -> None:
    r = passes(
        _proposal(),
        current_mid=Decimal("105"),
        threshold_pct=Decimal("0"),
        quote_ts=NOW,
        now=NOW,
    )
    assert r.ok is True
    assert r.reason == "gate_disabled"
    assert r.drift_pct == Decimal("5")


# ---------------------------------------------------------------------------
# Fail cases
# ---------------------------------------------------------------------------


def test_fails_when_drift_exceeds_threshold() -> None:
    r = passes(
        _proposal(),
        current_mid=Decimal("101"),
        threshold_pct=Decimal("0.5"),
        quote_ts=NOW,
        now=NOW,
    )
    assert r.ok is False
    assert r.reason == "drift_exceeds_threshold"
    assert r.drift_pct == Decimal("1")


def test_stale_quote_fails_immediately() -> None:
    quote_ts = NOW - dt.timedelta(seconds=10)
    r = passes(
        _proposal(),
        current_mid=Decimal("100"),
        threshold_pct=Decimal("0.5"),
        quote_ts=quote_ts,
        now=NOW,
        max_quote_age_s=5.0,
    )
    assert r.ok is False
    assert r.reason == "stale_quote"


def test_future_quote_ts_treated_as_stale() -> None:
    """Clock-skew safety: a quote_ts in the future is suspicious."""
    quote_ts = NOW + dt.timedelta(seconds=10)
    r = passes(
        _proposal(),
        current_mid=Decimal("100"),
        threshold_pct=Decimal("0.5"),
        quote_ts=quote_ts,
        now=NOW,
        max_quote_age_s=5.0,
    )
    assert r.ok is False
    assert r.reason == "stale_quote"


def test_quote_at_max_age_passes() -> None:
    """Boundary: 5s exactly is still fresh."""
    quote_ts = NOW - dt.timedelta(seconds=5)
    r = passes(
        _proposal(),
        current_mid=Decimal("100"),
        threshold_pct=Decimal("0.5"),
        quote_ts=quote_ts,
        now=NOW,
        max_quote_age_s=5.0,
    )
    assert r.ok is True


def test_quote_just_over_max_age_fails() -> None:
    quote_ts = NOW - dt.timedelta(seconds=5, milliseconds=1)
    r = passes(
        _proposal(),
        current_mid=Decimal("100"),
        threshold_pct=Decimal("0.5"),
        quote_ts=quote_ts,
        now=NOW,
        max_quote_age_s=5.0,
    )
    assert r.ok is False
    assert r.reason == "stale_quote"


def test_invalid_signal_price() -> None:
    p = OrderProposal(
        client_order_id="qc4-test-0001",
        symbol="X",
        side=Side.BUY,
        qty=Decimal("1"),
        limit_px=Decimal("100"),
        signal_px=Decimal("0"),  # invalid
        strategy_name="test",
        intent_ts_ms=1,
    )
    r = passes(
        p,
        current_mid=Decimal("100"),
        threshold_pct=Decimal("0.5"),
        quote_ts=NOW,
        now=NOW,
    )
    assert r.ok is False
    assert r.reason == "invalid_prices"


def test_invalid_mid_price() -> None:
    r = passes(
        _proposal(),
        current_mid=Decimal("0"),
        threshold_pct=Decimal("0.5"),
        quote_ts=NOW,
        now=NOW,
    )
    assert r.ok is False
    assert r.reason == "invalid_prices"


def test_drift_at_exact_threshold_passes() -> None:
    """Boundary: drift == threshold passes (strict-greater is the rule)."""
    r = passes(
        _proposal(),
        current_mid=Decimal("100.5"),
        threshold_pct=Decimal("0.5"),
        quote_ts=NOW,
        now=NOW,
    )
    assert r.ok is True


def test_result_is_frozen() -> None:
    r = SlippageGateResult(ok=True, reason="ok")
    with pytest.raises(Exception):  # noqa: B017
        r.ok = False  # type: ignore[misc]
