"""
Unit tests for ``scripts/modelforge_ingest.py`` (Stage 1).

These cover:
  * decisions.md -> trading-reflector splitting of pending vs realized blocks
  * llm-calls.jsonl -> per-role file routing by ``agent`` field
  * idempotency: re-running on the same date is a no-op
  * record-date filter respects the ``timestamp`` field
  * fail-soft semantics (missing files don't crash, exit code is 0)

The tests construct their own temporary ``~/.dgx-train`` root via the
``DGX_TRAIN_ROOT`` env var so they never touch operator state.
"""

from __future__ import annotations

import datetime as dt
import importlib.util
import json
import sys
from pathlib import Path

import pytest

# --------------------------------------------------------------------------- #
# Import the ingest module from scripts/ without polluting sys.path globally.
# --------------------------------------------------------------------------- #

_REPO_ROOT = Path(__file__).resolve().parents[1]
_INGEST_PATH = _REPO_ROOT / "scripts" / "modelforge_ingest.py"


def _load_ingest_module():
    spec = importlib.util.spec_from_file_location("modelforge_ingest", _INGEST_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    # `dataclass` looks the module up by name in sys.modules during class
    # construction; register before exec_module so the lookup succeeds.
    sys.modules["modelforge_ingest"] = module
    spec.loader.exec_module(module)
    return module


ingest_mod = _load_ingest_module()


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #

@pytest.fixture
def tmp_train_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A clean ~/.dgx-train-style root for one test."""
    root = tmp_path / "dgx-train"
    root.mkdir()
    monkeypatch.setenv("DGX_TRAIN_ROOT", str(root))
    return root


@pytest.fixture
def decisions_md(tmp_path: Path) -> Path:
    """Five-entry decisions.md fixture: 2 pending, 3 realized.

    Two of the three realized entries close on 2026-05-11 (target date);
    the third closes on a different day. The pending entries are dated
    2026-05-11 so the ingest should treat them as "in-flight today".
    """
    path = tmp_path / "decisions.md"
    path.write_text(
        "# Decisions log\n"
        "\n"
        "---\n"
        "[2026-05-09 | NVDA | BUY | +1.5% | +0.8% alpha | 2d]\n"
        "DECISION: Long NVDA on AI capex strength.\n"
        "REFLECTION: Worked. AI capex catalyst held; +0.8% alpha vs SPY. "
        "Lesson: ride the megacap-AI tape when DXY is rolling.\n"
        "---\n"
        "[2026-05-08 | AMD | BUY | -3.2% | -1.1% alpha | 3d]\n"
        "DECISION: Long AMD into MI300 ramp.\n"
        "REFLECTION: Missed. CRDO weakness bled across semis; -1.1% alpha. "
        "Lesson: cut faster on intra-sector dispersion.\n"
        "---\n"
        "[2026-05-09 | SOFI | BUY | +2.4% | +0.9% alpha | 1d]\n"
        "DECISION: SOFI short-vol play into earnings.\n"
        "REFLECTION: Earnings beat; held one day; +0.9% alpha.\n"
        "---\n"
        "[2026-05-11 | TSLA | BUY | pending]\n"
        "DECISION: Long TSLA on robotaxi launch headline.\n"
        "REFLECTION: \n"
        "---\n"
        "[2026-05-11 | GOOGL | WAIT | pending]\n"
        "DECISION: Waiting for breakout above 200d SMA.\n"
        "REFLECTION: \n"
        "---\n",
        encoding="utf-8",
    )
    return path


@pytest.fixture
def llm_calls_jsonl(tmp_path: Path) -> Path:
    """A mixed llm-calls.jsonl: bull, bear, arbiter, regime_tagger, indicator, plus noise.

    Two ``timestamp`` shapes -- one in-band (2026-05-11), one out-of-band
    (2026-05-10) -- so the date filter is exercised. Two of the bull records
    are missing the optional full-text fields -- they should be skipped.
    """
    path = tmp_path / "llm-calls.jsonl"
    records = [
        {
            "agent": "bull_analyst", "model": "qwen3:30b", "provider": "ollama",
            "tier": "deep", "role": "default", "latency_seconds": 3.1,
            "prompt_tokens": 800, "completion_tokens": 250,
            "timestamp": "2026-05-11T15:30:00+00:00",
            "prompt": "Make the bull case for NVDA at $1200 with RSI=62 and MACD bullish cross on 2026-05-10.",
            "system_message": "You are the bull analyst.",
            "response_text": "NVDA is set up well: $1200 holds the 20EMA, RSI 62 is healthy, the 2026-05-10 MACD cross extends the trend, and 12% revenue growth supports.",
            "messages": None, "redacted_count": 0,
        },
        {
            "agent": "bear_analyst", "model": "qwen3:30b", "provider": "ollama",
            "tier": "deep", "role": "default", "latency_seconds": 2.8,
            "prompt_tokens": 700, "completion_tokens": 220,
            "timestamp": "2026-05-11T15:31:00+00:00",
            "prompt": "Make the bear case for NVDA at $1200.",
            "system_message": "You are the bear analyst.",
            "response_text": "NVDA is overbought: ATR is 4% above the 30d mean, the $1200 print failed twice in April, MACD is decelerating, and put/call hit 1.7 on 2026-05-10.",
            "messages": None, "redacted_count": 0,
        },
        {
            "agent": "research_manager", "model": "qwen3:30b", "provider": "ollama",
            "tier": "deep", "role": "arbiter", "latency_seconds": 4.2,
            "prompt_tokens": 1100, "completion_tokens": 80,
            "timestamp": "2026-05-11T15:32:00+00:00",
            "prompt": "Resolve bull vs bear for NVDA.",
            "system_message": "You are the research manager.",
            "response_text": '{"decision":"BUY","size_pct":0.05,"stop_pct":0.07,"target_pct":0.15}',
            "messages": None, "redacted_count": 0,
            "valid": True,
        },
        {
            "agent": "regime_tagger", "model": "hermes3:8b", "provider": "ollama",
            "tier": "fast", "role": "default", "latency_seconds": 0.8,
            "prompt_tokens": 200, "completion_tokens": 30,
            "timestamp": "2026-05-11T20:00:00+00:00",
            "prompt": "Tag the regime for 2026-05-11 SPY",
            "system_message": "JSON only.",
            "response_text": '{"regime":"trending_up","confidence":0.78}',
            "messages": None, "redacted_count": 0,
            "valid": True,
        },
        {
            "agent": "indicator_selector", "model": "hermes3:8b", "provider": "ollama",
            "tier": "fast", "role": "default", "latency_seconds": 1.1,
            "prompt_tokens": 300, "completion_tokens": 50,
            "timestamp": "2026-05-11T20:30:00+00:00",
            "prompt": "Pick <=8 indicators for NVDA on the 4h chart.",
            "system_message": "JSON only.",
            "response_text": '{"indicators":["EMA20","RSI14","MACD","ATR14","BB20"]}',
            "messages": None, "redacted_count": 0,
            "valid": True,
        },
        # Out-of-band: yesterday's date relative to target 2026-05-11 -> filtered out
        {
            "agent": "bull_analyst", "model": "qwen3:30b", "provider": "ollama",
            "tier": "deep", "role": "default", "latency_seconds": 3.1,
            "prompt_tokens": 800, "completion_tokens": 250,
            "timestamp": "2026-05-10T15:30:00+00:00",
            "prompt": "Make the bull case for AMD.",
            "system_message": "You are the bull analyst.",
            "response_text": "AMD is set up well: $200 support held, RSI 58 healthy.",
            "messages": None, "redacted_count": 0,
        },
        # Missing optional full-text -> should be skipped (no training value)
        {
            "agent": "bull_analyst", "model": "qwen3:30b", "provider": "ollama",
            "tier": "deep", "role": "default", "latency_seconds": 3.1,
            "prompt_tokens": 800, "completion_tokens": 250,
            "timestamp": "2026-05-11T16:00:00+00:00",
            "prompt": None, "system_message": None, "response_text": None,
            "messages": None, "redacted_count": None,
        },
        # Unknown agent -> filtered
        {
            "agent": "some_other_agent", "model": "qwen3:30b", "provider": "ollama",
            "tier": "deep", "role": "default", "latency_seconds": 1.0,
            "prompt_tokens": 100, "completion_tokens": 30,
            "timestamp": "2026-05-11T16:00:00+00:00",
            "prompt": "noise", "system_message": "noise",
            "response_text": "noise",
            "messages": None, "redacted_count": 0,
        },
    ]
    with path.open("w", encoding="utf-8") as fh:
        for r in records:
            fh.write(json.dumps(r) + "\n")
    return path


# --------------------------------------------------------------------------- #
# Tests
# --------------------------------------------------------------------------- #

def test_parse_target_date_yesterday():
    """Yesterday-fallback is UTC and one full day behind ``now``."""
    fake_now = dt.datetime(2026, 5, 12, 1, 0, tzinfo=dt.timezone.utc)
    assert ingest_mod.parse_target_date(None, now=fake_now) == dt.date(2026, 5, 11)


def test_parse_target_date_explicit():
    assert ingest_mod.parse_target_date("2026-05-09") == dt.date(2026, 5, 9)


def test_parse_target_date_bad():
    with pytest.raises(ValueError):
        ingest_mod.parse_target_date("not-a-date")


def test_reflector_splits_pending_and_realized(decisions_md, tmp_train_root):
    """Five fixture entries: 2 pending today + 1 realized today + 2 realized other days.

    Target date is 2026-05-11.
      - Pending TSLA, GOOGL (open 2026-05-11) -> emitted with pending_outcome=True
      - Realized SOFI: open 2026-05-09, holding 1d -> closed 2026-05-10 -> filtered out
      - Realized NVDA: open 2026-05-09, holding 2d -> closed 2026-05-11 -> kept
      - Realized AMD:  open 2026-05-08, holding 3d -> closed 2026-05-11 -> kept

    Expected output: 4 rows in trading-reflector/20260511.jsonl
        2 pending + 2 realized
    """
    stats = ingest_mod.ingest(
        dt.date(2026, 5, 11),
        decisions_md=decisions_md,
        llm_calls_jsonl=tmp_train_root / "no-such-file.jsonl",
        raw_root=tmp_train_root / "raw",
    )
    out_path = tmp_train_root / "raw" / "trading-reflector" / "20260511.jsonl"
    assert out_path.exists()
    rows = [json.loads(l) for l in out_path.read_text().splitlines() if l.strip()]
    assert len(rows) == 4
    pendings = [r for r in rows if r["pending_outcome"]]
    realizeds = [r for r in rows if not r["pending_outcome"]]
    assert len(pendings) == 2
    assert len(realizeds) == 2
    pending_tickers = {r["ticker"] for r in pendings}
    realized_tickers = {r["ticker"] for r in realizeds}
    assert pending_tickers == {"TSLA", "GOOGL"}
    assert realized_tickers == {"NVDA", "AMD"}
    # outcome_key shape
    for r in realizeds:
        assert "|" in r["outcome_key"]
    # accepted count surfaced in stats
    assert stats.accepted["trading-reflector"] == 4


def test_llm_calls_split_by_role(decisions_md, llm_calls_jsonl, tmp_train_root):
    """Mixed JSONL fans out to the right per-role files."""
    target = dt.date(2026, 5, 11)
    stats = ingest_mod.ingest(
        target,
        decisions_md=decisions_md,
        llm_calls_jsonl=llm_calls_jsonl,
        raw_root=tmp_train_root / "raw",
    )

    expectations = {
        "trading-bull":               1,  # 2 bulls in band, 1 missing prompt+response
        "trading-bear":               1,
        "trading-arbiter":            1,
        "trading-regime-tagger":      1,
        "trading-indicator-selector": 1,
    }
    for role, expected in expectations.items():
        out = tmp_train_root / "raw" / role / "20260511.jsonl"
        assert out.exists(), f"missing {role} output"
        n = sum(1 for line in out.read_text().splitlines() if line.strip())
        assert n == expected, f"role={role}: expected {expected}, got {n}"
        assert stats.accepted[role] == expected


def test_idempotent_rerun(decisions_md, tmp_train_root):
    """Second run on the same date is a no-op and reports skip."""
    target = dt.date(2026, 5, 11)
    raw_root = tmp_train_root / "raw"
    first = ingest_mod.ingest(
        target,
        decisions_md=decisions_md,
        llm_calls_jsonl=tmp_train_root / "nofile",
        raw_root=raw_root,
    )
    second = ingest_mod.ingest(
        target,
        decisions_md=decisions_md,
        llm_calls_jsonl=tmp_train_root / "nofile",
        raw_root=raw_root,
    )
    assert first.accepted["trading-reflector"] == 4
    assert second.accepted["trading-reflector"] == 0
    assert "trading-reflector" in second.skipped_existing


def test_missing_sources_dont_crash(tmp_train_root):
    """A run with both inputs missing exits 0 and writes nothing."""
    stats = ingest_mod.ingest(
        dt.date(2026, 5, 11),
        decisions_md=tmp_train_root / "nope.md",
        llm_calls_jsonl=tmp_train_root / "nope.jsonl",
        raw_root=tmp_train_root / "raw",
    )
    assert stats.errors == []
    # Note: write_raw_jsonl still creates empty files; presence is fine since
    # downstream curate handles empty inputs as zero-accept zero-reject.
    # We only assert no row was written.
    for role in ingest_mod.ALL_ROLES:
        out = tmp_train_root / "raw" / role / "20260511.jsonl"
        if out.exists():
            assert out.read_text().strip() == ""


def test_cli_main_exit_zero(monkeypatch, decisions_md, llm_calls_jsonl, tmp_train_root):
    """CLI returns 0 on success and prints a summary."""
    monkeypatch.setenv("SHARK_DECISIONS_MD", str(decisions_md))
    monkeypatch.setenv("SHARK_TRACKER_LOG", str(llm_calls_jsonl))
    rc = ingest_mod.main(["2026-05-11", "--raw-root", str(tmp_train_root / "raw"), "--quiet"])
    assert rc == 0
    # Confirm at least one role file was written (bull case in-band).
    assert (tmp_train_root / "raw" / "trading-bull" / "20260511.jsonl").exists()


def test_cli_bad_date_exit_zero(tmp_train_root):
    """Bad date arg is fail-soft: returns 0, no traceback."""
    rc = ingest_mod.main(["not-a-date", "--raw-root", str(tmp_train_root / "raw")])
    assert rc == 0


def test_atomic_publish_no_partial_files(decisions_md, tmp_train_root):
    """The writer must not leave .partial files behind on success."""
    target = dt.date(2026, 5, 11)
    ingest_mod.ingest(
        target,
        decisions_md=decisions_md,
        llm_calls_jsonl=tmp_train_root / "nofile",
        raw_root=tmp_train_root / "raw",
    )
    partials = list((tmp_train_root / "raw").rglob("*.partial"))
    assert partials == []


def test_ticker_guesser_labelled():
    assert ingest_mod._guess_ticker("Ticker: NVDA on 4h") == "NVDA"


def test_ticker_guesser_unlabelled():
    assert ingest_mod._guess_ticker("Bull case for AMD given the MI300 ramp") in {"AMD", "MI300"}


def test_ticker_guesser_skips_common_words():
    assert ingest_mod._guess_ticker("YOU are the bull analyst") in {None, "MI300", "AMD"}
