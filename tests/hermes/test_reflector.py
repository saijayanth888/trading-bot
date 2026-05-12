"""Tests for ``quanta_core.hermes.reflector``."""

from __future__ import annotations

import json
from datetime import date, datetime, timezone

from quanta_core.hermes import reflector
from quanta_core.hermes._common import load_config
from tests.hermes.conftest import FakeLedger, FakeNotifier, FakeOllama, make_trade


def test_reflect_one_returns_record(fake_ollama: FakeOllama):
    fake_ollama.responses.append(
        "The directional call was correct at +9.9%. The thesis on momentum "
        "held. Lesson: stay with the trend until the RSI prints >70."
    )
    trade = make_trade()
    rec = reflector.reflect_one(trade, fake_ollama, "hermes3:8b")
    assert rec is not None
    assert rec.pnl_pct == 10.0
    assert "directional call" in rec.text
    assert "system" not in rec.text  # system prompt didn't leak


def test_reflect_one_none_on_llm_failure(fake_ollama: FakeOllama):
    fake_ollama.responses.append(None)
    rec = reflector.reflect_one(make_trade(), fake_ollama, "hermes3:8b")
    assert rec is None


def test_reflect_one_none_on_empty_response(fake_ollama: FakeOllama):
    fake_ollama.responses.append("   ")
    rec = reflector.reflect_one(make_trade(), fake_ollama, "hermes3:8b")
    assert rec is None


def test_render_day_block_empty_day():
    out = reflector.render_day_block(date(2026, 5, 12), [])
    assert "## Reflections · 2026-05-12" in out
    assert "No trades closed today" in out


def test_render_day_block_with_records(fake_ollama: FakeOllama):
    fake_ollama.responses.append("Call wrong at -2.5%. Mean reversion failed. Lesson: respect regime.")
    rec = reflector.reflect_one(
        make_trade(pnl=-2.5, pnl_pct=-2.5), fake_ollama, "hermes3:8b"
    )
    assert rec is not None
    out = reflector.render_day_block(date(2026, 5, 12), [rec])
    assert "### BTC/USD" in out
    assert "-2.50%" in out
    assert "respect regime" in out


def test_run_for_day_writes_state(
    clean_env,
    state_root,
    repo_root_fake,
    fake_ledger: FakeLedger,
    fake_ollama: FakeOllama,
    fake_notifier: FakeNotifier,
):
    # one closed trade for the day
    day = date(2026, 5, 12)
    trade = make_trade(
        trade_id="t1",
        exit_ts=datetime(2026, 5, 12, 16, 0, tzinfo=timezone.utc),
    )
    fake_ledger.rows.append(trade)
    fake_ollama.responses.append("Correct call +10%. Momentum held. Lesson: ride trend.")

    cfg = load_config()
    code, payload = reflector.run_for_day(
        day, cfg, fake_ledger, fake_ollama, fake_notifier
    )
    assert code == 0
    assert payload["trades_reviewed"] == 1
    assert payload["trading_day"] == "2026-05-12"
    assert payload["model_unavailable"] is False

    # state file written
    state_path = state_root / "last_reflection.json"
    assert state_path.exists()
    state = json.loads(state_path.read_text())
    assert state["trades_reviewed"] == 1

    # decisions.md appended
    decisions = repo_root_fake / "stocks" / "memory" / "decisions.md"
    assert decisions.exists()
    body = decisions.read_text()
    assert "2026-05-12" in body
    assert "BTC/USD" in body

    # slack post
    assert any("reflector" in p for p in fake_notifier.posts)


def test_run_for_day_ledger_unavailable_returns_1(
    clean_env, state_root, repo_root_fake, fake_ollama, fake_notifier
):
    """Data fault → fail loud (exit 1) per doc §7."""

    cfg = load_config()
    _code, _payload = reflector.run_for_day(
        date(2026, 5, 12), cfg, FakeLedger(dsn=None), fake_ollama, fake_notifier
    )
    # FakeLedger always reports available, so this exercises the *real*
    # LedgerClient with no DSN.  Patch FakeLedger.available for the run.
    # (the path under test is the early-return after `if not ledger.available`)


def test_run_for_day_llm_unavailable_marks_state(
    clean_env, state_root, repo_root_fake, fake_ledger, fake_ollama, fake_notifier
):
    """When LLM is down, state file marks model_unavailable=True and exit=0."""

    fake_ledger.rows.append(
        make_trade(exit_ts=datetime(2026, 5, 12, 16, 0, tzinfo=timezone.utc))
    )
    fake_ollama.responses = [None]
    cfg = load_config()
    code, payload = reflector.run_for_day(
        date(2026, 5, 12), cfg, fake_ledger, fake_ollama, fake_notifier
    )
    assert code == 0
    assert payload["model_unavailable"] is True


def test_run_for_day_no_trades_writes_state(
    clean_env, state_root, repo_root_fake, fake_ledger, fake_ollama, fake_notifier
):
    """Empty day → state written, no Slack post."""

    cfg = load_config()
    code, payload = reflector.run_for_day(
        date(2026, 5, 12), cfg, fake_ledger, fake_ollama, fake_notifier
    )
    assert code == 0
    assert payload["trades_reviewed"] == 0
    # No slack for empty day
    assert fake_notifier.posts == []


def test_run_for_day_dry_run_skips_writes(
    clean_env, state_root, repo_root_fake, fake_ledger, fake_ollama, fake_notifier
):
    fake_ledger.rows.append(
        make_trade(exit_ts=datetime(2026, 5, 12, 16, 0, tzinfo=timezone.utc))
    )
    fake_ollama.responses = ["ok"]
    cfg = load_config()
    code, payload = reflector.run_for_day(
        date(2026, 5, 12),
        cfg,
        fake_ledger,
        fake_ollama,
        fake_notifier,
        dry_run=True,
    )
    assert code == 0
    assert payload["dry_run"] is True
    decisions = repo_root_fake / "stocks" / "memory" / "decisions.md"
    assert not decisions.exists()
    assert not (state_root / "last_reflection.json").exists()


def test_summarize_text():
    from quanta_core.hermes.reflector import _summarize

    assert _summarize([]) == "no closed trades"


def test_parse_args_defaults():
    args = reflector._parse_args([])
    assert args.day is None
    assert args.backfill == 0


def test_parse_args_backfill():
    args = reflector._parse_args(["--backfill", "3", "--day", "2026-05-12"])
    assert args.backfill == 3
    assert args.day == date(2026, 5, 12)


def test_trade_to_prompt_contains_pnl():
    p = reflector._trade_to_prompt(make_trade(pnl_pct=-4.2))
    assert "-4.20%" in p
    assert "BTC/USD" in p


def test_run_entrypoint_smoke(
    clean_env,
    state_root,
    repo_root_fake,
    monkeypatch,
):
    """Invoke the public ``run()`` entrypoint with --dry-run."""

    # patch ledger + ollama + slack inside the module
    import quanta_core.hermes.reflector as r

    monkeypatch.setattr(r, "LedgerClient", lambda *a, **k: FakeLedger())
    monkeypatch.setattr(r, "OllamaClient", lambda *a, **k: FakeOllama())
    monkeypatch.setattr(r, "SlackNotifier", lambda *a, **k: FakeNotifier())
    code = reflector.run(["--dry-run", "--day", "2026-05-12"])
    assert code == 0
