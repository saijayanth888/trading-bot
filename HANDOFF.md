# Wave-2 Agents — Debate Orchestrator HANDOFF

**Branch:** `feat/v4-wave2-agents` (off `main`)
**Worktree:** `.claude/worktrees/agent-a9e645e45b4400995/`
**Scope:** `src/quanta_core/agents/` + `tests/agents/`
**Date:** 2026-05-12
**Status:** Build complete · all tests + lints green · NOT pushed

Implements doc `docs/quanta-core-v4-rev2/05-RESEARCH-PARALLEL_AGENTS.md`
(the 30-second deliberate debate, Ollama-only, asyncio.TaskGroup).

---

## 1. Pipeline (doc 05 rev2 §3)

```
t=0.0 s   SetupContext arrives at DebateOrchestrator.deliberate()
              │
              ▼
t=0.0–2.0 s   regime  (hermes3:8b)  ── AgentVote
              │
              ▼
t=2.0–4.0 s   micro   (hermes3:8b)  ── AgentVote
              │
              ▼
t=4.0 s   PRE-SCREEN GATE
              if regime.abstained OR micro.abstained                  → FLAT (pre_screen_veto)
              if regime/micro vote FLAT with conviction ≥ 0.6         → FLAT (pre_screen_veto)
              │
              ▼
t=4.0–14.0 s  bull    (hermes3:70b, BLIND to bear, KB-bull only)   ── AgentVote
              │
              ▼
t=14.0–24.0 s bear    (hermes3:70b, BLIND to bull, KB-bear only)   ── AgentVote
              │
              ▼
t=24.0–28.0 s arbiter (hermes3:70b, sees all 4 votes)              ── ArbiterSynthesis
              │                                                       (rationale only — NO VOTE)
              ▼
t=28.0–29.0 s risk_gate ⊕ micro_gate (concurrent asyncio.TaskGroup) ── RiskState + MicroState
              │
              ▼
t=29.0–30.0 s aggregate()  ── deterministic weighted vote
              │
              ▼
              ┌────────────────────────────────────────────────────────────────┐
              │ if low_conviction AND blind_panel.enable_repoll_for_low_conv   │
              │     trigger ROUND 2 (bull_r2 + bear_r2 with FULL visibility)   │
              │     re-aggregate; FLAT on failure  → repoll_no_consensus       │
              └────────────────────────────────────────────────────────────────┘
              │
              ▼
            DebateResult committed
```

Hard deadline: `45 s` (orchestrator-wide `asyncio.wait_for`). On overshoot
the result is `FLAT, method=veto_quorum`.

---

## 2. 9 FLAT fail-codes (`quanta_core.agents.FailCode`)

| # | Code | Trigger |
|---|------|---------|
| 1 | `unanimous` | (success — not a fail) all 4 agree + risk OK + micro OK + above conviction threshold |
| 2 | `veto_risk_engine` | risk MC gate vetoed OR gate unreachable |
| 3 | `veto_microstructure` | book stale / spread blown / halt OR feed unreachable |
| 4 | `veto_quorum` | < 4 non-abstain panel votes (or hard deadline) |
| 5 | `no_consensus` | panel split — any direction disagreement (incl. all-FLAT) |
| 6 | `low_conviction` | unanimous but `|score| < threshold` (default 1.5) |
| 7 | `pre_screen_veto` | regime/micro abstained OR voted FLAT with conviction ≥ 0.6 |
| 8 | `repoll_no_consensus` | round-2 re-poll fired and still no go |
| 9 | `abstain_default_closed` | bottom-of-ladder fallback (reserved; orchestrator path surfaces it via `veto_quorum`) |

All 9 are documented in `roles.FailCode` and exercised by tests.

---

## 3. File map

### Source (`src/quanta_core/agents/`, 1458 LOC inc. package init)

| File | LOC | Responsibility |
|------|----:|----------------|
| `__init__.py` | 95 | Public API barrel — re-exports + module docstring |
| `roles.py` | 363 | Pydantic schemas (`AgentVote`, `DebateResult`, `RiskState`, `MicroState`, `SetupContext`, `RoleSpec`, `FailCode`, `Direction`, `RoleName`, `ArbiterSynthesis`, `RepollRecord`, `AccountState`) + `DEFAULT_ROLE_SPECS` |
| `aggregator.py` | 290 | Pure-Python deterministic weighted vote + 5-step tie-break ladder (`AggregatorConfig`, `AggregatorDecision`, `aggregate()`) |
| `blind_panel.py` | 196 | Round-1 isolated prompt + round-2 visibility prompt + arbiter prompt assembly |
| `debate.py` | 500 | `DebateOrchestrator` — asyncio.TaskGroup wiring, timeouts, fail-closed defaults, optional round-2 re-poll |

### Tests (`tests/agents/`, 1232 LOC)

| File | LOC | What it locks down |
|------|----:|--------------------|
| `conftest.py` | 222 | `FakeOllama` client + `make_risk_gate` / `make_micro_gate` + `setup_ctx`, `vote_factory`, `arbiter_synthesis` fixtures |
| `test_aggregator.py` | 331 | Every ladder step, every legal direction combo, custom weights, abstain handling, duplicate roles, non-voting roles ignored |
| `test_blind_panel.py` | 154 | Round-1 KB isolation, arbiter rejection from round 1, round-2 visibility, arbiter sees full panel |
| `test_debate.py` | 525 | Happy path (LONG + SHORT) · all 9 fail codes · arbiter timeout doesn't block · wrong-role vote becomes abstain · monkeypatched socket asserts no real network |

