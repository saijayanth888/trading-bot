"""Unit tests for the lifted risk_gates config block.

Spec: stage/10-risk-gates-yaml — operator-editable risk thresholds.

Covers:
  1. Defaults match the JSON block when both are read fresh
  2. Override via on-disk config JSON loads correctly
  3. POST endpoint validates ranges (rejects out-of-band values)
  4. POST endpoint rolls back atomically on validation failure
     (config.json is never left in a corrupted state)

Run from the repo root:
    pytest tests/test_unified_risk_config.py -v
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

# Add repo root to path so `import user_data.modules.unified_risk` works
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from user_data.modules import unified_risk
from user_data.modules.unified_risk import (
    _RISK_GATE_DEFAULTS,
    _load_risk_gates,
    get_risk_gate,
    evaluate_loss_size_factor,
    evaluate_vix_size_factor,
    evaluate_single_name_cap,
    evaluate_correlation_cap,
)


# ---------------------------------------------------------------------------
# 1. Defaults match the JSON block (the operator-approved set)
# ---------------------------------------------------------------------------


# These are the operator-approved 2026-05-11 values. If anyone edits either
# the defaults dict in unified_risk.py OR the config.json block, this test
# fires until both sides agree again. That's the whole point — these MUST
# stay in lock-step so a missing config block silently falls back to the
# same numbers.
_OPERATOR_APPROVED = {
    "daily_loss_halt_pct":      0.03,
    "weekly_loss_size_cut_pct": 0.05,
    "weekly_loss_size_factor":  0.5,
    "single_name_cap_pct":      0.10,
    "correlation_cap":          0.85,
    "vix_high_multiplier":      2.0,
    "vix_high_min_size_factor": 0.25,
}


class TestDefaultsMatchSpec:
    def test_defaults_dict_matches_operator_approved(self):
        assert _RISK_GATE_DEFAULTS == _OPERATOR_APPROVED

    def test_config_json_block_matches_operator_approved(self):
        """The shipped config.json must carry the same values as the defaults
        so removing the block (rollback) is a true no-op."""
        cfg_path = ROOT / "user_data" / "config.json"
        cfg = json.loads(cfg_path.read_text())
        block = cfg.get("risk_gates")
        assert isinstance(block, dict), "config.json missing risk_gates block"
        for key, expected in _OPERATOR_APPROVED.items():
            assert key in block, f"risk_gates missing key {key}"
            assert block[key] == expected, (
                f"risk_gates.{key}={block[key]} differs from operator-approved {expected}"
            )

    def test_get_risk_gate_returns_default(self):
        assert get_risk_gate("daily_loss_halt_pct") == 0.03
        assert get_risk_gate("correlation_cap") == 0.85

    def test_get_risk_gate_unknown_key_raises(self):
        with pytest.raises(KeyError):
            get_risk_gate("not_a_real_gate")


# ---------------------------------------------------------------------------
# 2. Override via JSON file loads correctly
# ---------------------------------------------------------------------------


class TestConfigOverride:
    def test_block_present_overrides_default(self, tmp_path, monkeypatch):
        cfg = tmp_path / "config.json"
        cfg.write_text(json.dumps({
            "risk_gates": {
                "daily_loss_halt_pct": 0.02,    # tighter
                "single_name_cap_pct": 0.15,    # looser
            }
        }))
        monkeypatch.setattr(unified_risk, "_CONFIG_JSON", cfg)

        gates = _load_risk_gates()
        # Overridden values
        assert gates["daily_loss_halt_pct"] == 0.02
        assert gates["single_name_cap_pct"] == 0.15
        # Untouched values fall back to defaults
        assert gates["correlation_cap"] == _RISK_GATE_DEFAULTS["correlation_cap"]
        assert gates["weekly_loss_size_factor"] == _RISK_GATE_DEFAULTS["weekly_loss_size_factor"]

    def test_missing_block_falls_back_to_defaults(self, tmp_path, monkeypatch):
        """Rollback path: delete the block from config.json → defaults silently
        take over. No errors, no warnings, identical behaviour."""
        cfg = tmp_path / "config.json"
        cfg.write_text(json.dumps({"unrelated": 42}))
        monkeypatch.setattr(unified_risk, "_CONFIG_JSON", cfg)

        gates = _load_risk_gates()
        assert gates == _RISK_GATE_DEFAULTS

    def test_missing_config_file_falls_back_to_defaults(self, tmp_path, monkeypatch):
        monkeypatch.setattr(unified_risk, "_CONFIG_JSON", tmp_path / "nope.json")
        gates = _load_risk_gates()
        assert gates == _RISK_GATE_DEFAULTS

    def test_non_numeric_value_falls_back_to_default(self, tmp_path, monkeypatch):
        cfg = tmp_path / "config.json"
        cfg.write_text(json.dumps({
            "risk_gates": {"daily_loss_halt_pct": "haha not a number"}
        }))
        monkeypatch.setattr(unified_risk, "_CONFIG_JSON", cfg)

        gates = _load_risk_gates()
        # Bad value ignored, default wins → trading loop stays alive
        assert gates["daily_loss_halt_pct"] == _RISK_GATE_DEFAULTS["daily_loss_halt_pct"]

    def test_evaluators_pick_up_overrides(self, tmp_path, monkeypatch):
        cfg = tmp_path / "config.json"
        cfg.write_text(json.dumps({
            "risk_gates": {
                "daily_loss_halt_pct": 0.01,   # very tight halt
            }
        }))
        monkeypatch.setattr(unified_risk, "_CONFIG_JSON", cfg)

        # 1.5% daily loss now halts (was 3% by default)
        verdict = evaluate_loss_size_factor(daily_pnl_pct=-0.015, weekly_pnl_pct=0.0)
        assert verdict["halt"] is True
        assert verdict["size_factor"] == 0.0

    def test_vix_evaluator_picks_up_overrides(self, tmp_path, monkeypatch):
        cfg = tmp_path / "config.json"
        cfg.write_text(json.dumps({
            "risk_gates": {
                "vix_high_multiplier": 1.5,
                "vix_high_min_size_factor": 0.10,
            }
        }))
        monkeypatch.setattr(unified_risk, "_CONFIG_JSON", cfg)

        verdict = evaluate_vix_size_factor(vix_now=30.0, vix_historical=18.0)
        assert verdict["size_factor"] == 0.10

    def test_single_name_and_corr_evaluators_pick_up_overrides(self, tmp_path, monkeypatch):
        cfg = tmp_path / "config.json"
        cfg.write_text(json.dumps({
            "risk_gates": {
                "single_name_cap_pct": 0.05,
                "correlation_cap": 0.5,
            }
        }))
        monkeypatch.setattr(unified_risk, "_CONFIG_JSON", cfg)

        cap = evaluate_single_name_cap(intended_notional=1000, portfolio_equity=10000)
        # 5% of 10k == $500; intended $1000 should be clipped
        assert cap["was_capped"] is True
        assert cap["capped_notional"] == 500.0

        corr = evaluate_correlation_cap(0.6)
        assert corr["allowed"] is False  # 0.6 > new cap of 0.5


# ---------------------------------------------------------------------------
# 3 + 4. POST endpoint range validation + atomic rollback on failure
# ---------------------------------------------------------------------------


@pytest.fixture
def client_and_config(tmp_path, monkeypatch):
    """Spin up a TestClient pointed at an isolated config.json + bypass auth.

    The require_mcp_key dependency is overridden to a no-op so we can exercise
    POST validation without juggling HERMES_MCP_KEY in CI.
    """
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    # Isolated config.json the endpoint will read + write
    cfg_path = tmp_path / "config.json"
    cfg_path.write_text(json.dumps({
        "risk_gates": dict(_OPERATOR_APPROVED),
    }, indent=4))

    backup_root = tmp_path  # config-backup-*.json lands in tmp_path/data

    # Imports must happen AFTER monkeypatch.setenv so module-level constants
    # see the right env. ops_routes.CONFIG_PATH is captured at import time
    # from FREQTRADE_CONFIG_PATH; we override the module attribute directly.
    monkeypatch.setenv("HERMES_MCP_KEY", "test-key-not-used")
    from user_data.dashboard import ops_routes

    monkeypatch.setattr(ops_routes, "CONFIG_PATH", cfg_path)
    monkeypatch.setattr(ops_routes, "USER_DATA_ROOT_FOR_BACKUPS", backup_root)
    # Also point the unified_risk loader at the same file so the GET endpoint's
    # "resolved" field reflects our isolated config.
    monkeypatch.setattr(unified_risk, "_CONFIG_JSON", cfg_path)

    app = FastAPI()
    app.include_router(ops_routes.router)
    # Bypass auth — the dependency is wired through Depends(require_mcp_key)
    app.dependency_overrides[ops_routes.require_mcp_key] = lambda: None

    return TestClient(app), cfg_path


class TestPostEndpointValidation:
    def test_get_returns_current_block_and_schema(self, client_and_config):
        client, _ = client_and_config
        r = client.get("/api/ops/risk_gates")
        assert r.status_code == 200
        body = r.json()
        assert body["status"] == "ok"
        assert body["data"]["risk_gates"] == _OPERATOR_APPROVED
        assert body["data"]["resolved"] == _OPERATOR_APPROVED
        assert "ranges" in body["data"]["schema"]
        # Range dict matches the allowlist keys exactly
        assert set(body["data"]["schema"]["ranges"].keys()) == set(_OPERATOR_APPROVED.keys())

    def test_post_rejects_out_of_range_daily_loss(self, client_and_config):
        client, _ = client_and_config
        r = client.post("/api/ops/risk_gates", json={
            "risk_gates": {"daily_loss_halt_pct": 1.5}  # 150% — absurd
        })
        assert r.status_code == 400
        assert "outside allowed range" in r.text

    def test_post_rejects_unknown_key(self, client_and_config):
        client, _ = client_and_config
        r = client.post("/api/ops/risk_gates", json={
            "risk_gates": {"made_up_param": 0.5}
        })
        assert r.status_code == 400
        assert "unknown risk_gate" in r.text

    def test_post_rejects_non_numeric_value(self, client_and_config):
        client, _ = client_and_config
        r = client.post("/api/ops/risk_gates", json={
            "risk_gates": {"correlation_cap": "high"}
        })
        assert r.status_code == 400
        assert "must be a number" in r.text

    def test_post_rejects_boolean_value(self, client_and_config):
        """Python booleans are isinstance int — must NOT be accepted as numbers."""
        client, _ = client_and_config
        r = client.post("/api/ops/risk_gates", json={
            "risk_gates": {"correlation_cap": True}
        })
        assert r.status_code == 400

    def test_post_rejects_negative_value(self, client_and_config):
        client, _ = client_and_config
        r = client.post("/api/ops/risk_gates", json={
            "risk_gates": {"single_name_cap_pct": -0.10}
        })
        assert r.status_code == 400

    def test_post_rejects_vix_multiplier_below_one(self, client_and_config):
        """vix_high_multiplier < 1.0 means "trip when VIX is BELOW historical"
        which would fire constantly. Range gate catches this."""
        client, _ = client_and_config
        r = client.post("/api/ops/risk_gates", json={
            "risk_gates": {"vix_high_multiplier": 0.5}
        })
        assert r.status_code == 400


class TestPostAtomicRollback:
    def test_validation_failure_does_not_modify_config(self, client_and_config):
        """If validation throws, config.json must be untouched on disk."""
        client, cfg_path = client_and_config
        original = cfg_path.read_text()
        original_parsed = json.loads(original)

        r = client.post("/api/ops/risk_gates", json={
            "risk_gates": {"daily_loss_halt_pct": 99.0}  # rejected
        })
        assert r.status_code == 400

        # File contents IDENTICAL to before the bad request
        on_disk_now = cfg_path.read_text()
        assert on_disk_now == original
        assert json.loads(on_disk_now) == original_parsed

    def test_unknown_key_does_not_modify_config(self, client_and_config):
        client, cfg_path = client_and_config
        original = cfg_path.read_text()

        r = client.post("/api/ops/risk_gates", json={
            "risk_gates": {"daily_loss_halt_pct": 0.04, "bogus_key": 1.0}
        })
        assert r.status_code == 400
        # Even though one of the values WAS valid, the failure of the other
        # means NOTHING gets written.
        assert cfg_path.read_text() == original

    def test_partial_failure_in_batch_rolls_everything_back(self, client_and_config):
        """One good + one bad value → reject the whole batch, no partial write."""
        client, cfg_path = client_and_config
        original = cfg_path.read_text()

        r = client.post("/api/ops/risk_gates", json={
            "risk_gates": {
                "daily_loss_halt_pct": 0.025,        # valid
                "correlation_cap": 5.0,              # invalid (> 1.0)
            }
        })
        assert r.status_code == 400
        assert cfg_path.read_text() == original

    def test_valid_post_writes_through_and_persists(self, client_and_config):
        """Happy path: valid values DO update config.json + return diffs."""
        client, cfg_path = client_and_config

        r = client.post("/api/ops/risk_gates", json={
            "risk_gates": {"daily_loss_halt_pct": 0.025}
        })
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["status"] == "ok"
        # Diff line surfaced for the operator
        assert any("daily_loss_halt_pct" in c for c in body["data"]["changes"])

        on_disk = json.loads(cfg_path.read_text())
        assert on_disk["risk_gates"]["daily_loss_halt_pct"] == 0.025
        # Other keys untouched
        assert on_disk["risk_gates"]["correlation_cap"] == 0.85

    def test_noop_post_returns_ok_no_backup(self, client_and_config):
        """Submitting unchanged values is a no-op — no diff, no rewrite."""
        client, cfg_path = client_and_config
        before = cfg_path.read_text()

        r = client.post("/api/ops/risk_gates", json={
            "risk_gates": {"daily_loss_halt_pct": 0.03}  # already the value
        })
        assert r.status_code == 200
        assert r.json()["data"]["changes"] == []
        assert cfg_path.read_text() == before  # untouched
