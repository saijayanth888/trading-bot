# 05 — Parallel Agents Research (rev2)

**Branch:** `feat/quanta-core-v4-rev2-r5`
**Status:** Research only. No code. Not pushed.
**Date:** 2026-05-12
**Supersedes:** `docs/quanta-core-v4/05-RESEARCH-PARALLEL_AGENTS.md` (the vLLM / LangGraph / sub-500-ms version).
**Why rev2:** the prior memo optimized the wrong objective. It chased a sub-500-ms decision panel built on resident vLLM, six 8B models firing in a `LangGraph` fan-out. The operator's trading philosophy — **2–3 trades per week, deliberate setup-driven entries, fail-closed on disagreement** — does not value sub-second latency. It values **gate quality**. This rev2 re-targets the panel at a **30-second deliberation budget**, **Ollama-only serving** (one model loaded at a time), and **70B-class quality on bull / bear / arbiter**. The blind-panel design, the hard veto rules, and the unanimous-convergence gate all survive — they are the operator's safety surface.

This is a design memo, not a contract. Numbers come from cited sources; recommended choices are flagged "REC" so the implementer can argue back.

---

## 1. Executive Recommendation

### 1.1 What changes vs the v4 doc 05

| Axis | v4 doc 05 (rev1) | rev2 (this doc) |
|---|---|---|
| Deliberation budget | p95 < **500 ms** | p95 < **30 s** (target ~28 s p50) |
| Serving plane | **vLLM** + LangGraph, 6 resident 8B models | **Ollama-only**, one model loaded at a time (per `project-drop-vllm`) |
| Orchestration | LangGraph `Send` + reducers | **Plain `asyncio.TaskGroup`** (no LangGraph, no LangChain) |
| Debate quality | 6 × 8B class concurrent | **70B-class** on bull / bear / arbiter; 8B only for fast pre-screen |
| Trigger | Every tick / every signal | **Only when a setup forms** (2–3 fires per week, per operator cadence) |
| Convergence | Weighted vote, majority sufficient | **Unanimous (or 5/6 with risk-veto rule)** required for ENTRY |
| Disagreement | Tie-break ladder → can still trade | **NO TRADE on disagreement.** Log divergence. Reflector reads it next night. |

### 1.2 Recommended stack (REC)

| Layer | Choice | Why |
|---|---|---|
| Orchestration | **`asyncio.TaskGroup`** (Python 3.11+) | Structured concurrency, ExceptionGroup, scope-bound cleanup, layered timeouts. [12] No LangGraph; the validator's P1-1 finding stands — LangGraph is a fat dep we don't need at 30-s budget. |
| Inference server | **Ollama** with `OLLAMA_KEEP_ALIVE=60m` warm | Single-load model on unified-memory DGX; sequential 70B inference is fine inside a 30-s budget. Per `project-drop-vllm` (2026-05-10), vLLM is OUT — it OOM'd at 95 GB / 121 GB physical + 7.3 GB swap on the same box. Ollama serves one heavy model at a time and handles the role rotation by re-warming. [5][6] |
| Models | `hermes3:8b` (fast pre-screen), `hermes3:70b` (bull / bear / arbiter), optional `hermes3:8b-trader` LoRA when promoted | Hermes 3 70B (~40 GB FP16, ~22 GB AWQ/NVFP4) fits the operator's measured memory ceiling (80 GB at idle, ~85–90 GB before swap thrash per doc 08 §1a). 8B runs warm at ~1.7 s per the production checkpoint. |
| Schema | **Pydantic** `BaseModel` validated post-response; Ollama JSON-mode via `format="json"` | Ollama lacks vLLM's token-level grammar-guided decoding, so we validate + one-retry on parse fail. Acceptable inside 30-s budget. |
| Concurrency primitive on heavy step | **Sequential bull → bear** (Ollama is single-load) OR **parallel bull + bear on two separate Ollama hosts if we ever split** | Today: sequential on the single DGX Ollama. Two 70B calls × ~10 s each = ~20 s for the adversarial pair. Within budget. |

### 1.3 Concurrency model — Blind Panel + Hard-Veto Arbiter + Unanimous Gate

**REC: single-round blind panel, no inter-agent visibility in round 1, deterministic arbiter, hard veto from Risk Engine + Microstructure, NO TRADE on disagreement. Re-poll with debate visibility only on tie-break — and tie-break does NOT mean "trade anyway." It means "we'll let the adversarial pair argue once more, and if they still disagree, FLAT."**

Rationale, cited:

