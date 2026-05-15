# ModelForge stack audit — 2026-05-14 night

**Auditor:** T2 (read-only)
**Scope:** mf-api (:8000), mf-frontend (:3001), mf-postgres (:5433), mf-redis (:6379)
**Code root:** `/home/saijayanthai/Documents/spark/workspace/model-forge/`
**Compose project:** `model-forge_default` network (separate from trading-bot stack)

| Container    | Status                | Image notes |
|--------------|-----------------------|-------------|
| mf-api       | Up 6 hours (healthy)  | uvicorn / FastAPI 0.1.0, Python 3.13.13, aarch64 |
| mf-frontend  | Up 6 hours (healthy)  | nginx serving Vite SPA |
| mf-postgres  | Up 6 hours (healthy)  | PG 15.4 |
| mf-redis     | Up 6 hours (healthy)  | Redis 7.4.9 |

`/api/system/health` returns: `{"status":"ok","postgres":"ok","redis":"ok","ollama":"ok"}` (200).

Trading-bot integration is live: `dashboard` container has `MODELFORGE_API_URL=http://host.docker.internal:8000` and the matching `MODELFORGE_API_KEY`. `GET http://localhost:8081/api/ops/weekly_training` responds 200 with `model_forge_reachable: true`, `n_tracks: 6`. Logs show the dashboard polling `/api/forge/tracks` successfully (172.20.0.1 → 200, ~1.8 ms).

---

## P0 — Critical (block trading)

**None.** All four containers healthy, dashboard ↔ mf-api link live, no LLM-cost endpoints firing on their own (the only background workflow is a 15-min internal `health.check` cron — see P3 note).

---

## P1 — Important (degraded behaviour or schema rot)

### P1-1 · Postgres "column does not exist" errors as recently as 2026-05-14 16:35 UTC

40 `ERROR` entries in `mf-postgres` log over the 2-day retention window. The recurring pattern (see `docker logs mf-postgres | grep 'does not exist'`):

```
2026-05-14 16:34:46 ERROR  column "has_adapter" does not exist
2026-05-14 16:34:46 ERROR  column "id" does not exist
2026-05-14 16:34:47 ERROR  column "status" does not exist
2026-05-14 16:34:55 ERROR  column "cron_schedule" does not exist
2026-05-14 16:35:12 ERROR  column "benchmark_name" does not exist
```

Some are caller-side (the trading-bot ops endpoints query mf-api, not Postgres directly, so the source of these is internal mf-api code or another non-mf client). All five queries failed silently — none surface in the API responses we tested, so the user-facing impact is **degraded** (likely empty rows where a richer payload was expected), not broken. Worth fixing before the trading-reflector pipeline scales up.

Also seen in the same log: `column "trigger_kind" does not exist` (2026-05-12 13:44) — that's now resolved (the column exists; see `\d automation_workflow_runs`), but it suggests the mf-api code base went through at least one mid-session schema drift this week.

### P1-2 · Five empty critical tables on the trading path

| Table | Rows | Use |
|-------|------|-----|
| `evolution_tracks` | 0 | Should hold the 6 trading tracks registered via `modelforge_register_tracks.py`. The `/api/forge/tracks` endpoint returns 5 tracks (4 baseline + `trading-reflector`) — these are loaded from registry.json on disk, **not** the DB. |
| `evolution_runs` | 0 | Champion `run-d4dac705` is on disk (`/app/data/adapters/run-d4dac705/gen-1`) but has no DB row. |
| `generations` | 0 | No generation history in DB. |
| `track_generations` | 0 | No track→generation links in DB. |
| `training_samples` | 0 | No training samples logged. |

Combined with row counts in **all other** non-automation tables also being 0 (`benchmark_scores`, `campaign_plans`, `campaign_results`, `model_embeddings`, `evolution_presets`, `evolution_schedule`, `automation_jobs`, `automation_workflows`, `automation_settings`), this points to mf-api running in a **file-backed mode** (registry.json / on-disk adapters / file caches) rather than DB-backed. The DB is connected and writeable (the `automation_*` tables show recent activity) but the evolution / training write paths aren't using it. This is **likely intentional** for the early-Hermes phase, but operator should confirm before assuming the DB will hold rollback history when needed.

### P1-3 · Redis is empty (DBSIZE = 0)

```
# Keyspace
(empty)
DBSIZE: 0
```

The RDB snapshot was 0.96 MB at server start ~6 h ago and aged 150 338 s before re-load — consistent with Redis being used purely as a sometimes-cache, never as the system of record. No active queues or pending jobs, which aligns with `campaigns/status: idle` and `evolve/status: idle`.

---

## P2 — Stale / orphaned config

### P2-1 · Repeated FATAL `password authentication failed for user "tradebot"` (2026-05-12)

```
2026-05-12 22:56:50 FATAL: password authentication failed for user "tradebot"
... (8 occurrences over 1.5 hours, last 2026-05-13 00:01:45)
DETAIL: Role "tradebot" does not exist.
```

