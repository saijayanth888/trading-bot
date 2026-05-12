# 05 — Parallel Agents Research

**Branch:** `feat/quanta-core-v4-design-r5`
**Status:** Research only. No code. Not pushed.
**Date:** 2026-05-12
**Scope:** Replace today's sequential Bull → Bear → Arbiter → Reflector chain (5–20 s end-to-end) with a concurrent panel that closes in < 500 ms on DGX with all 8B-class models resident in VRAM.

This is a design memo, not a contract. Numbers come from cited sources; recommended choices are flagged "REC" so the implementer can argue back.

---

## 1. Executive Recommendation

### 1.1 Framework — LangGraph + native `asyncio.TaskGroup`

**Recommended stack:**

| Layer | Choice | Why |
|---|---|---|
| Orchestration | **LangGraph** (graph + Send API + reducers) | Fastest of the big three on independent 2,000-run latency benchmark; only mainstream framework with first-class fan-out / fan-in via `Send` and reducer-merged state; production-friendly checkpointing. [1][9] |
| Concurrency primitive | **`asyncio.TaskGroup`** (Python 3.11+, or `anyio.create_task_group`) | `asyncio.gather()` silently swallows errors and orphans tasks; TaskGroup gives structured concurrency, ExceptionGroup, scope-bound cleanup, layered timeouts. [12] |
| Inference server | **vLLM** for the 6 role agents (resident, batched); Ollama only acceptable if VRAM headroom forces single engine | vLLM hits 793 tok/s vs Ollama's 41 tok/s at concurrency; vLLM P99 80 ms vs Ollama 673 ms in Red Hat bench. [5] Ollama needs `OLLAMA_NUM_PARALLEL≥6` and context × parallel VRAM growth — fine for prototype, not for budget. [6] |
| Schema | **Pydantic** `BaseModel` with `response_format` JSON-schema mode on every role | Native-output mode forces token-level schema compliance; prompted-JSON retries are the #2 latency killer. [10][11] |

**Reject:**
- **CrewAI** — role/task DSL is sequential by default, ~3× token footprint on simple flows, no native concurrent dispatch. [1]
- **AutoGen v0.2 GroupChat** — debate is turn-based by construction; can't hit 500 ms with 6 sequential turns at 250 ms each. [1][2]
- **OpenAI Swarm / Agents SDK** — handoffs are sequential; "does not natively support parallel agent execution or graph-based routing." [2]
- **DSPy** — fits a different problem (prompt compilation); orthogonal to fan-out. We may use it later to optimize individual role prompts.

### 1.2 Concurrency model — Panel + Arbiter (not Debate)

**REC: single-round blind panel, no inter-agent visibility, deterministic arbiter, optional second round only on tie or low confidence.**

Rationale, cited:
- Du/Li/Tenenbaum debate (3 agents × 2 rounds, agents see each other) improves accuracy on knowledge tasks **but the second round adds ~250–500 ms per agent** and recent ACL'25 work shows "increasing discussion rounds slightly reduces accuracy"; the Collective Improvement variant that **hides full history and shows only previous-turn solutions improves performance by 7.4%**. [3][4]
- "Voting protocols improve performance by 13.2% in reasoning tasks; consensus protocols by 2.8% in knowledge tasks." Trading "should-we-enter-this-trade" is reasoning, not retrieval — **voting wins**. [4]
- "Making confidences visible provides limited benefit and can induce over-confidence cascades. Hiding confidences is generally preferred." Also: "majority opinion in MAD can strongly suppress independent correction." [4]

Translation: have the 6 role models reason **in parallel, blind to each other**, then let a typed Arbiter combine their JSON outputs. Round 2 (debate) is reserved for the disagreement case (Section 6).

---

## 2. Sequence Diagrams

### 2.1 Today (sequential, 5–20 s)

