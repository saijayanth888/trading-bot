"""Unit tests for `user_data.modules.producers.metrics`.

Covers **B3** — the Sharpe / max-DD single-truth fix.

Key cases pinned:
  - Zero-mean walk-forward window guard (legacy returned `inf`, then
    `sharpe: -306.15` after annualization noise).
  - Non-zero, well-defined return series — must produce a sane Sharpe.
  - Empty series → degenerate=true, sharpe=0.0 (NOT NaN).
  - Win-rate classifier — explicitly does NOT fall back to `pnl_pct`
    when only absolute pnl keys are missing (this is the B2 root cause
    being prevented in the producer-layer).
  - BTC 34× single-name-cap case (B8 forensic): a synthesized "stake
    poisoned by an oversized fill" return series annualizes to a
    legitimately bad Sharpe — but NOT to ±inf. The producer surfaces
    a number the operator can interpret.
"""
from __future__ import annotations

import math

import pytest

from user_data.modules.producers.metrics import (
    sharpe_max_dd,
    walk_forward_variance,
    win_rate,
    metrics_snapshot,
)


# -- sharpe_max_dd ----------------------------------------------------------


def test_sharpe_max_dd_zero_mean_returns_degenerate_zero_not_inf():
    """B3 — zero-mean returns must collapse to sharpe=0.0 + degenerate=true,
    NOT produce ±inf or `-306` style annualization noise."""
    # Sum-zero symmetric returns → mean exactly 0
    rs = [0.01, -0.01, 0.01, -0.01, 0.01, -0.01]
    out = sharpe_max_dd(rs, annualizer="daily_crypto")
    assert out["sharpe"] == 0.0
    assert out["degenerate"] is True
    assert out["reason"] == "zero-mean"
    # Stddev is still populated for forensic inspection
    assert out["stddev"] > 0


def test_sharpe_max_dd_empty_series_is_degenerate():
    out = sharpe_max_dd([], annualizer="daily_crypto")
    assert out["sharpe"] == 0.0
    assert out["n"] == 0
    assert out["degenerate"] is True


def test_sharpe_max_dd_constant_positive_returns_no_stddev():
    """Constant returns → stddev=0 → sharpe=0 + degenerate (zero-stddev)."""
    rs = [0.001] * 10
    out = sharpe_max_dd(rs, annualizer="daily_crypto")
    assert out["sharpe"] == 0.0
    assert out["stddev"] == 0.0


def test_sharpe_max_dd_well_defined_positive():
    """Sane positive series → positive Sharpe, NOT ±inf."""
    rs = [0.01, 0.005, 0.02, -0.005, 0.015, 0.0, 0.01]
    out = sharpe_max_dd(rs, annualizer="daily_crypto")
    assert out["sharpe"] > 0
    assert math.isfinite(out["sharpe"])
    assert out["degenerate"] is False
    # max_drawdown is a non-negative fraction
    assert 0 <= out["max_drawdown"] <= 1.0


def test_sharpe_max_dd_well_defined_negative():
    """Mostly-negative series → negative Sharpe, finite."""
    rs = [-0.01, -0.005, -0.02, 0.001, -0.015, -0.01]
    out = sharpe_max_dd(rs, annualizer="daily_crypto")
    assert out["sharpe"] < 0
    assert math.isfinite(out["sharpe"])
    # DD compounds the losses → significant max_drawdown
    assert out["max_drawdown"] > 0.04


def test_sharpe_max_dd_annualizers_diverge_per_asset_class():
    """daily_crypto (√365) > daily_stocks (√252) for the same series."""
    rs = [0.001, 0.002, 0.001, 0.0015]
    crypto = sharpe_max_dd(rs, annualizer="daily_crypto")
    stocks = sharpe_max_dd(rs, annualizer="daily_stocks")
    # Crypto Sharpe is sqrt(365/252) ≈ 1.203× the stocks Sharpe
    assert crypto["sharpe"] > stocks["sharpe"]
    ratio = crypto["sharpe"] / stocks["sharpe"]
    assert ratio == pytest.approx(math.sqrt(365) / math.sqrt(252), rel=0.001)


