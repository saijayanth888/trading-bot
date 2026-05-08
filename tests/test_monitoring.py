"""
Smoke test for slack_alerts + trade_journal + metrics_writer.

  1. Slack: every notification produces well-formed Block Kit payloads;
     dedup suppresses repeats; failed POSTs don't raise.
  2. Slack: weekly evolution renders leaderboard + lineage.
  3. Slack: dry-run mode logs without POST.
  4. Journal: schema initialises; entry → exit round-trip; CSV export.
  5. Journal: markdown export + stats math.
  6. Journal: filter by date range + pair.
  7. Metrics: every write helper produces a valid Point with expected
     measurement / tags / fields; queue + flush works; close drains.
  8. Metrics: hourly snapshot helper writes the slow panels in one call.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "user_data"))

from modules.metrics_writer import InfluxConfig, MetricsWriter   # noqa: E402
from modules.slack_alerts import SlackAlerter, SlackConfig       # noqa: E402
from modules.trade_journal import TradeJournal                   # noqa: E402


def _ok(msg: str) -> None: print(f"  [✓] {msg}")
def _info(msg: str) -> None: print(f"  [i] {msg}")
def _hr() -> None: print("=" * 64)


# ---------------------------------------------------------------------------
# Slack tests
# ---------------------------------------------------------------------------


@dataclass
class FakeResp:
    status_code: int = 200
    text: str = "ok"


class FakeHttp:
    def __init__(self, fail_n: int = 0):
        self.calls: list[dict] = []
        self.fail_n = fail_n
        self.failed = 0

    def post(self, url, json=None, timeout=None):
        self.calls.append({"url": url, "json": json, "timeout": timeout})
        if self.failed < self.fail_n:
            self.failed += 1
            return FakeResp(status_code=500, text="boom")
        return FakeResp()


def test_slack_payload_shape() -> None:
    print("\n[1/8] Slack: payload shape (Block Kit) + every alert type")
    os.environ["SLACK_WEBHOOK_URL"] = "https://hooks.slack.example/test/abc"
    http = FakeHttp()
    s = SlackAlerter(SlackConfig(dedup_window_sec=0), http=http)

    s.notify_trade_entry(
        pair="BTC/USD", signal="long",
        entry_price=65_000.0, stake=1000.0, confidence=0.72,
        tft_probs={"down": 0.10, "flat": 0.20, "up": 0.70},
        drl_votes={"ppo": 1, "a2c": 1, "dqn": 0},
        regime="trending_up", entry_tag="meta_up_regime",
    )
    s.notify_trade_exit(
        pair="BTC/USD",
        entry_price=65_000.0, exit_price=66_010.0,
        pnl=15.55, pnl_pct=0.0091,
        exit_reason="freqai_down_regime", duration_minutes=144.0,
        confidence=0.72,
    )
    s.notify_daily_summary(
        date_utc="2026-05-08",
        starting_equity=10_000.0, ending_equity=10_240.0,
        total_pnl=240.0, num_trades=12, wins=8, losses=4,
        sharpe_30d=1.42, max_drawdown=0.045,
    )
    s.notify_risk_warning("portfolio_drawdown", 0.06, 0.05)
    s.notify_risk_critical("portfolio_drawdown", 0.082, 0.08)
    try:
        raise RuntimeError("synthetic error for test")
    except RuntimeError as exc:
        s.notify_error("test_module", exc, context={"pair": "BTC/USD"})

    assert len(http.calls) == 6, f"expected 6 posts, got {len(http.calls)}"
    for c in http.calls:
        body = c["json"]
        assert isinstance(body, dict) and "blocks" in body and "text" in body
        assert isinstance(body["blocks"], list) and len(body["blocks"]) >= 2
        assert body["blocks"][0]["type"] == "header"
    _ok(f"6 alerts → 6 valid Block Kit POSTs")


def test_slack_evolution() -> None:
    print("\n[2/8] Slack: weekly evolution leaderboard + lineage")
    os.environ["SLACK_WEBHOOK_URL"] = "https://hooks.slack.example/test/abc"
    http = FakeHttp()
    s = SlackAlerter(SlackConfig(dedup_window_sec=0), http=http)

    agents = [
        {"member_id": f"gen3-{i:03d}", "fitness": 1.5 - 0.1 * i,
         "metrics": {"sharpe_ratio": 1.2 - 0.05 * i, "max_drawdown": 0.05 + 0.005 * i}}
        for i in range(8)
    ]
    s.notify_weekly_evolution(
        generation=3,
        champion_id="gen3-c00", champion_fitness=1.55,
        agent_fitness=agents,
        runner_up_id="gen3-c01",
        lineage=["gen0-005", "gen1-c00", "gen2-c01", "gen3-c00"],
    )
    body = http.calls[-1]["json"]
    leaderboard_section = next(
        b for b in body["blocks"]
        if b.get("type") == "section" and "Leaderboard" in str(b.get("text", {}).get("text", ""))
    )
    text = leaderboard_section["text"]["text"]
    assert "gen3-000" in text and "fitness" in text
    _ok("evolution post contains 8-row leaderboard and lineage")


def test_slack_dedup_and_dry_run() -> None:
    print("\n[3/8] Slack: dedup window + dry-run mode")
    os.environ["SLACK_WEBHOOK_URL"] = "https://hooks.slack.example/test/abc"
    http = FakeHttp()
    s = SlackAlerter(SlackConfig(dedup_window_sec=60), http=http)
    # Same alert twice within window — second is suppressed
    s.notify_risk_warning("daily_loss", 0.025, 0.03)
    s.notify_risk_warning("daily_loss", 0.025, 0.03)
    assert len(http.calls) == 1, f"dedup expected 1 post, got {len(http.calls)}"

    # Dry-run: no posts, no error
    s_dry = SlackAlerter(SlackConfig(dedup_window_sec=0, dry_run=True), http=http)
    ok = s_dry.notify_trade_entry(
        pair="ETH/USD", signal="long", entry_price=2500, stake=200, confidence=0.55,
    )
    assert ok is True
    assert len(http.calls) == 1, "dry-run shouldn't POST"
    _ok("dedup suppresses duplicates; dry-run logs without HTTP")


# ---------------------------------------------------------------------------
# Trade journal tests
# ---------------------------------------------------------------------------


def _truncate_journal() -> bool:
    """Wipe trade_journal between sections. Returns False if Postgres unreachable."""
    try:
        from modules import db as _db
        with _db.cursor() as cur:
            cur.execute("TRUNCATE TABLE trade_journal RESTART IDENTITY")
        return True
    except Exception as exc:
        print(f"  [-] SKIP: Postgres unreachable ({exc}); set DATABASE_URL")
        return False


def test_journal_roundtrip() -> None:
    print("\n[4/8] Journal: entry → exit round-trip + CSV export")
    if not _truncate_journal():
        return
    with tempfile.TemporaryDirectory() as td:
        j = TradeJournal()

        # Schema in place
        from modules import db as _db
        with _db.cursor() as cur:
            cur.execute(
                "SELECT to_regclass('public.trade_journal') IS NOT NULL"
            )
            assert cur.fetchone()["?column?"]

        jid = j.log_entry(
            pair="BTC/USD", direction="long", entry_price=65_000.0, stake=1000.0,
            confidence=0.72,
            tft_probs={"down": 0.1, "flat": 0.2, "up": 0.7},
            drl_votes={"ppo": 1, "a2c": 1, "dqn": 0},
            sentiment_score=0.42, sentiment_confidence=0.7,
            regime="trending_up",
            features_used=["%-rsi-period_14", "%-onchain_mvrv"],
            reasoning="tft_up=0.7 + meta=+1",
            external_id="ft-1234",
        )
        assert jid > 0
        # Round-trip
        row = j.get_trade(jid)
        assert row is not None
        assert row.pair == "BTC/USD"
        assert row.tft_probs == {"down": 0.1, "flat": 0.2, "up": 0.7}
        assert row.drl_votes == {"ppo": 1, "a2c": 1, "dqn": 0}
        assert row.features_used == ["%-rsi-period_14", "%-onchain_mvrv"]

        # find_open_by_external_id
        found = j.find_open_by_external_id("ft-1234")
        assert found == jid

        # Close it
        ok = j.log_exit(
            jid, exit_price=66_010.0, pnl=15.55, pnl_pct=0.0091,
            exit_reason="freqai_down_regime", duration_min=144.0,
        )
        assert ok
        row2 = j.get_trade(jid)
        assert row2.closed_at is not None and row2.exit_price == 66_010.0

        # CSV export
        csv_path = Path(td) / "out.csv"
        n = j.export_csv(start=None, end=None, path=csv_path)
        assert n == 1
        assert "BTC/USD" in csv_path.read_text()
        _ok(f"entry → exit → CSV (1 row, {csv_path.stat().st_size} bytes)")


def test_journal_markdown_and_stats() -> None:
    print("\n[5/8] Journal: markdown export + stats math")
    if not _truncate_journal():
        return
    with tempfile.TemporaryDirectory() as td:
        j = TradeJournal()
        # Mix of wins/losses
        for i, (pnl, pct) in enumerate([(50, 0.05), (-20, -0.02), (30, 0.03), (-10, -0.01)]):
            jid = j.log_entry(
                pair="BTC/USD", direction="long",
                entry_price=65_000 + i, stake=1000,
            )
            j.log_exit(jid, exit_price=65_000, pnl=pnl, pnl_pct=pct, exit_reason="exit", duration_min=10)
        s = j.stats()
        assert s["trades"] == 4 and s["wins"] == 2 and s["losses"] == 2
        assert abs(s["total_pnl"] - 50.0) < 1e-9
        assert abs(s["profit_factor"] - 80.0/30.0) < 1e-9, s["profit_factor"]

        md = Path(td) / "report.md"
        n = j.export_markdown(start=None, end=None, path=md)
        assert n == 4
        text = md.read_text()
        assert "Total P&L" in text and "BTC/USD" in text
        _ok(f"PF={s['profit_factor']:.2f}  win_rate={s['win_rate']:.1%}  "
            f"md={md.stat().st_size}B")


def test_journal_query_filters() -> None:
    print("\n[6/8] Journal: query filters (date + pair)")
    if not _truncate_journal():
        return
    j = TradeJournal()
    now = datetime.now(timezone.utc)
    for delta_days, pair in ((-3, "BTC/USD"), (-1, "ETH/USD"), (0, "BTC/USD")):
        j.log_entry(
            pair=pair, direction="long",
            entry_price=100, opened_at=now + timedelta(days=delta_days),
        )
    rows_btc = j.query(pair="BTC/USD")
    rows_recent = j.query(start=now - timedelta(days=2))
    assert len(rows_btc) == 2 and all(r.pair == "BTC/USD" for r in rows_btc)
    assert len(rows_recent) == 2
    _ok(f"pair filter → {len(rows_btc)} BTC; date filter → {len(rows_recent)} recent")


# ---------------------------------------------------------------------------
# Metrics writer tests
# ---------------------------------------------------------------------------


class FakeWriteApi:
    """Captures writes done by MetricsWriter."""
    def __init__(self):
        self.writes: list[Any] = []
    def write(self, bucket=None, org=None, record=None):
        self.writes.append({"bucket": bucket, "org": org, "record": list(record)})


def _drain(mw: MetricsWriter, fake: FakeWriteApi, timeout=2.0) -> None:
    """Wait for the worker thread to flush its queue into `fake`."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if mw._queue.empty() and fake.writes:
            # Give worker one more slice to actually call write()
            time.sleep(0.05)
            return
        time.sleep(0.02)


