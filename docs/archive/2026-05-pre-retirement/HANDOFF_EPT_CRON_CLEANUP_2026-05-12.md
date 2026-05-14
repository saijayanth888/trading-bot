# HANDOFF — EPT cron cleanup (2026-05-12)

Branch: `fix/ept-cron-cleanup`
Status: ready for review, **not pushed**

## What was done

Two Hermes crons paused and their shell scripts replaced with deprecation
no-ops:

| job_id | name | schedule (was) | state |
|---|---|---|---|
| `0ef7e5d701df` | `ept_training_daily` | `0 2 * * *` | **paused** |
| `79cebcba8474` | `ept_eval_breeding`  | `every 2160m` | **paused** |

The cron rows are **paused, not deleted** — `hermes cron list --all` still
shows them with `[paused]` and the original schedule/script bindings intact.

## Why retire

`ept_training_daily` was invoking `run_ept_generation.py --mode mock`.
`mock_eval_fn` is a pure function of the genome and the runner re-seeds with
`seed=42` on every fire, so the script emitted **the same output every
night**:

```
champion:   gen0-011
  fitness:  0.7540
  sharpe:   0.884
```

…across 4+ consecutive runs (2026-05-08 through 2026-05-12, last fire
`2026-05-12T02:00:39 ok` per `hermes cron list --all`). It also crashed on
every `train_fn` call with `No module named 'torch'` because the cron runs
outside the freqtrade container. Net effect: misleading Slack/Telegram
delivery + a wasted nightly cron slot + zero learning signal.

`ept_eval_breeding` reads the same `evolution.json` that `ept_training_daily`
writes — with training paused, the eval cron would post the same
flagged-weak alert every 36h forever. Paused as a sibling so the operator
doesn't keep seeing dead alerts.

Strategic direction per `docs/4_WEEK_EXECUTION_PLAN.md` and
`docs/MODELFORGE_INTEGRATION_PLAN.md` is **ModelForge** as the real evolution
platform (active crons: `modelforge_ingest`, `modelforge_curate`; UI at
`http://localhost:3001/automation`).

## Exact commands run

```bash
hermes cron pause 0ef7e5d701df     # ept_training_daily
hermes cron pause 79cebcba8474     # ept_eval_breeding
```

Plus shell-script replacements at:

- `~/.hermes/scripts/ept_training_daily.sh`  → deprecation banner, `exit 0`
- `~/.hermes/scripts/ept_eval_breeding.sh`   → deprecation banner, `exit 0`

Original script bodies remain in git history (they live under `~/.hermes/`
which is not in this repo, so the deprecation rewrites are *not* committable
here — see the "Operator action items" section).

## What was NOT touched

Per the spec, all Python modules and their call sites are intact:

- `user_data/modules/ept_evolution.py` — still imported by:
  - `hermes-mcp/server.py:425` (the `trigger_evolution_cycle` MCP tool)
  - `user_data/dashboard/mcp_local.py:355`
  - `user_data/dashboard/ops_routes.py:1118`
  - `tests/test_ept_evolution.py`
  Deleting it would break the MCP tool and the dashboard ops endpoint.
- `user_data/scripts/run_ept_generation.py` — same. Still callable manually
  (`python user_data/scripts/run_ept_generation.py --mode mock`) or from the
  MCP tool for ad-hoc runs.

## Verification

```bash
# 1. Both crons exist but are paused — should print [paused] twice
hermes cron list --all | grep -B1 -A5 -E "ept_(training|eval)"

# 2. Active cron list does NOT include them — should print nothing
hermes cron list | grep -E "ept_(training|eval)"

# 3. Manual invoke prints deprecation banner + exits 0
bash ~/.hermes/scripts/ept_training_daily.sh ; echo "exit=$?"
bash ~/.hermes/scripts/ept_eval_breeding.sh  ; echo "exit=$?"
```

Confirmed at session end:

- `hermes cron list --all | grep -E "ept_.*\[paused\]"` → both rows present
- `hermes cron list      | grep -E "ept_"`             → empty (paused jobs
  hidden from the default list)
- Manual `bash ~/.hermes/scripts/ept_training_daily.sh` prints banner, exit 0
- Manual `bash ~/.hermes/scripts/ept_eval_breeding.sh`  prints banner, exit 0

**One-liner cron-state check** (paste this in any future session):

```bash
hermes cron list --all | awk '/ept_(training|eval)/{p=1} p{print; if(/Last run/){p=0}}'
```

Should report both jobs `[paused]` until the operator resumes them.

## How to revive (if real EPT is ever built)

1. Implement per-agent paper-trading instances (one bot per population
   member). Trade journal rows must be tagged by `agent_id` so the live
   scorer in `run_ept_generation.py:_build_live_scorer` can route trades
   per genome.
2. Confirm `python user_data/scripts/run_ept_generation.py --mode live`
   emits differentiated fitness across genomes (no fallback to
   `mock_eval_fn`; check the runner log for the "using mock surrogate for
   …" warning — it must be absent).
3. Restore the original `~/.hermes/scripts/ept_training_daily.sh` and
   `~/.hermes/scripts/ept_eval_breeding.sh` bodies. The originals are
   reproduced inline in `docs/HERMES_CRONS_2026-05-11.md` Appendix 4.5 and
   the prior version of `ept_training_daily.sh` is in this branch's parent
   commit if a backup snapshot was taken.
4. `hermes cron resume 0ef7e5d701df` and `hermes cron resume 79cebcba8474`.
5. Watch one full 02:00 ET run and confirm the champion ID changes across
   generations (i.e. it is NOT `gen0-011` again).

## Replacement: ModelForge

| Concern | EPT (retired) | ModelForge (active) |
|---|---|---|
| Evolution engine | `ept_evolution.TradingPopulation` | `apps/api/src/agents/evolution_graph.py` |
| Scheduler | Hermes cron `ept_training_daily` | `AutomationEngine` |
| UI | dashboard champion-card (mock) | `http://localhost:3001/automation` |
| Data flow | local trade_journal | curated tracks via `modelforge_ingest`/`modelforge_curate` crons |
| Docs | (retired) | `docs/MODELFORGE_INTEGRATION_PLAN.md` |

## Operator action items

The `~/.hermes/scripts/` directory lives outside this repo (it's a Hermes
runtime path). The two `.sh` rewrites are on the filesystem but **cannot be
committed via this branch**. The in-repo changes that *can* be committed:

- `docs/MODELFORGE_INTEGRATION_PLAN.md` — appended "EPT retirement
  (2026-05-12)" section.
- `docs/HERMES_CRONS_2026-05-11.md` — struck through both EPT rows.
- `HANDOFF_EPT_CRON_CLEANUP_2026-05-12.md` — this file.

If you want the script rewrites version-controlled too, copy them under
`scripts/hermes/` in this repo and add a symlink-or-deploy step to the
backup playbook (`reference_backup_system.md`).
