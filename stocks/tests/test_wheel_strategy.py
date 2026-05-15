"""
Unit tests for wheel.strategy and wheel.config.

Pure-function tests: no Alpaca, no network. Fast (< 100ms).
Run from stocks/: source venv/bin/activate && pytest tests/test_wheel_strategy.py -v
"""

from datetime import date, timedelta

import pytest
from wheel.config import WheelConfig, load_config
from wheel.strategy import (
    OptionContract,
    filter_calls,
    filter_puts,
    is_earnings_blackout,
    profit_take_threshold,
    score_contract,
    select_best,
)

# ── Fixtures ────────────────────────────────────────────────────────────────


def _put(strike, dte, delta, bid, oi=1000, underlying="SOFI"):
    return OptionContract(
        symbol=f"{underlying}TEST{int(strike*100):08d}P",
        underlying=underlying,
        strike=strike,
        expiry=date.today() + timedelta(days=dte),
        contract_type="put",
        delta=-abs(delta),  # puts are negative
        bid=bid,
        ask=bid * 1.05,
        open_interest=oi,
    )


def _call(strike, dte, delta, bid, oi=1000, underlying="SOFI"):
    return OptionContract(
        symbol=f"{underlying}TEST{int(strike*100):08d}C",
        underlying=underlying,
        strike=strike,
        expiry=date.today() + timedelta(days=dte),
        contract_type="call",
        delta=abs(delta),
        bid=bid,
        ask=bid * 1.05,
        open_interest=oi,
    )


@pytest.fixture
def cfg():
    return WheelConfig()


# ── filter_puts ─────────────────────────────────────────────────────────────


def test_filter_puts_accepts_30_delta(cfg):
    candidate = _put(strike=15.0, dte=8, delta=0.30, bid=0.20)
    out = filter_puts([candidate], cfg)
    assert len(out) == 1


def test_filter_puts_rejects_outside_delta_band(cfg):
    too_high = _put(strike=15.0, dte=8, delta=0.50, bid=0.20)
    too_low = _put(strike=15.0, dte=8, delta=0.10, bid=0.20)
    out = filter_puts([too_high, too_low], cfg)
    assert out == []


def test_filter_puts_rejects_outside_dte_band(cfg):
    too_short = _put(strike=15.0, dte=2, delta=0.30, bid=0.20)
    too_long = _put(strike=15.0, dte=30, delta=0.30, bid=0.20)
    out = filter_puts([too_short, too_long], cfg)
    assert out == []


def test_filter_puts_rejects_low_open_interest(cfg):
    # Default min_open_interest is 500
    illiquid = _put(strike=15.0, dte=8, delta=0.30, bid=0.20, oi=10)
    out = filter_puts([illiquid], cfg)
    assert out == []


def test_filter_puts_rejects_low_yield(cfg):
    # Default min_yield_per_week is 0.008 (0.8%) → bid/strike < 0.008
    low_premium = _put(strike=100.0, dte=8, delta=0.30, bid=0.50)  # 0.5% yield
    out = filter_puts([low_premium], cfg)
    assert out == []


def test_filter_puts_rejects_calls(cfg):
    a_call = _call(strike=15.0, dte=8, delta=0.30, bid=0.20)
    out = filter_puts([a_call], cfg)
    assert out == []


def test_filter_puts_respects_min_strike(cfg):
    too_low = _put(strike=10.0, dte=8, delta=0.30, bid=0.15)
    out = filter_puts([too_low], cfg, min_strike=14.0)
    assert out == []


# ── filter_calls ────────────────────────────────────────────────────────────


def test_filter_calls_rejects_below_cost_basis(cfg):
    # We were assigned at $15 — never sell a CC at $14
    below = _call(strike=14.0, dte=8, delta=0.30, bid=0.20)
    above = _call(strike=16.0, dte=8, delta=0.30, bid=0.20)
    out = filter_calls([below, above], cfg, cost_basis=15.0)
    assert len(out) == 1
    assert out[0].strike == 16.0


def test_filter_calls_rejects_puts(cfg):
    not_a_call = _put(strike=15.0, dte=8, delta=0.30, bid=0.20)
    out = filter_calls([not_a_call], cfg, cost_basis=10.0)
    assert out == []


# ── score_contract ──────────────────────────────────────────────────────────


