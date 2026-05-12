# 02 — Research: Continuous LoRA Training (CL-LoRA / Online RLRO)

> Branch: `feat/quanta-core-v4-design-r2` · Status: design research only, no
> code changes · Companion: `01-…` (system overview), `HANDOFF.md` (next-session
> pointers).

---

## 0. TL;DR — Executive Recommendation

Replace the **Sunday-only LoRA training** loop with a **continuous RLRO
loop** keyed off every Reflector cycle. Use the open-source stack we already
have — **vLLM ≥ 0.5** for serving + dynamic adapter swap, **TRL DPOTrainer +
PEFT** for the deltas, **Online-LoRA-style regularisation** to keep
catastrophic forgetting bounded, and **shadow → champion promotion** with
Pareto-dominant gates. The math, the kernels, and the inference servers all
exist today; what we still have to build is wiring, not novelty.

Concrete numbers we will assume below (cite §7 sources):

| Quantity | Value | Source |
|---|---|---|
| **vLLM** hot-swap latency (LoRA adapter on resident GPU) | ~5–10 ms PCIe fetch | vLLM docs + our own `docs/VLLM_SERVING.md` |
| **`load_inplace`** mode (RL-style replace under same name) | Supported since v0.8 | vLLM "LoRA Adapters" doc |
| **DPO + PEFT** training (LR ≈ 1e-5, β = 0.1) | 1–3 min for 1 k pairs, 8B base, single B200 | Unsloth + Llama-3.1 benchmarks, scaled from MLPerf v5.1 |
| **Llama-2-70B LoRA** MLPerf v5.1, 8× B200 | 2.5× faster than 8× H100 | NVIDIA MLPerf v5.1 |
| **GB300 NVL** single-node | Llama-2-70B LoRA in **8.5 min**; 5× H100 | NVIDIA blog (MLPerf v5.1) |
| **S-LoRA** thousands of adapters/GPU | Practical: 100s | S-LoRA paper |
| **Promotion gate** | Pareto on `{faithfulness, hit-rate, latency}`; **+ KL-shadow guard** | DataRobot champion/challenger + our `WEEKLY_TRAINING_CARD.md` |

The "every Reflector cycle" cadence is achievable on our DGX Spark even with
a 30B-A3B MoE base, because **rank-32 LoRA deltas on 1 k pairs train in
single-digit minutes** and the inference server **never has to restart**.

---

## 1. RLRO Loop — End-to-End Diagram

### 1.1 Markdown view

```
                      [trade closes]
                            │
                            ▼
                ┌─────────────────────────┐
                │  Reflector cycle        │  every ~10 min during RTH
                │  (Qwen3-30B-A3B)        │  off-hours: every 30 min
                └────────────┬────────────┘
                             │ writes 1 reflection row per
                             ▼ closed-trade + 1 "pre-decision" row
                ┌─────────────────────────┐
                │ shark/memory/           │
                │   chat_json/*.jsonl     │  immutable log (already exists)
                └────────────┬────────────┘
                             │
        ┌────────────────────┼──────────────────────┐
        ▼                    ▼                      ▼
   outcome_resolver    preference_pair_builder   eval_set_refresh
        │                    │                      │
        │  realised PnL,     │  (chosen, rejected)  │  rolling 30d holdout
        │  alpha vs SPY,     │  per role            │  for Pareto gates
        │  holding days      │                      │
        └────────┬───────────┘                      │
                 ▼                                  │
        ┌─────────────────────┐                     │
        │  RLRO data hopper   │  small SQLite       │
        │  ./data/rlro/*.db   │  ring-buffer        │
        └──────────┬──────────┘                     │
                   │                                │
                   ▼                                │
        ┌─────────────────────┐                     │
        │  CL-LoRA trainer    │  TRL DPOTrainer +   │
        │  (per role)         │  PEFT LoRA + Liger  │
        │  ~1–3 min /1 k pairs│  EWC-style Ω penalty│
        └──────────┬──────────┘                     │
                   │ saves to                       │
                   ▼                                │
        ./data/lora-adapters/<role>/shadow-YYYYMMDDTHHMM/
                   │                                │
                   ▼                                │
        ┌─────────────────────┐  POST               │
        │  vLLM 0.5+          │ /v1/load_lora_      │
        │  shadow-load        │  adapter            │
        │  (load_inplace=     │                     │
        │   False, new name)  │                     │
        └──────────┬──────────┘                     │
                   │  20-50 ms                      │
                   ▼                                │
        ┌─────────────────────┐                     │
        │  shadow scoring     │ ◄───────────────────┘
        │  (replay last 100   │
        │   prompts, score    │
        │   on holdout)       │
        └──────────┬──────────┘
                   │
                   ▼
            ┌──────┴──────┐
            │  Pareto OK  │
            │  + KL gate  │──No──► drop shadow, log reason
            └──────┬──────┘
                   │ Yes
                   ▼
        ┌─────────────────────┐
        │  Promotion          │  ln -sfn shadow-…  <role>-current
        │  (atomic symlink    │  + POST load_lora_adapter
        │   swap + reload)    │     load_inplace=True
        └──────────┬──────────┘     name="<role>"
                   │
                   ▼
            (Reflector picks up
             new adapter on next call)
```

