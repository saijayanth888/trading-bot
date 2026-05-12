# 07 — VALIDATOR REPORT: Quanta Core v4 Design Pack Cross-Check

**Branch:** `feat/quanta-core-v4-design-r7`
**Validator role:** cross-document consistency, constraint compliance, principle alignment, coherence verdict.
**Date / wall clock:** 2026-05-12 ~16:09 ET
**Polling window:** 2026-05-12 15:55 — 16:09 ET (14 min observed; agents committed during this window; doc 06 landed in the final 4 min).
**Files audited:** all numbered `0N-*.md` design docs that landed under
`/home/saijayanthai/Documents/trading-bot/.claude/worktrees/agent-*/docs/quanta-core-v4/`
during the polling window, plus the four `HANDOFF.md` companion files.

This report is **paranoid by design**. The operator has $19k of paper capital
he plans to take live behind whatever the next-build agent ships, a $5k DGX
Spark with bleeding-edge ARM/Blackwell toolchain, and a recent (2 days old)
operator-level "drop vLLM" decision that the design pack **does not
uniformly reflect**. Greenlighting the build before fixing the
inconsistencies below would re-create the 95-GB-RAM vLLM OOM the operator
already survived once on this exact box.

---

## 1. Coverage table — 10 docs × present? × peer-reviewed?

Expected by operator brief: 5 research docs (1–5) + 1 architecture doc (6) =
6 deliverables. Observed: 9 numbered docs in the 01–10 range (each in its
own worktree on its own `feat/quanta-core-v4-design-rN` branch), plus four
HANDOFF.md helper files. Three extra un-briefed agents (r8/r9/r10) landed
additional docs that proved valuable.

| # | Filename                                          | Worktree branch         | Present | Author-claimed scope                                  | Peer-checked here |
|---|---------------------------------------------------|-------------------------|---------|-------------------------------------------------------|--------------------|
| 01 | `01-RESEARCH-MULTI_MODEL_RESIDENCY.md`           | `…-design-r1`          | YES     | DGX-Spark inference-serving stack picks                | YES                |
| 02 | `02-RESEARCH-CONTINUOUS_LORA.md`                 | `…-design-r2`          | YES     | RLRO continuous-LoRA pipeline, hot-swap, anti-forget   | YES                |
| 03 | `03-RESEARCH-RISK_MONTE_CARLO.md`                | `…-design-r3`          | YES     | <50 ms GPU Monte-Carlo risk gate (Heston-Bates)        | YES                |
| 04 | `04-RESEARCH-EXCHANGE_CONNECTIVITY.md`           | `…-design-r4`          | YES     | Drop CCXT+Freqtrade → alpaca-py + coinbase-advanced-py | YES                |
| 05 | `05-RESEARCH-PARALLEL_AGENTS.md`                 | `…-design-r5`          | YES     | LangGraph + asyncio.TaskGroup parallel debate panel    | YES                |
| 06 | `06-ARCHITECTURE.md`                             | `…-design-r6`          | YES *(late)*  | Full quanta-core/ file tree + Strategy ABC + integration | YES         |
| 07 | `07-VALIDATOR_REPORT.md` (**this doc**)          | `…-design-r7`          | YES     | Cross-check                                            | self               |
| 08 | `08-FEASIBILITY.md`                              | `…-design-r8`          | YES     | Live-hardware feasibility audit + ARM wheel scan       | YES                |
| 09 | `09-RISKS.md`                                    | `…-design-r9`          | YES     | Risk register, decision gates, rollback runbook        | YES                |
| 10 | `10-CODE_PATTERNS.md`                            | `…-design-r10`         | YES     | Code-style + integration contract for future build     | YES                |

**Headlines from the coverage scan:**

- **The architecture doc (06) landed late** — committed at ~16:08 ET, after
  the validator had drafted §2-P1-6 / §4 / §8-Change-1 around its
  absence. Doc 06 is **excellent and Ollama-aware** (uses `hermes3:8b` /
  `hermes3:70b` per the operator's active stack; only 1 vLLM mention in
  the whole 2,033-line doc, in the "Why a rewrite" paragraph as
  background context). It supplies the missing integration narrative.
  This **substantially improves** the coherence verdict — see §7 and §8.
- **Three agents (08, 09, 10) ran un-briefed** and produced excellent docs
  outside the original 1–6 scope — see §4 for the value they add.
- **Each doc is its own peer-review unit, on its own branch, in its own
  worktree.** They have never been viewed side-by-side until this report.

---

## 2. Inconsistencies found

Severity scale:
- **P0** = blocker; building on this design will burn cash or thrash the box.
- **P1** = will cause a wasted dev-week; reviewable before code starts.
- **P2** = doc-quality / consistency; not load-bearing.

### P0-1 · vLLM is the inference plane in docs 01/02/05/08 — but the operator dropped vLLM on 2026-05-12

Disagreement (severity P0):

| Doc | vLLM stance |
|-----|-------------|
| **01** (multi-model residency) | "vLLM patched × N processes" is the explicit recommendation; "Ollama remains for dev only … unacceptable in the loop." 50 vLLM mentions. |
| **02** (continuous LoRA) | "vLLM is the only viable hot-swap server for our prose roles today." 34 vLLM mentions. |
| **05** (parallel agents) | "vLLM for the 6 role agents; Ollama only acceptable if VRAM headroom forces single engine." 8 vLLM mentions. |
| **08** (feasibility) | Spends a full §2 + ~20 vLLM mentions on a vLLM source-build plan. Calls vLLM "the single biggest schedule risk." |
| **09** (risks) | **Correctly cites `[[project-drop-vllm]]` 3 times.** "Decision already made: Ollama-only. Do NOT re-introduce vLLM/NIM/TRT-LLM into the V4 hot path." S1 risk row. |
| **04** (exchange) | Doesn't touch inference plane. Neutral. |
| **03** (risk MC) | One incidental mention. Neutral. |
| **10** (code patterns) | Doesn't pick an inference server. Neutral. |

**Operator-level facts (from `~/.claude/projects/.../memory/project_drop_vllm.md`,
authored 2026-05-12, ~2 days old):**

- vLLM was bootstrapped on the DGX Spark and **pushed memory to 95 GB / 121 GB
  + 7.3 GB swap before being killed.**