```
t=0      Bull          (LLM call, ~300–2000 ms)         payload→S1
t=B      Bear          (reads Bull) (~300–2000 ms)      payload→S2
t=B+B'   Arbiter       (reads S1+S2) (~300–2000 ms)     decision→S3
t=B+B'+A Reflector     (reads S3) (~300–2000 ms)        critique→S4
                                                         ───────
                                                         5–20 s end-to-end
```
Tail latency dominates: any single agent above its budget pushes the chain. No back-pressure, no per-step deadline.

### 2.2 Proposed (concurrent panel, target p95 < 500 ms)

```
t=0       Universe state assembled (cached; < 5 ms)
          │
          ├──► Bull       ─┐
          ├──► Bear        │
          ├──► Macro       │   six concurrent vLLM calls,
          ├──► Quant        ── all reading the SAME prompt + market state;
          ├──► Risk        │   each writes one Pydantic JSON to shared
          ├──► Catalyst   ─┘   state via operator.add reducer.
          │
          │   asyncio.TaskGroup wraps all six with:
          │     · per-agent timeout 350 ms
          │     · TaskGroup-level deadline 400 ms
          │
t≈400 ms  Arbiter (deterministic Python, not an LLM):
          score = Σ wᵢ · vote_i · confidence_i
          tie-break / abstain → see §4
          │
t≈420 ms  Persist DecisionRecord (DB + chat_json) — Reflector deferred
          │
t≈420 ms  Return action to caller.

Reflector runs OUT OF BAND (fire-and-forget background task, 30–60 s
budget) and writes a post-hoc critique row keyed by decision_id.
```

**Three structural changes vs today:**
1. Reflector exits the critical path.
2. Arbiter is **deterministic** by default (weighted vote, not an LLM call). LLM-arbiter is a fallback only when the deterministic path can't decide (§4.4).
3. Panel runs blind — no agent sees another's output in round 1. This is the cited recommendation and also the cheapest way to parallelize. [3][4]

---

## 3. Per-Role I/O Schema

All six roles consume the same prompt skeleton: `{symbol, regime_features, last_n_bars, kb_context, account_state, ts}`. The schema below is enforced via vLLM `guided_json` (or Outlines / xgrammar) so the model cannot emit invalid output. [10][11]

### 3.1 Common `AgentVote` schema (every role returns this)

```python
class AgentVote(BaseModel):
    role: Literal["bull","bear","macro","quant","risk","catalyst"]
    vote: Literal["LONG","SHORT","FLAT","ABSTAIN"]
    conviction: float   # 0.0–1.0
    horizon_min: int    # holding period intent
    rationale: str      # ≤ 280 chars, used in audit trail only
    evidence_keys: list[str]  # references into the shared state, NOT free-form
    schema_version: Literal["v1"]
```

`evidence_keys` is the key audit invariant. Roles cite the slice of state they used (e.g. `"regime.trend_strength"`, `"kb.earnings.AMD_2026-05-06"`); the Arbiter and the Reflector replay against the same state snapshot, so disagreement is debuggable.

### 3.2 Per-role specialization

| role | reads (subset of state) | extra prompt frame | typical inference cost |
|---|---|---|---|
| Bull | bars, regime, KB-bull-context | "Argue the strongest case for LONG. Be specific. Cite evidence_keys." | ~200 input / ~120 output tok |
| Bear | bars, regime, KB-bear-context | symmetric, SHORT bias | ~200 / ~120 |
| Macro | regime, macro KB, calendar | "Top-down: regime + macro calendar only." | ~250 / ~100 |
| Quant | bars, features, model_signal | "Signals + features only. No news, no narrative." | ~150 / ~80 |
| Risk | account_state, vol, drawdown | "Block trades that violate risk envelope. ABSTAIN if neutral." | ~180 / ~80 |
| Catalyst | KB-news, earnings, options-IV | "Event-driven only. Argue catalyst pull." | ~250 / ~120 |

### 3.3 `DecisionRecord` (Arbiter output, what we persist)

