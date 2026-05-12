"""Tests for ``quanta_core.hermes.weekly_publisher``."""

from __future__ import annotations

import json
from datetime import date, timedelta

import pytest

from quanta_core.hermes import weekly_publisher as wp
from quanta_core.hermes._common import HermesError
from tests.hermes.conftest import FakeLedger, FakeNotifier, make_trade

# ---------------------------------------------------------------------------
# Week boundary
# ---------------------------------------------------------------------------


def test_iso_week_bounds_for_tuesday():
    monday, sunday, year, week = wp.iso_week_bounds(date(2026, 5, 12))
    assert monday == date(2026, 5, 11)
    assert sunday == date(2026, 5, 17)
    assert year == 2026
    assert week == 20


def test_iso_week_bounds_for_sunday():
    monday, sunday, _year, _week = wp.iso_week_bounds(date(2026, 5, 17))
    assert monday == date(2026, 5, 11)
    assert sunday == date(2026, 5, 17)


# ---------------------------------------------------------------------------
# Quality gates (advisory)
# ---------------------------------------------------------------------------


def test_gate_reconciliation_pass():
    trades = [make_trade(pnl=10.0), make_trade(pnl=-2.0)]
    g = wp.gate_reconciliation(trades, broker_delta=8.0)
    assert g.passed is True


def test_gate_reconciliation_fail():
    trades = [make_trade(pnl=10.0)]
    g = wp.gate_reconciliation(trades, broker_delta=5.0)
    assert g.passed is False
    assert "diff=$5.00" in g.message


def test_gate_reconciliation_skipped_when_no_broker():
    g = wp.gate_reconciliation([make_trade()], broker_delta=None)
    assert g.passed is False
    assert "broker_delta_unavailable" in g.message


def test_gate_reflector_daily_pass():
    monday = date(2026, 5, 11)
    sunday = date(2026, 5, 17)
    seen = [monday + timedelta(days=i) for i in range(5)]  # Mon-Fri
    g = wp.gate_reflector_daily(seen, monday, sunday)
    assert g.passed is True


def test_gate_reflector_daily_fail():
    monday = date(2026, 5, 11)
    sunday = date(2026, 5, 17)
    seen = [monday]  # only Monday
    g = wp.gate_reflector_daily(seen, monday, sunday)
    assert g.passed is False
    assert "missed 4 days" in g.message


def test_gate_risk_anchor_pass():
    g = wp.gate_risk_anchor(1000.0, 1000.0)
    assert g.passed is True


def test_gate_risk_anchor_fail():
    g = wp.gate_risk_anchor(1000.0, 1100.0)
    assert g.passed is False
    assert "drift=$100.00" in g.message


def test_gate_risk_anchor_skipped():
    g = wp.gate_risk_anchor(None, None)
    assert g.passed is False


# ---------------------------------------------------------------------------
# Adapter promotions read
# ---------------------------------------------------------------------------


def test_read_adapters_promoted_happy(tmp_path):
    p = tmp_path / "last_lora_promotion.json"
    p.write_text(
        json.dumps(
            {
                "promotions": [
                    {"role": "arbiter", "pareto_pass": True, "from": "v16", "to": "v17"},
                    {"role": "reflector", "pareto_pass": False, "kept_champion": "v9"},
                ]
            }
        )
    )
    out = wp.read_adapters_promoted(tmp_path)
    assert out == ["arbiter: v16→v17"]


def test_read_adapters_promoted_missing_file(tmp_path):
    assert wp.read_adapters_promoted(tmp_path) == []


def test_read_adapters_promoted_bad_json(tmp_path):
    (tmp_path / "last_lora_promotion.json").write_text("not json")
    assert wp.read_adapters_promoted(tmp_path) == []


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def test_render_post_no_trades():
    week = wp.WeekContext(
        iso_year=2026,
        iso_week=20,
        monday=date(2026, 5, 11),
        sunday=date(2026, 5, 17),
        trades=[],
        open_positions=[],
        lessons_added=0,
        adapters_promoted=[],
        regime_mix={},
    )
    out = wp.render_post(week, gates=[])
    assert "Quanta · Week 2026-W20" in out
    assert "No trades closed this week" in out
    assert "**Net P&L** · $0.00" in out


def test_render_post_with_trades():
    trade = make_trade()
    week = wp.WeekContext(
        iso_year=2026,
        iso_week=20,
        monday=date(2026, 5, 11),
        sunday=date(2026, 5, 17),
        trades=[trade],
        open_positions=[],
        lessons_added=3,
        adapters_promoted=["arbiter: v16→v17"],
        regime_mix={"trending_up": 1},
    )
    out = wp.render_post(week, gates=[])
    assert "1. BTC/USD · long" in out
    assert "**P&L**   $10.00" in out
    assert "trending_up=1" in out
    assert "arbiter: v16→v17" in out


def test_render_post_with_gate_warning():
    """Failing gate prepends a warning banner — anti-cherry-pick (doc §6)."""

    week = wp.WeekContext(
        iso_year=2026,
        iso_week=20,
        monday=date(2026, 5, 11),
        sunday=date(2026, 5, 17),
        trades=[],
        open_positions=[],
        lessons_added=0,
        adapters_promoted=[],
        regime_mix={},
    )
    gates = [wp.GateResult("reconciliation", False, "off by $1.50")]
    out = wp.render_post(week, gates=gates)
    assert out.startswith("> ❗ **Data integrity issue this week.**")
    assert "reconciliation" in out
    assert "off by $1.50" in out