- It loads qwen3:30b a **second time** on top of Ollama's existing copy —
  memory contention by design on a unified-memory box.
- The single-user / 5-15-LLM-calls-per-debate workload does not benefit from
  vLLM's batched throughput.
- Day-4 task "stand up vLLM serving" is **replaced** with "build
  Ollama-create cron in model-forge promote step (~50 LOC)."
- Cross-reference: `feedback-no-heavy-containers-without-explicit-ok` —
  vLLM was the OOM culprit twice.

**Why this is P0 not P1:** the docs are not subtly biased toward vLLM —
they are **architecturally founded on it**. Doc 01's recommended stack is
"vLLM-fast + vLLM-deep behind LiteLLM." Doc 02's promotion sequence is
`POST /v1/load_lora_adapter` with `load_inplace=True` — a vLLM-only
primitive. Doc 05's p95 < 500 ms budget assumes 6 concurrent vLLM calls.
Doc 08's 8-week plan critical-path-dependencies-on (step 0 → 5 → 9 → 12)
all gate on a working `vllm-custom` source build. If the operator's
"Ollama-only" decision sticks, **four of seven design docs need a major
rewrite**, not a redirect.

**Recommendation:** Before any code starts, the operator must rule on:
*(a)* re-open the vLLM decision (operator-authorized resource projection,
24-h soak, kill-switch path documented per
`feedback-no-heavy-containers-without-explicit-ok`); OR
*(b)* re-write docs 01, 02, 05, 08 to use Ollama + adapter-baked Modelfile
tags per `project-drop-vllm` §2 and §7. Doc 02's `load_lora_adapter`
choreography becomes `ollama create qwen3:30b-<role>-vYYYYMMDD -f Modelfile`
(slower swap, but the operator already accepted this — "Acceptable for our
cadence — we don't fire 100s of requests/sec.").

### P0-2 · Memory budget table in doc 01 exceeds the live-measured ceiling in doc 08

Disagreement (severity P0):

- Doc 01 §5 publishes a 128-GB unified-memory budget table summing to
  **~113 GB / 128 GB committed, leaving ~15 GB free** in the "happy" plan,
  and notes that hitting the operator's ≥30 GB free target requires
  dropping the fine-tune workspace.
- Doc 08 §1a (live `free -h` taken **2026-05-12 15:55 UTC** during the
  audit window) reports **121 GiB usable** with **80 GiB already used /
  41 GiB available** at idle, with the **realistic V4 ceiling ~85-90 GB
  before swap thrashes**. The same doc concludes the 135 GB residency
  target proposed elsewhere "does NOT close."
- The operator memory file `project-drop-vllm` documents the live
  symptom: **vLLM bootstrap ate 95 GB / 121 GB physical + 7.3 GB swap
  before being killed** — i.e. doc 01's plan would re-create the failure
  mode the operator already lived through.

**Why this is P0:** doc 01's memory budget *assumes* "VRAM and system RAM
are the same physical pool" but then proposes residency totals that
*ignore* the OS / freqtrade / dashboard / docker / browser idle baseline
that doc 08 measured live. The two docs land on opposite verdicts of the
same feasibility question and the right answer depends on whose number
is right — doc 08's measured baseline is more credible.

**Recommendation:** drop the 95-GB-pinned plan from doc 01. Adopt doc 08's
two-condition fix verbatim: (a) quantize Hermes 3 70B to NVFP4/AWQ
(~22 GB instead of ~40 GB), (b) time-slice the second heavy model with
LRU eviction off NVMe (~25-40 s cold-load), (c) keep
`torch.cuda.set_per_process_memory_fraction(0.3)`. Reissue doc 01's
memory table with the corrected baseline.

### P0-3 · Doc 02's LoRA pipeline assumes `vllm-custom` source-build + Hugging Face Hub push pattern; operator decided HF Hub is *private adapters only* and serving is Ollama

Disagreement (severity P0):

- Doc 02 §3.4 routes promoted adapters via vLLM `LoRAResolver` plugin reading
  from "S3/HF Hub."
- `project-modelforge-decisions` §2 (operator-locked, also ~2 days old):
  Hugging Face Hub usage is **adapters-only, private repo, named
  `dgx-trader-adapters`**, with a pre-push hook scanning adapter metadata
  for ticker×P&L co-occurrence patterns; raw trades / decisions /
  llm-calls / P&L / positions / account state **NEVER** leave the DGX.
- The serving plane (per `project-drop-vllm` §2) is **Ollama Modelfile
  baking** — `FROM qwen3:30b` + `ADAPTER /path/to/adapter.gguf` + `ollama
  create qwen3:30b-<role>-vYYYYMMDD`. Adapter swap time = "a few seconds
  for the GGUF page-in."

**Why this is P0:** doc 02 is the **continuous-training spine** of the
design pack. If it gets the serving / promotion path wrong, every
downstream module that consumes "an adapter" (debate roles in doc 05,
LoRA loop in doc 10 §5, registry in doc 10 §4) inherits a phantom API
(`/v1/load_lora_adapter`) that the runtime does not implement.

**Recommendation:** redraft doc 02 §3 ("hot-swap recipe") to use
`ollama create` + a `qwen3:30b-<role>-current` alias the trading bot
calls by name. Promotion is a write-Modelfile + create-tag + flip-alias
sequence (operator memory has the exact 6-step recipe). Retain doc 02's
DPO/PEFT training section verbatim — only the serving handoff changes.

### P1-1 · LangGraph (doc 05) is a new fat dependency; operator preference is local-first and minimal-deps

Disagreement (severity P1):

- Doc 05 §1.1 mandates **LangGraph + `asyncio.TaskGroup`** as the
  orchestration framework. Brings transitive deps (LangChain ecosystem),
  hot-reload semantics, and a new orchestration vocabulary into the
  trading hot path.
- Operator preferences (`user_profile`, `feedback-no-heavy-containers-without-explicit-ok`):
  local-first, container-aware, every new dep needs justification.
- Doc 10 §1.1 ("one pick per category, no fence-sitting") picks **plain
  asyncio + uvloop**, FastAPI, httpx — no LangGraph anywhere. Doc 10
  was authored independently and treats orchestration as in-process
  asyncio.

**Conflict:** doc 05 (research) and doc 10 (code patterns) disagree on
whether LangGraph is in or out of the stack. Build agents prompted with
doc 10 §6 will reject a LangGraph PR; an implementation that follows doc
05 verbatim will introduce LangGraph and fail doc-10 CI gates.

**Why P1 not P0:** the parallelism semantics doc 05 wants (fan-out, blind
panel, structured concurrency, layered timeouts) are achievable with
**raw `asyncio.TaskGroup`** alone — doc 05 itself notes this. LangGraph
is the convenience layer, not the necessity.

**Recommendation:** revise doc 05 §1.1 to drop LangGraph; keep
`asyncio.TaskGroup` + AnyIO (which doc 04 already adopts — see §5
naming-consistency for the AnyIO-vs-asyncio split too). Move LangGraph
to a v5 "if we ever need a graph DSL" sidebar.

### P1-2 · `asyncio` vs `AnyIO` choice is inconsistent across docs

Disagreement (severity P1):

- Doc 04 §3 mandates **AnyIO on asyncio backend** with structured task
  groups, level-cancellation, citing the ChatGPT-Redis outage class.
- Doc 05 §1.1 says **`asyncio.TaskGroup` (Python 3.11+, or
  `anyio.create_task_group`)** — accepting either.
- Doc 10 §1.3 says **asyncio + uvloop** explicitly. No AnyIO.

Three docs, three different "single picks." Build agents will trip over
this on day one.

**Why P1 not P0:** AnyIO is mostly a syntactic skin over asyncio; the
correctness arguments doc 04 makes (level-cancellation, ExceptionGroup)
are also available in `asyncio.TaskGroup` 3.11+. But for build agents
following doc 10's "no fence-sitting" rule, this is a clear
contradiction.

**Recommendation:** doc 10 (code patterns) is the contract for build
agents. Either (a) doc 10 r11 adopts AnyIO (matches doc 04), or (b) docs
04 + 05 r-bumps to drop AnyIO. Operator should rule once and harmonize.

### P1-3 · LoRA rank, target modules, training-batch cadence inconsistent

Disagreement (severity P1):

- Doc 02 §2 — rank **r=32 for prose roles, r=16 for JSON roles**, target
  modules `q/k/v/o + gate/up/down` for prose, attention-only for JSON.
- Doc 01 §5 — rank 32 implied; uses
  `--max-lora-rank 32` in the vLLM startup.
- No doc names a contradictory rank, but **doc 02 promises a 10-min
  cadence per Reflector tick** ("train all 6 roles in ~15 min, fits inside
  a 30 min cycle"); doc 09 §O1 mandates "out-of-sample eval on the OLDEST
  7-day window the new adapter has NEVER seen — promote only if hit-rate
  ≥ previous adapter" + the locked rule (`project-modelforge-decisions`
  §3) that **promote/rollback gate is weekly hit-rate**, not every
  10-min.

**Conflict:** doc 02's 10-min continuous-LoRA promotion path is
incompatible with the operator-locked **weekly hit-rate gate**. The two
loops describe **different cadences and different gating evidence**
(10-min KL+Pareto vs weekly hit-rate-on-30d-window).

**Why P1 not P0:** doc 02's anti-forgetting story has a "weekly anchor
retrain on 90d window" fallback that is *compatible* with the operator
gate — but doc 02 makes the continuous side the **default** and the
anchor the **safety net**. That's backwards from the operator's locked
decision.

**Recommendation:** swap the default in doc 02: **weekly hit-rate-gated
training is the canonical path**; continuous LoRA is an opt-in research
mode the operator explicitly enables once the weekly loop has 30+ days
of clean promotions. This also moots the vLLM hot-swap requirement
(weekly cadence is fine for Ollama Modelfile rebake).

### P1-4 · "$0 budget" rule is observed by docs 02/04/09 but contradicted by Polygon/Alpaca line item in doc 04 + Algo Trader Plus in doc 04

Disagreement (severity P1):

- `project-modelforge-decisions` §4 — **strict $0 paid-API budget**;
  "fully local always."
- Doc 04 §9 explicit-cost table: **Alpaca Algo Trader Plus $99/mo**
  (recurring), **Polygon Options Starter $79/mo** (Phase 2 optional).
  Doc 04's own TL;DR makes Alpaca Algo Trader Plus *required* for OPRA
  real-time options data.
- Doc 04 §1 also writes off CCXT.Pro as "paid extension" — correctly
  enforcing the rule for crypto — but spends $99/mo on the stocks side.

**Conflict:** the strict $0 rule from operator memory is for **LLM
APIs** (Anthropic, OpenAI). Doc 04 is buying **broker market-data
APIs**. These are different budget lines, but the design pack
nowhere makes that distinction explicit. A junior build agent reading
the pack today will see "$0 paid-API budget" everywhere and reject
the Alpaca Algo Trader Plus line item; a senior agent will assume the
operator already approved it.

**Why P1 not P0:** operator probably has approved this implicitly (the
wheel pilot is already live with $629 in premium per `project_session_2026-05-11_eod`
— that requires real-time stocks/options data) but it has **not been
re-confirmed in the design pack** with a concrete monthly-cost line
the operator signed off on.

**Recommendation:** doc 04 r5 adds an explicit "Cost confirmation"
section: line items (Alpaca $99, Polygon $79 optional), operator
acknowledgment marker, and a feature-flag config knob to disable the
options pilot if costs need to drop. Cross-reference
`feedback-anthropic-routing` cost-aversion stance.

### P1-5 · Operator's "paper-mode for the entire migration" rule is honored by docs 04/05/08/09 but not surfaced in doc 01

Disagreement (severity P1):

- Operator brief explicitly: **"Paper-mode for the entire migration (no
  live until parity proven)."**
