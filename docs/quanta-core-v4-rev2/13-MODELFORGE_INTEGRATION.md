# 13 · ModelForge Integration — quanta_core ← consumes, does NOT rebuild

> **Status:** Design · rev2-r13
> **Branch:** `feat/quanta-core-v4-rev2-r13`
> **Owner:** Quanta Core (consumer) · ModelForge (producer)
> **Cadence:** Weekly LoRA promotion · Sunday 04:00 ET (cron already live in mf-api)
> **Companion docs:** `12-WEEKLY_PUBLISHER.md` (reads adapter-promotion events) ·
> rev1 `02-RESEARCH-CONTINUOUS_LORA.md` (the theory; this doc is the contract)

---

## 0 · Why this doc exists

Earlier this week the operator stood up **ModelForge** — a standalone LoRA adapter
evolution platform — and registered six trading-bot tracks against it. ModelForge
is now a live, accountable system on this machine:

- `mf-api` (FastAPI, port 8000, network `model-forge_default`)
- `mf-frontend` (port 3001) · `mf-postgres` (pgvector, port 5433) · `mf-redis` (6379)
- Six `trading-*` tracks registered: `trading-reflector`, `trading-bull`,
  `trading-bear`, `trading-arbiter`, `trading-regime-tagger`, `trading-indicator-selector`
- A `Trading Tracks Weekly Champion` cron workflow (Sunday 04:00 ET) and a
  `Publish Promoted Adapter to Ollama` event-driven workflow (already enabled)
  that publishes to Ollama **and** mirrors to the private HF Hub repo
  `Saijayanyh532ai/dgx-trader-adapters`
- HF Arrow training data already flowing from `quanta_core`'s nightly cron to
  `~/.dgx-train/datasets/<track-id>/curated/` (bind-mounted into mf-api at
  `/app/data/dgx-train/`)
- Adapter artifacts at `/app/data/adapters/run-<id>/gen-<N>/` (mf-api private
  volume — **not** under `~/.dgx-train/`)

**The mistake V4 must not make:** re-implement training, eval, promotion, HF
mirror, or workflow scheduling inside `quanta_core`. Those are ModelForge's job
and ModelForge is the system of record for them.

**What V4 owns:** the producer side (training-sample generation, DPO pair
construction) and the consumer side (waiting for promotion events and rebuilding
the live Ollama Modelfile alias). Everything between those two surfaces is an
HTTP call to `mf-api`.

This doc is the **contract** — what crosses the boundary, in which direction,
with what schema. If a future change tries to add a trainer, a Pareto evaluator,
or an adapter version DB inside `quanta_core`, this doc is the receipt that
explains why that change is rejected.

---

## 1 · The three-way ownership contract (headline)

Every responsibility along the LoRA refresh path is assigned to exactly one
system. Anything ambiguous is a bug.

| Concern | Owner | Implementation pointer |
|---|---|---|
| Training-sample generation (raw trade outcomes → SFT / DPO pairs) | **quanta_core** | `scripts/modelforge_ingest.py` + `scripts/modelforge_curate.py` (nightly 21:00 / 21:30 ET) |
| Training execution (PEFT + TRL DPOTrainer, single-GPU, qwen3:30b base) | **ModelForge** | mf-api `apps/api/src/agents/training_backend.py`; triggered by `evolution.start` action |
| Pareto-promotion algorithm | **ModelForge** | mf-api built-in evaluator; emits `track.promoted` on pass |
| Adapter version registry + lineage | **ModelForge** | `mf-postgres` tables `evolution_tracks`, `adapters`, `track_generations` |
| HF Hub mirror (`Saijayanyh532ai/dgx-trader-adapters`) | **ModelForge** | `adapter.publish_huggingface` action on the `track.promoted` event |
| Adapter publish to Ollama (versioned model name + `-current` alias swing) | **ModelForge** | `adapter.publish_ollama` action on the same event |
| Track creation + workflow registration | **ModelForge** (one-time, **already done**) | `scripts/modelforge_register_tracks.py` (idempotent) |
| Sunday GPU yield / resume scheduling around the train window | **Hermes** | existing `gpu_yield_now.sh` / `gpu_resume.sh` crons |
| Drift detection between generations (advisory Slack alerts) | **ModelForge** | `Drift Detection` workflow, every 6 h |
| Adapter loading at quanta_core inference time | **quanta_core** | Ollama model name resolver — pin per-role to `qwen3:30b-{role}-current` |
| Adapter rollback (manual operator intent) | **quanta_core** ← **mf-api** | `POST /api/adapters/{adapter_id}/rollback` |
| Weekly Publisher's "adapters_promoted last Sunday" field | **quanta_core** (consumer) | reads from `GET /api/models/champion` + lineage scan |