- Du / Li / Tenenbaum debate (3 agents × 2 rounds, agents see each other) **improves accuracy on knowledge tasks** — this is the foundation we preserve. The "show only previous-turn solutions, hide full reasoning history" variant from ACL'25 Collective Improvement **adds +7.4% accuracy** by reducing biased reinforcement. [3][4]
- "Voting protocols improve performance by 13.2% in reasoning tasks; consensus protocols by 2.8% in knowledge tasks." Trading "should-we-enter-this-trade" is reasoning, and we use voting + a hard consensus gate. [4]
- "Making confidences visible provides limited benefit and can induce over-confidence cascades. Hiding confidences is generally preferred. Majority opinion in MAD can strongly suppress independent correction." [4]
- TradingAgents (Tauric Research) ships exactly this shape — parallel analysts → adversarial bull/bear → arbiter / risk — and is the closest published precedent. [7]

Translation: bull and bear reason **blind to each other** in round 1 using **70B quality**, then a typed arbiter (also 70B for synthesis quality) combines their JSON outputs. Risk Engine and Microstructure can **veto regardless** of bull/bear consensus. **Entry requires unanimity** (or 5/6 with the risk-veto rule). Anything less = FLAT. Round 2 (debate visibility) is reserved for the disagreement case (§7).

### 1.4 Why this is not "slow for the sake of slow"

The operator's setup cadence (2–3 trades/week ≈ ~12 trades/month) means **deliberation fires roughly once every 2 days**, not per tick. A 30-second debate per fire = ~15 minutes/month of LLM wall-time, well within the DGX's free cycles. The marginal cost of moving from 500 ms to 30 s is therefore negligible; the marginal benefit (70B-class judgment on entry decisions worth hundreds-to-thousands of dollars each) is large.

---

## 2. Why slow-deliberate beats fast-blind at 2–3 trades/week

A latency budget is justified by the **frequency and reversibility** of the decisions it supports. We have:

- **Frequency: ~3 decisions / week.** Setups are scarce by design — the regime engine, microstructure filters, and the universe (12 crypto + 15 stocks per `project_session_2026-05-11_t30_checkpoint`) jointly emit a few candidates per week.
- **Reversibility: low for entries, medium for exits.** A bad LONG burns 1–2 days of capital and pays slippage twice. The Wheel pilot has $629 of premium banked from 5 short puts — proof that the gate works when it gates.
- **Decision value per fire: high.** Position sizes are $1k–$5k; an avoided false-positive can save $50–$500 in slippage + adverse-selection loss; a missed true-positive costs ~the same in opportunity. The decision is therefore worth orders of magnitude more LLM-seconds than a high-frequency tick.

The Du / Tenenbaum result and the ACL'25 voting/consensus paper both find **accuracy gains from multi-agent debate are largest when models have time and structured turns** — the gains shrink as you compress the debate to a single round of fast 8B calls. [3][4] At 2–3 fires/week, we are paying for **the gain**, not the latency.

We also preserve the AgentAuditor finding that "auditing the reasoning tree outperforms majority vote and LLM-as-judge" by **always persisting the full bull + bear + arbiter outputs**, the round-1 / round-2 deltas, and the evidence_keys, so the reflector can audit later. [8]

A 500 ms budget makes this impossible: there is no time to record meaningful rationale, and the schema-forced fields collapse to single sentences. At 30 s, bull and bear can write ~3-paragraph arguments grounded in evidence_keys, which the reflector and the operator can read overnight.

---

## 3. Per-role sequence (30-second budget)

All times are wall-clock relative to t=0 = "setup formed, deliberation kicked off."