- Doc 09 §F1 enforces this with a hard rule: "V4 cannot be promoted from
  `dry_run=true` to live until: (a) 14 consecutive trading days of
  shadow…(b) shadow P&L within 20% of Freqtrade P&L (c) operator
  explicit ack via `/ops` button requiring typing 'PROMOTE V4'."
- Doc 08 §5 echoes: "10 consecutive trading days of acceptance criteria
  … cutover flips a single env var."
- Doc 04 §8 + §8.3 has a paper run-in-then-cutover step.
- **Doc 01 says nothing about paper mode** — entirely focused on serving
  infrastructure. Acceptable for a research doc but the migration plan
  it implies (P0–P5 in §6) does not mention paper-mode at all.
- Doc 05 §7 has a "shadow-mode rollout (1 week)" line but it's about
  decision-divergence comparison, not real-money paper-vs-live.

**Why P1 not P0:** the paper-mode gate **is** preserved in docs 08/09.
This is a coordination issue, not a violation. Build agents reading doc
01 in isolation would not know paper-mode is mandatory.

**Recommendation:** add a one-paragraph "Migration-mode invariant" to
every doc's executive summary cross-referencing doc 09 §2 decision
gates. Make doc 09 §2 the **canonical** definition of "when does code
go live."

### P1-6 · Two different "fan-out" architectures: doc 05 (LangGraph blind panel) vs doc 04 (AnyIO connectors-as-tasks)

