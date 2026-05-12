"""Tests for ``quanta_core.hermes.post_mortem``."""

from __future__ import annotations

import json
from datetime import date, datetime, timezone

from quanta_core.hermes import post_mortem
from tests.hermes.conftest import FakeLedger, FakeNotifier, FakeOllama, make_trade


def test_cluster_losses_groups_by_regime_and_exit_reason():
    a = make_trade(trade_id="a", pnl=-10.0, regime="bear", raw={"exit_reason": "stop"})
    b = make_trade(trade_id="b", pnl=-5.0, regime="bear", raw={"exit_reason": "stop"})
    c = make_trade(trade_id="c", pnl=-3.0, regime="bull", raw={"exit_reason": "trail"})
    d_winner = make_trade(trade_id="d", pnl=20.0)
    buckets = post_mortem.cluster_losses([a, b, c, d_winner])
    assert len(buckets) == 2
    # sort order: most-negative first
    assert buckets[0].regime == "bear"
    assert buckets[0].count == 2
    assert buckets[0].total_pnl == -15.0


def test_cluster_losses_empty_when_all_winners():
    trades = [make_trade(pnl=5.0), make_trade(pnl=2.0)]
    assert post_mortem.cluster_losses(trades) == []


def test_buckets_to_prompt_empty():
    assert "No losses" in post_mortem.buckets_to_prompt([])


def test_buckets_to_prompt_truncates_to_top_3():
    buckets = [
        post_mortem.LossBucket(f"r{i}", "stop", 1, -float(i + 1), [])
        for i in range(5)
    ]
    out = post_mortem.buckets_to_prompt(buckets)
    assert out.count("\n") == 3  # header + 3 lines
    assert "r0" in out


def test_render_post_mortem_md_no_losses():
    out = post_mortem.render_post_mortem_md(
        date(2026, 5, 6), date(2026, 5, 12), [], None
    )
    assert "## Weekly Post-mortem" in out
    assert "2026-05-06 → 2026-05-12" in out
    assert "_No losing trades to cluster._" in out


def test_render_post_mortem_md_with_llm():
    buckets = [post_mortem.LossBucket("bear", "stop", 2, -15.0, ["a", "b"])]
    out = post_mortem.render_post_mortem_md(
        date(2026, 5, 6), date(2026, 5, 12), buckets, "Bucket 1 failed because…"
    )
    assert "bear" in out
    assert "Bucket 1 failed because" in out


def test_run_writes_state_and_decisions(
    clean_env, state_root, repo_root_fake, monkeypatch
):
    end = date(2026, 5, 12)
    ledger = FakeLedger()
    ledger.rows.append(
        make_trade(
            pnl=-10.0,
            pnl_pct=-2.5,
            regime="bear",
            exit_ts=datetime(2026, 5, 10, tzinfo=timezone.utc),
            raw={"exit_reason": "stop"},
        )
    )
    ollama = FakeOllama(responses=["Stop-outs in bear regime — review entries."])
    notifier = FakeNotifier()

    monkeypatch.setattr(post_mortem, "LedgerClient", lambda *a, **k: ledger)
    monkeypatch.setattr(post_mortem, "OllamaClient", lambda *a, **k: ollama)
    monkeypatch.setattr(post_mortem, "SlackNotifier", lambda *a, **k: notifier)

    code = post_mortem.run(["--end", end.isoformat()])
    assert code == 0
    state = json.loads((state_root / "last_post_mortem.json").read_text())
    assert state["trade_count"] == 1
    assert state["bucket_count"] == 1
    assert state["top_buckets"][0]["regime"] == "bear"

    decisions = repo_root_fake / "stocks" / "memory" / "decisions.md"
    assert decisions.exists()
    body = decisions.read_text()
    assert "Weekly Post-mortem" in body
    assert "Stop-outs in bear regime" in body
    assert any("post-mortem" in p for p in notifier.posts)


def test_run_dry_run_skips_decisions(
    clean_env, state_root, repo_root_fake, monkeypatch
):
    ledger = FakeLedger()
    ledger.rows.append(
        make_trade(pnl=-1.0, exit_ts=datetime(2026, 5, 10, tzinfo=timezone.utc))
    )
    monkeypatch.setattr(post_mortem, "LedgerClient", lambda *a, **k: ledger)
    monkeypatch.setattr(post_mortem, "OllamaClient", lambda *a, **k: FakeOllama(responses=["x"]))
    monkeypatch.setattr(post_mortem, "SlackNotifier", lambda *a, **k: FakeNotifier())

    code = post_mortem.run(["--dry-run", "--end", "2026-05-12"])
    assert code == 0
    decisions = repo_root_fake / "stocks" / "memory" / "decisions.md"
    assert not decisions.exists()


def test_run_handles_empty_window(
    clean_env, state_root, repo_root_fake, monkeypatch
):
    monkeypatch.setattr(post_mortem, "LedgerClient", lambda *a, **k: FakeLedger())
    monkeypatch.setattr(post_mortem, "OllamaClient", lambda *a, **k: FakeOllama())
    monkeypatch.setattr(post_mortem, "SlackNotifier", lambda *a, **k: FakeNotifier())

    code = post_mortem.run(["--end", "2026-05-12"])
    assert code == 0
    state = json.loads((state_root / "last_post_mortem.json").read_text())
    assert state["trade_count"] == 0
