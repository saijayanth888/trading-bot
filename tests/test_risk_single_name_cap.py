"""Tests for ``quanta_core.risk.single_name_cap.enforce_single_name_cap``.

Covers B8 — the 34× cap-breach must be rejected at entry. Also asserts that
the audit-trail file is opened append-only (spec §5.4 hard constraint).
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from quanta_core.risk.single_name_cap import enforce_single_name_cap


@pytest.fixture(autouse=True)
def _isolate_alerts_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Redirect ``USER_DATA_ROOT`` so each test gets its own jsonl file."""
    monkeypatch.setenv("USER_DATA_ROOT", str(tmp_path))
    (tmp_path / "data").mkdir(parents=True, exist_ok=True)
    yield tmp_path


def _alerts_file(tmp_path: Path) -> Path:
    return tmp_path / "data" / "risk_alerts.jsonl"


# ---------------------------------------------------------------------------
# B8 — the canonical incident
# ---------------------------------------------------------------------------


def test_b8_btc_66k_against_19k_sleeve_rejects(_isolate_alerts_path: Path):
    """The exact scenario named in the spec: BTC $66,212.89 on $19k sleeve
    with a 10% cap must reject, not clip."""
    allowed, reason = enforce_single_name_cap(
        symbol="BTC/USD",
        stake_usd=66212.89,
        sleeve_equity_usd=19000.0,
        cap_pct=0.10,
    )
    assert allowed is False
    assert "BTC/USD" in reason
    # The reason string should expose both the cap and the over-cap ratio.
    assert "cap" in reason.lower()


def test_b8_alert_appended_to_jsonl(_isolate_alerts_path: Path):
    """Reject must append a row to ``risk_alerts.jsonl`` (append-only)."""
    tmp_path = _isolate_alerts_path
    allowed, _ = enforce_single_name_cap(
        symbol="BTC/USD",
        stake_usd=66212.89,
        sleeve_equity_usd=19000.0,
        cap_pct=0.10,
    )
    assert allowed is False

    fp = _alerts_file(tmp_path)
    assert fp.exists(), "risk_alerts.jsonl should be created on first reject"
    rows = [json.loads(line) for line in fp.read_text().splitlines() if line.strip()]
    assert len(rows) == 1
    row = rows[0]
    assert row["kind"] == "single_name_cap_breach"
    assert row["symbol"] == "BTC/USD"
    assert row["severity"] == "critical"  # 66k / (19k * 0.10) = 34.8× — critical
    assert pytest.approx(row["stake_usd"], rel=1e-6) == 66212.89
    assert pytest.approx(row["sleeve_equity_usd"], rel=1e-6) == 19000.0


def test_alert_file_is_append_only_multiple_breaches(_isolate_alerts_path: Path):
    """Two consecutive rejections must produce two rows — no truncation."""
    tmp_path = _isolate_alerts_path
    enforce_single_name_cap("BTC/USD", 66212.89, 19000.0, 0.10)
    enforce_single_name_cap("ETH/USD", 4000.0, 19000.0, 0.10)
    rows = [
        json.loads(line)
        for line in _alerts_file(tmp_path).read_text().splitlines()
        if line.strip()
    ]
    assert len(rows) == 2
    assert {r["symbol"] for r in rows} == {"BTC/USD", "ETH/USD"}


# ---------------------------------------------------------------------------
# Happy-path + edge cases
# ---------------------------------------------------------------------------


def test_within_cap_allows():
    allowed, reason = enforce_single_name_cap(
        symbol="SOFI",
        stake_usd=500.0,
        sleeve_equity_usd=10_000.0,
        cap_pct=0.10,
    )
    assert allowed is True
    assert "OK" in reason or "≤" in reason


def test_close_path_non_positive_stake_passes():
    """Closes/reduces (stake <= 0) are never gated by the cap."""
    allowed, _ = enforce_single_name_cap("BTC/USD", 0.0, 1000.0, 0.10)
    assert allowed is True
    allowed, _ = enforce_single_name_cap("BTC/USD", -100.0, 1000.0, 0.10)
    assert allowed is True


def test_zero_sleeve_equity_rejects():
    """No sleeve equity → reject (can't open positions on empty book)."""
    allowed, reason = enforce_single_name_cap("BTC/USD", 100.0, 0.0, 0.10)
    assert allowed is False
    assert "equity" in reason.lower()


def test_severity_ramp_warning_vs_critical():
    """1.5× breach == warning, 2× breach == critical."""
    # 1.5× — warning
    enforce_single_name_cap("XRP/USD", 1500.0, 10_000.0, 0.10)
    # 5× — critical
    enforce_single_name_cap("ADA/USD", 5000.0, 10_000.0, 0.10)
    fp = Path(_alerts_file_for_env())
    rows = [json.loads(line) for line in fp.read_text().splitlines() if line.strip()]
    sev_by_sym = {r["symbol"]: r["severity"] for r in rows}
    assert sev_by_sym["XRP/USD"] == "warning"
    assert sev_by_sym["ADA/USD"] == "critical"


def test_append_alert_false_skips_disk_write(_isolate_alerts_path: Path):
    """``append_alert=False`` must not touch the audit file."""
    enforce_single_name_cap(
        "BTC/USD", 100_000.0, 1000.0, 0.10, append_alert=False
    )
    assert not _alerts_file(_isolate_alerts_path).exists()


def _alerts_file_for_env() -> str:
    """Helper — recompute the path the module resolves from env."""
    import os
    return str(Path(os.environ["USER_DATA_ROOT"]) / "data" / "risk_alerts.jsonl")