Plain-English version: **mf-api trains and promotes. quanta_core only feeds it
data and reads its champion pointer.** Everything else is plumbing.

---

## 2 · Wire diagram (what flows where)

```
       ┌──────────────────────── quanta_core (host) ──────────────────────────┐
       │                                                                       │
       │   stocks/memory/decisions.md                                          │
       │   stocks/memory/llm-calls.jsonl                                       │
       │           │                                                           │
       │           ▼  21:00 ET nightly                                         │
       │   scripts/modelforge_ingest.py                                        │
       │           │                                                           │
       │           ▼  21:30 ET nightly                                         │
       │   scripts/modelforge_curate.py                                        │
       │           │                                                           │
       │           ▼  writes HF Arrow shards                                   │
       │   ~/.dgx-train/datasets/<track-id>/curated/                           │
       │                                                                       │
       └──────────────────────────────────┬────────────────────────────────────┘
                                          │  (bind mount)
                                          ▼
       ┌──────── mf-api container (network model-forge_default) ──────────────┐
       │                                                                       │
       │   /app/data/dgx-train/datasets/<track-id>/curated/   ← read-only      │
       │                                                                       │
       │   Sunday 04:00 ET — workflow "Trading Tracks Weekly Champion"         │
       │           │                                                           │
       │           ▼  evolution.start  (track_id, base_model=qwen3:30b,        │
       │           │                    lora_rank=16, lora_alpha=32,           │
       │           │                    learning_rate=2e-4, max_samples=2000)  │
       │   apps/api/src/agents/training_backend.py                             │
       │           │                                                           │
       │           ▼  writes                                                   │
       │   /app/data/adapters/run-<id>/gen-<N>/                                │
       │           │                                                           │
       │           ▼  Pareto evaluator                                         │
       │   pass → emit event  track.promoted  {track_id, generation, ...}      │
       │           │                                                           │
       │           ▼  event-driven workflow "Publish Promoted Adapter…"        │
       │           ├─ adapter.publish_ollama   →  ollama create                │
       │           │                              qwen3:30b-{role}-v<date>     │
       │           │                              swing -current alias         │
       │           ├─ adapter.publish_huggingface  →  push to                  │
       │           │                              Saijayanyh532ai/             │
       │           │                              dgx-trader-adapters          │
       │           └─ notify.slack  →  #quanta-models                          │
       │                                                                       │
       └──────────────────────────────────┬────────────────────────────────────┘
                                          │
                                          │  (Ollama model alias updated)
                                          ▼
       ┌──────── quanta_core inference path (consumer-side) ──────────────────┐
       │                                                                       │
       │   quanta_core.hermes.model_router  reads:                              │
       │       reflector  → qwen3:30b-trading-reflector-current                │
       │       bull       → qwen3:30b-trading-bull-current                     │
       │       bear       → qwen3:30b-trading-bear-current                     │
       │       arbiter    → qwen3:30b-trading-arbiter-current                  │
       │       regime     → qwen3:30b-trading-regime-tagger-current            │
       │       indicator  → qwen3:30b-trading-indicator-selector-current       │
       │                                                                       │
       │   adapter_loader hourly heartbeat:                                    │
       │       GET /api/models/champion  →  cache (adapter_id, generation)     │
       │       if changed since last check → write to                          │
       │       stocks/memory/adapter_state.json (for the publisher to read)    │
       │                                                                       │
       └──────────────────────────────────────────────────────────────────────┘
```