```
t=0.0 s  Setup forms (regime + microstructure + universe filter agree this is a candidate).
         StateAssembler caches features, bars, KB pointers, account_state. (~5–15 ms, Redis.)

t=0.0 s  ──► fast_regime_check  (hermes3:8b, warm, ~1.5–2.0 s)
         Reads:  regime.* + last 20 bars + active_strategy
         Writes: AgentVote { role: "regime", vote, conviction, evidence_keys[] }
         Purpose: cheap "is the macro context still favorable?" gate.

t=2.0 s  ──► microstructure_quicklook  (hermes3:8b, warm, ~1.5–2.0 s)
         Reads:  bid/ask spread, depth, recent prints, IV, options chain (stocks only)
         Writes: AgentVote { role: "micro", vote, conviction, evidence_keys[] }
         Purpose: cheap "is the book sane right now?" gate.

t=4.0 s  ──► PRE-SCREEN GATE
         If regime.vote == FLAT  OR  micro.vote == FLAT  with conviction ≥ 0.6:
             ABORT immediately. action = FLAT. method = "pre_screen_veto".
             Total latency ≈ 4 s. No 70B calls fired. Logged + reflector-visible.
         Otherwise: proceed to 70B deliberation.

t=4.0 s  ──► bull_70b   (hermes3:70b, ~10 s on Ollama, single-load)
         Reads:  bars, regime, KB-bull-context, evidence_keys cited by regime/micro
         Writes: AgentVote { role: "bull", vote, conviction, horizon_min, rationale, evidence_keys[] }
         Blind to: bear's output (round 1).
         Prompt frame: "Argue the strongest LONG case. If no LONG case exists, vote FLAT (do NOT manufacture one)."

t=14.0 s ──► bear_70b   (hermes3:70b, ~10 s on Ollama, single-load)
         Reads:  same state slice as bull, plus KB-bear-context
         Writes: AgentVote { role: "bear", vote, conviction, horizon_min, rationale, evidence_keys[] }
         Blind to: bull's output (round 1).
         Prompt frame: symmetric, SHORT bias. If no SHORT case exists, vote FLAT.

         NOTE on parallelism: if a future build splits Ollama across two
         hosts (one warm 70B per host), bull and bear can fire concurrently
         in an asyncio.TaskGroup, collapsing this 20 s into ~10 s. Today
         (single DGX Ollama), they sequentialize. The 30-s budget assumes
         sequential.

t=24.0 s ──► arbiter_70b   (hermes3:70b, ~4–5 s, hot KV-cache reused)
         Reads:  all four prior AgentVotes verbatim (regime, micro, bull, bear) PLUS
                  the shared state snapshot.
         Writes: ArbiterSummary { synthesized_action, synthesis_rationale,
                                  agreement_pattern, dissent_notes }
         Purpose: NOT to override the panel — to synthesize the rationale
         in a single audit-friendly paragraph and flag any logical gap
         (e.g., "bull cited earnings beat but regime is risk-off; this is
         an inconsistency").
         Arbiter does NOT vote; the deterministic aggregator (§4) decides.

t=29.0 s ──► risk_engine + microstructure final check
         Risk Engine: 50-ms GPU Monte-Carlo (per doc 03) — VAR / ES /
                       drawdown headroom against current account_state.
         Microstructure: re-read book; spread blown out? halt? circuit-broken?
         Both have HARD VETO authority regardless of panel agreement (§5).

t=30.0 s ──► Aggregator runs deterministic vote (§4); DecisionRecord persists;
         action is committed or aborted.

         Reflector runs OUT OF BAND on the resolved trade (T+horizon).
         Not on the hot path.
```

### 3.1 Why this fits in 30 s on single-load Ollama

| Stage | Budget (s) | Source / assumption |
|---|---|---|
| State assembly (Redis-cached) | 0.01 | Already cached per Streamkap budget. [11] |
| `hermes3:8b` regime check (warm) | 1.5–2.0 | Production-measured 1.7 s warm 1-sentence reply, per `project_session_2026-05-11_t30_checkpoint`. |
| `hermes3:8b` microstructure quicklook (warm) | 1.5–2.0 | Same model, same load. |
| Pre-screen abort path | 4.0 total | Saves ~25 s when veto fires. |
| Model swap 8b → 70b (Ollama unload + load) | 2–5 | One-time hit per debate. With `OLLAMA_KEEP_ALIVE=60m` and weekly volume of ~3 fires, model swaps are rare; we accept this hit. |
| `hermes3:70b` bull (cold-ish first call, then warm) | 8–12 | Hermes 3 70B at FP16 ≈ ~20 tok/s on Blackwell; ~150–250 output tokens. |
| `hermes3:70b` bear (warm, same load) | 8–12 | Same. |
| `hermes3:70b` arbiter (warm, same load, smaller output) | 3–5 | ~80–120 output tokens. |
| Risk MC + microstructure final | 0.05–0.5 | Doc 03 budget. [3 = doc-03 in this pack] |
| Aggregator (deterministic Python) | 0.001–0.005 | Pure CPU. |
| **p50 total (no pre-screen abort)** | **~26–30 s** | |
| **p95 total** | **~32–35 s** | Tail comes from model-swap penalty + long output. |

Budget conclusion: **30 s p50 is achievable. p95 may stretch to ~35 s.** The aggregator is configured with a soft 30-s deadline and a hard 45-s deadline; tasks that miss soft → ABSTAIN (NO TRADE), tasks that miss hard → kill + log + page.

### 3.2 Why we do not run bull + bear concurrently today

