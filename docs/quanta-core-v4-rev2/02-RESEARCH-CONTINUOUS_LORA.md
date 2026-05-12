# 02 — Research: Weekly LoRA Training (RLRO — Reflector-Linked, Roll-up Optimisation)

> Branch: `feat/quanta-core-v4-rev2-r2` · Status: design research only, no
> code changes · Companion: `01-RESEARCH-MULTI_MODEL_RESIDENCY.md` (sibling
> — Ollama-only confirmed), `11-HERMES_AGENT.md` (cron details — defer to it).
>
> **This doc supersedes** `docs/quanta-core-v4/02-RESEARCH-CONTINUOUS_LORA.md`
> per the validator-flagged cadence disagreement (P1-3) and the operator
> philosophy decisions captured in `project-drop-vllm` (2026-05-12) and
> `project-modelforge-decisions` (2026-05-12).

---

## 0. TL;DR — Executive Recommendation

Replace the prior "every 10-min continuous LoRA" loop with a **weekly LoRA
training cycle** that aligns with the existing GPU reservation
(Sunday 14:00 ET, 4 h window).  Drive the training set from a **nightly
Reflector** that captures lessons into `stocks/memory/decisions.md`.  Serve
adapters through **Ollama only** — promotion is `ollama create
qwen3:30b-<role>-vYYYY-WW -f Modelfile` (the `Modelfile` carries an
`ADAPTER /path/to/adapter.gguf` directive) followed by a daily-rebuild
cron that flips the `qwen3:30b-<role>-current` alias.  No vLLM, no
runtime hot-swap, no `load_inplace=True`.

Concrete numbers (cite §10 sources):

| Quantity | Value | Source |
|---|---|---|
| **Weekly training set** (per role, realistic for a 2-3 trade/week bot) | **10-15 preference pairs** | Operator volume: 2-3 trades/week × 6 roles → small-data regime |
| **PEFT + TRL DPOTrainer** (LR ≈ 1e-5, β = 0.1) | ~5-10 min on hermes3:70b Q4 base | TRL DPOTrainer docs, scaled down for small-data |
| **6-role fan-out** (sequential, gpu_yield reserves the window) | **~30-60 min wall-clock per Sunday** | Same trainer, 6 sequential runs |
| **Ollama adapter promotion** (Modelfile rebuild + alias flip) | ~5-15 s per role (GGUF page-in) | `project-drop-vllm` §5 (operator measured) |
| **Anchor cadence** | **Weekly = the only cadence**; the anchor IS the cycle | Operator decision: simpler than continuous |
| **KL gate** | **One-shot at promotion**, per role, on a stratified replay set | `project-modelforge-decisions` §3 |
| **Rollback** | `ollama cp qwen3:30b-<role>-w<prev>` → `qwen3:30b-<role>-current` | Single-command rebuild from prior-week tag |

Why this is **better than the prior continuous design** for this operator:

1. **Trade volume is the binding constraint, not training compute.**  At
   2-3 trades/week total, no role accumulates more than ~10-15 preference
   pairs in a 7-day window — and on weeks with fewer trades, fewer pairs.
   Running DPO every 10 min on 0-2 fresh pairs is statistically
   meaningless (Online-LoRA / SLT regime requires N ≥ ~50 to escape pure
   noise per arxiv 2411.05663 §4).
2. **Regimes change weekly, not every 10 min.**  Macro regime
   (`bull / chop / bear / unknown`) flips on the timescale of FOMC
   meetings, earnings season, BTC ETF flows — never inside a 10-min bar.
   Training cadence should match the timescale of the signal.
3. **Operator philosophy preserved.**  No vLLM, no hot-swap, no novel
   inference API.  One cron, aligned to one GPU window, one promotion
   path through `ollama create`.

---

## 1. Why weekly, not continuous

This section is the **decision record** for the cadence change versus the
prior doc, in case a future agent re-opens the question.

### 1.1 The math: small-data regime