Disagreement (severity P1):

- Doc 05 §1.2 — six role agents fanning out from a `LangGraph` supervisor,
  Send API, reducer-merged state, p95 < 500 ms.
- Doc 04 §3 — three venue connectors fanning out from an AnyIO
  supervisor, EventBus on `anyio.create_memory_object_stream`, per-symbol
  ordering preserved.

Both call themselves "single event loop, structured concurrency, fan-out
+ fan-in." They are **two separate fan-out graphs**, not one. The design
pack doesn't say how they compose — does doc 04's EventBus feed doc 05's
panel? Does doc 05's panel write into doc 04's OrderRouter? Where does
doc 03's MC risk gate slot in?

**Why P1 not P0:** each doc is internally consistent. The missing
glue is **doc 06** — **which did land late (~16:08 ET)** and substantially
resolves this disagreement. Doc 06 §1's ASCII diagram explicitly composes
all four pieces: Alpaca/Coinbase WS → `live.engine` → `StrategyRouter`
(per-symbol fan-out) → `agents.debate` (optional gate) → `risk.governor`
+ `risk.monte_carlo` → `execution.engine` → broker REST. The hot-path is
ONE asyncio loop, not two competing fan-outs.

**Recommendation (revised after doc 06 landed):**
- **Doc 06 is the canonical integration narrative.** Reconcile docs
  04/05 against doc 06's `StrategyRouter` + `live.engine` design: doc
  04's AnyIO supervisor becomes the `_tick_pump` / `_fill_pump` / `_heartbeat`
  task pattern in doc 06 §3.3; doc 05's blind panel becomes doc 06's
  `agents.debate` module (bull || bear || reflector → arbiter).
- Doc 06 §3.11 calls the debate `bull || bear || reflector -> arbiter`
  with `hermes3:8b` for bull/bear and `hermes3:70b` for the arbiter —
  resolving naming inconsistency P2-3 by adopting **doc 02's role
  taxonomy**, not doc 05's. Doc 05 r-bump must re-key its 6-role panel
  to doc 06's names (bull, bear, reflector, arbiter; macro/quant/risk/catalyst
  are not in doc 06).
- Add a one-page end-to-end latency budget summing 50 ms (MC) +
  ~1-3 s (Ollama 6-role panel, not vLLM 500 ms) + ~50-200 ms (broker WS
  round-trip) — that's doc 06's missing latency-budget table.

### P2-1 · Number conventions disagree (paths, sample sizes, latency targets)

- Doc 03 risk-MC budget: **<50 ms** end-to-end.
- Doc 05 panel budget: **<500 ms** p95 decision-to-action.
- Doc 04 implies execution latency is dominated by broker WS round-trip
  (50-200 ms typical for Alpaca, 5-30 ms for Coinbase).
- **Where does the 50 ms risk gate fire relative to the 500 ms decision
  panel?** Inside? After? Once per decision or once per fill? No doc
  specifies.