---

## 4. Verification (run from worktree root)

```bash
PYTHONPATH=src python3 -m pytest tests/agents/ --cov=src/quanta_core/agents --cov-report=term --cov-fail-under=90
ruff check src/quanta_core/agents tests/agents
PYTHONPATH=src mypy --strict src/quanta_core/agents tests/agents
```

Results at commit time:

```
tests/agents/test_aggregator.py ..........................         [ 44%]
tests/agents/test_blind_panel.py ............                      [ 64%]
tests/agents/test_debate.py .....................                  [100%]
================================ tests coverage ================================
Name                                    Stmts   Miss Branch BrPart  Cover
src/quanta_core/agents/__init__.py          6      0      0      0   100%
src/quanta_core/agents/aggregator.py       37      0      6      0   100%
src/quanta_core/agents/blind_panel.py      27      0     10      0   100%
src/quanta_core/agents/debate.py           96      0     10      0   100%
src/quanta_core/agents/roles.py            92      0      0      0   100%
TOTAL                                     258      0     26      0   100%
Required test coverage of 90% reached. Total coverage: 100.00%
59 passed in 0.90s

ruff: All checks passed!
mypy: Success: no issues found in 10 source files
```

Bars cleared:
* mypy `--strict` ✓
* ruff clean (`E,F,W,I,B,UP,T201,RET,SIM,PIE,PERF`) ✓
* 100 % line + branch coverage on `src/quanta_core/agents/` (target 90 %) ✓
* asyncio.TaskGroup (NOT LangGraph) ✓
* ROOT layout ✓
* No real network calls (monkeypatched socket test ensures this) ✓

---

## 5. Design notes for the next agent

### What's locked

* `FailCode` enum is the contract with the persistence layer. The 9 codes are
  the documented universe; adding one means updating doc 05 rev2 + the
  reflector grader.
* `aggregate()` is pure — no I/O, no LLM. Auditable in ~80 lines.
* Round-1 KB isolation is enforced at the prompt-assembly layer
  (`build_round1_prompt`). bull cannot read `kb_bear_context`, ever.
* Risk + Micro state being `None` is treated as a hard veto (fail-closed per
  doc §6 rows 6 & 7). This is the strongest invariant of the loop.

### Deliberate placeholders to swap later

* `roles.py` prompt templates are 2-sentence stubs. Real prompts get ported
  from `stocks/shark/agents/*.py` (esp. `analyst_bull.py`, `analyst_bear.py`,
  `decision_arbiter.py`) — the `RoleSpec.prompt_template` field is the seam.
* No real Ollama client wrapper here — that's a wave-3 task. The
  `OllamaClient` Protocol in `debate.py` documents the surface. Production
  impl wraps the `ollama` Python SDK + JSON-mode + 1 schema-retry.
* Reflector module is referenced by spec but lives out-of-band — not built
  here. The `RoleName.REFLECTOR` spec exists so the nightly cron has a place
  to look up its model + timeout.
* `pyproject.toml` at worktree root is minimal (just enough for ruff/mypy/
  pytest). When this merges into `feat/v4-build` it will overlap with the
  sibling agents' pyproject — wave-2 integrator should reconcile.

### Tunable defaults (`AggregatorConfig`)

* `quorum=4` — all four voting roles required
* `low_conviction_threshold=1.5` — strict (`<`); `|score|=1.5` trades
* `score_full_size=3.0` — `size_hint=min(1.0, |score|/3.0)`
* `role_weights=None` → all 1.0; future weight-fit lives here

### Round-2 re-poll

* OFF by default (`BlindPanelConfig.enable_repoll_for_low_conviction=False`)
* Per doc §7.3 the 8-week paper window decides whether to leave it on
* When fired, ONLY bull + bear re-vote; regime + micro stay frozen
* A failed round 2 produces `repoll_no_consensus` (FailCode #8)

---

## 6. Commits

```
0bfcfc6 feat(agents): 30s deliberate debate orchestrator (doc 05 rev2)
```

Single squash commit — clean 3009-line atomic add (14 files).

Branch is NOT pushed. Up to the operator / integrator to PR.

---

## 7. Open questions for the integrator

1. **Where does the real Ollama client live?** Probable home is
   `src/quanta_core/llm/` (wave-3). The `OllamaClient` Protocol in
   `debate.py` is the contract.
2. **Where does `StateAssembler` live?** Doc §3 puts it at t=0 building
   `SetupContext`. Not built here; assumed external. Probable home is
   `src/quanta_core/state/`.
3. **Persistence**: doc §5.2 specs a `DecisionRecord` row. Wave-3 owns
   the DB schema + audit log. `DebateResult` is the in-memory shape
   that maps 1:1 onto that row.
4. **Hermes hook**: doc §11 (Hermes Layer 8) is where the deliberation
   gets *triggered* on setup-formation events. Not built here — this
   module exposes `deliberate(setup_context)` as the call site.