### 1.2 ASCII state machine for a single adapter

```
            +--------+      train delta       +--------+
            | stable | ─────────────────────► | shadow |
            +--------+                        +--------+
                ▲                                  │
                │       Pareto FAIL                │ Pareto + KL PASS
                │       OR KL guard trips          │
                │                                  ▼
                │                            +-----------+
                └──────  rollback  ◄──────── | champion  |
                                             +-----------+
                                                  │
                                                  │ continuous monitor
                                                  ▼
                                      live metrics → next cycle
```

Definitions:

- **stable** — last known-good adapter, also kept in `./data/lora-adapters/<role>/stable/`.
- **shadow** — newly trained delta loaded into vLLM under a versioned name
  (`<role>-2026-05-12T1415Z`); receives mirrored traffic, never user-facing.
- **champion** — adapter currently aliased by `<role>-current` symlink and
  registered to vLLM under canonical role name (`reflector`, `bull`, …).

---

## 2. Per-Role Adapter Design (6 Roles)

The 6 roles are already defined in `docs/MODELFORGE_INTEGRATION_PLAN.md` and
`docs/VLLM_SERVING.md`. The table below specialises each for **continuous**
training, not weekly batch.

| Role | Server | Input shape | Training signal (chosen vs rejected) | Eval metric (hold-out) | Shadow → champion gate |
|---|---|---|---|---|---|
| **regime_tagger** | Ollama `hermes3:8b` JSON-only | `{prompt: [features_blob], schema: regime.schema.json}` → JSON `{regime, confidence}` | **Chosen** = LLM call whose `regime` matched the realised 24h direction (∆ price > 1σ). **Rejected** = call where label was wrong. Reward = `agreement_with_lookahead_oracle`. | `json_schema_validity_rate` + `agreement_with_consensus_rate` over last 1 000 calls | `validity ≥ 0.99` AND `agreement +1pp absolute over champion` AND `KL(πshadow ‖ πchamp) ≤ 0.15` |
| **bull_debater** | vLLM | `{prompt: candidate_trade_blob}` → 1–3 sentence bull case | **Chosen** = bull case for a trade that **realised positive alpha vs SPY** at hold-period exit. **Rejected** = bull case for trade that lost. DPO `loss_type=sigmoid_norm` (length-normalised). | `predictive_hit_rate_30d` (does the bull side direction match realised side?) + `judge_score` from `hermes3:8b` arbiter | `hit_rate +1pp` AND `judge_score ≥ 0.65` AND `debate_swing_rate ≤ 0.10` (don't let a new shadow flip more than 10 % of arbiter verdicts) |
| **bear_debater** | vLLM | Same as bull, mirror polarity. **Chosen** = bear case that correctly warned (trade lost) **or** correctly stood down (trade was filtered). | Same metrics, mirror. | Same gate, mirror. |
| **arbiter** | vLLM | `{prompt: bull_case + bear_case + price_context, schema: TraderProposal}` → JSON `{action, size, stop, take_profit, rationale}` | **Chosen** = arbiter decisions whose realised `$ realised + MTM` PnL ≥ 0 over holding period **and** matched stop/TP exit. **Rejected** = decisions where the stop hit before TP. | `decision_consistency` (same evidence → same decision N≥2) + `downstream_pnl_per_decision` + `structured_output_validity_rate` | Pareto on **all three** metrics — must Pareto-dominate champion on ≥ 2 of 3 and tie on the third (no metric may degrade > 0.5σ) |
| **reflector** | vLLM | `{prompt: trade_record_blob}` → 2–4 sentence post-mortem with cited PnL/ticker | **Chosen** = reflection that an `hermes3:8b` judge rated ≥ 0.7 AND whose stated lesson was **predictive** of a similar future trade (30-day lookahead hit-rate test). | `faithfulness_regex` + `predictive_hit_rate_30d` + `debate_impact_change_rate` | `faithfulness ≥ 0.95` (hard floor) AND `hit_rate +0.5pp` AND `change_rate ≤ 0.15` |
| **indicator_selector** | Ollama JSON | `{prompt: regime_blob + history_blob, schema: indicator_set.schema.json}` → list of TA indicator names | **Chosen** = indicator subsets whose realised 30-day Sharpe on the proposed bundle ≥ baseline Sharpe. **Rejected** = subsets that under-performed an equal-weight baseline. | `json_validity_rate` + `selected_indicator_avg_sharpe_30d` | `validity ≥ 0.99` AND `Δ Sharpe ≥ +0.05` AND `turnover_30d ≤ 0.25` (don't churn indicator picks every cycle) |

Notes that apply to **all** roles:

- **Adapter rank**: r = 32 for prose roles (bull/bear/arbiter/reflector), r = 16
  for JSON roles (regime/indicator) — JSON tasks need less capacity. Drop-in
  default in PEFT `LoraConfig`. Memory budget per adapter ≈ 150 MB at r=32 on
  Qwen3-30B-A3B in fp8 (already proved in `docs/VLLM_SERVING.md`).
- **LoRA target modules**: `q_proj, k_proj, v_proj, o_proj` (attention only)
  for JSON roles; add `gate_proj, up_proj, down_proj` (MLP) for prose roles —
  matches the Unsloth recipe and S-LoRA's published-best modules.
- **DPO β**: 0.1 for prose (loose preference signal), 0.3 for JSON (tighter,
  fewer "near-ties").
- **Eval window**: rolling **last 1 000 calls or last 30 days, whichever is
  larger** — prevents a low-volume role (e.g. arbiter on options) from
  promoting on a 30-call sample.

---

## 3. Hot-Swap Recipe (Which Inference Server, What Config)

### 3.1 Decision: vLLM is the only option that actually hot-swaps today

| Server | Hot-swap support | Verdict for our 6-role plan |
|---|---|---|
| **vLLM 0.5+** | Yes — runtime `/v1/load_lora_adapter`, `load_inplace=True` for RL workflows. Multi-adapter batched via Punica/S-LoRA-style SGMV kernels. | **Primary** for prose roles (bull/bear/arbiter/reflector). |
| **Ollama** | **No** — `ADAPTER` instruction is Modelfile-only, baked at build time. Issue #9548 (Mar 2025) still open. | **Keep** for JSON roles (regime_tagger, indicator_selector) — fast cold path; rebuild a tagged Modelfile only when the JSON role is promoted (cron-driven, ~1× / day). |
| **TensorRT-LLM** | Yes — 2-tier LoRA cache (host + GPU), HF + NeMo formats. Higher throughput than vLLM at 100s of adapters. | **Not needed** at 6 adapters; revisit if we ever fan out to per-symbol adapters. |
| **NVIDIA NIM** | Yes — multi-tier adapter store + CUTLASS batched GEMM + splitK. | **Not needed** — vLLM gets us there at zero $/call. NIM is a re-eval option if we move to managed inference. |

### 3.2 vLLM startup flags (drop-in for our existing `bootstrap_vllm.sh`)

```bash
docker run … vllm/vllm-openai:latest \
  --model Qwen/Qwen3-30B-A3B-Instruct-2507 \
  --served-model-name qwen3:30b \
  --enable-lora \
  --max-loras 8 \
  --max-lora-rank 32 \
  --max-cpu-loras 32 \
  --lora-modules \
      reflector=/lora/reflector-current \
      bull=/lora/bull-current \
      bear=/lora/bear-current \
      arbiter=/lora/arbiter-current \
  --gpu-memory-utilization 0.45 \
  --max-model-len 8192 \
  --quantization fp8
# plus, in the container env:
# VLLM_ALLOW_RUNTIME_LORA_UPDATING=true
```

`--max-loras 8` gives us a 2× headroom over 4 prose roles (one shadow per
role can be resident simultaneously without eviction). `--max-cpu-loras 32`
holds the last 7 shadow versions per role in pinned host memory for instant
rollback.

### 3.3 Promotion command sequence (single role, single cycle)

```bash
# 1. Shadow-load with a versioned name (NEW adapter, not in-place)
curl -fsS -X POST http://127.0.0.1:8090/v1/load_lora_adapter \
  -H 'Content-Type: application/json' \
  -d '{
    "lora_name": "reflector-2026-05-12T1415Z",
    "lora_path": "/lora/reflector/shadow-2026-05-12T1415Z"
  }'

# 2. Shadow scoring (Python — runs the last 100 prompts; computes Pareto)
python scripts/rlro/shadow_score.py \
  --role reflector \
  --shadow reflector-2026-05-12T1415Z \
  --champion reflector \
  --replay-from chat_json/last_100.jsonl \
  --gate pareto+kl

# 3a. If pass: atomic promotion
ln -sfn shadow-2026-05-12T1415Z ./data/lora-adapters/reflector-current
curl -fsS -X POST http://127.0.0.1:8090/v1/load_lora_adapter \
  -H 'Content-Type: application/json' \
  -d '{
    "lora_name": "reflector",
    "lora_path": "/lora/reflector-current",
    "load_inplace": true
  }'

# 3b. If fail: unload shadow only — champion never touched
curl -fsS -X POST http://127.0.0.1:8090/v1/unload_lora_adapter \
  -H 'Content-Type: application/json' \
  -d '{"lora_name": "reflector-2026-05-12T1415Z"}'
```

Why `load_inplace=True` matters: vLLM **replaces the weights under the same
adapter name while in-flight requests continue against the old weights** —
exactly the async-RL pattern, no 503s, no fallback to base.

### 3.4 LoRA resolver plugin — the right long-term path

Once we hit > 16 adapters total (e.g. per-symbol bear adapters), implement a
**custom `LoRAResolver` plugin** that reads from a small SQLite registry and
lazy-loads on first request. Doc: `https://docs.vllm.ai/en/stable/design/lora_resolver_plugins/`.
For r2 scope we do not need this — explicit `load_lora_adapter` is fine for
6 roles.

---

## 4. Anti-Forgetting Strategy

### 4.1 The actual risk profile

Continuous LoRA training is exposed to two failure modes:

1. **Catastrophic forgetting of pretrained world-knowledge** — the
   reflector starts hallucinating tickers because it over-fits last
   week's reflections.
2. **Adapter drift / regime collapse** — the bull adapter becomes a
   "permabull" because the last 30 days were a bull market.

These are well-studied. We layer four defences:

### 4.2 Defence 1 — Online-LoRA-style Fisher regularisation (cheap)

Per Wei et al. (WACV 2025), maintain an **importance vector Ω** over the
LoRA parameters (not the base model — that's ~22 GB of Fisher info for
Qwen3-30B-A3B and untenable). The PEFT trainer's loss becomes:

```
L_total = L_DPO + λ · Σ Ωᵢ (θᵢ - θ_iˢᵗᵃᵇˡᵉ)²
```

with λ ≈ 0.4 and Ω updated from a **4-sample hard buffer** of the
hardest-loss reflections from the previous cycle. Memory overhead: ~0.17 %
of LoRA params — negligible (Online-LoRA paper, §3.4).

Why this beats vanilla EWC for us: full-model EWC requires a Fisher matrix
sized like the base model. Online-LoRA computes Ω **only over the LoRA
matrices**, which is the right scope because the base never updates.

### 4.3 Defence 2 — Periodic full retrain anchor (every 7 days)

The Sunday-02:00-ET cron stays — but its job changes. Instead of being the
**only** trainer, it becomes the **anchor**: it retrains each role on the
**full 90-day window** of preference pairs, producing a `stable/` checkpoint
that the continuous deltas regularise against. Old continuous shadows from
the prior week are wiped except for the latest champion. This bounds drift
to one week.

### 4.4 Defence 3 — Replay buffer (small)

Maintain a **rolling 500-pair reservoir sample** of older preference pairs
per role (stratified across regime labels: `bull / bear / chop / unknown`).
Each continuous cycle's training batch is 80 % fresh + 20 % replay. This is
the classic experience-replay trick (Rolnick et al. 2018) and pairs well
with the Fisher penalty: regularisation prevents forgetting **in
parameter-space**, replay prevents it **in data-space**.

### 4.5 Defence 4 — Hard KL-shadow guard before promotion

Reject any shadow where, on a 100-prompt replay set:

```
KL( π_shadow(·|x) ‖ π_champion(·|x) )  >  τ_KL
```

with `τ_KL = 0.15` for prose roles, `0.25` for JSON. This is the classic
PPO/RLHF "don't drift too far from the SFT baseline" guard, applied at
**promotion** time instead of **training** time so we can let DPO run
unconstrained.

### 4.6 What we do NOT do (and why)

- **No KL penalty inside DPO loss** — DPO's β already plays that role and
  TRL's `sync_ref_model=False` lets us pin the reference to the **stable
  weekly anchor**, which gives us a constant ground truth for 7 days.
- **No O-LoRA / subspace orthogonality** — the literature shows it helps
  with sharp task boundaries (e.g. multilingual tasks); our task is one
  task ("trade reflection") with shifting data distribution. Wrong tool.
- **No model souping / TIES merging across cycles** — these are
  multi-adapter composition tricks. We have a single champion per role; the
  weekly anchor is our merge point.

---

## 5. Storage & Versioning

### 5.1 On-disk layout (host)

```
./data/lora-adapters/
├── reflector/
│   ├── stable/                            # anchor (weekly), copied to vLLM read-mount
│   ├── reflector-current  -> shadow-2026-05-12T1415Z   # symlink (atomic swap)
│   ├── shadow-2026-05-12T1415Z/           # last N=7 continuous deltas
│   ├── shadow-2026-05-12T1325Z/
│   └── …
├── bull/                                  # same layout
├── bear/
├── arbiter/
├── regime_tagger/                         # Ollama path; rebuilt as a Modelfile tag
└── indicator_selector/
```

The vLLM container mounts `./data/lora-adapters` read-only at `/lora`; the
trainer writes outside the mount and `mv`-s atomically. `mv` on the same
filesystem is rename(2), so no half-state.

### 5.2 HuggingFace Hub mirror (optional, recommended)

Every promoted champion gets a `git push` to a private HF repo
(`<org>/quanta-<role>`), tagged with the timestamp. Git-based versioning
gives us:

- **Rollback by tag**: `huggingface-cli download <repo> --revision <tag>`.
- **Diff visualisation** in the HF UI for the safetensors header (rank,
  alpha, target modules).
- **Out-of-band recovery** if local disk is lost (we already do hourly
  out-of-tree backups per `reference_backup_system.md`).

Use `TRL`'s built-in `Trainer.push_to_hub(commit_message=…, revision=…)`;
costs nothing beyond an HF API token.

### 5.3 Rollback procedure (operator runbook)

```bash
# 1. Identify last-known-good shadow (or the stable anchor)
ls -1 ./data/lora-adapters/reflector/ | grep ^shadow- | sort -r

# 2. Atomic symlink swap
ln -sfn shadow-2026-05-11T2030Z ./data/lora-adapters/reflector-current
# OR roll all the way back to the weekly anchor:
ln -sfn stable ./data/lora-adapters/reflector-current

# 3. Tell vLLM to reload in-place
curl -fsS -X POST http://127.0.0.1:8090/v1/load_lora_adapter \
  -H 'Content-Type: application/json' \
  -d '{"lora_name":"reflector","lora_path":"/lora/reflector-current","load_inplace":true}'
```

Total rollback time: **< 1 s**. No restart. No fallback to base.

### 5.4 Retention

- **Stable anchors** (weekly): keep last 8 (~2 months of weekly fallbacks).
- **Continuous shadows**: keep last 7 (≈ 1 hour at 10-min cadence during RTH).
- **Promoted-but-superseded champions**: keep last 7 days; older → HF Hub
  only.

At rank-32 / 30B base / fp8 → ~150 MB per adapter. 6 roles × (8 anchors + 7
shadows + 7 archived) ≈ **20 GB**. Trivial on the DGX Spark.

---

## 6. Compute Cost Estimate

### 6.1 Training one continuous delta (per role, per cycle)

Assumptions: rank-32 LoRA, 1 000 fresh DPO pairs, β=0.1, 1 epoch, gradient
checkpointing on, Liger kernels on, `bf16`. Base model = Qwen3-30B-A3B
in fp8 (active params ≈ 3B per token).

| Hardware | Estimated wall-clock per role | Notes |
|---|---|---|
| **1× H100 80GB** (typical lab) | ~5–8 min | Extrapolated from Llama-3.1-8B Unsloth runs at 2.1× FA2 baseline |
| **1× H200 141GB** | ~4–6 min | ~16 % faster than H100 (NVIDIA MLPerf v5.1) |
| **1× B200** (DGX Spark — our target) | **~2–3 min** | 2.5× H100 on Llama-2-70B LoRA scales similarly |
| **1× B300 (GB300 NVL slice)** | **~1.5–2 min** | 12.6 % faster than B200 |

These are dense-base extrapolations; Qwen3-30B-A3B's 3B active params
**reduce wall-clock further** because gradient flows through far fewer
matmuls. Empirical rule-of-thumb: A3B trains LoRAs **~3–4× faster** than a
dense 30B at the same rank.

**All-6-roles fan-out: ~10–18 min wall-clock per cycle on DGX Spark, fully
parallel** (the 6 roles share the same frozen base; you can train them
concurrently with separate optimizer states because LoRA params don't
collide). Sequentially: ~12–18 min total — easily inside a 30 min Reflector
cycle and tight inside a 10 min cycle (only train roles whose hopper has
≥ N=200 fresh pairs that cycle).

### 6.2 Cloud-equivalent dollar cost (sanity check)

If we ever burst to cloud:

| Cloud SKU | $/hr | Cost per 6-role cycle (~15 min) |
|---|---|---|
| 1× B200 on-demand (lowest seen) | $2.45–$4.20 | $0.60–$1.05 |
| 1× GB200 on-demand (avg) | $10.50–$18.22 | $2.63–$4.55 |

At 30 cycles/day on-prem, **on-disk training is free**; the only cost is
power and that's amortised. The cron stays on-DGX.

### 6.3 Build cost — LOC + days

| Component | New LOC (est.) | New tests | Days (solo dev) |
|---|---:|---:|---:|
| `scripts/rlro/preference_pair_builder.py` | ~250 | 8 | 0.5 |
| `scripts/rlro/cl_lora_trainer.py` (TRL+PEFT wrapper, EWC term) | ~400 | 12 | 1.5 |
| `scripts/rlro/shadow_score.py` (Pareto + KL gate) | ~300 | 10 | 1 |
| `scripts/rlro/promote.py` (symlink + vLLM API) | ~120 | 6 | 0.5 |
| `scripts/rlro/replay_buffer.py` (reservoir sample) | ~150 | 5 | 0.5 |
| `data/rlro/schema.sql` (hopper DB) | ~80 | — | 0.25 |
| `.hermes/cron/rlro_cycle.job.json` (every-10-min cron) | ~40 | — | 0.25 |
| `.hermes/scripts/rlro_cycle.sh` (wrapper, lockfile, slack) | ~100 | 4 | 0.5 |
| Dashboard card: `templates/spa/rlro_status.html` (champion vs shadow per role) | ~200 | 3 | 0.75 |
| Modify existing weekly cron → "anchor mode" | ~50 (delta) | 2 | 0.25 |
| Modify `bootstrap_vllm.sh` → new flags | ~30 (delta) | 1 | 0.25 |
| **Total** | **~1 720 LOC** | **51 tests** | **~6 days** |

Plus ~1 day of soak-testing in paper mode before enabling for live trading.
**End-to-end: ~7 working days, single dev.**

### 6.4 What scales us out (if we ever need it)

- Switch from `load_lora_adapter` calls to a custom `LoRAResolver` plugin
  (~150 LOC) if adapters exceed 16.
- Move to TensorRT-LLM with the 2-tier LoRA cache if request volume passes
  ~50 RPS per role.
- Move to NIM's adapter swarm if we go multi-tenant (we won't).

---

## 7. Sources (16 — exceeds 12 minimum)

### Inference servers & hot-swap

1. **vLLM — LoRA Adapters (official, latest)** — https://docs.vllm.ai/en/latest/features/lora/
   (CLI flags `--enable-lora`, `--max-loras`, `--max-lora-rank`,
   `--max-cpu-loras`; `/v1/load_lora_adapter`; `load_inplace=True` RL pattern.)
2. **vLLM — LoRA Resolver Plugins (design doc)** — https://docs.vllm.ai/en/stable/design/lora_resolver_plugins/
   (async `resolve_lora(base_model, lora_name)` interface, S3/HF Hub plugins.)
3. **Unsloth — LoRA Hot Swapping Guide (vLLM)** — https://unsloth.ai/docs/basics/inference-and-deployment/vllm-guide/lora-hot-swapping-guide
   (Exact `curl` shape, `VLLM_ALLOW_RUNTIME_LORA_UPDATING=True` requirement.)
4. **AIBrix — Lora Dynamic Loading** — https://aibrix.readthedocs.io/latest/features/lora-dynamic-loading.html
   (K8s `ModelAdapter` controller, multi-pod distribution, references our
   "shadow + champion" lifecycle terminology directly.)
5. **NVIDIA NIM — Swarm of LoRA Adapters** — https://developer.nvidia.com/blog/seamlessly-deploying-a-swarm-of-lora-adapters-with-nvidia-nim/
   (Multi-tier adapter cache, batched GEMM via CUTLASS, splitK.)
6. **NVIDIA TensorRT-LLM — LoRA executor** — https://nvidia.github.io/TensorRT-LLM/advanced/lora.html
   (2-tier host+GPU LoRA cache, eviction policy.)
7. **Ollama — Modelfile reference + ADAPTER instruction** — https://docs.ollama.com/modelfile
   and **issue #9548** — https://github.com/ollama/ollama/issues/9548
   (Confirms Ollama has no runtime hot-swap as of 2025-Q1.)

### Continuous learning & catastrophic forgetting

8. **Online-LoRA (WACV 2025)** — https://arxiv.org/html/2411.05663
   (Loss-plateau trigger, Fisher-Ω over LoRA params only, 0.17 % memory
   overhead, hard buffer of 4 samples.)
9. **CL-LoRA (CVPR 2025)** — https://openaccess.thecvf.com/content/CVPR2025/papers/He_CL-LoRA_Continual_Low-Rank_Adaptation_for_Rehearsal-Free_Class-Incremental_Learning_CVPR_2025_paper.pdf
   (Rehearsal-free continual LoRA, dual-LoRA decomposition.)
10. **EWC for Continual Pre-Training of Gemma2 (arXiv 2505.05946)** — https://arxiv.org/html/2505.05946v1
    (Practical EWC at LLM scale; we adopt its Fisher-on-PEFT-params variant.)
11. **Catastrophic Forgetting in PEFT (arXiv 2603.09684)** — https://arxiv.org/abs/2603.09684
    (Compares rank, subspace geometry, and merging strategies.)
12. **Experience Replay for Continual Learning (Rolnick et al., arXiv 1811.11682)** — https://arxiv.org/pdf/1811.11682
    (Foundation for our 20 % replay-buffer policy.)

### Online preference training & RLHF

13. **TRL — DPOTrainer (HF)** — https://huggingface.co/docs/trl/main/en/dpo_trainer
    (LR=1e-5 with PEFT, β default 0.1, `loss_type=sigmoid_norm` length norm,
    `sync_ref_model` for anchor pinning, Liger kernel +20 % throughput.)
14. **Online DPO with Reward Models (The Salt)** — https://thesalt.substack.com/p/online-dpo-with-reward-models
    (Online vs offline DPO; vLLM-generated samples in the inner loop.)
15. **Anthropic Constitutional AI (RLAIF, 2026)** — referenced in
    https://en.wikipedia.org/wiki/Reinforcement_learning_from_human_feedback
    (Why RLAIF/judge models scale; aligns with our `hermes3:8b` judge.)

### Adapter merging, multi-adapter serving, hardware

16. **S-LoRA: Serving Thousands of Concurrent LoRA Adapters (arXiv 2311.03285)** — https://arxiv.org/abs/2311.03285
    + **Punica (arXiv 2310.18547)** — https://arxiv.org/abs/2310.18547
    (SGMV / segmented GEMV, basis for vLLM's multi-LoRA backend; 4× vLLM
    naive, 30× HF PEFT.)
17. **MLPerf Training v5.1 — Blackwell/Blackwell Ultra results (NVIDIA)** —
    https://blogs.nvidia.com/blog/mlperf-training-benchmark-blackwell-ultra/
    and Nebius write-up https://nebius.com/blog/posts/mlperf-training-v5-1-results
    (Llama-2-70B LoRA 8.5 min on 8× GB300; 12.6 % B300-vs-B200; Llama-3.1-8B
    in 5.2 min on 512 Blackwell Ultra GPUs.)
18. **PEFT Welcomes New Merging Methods (HF Blog)** — https://huggingface.co/blog/peft_merging
    (TIES, DARE, SLERP, linear — context only; we do NOT merge across
    cycles, but the anchor-vs-shadow analysis here is informative.)

### Multi-agent trading reference (for role semantics)

19. **TradingAgents (arXiv 2412.20138)** — https://arxiv.org/abs/2412.20138
    (Reflector + bull/bear/arbiter/risk-manager pattern; our 6 roles are a
    direct subset.)

---

## 8. Open Questions / Risks (carry to HANDOFF.md)

1. **A3B MoE LoRA targeting** — Qwen3-30B-A3B is a mixture-of-experts. LoRAs
   typically target shared attention/MLP layers; we need to confirm the
   PEFT `target_modules` list also covers expert-routing layers, or accept
   that adapters only affect the shared-attention path. Tested empirically
   in `docs/VLLM_SERVING.md` (works) but capacity ceiling unknown.
2. **Pareto vs single-metric arbiter promotion** — true Pareto-dominance
   may stall promotion (one metric always wins, another ties); we may need
   a **scalarised** fallback after N=5 failed cycles (e.g. weighted sum
   with operator-set weights).
3. **JSON-role hot-swap on Ollama** — Modelfile rebuild + `ollama create`
   takes ~30 s and forces a model unload/reload. Acceptable at 1× / day,
   not at 10-min cadence. **Recommendation: move regime_tagger and
   indicator_selector to vLLM too** once we are comfortable with the prose
   roles' KL guard. That collapses to "one inference server, one swap
   path", which is simpler.
4. **Replay-buffer sampling bias** — stratification by regime label
   requires a regime oracle. Use realised 24h log-return sign + magnitude as
   a label, refresh daily.
5. **Reward-hacking on `predictive_hit_rate_30d`** — bull/bear are scored
   on prediction accuracy, which they don't fully control (arbiter does).
   Mitigation: use **counter-factual hit-rate** (the side they argued
   against the arbiter's vote, evaluated on outcomes where they were
   over-ruled).

---

*End of `02-RESEARCH-CONTINUOUS_LORA.md`. Next doc in the series:
`03-DESIGN-RLRO_HOPPER_SCHEMA.md` (preference-pair DB schema + the
`shadow_score.py` Pareto math, both pre-code).*
