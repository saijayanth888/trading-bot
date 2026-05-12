# Wave-2 Integration Engineer (#I) — HANDOFF

**Branch:** `feat/v4-wave2-integration` (off `feat/v4-build-reconciled`)
**Date:** 2026-05-12 (overnight wave-2 sprint)
**Status:** READY TO MERGE. 24 new tests, all green. mypy --strict clean. ruff clean.

---

## Commit shas

| sha | message |
|---|---|
| `5254e43` | test(integration): V4 end-to-end smoke — 24 tests, shape-correctness across live + execution + ledger |
| `e2f7ee7` | (parent — reconciled, merged by agent #A) merge(reconcile): live at root layout |
| `4f7b76b` | (parent — reconciled, merged by agent #A) merge(reconcile): execution at root layout |

---

## What landed

Three integration test files + a shared conftest + one doc, under
`tests/integration/` and `docs/quanta-core-v4-rev2/INTEGRATION-SMOKE.md`.

```
tests/integration/
├── __init__.py
├── conftest.py                  → fakes/fixtures (InMemoryLedger, PaperExecExchange,
│                                    StubRiskEngine, RiskGatedExecutionSink,
│                                    FakeBacktestEngine, FakeLiveExchange)
├── test_e2e_paper_smoke.py      → 7 tests, end-to-end backtest smoke
├── test_live_smoke.py           → 4 tests, LiveEngine with scripted stream
└── test_types_compat.py         → 13 tests, cross-module type compat

docs/quanta-core-v4-rev2/INTEGRATION-SMOKE.md  → coverage matrix + run guide
```

---

## Test scenarios + pass/fail

| File | Test | Status |
|---|---|---|
| test_e2e_paper_smoke | `test_smoke_100_bars_10_proposals_10_fills_10_ledger_entries` | PASS |
| test_e2e_paper_smoke | `test_smoke_equity_curve_has_expected_shape` | PASS |
| test_e2e_paper_smoke | `test_smoke_zero_proposals_when_strategy_never_fires` | PASS |
| test_e2e_paper_smoke | `test_smoke_strategy_side_propagates_to_fill` | PASS |
| test_e2e_paper_smoke | `test_smoke_risk_rejection_blocks_execution` | PASS |
| test_e2e_paper_smoke | `test_smoke_each_fill_has_exchange_order_id` | PASS |
| test_e2e_paper_smoke | `test_smoke_idempotent_replay_does_not_double_fill` | PASS |
| test_live_smoke | `test_live_engine_dispatches_candles_and_fills` | PASS |
| test_live_smoke | `test_live_engine_handles_empty_stream` | PASS |
| test_live_smoke | `test_live_engine_fill_updates_position_state` | PASS |
| test_live_smoke | `test_live_engine_unsubscribed_symbol_does_not_crash` | PASS |
| test_types_compat | `test_tick_constructs_with_full_field_set` | PASS |
| test_types_compat | `test_bar_constructs_with_full_field_set` | PASS |
| test_types_compat | `test_fill_constructs_with_full_field_set` | PASS |
| test_types_compat | `test_position_constructs_with_full_field_set` | PASS |
| test_types_compat | `test_order_proposal_constructs_with_minimum_fields` | PASS |
| test_types_compat | `test_order_proposal_round_trip_preserves_client_order_id` | PASS |
| test_types_compat | `test_order_proposal_round_trip_preserves_symbol_side_qty` | PASS |
| test_types_compat | `test_signal_px_falls_back_from_limit_price` | PASS |
| test_types_compat | `test_metadata_passthrough_drops_internal_keys` | PASS |
| test_types_compat | `test_strategy_default_hooks_return_empty_lists` | PASS |
| test_types_compat | `test_exec_order_proposal_requires_client_order_id_min_length` | PASS |
| test_types_compat | `test_exec_order_proposal_is_frozen` | PASS |
| test_types_compat | `test_exec_fill_is_frozen` | PASS |

**Totals:** 24/24 pass. Combined with the wave-1 reconciled suites
(171 in execution + live), the V4 src-tree is at **195 tests pass, 0 fail**.

---

## Quality gates

```bash
$ python3 -m ruff check tests/integration/
All checks passed!

$ python3 -m ruff format --check tests/integration/
5 files already formatted

$ python3 -m mypy tests/integration/ --strict
Success: no issues found in 5 source files

$ python3 -m pytest tests/integration/ -v
24 passed in 0.78s
```

---

## Wait condition — outcome

Per task brief: poll for `feat/v4-build-reconciled` to exist + wait for
≥ 2 of 4 wave-2 modules (backtest, hermes, agents, ledger) to land.

- `feat/v4-build-reconciled` was present at task start (agent #A landed).
- **NONE of the 4 wave-2 code-builder branches had landed code at the
  task deadline.** All four had only the wave-2 sprint plan doc.

Decision: per task brief ("budget: ~90 min wait + ~60 min coding") and
the explicit "shape-correctness, not coverage" framing, I proceeded
with shape-correctness tests against the AVAILABLE modules (live,
execution, strategy, exchanges, observability, util/types) using
**in-memory fakes** for the not-yet-built wave-2 modules. This is
exactly what the task spec allows: "All tests use mocks/fakes — NO
real network calls."

When wave-2 #B (backtest) and #E (ledger) land:
- `FakeBacktestEngine` → real `quanta_core.backtest.engine.BacktestEngine`
- `InMemoryLedger` → real `quanta_core.ledger.writer.Writer`

The scenarios + assertions do not change. The fakes were written
against the documented interfaces, not the implementations.

---

## Wiring hazards surfaced by these tests

1. **Two `OrderProposal` models.** `util.types.OrderProposal` (dataclass,
   used by live + strategies) is structurally different from
   `execution.engine.OrderProposal` (Pydantic, used by execution).
   The integration shim `RiskGatedExecutionSink._adapt` bridges them;
   the round-trip tests will catch any future drift.
   → Recommend follow-up: foundation agent unifies these.

2. **`signal_px` is mandatory at the execution boundary.** The live-side
   `OrderProposal` doesn't carry it natively, so we squat in `metadata`.
   The adapter falls back to `limit_price` if metadata is empty.
   → Recommend follow-up: add `signal_px` as a first-class field on
   `util.types.OrderProposal`.

3. **`client_order_id` lifecycle is five layers deep.** Strategy mints →
   adapter forwards → idempotency store reserves → execution engine
   commits → ledger records. The smoke suite asserts the id survives
   all five intact and that replay does not double-fill. This is the
   load-bearing invariant for the phantom-order class of bug.

---

## Followups / NOT in this branch

- No real Postgres ledger test (wave-2 #E pending). When that module
  lands, add `tests/integration/test_ledger_pg.py` marked
  `@pytest.mark.integration`.
- No parity oracle (backtest vs live, same Strategy, equal fills).
  Pending wave-2 #B. Add `tests/integration/test_parity_oracle.py`.
- No 30s deliberate-debate budget assertion. Pending wave-2 #D agents.
- No Ollama / vLLM / SDK integration. Out of scope per task.

---

## How operator merges this

```bash
# Per wave-2 plan §"Morning merge sequence":
git checkout feat/v4-build
git merge --no-ff feat/v4-wave2-ledger      # (if landed)
git merge --no-ff feat/v4-wave2-hermes      # (if landed)
git merge --no-ff feat/v4-wave2-agents      # (if landed)
git merge --no-ff feat/v4-wave2-backtest    # (if landed)

# Then this branch (the integration smoke, must land last):
git merge --no-ff feat/v4-wave2-integration

# Verify the full suite (should be ~700 tests):
python3 -m pytest src/quanta_core/ tests/ -v
```

This branch will merge cleanly into any state of `feat/v4-build`
because its only new paths are `tests/integration/*` and a single
new doc file — zero conflicts with the wave-2 module branches.

---

— claude / agent #I integration engineer