Plus three one-off probes for non-existent roles `mf`, `postgres`, `mf_user` on 2026-05-13 16:23-16:37. Suggests a misconfigured caller (possibly an old trading-bot service, an n8n connection, or a manual DB tool) that pointed at `mf-postgres` with the wrong credentials. **No occurrences in the last ~24 hours** — caller appears to have been fixed or removed. Worth grepping for the source so it doesn't come back.

### P2-2 · Adapter-cleanup backlog

`/api/adapters/` lists multiple `status: archived, has_weights: false` rows (e.g. `run-0740348b__gen1`, `run-0c72861f__gen1`) — these are zero-size adapter directories left over after evolution-discard. `/app/data/adapters` is 11.7 GB across 92 files; not a disk problem (see Healthy section) but the dead rows clutter the API and the lineage UI. There's a `POST /api/adapters/cleanup` endpoint to drain them — operator decision.

### P2-3 · `/api/system/storage` reports no per-bucket free-space pressure but HF cache is large

180 GB in `/app/data/.cache` (HuggingFace hub cache, 1369 files). Not currently a problem (3.0 TB free, 17 % used at the host level) but if disk usage climbs, the HF cache is the obvious lever.

---

## P3 — Notes / observations

- **Internal `health.check` cron runs every 15 min** (`automation_workflow_runs` table — workflow `bb47d22d-…`). Each run does only `health.check` + a conditional `notify.slack` (skipped because services are healthy). No LLM call, no GPU spend, ~22 ms duration. Safe to leave on; mention so operator isn't surprised by 96 runs/day in the table.
- 95 endpoints in OpenAPI; protected with `X-API-Key` header (the key is in trading-bot `.env` as `MODELFORGE_API_KEY=pAuhGy_…`). Public endpoints: `/health`, `/api/health` (returns 401 — confusingly named), `/api/system/health`, `/api/system/status`. Everything else 401s without the key.
- GPU gate: `/api/system/gpu` reports `gpu_available: true, util: 7%, temp: 41 C, NVIDIA GB10 unified memory 44.8/121.7 GB used`. Healthy.
- `evolve/status` shows the last run `run-d4dac705` finished promote-or-discard 2026-05-13 17:57 UTC (≈30 h ago) on the `trading-reflector` track, base `qwen3:30b`, max_generations=1. Champion is in place (`/api/models/champion` confirms generation 1, avg_score 0.125 — low but expected for a 1-gen warm-up).
- `mf-api` log shows continuous polling from 172.20.0.3 (the mf-frontend container at the SPA dashboard) — that's expected and healthy. No errors in the 500-line tail.
- mf-frontend nginx log: clean 200s, no 4xx/5xx in the recent window.
- mf-redis log: clean startup, no errors.

---

## Healthy components

- All 4 containers up and healthy for 6 h with no restarts.
- mf-api `/api/system/health` green for postgres, redis, and ollama.
- 95-endpoint API responds with low latency (most calls 0.8 - 35 ms).
- Disk: 17 % used at host level; 3.0 TB free. Well under the 85 % threshold.
- mf-api → ollama (`http://host.docker.internal:11434`) link working — `/api/tags` HTTP 200 in the log.
- Trading-bot dashboard ↔ mf-api integration verified end-to-end:
  - `dashboard` container env has both `MODELFORGE_API_URL` and `MODELFORGE_API_KEY` set.
  - `GET /api/ops/weekly_training` returns 200 with `model_forge_reachable: true` and 6 tracks.
  - mf-frontend access log shows the dashboard's `/api/forge/tracks` calls succeeding.
- Schema integrity OK for the active path: `automation_workflow_runs` has all expected columns, recent rows look clean (status=success, both timestamps populated, step_traces populated).
- Five trading-bot integration touchpoints found (`hermes/healthcheck.py`, `hermes/lora_promoter.py`, `scripts/modelforge_*.py`, `dashboard/ops_routes.py`, `dashboard/v4_routes.py`) — all reference mf-api on `:8000`, all read-only or trigger-then-poll patterns. No 404 risk.

---

## Recommended follow-ups (operator decision)

1. **(P1)** Have someone walk the mf-api code paths that issue `column "X" does not exist` queries; either add the columns or rewrite the queries. Five distinct columns are missing today; if the schema migration isn't versioned, this will keep recurring.
2. **(P1)** Decide whether evolution / training history should hit Postgres or stay file-backed. If DB, populate the empty tables; if file, drop them (they confuse anyone inspecting the DB).
3. **(P2)** Find the `tradebot` user that briefly tried to auth against mf-postgres — likely an old `.env` or a stale n8n DB connection. Already silent for ~24 h, but worth confirming.
4. **(P2)** Run `POST /api/adapters/cleanup` once to reclaim the zero-weight archived rows (operator-authorized only — this is a write).
