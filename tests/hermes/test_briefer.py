"""Tests for ``quanta_core.hermes.briefer``."""

from __future__ import annotations

import json
from datetime import date

from quanta_core.hermes import briefer
from tests.hermes.conftest import FakeLedger, FakeNotifier, make_trade


def test_render_briefing_summary():
    inputs = briefer.BriefingInputs(
        regime={"regime": "trending_up"},
        sentiment={"score": 0.42},
        calendar=[{"name": "FOMC"}],
        open_positions=[{"pair": "BTC/USD"}],
    )
    out = briefer.render_briefing(inputs, date(2026, 5, 12))
    assert out["regime"]["regime"] == "trending_up"
    assert "regime=trending_up" in out["summary"]
    assert "sentiment=+0.42" in out["summary"]
    assert "1 upcoming event" in out["summary"]
    assert "1 open position" in out["summary"]


def test_render_briefing_missing_sentiment():
    inputs = briefer.BriefingInputs(
        regime={}, sentiment={"score": None}, calendar=[], open_positions=[]
    )
    out = briefer.render_briefing(inputs, date(2026, 5, 12))
    assert "sentiment=n/a" in out["summary"]


def test_render_briefing_includes_next_monday():
    """Briefing fires Monday — for_week_starting is the *next* Monday."""

    inputs = briefer.BriefingInputs(
        regime={}, sentiment={}, calendar=[], open_positions=[]
    )
    # Mon 2026-05-11 → next Mon is 2026-05-18
    out = briefer.render_briefing(inputs, date(2026, 5, 11))
    assert out["for_week_starting"] == "2026-05-18"


def test_fetch_regime_state_fallback(state_root, clean_env):
    (state_root / "regime.json").write_text('{"regime": "bear_volatile"}')
    from quanta_core.hermes._common import load_config

    cfg = load_config()
    out = briefer.fetch_regime(cfg)
    assert out["regime"] == "bear_volatile"


def test_fetch_regime_missing_returns_default(state_root, clean_env):
    from quanta_core.hermes._common import load_config

    cfg = load_config()
    out = briefer.fetch_regime(cfg)
    assert out["regime"] == "unknown"


def test_fetch_calendar_from_kb_file(state_root, repo_root_fake, clean_env):
    kb_dir = repo_root_fake / "stocks" / "kb"
    kb_dir.mkdir(parents=True, exist_ok=True)
    (kb_dir / "economic_calendar.json").write_text(
        json.dumps([{"name": "CPI", "ts": "2026-05-13"}])
    )
    from quanta_core.hermes._common import load_config

    cfg = load_config()
    items = briefer.fetch_calendar(cfg)
    assert len(items) == 1
    assert items[0]["name"] == "CPI"


def test_fetch_calendar_missing_returns_empty(state_root, repo_root_fake, clean_env):
    from quanta_core.hermes._common import load_config

    cfg = load_config()
    assert briefer.fetch_calendar(cfg) == []


def test_run_writes_state(clean_env, state_root, repo_root_fake, monkeypatch):
    monkeypatch.setattr(briefer, "LedgerClient", lambda *a, **k: FakeLedger())
    monkeypatch.setattr(briefer, "SlackNotifier", lambda *a, **k: FakeNotifier())
    code = briefer.run(["--no-slack"])
    assert code == 0
    state = json.loads((state_root / "briefing.json").read_text())
    assert "summary" in state
    assert "for_week_starting" in state


def test_run_posts_slack_by_default(
    clean_env, state_root, repo_root_fake, monkeypatch
):
    notifier = FakeNotifier()
    monkeypatch.setattr(briefer, "LedgerClient", lambda *a, **k: FakeLedger())
    monkeypatch.setattr(briefer, "SlackNotifier", lambda *a, **k: notifier)
    code = briefer.run([])
    assert code == 0
    assert any("pre-market briefing" in p for p in notifier.posts)


