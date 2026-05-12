# Quanta Core v4 rev2 — 01: Multi-Model Residency on DGX Spark (GB10) — Ollama-only Revision

**Status:** Research only. No code changes. Branch: `feat/quanta-core-v4-rev2-r1`
**Date:** 2026-05-12
**Author:** Claude (subagent, rev2 cycle — post-validator)
**Supersedes:** `docs/quanta-core-v4/01-RESEARCH-MULTI_MODEL_RESIDENCY.md` (vLLM-first; left in place for diff record)
**Scope:** Re-pick the inference-serving stack to host the v4 LLM workload on **one DGX Spark (Grace-Blackwell, 128 GB unified LPDDR5X, ARM aarch64, sm_121, CUDA 13)** after three operator-level facts that landed after the original doc shipped:

1. **vLLM was DROPPED** by the operator on 2026-05-12 (memory: `project_drop_vllm.md`). vLLM bootstrap pushed the box to 95 GiB / 121 GiB + 7.3 GiB swap before being killed; it also loaded qwen3:30b a *second* time on top of Ollama's existing copy. The single-user / 5-15-calls-per-debate workload does not benefit from vLLM's batched throughput.
2. **Idle baseline is ~80 GB used / ~41 GB available** (live `free -h` snapshot in `08-FEASIBILITY.md` §1a, 2026-05-12 15:55 UTC). The original doc 01's "happy plan" committed ~113 GB simultaneously — it re-creates the OOM the operator already survived.
3. **Trading philosophy is Buffett-style: 2-3 trades per week TOTAL across all assets.** Sub-second loops are not the design goal. Most candles produce NO TRADE. 30-second deliberation by a 70B arbiter on the rare candle that does fire IS the design.

Doc `07-VALIDATOR_REPORT.md` flagged all three of these as **P0** against the original doc 01. This rev2 doc deletes the vLLM stack from the architecture and re-spec's residency as **single-model-resident-at-a-time, load-on-demand, evict-on-completion**, served by **Ollama**.

---

## 1. Executive Recommendation

**Run a single Ollama daemon (the one already installed and serving the production trading bot) as the inference plane for every LLM role in v4.** Concretely:

- **8B fast classifier** — `hermes3:8b` (or a small, adapter-baked variant `hermes3:8b-<role>-vYYYYMMDD`) handles regime tagging, indicator selection, and the prefilter that decides whether a candle even warrants debate. Warm latency ~1.7 s (measured, `project_session_2026-05-11_t30_checkpoint`). Stays warm during US/crypto trading hours via `OLLAMA_KEEP_ALIVE`.
- **70B deliberate debate model** — `hermes3:70b` (Q4 GGUF, ~40 GB resident) is **loaded on demand** when the 8B prefilter says "this candle deserves debate." It plays bull_debater, bear_debater, arbiter, and (nightly) reflector. Multi-round debate budget is ~30 s p95 — well within the operator's 2-3-trades-per-week cadence. After the debate completes, the 70B is **evicted immediately** to free unified memory for TFT training, sentiment polling, and the dashboard.
- **NO vLLM. NO TensorRT-LLM. NO NIM. NO NVFP4.** The operator has dropped vLLM (2026-05-12) per `project_drop_vllm.md`; TensorRT-LLM and NIM were already deferred in the original doc; NVFP4 is documented as crashing with CUDA illegal instruction on ARM64 GB10 sm_121 in current vLLM mainline ([vLLM #35519](https://github.com/vllm-project/vllm/issues/35519)) and is moot once we leave vLLM behind.
- **No permanent multi-resident plan.** With 2-3 trades/week across all assets and a measured 80 GB-already-burned idle baseline (`08-FEASIBILITY.md` §1a), keeping a 40 GB 70B model permanently resident alongside `hermes3:8b` + TFT + the sentiment pipeline + Freqtrade + Postgres + the dashboard + Chromium dev tabs simply doesn't fit and isn't needed.
- **LoRA hot-swap via Ollama Modelfile `ADAPTER` directive.** Per-role adapters are baked into named tags (`hermes3:70b-arbiter-v20260512`, etc.) by a ModelForge promotion cron. Hermes triggers `ollama create` on adapter publish. First request to a freshly-tagged model takes a few seconds (GGUF page-in); subsequent calls are warm. This is the choreography already locked in `project_drop_vllm.md` §2 and `project_modelforge_decisions.md` (qwen3:30b base, Unsloth → GGUF native, weekly hit-rate gate).

Net effect vs the original doc 01: **simpler architecture, zero memory risk, same end state.** Ollama already runs on the box (port 11434); the operator already knows its ops surface (`OLLAMA_KEEP_ALIVE`, `OLLAMA_MAX_LOADED_MODELS`, `ollama ps`); the existing trading bot already calls it via `chat_json()`. The remaining v4 work is choreography (load-on-demand + evict + Modelfile rebuild cron) — not a new serving stack.

---

## 2. Why Ollama Wins for THIS Workload

The original doc 01 scored serving stacks on multi-tenant data-center criteria (throughput, multi-LoRA hot-swap APIs, PagedAttention KV reuse, p95 < 500 ms). Those criteria are the right scorecard for a SaaS inference platform. They are the **wrong scorecard** for a Buffett-style single-operator trading bot that fires 2-3 times per week. The right scorecard:

| Criterion | Why it matters here | Ollama | vLLM |
|---|---|---|---|
| **Selectivity over speed** | Most candles produce NO trade. The model only thinks DEEPLY when a setup forms. A 1.7 s warm latency on the fast classifier is plenty when the next decision point is 5-15 minutes away. | YES (already deployed, already 1.7 s warm) | Faster batched throughput; irrelevant at our QPS |
| **Memory ceiling discipline** | Measured 80 GB used at idle; box dies at ~95-100 GB physical + swap thrash. Adding a second weight loader (vLLM loads qwen3:30b a second time on top of Ollama's copy) is suicidal. | Single weights pool, single resident model at a time | Loads its own copy — measured to push box to 95 GB / 121 GB then OOM |
| **Eviction acceptable** | 2-3 trades/week → cold-load penalty of 25-40 s for a 40 GB model off NVMe (per `08-FEASIBILITY.md` §1a) is paid maybe 3 times per week. That's 90-120 s of cold-load cost per week. Nothing. | YES — `OLLAMA_KEEP_ALIVE` semantics make this natural | Anti-pattern in vLLM's design |
| **Mature ARM64 build** | DGX Spark is aarch64. PyPI does not ship a vLLM wheel for `aarch64 + CUDA 13 + sm_121`; the recommended path was a community `vllm-custom` fork with un-upstreamed MXFP4 patches (`08-FEASIBILITY.md` §2). Ollama ships first-party ARM64 builds and has a published NVIDIA Spark perf blog. | YES — first-party, NVIDIA-blessed | NO — community fork, sm_121 patch set, NVFP4 broken |
| **Operator already knows the ops surface** | `OLLAMA_KEEP_ALIVE`, `OLLAMA_MAX_LOADED_MODELS`, `ollama ps`, `ollama create`. Familiar, debuggable, documented in operator memory. | YES | NO — new ops surface, new failure modes, new dashboard tiles |
| **30s debate budget makes single-model-resident OK** | A 6-message bull/bear/arbiter round on `hermes3:70b` Q4 at ~25-35 tok/s decode (measured class on Spark per `01` original sources) is ~30 s wall-time. We don't need 6 concurrent vLLM processes; we need one model thinking carefully. | Natural fit | Over-engineered for this cadence |
| **Adapter hot-swap latency** | Weekly hit-rate-gated promotion (per `project_modelforge_decisions.md`) means adapter rebuild + tag-swap happens at MOST weekly, typically less. "A few seconds for the GGUF page-in" on first request is acceptable. | YES — `ollama create` + alias flip | vLLM's runtime `/v1/load_lora_adapter` is faster but unnecessary at this cadence |
| **$0 paid-API budget** | Operator-locked rule. Ollama is MIT, free, local. | YES | YES (Apache-2.0), but the source-build DevOps cost is non-zero |

**The killer argument is operational:** every "we should rebuild on vLLM" benefit (PagedAttention, multi-LoRA, batched throughput, p95 < 500 ms) optimizes a dimension we don't need. Every Ollama drawback (no PagedAttention, single hot model at a time, slower adapter swap) costs nothing at 2-3 trades/week. The original doc 01's analysis was correct in a vacuum and wrong in context.

Doc 07's verdict aligns: "Operator's 2-day-old `project-drop-vllm` decision is honored by 2 of 8 numbered docs (doc 06 architecture + doc 09 risks)... the vLLM-first docs need to align to it, not the other way around."

---

## 3. Memory Budget Table (revised against measured baseline)

Working from `08-FEASIBILITY.md` §1a's live `free -h` snapshot (2026-05-12 15:55 UTC) and a **single-model-at-a-time** residency rule:

| Component | Format | Resident size | KV cache (peak) | Notes |
|---|---|---|---|---|
| **Idle baseline** (Linux + Freqtrade + Postgres + Ollama daemon + dashboard SPA + Chromium dev tabs + docker + python3.14 worker) | — | **~80 GB** | — | **Measured live** 2026-05-12 15:55 UTC. This is non-negotiable; it is what the box looks like before v4 loads anything. |
| `hermes3:8b` (warm, always-loaded during trading hours) | Q5_K_M GGUF | ~8-10 GB | ~2 GB (4k ctx, batch 4) | The fast classifier. Pinned via `OLLAMA_KEEP_ALIVE=12h` during US/crypto active hours; evicted overnight via `OLLAMA_KEEP_ALIVE=0s` to free the box for the nightly reflector + TFT retrain. |
| 1 LoRA adapter baked into the active tag (rank 32, prose) | GGUF (folded into base via Modelfile `ADAPTER`) | ~150 MB extra at create-time | shared base KV | Adapter weights are folded at `ollama create` time, so runtime memory is essentially the base. |
| **8B-only "fast path" subtotal (deliberation NOT firing)** | | **~90 GB** | | Idle baseline + 8B warm. Safe; leaves ~30 GB headroom. This is the modal state of the box — 99%+ of candles. |
| `hermes3:70b` Q4 GGUF (loaded ON DEMAND when a setup forms) | Q4_K_M GGUF | ~40 GB | ~4 GB (4k ctx, batch 2) | Loaded by the orchestrator when the 8B prefilter flags a candle for debate. **Cold-load from NVMe: ~25-40 s.** Acceptable — we have already decided to take a position; 30 s of model paging is cheap. |
| **Peak deliberate subtotal (debate firing)** | | **~120 GB** | | 80 (idle) + 10 (8B) + 40 (70B) + ~4 (KV). **Within 121 GiB usable; ~1-2 GB margin.** Tight but survivable if discipline is maintained (no concurrent dashboard heavy task, no concurrent TFT retrain). |
| **Post-debate eviction** | — | back to ~90 GB | — | Immediately after `arbiter.respond()` returns, the orchestrator issues a 70B keep-alive of `0s` to free unified memory for the TFT training cron or the next sentiment poll. |
| Reserved overflow / swap (if discipline slips) | — | swap 31 GiB total, 5.6 GiB used at audit | — | Last-resort cushion. If we hit it we have a bug; trip an alert in `risk.governor`. |

### 3.1 Strategy: NO PERMANENT RESIDENCY

The rev2 architecture has **two memory profiles**, not one:

- **Fast-path profile (~90 GB committed, ~30 GB free):** the modal state. `hermes3:8b` warm; 70B not loaded; TFT and sentiment models available for inference (they're tiny — a few GB each). The box is comfortable in this mode for hours at a time.
- **Deliberate profile (~120 GB committed, ~1-2 GB free):** transient. Lasts ~30 s while the 70B is paged in, decodes the debate, and is paged out. During this window the orchestrator MUST hold off:
  - any TFT training run (`gpu_reservation.yaml` already gates this for the Sunday 14-18 ET ModelForge window; the load-on-demand gate is a tighter, per-debate version of the same idea)
  - any backfill cron that touches the GPU (the `gpu_gate.sh` pattern in `~/.hermes/cron/` is the right primitive)
  - any non-essential dashboard tile that pre-warms an embedding model

The mechanics for this gate are operationally trivial: a single Hermes flag (e.g. `~/.hermes/state/debate_in_flight`) checked by gpu-sensitive crons via the existing `gpu_gate.sh` skeleton.

### 3.2 Why we DO NOT keep the 70B resident across debates

Three reasons:

1. **It doesn't fit comfortably.** 80 + 10 + 40 = 130 GB, and we measured 121 GiB usable. Even with 0 KV cache that's a 9 GB deficit; with realistic 4-8 GB of KV growth on long contexts it's deeper. The original doc 01's "~113 GB committed / 15 GB free" line assumed an idle baseline that no longer reflects reality.
2. **It blocks the TFT retrain.** TFT QLoRA workspace is ~15 GB (per the original doc's source [[28]]). With the 70B permanently resident there is no room. The TFT retrain runs nightly and is the alpha source for the BollingerRSI MR strategy; cancelling it to keep a model warm we use 2-3 times a week is the wrong trade.
3. **It buys us nothing.** Cold-load cost is paid ~3x/week; warm-only saves ~120 s/week of wall-time. Not worth the residency cost.

The original doc 01 considered "load-on-demand" only briefly and rejected it because the original brief assumed sub-500 ms latency. With doc 05's revised debate budget (now ~1-3 s p95 on 8B Ollama + ~30 s for 70B deliberate per doc 07's reconciliation), load-on-demand is the correct architecture.

### 3.3 Discipline rules (codify in `quanta_core/serving/llm_budget.py`)

- `hermes3:8b` keep-alive defaults to `12h` during 08:00-20:00 ET on US trading days and crypto-active hours; `0s` otherwise.
- `hermes3:70b` keep-alive defaults to `5m` while a debate is in flight; the orchestrator's post-debate hook explicitly issues `keep_alive: 0s` on the final API call to evict immediately.
- A "debate-in-flight" flag at `~/.hermes/state/debate_in_flight` gates gpu-sensitive crons.
- `OLLAMA_MAX_LOADED_MODELS=2` (the 8B + at most one heavy on demand). Never 3.
- A periodic `ollama ps` poll exported to Prometheus + a dashboard tile. If the 70B is resident for >5 min outside a debate, alert.

---

## 4. Model Swap Strategy

### 4.1 Keep-alive choreography

Ollama's `keep_alive` parameter is the only knob we need. Per `project_drop_vllm.md` §5: "Ollama swaps the running model on first request with the new tag." Concretely:

| Lifecycle stage | API call shape | `keep_alive` | Effect |
|---|---|---|---|
| Trading day startup (06:30 ET) | `POST /api/generate` with empty prompt against `hermes3:8b` | `12h` | Pre-warm 8B for the session. |
| Steady state — every 5-15 min tick | regime_tagger / indicator_selector calls against `hermes3:8b` | `12h` (sticky) | 8B stays resident; no eviction race. |
| 8B prefilter says "debate this candle" | orchestrator calls `POST /api/generate` against `hermes3:70b` for the first debate turn (bull) | `5m` | 70B pages in (~25-40 s cold, much faster after the first call of the day). |
| Debate continues (bear, arbiter turns) | further calls against `hermes3:70b` | `5m` (refreshed) | Warm; sub-second to first token after the first turn. |
| Debate completes | final API call passes `keep_alive: 0s` | `0s` | 70B evicted immediately. Unified memory returned. |
| End of trading day (20:00 ET) | scripted call against `hermes3:8b` with `keep_alive: 0s` | `0s` | 8B evicted. Box freed for the nightly reflector + TFT retrain. |
| Nightly reflector cron (00:30 ET) | bulk call against `hermes3:70b` | `30m` (covers the full reflection pass) | 70B paged in for the nightly review; evicted at the end. |

This is **just Ollama API discipline.** No new daemons, no patched wheels, no new Docker stack. The choreography lives in `quanta_core/serving/ollama_client.py` as a thin wrapper around the existing `chat_json()` helper.

### 4.2 Failure modes and mitigations

| Failure | Cause | Mitigation |
|---|---|---|
| 70B fails to load (OOM) | Idle baseline drifted >85 GB (e.g. a Chromium tab opened a heavy dashboard) | The wrapper retries once after issuing `keep_alive: 0s` on the 8B. If still failing, skip the debate, log a `risk.gate.miss` event, and fall back to a no-debate "8B-only" decision (which the strategy may then choose to skip or trade at reduced size). |
| 70B page-in races a TFT training run | The debate-in-flight flag was missing or stale | TFT cron's `gpu_gate.sh` short-circuits if the flag is set; conversely, the orchestrator's debate gate refuses to fire if `gpu_reservation.yaml` says we're inside the Sunday 14-18 ET training window. Mutual exclusion. |
| Adapter tag missing | ModelForge promotion cron failed | Trading bot falls back to the previous date-stamped tag (kept on disk per `project_drop_vllm.md` §7). Alert the operator via the existing Slack template. |
| Ollama daemon crash | Anything | systemd auto-restart (already configured). The debate gate refuses to fire while `ollama ps` is unreachable. |

---

## 5. LoRA Hot-Swap — Ollama Modelfile `ADAPTER`

The original doc 01's killer feature claim for vLLM was `POST /v1/load_lora_adapter` with `load_inplace:true`. We don't need that primitive in rev2 because:

- adapter promotion is **weekly**, gated by hit-rate (per `project_modelforge_decisions.md` §3), not 10-minute as the original doc 02 proposed,
- Ollama's `ADAPTER` directive bakes the adapter into a new tag at `ollama create` time — no runtime `/v1/load_lora_adapter` needed,
- the first request to a newly-tagged model takes a few seconds (GGUF page-in). At weekly cadence, paying 5-10 s once per role per week is invisible.

### 5.1 The Modelfile rebuild flow

Owned by ModelForge (not by quanta-core). Concrete steps per `project_drop_vllm.md` §7:

1. Training step exits with a fresh `adapter.gguf` on disk (Unsloth's native `save_pretrained_gguf()` — no conversion script needed).
2. Promotion gate evaluates weekly hit-rate against the previous adapter's 7-day OOS window.
3. If the gate passes, ModelForge writes a Modelfile of the form:
   ```
   FROM hermes3:70b
   ADAPTER /opt/modelforge/adapters/<role>/<date>/adapter.gguf
   ```
4. ModelForge calls the host's Ollama REST `/api/create` with the new tag `hermes3:70b-<role>-vYYYYMMDD`.
5. A `hermes3:70b-<role>-current` alias is updated to point at the new tag (e.g. `ollama cp`). The trading bot reads from `current`.
6. Old tags are kept on disk for rollback (per `project_modelforge_decisions.md` weekly rollback gate).

The trading bot is unaffected by any of this — it always asks for `hermes3:70b-arbiter-current` (etc.) and Ollama resolves the alias. Promotion is transparent.

### 5.2 What this loses vs vLLM

- **In-process LoRA hot-swap latency.** vLLM's `load_inplace:true` is on the order of 5-10 ms PCIe fetch; Ollama's `create + page-in` is a few seconds. **Cost at 2-3 trades/week and weekly adapter rebuilds:** zero noticeable wall-time.
- **Multi-adapter batching.** vLLM can serve 8 adapters in a single batched request; Ollama serves one. **Cost:** zero — we never have 8 concurrent debates.
- **CPU-side LRU adapter cache.** vLLM keeps 32 in CPU memory, LRU. **Cost:** zero — we have <10 adapters total in the design (one per role per active version).

There is nothing v4 needs that Ollama's `ADAPTER` directive can't deliver at this cadence.

---

## 6. Per-Role Model Map

This is the binding contract for `quanta_core/serving/role_map.py`. Cross-checked against doc 06 §3.11 (which already picked Ollama + hermes3:8b / hermes3:70b) and `project_modelforge_decisions.md` (which locks qwen3:30b as the base — but doc 06 and `project_session_2026-05-11_t30_checkpoint` show the production runtime has standardized on `hermes3:*`. Where they conflict, doc 06's choice wins for the v4 trading hot path; ModelForge is free to also produce qwen3:30b-baked adapters in a parallel track for research).

| Role | Model | Cadence | Latency target | Loaded when | Adapter source |
|---|---|---|---|---|---|
| `regime_tagger` | `hermes3:8b-regime-current` | Every 5-15 min tick | < 2 s p95 (warm) | Always-warm during trading hours | Weekly ModelForge rebuild on regime-classification hit-rate |
| `indicator_selector` | `hermes3:8b-indicators-current` | Every tick that survives the regime gate | < 2 s p95 (warm) | Always-warm | Weekly ModelForge rebuild |
| `bull_debater` | `hermes3:70b-bull-current` | Only when 8B prefilter flags candle for debate (~2-3x/week) | First-token < 5 s p95 (post-page-in); full turn ~5-10 s | Loaded on demand; evicted post-debate | Weekly ModelForge rebuild |
| `bear_debater` | `hermes3:70b-bear-current` | Same as bull | Same as bull | Same as bull (same base weights, different baked adapter) | Weekly ModelForge rebuild |
| `arbiter` | `hermes3:70b-arbiter-current` | After both debaters respond | First-token < 5 s; full turn ~10-15 s | Same load-on-demand slot as bull/bear | Weekly ModelForge rebuild |
| `reflector` (nightly) | `hermes3:70b-reflector-current` | Nightly cron at 00:30 ET | n/a (offline) | Loaded for the cron window only | Weekly ModelForge rebuild from the previous week's outcomes |
| `TFT direction` | `quanta_core/models/tft.py` (PyTorch) | Per-tick inference; nightly retrain | < 100 ms inference | Weights mmap'd from disk; resident in unified memory ~5.5 GB; GPU-loaded only during the forward pass | Not an LLM; not a LoRA target |
| `sentiment classifier` | `hermes3:8b` (no separate model) | Every 15 min batch | < 5 s for 20-headline batch | Reuses the 8B warm slot | Optional adapter; default = base 8B prompt-tuned in code |

### 6.1 What this changes from the original doc 01

- **Drops** `vllm-fast` and `vllm-deep` as separate processes. There is exactly **one daemon: Ollama**.
- **Drops** the 8 GPU-resident + 32 CPU-cached LoRA slots. There are **N date-stamped Modelfile tags on disk** + a small set of `*-current` aliases.
- **Drops** the dedicated sentiment classifier (DeBERTa-v3-large class). Sentiment routes through the 8B Ollama path with a baked prompt. (Per `08-FEASIBILITY.md` we'd need a separate 3-5 GB model otherwise; not worth the residency cost given our throughput.)
- **Drops** the dedicated microstructure model (custom Transformer ~1B class). Doc 06 doesn't include it in the v4 hot path; rev2 follows.
- **Keeps** TFT as a PyTorch process with its own CUDA context, mmap'd weights. Unchanged from original.

### 6.2 LiteLLM gateway — KEEP, with caveat

The original doc 01's recommendation to put LiteLLM in front for unified OpenAI-compatible routing, per-model rate limits, spend tracking, and OpenRouter fallback **still makes sense** in rev2, because:

- the trading bot's `chat_json()` already speaks OpenAI-compatible,
- centralized logging of every LLM call is operator-required (see `LLM_CALLS_UX.md` + `LLM_LOGGER_SCHEMA.md`),
- emergency fallback to Anthropic on Ollama-daemon-outage is a real operator preference (per `feedback_anthropic_routing` — cost-averse but not zero-tolerance).

LiteLLM in rev2 fronts ONE backend (Ollama on :11434) instead of two (vLLM-fast on :8000, vLLM-deep on :8001). Smaller blast radius. Drop the `llama-swap` middlebox entirely — it would evict our pinned 8B.

---

## 7. Why We DON'T Need Multi-Resident

The original doc 01's core assumption was "the operator's hard requirement is **simultaneous residency, never evict**." That assumption no longer holds. It was inherited from a SaaS-shaped read of "multi-model serving" and was never reality-checked against the trading philosophy.

Replace it with the operator's actual policy (`project_session_2026-05-11_eod`, `user_profile`):

> "Buffett-style: 2-3 trades per week TOTAL across all assets. Most candles produce NO TRADE. Sub-second loops are over-engineering."

Concrete implications:

- **Sub-500 ms p95 is the wrong target.** Doc 05's revised debate budget (per doc 07 §8 Change 2) is 1-3 s p95 for 8B Ollama warm calls, ~30 s for a full 70B debate. Both are comfortable inside a 2-3-trade/week cadence.
- **6 concurrent vLLM processes is the wrong shape.** We don't have 6 concurrent decisions. We have ONE pending decision at a time, and it's allowed to take 30 s. The right concurrency primitive is `asyncio.TaskGroup` for the bull/bear fan-out (per doc 10's pick, ratified by doc 06 §3.3), and a single Ollama daemon underneath.
- **The DGX earns its keep by thinking DEEPLY, not by thinking FAST always.** $5k of hardware that handles the 30-second debate well when a real setup forms beats $5k of hardware that handles 8 concurrent debates at 500 ms p95 we never have.
- **Eviction between setups is acceptable.** Cold-load cost is 25-40 s for the 70B off NVMe. We pay it ~3 times per week. That's 90-120 s of wall-time per week paid for an architecture that doesn't blow up unified memory and doesn't force us to skip the TFT nightly retrain.

If a future v5 ever moves to higher-frequency strategies (intraday momentum, microstructure scalping) the right answer is to add a second DGX Spark (per the original doc 01's §7 question 9) and revisit Dynamo + TP=2 — not to compromise the rev2 design today.

---

## 8. Migration Cost

The original doc 01 budgeted **~9-11 focused days** across 6 phases, with the critical path being P0 (vLLM source build), P1 (8B migration to vLLM), P2 (LoRA wiring). Rev2 is **shorter** because we're keeping Ollama:

| Phase | Effort | Risk | Outcome |
|---|---|---|---|
| **P0 — Codify the keep-alive choreography** (`quanta_core/serving/ollama_client.py` wraps `chat_json()` with per-call `keep_alive` and the post-debate evict hook) | 1 day | Low — pure Python around an existing daemon | Single source of truth for load-on-demand and evict-on-completion. |
| **P1 — `debate_in_flight` gate** (file flag at `~/.hermes/state/debate_in_flight`, write/read helpers, `gpu_gate.sh` integration) | 0.5 day | Low — file IO + cron flag | TFT retrain and other gpu-heavy crons short-circuit while a debate is in flight; the debate refuses to fire during the Sunday 14-18 ET ModelForge window. |
| **P2 — Memory-budget watchdog** (`quanta_core/serving/llm_budget.py` polls `ollama ps` + `free -h` + writes a Prometheus gauge; alert if 70B resident outside a debate, or if idle baseline drifts > 90 GB) | 1 day | Low — observability only | Operator-visible memory pressure. Triggers a Slack alert before the box thrashes. |
| **P3 — Per-role aliases + ModelForge promotion hook** (Ollama-create cron in ModelForge promote step, ~50 LOC per `project_drop_vllm.md` §6) | 1 day | Low | `hermes3:70b-arbiter-current` resolves to the latest gated adapter; weekly hit-rate rollback works. |
| **P4 — LiteLLM gateway in front of Ollama** (config-only — LiteLLM already supports `ollama` provider) | 0.5 day | Low | Single endpoint, central log, spend tracking, OpenRouter / Anthropic fallback for daemon outages. |
| **P5 — Dashboard tile + Slack template** (memory-pressure card, debate-in-flight indicator, last-debate latency histogram) | 1 day | Low | Operator visibility per `feedback-dashboard-design` + `feedback-session-lessons`. |
| **Total** | **~5 focused days** (lower bound ~3 if everything ships clean) | Low overall | |

**Compared to original doc 01:** ~5-6 days saved. The biggest single savings is **not** building a patched vLLM (the original P0 + P1 was 3-5 days of CUDA/CUTLASS yak-shave). The second biggest is **not** writing a new dashboard for two vLLM processes.

**Sticky bits — what to watch:**

- **First-time 70B cold-load is slower than steady-state.** First debate of the day will hit ~25-40 s. Subsequent debates within the same 5 min window are warm. Cron a 06:30 ET pre-warm call against the 70B with `keep_alive: 60s` if the operator wants the first US-trading-hours debate to be warm.
- **Concurrent debate + TFT retrain is forbidden.** The `debate_in_flight` flag is load-bearing. Test the gate.
- **Adapter rollback must be one command.** `ollama cp hermes3:70b-arbiter-v20260505 hermes3:70b-arbiter-current` — verify this in a dry-run before relying on it.
- **`OLLAMA_MAX_LOADED_MODELS=2` is non-negotiable.** Three loaded simultaneously will OOM the box.

---

## 9. Open Questions for Operator

1. **First-debate warm-up:** do we want a 06:30 ET cron pre-warm of `hermes3:70b` to avoid the 25-40 s cold-load on the day's first real setup? Costs ~40 GB of unified memory for ~60 s. Recommendation: no — the rare 30 s delay on the first debate of the day is fine; we save the memory for any TFT retrain that ran overnight.
2. **Sentiment routing — confirm 8B is enough.** Original doc 01 budgeted a 3-5 GB dedicated sentiment classifier (DeBERTa-v3-large class). Rev2 routes sentiment through the 8B Ollama path with a baked prompt. Acceptable? If not, this single decision adds ~5 GB to the fast-path profile and brings us closer to the limit.
3. **`hermes3:70b` vs `qwen3:30b` as the deliberate base.** `project_modelforge_decisions.md` locks **qwen3:30b** as the ModelForge base. Doc 06 and `project_session_2026-05-11_t30_checkpoint` show the production runtime uses **hermes3:8b/70b**. Which is canonical for the v4 trading hot path? Rev2 assumes hermes3:70b matches doc 06; flag for operator confirmation.
4. **Nightly reflector — separate model or same 70B with a reflector adapter?** Rev2 assumes the latter (`hermes3:70b-reflector-current`). Means the reflector cron does NOT need a different base model paged in — same weights, different adapter-baked tag.
5. **`OLLAMA_KEEP_ALIVE` defaults by trading hours.** Rev2 proposes 12h during 08:00-20:00 ET on US trading days + crypto-active hours; 0s otherwise. Operator confirm the schedule and whether weekends keep the 8B warm for crypto (crypto trades 24/7).
6. **Multi-Spark v5 path.** If the operator ever adds a second DGX Spark, we should re-evaluate Dynamo + TP=2 + MXFP4 for the 70B (per original doc 01 question 9). Not in scope here; flag in v4.1 backlog.
7. **vLLM resurrection trigger.** Under what conditions would we re-evaluate? Suggested: only if (a) we add a second Spark, OR (b) the trading philosophy shifts to >50 trades/week. Both should require the explicit `feedback-no-heavy-containers-without-explicit-ok` authorization format.

---

## 10. Sources

Trimmed against the original doc 01's source list. Removed vLLM-specific patch-set citations, TensorRT-LLM playbook citations, NVFP4 / MXFP4 kernel citations, and Anyscale Ray Serve multi-LoRA refs (no longer relevant). Kept the Ollama refs, the Grace-Blackwell hardware refs (still load-bearing for the memory model), and the trade-philosophy / operator-decision refs.

1. [NVIDIA NVLink-C2C product page](https://www.nvidia.com/en-us/data-center/nvlink-c2c/) — coherent CPU/GPU interconnect, AMBA CHI / CXL, shared address space — still load-bearing because Ollama's runtime memory accounting straddles "VRAM" and "system RAM."
2. [Arm Learning Path: Unlock quantized LLM perf on DGX Spark — GB10 introduction](https://learn.arm.com/learning-paths/laptops-and-desktops/dgx_spark_llamacpp/1_gb10_introduction/) — NVLink-C2C 900 GB/s, unified 128 GB, zero-copy. Confirms why a single Ollama daemon sees the whole pool.
3. [Ollama Blog: NVIDIA DGX Spark performance](https://ollama.com/blog/nvidia-spark-performance) — first-party perf numbers on Spark: llama3.1-8B 7.614k prefill, gpt-oss-20B 3.224k/58.27, gpt-oss-120B 1.169k prefill. Establishes the 30 s 70B debate budget is realistic.
4. [Ollama FAQ — KEEP_ALIVE semantics](https://docs.ollama.com/faq) — `OLLAMA_MAX_LOADED_MODELS`, `OLLAMA_KEEP_ALIVE`, concurrent model load. The runtime knobs the rev2 choreography depends on.
5. [GitHub: ggml-org/llama.cpp Discussion #16578 — Performance of llama.cpp on DGX Spark](https://github.com/ggml-org/llama.cpp/discussions/16578) — 61 tps GPT-OSS-20B, 35 tps GPT-OSS-120B, 44 tps Qwen3-Coder-30B-A3B. Provides the per-token decode rate underpinning the 30 s debate budget estimate.
6. [NVIDIA DGX Spark hardware docs](https://docs.nvidia.com/dgx/dgx-spark/hardware.html) — 128 GB LPDDR5X spec; bandwidth 273 GB/s; CPU 20-core ARM. Establishes the gap between vendor-spec (128 GB) and measured-usable (121 GiB) we work against.
7. [Medium / Sparktastic: Choosing an Inference Engine on DGX Spark](https://medium.com/sparktastic/choosing-an-inference-engine-on-dgx-spark-8a312dfcaac6) — operator field reports: llama.cpp wins single-user; Ollama is the operationally easiest; vLLM container issues are real; TRT-LLM compile times are hours. Independently confirms the rev2 choice.
8. [NVIDIA Tech Blog: How DGX Spark's Performance Enables Intensive AI Tasks](https://developer.nvidia.com/blog/how-nvidia-dgx-sparks-performance-enables-intensive-ai-tasks/) — Llama-3.3-70B QLoRA 759.79 tps; Llama-3.2-3B FT 13519 tps; Llama-3.1-8B LoRA 6969 tps. Validates the 70B-class deliberate-debate latency expectation.
9. **In-tree:** `docs/quanta-core-v4/07-VALIDATOR_REPORT.md` — P0 findings on vLLM + memory; this rev2 doc directly implements §8 Change 2 and Change 3.
10. **In-tree:** `docs/quanta-core-v4/08-FEASIBILITY.md` — live-hardware measurement (80 GB used at idle, 121 GiB usable, ~85-90 GB realistic V4 ceiling). Source of the memory-budget revision.
11. **In-tree:** `docs/quanta-core-v4/06-ARCHITECTURE.md` — Ollama + hermes3:8b/70b role taxonomy that rev2 ratifies.
12. **Operator memory:** `~/.claude/projects/-home-saijayanthai-Documents-trading-bot/memory/project_drop_vllm.md` — the load-bearing 2-day-old vLLM-drop decision. Rev2 implements this verbatim for the trading hot path.
13. **Operator memory:** `…/memory/project_modelforge_decisions.md` — qwen3:30b base, $0 paid-API budget, HF Hub adapters-only-private, weekly hit-rate gate. The promotion-gate cadence rev2 inherits.
14. **Operator memory:** `…/memory/feedback_no_heavy_containers_without_explicit_ok.md` — the explicit per-action authorization rule. Any future re-introduction of vLLM/NIM/TRT-LLM must clear this gate.
15. **Operator memory:** `…/memory/project_session_2026-05-11_eod.md` + `…/memory/project_session_2026-05-11_t30_checkpoint.md` — production reality: hermes3:8b warm latency 1.7 s; 12 crypto + 15 stocks via universe.json; wheel pilot live with $629 banked premium.
16. **Operator memory:** `…/memory/user_profile.md` — Buffett-style cadence, UI > CLI, config > hardcoded, local-first.

---

## 11. TL;DR Decision

| Question | Answer |
|---|---|
| Stack? | **Ollama only, single daemon, load-on-demand 70B, evict-on-completion.** LiteLLM in front for unified routing + logging. |
| Why not vLLM? | Operator dropped it 2026-05-12 after it pushed the box to 95 GiB + 7.3 GiB swap before OOM. It loads weights a second time on top of Ollama's copy. And the workload (2-3 trades/week, 5-15 LLM calls per debate, 1 operator) does not benefit from batched throughput. |
| Why not multi-resident? | Doesn't fit (80 GB idle + 10 GB 8B + 40 GB 70B = 130 GB > 121 GiB usable). Doesn't earn its keep (we'd evict for the nightly TFT retrain anyway). Doesn't matter (~3 cold-loads/week = 90-120 s of wall-time we don't notice). |
| Why not NVFP4 / MXFP4? | Crashes on ARM64 GB10 sm_121 in current vLLM mainline ([vLLM #35519](https://github.com/vllm-project/vllm/issues/35519)). Moot once we leave vLLM; Q4 GGUF on Ollama is the safe path. |
| Hot-swap LoRA? | Ollama Modelfile `ADAPTER` directive + `ollama create hermes3:70b-<role>-vYYYYMMDD` + alias flip. Weekly cadence (hit-rate-gated, per `project_modelforge_decisions`). A few seconds of GGUF page-in on first call after promotion. |
| Per-role map? | 8B for regime / indicators / sentiment / prefilter; 70B for bull / bear / arbiter / reflector. TFT stays in PyTorch. |
| 5+ models simultaneously resident? | **No, and we don't need to.** Modal state is ~90 GB (idle + 8B warm); deliberate transient is ~120 GB (idle + 8B + 70B paged in for ~30 s); then back to ~90 GB after evict. |
| Biggest residual risk? | Idle baseline drift — if Chromium dev tabs or a stray docker-compose nudges the 80 GB baseline above ~85 GB, the 70B can fail to page in. Watchdog + Slack alert + the discipline to keep `OLLAMA_MAX_LOADED_MODELS=2`. |
| Migration cost? | ~3-5 focused days, 6 small phases. ~5-6 days *less* than the original doc 01's plan because we skip the vLLM source build. |

End of rev2 research document. NOT pushed. NO code.
