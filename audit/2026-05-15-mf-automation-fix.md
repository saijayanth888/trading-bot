# ModelForge Automation Engine — Fix Analysis & Implementation
**Date:** 2026-05-15  
**Status:** COMPLETE — 4 new workflows created, scheduler confirmed active

---

## What the Engine IS

The ModelForge automation engine is a full APScheduler-backed workflow orchestrator embedded directly in the mf-api FastAPI process. It owns three concerns: (1) a per-process APScheduler instance (`AsyncIOScheduler`) that fires cron-triggered workflows, (2) a wildcard event-bus subscription that routes domain events (`track.promoted`, `champion.promoted`, etc.) to event-triggered workflows, and (3) a Postgres-persisted workflow table (`automation_workflows`) that survives container restarts. On every `start()` call (lifespan hook at API boot), the engine seeds the default system workflows if they don't exist, loads all enabled workflows from Postgres, mounts them into APScheduler or the event bus, and begins ticking. The REST surface at `/api/automation/workflows` is the canonical way to create, update, enable, and delete workflows; the route handler calls `eng.remount(wf)` after every write so new workflows are live immediately without a restart.

---

## What Was CURRENTLY Wired vs Broken vs Missing-Data

### Wired and working (before this fix)
| Workflow | Kind | Cron/Trigger | Status |
|----------|------|-------------|--------|
| Health Monitor | system | `*/15 * * * *` | enabled, firing every 15 min |
| System Metrics Post | system | `0 * * * *` | enabled, firing every hour |
| Drift Detection | system | `0 */6 * * *` | enabled, firing every 6h |
| Auto Cleanup | system | `0 3 * * 0` | enabled, Sunday 03:00 UTC |
| Publish Promoted Adapter to Ollama | system | event: `track.promoted` | enabled |
| Trading Tracks Weekly Champion | user | `0 18 * * 0` | enabled — Sunday 18:00 UTC = 14:00 ET |

### Disabled by design (off by default, operator decision)
- Nightly Evolution (`0 2 * * *`) — off; uses generic Llama-3.2-3B, not the trading-specific qwen3:30b config
- Daily Report, Weekly Summary, Champion-Promoted Slack Ping — off; Slack not configured

### The gap (root cause of "automation not running properly")
The `Trading Tracks Weekly Champion` workflow fires at the right time (Sunday 14:00 ET) and targets `trading-reflector` correctly — BUT it covers ONLY `trading-reflector`. Four other tracks with complete datasets and zero training history had no automation at all:
- `trading-bear` (benchmarks: evidence_density, judge_preference) — dataset present, never trained
- `trading-bull` (benchmarks: evidence_density, judge_preference) — dataset present, never trained
- `trading-arbiter` (benchmarks: decision_consistency, downstream_pnl_per_decision, structured_output_validity) — dataset present, never trained
- `trading-regime-tagger` (benchmarks: structured_output_validity, agreement_with_hmm) — dataset present, never trained

Note: `trading-indicator-selector` has no dataset dir — not a gap introduced here, it was never seeded.

---

## Why No Automation Was Firing for 4/5 Tracks

The engine itself was working. The 6 historical evolution runs from 2026-05-13 were ALL manually triggered (`trigger_kind=manual`) — the cron had never fired because the engine started after those runs and the next Sunday 18:00 UTC hadn't arrived yet. The engine is correctly configured to fire this Sunday (2026-05-17 18:00 UTC).

The structural problem: one workflow per track is the correct model because `EvolutionStart.execute()` has an "already-running" guard (`get_dashboard_run()` returns `skipped` if status is `running` or `starting`). If two tracks were in a single workflow's action list, only the first would train; the second would be skipped. Separate staggered workflows IS the engine's intended multi-track design.

---

## The Fix (Implemented)

Created 4 new user workflows via `POST /api/automation/workflows`, staggered 20 minutes apart inside the GPU reservation window (Sunday 14:00-18:00 ET):

| Workflow | track_id | UTC cron | ET fire time |
|----------|----------|----------|--------------|
| Trading Tracks Weekly Champion (existing) | trading-reflector | `0 18 * * 0` | 14:00 ET |
| **Trading Bear Weekly LoRA** (new) | trading-bear | `20 18 * * 0` | 14:20 ET |
| **Trading Bull Weekly LoRA** (new) | trading-bull | `40 18 * * 0` | 14:40 ET |
| **Trading Arbiter Weekly LoRA** (new) | trading-arbiter | `0 19 * * 0` | 15:00 ET |
| **Trading Regime Tagger Weekly LoRA** (new) | trading-regime-tagger | `20 19 * * 0` | 15:20 ET |

All 5 tracks complete by ~15:27 ET (7 min/run * 5 runs + stagger = 1h 27min), well within the 4-hour window. Each workflow uses the same config as the proven `trading-reflector` run: `qwen3:30b`, `lora_rank=16`, `lora_alpha=32`, `max_generations=1`, `max_samples=2000`, `batch_size=2`, `learning_rate=0.0002`.

No source code edits were required. The engine already supported everything needed.

---