def test_run_with_open_positions(
    clean_env, state_root, repo_root_fake, monkeypatch
):
    ledger = FakeLedger()
    ledger.opens.append(make_trade(pair="ETH/USD", side="long"))
    monkeypatch.setattr(briefer, "LedgerClient", lambda *a, **k: ledger)
    monkeypatch.setattr(briefer, "SlackNotifier", lambda *a, **k: FakeNotifier())
    code = briefer.run(["--no-slack"])
    assert code == 0
    state = json.loads((state_root / "briefing.json").read_text())
    assert len(state["open_positions"]) == 1
    assert state["open_positions"][0]["pair"] == "ETH/USD"


# ---------------------------------------------------------------------------
# HTTP fallback paths — drive the api responses with monkeypatched httpx
# ---------------------------------------------------------------------------


class _FakeResp:
    def __init__(self, status: int, body):
        self.status_code = status
        self._body = body

    def json(self):
        return self._body


def test_http_get_json_dict_passthrough(monkeypatch):
    monkeypatch.setattr(briefer.httpx, "get", lambda *a, **k: _FakeResp(200, {"x": 1}))
    out = briefer._http_get_json("http://x", 1.0)
    assert out == {"x": 1}


def test_http_get_json_list_wraps_in_items(monkeypatch):
    monkeypatch.setattr(briefer.httpx, "get", lambda *a, **k: _FakeResp(200, [1, 2, 3]))
    out = briefer._http_get_json("http://x", 1.0)
    assert out == {"items": [1, 2, 3]}


def test_http_get_json_non_200(monkeypatch):
    monkeypatch.setattr(briefer.httpx, "get", lambda *a, **k: _FakeResp(500, None))
    assert briefer._http_get_json("http://x", 1.0) is None


def test_http_get_json_raises(monkeypatch):
    def raises(*a, **k):
        raise OSError("nope")

    monkeypatch.setattr(briefer.httpx, "get", raises)
    assert briefer._http_get_json("http://x", 1.0) is None


def test_fetch_regime_api_wins(monkeypatch, state_root, clean_env):
    """API response trumps the fallback state file."""

    (state_root / "regime.json").write_text('{"regime": "fallback"}')
    monkeypatch.setattr(
        briefer.httpx, "get", lambda *a, **k: _FakeResp(200, {"regime": "from_api"})
    )
    from quanta_core.hermes._common import load_config

    cfg = load_config()
    assert briefer.fetch_regime(cfg)["regime"] == "from_api"


def test_fetch_sentiment_state_fallback(monkeypatch, state_root, clean_env):
    (state_root / "sentiment.json").write_text('{"score": 0.7}')
    monkeypatch.setattr(briefer.httpx, "get", lambda *a, **k: _FakeResp(500, None))
    from quanta_core.hermes._common import load_config

    cfg = load_config()
    out = briefer.fetch_sentiment(cfg)
    assert out["score"] == 0.7


def test_fetch_sentiment_default(monkeypatch, state_root, clean_env):
    monkeypatch.setattr(briefer.httpx, "get", lambda *a, **k: _FakeResp(500, None))
    from quanta_core.hermes._common import load_config

    cfg = load_config()
    out = briefer.fetch_sentiment(cfg)
    assert out["source"] == "missing"


def test_fetch_calendar_api_items(monkeypatch, clean_env):
    monkeypatch.setattr(
        briefer.httpx,
        "get",
        lambda *a, **k: _FakeResp(200, {"items": [{"name": "FOMC"}]}),
    )
    from quanta_core.hermes._common import load_config

    cfg = load_config()
    out = briefer.fetch_calendar(cfg)
    assert out[0]["name"] == "FOMC"


def test_fetch_calendar_kb_dict_events_key(state_root, repo_root_fake, clean_env):
    kb_dir = repo_root_fake / "stocks" / "kb"
    kb_dir.mkdir(parents=True, exist_ok=True)
    (kb_dir / "economic_calendar.json").write_text(
        json.dumps({"events": [{"name": "CPI"}]})
    )
    from quanta_core.hermes._common import load_config

    cfg = load_config()
    items = briefer.fetch_calendar(cfg)
    assert items[0]["name"] == "CPI"
