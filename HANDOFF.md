# HANDOFF — `stage/modelforge-register-tracks`

**Status:** Ready to merge. Tested with mocked httpx (`pytest tests/test_modelforge_register.py`). **Not yet POSTed against a live ModelForge.** Operator runs the script after merge.

**Files added:**
* `scripts/modelforge_register_tracks.py` — the one-shot script (~530 LOC including docstrings and CLI plumbing).
* `tests/test_modelforge_register.py` — 22 unit tests, httpx fully mocked.
* `docs/MODELFORGE_TRACK_REGISTRATION.md` — operator runbook.

**Files touched:** none. **No production code paths altered.**

---

## The 6 tracks this script registers

| `track_id` | Base | Target benchmarks | Schedule |
|---|---|---|---|
| `trading-reflector` | `qwen3:30b` | `faithfulness_regex`, `predictive_hit_rate_30d`, `judge_score`, `debate_impact` | weekly |
| `trading-bull` | `qwen3:30b` | `evidence_density`, `judge_preference` | weekly |
| `trading-bear` | `qwen3:30b` | `evidence_density`, `judge_preference` | weekly |
| `trading-arbiter` | `qwen3:30b` | `decision_consistency`, `downstream_pnl_per_decision`, `structured_output_validity` | weekly |
| `trading-regime-tagger` | `qwen3:30b` | `structured_output_validity`, `agreement_with_hmm` | weekly |
| `trading-indicator-selector` | `qwen3:30b` | `structured_output_validity`, `downstream_strategy_alpha` | weekly |

LoRA defaults: rank=16, alpha=32, lr=2e-4, max_samples=2000, enabled=true. Edit `TRACKS` in the script to change.

---

## Pre-flight (run BEFORE the activation one-liner)

1. **ModelForge up on `:8000`:**

   ```bash
   docker compose -f /home/saijayanthai/Documents/spark/workspace/model-forge/docker-compose.yml up -d
   curl -s http://localhost:8000/api/system/health    # expect 200 OK
   ```

2. **API key (prod only — dev runs leave middleware open with a warning):**

   ```bash
   echo 'MODELFORGE_API_KEY=<your-key>' >> ~/.env-modelforge && chmod 600 ~/.env-modelforge
   ```

---

## Activation (the one-liner)

```bash
python3 /home/saijayanthai/Documents/trading-bot/scripts/modelforge_register_tracks.py
```

Idempotent: re-runs are no-ops. Exit 0 iff all 6 finished in a known-good state.

### Other modes

* `--dry-run` — preview each POST body, no HTTP issued.
* `--track trading-reflector` — register one specific role.
* `--force` — re-POST even if already registered.
* `--delete <track-id>` — rollback (deletes the row; cascade drops `track_generations`; adapters on disk untouched).

---

## Verify

```bash
curl -s http://localhost:8000/api/forge/tracks | jq '.tracks[] | .track_id'
```

Should list the 6 `trading-*` ids (plus any pre-existing seeded ones like `reasoning`, `code`, `math`, `general` — those are ModelForge's own defaults).

---

## Run the tests locally

```bash
cd /home/saijayanthai/Documents/trading-bot
python3 -m pytest tests/test_modelforge_register.py -v
```

httpx is fully mocked — zero network calls, safe to run on any host.

---

## Known gap — ModelForge POST endpoint is NOT yet shipped

This is the one thing the spec asked me to flag explicitly.

**Inspection of `/home/saijayanthai/Documents/spark/workspace/model-forge/apps/api/src/api/routes/forge.py`** confirms ModelForge currently exposes:

* `GET  /api/forge/tracks` (list)
* `POST /api/forge/query`, `POST /api/forge/classify`, `POST /api/forge/compare`
* `POST /api/forge/sync_tracks`
* `POST /api/adapters/{id}/promote_to_track`

It does **NOT yet expose `POST /api/forge/tracks` or `GET /api/forge/tracks/{id}` or `DELETE /api/forge/tracks/{id}`.** The CRUD primitive exists at the DB layer (`LineageDB.upsert_track()` in `lineage_db.py:814`) and is only invoked from `services/track_seed.py:65` during app startup against a hardcoded list of 4 default tracks.

The script is written to the spec — it assumes those endpoints will land. Until they do:

* **GET fallback:** the script handles 405/501 on the per-id GET by falling back to listing `/api/forge/tracks` and scanning for the id. Already covered by `test_per_id_405_falls_back_to_list`.
* **POST fallback:** the script will get a 405 on every POST until the route lands. The error surfaces in the summary table with `status=error, HTTP=405`. **The script does NOT crash — it reports and exits 1.**

### Two interim paths to seed the rows today

1. **Preferred — in-process seed.** Add 6 entries to `DEFAULT_TRACKS` in `/home/saijayanthai/Documents/spark/workspace/model-forge/apps/api/src/services/track_seed.py` and bounce the API container. The seed routine is idempotent (it skips ids that already exist).
2. **Escape hatch — direct SQL into the `evolution_tracks` table.** Operator only.

When ModelForge adds the POST endpoint, this script becomes the authoritative path with **zero code changes** on the trading-bot side.

---

## Schema mapping notes

The POST body matches the `evolution_tracks` row schema per `lineage_db.py:820-848`:

| Field | Source from spec |
|---|---|
| `track_id` | `id` |
| `name` | `display_name` |
| `description` | `role` |
| `base_model` | `base_model` |
| `target_benchmarks[]` | `evals` (JSONB list of arbitrary strings — schema accepts our names) |
| `lora_rank`, `lora_alpha`, `learning_rate`, `max_samples`, `enabled` | hardcoded defaults |

**Extras sent in the body that ModelForge today does NOT persist** (spec asked for them; ModelForge's `upsert_track` ignores unknown keys):

* `schedule` — operator's intended cadence (`weekly`). Currently ModelForge owns scheduling via `evolution_schedule` table; this field is documentation-only until the schema bumps.
* `expected_data_path` — where trading-bot's ingest+curate cron writes the HF Arrow shards. Documentation-only; ModelForge today picks up curated data via its own `data_curator` from a hardcoded path.

If/when ModelForge's `EvolutionRequest` schema bumps to accept these (per integration plan §5.4), no script changes needed — the body already carries them.

---

## What ships next (NOT in this branch)

* ModelForge: ship `POST /api/forge/tracks` route — minimal handler that calls `db.upsert_track(body)`.
* ModelForge: ship `GET /api/forge/tracks/{id}` and `DELETE /api/forge/tracks/{id}` — same pattern.
* trading-bot: `stocks/shark/llm/modelforge_client.py` — thin wrapper that pins each role via `POST /api/forge/query {track_id: ...}`.

Reference plan: `docs/MODELFORGE_INTEGRATION_PLAN.md` §3.2, §6.

---

## Rollback this branch

Nothing to roll back. No production code touched. Three new files only — delete them.