| Quantity | Value | Implication |
|---|---|---|
| Total trades/week (operator's current pace) | 2-3 | Tiny |
| Roles consuming each trade for preference pairs | 4 (bull, bear, arbiter, reflector) — regime_tagger + indicator_selector are session-level, not trade-level | 8-12 prose pairs/week |
| Regime/indicator decisions per week | ~5-15 (one per session, multiple regime checks per RTH day) | 10-15 JSON pairs/week |
| **Per-role training set after 7 days** | **~10-15 pairs** | Small-data |
| DPO statistical floor (effective sample size, β=0.1, p < 0.1) | ~50 pairs | One week's data **alone** is below the floor |
| Mitigation | Carry 90 d of replay (~80-130 pairs) in the trainer's reservoir; weekly fresh data is the gradient signal, replay is the anchor | Standard small-data DPO recipe |

The prior "continuous" doc assumed 1 000 pairs per cycle.  That number
is **3 orders of magnitude** off our actual volume.  The operator is not
a hedge fund running 10 k decisions/day — he is a solo dev with $19 k
of paper capital and a wheel pilot worth $629 in premium.  The training
plan must match.

### 1.2 The cadence: aligns with the GPU lease

`~/.hermes/config/gpu_reservation.yaml` already books **Sunday 14:00 ET
(4 h)** under the holder `modelforge-weekly-lora-training`.  The window
exists, the gate (`gpu_gate.sh`) already evicts heavy consumers, and the
operator has accepted this rhythm.  Continuous training would either:

- Collide with that window weekly (4 h dead-time per `gpu_gate.sh`
  "log + wait"); or
- Require a second GPU lease, which contradicts the single-source-of-truth
  schedule.

Weekly LoRA **uses** the lease; continuous LoRA **fights** the lease.

### 1.3 The risk: over-fitting on noise

DPO with too few preference pairs is dominated by reward-hacking on
noise.  Each weekly cohort of 10-15 pairs is mixed 20 % fresh / 80 %
replay (Rolnick 2018 reservoir).  This bounds the per-week gradient
update and gives the model 7 days of out-of-sample evidence before its
next nudge — well-aligned with the operator-locked **weekly hit-rate
gate** (`project-modelforge-decisions` §3).

Continuous training amplifies the noise.  Weekly damps it.

---

## 2. RLRO Loop — Revised End-to-End

### 2.1 Two cadences, clearly separated

The system has **two** distinct loops, deliberately decoupled:

1. **Nightly Reflector loop** — captures **lessons** from every closed
   trade and writes them to `stocks/memory/decisions.md` (and the
   structured shadow `chat_json/*.jsonl`).  This is the **lesson
   capture** cycle.  Runs Mon-Sat (Sunday is the training day).
2. **Sunday weekly LoRA loop** — at 14:00 ET, gpu_yield reserves the
   GPU, the trainer reads accumulated lessons + the replay reservoir,
   trains all 6 roles, Pareto-promotes each adapter, rebuilds Modelfiles,
   and reloads Ollama.  This is the **promotion** cycle.

### 2.2 Markdown view

```
                      [trade closes]
                            │
                            ▼
              ┌─────────────────────────────┐
              │ Nightly Reflector cron      │  one pass / day, ~23:30 ET
              │   model: qwen3:30b-reflector│  reads closed trades, options
              │   -current (Ollama)         │  fills, paper-mode P&L
              └────────────┬────────────────┘
                           │ writes 1 lesson row per closed trade +
                           │ 1 "session post-mortem" row
                           ▼
              ┌─────────────────────────────┐
              │ stocks/memory/decisions.md  │   append-only, human-readable
              │ + shark/memory/             │   immutable mirror (machine-parseable)
              │     chat_json/*.jsonl       │
              └────────────┬────────────────┘
                           │
                           │   ── Mon ── Tue ── Wed ── Thu ── Fri ── Sat ──
                           │   lessons accumulate (~5-10 entries/week)
                           ▼
                  ┌─────────────────────────┐
                  │ Sunday 14:00 ET cron    │  schedule_cron: "0 14 * * 0"
                  │   gpu_gate.sh yields    │  tz: America/New_York
                  │   → 4 h GPU lease       │  pre_drain: 5 min · grace: 30 min
                  └────────────┬────────────┘
                               │
            ┌──────────────────┼──────────────────────────┐
            ▼                  ▼                          ▼
   outcome_resolver    preference_pair_builder       eval_set_refresh
        │                     │                          │
        │  realised PnL,      │  (chosen, rejected)      │  rolling 30d
        │  alpha vs SPY,      │  per role; 20 % fresh,   │  out-of-sample
        │  holding days       │  80 % from reservoir     │  holdout
        └────────┬────────────┘                          │
                 ▼                                        │
        ┌─────────────────────┐                           │
        │  RLRO reservoir DB  │  SQLite — last 90 d       │
        │  ./data/rlro/*.db   │  per role, stratified by  │
        │                     │  regime label             │
        └──────────┬──────────┘                           │
                   │                                      │
                   ▼ (6 sequential role trainings)        │
        ┌─────────────────────┐                           │
        │  Weekly LoRA trainer│  PEFT LoRA (r=32 prose,   │
        │  per role           │  r=16 JSON) + TRL         │
        │  ~5-10 min/role     │  DPOTrainer, Liger kernel │
        └──────────┬──────────┘                           │
                   │ saves to                             │
                   ▼                                      │
        ~/.dgx-train/adapters/<role>/wYYYY-WW/            │
        ├── adapter.safetensors                           │
        ├── adapter.gguf            (Unsloth export)      │
        └── Modelfile               (FROM qwen3:30b +     │
                                     ADAPTER ./*.gguf)    │
                   │                                      │
                   ▼                                      │
        ┌─────────────────────┐                           │
        │  Pareto + KL gate   │ ◄─────────────────────────┘
        │  (one-shot, on 100  │
        │  prompt eval set)   │
        └──────────┬──────────┘
                   │
            ┌──────┴──────┐
            │  pass?      │── No ──► keep prev week tag as current
            └──────┬──────┘            (rollback is the default)
                   │ Yes
                   ▼
        ┌─────────────────────────────────────┐
        │  ollama create                      │
        │    qwen3:30b-<role>-w2026-19        │
        │    -f Modelfile                     │
        │  → tag the new adapter              │
        │                                     │
        │  ollama cp                          │
        │    qwen3:30b-<role>-w2026-19        │
        │    qwen3:30b-<role>-current         │
        │  → atomic alias flip                │
        └──────────┬──────────────────────────┘
                   │
                   ▼
        (Trading bot's chat_json() picks
         up new adapter on next call —
         model name is unchanged: it's the
         "-current" alias that swung)
```

### 2.3 ASCII state machine for a single adapter

```
              +-----------+    nightly lessons    +-----------+
              | wPrev     |  ───── feed in ─────► | wCurrent  |
              | (last     |                       | (training |
              |  week's   |                       |  next)    |
              |  champ)   |                       +-----------+
              +-----------+                            │
                    ▲                                  │ Sunday 14:00 ET
                    │                                  │ train + Pareto/KL gate
                    │  promotion FAIL                  │
                    │  (one-shot KL > τ                │
                    │   OR Δhit-rate < 0)              ▼
                    │                            +-----------+
                    └────── keep wPrev  ◄──────  | wNew      |
                            as current           +-----------+
                                                      │ pass
                                                      ▼
                                              +-----------------+
                                              | wNew is promoted|
                                              | → Modelfile     |
                                              | → ollama create |
                                              | → alias flip    |
                                              +-----------------+
```

Note the difference from the prior doc's state machine: **there is no
"shadow" state separate from "champion"**.  At weekly cadence with one
training pass per role per week, the post-training adapter is either
promoted-to-current or discarded-in-place.  No mirrored traffic, no
parallel scoring, no continuous shadow population to manage.

### 2.4 Where Reflector lessons feed the prompt

Two consumers of `decisions.md`:

- **Next-day debate prompt** (online, daily): the Bull/Bear/Arbiter system
  prompt is **prefixed with the last 7 lessons** so the panel can "see"
  recent mistakes without retraining.  This is the **fast path** —
  prompt-time conditioning, no weights changed.
- **Weekly LoRA training set** (offline, Sunday): the trainer reads the
  **accumulated lessons since the previous Sunday** as part of the
  preference-pair construction.  This is the **slow path** — weights
  change.

The fast path gives the system a 1-day "memory horizon"; the slow path
consolidates it into the weights at week boundaries.  Same pattern as a
human trader: "remember today's loss tonight; internalise it over the
weekend."

---

## 3. Per-Role Adapter Design (6 Roles)

The 6-role taxonomy from prior doc §2 stands.  Only the **cadence**
bumps from per-Reflector-cycle to per-week.  Table preserved (with the
cadence column updated).

| Role | Server | Input shape | Training signal (chosen vs rejected) | Eval metric (hold-out) | Promotion gate (weekly) |
|---|---|---|---|---|---|
| **regime_tagger** | Ollama `qwen3:30b-regime_tagger-current` JSON-only | `{prompt: [features_blob], schema: regime.schema.json}` → JSON `{regime, confidence}` | **Chosen** = LLM call whose `regime` matched the realised 24 h direction (∆ price > 1σ). **Rejected** = call where label was wrong. Reward = `agreement_with_lookahead_oracle`. | `json_schema_validity_rate` + `agreement_with_consensus_rate` over last **30 d** | `validity ≥ 0.99` AND `agreement +1 pp absolute over prev-week champion` AND `KL(πnew ‖ πprev) ≤ 0.15` |
| **bull_debater** | Ollama `qwen3:30b-bull_debater-current` | `{prompt: candidate_trade_blob}` → 1-3 sentence bull case | **Chosen** = bull case for a trade that **realised positive alpha vs SPY** at hold-period exit. **Rejected** = bull case for trade that lost. DPO `loss_type=sigmoid_norm`. | `predictive_hit_rate_30d` + `judge_score` from `qwen3:30b-arbiter-current` | `hit_rate +1 pp` AND `judge_score ≥ 0.65` AND `debate_swing_rate ≤ 0.10` |
| **bear_debater** | Ollama `qwen3:30b-bear_debater-current` | Same as bull, mirror polarity. **Chosen** = bear case that correctly warned or correctly stood down. | Same metrics, mirror. | Same gate, mirror. |
| **arbiter** | Ollama `qwen3:30b-arbiter-current` | `{prompt: bull_case + bear_case + price_context, schema: TraderProposal}` → JSON `{action, size, stop, take_profit, rationale}` | **Chosen** = arbiter decisions whose realised `$ realised + MTM` PnL ≥ 0 **and** matched stop/TP exit.  **Rejected** = decisions where stop hit before TP. | `decision_consistency` + `downstream_pnl_per_decision` + `structured_output_validity_rate` | Pareto over all three — must Pareto-dominate prev week on ≥ 2 of 3 and tie on the third (no metric may degrade > 0.5σ) |
| **reflector** | Ollama `qwen3:30b-reflector-current` | `{prompt: trade_record_blob}` → 2-4 sentence post-mortem with cited PnL/ticker | **Chosen** = reflection that a `qwen3:30b-arbiter-current` judge rated ≥ 0.7 AND whose stated lesson was **predictive** of a similar future trade (30-d lookahead hit-rate test). | `faithfulness_regex` + `predictive_hit_rate_30d` + `debate_impact_change_rate` | `faithfulness ≥ 0.95` (hard floor) AND `hit_rate +0.5 pp` AND `change_rate ≤ 0.15` |
| **indicator_selector** | Ollama `qwen3:30b-indicator_selector-current` JSON | `{prompt: regime_blob + history_blob, schema: indicator_set.schema.json}` → list of TA indicator names | **Chosen** = indicator subsets whose realised 30 d Sharpe ≥ baseline. **Rejected** = subsets that under-performed equal-weight. | `json_validity_rate` + `selected_indicator_avg_sharpe_30d` | `validity ≥ 0.99` AND `Δ Sharpe ≥ +0.05` AND `turnover_30d ≤ 0.25` |

Notes that apply to **all** roles:

- **Adapter rank**: r = 32 for prose roles (bull/bear/arbiter/reflector),
  r = 16 for JSON roles (regime/indicator).  Unchanged from prior doc.
- **LoRA target modules**: `q_proj, k_proj, v_proj, o_proj` for JSON
  roles; add `gate_proj, up_proj, down_proj` for prose.  Unchanged.
- **DPO β**: 0.1 prose, 0.3 JSON.  Unchanged.
- **Eval window**: rolling **30 d** out-of-sample (operator-locked
  `project-modelforge-decisions` §3 — hit-rate is the unfakeable gate;
  see §5 below for the KL guard layered on top).
- **Inference target**: every role hits the same `qwen3:30b` base; the
  `<role>-current` alias is the only thing the trading bot sees.

---

## 4. Promotion Recipe — Ollama Modelfile + daily-rebuild cron

### 4.1 The 6-step recipe (per role, per Sunday)

This recipe is the operator's locked path from `project-drop-vllm` §7,
adapted for the weekly cadence.

```bash
# Step 1 — trainer writes the adapter (Unsloth handles safetensors → GGUF)
~/.dgx-train/adapters/reflector/w2026-19/
├── adapter.safetensors        # for HF Hub mirror
├── adapter.gguf               # for Ollama
└── Modelfile                  # generated by trainer:
                               #   FROM qwen3:30b
                               #   ADAPTER ./adapter.gguf
                               #   PARAMETER temperature 0.2  (role-specific)
                               #   SYSTEM "..."               (role-specific)

# Step 2 — Pareto + KL gate (Python; one-shot on the 30-d holdout)
python ~/.dgx-train/scripts/rlro/weekly_gate.py \
  --role reflector \
  --candidate ~/.dgx-train/adapters/reflector/w2026-19/ \
  --baseline qwen3:30b-reflector-current \
  --holdout ~/.dgx-train/eval/reflector/30d.jsonl \
  --metrics faithfulness,hit_rate,change_rate \
  --kl-tau 0.15
# exit code 0 → pass; non-zero → fail

# Step 3a — on pass: bake the adapter into a new tagged Ollama model
cd ~/.dgx-train/adapters/reflector/w2026-19/
ollama create qwen3:30b-reflector-w2026-19 -f Modelfile
# ~5-15 s, page-in of the ~150 MB GGUF adapter

# Step 3b — flip the "-current" alias
ollama cp qwen3:30b-reflector-w2026-19 qwen3:30b-reflector-current
# instantaneous; trading-bot's next chat_json() call picks up the new weights

# Step 4 — push the safetensors to the private HF Hub mirror
huggingface-cli upload \
  dgx-trader-adapters reflector/w2026-19/adapter.safetensors \
  --revision w2026-19

# Step 5 — on FAIL: do nothing.  The "-current" alias still points at
# last week's adapter.  No restart, no fallback to base, no operator
# intervention.  The next weekly cycle will train against the same
# baseline + 7 more days of lessons.

# Step 6 — emit Hermes Slack 4-question card (per session-lessons)
# Q1: What ran? Q2: What changed? Q3: What's next? Q4: What needs you?
~/.hermes/scripts/post_slack.sh --template weekly_lora_summary
```

### 4.2 Daily-rebuild cron (the safety net)

Separate from the Sunday training cron, a **daily 03:00 ET cron**
rebuilds every `<role>-current` Modelfile against the **same** adapter
GGUF.  Purpose:

- Guards against Ollama's GGUF cache being evicted under memory
  pressure (the box runs near 80 GB idle per doc 08 measurement).
- Guards against the operator manually `ollama rm`-ing a model and
  not realising the alias broke.
- Cheap: ~5 s per role × 6 roles = ~30 s wall-clock.  Runs while the
  trading bot is asleep (pre-market, no debate calls).

Cron defer to `11-HERMES_AGENT.md` for the exact `cron/jobs.json`
entry.

### 4.3 Why NOT vLLM hot-swap

Verbatim from `project-drop-vllm`:

> vLLM was bootstrapped on the DGX Spark and pushed memory to 95 GB / 121 GB
> + 7.3 GB swap before being killed. … The single-user / 5-15-LLM-calls-per-debate
> workload does not benefit from vLLM's batched throughput. … Adapter
> swap "speed" — Ollama swaps the running model on first request with
> the new tag (a few seconds for the GGUF page-in).  Acceptable for our
> cadence — we don't fire 100s of requests/sec.

The validator's P0-1 disagreement (prior doc 02 vs operator decision)
is **resolved by this rewrite**.  No `load_lora_adapter` calls, no
`load_inplace=True`, no S-LoRA / Punica / SGMV.  The runtime is
`ollama serve` on port 11434, unchanged.

---

## 5. Anti-Forgetting — simpler at weekly cadence

The prior doc layered **four** defences (Online-LoRA Fisher Ω, weekly
anchor, replay buffer, KL guard) because continuous training was its
core risk.  At weekly cadence the picture flips: the **weekly cycle IS
the anchor**, so the defence set collapses.

### 5.1 Defence 1 — the cadence itself

Catastrophic forgetting is a continuous-training pathology.  At one
training pass per week, with 7 days of trade volume per pass, the per-week
weight delta is small by construction.  We do not need EWC / Online-LoRA
Fisher regularisation as a separate trick — the cycle's natural rhythm
provides the same effect.

### 5.2 Defence 2 — replay reservoir (small, stratified)

Maintain a **rolling 500-pair reservoir** of older preference pairs per
role, stratified across regime labels (`bull / chop / bear / unknown`).
Weekly training batch = **20 % fresh + 80 % replay** (the inverse of the
prior doc's 80/20 because fresh data is now much rarer).  This is
classic experience replay (Rolnick et al. 2018) and is the only defence
needed against data-distribution drift.

### 5.3 Defence 3 — one-shot KL guard at promotion

Before the alias flip, on a stratified 100-prompt replay set:

```
KL( π_new(·|x) ‖ π_prev(·|x) )  >  τ_KL    →    reject promotion
```

With `τ_KL = 0.15` for prose, `0.25` for JSON.  Same as prior doc; only
the **firing frequency** changes (once/week vs every 10 min).  The KL
guard is now **doing real work** because the weekly delta is large
enough to occasionally trip — and the failure mode (alias stays on prev
week) is **the desired default** under operator policy
(`project-modelforge-decisions` §3 — when in doubt, rollback).

### 5.4 What we do NOT do (vs prior doc)

- **No Online-LoRA Fisher Ω penalty.**  Weekly cadence + replay is
  sufficient.  Saves ~150 LOC and one literature dependency.
- **No periodic full-90-d retrain anchor.**  The weekly retrain IS the
  anchor.  The prior doc's "anchor = Sunday cron, deltas = every 10 min"
  inversion is gone.
- **No KL penalty inside DPO loss.**  TRL's `sync_ref_model=False` pins
  the reference to **last week's promoted champion**, giving a stable
  ground-truth for 7 days.
- **No O-LoRA / subspace orthogonality, no model-souping, no TIES.**
  Same rationale as prior doc — wrong tool for one shifting-distribution
  task.

---

## 6. Storage & Versioning

### 6.1 On-disk layout (host, separated from prior doc)

```
~/.dgx-train/adapters/
├── reflector/
│   ├── w2026-19/                       # ISO-week tag (YYYY-WW)
│   │   ├── adapter.safetensors
│   │   ├── adapter.gguf
│   │   ├── Modelfile                   # FROM qwen3:30b + ADAPTER ./adapter.gguf
│   │   └── eval/                       # 30-d holdout scores at promotion time
│   │       ├── faithfulness.csv
│   │       ├── hit_rate.csv
│   │       └── kl_vs_prev.csv
│   ├── w2026-18/                       # last week's promoted champion
│   ├── w2026-17/                       # kept 8 weeks deep on local disk
│   └── …
├── bull_debater/                       # same layout
├── bear_debater/
├── arbiter/
├── regime_tagger/
└── indicator_selector/
```

**The Ollama-level "current" pointer is NOT a symlink in this dir.** It
lives in Ollama's own model store (`~/.ollama/models/manifests/...`) as
an `ollama cp`-created tag — `qwen3:30b-reflector-current`.  The
trading bot calls the named model; Ollama resolves which underlying
adapter the alias points at.  This is the operator-locked design from
`project-drop-vllm` §2/§7.

### 6.2 ISO week numbering

`wYYYY-WW` follows ISO 8601 (e.g. `w2026-19` = the 19th week of 2026 =
the week of 2026-05-12).  Operator runbooks and dashboards both use
`isoweekday` for cron alignment; this matches.

### 6.3 HuggingFace Hub mirror (operator-locked: adapters-only, private)

Per `project-modelforge-decisions` §2:

- Repo: **`dgx-trader-adapters`** (private).
- Push: every **promoted** week's `adapter.safetensors` only — never the
  Modelfile, never the eval scores, never the SYSTEM prompt (which may
  encode strategy edge).
- Pre-push hook: scan adapter metadata for ticker × P&L co-occurrence
  patterns; refuse push if found.
- Revision tag: `wYYYY-WW`.
- Retrieval (operator runbook): `huggingface-cli download
  dgx-trader-adapters reflector/w2026-19/adapter.safetensors`.

### 6.4 Rollback procedure (operator runbook — one command)

```bash
# 1. Identify last-known-good week
ollama list | grep qwen3:30b-reflector-
# qwen3:30b-reflector-w2026-19
# qwen3:30b-reflector-w2026-18      <- last good
# qwen3:30b-reflector-w2026-17
# qwen3:30b-reflector-current       (currently aliases w2026-19)

# 2. Flip the alias back to prev week
ollama cp qwen3:30b-reflector-w2026-18 qwen3:30b-reflector-current

# That's it.  Trading bot picks up the prev-week weights on next call.
# Total rollback time: < 1 s.  No restart.  No service interruption.
```

If even the local disk is gone, recover from HF Hub:

```bash
huggingface-cli download dgx-trader-adapters \
  reflector/w2026-18/adapter.safetensors --revision w2026-18 \
  --local-dir ~/.dgx-train/adapters/reflector/w2026-18/
# regenerate the Modelfile + ollama create + ollama cp
```

### 6.5 Retention

| Artefact | Where | Retention |
|---|---|---|
| `wYYYY-WW/` adapter dir | `~/.dgx-train/adapters/<role>/` | **8 weeks** local |
| `adapter.safetensors` | HF Hub `dgx-trader-adapters` | **forever** (private) |
| Ollama model tag `qwen3:30b-<role>-wYYYY-WW` | `~/.ollama/models/` | **last 4 weeks** (older → `ollama rm` by daily-rebuild cron) |
| `qwen3:30b-<role>-current` alias | `~/.ollama/models/` | always present (recreated daily by §4.2 cron) |

Disk footprint: ~150 MB per adapter × 6 roles × 8 weeks ≈ **7 GB local**
plus ~5 GB on HF Hub.  Trivial.

---

## 7. Compute Cost Estimate (substantially lower than prior doc)

### 7.1 Training one role's weekly delta

Assumptions: rank-32 LoRA (prose) or r=16 (JSON), **10-15 fresh DPO
pairs + ~80 replay pairs**, β=0.1, 1-3 epochs, gradient checkpointing
on, Liger kernels on, `bf16`.  Base model = `qwen3:30b` (Q4_K_M, MoE
30B-A3B, 3B active params/token).

| Hardware | Wall-clock per role | Notes |
|---|---|---|
| **1× B200** (DGX Spark — operator's target) | **~5-10 min** | Q4 base + small batch; gradient flows through few matmuls |
| **1× H100 80 GB** (sanity check) | ~7-12 min | Slower than B200 per MLPerf v5.1 |
| **6 roles sequential** | **~30-60 min wall-clock per Sunday** | Inside the 4 h GPU lease, with comfortable margin |
| **6 roles parallel** (option, if VRAM headroom) | ~10-15 min | Risk: pushes unified memory budget; defer |

The prior doc projected ~12-18 min for **all 6 roles** at 1 000
pairs/role/cycle.  At 10-15 pairs/role/week we are not gated by compute
— we are gated by **statistical power**, not throughput.  The ~30-60
min estimate above is mostly **Ollama model load + adapter export +
Modelfile rebuild + KL-eval pass**, not gradient descent itself.

### 7.2 Cloud-equivalent dollar cost (never burst — for the viral-story angle)

| Cloud SKU | $/hr | Cost per Sunday (~1 h) |
|---|---|---|
| 1× B200 on-demand (lowest seen) | $2.45-$4.20 | **$2.45-$4.20** |
| 1× GB200 on-demand (avg) | $10.50-$18.22 | $10.50-$18.22 |

But we **never burst** — the DGX Spark earns its keep here.  The cloud
column exists only for the viral-release angle ("we trained 6 LoRAs on
hardware your laptop could replicate, weekly, for free").

### 7.3 Build cost — LOC + days (LOWER than prior continuous estimate)

| Component | New LOC (est.) | New tests | Days (solo dev) |
|---|---:|---:|---:|
| `scripts/rlro/preference_pair_builder.py` (reads `decisions.md` + chat_json) | ~250 | 8 | 0.5 |
| `scripts/rlro/weekly_lora_trainer.py` (TRL DPOTrainer + PEFT, Unsloth GGUF export) | ~300 | 10 | 1 |
| `scripts/rlro/weekly_gate.py` (Pareto + KL on 30-d holdout) | ~200 | 7 | 0.5 |
| `scripts/rlro/promote.py` (Modelfile gen + `ollama create` + `ollama cp`) | ~80 | 5 | 0.25 |
| `data/rlro/schema.sql` (90 d reservoir DB) | ~80 | — | 0.25 |
| `~/.hermes/cron/weekly_lora.job.json` (Sunday 14:00 ET) | ~40 | — | 0.25 |
| `~/.hermes/cron/daily_modelfile_rebuild.job.json` (03:00 ET) | ~30 | — | 0.1 |
| Dashboard card: `templates/spa/weekly_lora_status.html` (last 4 weeks per role + KL/hit-rate sparkline) | ~150 | 3 | 0.5 |
| HF Hub push hook (`scripts/rlro/hf_push.py` with PII scrub) | ~120 | 4 | 0.25 |
| **Total** | **~1 250 LOC** | **37 tests** | **~2-3 days** |

(Prior continuous design estimated ~1 720 LOC and ~7 days.  This drops
~470 LOC and ~4 days by **deleting** Online-LoRA Fisher math, the
shadow-scoring infra, and the every-10-min cron wrapper.)

Plus ~0.5 day of soak-testing in paper mode before enabling for live
trading.  **End-to-end: ~2-3 working days, single dev.**

---

## 8. Integration with Hermes (defer to sibling doc)

The cron entries, lockfile paths, GPU-yield handshake, and Slack
4-question card format all live in `11-HERMES_AGENT.md` (sibling doc,
to-be-written for rev2).  This doc references but does not duplicate:

- **Sunday 14:00 ET weekly LoRA cron** — entry name
  `weekly_lora_training`; consumes the `modelforge-weekly-lora-training`
  GPU lease.
- **Daily 03:00 ET Modelfile-rebuild cron** — entry name
  `daily_modelfile_rebuild`; does NOT consume the GPU lease (just
  `ollama create` round-trips).
- **Nightly 23:30 ET Reflector cron** — entry name `nightly_reflector`;
  writes to `stocks/memory/decisions.md`; does NOT consume the GPU
  lease (Ollama serving uses already-resident weights).

The Reflector cron itself is **outside the LoRA scope** — it is a
trading-bot operational cron, not a training cron.  Doc 11 owns the
wiring.

---

## 9. Open Questions / Risks (carry to HANDOFF.md)

1. **Small-data DPO stability**.  10-15 fresh pairs/week is below the
   classic DPO statistical floor (~50).  Mitigation: 80 % replay
   reservoir; weekly cadence so each cohort gets 7 days of out-of-sample
   evidence before the next nudge.  **Open**: does this actually
   converge in practice?  Validate in first 4 weeks of paper-mode soak.
2. **Reservoir cold-start**.  Week 1 has no replay reservoir.  Plan:
   bootstrap from the **first 30 days of live-paper trading history**
   (operator went live-paper 2026-05-11; we have ~2 weeks of data at
   doc-write time).  Don't run the trainer until week 4 minimum.
3. **Reflector judging itself**.  The reflector role's preference signal
   uses an arbiter-judge.  If the arbiter has not yet been promoted
   (week 1), use the **base `qwen3:30b`** as judge.  Document the
   bootstrapping order in the operator runbook.
4. **HF Hub pre-push scrubber**.  Scanning safetensors for ticker × P&L
   co-occurrence is non-trivial (the adapter weights themselves don't
   contain text, but the metadata + tokenizer config might).  Spec the
   scrubber explicitly in `09-RISKS.md` rev2.
5. **GPU-lease overrun**.  4 h is generous for 6 roles × ~10 min each,
   but if Pareto + KL gate logic accidentally re-trains on failure, the
   lease could blow past its 30-min grace.  Add a hard wall-clock kill
   in the trainer wrapper.
6. **Reward-hacking on `predictive_hit_rate_30d`** (carried from prior
   doc).  Bull/bear are scored on prediction accuracy they don't fully
   control (arbiter does).  Mitigation: **counter-factual hit-rate**
   (the side they argued against the arbiter's vote, evaluated on
   outcomes where they were over-ruled).

---

## 10. Sources

Same baseline as prior doc, pruned to drop vLLM-specific references and
add Ollama + small-data DPO sources.

### Inference serving (Ollama-only per `project-drop-vllm`)

1. **Ollama — Modelfile reference + ADAPTER instruction** — https://docs.ollama.com/modelfile
   (`FROM` + `ADAPTER` + `PARAMETER` + `SYSTEM`; `ollama create` semantics.)
2. **Ollama — model management (`ollama cp`, `ollama rm`, `ollama list`)** — https://github.com/ollama/ollama/blob/main/docs/api.md
   (Atomic tag aliasing via `ollama cp`; the runtime primitive we use
   for "promotion.")
3. **Ollama — issue #9548 (no runtime adapter hot-swap)** — https://github.com/ollama/ollama/issues/9548
   (Confirms Modelfile-only adapter loading is the supported path.)
4. **Ollama — `/api/create` REST endpoint** — https://github.com/ollama/ollama/blob/main/docs/api.md#create-a-model
   (For programmatic Modelfile-from-string creation from the
   trainer container.)
5. **Unsloth — `save_pretrained_gguf()` for native GGUF export** — https://github.com/unslothai/unsloth
   (One-shot safetensors → GGUF for Ollama consumption.)

### Continuous learning at small data (this is the small-data regime)

6. **Online-LoRA (WACV 2025)** — https://arxiv.org/html/2411.05663
   (Same paper as prior doc; we now cite §4 for the **floor on N**
   below which DPO is noise-dominated.)
7. **Experience Replay for Continual Learning (Rolnick et al., arXiv 1811.11682)** — https://arxiv.org/pdf/1811.11682
   (Foundation for the 80 %-replay-buffer policy.)
8. **CL-LoRA (CVPR 2025)** — https://openaccess.thecvf.com/content/CVPR2025/papers/He_CL-LoRA_Continual_Low-Rank_Adaptation_for_Rehearsal-Free_Class-Incremental_Learning_CVPR_2025_paper.pdf
   (Continual LoRA at low-data regimes; reservoir + rank choice.)

### Online preference training (DPO at small N)

9. **TRL — DPOTrainer (HF)** — https://huggingface.co/docs/trl/main/en/dpo_trainer
   (LR=1e-5 with PEFT, β default 0.1, `loss_type=sigmoid_norm`,
   `sync_ref_model=False` for anchor pinning, Liger kernel
   +20 % throughput.)
10. **PEFT LoRA config (`target_modules`, `r`, `lora_alpha`)** — https://huggingface.co/docs/peft/main/en/package_reference/lora
    (Drop-in for our r=32/r=16 split + target modules.)
11. **Liger Kernel (kernel fusion for DPO)** — https://github.com/linkedin/Liger-Kernel
    (Memory savings making the trainer fit comfortably in our budget.)

### Hardware

12. **MLPerf Training v5.1 — Blackwell results (NVIDIA)** — https://blogs.nvidia.com/blog/mlperf-training-benchmark-blackwell-ultra/
    (Llama-2-70B LoRA on B200/B300; basis for the per-role wall-clock
    estimate.)

### Multi-agent trading reference (for role semantics)

13. **TradingAgents (arXiv 2412.20138)** — https://arxiv.org/abs/2412.20138
    (Reflector + bull/bear/arbiter pattern; our 6 roles are a direct
    subset.)

### Operator-locked decisions (canonical for this rewrite)

14. `~/.claude/projects/-home-saijayanthai-Documents-trading-bot/memory/project_drop_vllm.md` — Ollama-only.
15. `…/memory/project_modelforge_decisions.md` — qwen3:30b lock; HF Hub adapters-only-private; weekly hit-rate gate; $0 paid-LLM budget.
16. `…/memory/feedback_no_heavy_containers_without_explicit_ok.md` — why vLLM was dropped.
17. `~/.hermes/config/gpu_reservation.yaml` — Sunday 14:00 ET window (lives at `holder: modelforge-weekly-lora-training`).
18. `~/.claude/projects/-home-saijayanthai-Documents-trading-bot/memory/project_session_2026-05-11_t30_checkpoint.md` — trading-volume context (2-3 trades/week, $629 wheel premium); the live-paper baseline week 1 of the reservoir.

---

*End of `02-RESEARCH-CONTINUOUS_LORA.md` (rev2).  Branch:
`feat/quanta-core-v4-rev2-r2`.  NOT pushed; NO code.  Next doc in the
series: `11-HERMES_AGENT.md` (cron details — defer cadence wiring
there).*
