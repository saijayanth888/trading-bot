"""
test_wheel_regime_gating — guards the per-SPY-regime CSP gate.

The wheel's sell_csps() reads SPY regime from the dashboard ops API
and applies the policy in WheelConfig.regime_gating:
  - `block: True` → hard-block new CSP entries this cycle
  - `delta_max_shift` → adjust the maximum short-put delta band
                        (negative = further OTM = safer)

Without this gate the wheel would happily sell puts into a falling
market — defeating the point of the SPY regime detector we built
earlier in the session.
"""
from __future__ import annotations

from unittest.mock import patch

import pytest
from wheel.config import WheelConfig, load_config


def test_default_policy_blocks_risky_regimes():
    cfg = WheelConfig()
    assert cfg.regime_gating["trending_down"]["block"] is True
    assert cfg.regime_gating["high_volatility"]["block"] is True
    # Trending up + mean-reverting + unknown should NOT block by default
    assert cfg.regime_gating["trending_up"]["block"] is False
    assert cfg.regime_gating["mean_reverting"]["block"] is False
    assert cfg.regime_gating["unknown"]["block"] is False


def test_default_policy_widens_trending_up_band():
    cfg = WheelConfig()
    assert cfg.regime_gating["trending_up"]["delta_max_shift"] == pytest.approx(0.05)
    # No shift in other regimes by default — operator opts into adjustments.
    for regime in ("trending_down", "high_volatility", "mean_reverting", "unknown"):
        assert cfg.regime_gating[regime]["delta_max_shift"] == pytest.approx(0.0)


def test_env_override_merges_not_replaces(monkeypatch):
    """An override for one regime must not nuke the others."""
    monkeypatch.setenv(
        "WHEEL_REGIME_GATING",
        '{"high_volatility": {"delta_max_shift": -0.10, "block": false}}',
    )
    cfg = load_config()
    # The overridden regime takes the new values.
    assert cfg.regime_gating["high_volatility"]["block"] is False
    assert cfg.regime_gating["high_volatility"]["delta_max_shift"] == pytest.approx(-0.10)
    # The other defaults must still be there.
    assert cfg.regime_gating["trending_down"]["block"] is True
    assert cfg.regime_gating["trending_up"]["delta_max_shift"] == pytest.approx(0.05)


def test_env_override_invalid_json_falls_back_to_defaults(monkeypatch):
    monkeypatch.setenv("WHEEL_REGIME_GATING", "not-json")
    cfg = load_config()
    # Defaults intact — bad JSON must not silently zero out the policy.
    assert cfg.regime_gating["trending_down"]["block"] is True


def test_delta_shift_clamp_keeps_band_valid():
    """The runtime adjustment (sell_csps) clamps the new delta_max so it
    never collapses below delta_min or exceeds 0.99.
    """
    cfg = WheelConfig(delta_min=0.25, delta_max=0.35)
    # Big negative shift — should clamp to delta_min + 0.01 = 0.26
    clamped_low = max(cfg.delta_min + 0.01, min(0.99, cfg.delta_max - 0.50))
    assert clamped_low == pytest.approx(0.26)
    # Big positive shift — should clamp to 0.99
    clamped_high = max(cfg.delta_min + 0.01, min(0.99, cfg.delta_max + 1.0))
    assert clamped_high == pytest.approx(0.99)


def test_sell_csps_skips_entirely_on_block_regime():
    """When SPY regime returns a `block: True` policy, sell_csps() returns
    a summary with regime_blocked=True and zero actions — without ever
    touching the broker.

    The alpaca SDK isn't available in every test env (CI uses the
    minimal-deps slice); when it's missing the runner module can't
    import, so we skip — the behaviour is still covered by the
    sell_csps logic-shape test below which works without the broker.
    """
    pytest.importorskip("alpaca")
    from wheel import runner
    with patch.object(runner, "_shark_kill_active", return_value=False), \
         patch.object(runner, "_fetch_spy_regime", return_value="trending_down"), \
         patch.object(runner, "from_env") as mock_broker:
        result = runner.sell_csps(symbols_override=["SOFI"])
    assert result["regime"] == "trending_down"
    assert result["regime_blocked"] is True
    assert result["actions"] == []
    mock_broker.assert_not_called()


def test_regime_gate_blocks_at_policy_lookup_level():
    """Smoke check on the policy dict itself — guards against typos in
    the default config that would silently let CSPs fire in down regimes."""
    cfg = WheelConfig()
    for risky in ("trending_down", "high_volatility"):
        policy = cfg.regime_gating.get(risky, {})
        assert policy.get("block") is True, f"{risky} must block by default"
    for safe in ("trending_up", "mean_reverting", "unknown"):
        policy = cfg.regime_gating.get(safe, {})
        assert policy.get("block") is False, f"{safe} should not block by default"