## Workflow Records Created

```
id=633db69d-060e-41b0-be85-c3869a56e3ed  Trading Bear Weekly LoRA       cron: 20 18 * * 0
id=cd09c269-52c6-4a4d-afc7-9087f22c4b51  Trading Bull Weekly LoRA       cron: 40 18 * * 0
id=7bf0b74a-058c-431c-87e0-866ab7e2ad5d  Trading Arbiter Weekly LoRA    cron: 0 19 * * 0
id=9e938ae8-c453-4f81-a3e4-c7c74a327816  Trading Regime Tagger LoRA     cron: 20 19 * * 0
```

All 4 jobs immediately added to APScheduler (confirmed in mf-api logs at 13:25 UTC):
```
2026-05-15 13:25:22 [apscheduler.scheduler] INFO: Added job "AutomationEngine._run_workflow_by_id" to job store "default"
2026-05-15 13:25:29 [apscheduler.scheduler] INFO: Added job "AutomationEngine._run_workflow_by_id" to job store "default"
2026-05-15 13:25:39 [apscheduler.scheduler] INFO: Added job "AutomationEngine._run_workflow_by_id" to job store "default"
2026-05-15 13:25:50 [apscheduler.scheduler] INFO: Added job "AutomationEngine._run_workflow_by_id" to job store "default"
```

---

## Verification

```
DB state:    14 total workflows, 10 enabled
Scheduler:   9 cron jobs + 1 event subscription active in APScheduler
Run history: 387 automation_workflow_runs rows, all successful
Next fires:
  trading-reflector:    2026-05-17T18:00:00 UTC = Sun 14:00 ET
  trading-bear:         2026-05-17T18:20:00 UTC = Sun 14:20 ET
  trading-bull:         2026-05-17T18:40:00 UTC = Sun 14:40 ET
  trading-arbiter:      2026-05-17T19:00:00 UTC = Sun 15:00 ET
  trading-regime-tagger: 2026-05-17T19:20:00 UTC = Sun 15:20 ET
```

---

## What Was Deliberately NOT Done

1. **No `trading-indicator-selector` workflow** — no dataset exists (`/app/data/dgx-train/datasets/` does not contain this track). Seeding a workflow for it would produce a failed evolution run. Deferred until the operator seeds the dataset.

2. **No `code`, `general`, `math`, `reasoning` track workflows** — these tracks use `meta-llama/Llama-3.2-3B-Instruct`, not `qwen3:30b`. They have no datasets under `/app/data/dgx-train/datasets/`. The "Nightly Evolution" system workflow (disabled) is the right vehicle for these if datasets are provided. Left alone.

3. **No `adapter.publish_ollama` action added to the new workflows** — the existing "Publish Promoted Adapter to Ollama" system workflow already fires on `track.promoted` events for all `trading-*` tracks. Adding a redundant publish step inside each training workflow would double-publish. The event-driven publish workflow handles this correctly.

4. **No GPU gate integration** — the `gpu_gate.sh` script at `~/.hermes/scripts/gpu_gate.sh` is a Hermes-layer concern. The engine has no hook into it. If Hermes is actively using the GPU at 14:00 ET, the training run will either queue behind it or contend. This is pre-existing; documented as future work (add an `http.post` action step to check GPU availability before calling `evolution.start`).

5. **No `HERMES_GPU_GATE_DISABLE=1` integration** — same reason. The workflows fire regardless of the gate.

6. **No rebuild of mf-api image** — the source edits from the prior schema fix session are already running in the container (confirmed by the engine seeding 10 workflows correctly at 11:42 UTC). No new source changes were made here; everything was done via the REST API.

---

## What the Operator Should Expect Sunday 2026-05-17

**14:00 ET (18:00 UTC):** The engine fires `Trading Tracks Weekly Champion`. It calls `evolution.start` with `track_id=trading-reflector`. A new `run-XXXXXXXX` is created in `evolution_runs`. Training begins on the DGX GPU. If Slack is configured, a notification fires.

**14:20-15:20 ET:** The 4 new workflows fire sequentially (20 min gaps). Each starts a new run on its track. The 20-minute gap ensures the previous run's GPU demand has wound down before the next one starts (proven run time: ~7 min). The engine's "already running" guard is the safety net — if for any reason a prior run is still going, the next workflow logs a `skipped` result rather than failing.

**After each run completes:** The `Publish Promoted Adapter to Ollama` event-driven workflow fires automatically on `track.promoted`, pushing the new adapter into Ollama and creating the `-current` alias. No manual step needed.

**Where to watch:** `docker logs mf-api -f 2>&1 | grep -i "workflow\|evolution\|trading"`. Each workflow start logs `[workflow:Trading Bear Weekly LoRA] start run_id=NNN trigger=cron`. Completion logs include status and step count. The mf-frontend `/automation` page shows the workflow list with `last_run_status` and `last_run_at`.

**If a run fails:** The `automation_workflow_runs` table records the error. Hit `/api/automation/workflows/{id}/trigger` manually after diagnosing. The "already running" guard will be gone since the failed run completes immediately on error.