def test_score_higher_for_lower_delta():
    low_delta = _put(strike=15.0, dte=8, delta=0.20, bid=0.15)
    high_delta = _put(strike=15.0, dte=8, delta=0.40, bid=0.15)
    assert score_contract(low_delta) > score_contract(high_delta)


def test_score_higher_for_shorter_dte():
    short = _put(strike=15.0, dte=5, delta=0.30, bid=0.15)
    long = _put(strike=15.0, dte=15, delta=0.30, bid=0.15)
    assert score_contract(short) > score_contract(long)


def test_score_higher_for_better_premium():
    low_premium = _put(strike=15.0, dte=8, delta=0.30, bid=0.10)
    high_premium = _put(strike=15.0, dte=8, delta=0.30, bid=0.30)
    assert score_contract(high_premium) > score_contract(low_premium)


# ── select_best ─────────────────────────────────────────────────────────────


def test_select_best_returns_top_n():
    a = _put(strike=15.0, dte=8, delta=0.20, bid=0.20, underlying="SOFI")
    b = _put(strike=15.0, dte=8, delta=0.30, bid=0.10, underlying="MARA")
    c = _put(strike=15.0, dte=8, delta=0.40, bid=0.05, underlying="F")
    out = select_best([a, b, c], n=2)
    assert len(out) == 2
    assert out[0].underlying == "SOFI"  # highest score


def test_select_best_dedups_by_underlying():
    """Same underlying, two contracts — only the higher-scoring one returns."""
    high = _put(strike=15.0, dte=7, delta=0.20, bid=0.30, underlying="SOFI")
    low = _put(strike=15.0, dte=7, delta=0.30, bid=0.10, underlying="SOFI")
    out = select_best([high, low])
    assert len(out) == 1
    assert out[0] is high


def test_select_best_empty_input():
    assert select_best([]) == []
    assert select_best([], n=5) == []


# ── earnings blackout ──────────────────────────────────────────────────────


def test_earnings_blackout_within_window():
    today = date(2026, 5, 10)
    earnings_in_3_days = date(2026, 5, 13)
    assert is_earnings_blackout(earnings_in_3_days, today=today, blackout_days=3)


def test_earnings_blackout_outside_window():
    today = date(2026, 5, 10)
    earnings_in_5_days = date(2026, 5, 15)
    assert not is_earnings_blackout(earnings_in_5_days, today=today, blackout_days=3)


def test_earnings_blackout_none_means_no_blackout():
    assert not is_earnings_blackout(None)


def test_earnings_blackout_past_earnings_not_blocked():
    today = date(2026, 5, 10)
    yesterday = date(2026, 5, 9)
    assert not is_earnings_blackout(yesterday, today=today, blackout_days=3)


# ── profit_take_threshold ──────────────────────────────────────────────────


def test_profit_take_threshold_default():
    cfg = WheelConfig()  # 0.5 fraction
    # sold at $0.30 → close at $0.15 (50% off)
    assert profit_take_threshold(0.30, cfg) == pytest.approx(0.15)


def test_profit_take_threshold_aggressive():
    cfg = WheelConfig(profit_take_fraction=0.75)  # close at 75% gain
    # sold at $0.40 → close at $0.10 (75% off)
    assert profit_take_threshold(0.40, cfg) == pytest.approx(0.10)


# ── WheelConfig validation ─────────────────────────────────────────────────


def test_config_validates_default():
    WheelConfig().assert_valid()


def test_config_rejects_inverted_delta_band():
    cfg = WheelConfig(delta_min=0.5, delta_max=0.3)
    with pytest.raises(ValueError):
        cfg.assert_valid()


def test_config_rejects_zero_dte():
    cfg = WheelConfig(dte_min=0, dte_max=10)
    with pytest.raises(ValueError):
        cfg.assert_valid()


def test_config_rejects_invalid_profit_take():
    cfg = WheelConfig(profit_take_fraction=1.5)
    with pytest.raises(ValueError):
        cfg.assert_valid()


def test_load_config_uses_env_overrides(monkeypatch):
    monkeypatch.setenv("WHEEL_SYMBOLS", "SOFI,MARA")
    monkeypatch.setenv("WHEEL_DELTA_MIN", "0.20")
    monkeypatch.setenv("WHEEL_MAX_RISK_PER_TICKER", "2500")
    cfg = load_config()
    assert cfg.symbols == ("SOFI", "MARA")
    assert cfg.delta_min == 0.20
    assert cfg.max_risk_per_ticker_usd == 2500.0