def test_render_post_losing_week_no_apology():
    """Anti-cherry-pick: losing-week render must contain no adjective branch."""

    trade = make_trade(pnl=-200.0, pnl_pct=-15.0)
    week = wp.WeekContext(
        iso_year=2026,
        iso_week=20,
        monday=date(2026, 5, 11),
        sunday=date(2026, 5, 17),
        trades=[trade],
        open_positions=[],
        lessons_added=0,
        adapters_promoted=[],
        regime_mix={"trending_down": 1},
    )
    out = wp.render_post(week, gates=[])
    forbidden = ["unfortunately", "disappointing", "tough week", "rough"]
    for word in forbidden:
        assert word not in out.lower(), f"forbidden adjective '{word}' leaked"
    # Negative P&L is shown in the headline as-is.
    assert "$-200.00" in out


# ---------------------------------------------------------------------------
# Atomic write
# ---------------------------------------------------------------------------


def test_write_post_atomic(tmp_path):
    path = tmp_path / "subdir" / "2026-W20.md"
    wp.write_post(path, "hello", force=False)
    assert path.read_text() == "hello"


def test_write_post_refuses_overwrite(tmp_path):
    path = tmp_path / "2026-W20.md"
    path.write_text("existing")
    with pytest.raises(HermesError):
        wp.write_post(path, "new", force=False)


def test_write_post_force(tmp_path):
    path = tmp_path / "2026-W20.md"
    path.write_text("existing")
    wp.write_post(path, "new", force=True)
    assert path.read_text() == "new"


# ---------------------------------------------------------------------------
# Missed-week audit (anti-cherry-pick #4)
# ---------------------------------------------------------------------------


def test_missed_weeks_all_present(tmp_path):
    weekly = tmp_path / "weekly"
    weekly.mkdir()
    for tag in ("2026-W18", "2026-W19", "2026-W20"):
        (weekly / f"{tag}.md").write_text("x")
    missed = wp.missed_weeks(tmp_path, date(2026, 5, 4), date(2026, 5, 17))
    assert missed == []


def test_missed_weeks_detects_gap(tmp_path):
    weekly = tmp_path / "weekly"
    weekly.mkdir()
    (weekly / "2026-W18.md").write_text("x")
    (weekly / "2026-W20.md").write_text("x")
    missed = wp.missed_weeks(tmp_path, date(2026, 4, 27), date(2026, 5, 17))
    assert "2026-W19" in missed


def test_missed_weeks_empty_dir(tmp_path):
    missed = wp.missed_weeks(tmp_path, date(2026, 5, 4), date(2026, 5, 11))
    assert len(missed) >= 1


# ---------------------------------------------------------------------------
# End-to-end entrypoint
# ---------------------------------------------------------------------------


def test_run_renders_file_and_writes_state(
    clean_env, state_root, repo_root_fake, monkeypatch, fake_ledger: FakeLedger
):
    monkeypatch.setattr(wp, "LedgerClient", lambda *a, **k: fake_ledger)
    monkeypatch.setattr(wp, "SlackNotifier", lambda *a, **k: FakeNotifier())
    # Run for the current week (no trades — empty render is still valid)
    code = wp.run(["--week", "current"])
    assert code == 0
    state = json.loads((state_root / "weekly_publish_state.json").read_text())
    assert state["trade_count"] == 0
    # Markdown file written under repo_root_fake/docs/weekly/
    weekly_dir = repo_root_fake / "docs" / "weekly"
    files = list(weekly_dir.glob("*.md"))
    assert len(files) == 1
    body = files[0].read_text()
    assert "Quanta · Week" in body


def test_run_audit_mode(clean_env, state_root, repo_root_fake, monkeypatch):
    monkeypatch.setattr(wp, "SlackNotifier", lambda *a, **k: FakeNotifier())
    code = wp.run(["--audit", "--audit-since", "2026-05-04"])
    assert code == 0
    state = json.loads((state_root / "weekly_publish_state.json").read_text())
    assert state["audit"] is True
    assert "missed_weeks" in state


def test_run_force_overwrites_existing(
    clean_env, state_root, repo_root_fake, monkeypatch, fake_ledger
):
    monkeypatch.setattr(wp, "LedgerClient", lambda *a, **k: fake_ledger)
    monkeypatch.setattr(wp, "SlackNotifier", lambda *a, **k: FakeNotifier())

    # First run creates the file
    code1 = wp.run(["--week", "current"])
    assert code1 == 0
    # Second run without --force returns 1
    code2 = wp.run(["--week", "current"])
    assert code2 == 1
    # With --force, second run succeeds
    code3 = wp.run(["--week", "current", "--force"])
    assert code3 == 0


def test_anti_cherry_pick_no_skip_flag():
    """There is no ``--skip`` flag in the argparser — verifies doc §5 rule #3."""

    parser_args = wp._parse_args(["--week", "current"])
    assert not hasattr(parser_args, "skip")
    # Confirm argparser rejects an attempted skip
    with pytest.raises(SystemExit):
        wp._parse_args(["--skip"])


def test_resolve_reference_date_current():
    out = wp._resolve_reference_date("current")
    assert isinstance(out, date)


def test_resolve_reference_date_previous():
    out_cur = wp._resolve_reference_date("current")
    out_prev = wp._resolve_reference_date("previous")
    assert (out_cur - out_prev).days == 7


def test_resolve_reference_date_explicit():
    out = wp._resolve_reference_date("2026-W20")
    assert out == date(2026, 5, 11)


def test_resolve_reference_date_bad():
    with pytest.raises(HermesError):
        wp._resolve_reference_date("nonsense")