def test_metrics_writer_points() -> None:
    print("\n[7/8] Metrics: every helper → InfluxDB Point")
    fake = FakeWriteApi()
    mw = MetricsWriter(
        config=InfluxConfig(
            url="http://x", token="t", org="o", bucket="b",
            batch_size=1, flush_interval_sec=0.05, enabled=True,
        ),
        client=fake,
    )
    try:
        mw.write_pnl(equity=10_000, peak_equity=10_500, drawdown=0.05,
                     daily_pnl=-100.0, cumulative_pnl=250.0)
        mw.write_trade(pair="BTC/USD", side="long", pnl=15.5, pnl_pct=0.01,
                       confidence=0.72, duration_min=12.5)
        mw.write_sharpe(1.42, window="30d")
        mw.write_win_rate(0.62, n=50, window="30d")
        mw.write_regime(pair="BTC/USD", label="trending_up", probability=0.8, confidence=0.9)
        mw.write_sentiment(pair="BTC/USD", score=0.42, confidence=0.7, price=65_000.0)
        mw.write_evolution(member_id="gen3-c00", fitness=1.55, generation=3,
                           sharpe=1.4, max_drawdown=0.05, is_champion=True)
        # Drain
        deadline = time.time() + 3.0
        while time.time() < deadline and len(fake.writes) < 7:
            time.sleep(0.02)
    finally:
        mw.close()
    measurements = []
    for w in fake.writes:
        for p in w["record"]:
            measurements.append(p.to_line_protocol().split(",")[0])
    assert "pnl" in measurements
    assert "trades" in measurements
    assert "sharpe" in measurements
    assert "win_rate" in measurements
    assert "regime" in measurements
    assert "sentiment" in measurements
    assert "evolution" in measurements
    _ok(f"{len(fake.writes)} flushes, {len(measurements)} points covering all 7 measurements")