```python
class DecisionRecord(BaseModel):
    decision_id: UUID
    ts: datetime
    symbol: str
    state_snapshot_hash: str         # for replay
    action: Literal["LONG","SHORT","FLAT","DEFERRED"]
    size_hint: float                 # 0.0–1.0 of allowed
    weighted_score: float            # raw arbiter math
    consensus: Literal["unanimous","majority","split","tie"]
    panel: list[AgentVote]           # all 6, including ABSTAIN
    arbiter_method: Literal["weighted_vote","llm_tiebreak","veto_risk"]
    arbiter_latency_ms: int
    panel_latency_ms: int            # max() over panel, not sum
    reflector_id: Optional[UUID]     # filled async later
```

This is the single audit-trail row. One row per decision. Pluck `panel` to reconstruct any disagreement.

---

## 4. Aggregator Algorithm

### 4.1 Core formula (REC: weighted vote, blind panel)

```
For each role i in {bull, bear, macro, quant, risk, catalyst}:
    direction_i ∈ {+1 (LONG), -1 (SHORT), 0 (FLAT/ABSTAIN)}
    weight_i    ∈ tunable per-role weight, default 1.0
    conviction_i ∈ [0,1]

score = Σ ( weight_i · direction_i · conviction_i )    over non-ABSTAIN
n_valid = count of non-ABSTAIN votes

if n_valid < QUORUM (default 4 of 6):
    action = DEFERRED;  consensus = "tie";  method = "veto_quorum"
elif Risk role voted FLAT with conviction ≥ 0.7:
    action = FLAT;      consensus = ?;       method = "veto_risk"      # hard veto
elif |score| < THRESHOLD (default 0.6):
    → tie-break (§4.3)
else:
    action = LONG if score > 0 else SHORT
    size_hint = min(1.0, |score| / SCORE_FULL_SIZE)
    consensus = "unanimous" if all non-abstain agree, else "majority"
```

Default weights start at 1.0 across the board. We re-fit them weekly from Reflector outcome data (§6.4). This is the **only** learned component in the loop — everything else is auditable rule code.

### 4.2 Why deterministic, not LLM-arbiter

- An LLM arbiter adds one more 200–400 ms inference call and a sycophancy risk: AgentAuditor research shows "judges tend to favor majority positions even when wrong." [8]
- Auditing a `score = Σ w·d·c` formula is trivial; auditing why an LLM judge picked Bull is not.
- Deterministic arbiter is **0–5 ms**, leaving the latency budget intact.

### 4.3 Tie-break ladder (executed in order)

1. **Risk hard veto** — if Risk voted FLAT/ABSTAIN with conviction ≥ 0.7, action = FLAT. Documented evidence: ACL'25 paper notes "consensus protocols offer advantages by requiring the agreement of the majority of agents, providing built-in error detection" for high-stakes domains. [4]
2. **Macro tilt** — if Macro's direction conflicts with the raw majority, downgrade size_hint by 50%.
3. **Re-poll with visibility** — if still tied, fire a *second* round of Bull + Bear only (2 agents, not 6), now showing them the round-1 panel JSON. Budget +250 ms. Cap: one re-poll per decision.
4. **LLM arbiter fallback** — if step 3 still ties, call a small judge model (same 8B class, separate role prompt) with all 6 round-1 votes as input. This is the only place the arbiter LLM ever fires. Method label = `"llm_tiebreak"`.
5. **Final fallback** — action = FLAT, method = `"abstain"`. Better to miss a trade than to coin-flip with real capital.

### 4.4 Why the re-poll is Bull + Bear only

- AgentAuditor & ACL'25 both find majority-vote failure is "confabulation consensus" — agents reinforce each other's biases. [8][4] Adding more voices to a tied panel does not help; **adversarial pair-wise debate does**.
- 2 agents × 1 round × 250 ms = +500 ms wall-clock, but it only triggers on ties, so it does not blow the p50 budget.

---

## 5. Latency Budget Breakdown

Target: **p95 < 500 ms** decision-to-action. p50 should sit at ~350 ms.

