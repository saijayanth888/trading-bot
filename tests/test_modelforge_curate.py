"""
Unit tests for ``scripts/modelforge_curate.py`` (Stage 2).

Covers:
  * per-role filter pass/fail with the documented reject-reason codes
  * HF Arrow shard layout matches what ModelForge's ``HuggingFaceDataCurator``
    writes (columns + ``mf_meta.json`` sidecar)
  * Idempotent state-file gate
  * Accept-rate Slack alert fires when the rate falls outside the band
  * Fail-soft CLI behaviour (exit 0 on bad date, missing dirs, etc.)
"""

from __future__ import annotations

import datetime as dt
import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]
_CURATE_PATH = _REPO_ROOT / "scripts" / "modelforge_curate.py"


def _load_curate_module():
    spec = importlib.util.spec_from_file_location("modelforge_curate", _CURATE_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules["modelforge_curate"] = module
    spec.loader.exec_module(module)
    return module


curate_mod = _load_curate_module()


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

def _write_raw(path: Path, examples: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for ex in examples:
            fh.write(json.dumps(ex) + "\n")


def _reflector_example(*, response: str, alpha_pct: str = "+1.2% alpha",
                       pending: bool = False) -> dict[str, Any]:
    return {
        "ts": "2026-05-11",
        "ticker": "NVDA",
        "system_message": "reflector",
        "user_message": "thesis...",
        "response": response,
        "pending_outcome": pending,
        "outcome_key": "2026-05-11|NVDA",
        "ledger": {
            "open_date": "2026-05-09",
            "closed_at": "2026-05-11",
            "ticker": "NVDA",
            "rating": "BUY",
            "raw_pct": "+2.5%",
            "alpha_pct": alpha_pct,
            "holding": "2d",
        },
    }


def _bull_example(*, response: str) -> dict[str, Any]:
    return {
        "ts": "2026-05-11T15:30:00+00:00",
        "ticker": "NVDA",
        "system_message": "bull",
        "user_message": "make the bull case",
        "response": response,
        "pending_outcome": False,
        "outcome_key": "2026-05-11|bull_analyst",
        "ledger": {"agent": "bull_analyst", "valid": True},
    }


def _structured_example(*, response: str, valid: bool = True) -> dict[str, Any]:
    return {
        "ts": "2026-05-11T20:00:00+00:00",
        "ticker": "",
        "system_message": "regime",
        "user_message": "tag",
        "response": response,
        "pending_outcome": False,
        "outcome_key": "2026-05-11|regime_tagger",
        "ledger": {"agent": "regime_tagger", "valid": valid},
    }


# --------------------------------------------------------------------------- #
# Per-role filter unit tests
# --------------------------------------------------------------------------- #

class TestReflectorFilter:
    """``filter_reflector`` rules: length, alpha-citation accuracy, pending."""

    def test_passes_well_formed_realized(self):
        ex = _reflector_example(
            response=(
                "Worked: NVDA AI capex catalyst held; realized +1.0% alpha vs SPY. "
                "Lesson: ride the megacap-AI tape when DXY is rolling, and "
                "tighten trail when stretched."
            ),
        )
        ok, code = curate_mod.filter_reflector(ex)
        assert ok, f"expected pass, got reject={code}"
        assert code is None

    def test_rejects_pending(self):
        ex = _reflector_example(response="X" * 200, pending=True)
        ok, code = curate_mod.filter_reflector(ex)
        assert not ok
        assert code == curate_mod.Reject.PENDING

    def test_rejects_too_short(self):
        ex = _reflector_example(response="too short.")
        ok, code = curate_mod.filter_reflector(ex)
        assert not ok
        assert code == curate_mod.Reject.LENGTH_OUT_OF_BAND

    def test_rejects_too_long(self):
        # filter_reflector accepts response lengths in [80, 1200]; widened from
        # the original [80, 600] band. Use 1500 chars to exceed the new cap.
        ex = _reflector_example(response="X" * 1500)
        ok, code = curate_mod.filter_reflector(ex)
        assert not ok
        assert code == curate_mod.Reject.LENGTH_OUT_OF_BAND

    def test_rejects_alpha_far_off(self):
        # Ledger says +1.2% alpha; response cites +50% — should miss the ±5pp tolerance.
        ex = _reflector_example(
            response=(
                "Worked: NVDA AI capex catalyst held; realized +50.0% alpha vs SPY. "
                "Lesson: ride the megacap-AI tape when DXY is rolling, and "
                "tighten trail when stretched."
            ),
            alpha_pct="+1.2% alpha",
        )
        ok, code = curate_mod.filter_reflector(ex)
        assert not ok
        assert code == curate_mod.Reject.ALPHA_REGEX_MISMATCH

    def test_passes_alpha_within_tolerance(self):
        # Cited +3.0% vs ledger +1.2% — within ±5pp tolerance.
        ex = _reflector_example(
            response=(
                "Worked: realized about +3.0% alpha vs SPY, close enough to the "
                "thesis. Lesson: ride megacap-AI tape when DXY rolls over."
            ),
            alpha_pct="+1.2% alpha",
        )
        ok, code = curate_mod.filter_reflector(ex)
        assert ok, f"expected pass, got reject={code}"

    def test_rejects_unknown_exit_reason(self):
        ex = _reflector_example(
            response="X" * 200 + " realized +1.1% alpha vs SPY. Lesson learned.",
        )
        ex["ledger"]["exit_reason"] = "operator_panic"  # not in KNOWN_EXIT_REASONS
        ok, code = curate_mod.filter_reflector(ex)
        assert not ok
        assert code == curate_mod.Reject.UNKNOWN_EXIT_REASON


class TestBullBearFilter:
    """Length + ≥2 evidence-items gate."""

    def test_passes_with_two_evidence_items(self):
        text = (
            "NVDA is set up well: $1200 holds the 20-EMA, RSI 62 is healthy, "
            "the 2026-05-10 MACD cross extends the trend, and 12% revenue "
            "growth supports the bull case. "
            + "More analysis padding to clear the 200-char floor. " * 4
        )
        ok, code = curate_mod.filter_bull_bear(_bull_example(response=text))
        assert ok, f"expected pass got code={code}"

    def test_rejects_too_short(self):
        ok, code = curate_mod.filter_bull_bear(_bull_example(response="too short"))
        assert not ok
        assert code == curate_mod.Reject.LENGTH_OUT_OF_BAND

    def test_rejects_too_long(self):
        ok, code = curate_mod.filter_bull_bear(_bull_example(response="X" * 2000))
        assert not ok
        assert code == curate_mod.Reject.LENGTH_OUT_OF_BAND

    def test_rejects_thin_evidence(self):
        # Long enough but zero numerics, indicators, or dates.
        long_prose = (
            "I really like this stock because the company has been doing well "
            "and management seems competent and the product feels strong and "
            "there is a feeling of momentum and customers like it.\n"
        ) * 3
        ok, code = curate_mod.filter_bull_bear(_bull_example(response=long_prose))
        assert not ok
        assert code == curate_mod.Reject.EVIDENCE_TOO_THIN


class TestStructuredFilter:
    """Arbiter / regime / indicator filter -- JSON validity is the gate."""

    def test_passes_valid_json(self):
        ok, code = curate_mod.filter_structured(
            _structured_example(response='{"regime":"trending_up","confidence":0.78}')
        )
        assert ok, f"unexpected reject {code}"

    def test_rejects_when_upstream_flagged_invalid(self):
        ok, code = curate_mod.filter_structured(
            _structured_example(response='{"x":1}', valid=False)
        )
        assert not ok
        assert code == curate_mod.Reject.STRUCTURED_INVALID

    def test_rejects_non_json_response(self):
        ok, code = curate_mod.filter_structured(
            _structured_example(response="this is prose, not JSON")
        )
        assert not ok
        assert code == curate_mod.Reject.STRUCTURED_INVALID


# --------------------------------------------------------------------------- #
# End-to-end curate pass: HF Arrow output + state idempotency
# --------------------------------------------------------------------------- #

@pytest.fixture
def populated_root(tmp_path: Path) -> Path:
    """Build a ~/.dgx-train style tree with one raw file per role.

    Each role's raw file contains a mix of pass/fail examples so the curate
    pass exercises both the keep and reject branches.
    """
    root = tmp_path / "dgx-train"
    raw = root / "raw"

    # reflector: 2 keeps + 1 pending + 1 length-fail
    _write_raw(
        raw / "trading-reflector" / "20260511.jsonl",
        [
            _reflector_example(
                response=(
                    "Worked: NVDA AI capex catalyst held; realized +1.1% alpha "
                    "vs SPY. Lesson: ride the megacap-AI tape when DXY rolls."
                ),
            ),
            _reflector_example(
                response=(
                    "Missed: AMD CRDO weakness bled through; -1.0% alpha. "
                    "Lesson: cut faster on intra-sector dispersion."
                ),
                alpha_pct="-1.1% alpha",
            ),
            _reflector_example(response="X" * 200, pending=True),
            _reflector_example(response="too short."),
        ],
    )
    # bull: 1 keep + 1 evidence-fail
    _write_raw(
        raw / "trading-bull" / "20260511.jsonl",
        [
            _bull_example(response=(
                "NVDA bull: $1200 holds 20-EMA, RSI 62 healthy, 12% revenue "
                "growth, MACD cross 2026-05-10."
                + " filler to clear 200-char floor. " * 5
            )),
            _bull_example(response="vague prose with no evidence. " * 10),
        ],
    )
    # bear: 1 keep (mirror of bull)
    _write_raw(
        raw / "trading-bear" / "20260511.jsonl",
        [
            _bull_example(response=(
                "NVDA bear: ATR 4% above mean, $1200 failed twice in April, "
                "MACD decelerating, put/call 1.7 on 2026-05-10."
                + " filler to clear 200-char floor. " * 5
            )),
        ],
    )
    # arbiter: 1 keep + 1 invalid
    _write_raw(
        raw / "trading-arbiter" / "20260511.jsonl",
        [
            _structured_example(response='{"decision":"BUY","size":0.05}'),
            _structured_example(response='not json', valid=False),
        ],
    )
    # regime: 1 keep
    _write_raw(
        raw / "trading-regime-tagger" / "20260511.jsonl",
        [_structured_example(response='{"regime":"trending_up","confidence":0.78}')],
    )
    # indicator: 1 keep
    _write_raw(
        raw / "trading-indicator-selector" / "20260511.jsonl",
        [_structured_example(response='{"indicators":["EMA20","RSI14"]}')],
    )
    return root


def test_end_to_end_writes_hf_arrow_per_role(populated_root: Path, monkeypatch):
    """The whole pipeline yields a loadable HF Arrow shard per role.

    The test fixtures seed only 1-4 records per role to keep the suite fast.
    Production N_MIN gates (100 for reflector/bull/bear/arbiter, 40 for
    regime/indicator) would block the shard write. Set the override env var
    so the happy-path shard logic can be exercised — production callers must
    NOT set this; the gate is the structural safeguard against undertrained
    adapters.
    """
    try:
        from datasets import load_from_disk  # noqa: F401
    except ImportError:
        pytest.skip("datasets library not installed in this env")

    monkeypatch.setenv("MODELFORGE_CURATE_N_MIN_OVERRIDE", "1")
    stats = curate_mod.curate(dt.date(2026, 5, 11), root=populated_root)

    # Reflector: 2 keeps out of 4 inputs
    refl = stats.by_role["trading-reflector"]
    assert refl.accept_count == 2
    assert refl.reject_count == 2
    assert curate_mod.Reject.PENDING in refl.reject_reasons
    assert curate_mod.Reject.LENGTH_OUT_OF_BAND in refl.reject_reasons

    # Load the Arrow shard and check columns match ModelForge's contract.
    from datasets import load_from_disk
    ds = load_from_disk(refl.out_path)
    assert set(ds.column_names) == {
        "category", "source", "dataset_name", "instruction", "response",
    }
    assert len(ds) == 2
    assert ds[0]["category"] == "trading-reflector"
    assert ds[0]["source"] == "trading-bot"
    assert ds[0]["dataset_name"] == "trading-reflector"

    # mf_meta.json sidecar shape
    meta_path = Path(refl.out_path) / "mf_meta.json"
    assert meta_path.exists()
    meta = json.loads(meta_path.read_text())
    assert meta["track_id"] == "trading-reflector"
    assert meta["sample_count"] == 2
    assert meta["num_samples"] == 2          # compat with ModelForge's own
    assert meta["categories"] == ["trading-reflector"]
    assert meta["sources"] == ["trading-bot"]
    assert "generation" in meta
    assert "timestamp_utc" in meta


def test_end_to_end_role_summary_counts(populated_root: Path):
    """Per-role accept counts match the fixture's pass/fail mix."""
    pytest.importorskip("datasets")
    stats = curate_mod.curate(dt.date(2026, 5, 11), root=populated_root)
    assert stats.by_role["trading-bull"].accept_count == 1
    assert stats.by_role["trading-bear"].accept_count == 1
    assert stats.by_role["trading-arbiter"].accept_count == 1
    assert stats.by_role["trading-regime-tagger"].accept_count == 1
    assert stats.by_role["trading-indicator-selector"].accept_count == 1


def test_idempotent_state_blocks_rerun(populated_root: Path):
    """Re-running on the same date is a no-op once state is recorded."""
    pytest.importorskip("datasets")
    first = curate_mod.curate(dt.date(2026, 5, 11), root=populated_root)
    second = curate_mod.curate(dt.date(2026, 5, 11), root=populated_root)
    assert first.by_role["trading-reflector"].accept_count > 0
    assert second.by_role["trading-reflector"].accept_count == 0
    assert second.by_role["trading-reflector"].reject_count == 0


def test_per_day_stats_file_written(populated_root: Path):
    """``curate/<role>_<date>.json`` is written per role."""
    pytest.importorskip("datasets")
    curate_mod.curate(dt.date(2026, 5, 11), root=populated_root)
    out = populated_root / "curate" / "trading-reflector_2026-05-11.json"
    assert out.exists()
    parsed = json.loads(out.read_text())
    # The ``RoleCurationResult.as_dict()`` emits ``track_id`` per the spec at
    # docs/superpowers/specs/2026-05-17-trading-data-pipeline-rebuild.md
    # Section D — the older ``role`` key was renamed for cross-stack
    # consistency with model-forge's evolution.start config payload.
    assert parsed["track_id"] == "trading-reflector"
    assert parsed["accept_count"] == 2
    assert "accept_rate" in parsed


def test_accept_rate_alert_fires(populated_root: Path, monkeypatch):
    """A high reject rate triggers the notifier shim."""
    pytest.importorskip("datasets")

    alerts: list[str] = []
    monkeypatch.setattr(curate_mod, "_notify", lambda msg: alerts.append(msg))

    # With band [50%, 60%], the reflector's 50% rate is on the boundary --
    # use [60%, 90%] to force an under-band trip on the reflector.
    curate_mod.curate(
        dt.date(2026, 5, 11),
        root=populated_root,
        accept_rate_lo=0.60,
        accept_rate_hi=0.90,
    )
    # reflector at 2/4 = 50% triggers; arbiter at 1/2 = 50% triggers too.
    assert any("trading-reflector" in m for m in alerts)


def test_cli_main_exit_zero(populated_root: Path, monkeypatch, capsys):
    """CLI returns 0 and prints a summary line."""
    pytest.importorskip("datasets")
    rc = curate_mod.main(["2026-05-11", "--root", str(populated_root), "--quiet"])
    assert rc == 0


def test_cli_bad_date_exit_zero(tmp_path: Path):
    """Bad date arg is fail-soft."""
    rc = curate_mod.main(["not-a-date", "--root", str(tmp_path)])
    assert rc == 0


def test_empty_role_input_no_crash(tmp_path: Path):
    """A role with no raw files produces a zero result, no exception."""
    root = tmp_path / "dgx-train"
    (root / "raw" / "trading-reflector").mkdir(parents=True)
    stats = curate_mod.curate(dt.date(2026, 5, 11), root=root)
    refl = stats.by_role.get("trading-reflector")
    assert refl is not None
    assert refl.accept_count == 0
    assert refl.reject_count == 0