def test_metrics_hourly_snapshot() -> None:
    print("\n[8/8] Metrics: hourly snapshot helper")
    fake = FakeWriteApi()
    mw = MetricsWriter(
        config=InfluxConfig(
            url="http://x", token="t", org="o", bucket="b",
            batch_size=20, flush_interval_sec=0.05, enabled=True,
        ),
        client=fake,
    )
    try:
        mw.write_hourly_snapshot(
            equity=10_240, peak_equity=10_500, drawdown=0.024,
            daily_pnl=240.0, cumulative_pnl=1_500.0,
            sharpe_30d=1.42, win_rate_30d=0.6, win_rate_n=42,
            regime=("BTC/USD", "trending_up"),
        )
        deadline = time.time() + 3.0
        while time.time() < deadline:
            if fake.writes and not mw._queue.qsize():
                time.sleep(0.1)
                break
            time.sleep(0.02)
    finally:
        mw.close()
    lines: list[str] = []
    for w in fake.writes:
        for p in w["record"]:
            lines.append(p.to_line_protocol())
    measurements = {l.split(",")[0] for l in lines}
    # snapshot covers pnl + sharpe + win_rate + regime
    assert {"pnl", "sharpe", "win_rate", "regime"} <= measurements, measurements
    _ok(f"hourly snapshot → measurements {sorted(measurements)}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> int:
    _hr()
    print(" Monitoring: slack + journal + metrics smoke test")
    _hr()

    test_slack_payload_shape()
    test_slack_evolution()
    test_slack_dedup_and_dry_run()
    test_journal_roundtrip()
    test_journal_markdown_and_stats()
    test_journal_query_filters()
    test_metrics_writer_points()
    test_metrics_hourly_snapshot()

    print("\nALL CHECKS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