Key invariant: **quanta_core never touches `/app/data/adapters/`, never runs
Ollama `create`, never pushes to HF Hub.** It pins to the `-current` model
aliases that ModelForge swings.

---

## 3 · mf-api endpoint usage (concrete API surface)

All endpoints below have been ground-truth probed against the live `mf-api`
container (`docker exec mf-api curl -fsS …`). Auth header is `X-API-Key:
$MODELFORGE_API_KEY`. The `MODELFORGE_API_KEY` env var is mirrored into the
trading-bot `.env` already.

Operator-aliased names from the brief (`/api/forge/champions/list` etc.) **do
not exist** in mf-api today. The real endpoint shapes are:

| Operator-aliased name (from brief) | **Actual mf-api endpoint** | quanta_core usage |
|---|---|---|
| `GET /api/forge/tracks` | `GET /api/forge/tracks` ✓ | Enumerate tracks at boot; sanity-check that all 6 `trading-*` ids are present and `enabled=true`. Read-only. |
| `GET /api/forge/champions/list` | `GET /api/models/champion` (single) + `GET /api/adapters/` (filter `is_champion=true`) | Read current champion per track. Called hourly by `adapter_loader`. |
| `POST /api/forge/tracks/{track_id}/train` | `POST /api/automation/workflows/{workflow_id}/trigger` (with the existing `Trading Tracks Weekly Champion` workflow) **OR** `POST /api/evolve/start` (with `track_id` in body) | Manual re-trigger if the Sunday cron is missed. Normal weekly run fires automatically. |
| `GET /api/forge/runs/{run_id}` | `GET /api/evolve/{run_id}` (status) · `GET /api/evolve/{run_id}/events` (live event stream) | Poll training progress when quanta_core triggers a manual run. Not used on the happy path (the Sunday cron handles itself). |
| `POST /api/forge/champions/{track_id}/promote` | **Not called by quanta_core.** mf-api emits `track.promoted` automatically when the Pareto evaluator passes. | n/a — automatic |
| `POST /api/forge/champions/{track_id}/rollback` | `POST /api/adapters/{adapter_id}/rollback` | Manual operator rollback via a quanta_core CLI wrapper. |

Auxiliary endpoints quanta_core touches:

- `GET /api/system/health` — bootstrap precondition. If non-200, the
  `adapter_loader` exits cleanly and Slacks an alert; no fallback fetch.
- `GET /api/automation/workflows/{workflow_id}/runs` — used by the post-mortem
  publisher to attribute the previous Sunday's run to a week.

**API key handling.** The key is read from `os.environ["MODELFORGE_API_KEY"]`.
It is never logged, never echoed to a transcript, never written into a state
file. The HTTP client wraps every request and strips `X-API-Key` from any
captured response trace.

---

## 4 · Training-sample contract (producer side, quanta_core)

This section is the **public schema** of the JSONL the trading bot writes for
ModelForge to consume. The current implementation lives in
`scripts/modelforge_ingest.py` (Stage 1) + `scripts/modelforge_curate.py` (Stage
2). V4 keeps that contract; it does **not** rewrite it.

### 4.1 On-disk location

```
~/.dgx-train/
├── raw/<track-id>/<YYYYMMDD>.jsonl            ← Stage 1 output (Stage 1 schema)
├── datasets/<track-id>/curated/               ← Stage 2 output (HF Arrow)
│   ├── data-00000-of-00001.arrow
│   ├── dataset_info.json
│   ├── state.json
│   └── mf_meta.json                           ← track_id, generation, sample_count
└── curate/<track-id>_<YYYY-MM-DD>.json        ← per-day curation stats
```

mf-api sees these via the bind mount `~/.dgx-train → /app/data/dgx-train` on the
`mf-api` container.