| stage | budget (ms) | source / assumption |
|---|---|---|
| State assembly (Redis-cached features, bars, KB pointers) | 5–15 | Streamkap latency budget cites Redis 1–5 ms, vector search 10–50 ms. [11] |
| Prompt assembly (templating, 6× variants) | 5 | Trivial; pre-templated. [11] |
| Concurrent panel inference (6× 8B-class vLLM calls) | 250–350 | 8B FP16 on resident GPU ~38–45 tok/s single-stream; vLLM batched serves 6 concurrent requests near-flat at ~250 ms for ~100 output tokens. [5][6] |
| Pydantic / guided-JSON validation | 5 | In-process; guided_json prevents retry. [10] |
| Arbiter (deterministic Python) | 1–5 | Pure CPU math, no I/O. |
| Decision persist (async write, non-blocking) | 0 in critical path | `asyncio.create_task(persist())` |
| **p50 total** | **~270–380** | |
| **p95 total** | **~480** | adds tail of slowest panel agent |
| Reflector | OUT of critical path | runs as separate task, 30–60 s tolerable |
| Re-poll (tie only, +500 ms) | only when triggered | acceptable, rare path |

**Feasibility verdict:** **Yes, sub-500ms is feasible** under three conditions:
1. **All 6 role models resident in VRAM at start.** DGX has the headroom; cold-load on first call would blow the budget by 5–10 s.
2. **vLLM with PagedAttention, not Ollama, for the hot path.** Ollama's per-request VRAM growth and `OLLAMA_NUM_PARALLEL` defaults will tail-latency us. [5][6]
3. **Output cap at ~120 tokens per agent.** Each agent writes a structured vote, not an essay. The rationale field is hard-capped at 280 chars in schema.

If any of those slip, we miss the budget — caller should fall back to last-known regime decision, not block.

---

## 6. Failure Handling

### 6.1 Per-agent timeout

`asyncio.TaskGroup` with `anyio.fail_after(0.35)` around each role call:
- 350 ms hard per-agent.
- If 1 agent times out → treat as ABSTAIN, document `agent_status = "timeout"` in the AgentVote row. Panel proceeds with 5 valid votes (quorum = 4, so we survive 2 timeouts).
- If 3+ agents time out → action = DEFERRED, no trade. Page the operator.

This matches the structured-concurrency recommendation: layered timeouts (per-tool / per-fan-out / per-turn / workflow) so one slow node never poisons the whole superstep. [12]

### 6.2 Disagreement (Bull = LONG, Bear = SHORT) — the audit trail

Every decision row contains the full `panel: list[AgentVote]`, the `weighted_score`, the `consensus` label, and the `arbiter_method`. So a SHORT decision where Bull voted LONG looks like:

```json
{
  "decision_id": "…",
  "symbol": "SOL",
  "action": "SHORT",
  "weighted_score": -1.4,
  "consensus": "majority",
  "panel": [
    {"role":"bull","vote":"LONG","conviction":0.65,"evidence_keys":["kb.news.SOL_2026-05-11"]},
    {"role":"bear","vote":"SHORT","conviction":0.80,"evidence_keys":["regime.trend_strength","bars.bb_width"]},
    {"role":"macro","vote":"SHORT","conviction":0.70,"evidence_keys":["macro.dxy_5d"]},
    {"role":"quant","vote":"SHORT","conviction":0.55,"evidence_keys":["features.mom_20d"]},
    {"role":"risk","vote":"FLAT","conviction":0.40,"evidence_keys":["acct.dd_4w"]},
    {"role":"catalyst","vote":"ABSTAIN","conviction":0.0,"evidence_keys":[]}
  ],
  "arbiter_method": "weighted_vote"
}
```

This is "audit-friendly disagreement": one row, every voice preserved, every evidence pointer replayable against the snapshot hash. AgentAuditor's recommendation: keep the *reasoning tree*, not just the verdict. [8] We keep votes + evidence_keys; the reasoning replay is the prompt itself plus the state snapshot.