def test_sharpe_max_dd_btc_34x_single_name_cap_case():
    """B8 forensic — a series dominated by ONE oversized BTC stake-out
    (single -34× position taking the book down 5.5%) must annualize
    to a legitimately bad Sharpe, NOT to `-306` (which was the legacy
    artifact). The producer just needs to surface a finite, sane number.

    Synth shape: 30 days of tiny noise, one −5.5% day (the BTC blow-up).
    """
    rs = [0.0005, -0.0003, 0.0008, -0.0002] * 7 + [-0.055] + [0.001, 0.0008]
    out = sharpe_max_dd(rs, annualizer="daily_crypto")
    assert math.isfinite(out["sharpe"])
    # The blow-up day dominates → negative Sharpe
    assert out["sharpe"] < 0
    # And NOT the legacy "-306" annualization-noise artifact
    assert out["sharpe"] > -100
    # Max-DD must capture the single-day blow-up
    assert out["max_drawdown"] > 0.04


# -- walk_forward_variance --------------------------------------------------


def test_walk_forward_variance_zero_mean_returns_abs_stddev_not_inf():
    """B3 — legacy `stddev/mean` was `inf` when mean ≈ 0. We return
    `abs(stddev)` instead, mode='abs_stddev', degenerate=True."""
    rs = [0.01, -0.01, 0.01, -0.01]
    out = walk_forward_variance(rs)
    assert math.isfinite(out["dispersion"])
    assert out["mode"] == "abs_stddev"
    assert out["degenerate"] is True
    assert out["dispersion"] == pytest.approx(0.01, abs=1e-9)


def test_walk_forward_variance_nonzero_mean_is_cv():
    """Non-degenerate window → standard CV."""
    rs = [0.01, 0.02, 0.015, 0.012, 0.018]
    out = walk_forward_variance(rs)
    assert out["mode"] == "coef_variation"
    assert out["degenerate"] is False
    assert math.isfinite(out["dispersion"])


def test_walk_forward_variance_empty():
    out = walk_forward_variance([])
    assert out["mode"] == "empty"
    assert out["degenerate"] is True


# -- win_rate ---------------------------------------------------------------


def test_win_rate_classifies_absolute_pnl():
    trades = [
        {"realized_pnl": 100.0},
        {"realized_pnl": -50.0},
        {"realized_pnl": 200.0},
        {"realized_pnl": 0.0},   # scratch
    ]
    out = win_rate(trades)
    assert out["total_trades"] == 4
    assert out["wins"] == 2
    assert out["losses"] == 1
    assert out["scratches"] == 1
    assert out["total_pnl"] == 250.0
    assert out["win_rate"] == 50.0


def test_win_rate_b2_does_not_silently_classify_by_pnl_pct():
    """B2 prevention — the producer-layer `win_rate` must NOT classify
    by pnl_pct in the absence of absolute pnl, otherwise the same
    schema-mismatch bug recurs. Records with only pnl_pct count toward
    `missing_pnl`, not wins/losses."""
    trades = [
        {"pnl_pct": -45.714},
        {"pnl_pct": -28.505},
        {"pnl_pct": -35.294},
        {"pnl_pct": -38.000},
        {"pnl_pct": -32.000},
    ]
    out = win_rate(trades)
    assert out["total_trades"] == 5
    assert out["missing_pnl"] == 5
    assert out["wins"] == 0
    assert out["losses"] == 0
    # win_rate denominator is `classified` (wins+losses+scratches)=0 → 0.0
    assert out["win_rate"] == 0.0


def test_win_rate_accepts_pnl_and_pnl_usd_aliases():
    trades = [
        {"pnl": 10.0},
        {"pnl_usd": -5.0},
        {"realized_pnl": 7.5},
    ]
    out = win_rate(trades)
    assert out["wins"] == 2
    assert out["losses"] == 1
    assert out["total_pnl"] == 12.5


# -- metrics_snapshot bundle ------------------------------------------------


def test_metrics_snapshot_smoke():
    """The bundle producer composes per-side metrics + win-rate without crashing."""
    crypto_rs = [0.001, -0.002, 0.003]
    stocks_rs = [0.0005, 0.0007, 0.0009]
    crypto_tr = [{"realized_pnl": 10.0}, {"realized_pnl": -5.0}]
    stocks_tr = [{"realized_pnl": 20.0}]
    out = metrics_snapshot(crypto_rs, stocks_rs, crypto_tr, stocks_tr)
    assert "crypto" in out and "stocks" in out and "_meta" in out
    assert out["crypto"]["annualizer"] == "daily_crypto"
    assert out["stocks"]["annualizer"] == "daily_stocks"
    assert out["crypto"]["wins"] == 1
    assert out["stocks"]["wins"] == 1