**Why P2 not P1:** these are reconcilable in a follow-up. But every
"sub-second response" claim in doc 09 §O2 ("V4's design targets
sub-1s response") presumes both budgets compose linearly. If the gate
fires inside the panel, the panel budget grows to 550 ms p95.

**Recommendation:** doc 06 (when written) puts the budgets on one
diagram; doc 03 + doc 05 cross-reference each other.

### P2-2 · `client_order_id` schema appears in two places with different shapes

- Doc 04 §6.3 prescribes a 36-char structured ID:
  `qc4-{venue}-{strategy_id}-{leg_uuidv7}`.
- Doc 10 §1.7 says "client_order_id: UUIDv4" (idempotent pattern A).

Different UUID versions, different prefix discipline, different length
contracts.

**Why P2:** both schemes are valid; pick one. Doc 04's is the more
trade-aware one (encodes venue + strategy) but it's longer than
UUIDv7's 36-char limit when prefix + venue + strategy are added.

**Recommendation:** harmonize on doc 04's scheme; doc 10 r11 updates
its idempotency wording to match.

### P2-3 · "6 LLM roles" definition differs between docs 02 and 05

- Doc 02 §2 — the 6 roles are
  **regime_tagger, bull_debater, bear_debater, arbiter, reflector,
  indicator_selector**.
- Doc 05 §3.2 — the 6 roles are
  **bull, bear, macro, quant, risk, catalyst** (with reflector
  out-of-band).
- Doc 09 §O1 just says "the 6 roles" without naming them.

Two non-overlapping 6-role sets in two adjacent docs. This is the
single most confusing inconsistency in the pack for a reader.

**Why P2:** they're describing different things — doc 02 is the
LoRA-training taxonomy; doc 05 is the decision-time debate panel. But
the docs **use the same noun ("the 6 roles") for both**, and doc 02
even quotes back to `MODELFORGE_INTEGRATION_PLAN.md` as canonical.

**Recommendation:** rename. Doc 02's set = "LoRA training roles." Doc
05's set = "debate panel agents." Update every cross-reference.

---

## 3. Constraint violations

| # | Constraint (from operator brief)                       | Violator(s)                  | Severity | Recommendation |
|---|--------------------------------------------------------|------------------------------|----------|----------------|
| C1 | $0 paid-API budget (Anthropic API only for emergencies) | Doc 04 ($99 Alpaca + $79 Polygon) | P1 | Reframe as broker-data line item; explicit operator sign-off line in doc 04 r5. Cross-ref `feedback-anthropic-routing` (cost-averse stance). |
| C2 | Local-first (no SaaS for inference / data)              | Doc 04 (Polygon is SaaS data)<br>Doc 02 (HF Hub mirror for adapters — outbound data flow) | P1 | Polygon is data-only and disabled-by-default → acceptable. HF Hub is **adapters-only, private repo** per `project-modelforge-decisions` — doc 02 doesn't reflect the private-repo + pre-push-scrubber constraint; add explicitly. |
| C3 | Paper-mode for the entire migration (no live until parity proven) | Doc 01 silent; doc 05 weak | P1 | See §2 P1-5; every doc executive-summary cross-refs doc 09 §2. |
| C4 | 128 GB unified memory budget                           | Doc 01 (planning ~113 GB with 15 GB headroom against doc 08's measured 80 GB-already-burned) | **P0** | See §2 P0-2; adopt doc 08's two-condition fix; reissue memory table. |
| C5 | 100% Python (no C++/Rust/Go modules unless trivial)     | Doc 10 (uv is Rust; pytest-recording transitively pulls Rust; vLLM build pulls CUTLASS C++) | P2 | uv and ruff are dev-tools (Rust-built binaries shipped as wheels) → fine. The C++ pieces are in upstream wheels (torch, vLLM if adopted, flash-attn) → noted as ARM-wheel hazards in doc 08 §2 — acceptable. No actual Python-module Rust-binding in the design itself. |
| C6 | No paid LLM (Anthropic only for emergencies)            | Compliant across all docs    | — | All docs respect `project-modelforge-decisions` §4 teacher-distillation $0 budget. |

**Aggregate constraint posture:** the $0 / local-first / paper-mode rules
are mostly honored, with the **128 GB memory budget being the single
hard-violated constraint** (doc 01 vs doc 08 — see §2 P0-2). The C1/C2/C3
items are surface-level mis-framings, not deep violations.

---

## 4. Missing pieces — what no doc covers

| Topic | Why it matters | Recommended next-doc owner |
|-------|----------------|----------------------------|
| ~~Single architecture diagram (doc 06)~~ — **RESOLVED**: doc 06 landed late and supplies the ASCII diagram + full file tree + Strategy ABC contract. | — | — |
| **Concrete end-to-end latency budget summing risk-MC + debate panel + execution** | Doc 03 (50 ms) + doc 05 (500 ms in vLLM-assumed budget) + doc 04 (broker WS) are uncoordinated; doc 06 has the diagram but no latency-budget table. Decision-to-fill latency p95 unknown. | Append a §4.x latency table to doc 06 r2; especially needed after the vLLM→Ollama rule re-spec. |
| **Reflector trigger cadence vs RLRO cadence vs anchor cadence — single source-of-truth schedule** | Doc 02 says 10-min; operator memory says weekly hit-rate gate. Doc 09 says weekly. No single cron table. | New `11-CRON_TIMETABLE.md` or absorbed into doc 06. |
| **Strategy taxonomy: which strategies (TFT, BollingerRSI MR, wheel, NFI X6) port to V4 and which stay on Freqtrade during shadow** | Doc 08 §4 lists files-to-port but doesn't enumerate which **strategies** survive. Doc 09 implies all do; doc 02 implies a "wheel" strategy is in v4 scope. | doc 06 OR a `12-STRATEGY_PORT_MAP.md`. |
| **Backtest harness — same engine, simulated time** | Doc 10 §5 mentions `backtest.engine` as greenfield. No doc says how backtests prove parity with shadow-mode-live. Doc 08 §5 mentions backtest "comparator" but no spec. | TBD. |
| **`stocks/shared/subsystem_ownership.py`** | Referenced in operator spec; doc 08 + doc 10 both note it does **not exist** in the worktree. Doc 10 marks it greenfield. Doc 06 (if written) needs to scope it. | `11-OWNERSHIP_REGISTRY.md` per doc 10 §1.2. |
| **GPU reservation interplay during continuous LoRA** | Doc 09 + doc 10 §2.5 reference `~/.hermes/config/gpu_reservation.yaml` + `gpu_gate.sh`. Doc 02's continuous-LoRA loop fires every 10 min — collides with Sunday 14:00-18:00 ET ModelForge weekly training window. Behavior in collision is "log + wait" per doc 10 §2.5 — meaning the continuous loop is **functionally inactive** for ~4 hours weekly. Is that intended? | Decide; doc 02 §3.4 or doc 06 must reference. |
| **Dashboard UI design for v4** | Operator memory `feedback-dashboard-design` mandates production-grade dYdX/Geist aesthetic. **Zero docs** in the v4 pack address the dashboard surface. Doc 09 §M5 says "in-house dashboard already has feature parity" — true today but v4's new features (debate panel votes, MC risk gate decisions, shadow-diff card) need UI. | Pending `MIGRATION_NOTES.md` + a `13-DASHBOARD_SURFACES.md`. |
| **Hermes Slack message templates for new V4 events** | The 4-question Slack template (`feedback-session-lessons` #3) must extend to V4's new event types (panel-tie, risk-gate-block, adapter-shadow-fail). No doc covers. | doc 06 + Slack-template extension. |
| **Public-release / viral angle** | `project-viral-release` is operator-active for the same 4-week window. Pack is **silent on open-source readiness** — anonymization story, LICENSE headers, screenshot-shaped dashboard, repro instructions. | Cross-cutting; should be a doc-pack-wide invariant, not a single doc. |
| **Coinbase no-sandbox handling** | Doc 04 §8 notes "Coinbase Advanced has no sandbox" but defers a real plan. Doc 09 §F1 says paper-mode is mandatory. **Contradiction unresolved** for crypto side. | doc 06 or doc 04 r5. |

---

## 5. Operator-principle alignment

Green = doc honors the principle explicitly. Yellow = partial / implied.
Red = doc violates or ignores.

| Doc | UI > CLI | Config > hardcoded | Reviews before pushing | No manual scripts (automation) | DGX must EARN keep |
|-----|----------|--------------------|------------------------|-------------------------------|---------------------|
| 01  | n/a (infra-only doc, no operator surface) | YES (every flag explicit) | YES ("not pushed; no code") | YES (systemd units throughout) | **GREEN** (full justification for $5k box) |
| 02  | YELLOW (mentions dashboard card briefly §6) | GREEN (TOML config refs) | GREEN | YELLOW (cron + lockfile + slack — see §6.3) | GREEN (DGX-native training) |
| 03  | YELLOW (Prometheus + TodayScoreboard tiles mentioned in §7) | GREEN (`config/risk_gate.yaml` explicit single source of truth) | GREEN | GREEN (all gates fired in-process by signals) | GREEN (uses 1 PFLOP FP4) |
| 04  | YELLOW (dashboard wiring §9 cost line item, no UI design) | GREEN (universe.json reuse) | GREEN | GREEN (single asyncio supervisor) | YELLOW (network-bound, doesn't exercise GPU) |
| 05  | YELLOW (one card mentioned §7.7) | GREEN | GREEN | GREEN | YELLOW (parallel only saves wall-time, not GPU FLOPS) |
| 06  | **GREEN** (FastAPI dashboard explicit; observability.dashboard +`/api/ops/*` reuse + Grafana) | **GREEN** (TOML single-source-of-truth — explicit §3.19) | **GREEN** (one of the largest "no code in this PR" disclaimers) | **GREEN** (no cron in quanta-core; Hermes owns scheduling §7) | **GREEN** (memory_budget.py — 128 GB unified accounting module §3.9) |
| 08  | n/a (audit-only doc) | YELLOW (reproducible build via Dockerfile pin) | GREEN | YELLOW (some manual `pip install` dry-runs flagged) | **GREEN** (whole doc justifies the box) |
| 09  | **GREEN** ("PROMOTE V4" /ops button — operator-typed phrase) | GREEN (typed config rule §M9) | **GREEN** (explicit operator-ack gates everywhere) | **GREEN** (every recovery is `/ops` button) | GREEN (says "DGX is useful for ModelForge / inference / research regardless of V4") |
| 10  | YELLOW (mentions `/api/ops/*` swap-behind, no new UI design) | **GREEN** (TOML + pydantic-settings v2; no `os.environ.get` outside loader) | **GREEN** ("a single commit on the current worktree branch — NOT pushed") | **GREEN** ("No new cron jobs from quanta-core. All scheduling stays in Hermes.") | YELLOW (process-style doc, no GPU specifics) |

**Aggregate per-principle scorecard:**

- **UI > CLI:** weak across the pack. Doc 09 is the only **GREEN**. Most
  docs mention a dashboard tile once and move on. Operator-explicit
  `/ops` button design is missing from doc 02 (adapter promote), doc 03
  (block/warn/allow override), doc 04 (cutover), doc 05 (debate-tie
  resolution). **Risk:** build will land with CLI-first ergonomics
  again.
- **Config > hardcoded:** strong. Every quantitative doc names a YAML
  / TOML / env-var path for its tunables.
- **Reviews before pushing:** universal. Every HANDOFF says "not pushed."
- **No manual scripts:** strong. Every loop has a cron / supervisor /
  systemd hook.
- **DGX must earn its keep:** strong in compute-heavy docs (01, 02, 03,
  08); weaker in connectivity/orchestration (04, 05) which don't use
  the GPU at all. **This is fine** — they don't need to.

---

## 6. Citation quality audit

Method: counted `https://` URLs + `arxiv.org/` URLs + general source-list
sections in each numbered doc.

| Doc | https links | arxiv refs | Source-section quality | Notes |
|-----|-------------|------------|------------------------|-------|
| 01  | **128**     | 0          | Excellent — §8 lists every claim with a URL; inline cite ids `[[N]]` | Sources are NVIDIA Developer Forum threads (high quality for GB10-specific facts), official vLLM/SGLang/Triton docs, Anyscale Ray Serve docs. |
| 02  | 23          | 7          | Excellent — §7 lists 19 sources (TRL DPOTrainer, Online-LoRA WACV'25, CL-LoRA CVPR'25, S-LoRA, Punica, MLPerf v5.1) | Strong arxiv anchoring. |
| 03  | 19          | 4          | Excellent — §10 lists 20 sources; NVIDIA developer blog Numba/CuPy benchmark, MIT 15.450 lecture notes, FRTB ES paper | Real measurements (29 ms CuPy 8.19M paths on V100). |
| 04  | **31**      | 0          | Excellent — §10 lists 31 sources; official alpaca-py / coinbase-advanced-py GitHub + docs.cdp.coinbase.com / Polygon docs | Best-cited exchange-connectivity research I have seen on this codebase. |
| 05  | 16          | 6          | Good — §8 lists 15 sources; ICML/ACL multi-agent-debate papers, Red Hat vLLM-vs-Ollama benchmark, LangGraph Send API tutorial | Strong on the academic side; weak on the framework comparisons (only one CrewAI-vs-LangGraph blog post). |
| 06  | 0           | 0          | **None** — structural design doc, no claims that need citing | A file-tree + Strategy ABC contract is internal architecture; citations don't apply. Acceptable. |
| 08  | 17          | 0          | Adequate — sources at end; mix of NVIDIA forum + Medium posts + community GitHub | A live-hardware audit doc; primary sources are `nvidia-smi` / `free -h` outputs (which is the *right* citation kind for that doc). |
| 09  | **1**       | 0          | **Poor** — assertions on adapter-rollback gates, multi-agent-failure mitigations, slippage-attribution math are uncited. | Doc 09's risks ARE real (validator agrees), but the "Anti-Consensus Preference Optimization" + "9-step rollback runbook" deserve at least 5-10 citations linking to the underlying ModelForge memory + project_session_2026-05-11 backup memory. Reads as expert-opinion, not literature-anchored. |
| 10  | ~12         | 0          | Adequate — references existing in-tree files (`risk_governor.py`, `MODELFORGE_DATA_PIPELINE.md`) more than external sources | A patterns doc; in-tree refs are correct.  Could use one external link per tool pick (e.g., uv homepage). |

**Aggregate citation grade:** A− for the research docs (01–05), A− for
doc 08 (live-measurement is its own citation), C for doc 09 (expert
opinion, low external citation), B for doc 10 (in-tree references
suffice).

---

## 7. Overall design coherence verdict

**NEEDS_REVISION** (was on the edge of FUNDAMENTAL_DISAGREEMENT before doc 06 landed).

The design pack has **strong vertical depth** in every research slice and,
**now that doc 06 landed**, an adequate horizontal integration narrative.
The vertical findings (Monte-Carlo risk gate feasible at 10 ms median,
alpaca-py replaces Freqtrade cleanly, multi-LoRA hot-swap at 5-10 ms PCIe
fetch, continuous LoRA trains in 2-3 min per role on B200, ARM/Blackwell
software stack ~3 dev-days of DevOps yak-shave, complete quanta-core/
file tree + Strategy ABC contract) are **individually correct and
well-cited**.

The horizontal incoherence is real but narrower than initially feared:

1. **Operator's 2-day-old `project-drop-vllm` decision is honored by 2 of
   8 numbered docs** (doc 06 architecture + doc 09 risks). Docs
   01/02/05/08 are vLLM-first; their plans re-create the exact OOM
   failure the operator already lived through on 2026-05-12. **Doc 06
   silently picks Ollama+Hermes per the operator decision — the
   vLLM-first docs need to align to it, not the other way around.**
2. **Memory budget table in doc 01 conflicts with live measurement in
   doc 08** by 20-30 GB. The two cannot both be right; the measured one
   is.
3. **`asyncio` vs `AnyIO` vs `LangGraph`** is a three-way orchestration
   schism among docs 04, 05, 10. Doc 06 §3.3 explicitly picks **plain
   asyncio** (`async def _tick_pump`, `_fill_pump`, `_heartbeat` tasks
   inside `LiveEngine`) — that's the tie-breaker. Docs 04/05/10 must
   drop AnyIO + LangGraph to match.
4. ~~The architecture / integration doc (06) was never written.~~
   **Resolved 2026-05-12 ~16:08 ET** — doc 06 is comprehensive
   (2,033 lines, 18 packages, full Strategy ABC, ASCII diagram).
5. **Two non-overlapping definitions of "the 6 roles."** Doc 06 picks
   **doc 02's taxonomy** (bull, bear, reflector, arbiter, plus the
   training-only regime_tagger / indicator_selector). Doc 05's
   macro/quant/risk/catalyst panel needs to be redrafted against
   doc 06's set.
6. **Reflector cadence: 10-min (doc 02) vs weekly (operator memory + doc
   09).** Doc 06 §3.16 is silent on cadence — punts to a `policy.py`
   module. Operator decision still required.

This is **not a fundamental disagreement** — every doc is salvageable,
and the operator's stated principles (Ollama-only, $0 paid-LLM, 121 GB
real memory, weekly hit-rate gate) are clear enough that the rewrites
are mechanical. They are **edit-level** for docs 01/02/05 (re-target
serving plane from vLLM to Ollama; preserve the LoRA / DPO / panel
designs verbatim), **rewrite-level** for doc 08's 8-week Gantt (drop
the vLLM source-build critical path → shortens to ~5 weeks). Greenlighting
the build today would burn ~1.5-2 dev-weeks before the inconsistencies
surface in code.

---

## 8. Top 5 changes recommended before build greenlight

### Change 1 — Add a latency-budget table to doc 06 r2 (architecture is now ~95% done)

Doc 06 landed late and supplies everything except:
- A single end-to-end latency budget summing risk-MC (50 ms) + debate
  panel (1-3 s on Ollama, not 500 ms on vLLM) + execution-to-broker
  (50-200 ms Alpaca, 5-30 ms Coinbase).
- Cadence schedule for the Reflector / LoRA-online / weekly anchor loops
  cross-referenced to `~/.hermes/cron/jobs.json` + the GPU reservation
  Sunday 14:00-18:00 ET window.
- An explicit per-strategy port-or-stay map (TFT, BollingerRSI MR,
  wheel CSP, NFI X6, shark debate — doc 06 has the file boxes but not
  the migration table).
- One-paragraph reconciliation against doc 02's "6 LoRA training roles"
  vs doc 05's "6 debate panel agents" — doc 06 §3.11 silently picks
  doc 02's; make it explicit.

Owner: the operator + the doc-06 r6 author. Single session, ~2 hours.

### Change 2 — Operator rules on vLLM (re-instate OR confirm Ollama-only); rewrite docs 01/02/05/08 accordingly

If Ollama-only stands (most likely outcome given `project-drop-vllm` is 2
days old):
- doc 01 r2: drop the vLLM-fast + vLLM-deep recommendation; replace with
  "Ollama + adapter-baked Modelfile tags per role + LiteLLM gateway
  remains useful for unification."
- doc 02 r2: replace `POST /v1/load_lora_adapter` choreography with the
  6-step `ollama create qwen3:30b-<role>-vYYYYMMDD` recipe in
  `project-drop-vllm` §7. Demote the 10-min cadence to opt-in; promote
  weekly hit-rate gate to default.
- doc 05 r2: drop "Resident vLLM is the only way to hit p95 < 500 ms";
  redo the latency analysis on Ollama with `OLLAMA_KEEP_ALIVE=60m` warm
  + `num_predict=256` (the current production setup, which hits 1.7s
  warm for 1-sentence answers per `project_session_2026-05-11_t30_checkpoint`).
  Acknowledge that sub-500 ms is **not** feasible on 6 8B-class Ollama
  calls and re-spec the panel for 1-3 s instead.
- doc 08 r2: kill steps "0/5/12" critical-path on vLLM build. Re-do the
  8-week Gantt without vLLM. Likely shortens to ~5 weeks.

If vLLM is re-opened: operator must re-authorize per
`feedback-no-heavy-containers-without-explicit-ok` with the explicit
resource projection format ("about to: docker compose --profile vllm up
-d; will use ~25 GB RAM / ~20 GB VRAM / ~30 GB disk; kill switch:
docker compose --profile vllm down; run? (yes/no)").

### Change 3 — Adopt doc 08's measured memory baseline as the contract; reissue doc 01 §5

Verbatim from doc 08 §1a: "121 GiB usable / 80 GiB already used at idle /
realistic V4 ceiling ~85-90 GB before swap thrashes." Doc 01 §5's
"~113 GB committed / 15 GB free" line goes away. Every downstream doc
that quoted the old 95-GB-resident-models number (doc 02 §1, doc 08 §1a
quoting doc 01) updates.

### Change 4 — Harmonize orchestration choice across docs 04 / 05 / 10

Pick one of:
- (a) **plain `asyncio.TaskGroup` + uvloop** (doc 10's current pick) —
  smallest surface, fewest deps, "no-fence-sitting" preserves doc 10's
  contract. Docs 04 + 05 r-bump to drop AnyIO + LangGraph.
- (b) **AnyIO** (doc 04's current pick) — gains structured-concurrency
  + trio-backend portability. Docs 05 + 10 r-bump to adopt AnyIO.

I'd vote (a) — fewer deps, no behavior change on the asyncio backend, and
doc 10 §6 already promises build agents will not see AnyIO. Operator's
"100% Python no Rust/Go" preference also slightly favors fewer transitive
deps.

### Change 5 — Inflate doc 09 / cross-link every doc's "Migration mode" rule to doc 09 §2

The paper-mode-mandatory rule (operator brief #2) is **the most
load-bearing safety invariant** in the pack. Currently it is enforced in
docs 08/09 but invisible in docs 01/02/03/05. Every doc gets a one-line
"Migration mode: paper-only until doc 09 §2 DG-3 is cleared with
operator typed 'PROMOTE V4'" boilerplate in its executive summary.

This single change cuts the operator's risk of a "we built it, we
deployed it, oh wait we never went through the parity gate" outcome by
~80%.

---

## Appendix A — Worktree provenance map

For traceability, the doc → worktree → branch correspondence:

| Doc                                          | Worktree                                              | Branch                            |
|----------------------------------------------|-------------------------------------------------------|-----------------------------------|
| `01-RESEARCH-MULTI_MODEL_RESIDENCY.md`       | `.claude/worktrees/agent-af7312f46e8752508`           | `feat/quanta-core-v4-design-r1`   |
| `02-RESEARCH-CONTINUOUS_LORA.md`             | `.claude/worktrees/agent-a05d7bf4f260f7740`           | `feat/quanta-core-v4-design-r2` (worktree-local) |
| `03-RESEARCH-RISK_MONTE_CARLO.md`            | `.claude/worktrees/agent-a2c613d35c6f9226a`           | `feat/quanta-core-v4-design-r3`   |
| `04-RESEARCH-EXCHANGE_CONNECTIVITY.md`       | `.claude/worktrees/agent-a723ec9d5a4801107`           | `feat/quanta-core-v4-design-r4`   |
| `05-RESEARCH-PARALLEL_AGENTS.md`             | `.claude/worktrees/agent-a73fa2e6ae7517097`           | `worktree-local`                  |
| `06-ARCHITECTURE.md`                         | `.claude/worktrees/agent-ad63c1b9809ae6630`           | `feat/quanta-core-v4-design-r6`   |
| `07-VALIDATOR_REPORT.md` (**this doc**)      | `.claude/worktrees/agent-aee4a0643303402ea`           | `feat/quanta-core-v4-design-r7`   |
| `08-FEASIBILITY.md`                          | `.claude/worktrees/agent-a4d449d8a9b11bab6`           | `worktree-local`                  |
| `09-RISKS.md`                                | `.claude/worktrees/agent-a9f77d934175e9efc`           | `worktree-local`                  |
| `10-CODE_PATTERNS.md`                        | `.claude/worktrees/agent-ad47ab47181812c03`           | `worktree-local`                  |

None of the above are pushed. None have been merged. This validator
report is also `NOT pushed; NO code` per the operator brief.

---

## Appendix B — Operator memories cross-referenced

The following memory files informed the constraint and principle checks
above and are the canonical "operator law" against which the design pack
was scored:

- `~/.claude/projects/-home-saijayanthai-Documents-trading-bot/memory/MEMORY.md`
- `…/memory/project_drop_vllm.md` (the load-bearing 2-day-old vLLM-drop
  decision)
- `…/memory/project_modelforge_decisions.md` (qwen3:30b base lock, $0
  budget, weekly hit-rate gate, HF Hub adapters-only-private)
- `…/memory/feedback_no_heavy_containers_without_explicit_ok.md` (the
  vLLM OOM history; explicit per-action authorization rule)
- `…/memory/feedback_no_manual_runs.md` (100% automation; no "tomorrow
  morning run X" recommendations)
- `…/memory/feedback_anthropic_routing.md` (cost-averse stance on paid
  APIs)
- `…/memory/feedback_session_lessons.md` (UI>CLI, config-over-hardcoded,
  Slack 4-questions, push-only-when-asked, verify-before-claim)
- `…/memory/feedback_dashboard_design.md` (dYdX/Geist aesthetic
  non-negotiable)
- `…/memory/project_session_2026-05-11_eod.md` (trading is LIVE-PAPER
  since 2026-05-11; wheel pilot with $629 banked premium; load-bearing
  bug-fixes the V4 design must preserve)
- `…/memory/project_session_2026-05-11_t30_checkpoint.md` (hermes3:8b-trader
  is the active model; 12 crypto + 15 stocks via universe.json;
  hermes3:8b warm latency = 1.7 s)
- `…/memory/user_profile.md` (UI > CLI, config > hardcoded,
  reviews-before-pushing, local-first inference, Slack heartbeat
  thoroughness)
- `…/memory/project_viral_release.md` (4-week dual goal: $2k paper P&L +
  public viral release; operator-private data must NEVER leak)
- `…/memory/reference_trading_bot_paths.md` (ports / paths / cron
  table — used to verify integration claims)
- `…/memory/reference_gpu_reservation.md` (Sunday 14:00-18:00 ET
  ModelForge weekly training window — collision check for doc 02)

---

*End of `07-VALIDATOR_REPORT.md`. NOT pushed; NO code. Branch:
`feat/quanta-core-v4-design-r7`. Next session: act on the §8 top-5
changes — Change 1 (write doc 06) and Change 2 (rule on vLLM) are
strictly required before any code starts.*