### 4.2 Stage-1 raw row (one JSONL line per LLM call)

```json
{
  "ts":              "2026-05-11",
  "ticker":          "NVDA",
  "role":            "trading-bull",
  "system_message":  "<system prompt>",
  "user_message":    "<user prompt>",
  "response":        "<model output>",
  "pending_outcome": false,
  "outcome_key":     "2026-05-11|NVDA",
  "ledger":          { "open_date": "...", "raw_pct": "...", "exit_reason": "..." }
}
```

### 4.3 Stage-2 curated row (HF Arrow, what mf-api actually trains on)

```json
{
  "category":     "trading-reflector",
  "source":       "trading-bot",
  "dataset_name": "trading-reflector",
  "instruction":  "[SYSTEM]\n<sys>\n[USER]\n<user>",
  "response":     "<model output>"
}
```

This is the **SFT shape** mf-api's current trainer expects. The trainer reads
`config["curated_path"]` and calls `datasets.load_from_disk()` on it.

### 4.4 Future: DPO-pair shape (when V4 ships the preference builder)

For tracks that benefit from preference training (`trading-bull`, `trading-bear`,
`trading-arbiter`, `trading-reflector`), V4 layers a second producer
`quanta_core.hermes.preference_pair_builder` that writes to:

```
~/.dgx-train/preferences/<track-id>/<YYYY-WW>.jsonl
```

with rows:

```json
{
  "timestamp":         "2026-05-11T14:32:00-04:00",
  "trade_id":          "t_8df091",
  "role":              "trading-bull",
  "prompt":            "<system + user, normalized>",
  "chosen":            "<the actual response that led to a + outcome>",
  "rejected":          "<counterfactual OR actual response that led to a − outcome>",
  "outcome_signal":    "win",                // win | loss | skip_correct | skip_wrong
  "horizon_days":      14,
  "pnl_pct":           1.7,
  "regime_at_entry":   "trending_up"
}
```

Definitions:

- **`chosen`** — the agent's actual response on a trade that (a) realised positive
  alpha vs SPY at the planned exit, **or** (b) was correctly skipped (the
  agent's "no-trade" preserved capital vs a counterfactual loser).
- **`rejected`** — either (a) a synthetic counterfactual (an alternate-response
  prompted from a deliberately weaker variant), **or** (b) the actual response
  on a sibling trade that lost. Default: prefer real losers when the role has
  enough negative samples; fall back to counterfactual only when the loser
  bucket has < 50 examples in the rolling 30-day window.
- **`outcome_signal`** — the unambiguous label. `skip_correct` rows allow the
  reflector and arbiter to learn the value of standing down.

The producer is `quanta_core.hermes.reflector` (existing module) extended with
a `--emit-preferences` flag; runs as part of the same 21:00 ET nightly cron.
The trainer-side adapter for DPO is **internal to ModelForge** — quanta_core
only emits the file.

### 4.5 Required mf-api patch (already documented)

`apps/api/src/agents/training_backend.py:301` must honour `config["curated_path"]`
instead of the hardcoded OpenOrca fallback. This is tracked as R1 in
`docs/MODELFORGE_INTEGRATION_PLAN.md` and is **not** a V4 deliverable — it is a
mf-api upstream fix that V4 depends on.

---

## 5 · Adapter consumption contract (consumer side, quanta_core)

quanta_core does not subscribe to mf-api events. It polls. The poll is cheap and
the cadence is loose because LoRA promotions happen weekly.

### 5.1 The hourly heartbeat

`quanta_core.hermes.adapter_loader`, runs on cron `0 * * * *`:

1. `GET /api/system/health` — abort if not 200.
2. `GET /api/models/champion` — current global champion.
3. `GET /api/adapters/?is_champion=true` (or `GET /api/forge/tracks` and inspect
   each track's `champion_adapter_path` field) — per-track champions.
4. Compare against the last snapshot in
   `stocks/memory/adapter_state.json`:
   ```json
   {
     "last_checked":          "2026-05-12T17:00:00-04:00",
     "tracks": {
       "trading-reflector":         { "adapter_id": "run-9d5f1b58__gen3", "ollama_model": "qwen3:30b-trading-reflector-current", "promoted_at": "2026-05-12T04:18:32Z" },
       "trading-bull":              { ... },
       ...
     }
   }
   ```
5. **If any track's `adapter_id` changed:** log a `MODEL_REFRESHED` entry to
   `decisions.md`, write the updated `adapter_state.json` atomically, and post
   a one-line Slack to `#quanta-models`. **Do not** invoke `ollama create` —
   that already happened via mf-api's `adapter.publish_ollama` action on the
   `track.promoted` event. quanta_core only **observes** the change.
6. **If no change:** touch the state-file mtime and exit.

### 5.2 The inference-time resolver

The inference paths in `quanta_core.shark.llm` resolve role → Ollama model name
via a static map:

| Role | Ollama model alias (resolved at inference time) |
|---|---|
| reflector | `qwen3:30b-trading-reflector-current` |
| bull | `qwen3:30b-trading-bull-current` |
| bear | `qwen3:30b-trading-bear-current` |
| arbiter | `qwen3:30b-trading-arbiter-current` |
| regime_tagger | `qwen3:30b-trading-regime-tagger-current` |
| indicator_selector | `qwen3:30b-trading-indicator-selector-current` |

Because mf-api's `adapter.publish_ollama` action **swings the `-current` alias
atomically**, the next inference call automatically uses the new adapter. There
is no Modelfile to rebuild client-side, no warmup, no eviction logic in
quanta_core.

### 5.3 Fall-through when an alias is missing

If `qwen3:30b-trading-{role}-current` doesn't exist yet (e.g. first week, no
adapter promoted), the resolver falls back to bare `qwen3:30b`. This is
checked on `model_router` startup and logged as a `MODEL_FALLBACK` line.
Operators can confirm the fallback list in the morning briefing.

---

## 6 · Hermes wiring (Sunday flow, end-to-end)

The Sunday refresh is **already running** inside mf-api on cron `0 4 * * 0`
(Sunday 04:00 ET, shifted from 02:00 to avoid GPU contention with the Hermes
`ept_training_daily` slot — see `MODELFORGE_INTEGRATION_PLAN.md` § R4). The
table below documents what quanta_core / Hermes layer on top of that cron. The
operator brief mentioned 14:00 ET; that was a planning shorthand. **The live
schedule is 04:00 ET** and this doc treats that as the source of truth.

| ET time | Owner | Action |
|---|---|---|
| Sun 03:55 | Hermes | `gpu_yield_now.sh` — vacate any non-essential GPU residents (TFT eval idles, dashboards) so mf-api training has the full 64 GB. |
| Sun 04:00 | **mf-api** | `Trading Tracks Weekly Champion` workflow fires. Action: `evolution.start` with `track_id=trading-reflector`. (Note: today's workflow only trains the reflector. Expanding to all six tracks is a one-line workflow edit inside mf-api's UI — out of scope for r13.) |
| Sun 04:00–05:30 | mf-api | Training, eval, Pareto gate. On pass, emits `track.promoted`. |
| Sun 04:00–05:30 | mf-api | On `track.promoted`: `adapter.publish_ollama` (Ollama `create` + `-current` alias swing) → `adapter.publish_huggingface` (push to `Saijayanyh532ai/dgx-trader-adapters`) → Slack ping. |
| Sun 05:00 (hourly) | quanta_core | `adapter_loader` hourly tick picks up the change, writes `adapter_state.json`, logs `MODEL_REFRESHED` to `decisions.md`. |
| Sun 06:00 | Hermes | `gpu_resume.sh` — restore non-essential GPU residents. |
| Sun 09:00 | quanta_core | (Optional) `Weekly Summary` workflow inside mf-api can fire; not required by quanta_core. |
| Fri 16:00 | quanta_core | `weekly_publisher.py` (see doc 12) reads `adapter_state.json` for the `adapters_promoted` field on the post. |

**Manual re-trigger.** If the Sunday window is missed, the operator can fire
the workflow manually from the mf-frontend UI **or** call the API:

```bash
WORKFLOW_ID=c1f9eb2d-7089-477f-96c2-a850fab41fb2
docker exec mf-api sh -c \
  'curl -fsS -X POST -H "X-API-Key: $MODELFORGE_API_KEY" \
     http://localhost:8000/api/automation/workflows/'$WORKFLOW_ID'/trigger'
```

(The `docker exec` form is preferred: it keeps the API key out of shell history
and process listings on the host. Same hygiene rule the operator's transcript
classifier enforces.)

---

## 7 · Failure modes & quanta_core behaviour

| Failure | Detection | quanta_core response |
|---|---|---|
| mf-api unreachable (container down, network partitioned) | `GET /api/system/health` non-200 from `adapter_loader` | Log to `decisions.md` (`mf_api_unreachable`), Slack `#quanta-models`, skip this hour's poll. **Keep prior `-current` aliases.** No fallback to bare base. Inference continues against the last known champion. |
| Sunday training run errors mid-cycle | mf-api marks the run `failed` in `evolution_runs` table; no `track.promoted` event fires | quanta_core sees no change to `adapter_id` on next hourly poll → emits a `MODEL_STAGNANT` warning to Slack after 8 days of no promotion (one missed Sunday). Operator decides whether to re-trigger or investigate. |
| Pareto evaluator rejects the new generation | mf-api logs `champion_unchanged` and skips publish | Same as above — quanta_core sees no change. This is the **happy non-promotion** case; it is treated as evidence the prior adapter is still good. |
| `adapter.publish_ollama` fails (Ollama daemon down, GGUF conversion error) | `Publish Promoted Adapter to Ollama` workflow's `last_run_status` becomes `error` in mf-api | quanta_core's resolver falls through to bare `qwen3:30b` for the affected role on next inference. Logged as `MODEL_FALLBACK`. Operator alerted via mf-api's own Slack action. |
| HF Hub upload fails (token expired, repo full) | `adapter.publish_huggingface` action errors | **quanta_core is agnostic.** ModelForge retries on the next promotion cycle. HF mirror is for disaster recovery, not the live serving path. |
| Operator-requested rollback | `quanta_core` CLI: `python -m quanta_core.hermes.adapter_rollback --role bull --to-generation 4` | quanta_core calls `POST /api/adapters/{adapter_id}/rollback`. mf-api swings the Ollama `-current` alias back. quanta_core's next hourly tick observes the change. |
| Champion drift detected (>5% benchmark regression vs prior generation) | mf-api `Drift Detection` workflow runs every 6 h | mf-api Slacks `#quanta-models`. quanta_core takes no automatic action — drift is advisory only. The next operator decision is whether to rollback or accept. |

The shared invariant: **on any failure, quanta_core does not lose the prior
champion.** No retraining is attempted client-side. No bare-base fallback is
made silently. Every fallback is logged.

---

## 8 · What V4 explicitly does NOT build

This list exists so the next "let's just add a small trainer to quanta_core"
proposal has a written rebuttal:

- **Adapter training (LoRA forward + backward + optimizer).** mf-api's
  `apps/api/src/agents/training_backend.py` is the only owner.
- **Pareto evaluator.** mf-api owns the multi-benchmark Pareto comparison;
  the algorithm has its own tests inside the mf-api repo.
- **Workflow scheduler / cron engine.** mf-api ships an
  `automation_engine` with cron + event triggers. Hermes still runs the
  GPU yield/resume scripts, but the training cron itself lives in mf-api.
- **HF Hub upload code.** `adapter.publish_huggingface` action (mf-api).
  quanta_core has no `huggingface_hub` Python dependency.
- **Adapter version DB / lineage tree.** `mf-postgres` tables
  `evolution_tracks`, `adapters`, `track_generations`. Read-only via
  mf-api HTTP for quanta_core.
- **Ollama Modelfile generation + `ollama create` orchestration.**
  `adapter.publish_ollama` action (mf-api). quanta_core only references the
  resulting model name.
- **Drift detection between generations.** `Drift Detection` workflow
  (mf-api). quanta_core consumes the Slack ping like any other operator.
- **Cleanup of stale adapter dirs.** `Auto Cleanup` workflow (mf-api),
  Sunday 03:00 ET.

If any of these surfaces ever needs to move into quanta_core (e.g. mf-api gets
sunset), the migration plan replaces this doc — it does not coexist with it.

---

## 9 · Migration path & shadow week

The current Sunday cron has been live for less than two weeks. Before V4 fully
trusts it as the canonical refresh path, run **one shadow week** in parallel:

**Shadow week (Sun 2026-05-17 → Sun 2026-05-24):**

1. Keep the existing mf-api `Trading Tracks Weekly Champion` workflow enabled
   (it is today). This is the *production* path.
2. Also run the existing manual cron `scripts/modelforge_register_tracks.py
   --force` on Saturday 2026-05-23 as a re-validation that the six tracks have
   the right config. This is a no-op if everything matches.
3. On Sunday 2026-05-24 at 06:00 ET, capture the promoted adapter ids from
   both:
   - `GET /api/models/champion` (current source of truth)
   - `~/.dgx-train/datasets/<track-id>/curated/mf_meta.json` (sidecar from the
     curator — independent record of which generation was trained on what data)
4. Diff the two. They must agree on `(track_id, generation, adapter_id)`. If
   they disagree, the curator or the trainer is out of sync — investigate
   before relying on the pipeline.
5. After one clean shadow week, mark the integration **production** in
   `decisions.md` and remove the manual `--force` step. The V4 docs assume the
   production state from week 3 onward.

**Cutover criterion:** the shadow week's `(track_id, generation, adapter_id)`
tuple agrees with the curator sidecar, and the published HF tag on
`Saijayanyh532ai/dgx-trader-adapters` matches the one mf-api recorded for that
generation. No code change. Just one operator-confirmed checkpoint.

---

## 10 · Build cost (quanta_core side only)

mf-api is already running. The Sunday cron is already firing. HF mirror is
already configured. What V4 adds inside `quanta_core`:

| Component | New LOC (est.) | Tests | Days |
|---|---:|---:|---:|
| `quanta_core/hermes/adapter_loader.py` (hourly poll + state file + Slack on change) | ~180 | 6 | 0.4 |
| `quanta_core/shark/llm/model_router.py` patch — role → `…-current` alias + fall-through | ~80 | 4 | 0.2 |
| `quanta_core/hermes/adapter_rollback.py` (CLI wrapper around `POST /api/adapters/{id}/rollback`) | ~120 | 4 | 0.3 |
| `quanta_core/hermes/preference_pair_builder.py` (DPO pair emitter; depends on §4.4) | ~250 | 8 | 0.6 |
| `.hermes/cron/adapter_loader.job.json` (hourly cron + lockfile + Slack on failure) | ~30 | — | 0.1 |
| Tests: shadow-week diff harness, fall-through behaviour, rollback flow | ~140 | 7 | 0.4 |
| **Total** | **~800 LOC** | **29 tests** | **~2.0 dev-days** |

Notes:

- No new infra. Reuses existing Slack webhook, existing cron pattern from other
  Hermes modules, existing `MODELFORGE_API_KEY` env var.
- No new dependencies. `httpx` is already a dep on this host.
- The DPO pair builder (~0.6 day) is optional and can be deferred to a later
  doc; it requires the mf-api trainer to also accept DPO-shape data, which is
  a separate upstream change.

---

## 11 · Acceptance criteria

A reviewer can sign off on r13 when:

- [ ] mf-api `GET /api/forge/tracks` returns all six `trading-*` tracks with
      `enabled=true` and the documented `target_benchmarks`.
- [ ] mf-api `GET /api/automation/workflows` shows the
      `Trading Tracks Weekly Champion` cron (Sunday 04:00 ET, `enabled=true`)
      and `Publish Promoted Adapter to Ollama` event workflow (`enabled=true`).
- [ ] `MODELFORGE_API_KEY` is set in the trading-bot `.env` (verified via
      `printenv | grep MODELFORGE_API_KEY` — value not printed).
- [ ] `~/.dgx-train/datasets/<track-id>/curated/` exists for at least one
      `trading-*` track with a non-empty Arrow shard and a `mf_meta.json`.
- [ ] One shadow-week run completes per §9 with matching adapter ids on both
      sides of the diff.
- [ ] `adapter_loader` cron is installed, has run at least once, and
      `stocks/memory/adapter_state.json` exists with the six tracks keyed.
- [ ] The Friday `weekly_publisher.py` (doc 12) can read `adapter_state.json`
      and renders the `adapters_promoted` field correctly.
- [ ] An operator-initiated rollback via `quanta_core.hermes.adapter_rollback`
      correctly swings the Ollama `-current` alias back to the prior generation
      (verified by `ollama show qwen3:30b-trading-{role}-current`).

---

## 12 · Open items / risks

1. **Workflow currently only trains `trading-reflector`.** The Sunday cron's
   `evolution.start` action is hardcoded to one track. Expanding to all six is
   an mf-api UI edit (six workflow actions or a loop helper); operator should
   decide whether to fan out on week 3 or stay single-track through paper
   rollout. r13 does not block on this.
2. **No DPO trainer in mf-api yet.** The current trainer is SFT-shape only. The
   §4.4 preference-pair format is forward-looking; until mf-api adds a
   `dpo.start` action, `preference_pair_builder.py` writes the files but they
   are not yet consumed. Operator-tracked as upstream R2.
3. **HF Hub repo size growth.** With six tracks × 8 retained tags ≈ 48 adapter
   versions × ~150 MB ≈ 7 GB. Well within the private-repo limit but worth
   monitoring after month 3.
4. **mf-api auth boundary.** Today the API key is mirrored from mf-api into
   the trading-bot `.env` by hand. If mf-api ever rotates the key, both
   sides must be updated. Document a rotation runbook before the public
   build-in-public cutover (week 4 in `4_WEEK_EXECUTION_PLAN.md`).
5. **Curator and trainer "version skew".** The curator writes `mf_meta.json`
   based on its own schema; the trainer reads from `dataset_info.json`. The
   shadow-week check (§9) catches divergence but does not prevent it. If
   skew recurs, add a CI step that runs the curator's `--check-schema` mode
   against mf-api's `data_curator.py` import.

---

## 13 · References

- `docs/MODELFORGE_INTEGRATION_PLAN.md` — the prior integration design
  (consolidated; many of its decisions land here as runtime facts).
- `docs/MODELFORGE_TRACK_REGISTRATION.md` — operator runbook for the
  one-shot `scripts/modelforge_register_tracks.py`.
- `docs/MODELFORGE_DATA_PIPELINE.md` — Stage 1 + Stage 2 schemas referenced
  by §4 above.
- `docs/quanta-core-v4-rev2/12-WEEKLY_PUBLISHER.md` — the Friday publisher
  that consumes `adapter_state.json` for the `adapters_promoted` field.
- `docs/quanta-core-v4/02-RESEARCH-CONTINUOUS_LORA.md` — rev1 research on
  continuous LoRA; the **theory** behind this doc, which now lives across
  the mf-api / quanta_core boundary instead of inside quanta_core.
- `docs/4_WEEK_EXECUTION_PLAN.md` — week-by-week public-launch schedule
  that this contract supports.
- mf-api source of truth (read-only from quanta_core): `/home/saijayanthai/
  Documents/spark/workspace/model-forge/apps/api/src/`.

---

*End of `13-MODELFORGE_INTEGRATION.md`. The boundary is the contract; the
contract is the boundary.*