### 6.3 All-agree-but-wrong (the dangerous case)

Six 8B models pretrained on overlapping corpora **can confabulate together**. Mitigations, layered:

1. **Heterogeneous prompts and tool-access per role.** Quant only sees features; Catalyst only sees news; Risk only sees account state. This forces them to reason from different inputs, breaking "shared blind spot." Cited rationale: ACL'25 paper finds independent-information panels outperform full-context panels. [4]
2. **Risk role has hard veto power** even on unanimous decisions when account drawdown exceeds threshold. (§4.3 step 1.)
3. **Reflector trains a "consensus-was-wrong" classifier offline** from outcomes — when N decisions in a row are unanimous and losers, raise the Risk weight automatically (the only learned knob). This is AgentAuditor's "Anti-Consensus Preference Optimization" idea adapted to weights instead of fine-tuning. [8]
4. **Circuit breaker.** If the 24-hour realized-vs-predicted Brier score on unanimous decisions degrades by > X, the loop switches to `DEFERRED` and pages.

### 6.4 Reflector role (out of band)

Reflector runs *after* the trade resolves (T+horizon), reads the DecisionRecord + actual P&L, writes a critique row. Its outputs feed:
- Weekly weight re-fit for the arbiter.
- Per-role "calibration score" (does conviction predict P&L?) — surfaces in dashboard.
- The "consensus-was-wrong" classifier above.

It is not on the hot path. It does not see live state. It only ever reads sealed history.

---

## 7. Build Cost Estimate

Assumes one engineer (operator) plus Claude pair. Times are wall-clock hours of focused work.

| chunk | what | hours |
|---|---|---|
| 1. vLLM serving setup for 6 resident 8B models | docker-compose or systemd, GPU memory plan, `--guided-decoding-backend xgrammar`, health checks | 4–6 |
| 2. Pydantic schemas + `guided_json` wiring | `AgentVote`, `DecisionRecord`, snapshot hashing | 2 |
| 3. LangGraph fan-out graph | one supervisor node, 6 worker nodes via `Send`, `operator.add` reducer on `panel: list[AgentVote]` | 4 |
| 4. TaskGroup + timeout layering | per-agent 350 ms, group 400 ms, retries off | 2 |
| 5. Deterministic arbiter | the scoring function + tie-break ladder + tests | 3 |
| 6. Persistence + audit log | DB schema for `DecisionRecord`, chat_json mirror | 3 |
| 7. Dashboard panel ("today's decisions") | one card: row per decision, expandable to show 6 votes | 4 |
| 8. Reflector job (cron) | reads outcomes, writes critique, weight-refit script | 4 |
| 9. Failure-mode tests | timeout sim, all-abstain sim, tie sim, risk-veto sim | 3 |
| 10. Shadow-mode rollout | run new loop alongside old, log only, compare for 1 week | 2 (setup) + 1 week wall-clock |
| **Total engineering** | | **~31 hours** (~1 focused week) |

Plus ~1 week of shadow comparison before cutover. Total calendar: **2 weeks** from green-light to live cutover.

**Risks that could double the estimate:**
- vLLM guided-JSON conflict with chosen model family — fallback is Outlines, but it's another integration day.
- Per-role prompt engineering taking longer than budgeted (the 6 prompts are the soul of the system — they will iterate).
- DGX VRAM accounting: 6 × 8B FP16 ≈ 100 GB resident. If we don't have it, quantize to FP8 or share base weights with role-specific LoRAs (separate design memo).

---

## 8. Sources

