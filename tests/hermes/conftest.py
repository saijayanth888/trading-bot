"""Shared fixtures for ``tests/hermes``.

These fixtures fake out the three external surfaces Hermes touches:

* ``OllamaClient`` — replaced with a recorder.
* ``LedgerClient`` — replaced with in-memory trade rows.
* ``SlackNotifier`` — replaced with a recorder.

This lets the entire suite run without network or Postgres.
"""

from __future__ import annotations

import sys
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

# Ensure ``src/`` is on the path so ``quanta_core`` imports without a wheel install.
ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


from quanta_core.hermes._ledger import TradeRow  # noqa: E402


@dataclass
class FakeLedger:
    """In-memory ledger with the same surface as ``LedgerClient``."""

    rows: list[TradeRow] = field(default_factory=list)
    opens: list[TradeRow] = field(default_factory=list)
    ping_ok: bool = True
    dsn: str | None = "fake://"
    timeout_seconds: float = 1.0

    @property
    def available(self) -> bool:
        return True

    def closed_trades_for_day(self, day):
        return [r for r in self.rows if r.exit_ts and r.exit_ts.date() == day]

    def closed_trades_for_range(self, start, end):
        return [
            r
            for r in self.rows
            if r.exit_ts and start <= r.exit_ts.date() <= end
        ]

    def open_positions(self):
        return list(self.opens)

    def ping(self) -> bool:
        return self.ping_ok


@dataclass
class FakeOllama:
    """Recorder Ollama client; replays a queue of canned responses."""

    responses: list[str | None] = field(default_factory=list)
    calls: list[tuple[str, str, str | None]] = field(default_factory=list)
    resident_models: list[str] = field(default_factory=lambda: ["hermes3:8b"])
    ping_ok: bool = True

    def generate(self, model, prompt, system=None):
        self.calls.append((model, prompt, system))
        if not self.responses:
            return None
        return self.responses.pop(0)

    def list_resident(self):
        return list(self.resident_models)

    def ping(self):
        return (self.ping_ok, 12.5, list(self.resident_models))


@dataclass
class FakeNotifier:
    posts: list[str] = field(default_factory=list)
    webhook_url: str | None = "http://fake"
    channel: str | None = None
    timeout_seconds: float = 5.0

    def post(self, text: str) -> bool:
        self.posts.append(text)
        return True


@pytest.fixture
def fake_ledger() -> FakeLedger:
    return FakeLedger()


@pytest.fixture
def fake_ollama() -> FakeOllama:
    return FakeOllama()


@pytest.fixture
def fake_notifier() -> FakeNotifier:
    return FakeNotifier()


@pytest.fixture
def state_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point ``state_dir()`` at a tmp_path."""

    root = tmp_path / "state"
    root.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("QUANTA_STATE_DIR", str(root))
    return root


@pytest.fixture
def repo_root_fake(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point ``repo_root()`` at a tmp_path with stocks/memory/ pre-seeded."""

    root = tmp_path / "repo"
    (root / "stocks" / "memory").mkdir(parents=True, exist_ok=True)
    (root / "docs" / "weekly").mkdir(parents=True, exist_ok=True)
    # pyproject.toml so repo_root() resolves to ``root``
    (root / "pyproject.toml").write_text("[project]\nname='fake'\n")
    monkeypatch.setenv("QUANTA_REPO_ROOT", str(root))
    return root


@pytest.fixture
def clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Wipe env vars Hermes reads so each test starts from a clean slate."""

    for key in (
        "OLLAMA_BASE_URL",
        "HERMES_REFLECTOR_MODEL",
        "HERMES_POST_MORTEM_MODEL",
        "POSTGRES_DSN",
        "MODELFORGE_API_URL",
        "MODELFORGE_API_KEY",
        "MODELFORGE_WORKFLOW_ID",
        "ALPACA_KEY_ID",
        "ALPACA_SECRET_KEY",
        "COINBASE_API_KEY",
        "SLACK_WEBHOOK_URL",
        "SLACK_CHANNEL",
        "HERMES_HEALTH_FAIL_THRESHOLD",
    ):
        monkeypatch.delenv(key, raising=False)


def make_trade(
    *,
    trade_id: str = "t1",
    pair: str = "BTC/USD",
    side: str = "long",
    entry: float = 100.0,
    exit: float = 110.0,
    entry_ts: datetime | None = None,
    exit_ts: datetime | None = None,
    pnl: float | None = 10.0,
    pnl_pct: float | None = 10.0,
    strategy: str = "mean_rev",
    regime: str = "trending_up",
    raw: Mapping[str, Any] | None = None,
) -> TradeRow:
    """Helper to build a :class:`TradeRow` for fixtures."""

    if entry_ts is None:
        entry_ts = datetime(2026, 5, 11, 9, 30, tzinfo=timezone.utc)
    if exit_ts is None:
        exit_ts = entry_ts + timedelta(hours=8)
    return TradeRow(
        trade_id=trade_id,
        pair=pair,
        side=side,
        entry_price=entry,
        exit_price=exit,
        entry_ts=entry_ts,
        exit_ts=exit_ts,
        pnl=pnl,
        pnl_pct=pnl_pct,
        strategy=strategy,
        regime=regime,
        raw=raw or {},
    )