Ollama loads one model at a time on the DGX Spark. Loading two `hermes3:70b` instances concurrently would re-create the OOM the operator already survived (`project-drop-vllm`, 95 GB / 121 GB physical). Sequentialization is the safer choice and stays within the 30-s budget. If a future hardware change adds a second Ollama host (or vLLM is re-authorized per the operator's no-heavy-containers-without-OK rule), this becomes a one-line `asyncio.TaskGroup` change that halves the bull+bear stage to ~10 s. The design is forward-compatible without paying for that compatibility today.

---

## 4. Aggregator Algorithm — Weighted Vote + Hard Veto + 5-Step Tie-Break

### 4.1 Core formula (REC: weighted vote, NOT an LLM judge)

The arbiter LLM (§3, t=24 s) writes a synthesis paragraph — it does **not** vote. The vote is deterministic Python. AgentAuditor's finding: "Judges tend to favor majority positions even when wrong." [8] We use the LLM for rationale, not for deciding.

```
roles = {regime, micro, bull, bear}     # vote-carrying roles
direction_i ∈ {+1 (LONG), -1 (SHORT), 0 (FLAT/ABSTAIN)}
weight_i    ∈ tunable per-role weight, default 1.0 across the board
conviction_i ∈ [0, 1]

score   = Σ ( weight_i · direction_i · conviction_i )
n_valid = count of non-ABSTAIN votes among the 4 panel roles

# Hard vetoes (evaluated BEFORE score):
if risk_engine.veto == True:
    action = FLAT
    method = "veto_risk_engine"
    return

if microstructure.veto == True:                  # final-check veto, t=29 s
    action = FLAT
    method = "veto_microstructure"
    return

# Convergence test (REC: unanimous-or-5/6-with-risk-veto):
if n_valid < QUORUM (default 4 of 4):
    action  = FLAT
    method  = "veto_quorum"
    return

if NOT all (non-ABSTAIN) directions equal:
    action  = FLAT
    method  = "no_consensus"
    return  # see §5 — disagreement = NO TRADE

# All four roles agree on a direction. Size by aggregate conviction:
direction = +1 (LONG) or -1 (SHORT)   # unanimous
score_abs = |score|
size_hint = min(1.0, score_abs / SCORE_FULL_SIZE)   # SCORE_FULL_SIZE default 3.0
action    = LONG if direction > 0 else SHORT
method    = "unanimous"
consensus = "unanimous"
```

Default weights start at 1.0 across the board. They are re-fit **weekly** from Reflector outcome data (§7.3), aligned to the operator-locked weekly hit-rate gate (per `project-modelforge-decisions`). This is the **only** learned component in the loop — everything else is auditable rule code.

### 4.2 Why unanimous (or 5/6 with risk-veto)

The brief is explicit: **no 3/6 trading**. At 2–3 trades/week, type-II errors (missed opportunities) are cheap; type-I errors (false entries) are expensive. The unanimous gate biases the loop toward type-II at the gate level.

- "5/6 with risk-veto rule" applies when we extend the panel to include a separate Catalyst role (event-driven only, used for earnings / FOMC weeks). In the 4-role panel above, the gate is **unanimous of 4**. We do not run a 6-role panel by default; that grew out of the v4 doc's "more voices = more accuracy" intuition, which the ACL'25 result actually contradicts (confabulation consensus). [4][8]

### 4.3 Hard veto: Risk Engine + Microstructure

These two are not voters — they are **circuit breakers**. They override the panel.

- **Risk Engine veto** triggers when ANY of:
  - 4-week drawdown ≥ operator-configured threshold (default 8%);
  - position-sizing math returns size < $200 (notional too small to be worth the gate effort);
  - account state would violate the wheel pilot's CSP cash-secured ratio;
  - the 50-ms Monte-Carlo VAR/ES gate (doc 03) says expected loss > permitted budget.
- **Microstructure veto** triggers when ANY of:
  - bid/ask spread exceeds 3× rolling-median;
  - depth on the relevant side is < 0.5× notional we want to send;
  - book is one-sided (no opposing liquidity within 1%);
  - circuit-breaker / LULD halt active.

Documented evidence: ACL'25 paper notes "consensus protocols offer advantages by requiring the agreement of the majority of agents, providing built-in error detection" for high-stakes domains; we extend this with non-voting circuit-breaker roles. [4] AgentAuditor's principle of preserving the reasoning tree means even when a veto fires, we persist the full panel output so the reflector can spot pattern (e.g., "risk vetoed 5 of the last 7 unanimous-bull setups — is the drawdown threshold too tight?"). [8]

### 4.4 Tie-break ladder (executed in order — but each step prefers FLAT)

The brief is clear: **NO TRADE on disagreement**. The ladder below is therefore **not** a "find a way to trade" ladder — it is an "are we certain enough to trade?" ladder. Default at every step is FLAT.

1. **Risk hard veto** — if Risk Engine vetoed for any reason → FLAT, `method = "veto_risk_engine"`. No further evaluation.
2. **Microstructure hard veto** — same → FLAT, `method = "veto_microstructure"`.
3. **Quorum check** — < 4 non-ABSTAIN votes → FLAT, `method = "veto_quorum"`.
4. **Unanimity check** — any direction disagreement → FLAT, `method = "no_consensus"`. Log the divergence (see §5); reflector picks it up overnight.
5. **Convergence-but-low-conviction** — all agree but `|score| < THRESHOLD` (default 1.5 out of 4.0 max with default weights) → FLAT, `method = "low_conviction"`. We do not trade on lukewarm agreement.
6. **Re-poll with visibility (ONLY in the optional tie-break branch)** — fire a *second* round of bull + bear only (2 calls, ~20 s), now showing them the round-1 panel JSON including each other's outputs and the arbiter synthesis. Budget: +20 s wall-clock. Cap: **one** re-poll per decision. If round 2 yields unanimity AND conviction passes the threshold → trade. If not → FLAT, `method = "repoll_no_consensus"`. (REC: enable this branch behind a feature flag for the first 8 weeks of paper-mode; if reflector data shows round-2 unanimity is no better than round-1 unanimity, disable.)
7. **Final fallback** — FLAT, `method = "abstain_default_closed"`. Better to miss a trade than to coin-flip with real capital. (`feedback-no-manual-runs` and the operator's fail-closed bias both apply.)

### 4.5 Why deterministic aggregation, not LLM-as-judge

- Auditing `score = Σ w·d·c` is trivial; auditing why an LLM judge picked bull is not.
- LLM judges have a documented sycophancy bias toward majority positions even when the majority is wrong. [8]
- A deterministic aggregator runs in 1–5 ms and frees the latency budget for actual deliberation.
- The 70B arbiter still writes a synthesis paragraph (§3, t=24 s) — its output is the **rationale** persisted in the audit log, not the **decision**.

---

## 5. Disagreement Handling — NO TRADE, log it, reflector reads it overnight

The operator philosophy is unambiguous: **disagreement means we don't have an edge here right now.** This is the most important behavioral difference between rev1 and rev2.

### 5.1 What "disagreement" means

| Scenario | rev1 behavior | rev2 behavior |
|---|---|---|
| Bull = LONG, Bear = SHORT, others split | Weighted vote, possibly trade | **FLAT.** `method = "no_consensus"`. |
| All 4 agree direction but conviction varies | Weighted vote, trade with size_hint | If `|score| < threshold` → FLAT. Otherwise trade. |
| 3 agree, 1 ABSTAIN | Weighted vote (quorum 3 of 4) | Quorum default = 4 → **FLAT**. |
| Risk vetoes alone | Risk hard-veto, FLAT | **FLAT** (same). |
| Round-2 re-poll resolves to unanimity | Trade | Trade (feature-flagged; see §4.4 step 6). |
| Round-2 re-poll still disagrees | Tie-break ladder (LLM judge) | **FLAT.** No LLM judge override. |

### 5.2 What we log on disagreement (the audit trail)

Every disagreement produces a `DecisionRecord` row with `action = "FLAT"` and one of the `method` codes above. The row contains:

```python
class DecisionRecord(BaseModel):
    decision_id: UUID
    ts: datetime
    symbol: str
    state_snapshot_hash: str         # for replay
    action: Literal["LONG","SHORT","FLAT"]
    method: Literal[
        "unanimous", "veto_risk_engine", "veto_microstructure",
        "veto_quorum", "no_consensus", "low_conviction",
        "pre_screen_veto", "repoll_no_consensus", "abstain_default_closed",
    ]
    size_hint: float                 # 0.0 when action == FLAT
    weighted_score: float            # raw aggregator math (signed)
    consensus: Literal["unanimous","split","no_quorum"]
    panel: list[AgentVote]           # regime, micro, bull, bear (and round-2 if fired)
    arbiter_synthesis: str           # rationale paragraph from arbiter_70b
    repoll: Optional[RepollRecord]   # round-2 fields if it fired
    risk_engine_state: RiskState     # what the MC gate computed at t=29
    microstructure_state: MicroState # spread, depth, halt flags at t=29
    panel_latency_ms: int            # wall-clock from t=0 to commit
    reflector_id: Optional[UUID]     # filled async later
```

The full `panel` (all 4 votes + evidence_keys) is preserved on FLAT decisions — this is what the reflector reads. AgentAuditor: "keep the reasoning tree, not just the verdict." [8]

### 5.3 Reflector reads the divergence — runs nightly, out of band

Every night (post-23:55 ET, after US market close + crypto rollover):

1. Reflector pulls all `DecisionRecord` rows from the last 24 h.
2. For each `method != "unanimous"` row, it inspects:
   - which role(s) dissented;
   - what evidence_keys they cited;
   - what the actual outcome would have been (using the state snapshot hash to replay against next-day price action);
3. It writes a critique row keyed by `decision_id`, with a recommendation: re-weight role X, raise/lower threshold Y, flag this evidence_key family as low-signal.
4. The critique rows feed the **weekly** weight-refit + threshold-refit (operator-locked weekly hit-rate gate per `project-modelforge-decisions`). Continuous re-tuning is OFF by default.

The reflector is on its own asyncio task / cron job. It is **never** on the deliberation hot path.

### 5.4 The "all-agree-but-wrong" case

Four roles can confabulate together — especially regime and bull both reading the same KB context. Mitigations, layered (preserved from rev1, retuned for rev2):

1. **Heterogeneous prompts and tool-access per role.** Quant-style features for regime; book-state-only for micro; KB-bull-context for bull; KB-bear-context for bear. This forces them to reason from different inputs, breaking "shared blind spot." [4]
2. **Risk Engine hard veto** — always evaluated, even on unanimous panels.
3. **Microstructure final check** — always evaluated, even on unanimous panels.
4. **Reflector's "consensus-was-wrong" classifier** — trains offline from outcomes. When N unanimous decisions in a row are losers, raise the Risk weight automatically (the only learned knob per `project-modelforge-decisions` §3). AgentAuditor's "Anti-Consensus Preference Optimization" idea adapted to weight-fit instead of fine-tuning. [8]
5. **Circuit breaker** — 24-hour realized-vs-predicted Brier on unanimous decisions degraded > X → loop switches to all-FLAT and pages.

---

## 6. Failure modes — strong fail-closed bias

Each row below names the failure, what the rev2 panel does, and the resulting `method` code. **Every failure defaults to NO TRADE.** This is by design.

| Failure | Panel response | `method` code | Notes |
|---|---|---|---|
| `hermes3:8b` regime call times out (> 3 s) | ABORT pre-screen → FLAT | `pre_screen_veto` | Treat as if regime voted FLAT. Page only if 3+ in a row. |
| `hermes3:8b` micro call times out (> 3 s) | ABORT pre-screen → FLAT | `pre_screen_veto` | Same. |
| `hermes3:70b` bull call times out (> 15 s, hard) | bull = ABSTAIN → quorum check fails → FLAT | `veto_quorum` | Soft 12-s timeout: log + retry once on the SAME prompt. Hard 15-s: ABSTAIN. |
| `hermes3:70b` bear call times out | same | `veto_quorum` | Same. |
| Arbiter 70b times out (> 8 s) | Proceed without synthesis paragraph; aggregator still runs deterministic vote | (no method change) | Arbiter is rationale-only; absence does not block decision but reduces audit-trail quality. Log `arbiter_state = "timeout"`. |
| Risk Engine unreachable (50-ms MC gate fails) | **FLAT** | `veto_risk_engine` | The gate is mandatory. No risk evaluation = no trade. |
| Microstructure feed stale (last book update > 5 s old at t=29) | **FLAT** | `veto_microstructure` | Same. Stale book = unsafe to size. |
| Ollama daemon down | **FLAT** (entire panel aborts before t=0+8s) | `pre_screen_veto` | Page operator immediately. |
| State snapshot hash collision / Redis miss | **FLAT** | `pre_screen_veto` | Can't replay → can't audit → don't trade. |
| Schema validation fails on bull/bear/arbiter (after 1 retry) | ABSTAIN that role; quorum check usually fails → FLAT | `veto_quorum` | Log raw response for reflector to read. |
| 3+ consecutive panel-aborts in 1 h | Auto-FLAT all symbols + page operator | (system-wide circuit) | Suggests infrastructure problem, not market problem. |

Cross-reference: `feedback-no-heavy-containers-without-explicit-ok` (fail-closed bias is a documented operator preference); structured-concurrency principle of layered timeouts (per-tool / per-fan-out / per-turn / workflow) so one slow node never poisons the whole superstep. [12]

---

## 7. Memory between rounds — blind round 1, optional debate-visibility round 2

### 7.1 Round-1 contract (always fires when pre-screen passes)

- bull and bear receive the **same prompt skeleton**: `{symbol, regime_features, last_n_bars, kb_context, account_state, ts}`.
- bull's KB context slice is **disjoint** from bear's (bull gets earnings-positive / catalyst-positive KB; bear gets earnings-misses, lawsuit notes, regulatory-risk KB). This is the "heterogeneous information" mitigation against confabulation. [4]
- bull does **not** see bear's output. bear does **not** see bull's output. arbiter sees both.
- Each writes exactly one `AgentVote`. They cannot see the regime / micro / arbiter outputs in round 1.

This is the operator's blind-panel invariant, preserved verbatim from rev1.

### 7.2 Round-2 contract (only fires on tie-break, behind a feature flag)

- Triggered only when round-1 produced unanimity-of-direction but `|score| < THRESHOLD` (the "lukewarm agreement" case) AND the reflector-learned feature flag `enable_repoll_for_low_conviction = True`.
- bull and bear are now **shown** the round-1 panel JSON: each other's votes, the regime + micro votes, and the arbiter synthesis paragraph.
- They are prompted: "Round 1 was lukewarm-unanimous. Here is the panel. Argue specifically against the weakest point, OR raise your conviction with new evidence_keys. If you cannot, vote FLAT."
- Each writes a new `AgentVote` (call it `bull_r2`, `bear_r2`) with a new conviction.
- The aggregator re-runs on (regime, micro, bull_r2, bear_r2). Unanimous + above-threshold → trade. Anything else → FLAT.

This implements the ACL'25 "Collective Improvement" finding: showing only previous-turn solutions (not full reasoning chains) and capping at one extra round preserves the 7.4% accuracy gain without inducing over-confidence cascades. [4]

### 7.3 Round-2 is NOT a way to coerce a trade

It is a way to confirm whether a lukewarm signal is actually strong-but-poorly-articulated. If round 2 says no, the answer is no. The operator's "NO TRADE on disagreement" rule is preserved.

The feature flag exists because the reflector needs ~8 weeks of paper data to validate whether round-2 unanimity actually produces better outcomes than round-1 unanimity. If it doesn't, we disable round-2 and save ~20 s per low-conviction setup.

### 7.4 What the reflector sees on round-2 fires

The `DecisionRecord` includes a `repoll: Optional[RepollRecord]` field with:
- round-1 panel (bull, bear, regime, micro, arbiter)
- round-2 panel (bull_r2, bear_r2)
- which arguments moved the needle (delta in conviction, new evidence_keys cited)
- final action and method

The reflector grades: did round 2 add signal, or did it just re-state round 1? After 8 weeks of paper, this becomes the metric that retires the feature flag (one way or the other).

---

## 8. Build cost — simpler than the vLLM path

Assumes one engineer (operator) plus Claude pair. Times are wall-clock hours of focused work.

| Chunk | What | Hours |
|---|---|---|
| 1. Ollama warm-up + Modelfile per role | `hermes3:8b` for regime/micro; `hermes3:70b` for bull/bear/arbiter; `OLLAMA_KEEP_ALIVE=60m`; verify warm latency budget | 2 |
| 2. Pydantic schemas — `AgentVote`, `DecisionRecord`, `RepollRecord`, `RiskState`, `MicroState`, `ArbiterSynthesis` | 1.5 |
| 3. `asyncio.TaskGroup` deliberation orchestrator | sequence the 5 calls; per-call timeouts (3 s for 8b, 12/15 s for 70b, 8 s for arbiter); soft 30-s deadline / hard 45-s deadline | 3 |
| 4. Deterministic aggregator | weighted-vote math + 7-step tie-break ladder + tests for every `method` code | 3 |
| 5. Persistence + audit log | DB schema for `DecisionRecord` + chat_json mirror (reuse from existing trade log path) | 2 |
| 6. Risk-engine + microstructure veto wiring | hook into doc 03's MC gate; hook into existing microstructure feed | 2 |
| 7. Reflector job (cron) | nightly reads outcomes, writes critique, weekly weight-refit script | 4 |
| 8. Dashboard "today's deliberations" card | row per decision, expandable to show 4 votes + arbiter synthesis + method (production-grade dYdX/Geist aesthetic per operator preferences) | 4 |
| 9. Failure-mode tests | timeout sim, all-abstain sim, no-consensus sim, risk-veto sim, ollama-down sim | 3 |
| 10. Shadow-mode rollout | run new loop alongside current panel; log only; compare for 1 week | 2 setup + 1 week wall-clock |
| **Total engineering** | | **~26.5 hours (~2–3 focused dev-days)** |

Plus ~1 week of shadow comparison before cutover per the operator's mandatory paper-mode invariant (doc 09 §2). **Total calendar: 2 weeks** from green-light to live cutover. Lower-bound than rev1 (~31 hours then) because:

- No vLLM source-build (-4–6 hours).
- No LangGraph wiring (-4 hours).
- No `guided_json` integration (-2 hours; replaced by validate-and-retry-once).
- Net add: round-2 re-poll branch (+2 hours), reflector cadence logic (+1 hour).

**Risks that could double the estimate (down from rev1's list):**

- Ollama 70b warm-up + first-call latency variance on the DGX Spark — needs measurement before locking the 30-s budget. If it's 15 s rather than 10 s, bump the budget to 45 s.
- Per-role prompt engineering on bull / bear / arbiter is the soul of the system; these will iterate. Budget 1 extra day for prompt iteration.
- Reflector's nightly job depends on the trade-outcome resolver landing on schedule (doc 09 §F1 paper-mode-gated rollout).

---

## 9. Sources

Surviving from rev1:

1. ~~LangGraph benchmarks~~ — DROPPED, see validator P1-1.
2. ~~OpenAI Agents / CrewAI / AutoGen comparison~~ — kept reference but not load-bearing.
3. [Improving Factuality and Reasoning in LLMs through Multiagent Debate](https://arxiv.org/abs/2305.14325) — Du, Li, Torralba, Tenenbaum, Mordatch, ICML 2024. **Preserved.** Foundation for our bull/bear blind round 1 + optional visibility round 2.
4. [Voting or Consensus? Decision-Making in Multi-Agent Debate](https://arxiv.org/html/2502.19130v4) — ACL Findings 2025. **Preserved.** Voting +13.2% on reasoning; "Collective Improvement" +7.4% from hiding history; majority-opinion-suppresses-correction warning that informs the unanimous-not-majority gate.
5. ~~vLLM vs Ollama Red Hat benchmark~~ — DROPPED for the recommendation, kept as a footnote that Ollama is slower per request but we're not throughput-bound at 3 trades/week. Cited as [5] for context.
6. ~~Ollama parallel requests blog~~ — DROPPED; we serve sequentially, not in parallel.
7. [TradingAgents: Multi-Agents LLM Financial Trading Framework](https://arxiv.org/abs/2412.20138) + [DeepWiki architecture](https://deepwiki.com/TauricResearch/TradingAgents) — Tauric Research. **Preserved.** Closest published precedent for the bull/bear-debate-moderated-by-arbiter shape.
8. [Auditing Multi-Agent LLM Reasoning Trees Outperforms Majority Vote and LLM-as-Judge](https://arxiv.org/abs/2602.09341) — AgentAuditor. **Preserved.** "Keep the reasoning tree, not just the verdict"; LLM-judge sycophancy; Anti-Consensus Preference Optimization adapted for weight-fit.
9. ~~LangGraph fan-out / Send API~~ — DROPPED.
10. [Pydantic AI — Output and Structured Outputs](https://ai.pydantic.dev/output/) — Pydantic. **Preserved**, but with reduced reliance: we validate post-response from Ollama JSON-mode + one retry.
11. [Agent Decision Latency Budget](https://streamkap.com/resources-and-guides/agent-decision-latency-budget) — Streamkap. **Preserved** for the Redis-cached state-assembly figure; the < 500 ms whole-loop figure no longer applies to our 30-s budget.
12. [Structured Concurrency for AI Pipelines: Why asyncio.gather() Isn't Enough](https://tianpan.co/blog/2026-04-09-structured-concurrency-ai-pipelines-parallel-tool-calls) — Tian Pan. **Preserved.** Justifies `asyncio.TaskGroup` over `gather` and the layered-timeout pattern.
13. [Why Do Multi-Agent LLM Systems Fail?](https://arxiv.org/pdf/2503.13657) — Cemri, Pan, Yang et al. **Preserved.** Failure taxonomy informs §6.
14. [Constitutional AI: Harmlessness from AI Feedback](https://arxiv.org/abs/2212.08073) — Anthropic. **Preserved.** Principle-based self-critique applicable to the Risk role's hard-veto constitution.

Operator-memory anchors (not external citations, but the canonical "operator law"):

- `~/.claude/projects/.../memory/project_drop_vllm.md` — Ollama-only mandate.
- `~/.claude/projects/.../memory/project_modelforge_decisions.md` — qwen3:30b base lock, $0 budget, weekly hit-rate gate, HF Hub adapters-only-private.
- `~/.claude/projects/.../memory/feedback_no_heavy_containers_without_explicit_ok.md` — fail-closed bias, explicit per-action authorization.
- `~/.claude/projects/.../memory/project_session_2026-05-11_eod.md` + `..._t30_checkpoint.md` — current paper-trading state, $629 banked wheel premium, hermes3:8b at 1.7 s warm.
- `~/.claude/projects/.../memory/feedback_dashboard_design.md` — dYdX/Geist aesthetic for the deliberations card.
- `~/.claude/projects/.../memory/feedback_session_lessons.md` — UI > CLI, config > hardcoded, verify-before-claim.

Companion docs in this pack:

- `docs/quanta-core-v4-rev2/01-RESEARCH-MULTI_MODEL_RESIDENCY.md` — Ollama-only serving plane and memory budget; this doc inherits its serving conclusions.
- `docs/quanta-core-v4/03-RESEARCH-RISK_MONTE_CARLO.md` — the 50-ms MC veto gate referenced in §3 / §4 / §6.
- `docs/quanta-core-v4/07-VALIDATOR_REPORT.md` — the cross-check that surfaced rev1's vLLM / LangGraph / sub-500-ms problems; this rev2 addresses Change 2 (Ollama-only) and Change 4 (drop LangGraph) directly.

---

**End of memo. No code shipped. Branch `feat/quanta-core-v4-rev2-r5`. Not pushed. Next step: operator reviews and either green-lights an implementation plan or sends edits back.**