1. [CrewAI vs LangGraph vs AutoGen 2026: Benchmarks](https://pooya.blog/blog/crewai-vs-langgraph-autogen-comparison-2026/) — Pooya Golchian (latency rankings, token efficiency, completion rates by task complexity).
2. [The 2026 AI Agent Framework Showdown](https://qubittool.com/blog/ai-agent-framework-comparison-2026) — QubitTool (OpenAI Agents SDK sequential handoffs; AutoGen v0.2 dialogue debate).
3. [Improving Factuality and Reasoning in LLMs through Multiagent Debate](https://arxiv.org/abs/2305.14325) — Du, Li, Torralba, Tenenbaum, Mordatch, ICML 2024 (3 agents × 2 rounds, society-of-minds debate baseline).
4. [Voting or Consensus? Decision-Making in Multi-Agent Debate](https://arxiv.org/html/2502.19130v4) — ACL Findings 2025 (voting +13.2% on reasoning, consensus +2.8% on knowledge; Collective Improvement +7.4% by hiding history; tie-break analysis; hidden confidences preferred).
5. [Ollama vs. vLLM: A deep dive into performance benchmarking](https://developers.redhat.com/articles/2025/08/08/ollama-vs-vllm-deep-dive-performance-benchmarking) — Red Hat Developer (vLLM 793 tok/s vs Ollama 41 tok/s; P99 80 ms vs 673 ms).
6. [How Ollama Handles Parallel Requests](https://www.glukhov.org/post/2025/05/how-ollama-handles-parallel-requests/) — Glukhov (`OLLAMA_NUM_PARALLEL`, context × parallel VRAM scaling, 20–40% per-request latency penalty under concurrency).
7. [TradingAgents: Multi-Agents LLM Financial Trading Framework](https://arxiv.org/abs/2412.20138) + [DeepWiki architecture](https://deepwiki.com/TauricResearch/TradingAgents) — Tauric Research (5-phase pipeline: parallel analysts → sequential research debate → trader → risk → portfolio manager; bull/bear debate moderated by Research Manager; structured outputs since v0.2.4).
8. [Auditing Multi-Agent LLM Reasoning Trees Outperforms Majority Vote and LLM-as-Judge](https://arxiv.org/abs/2602.09341) — AgentAuditor (Divergence Packets, +3% vs majority vote, recovers 65% of majority-failure cases, Anti-Consensus Preference Optimization).
9. [Scaling LangGraph Agents: Parallelization, Subgraphs, and Map-Reduce Trade-Offs](https://aipractitioner.substack.com/p/scaling-langgraph-agents-parallelization) — AI Practitioner (fan-out superstep, `max_concurrency`, reducer-merged state, `defer=True` for uneven branches).
10. [Pydantic AI — Output and Structured Outputs](https://ai.pydantic.dev/output/) — Pydantic (native JSON-schema mode forces token-level compliance; prompted mode is least reliable).
11. [Agent Decision Latency Budget: Where Time Goes in Every AI Agent Request](https://streamkap.com/resources-and-guides/agent-decision-latency-budget) — Streamkap (Real-Time Decision Agent template: <500 ms requires Redis 10 ms + small local model 200 ms; per-stage inference 100–500 ms for small models).
12. [Structured Concurrency for AI Pipelines: Why asyncio.gather() Isn't Enough](https://tianpan.co/blog/2026-04-09-structured-concurrency-ai-pipelines-parallel-tool-calls) — Tian Pan (TaskGroup vs gather, layered timeouts, ExceptionGroup, two-layer rate limiting).
13. [Why Do Multi-Agent LLM Systems Fail?](https://arxiv.org/pdf/2503.13657) — Cemri, Pan, Yang et al. (failure taxonomy: comm/coord, task spec, context loss, capability gap, hallucination; mitigations).
14. [Constitutional AI: Harmlessness from AI Feedback](https://arxiv.org/abs/2212.08073) — Anthropic (Constitutional AI as a scalable-oversight precedent; principle-based self-critique applicable to a "trading constitution" for the Risk role).
15. [LangGraph Map-Reduce with Send API](https://medium.com/ai-engineering-bootcamp/map-reduce-with-the-send-api-in-langgraph-29b92078b47d) — concrete `Send` usage, runtime-discovered fan-out width, reducer composition.

---

**End of memo. No code shipped. Next step: operator reviews and either green-lights a separate implementation plan or sends edits back.**
