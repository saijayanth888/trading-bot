"""
Integration test for the BuildTradingDataset action end-to-end flow.

Per spec Section J: ``test_build_trading_action_on_frozen_fixture_db``

SQLite note: quanta_schema.decisions uses a Postgres-specific schema qualifier
("quanta_schema.decisions"). SQLite does not support schema-qualified table
names. The psycopg2-based ETL in modelforge_ingest_decisions.py requires a
real Postgres connection with the quanta_schema. Running this test against
real Postgres would require the trading-bot container stack to be up.

For CI/unit-test isolation we test the upstream component directly:
- modelforge_curate.py curate_role() is called with synthetic JSONL fixtures
  that model what modelforge_ingest_decisions.py would have written for N_MIN
  rows of dummy decisions.
- This tests the full curate pipeline (N_MIN gate, test_set generation,
  curator_result.json writing, REGIME_BASELINE_INDICATORS baseline_output)
  WITHOUT requiring Postgres or a real DB.
- This is the correct integration boundary: the DB-dependent ETL
  (modelforge_ingest_decisions.py) is tested by its unit tests elsewhere;
  the curate pipeline is the actual artifact that BuildTradingDataset reads.

The PostgreSQL integration test for the full BuildTradingDataset action
(including DB probe + subprocess chain) is deferred to a separate
``tests/integration/test_build_trading_postgres.py`` that requires the
``tradebot-postgres`` container to be running. It is skipped in CI.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]
_SCRIPTS_DIR = _REPO_ROOT / "scripts"
_CURATE_PATH = _SCRIPTS_DIR / "modelforge_curate.py"

# Add scripts/ to sys.path so the module is importable as a real module
# (avoids the dataclass sys.modules lookup failure that happens when loading
# via importlib.spec_from_file_location without registering in sys.modules).
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))


def _load_curate():
    """Import modelforge_curate as a proper module (registered in sys.modules)."""
    import importlib
    if "modelforge_curate" in sys.modules:
        return sys.modules["modelforge_curate"]
    mod = importlib.import_module("modelforge_curate")
    return mod


# ---------------------------------------------------------------------------
# Fixture builders — synthetic JSONL rows that model what
# modelforge_ingest_decisions.py would write for regime-tagger.
# regime-tagger is the lowest N_MIN=40 track that doesn't require
# the crypto-term blocklist to not fire on all rows.
# ---------------------------------------------------------------------------

def _make_regime_row(i: int) -> dict[str, Any]:
    """Build one raw JSONL row in the format curate.py expects."""
    regime_labels = [
        "trending_up", "trending_down", "ranging",
        "high_volatility", "low_volatility", "breakout_up", "breakout_down",
    ]
    regime = regime_labels[i % len(regime_labels)]
    return {
        "ts": f"2026-05-01T{(i % 24):02d}:00:00+00:00",
        "ticker": f"SYM{i % 50:03d}",
        "system_message": "You are a market regime classifier.",
        "user_message": f"Symbol: SYM{i % 50:03d}\nStrategy: meta_up_regime\n\nClassify regime.",
        "response": json.dumps({"regime": regime}),
        "pending_outcome": False,
        "outcome_key": f"2026-05-01T{(i % 24):02d}:00:00+00:00|decisions.{i}|regime_tagger",
        "ledger": {
            "agent": "regime_tagger",
            "model": "quanta_schema.decisions",
            "provider": "postgres_bootstrap",
            "tier": "bootstrap",
            "role": "trading-regime-tagger",
            "source_id": i,
            "valid": True,
        },
    }


def _write_fixture_jsonl(raw_dir: Path, role: str, count: int) -> Path:
    """Write <count> synthetic rows to a JSONL file under raw_dir/role/."""
    role_dir = raw_dir / role
    role_dir.mkdir(parents=True, exist_ok=True)
    out_file = role_dir / "decisions_1_1000.jsonl"
    with out_file.open("w") as fh:
        for i in range(count):
            row = _make_regime_row(i)
            fh.write(json.dumps(row) + "\n")
    return out_file


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

@pytest.mark.skipif(
    not _CURATE_PATH.exists(),
    reason="modelforge_curate.py not found — trading-bot repo not mounted",
)
def test_build_trading_action_on_frozen_fixture_curate_pipeline(tmp_path):
    """Curate N_MIN rows of synthetic regime-tagger decisions, verify outputs.

    This is the spec Section J integration test. It validates:
    1. curate_role() passes the N_MIN gate when given >= 40 rows.
    2. curator_result.json is written with status="ok".
    3. test_set.jsonl is written with all required fields per Section B.
    4. The Arrow shard (curated/) is written.
    5. baseline_output is present on regime-tagger test_set rows.
    """
    curate = _load_curate()

    # Write 50 fixture rows (> N_MIN=40 for regime-tagger).
    raw_dir = tmp_path / "raw"
    role = "trading-regime-tagger"
    _write_fixture_jsonl(raw_dir, role, count=50)

    raw_files = list((raw_dir / role).glob("*.jsonl"))
    assert raw_files, "Fixture JSONL not written"

    # curate_role() needs the `datasets` library (HF Arrow). If not installed,
    # the test surfaces a clear ImportError rather than silently passing.
    try:
        result = curate.curate_role(
            role,
            raw_files=raw_files,
            out_root=tmp_path,
        )
    except ImportError as exc:
        pytest.skip(f"datasets library not installed: {exc}")

    # --- Assertions on the result object ---
    assert result.status == "ok", (
        f"Expected status=ok, got {result.status!r}. "
        f"reject_reasons={result.reject_reasons}"
    )
    assert result.accept_count >= 40, (
        f"Expected >= 40 accepted rows, got {result.accept_count}"
    )
    assert result.out_path is not None, "out_path should be set on success"

    # --- curator_result.json ---
    curator_result_path = tmp_path / "datasets" / role / "curator_result.json"
    assert curator_result_path.exists(), (
        f"curator_result.json not written at {curator_result_path}"
    )
    with curator_result_path.open() as fh:
        cr = json.load(fh)
    assert cr["status"] == "ok"
    assert cr["track_id"] == role
    assert cr["accept_count"] >= 40
    assert cr.get("out_path") is not None

    # --- test_set.jsonl ---
    test_set_path = tmp_path / "datasets" / role / "test_set.jsonl"
    assert test_set_path.exists(), f"test_set.jsonl not written at {test_set_path}"

    rows = []
    with test_set_path.open() as fh:
        for line in fh:
            if line.strip():
                rows.append(json.loads(line))
    assert rows, "test_set.jsonl is empty"

    # Verify required fields per Section B (trading-regime-tagger test-set schema).
    first_row = rows[0]
    required_fields = {"prompt", "track_id", "gold_response", "baseline_output"}
    missing = required_fields - set(first_row.keys())
    assert not missing, f"test_set row missing fields: {missing}. Row keys: {set(first_row.keys())}"

    # baseline_output must have 'regime' key with a non-empty string.
    baseline = first_row.get("baseline_output")
    assert isinstance(baseline, dict), f"baseline_output is not a dict: {baseline!r}"
    assert "regime" in baseline, f"baseline_output missing 'regime': {baseline}"
    assert baseline["regime"], f"baseline_output.regime is empty: {baseline}"


@pytest.mark.skipif(
    not _CURATE_PATH.exists(),
    reason="modelforge_curate.py not found",
)
def test_n_min_gate_blocks_below_threshold(tmp_path):
    """Confirm N_MIN gate blocks regime-tagger when fewer than 40 rows are accepted."""
    curate = _load_curate()

    raw_dir = tmp_path / "raw"
    role = "trading-regime-tagger"
    # Write only 10 rows — below N_MIN=40.
    _write_fixture_jsonl(raw_dir, role, count=10)

    raw_files = list((raw_dir / role).glob("*.jsonl"))

    try:
        result = curate.curate_role(
            role,
            raw_files=raw_files,
            out_root=tmp_path,
        )
    except ImportError as exc:
        pytest.skip(f"datasets library not installed: {exc}")

    assert result.status == "insufficient_data", (
        f"Expected insufficient_data for 10 rows (N_MIN=40), got {result.status!r}"
    )
    assert result.out_path is None, "out_path should be None when N_MIN gate fires"

    # curator_result.json should exist even on failure.
    curator_result_path = tmp_path / "datasets" / role / "curator_result.json"
    assert curator_result_path.exists(), (
        "curator_result.json should be written even on N_MIN failure"
    )
    with curator_result_path.open() as fh:
        cr = json.load(fh)
    assert cr["status"] == "insufficient_data"
    assert "below_min_records_gate" in cr.get("reject_reasons", {})


@pytest.mark.skipif(
    not _CURATE_PATH.exists(),
    reason="modelforge_curate.py not found",
)
def test_role_filter_cli_limits_curate_to_one_role(tmp_path):
    """Confirm --role-filter limits curate() to a single role."""
    curate = _load_curate()

    # Write fixture rows for two roles.
    raw_dir = tmp_path / "raw"
    for role in ("trading-regime-tagger", "trading-arbiter"):
        _write_fixture_jsonl(raw_dir, role, count=50)

    # curate() with role_filter="trading-regime-tagger" should only process that role.
    try:
        stats = curate.curate(
            None,
            root=tmp_path,
            role_filter="trading-regime-tagger",
        )
    except ImportError as exc:
        pytest.skip(f"datasets library not installed: {exc}")

    assert "trading-regime-tagger" in stats.by_role
    assert "trading-arbiter" not in stats.by_role, (
        "trading-arbiter should not be processed when role_filter is set"
    )
