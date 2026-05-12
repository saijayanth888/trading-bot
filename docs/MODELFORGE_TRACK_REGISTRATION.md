# ModelForge Track Registration — Operator Runbook

> One-shot, idempotent registration of the 6 trading-bot LLM roles as
> ModelForge `evolution_tracks` rows. Once registered, ModelForge's
> LangGraph + APScheduler + Pareto-promotion loop ("Sunday champion run")
> owns the training/eval/promote lifecycle for each role.

**Script:** `scripts/modelforge_register_tracks.py`
**Tests:** `tests/test_modelforge_register.py`
**Branch:** `stage/modelforge-register-tracks`

---

## The 6 tracks

| `track_id` | Base | Target benchmarks (eval keys) | Schedule | Expected data path |
|---|---|---|---|---|
| `trading-reflector` | `qwen3:30b` | `faithfulness_regex`, `predictive_hit_rate_30d`, `judge_score`, `debate_impact` | weekly | `~/.dgx-train/datasets/trading-reflector/curated/` |
| `trading-bull` | `qwen3:30b` | `evidence_density`, `judge_preference` | weekly | `~/.dgx-train/datasets/trading-bull/curated/` |
| `trading-bear` | `qwen3:30b` | `evidence_density`, `judge_preference` | weekly | `~/.dgx-train/datasets/trading-bear/curated/` |
| `trading-arbiter` | `qwen3:30b` | `decision_consistency`, `downstream_pnl_per_decision`, `structured_output_validity` | weekly | `~/.dgx-train/datasets/trading-arbiter/curated/` |
| `trading-regime-tagger` | `qwen3:30b` | `structured_output_validity`, `agreement_with_hmm` | weekly | `~/.dgx-train/datasets/trading-regime-tagger/curated/` |
| `trading-indicator-selector` | `qwen3:30b` | `structured_output_validity`, `downstream_strategy_alpha` | weekly | `~/.dgx-train/datasets/trading-indicator-selector/curated/` |

Defaults baked into the POST body: `lora_rank=16`, `lora_alpha=32`,
`learning_rate=2e-4`, `max_samples=2000`, `enabled=true`. Edit the `TRACKS`
constant in the script if you need to override per-role.

---

## Pre-flight

1. **ModelForge must be running on `:8000`.** From the spark workspace:

   ```bash
   docker compose -f /home/saijayanthai/Documents/spark/workspace/model-forge/docker-compose.yml up -d
   ```

   Verify health:

   ```bash
   curl -s http://localhost:8000/api/system/health
   ```

2. **API key** (production only — dev runs leave the middleware open with a
   warning). Set one of:

   ```bash
   export MODELFORGE_API_KEY=<your-key>
   # or persist to ~/.env-modelforge:
   echo 'MODELFORGE_API_KEY=<your-key>' >> ~/.env-modelforge
   chmod 600 ~/.env-modelforge
   ```

3. **httpx must be importable.** Already a dep on this host; no install
   needed.

---

## Activation

```bash
python3 /home/saijayanthai/Documents/trading-bot/scripts/modelforge_register_tracks.py
```

Re-running is a no-op for tracks already registered. Sample output:

```
TRACK_ID                       STATUS               HTTP   MESSAGE
trading-reflector              created              201    HTTP 201
trading-bull                   created              201    HTTP 201
...
6/6 OK
```

### Preview only

```bash
python3 scripts/modelforge_register_tracks.py --dry-run
```

Prints the exact request body for each track without issuing a single HTTP
request.

### Register a single role

```bash
python3 scripts/modelforge_register_tracks.py --track trading-reflector
```

### Re-POST (force update)

```bash
python3 scripts/modelforge_register_tracks.py --force
```

Risk: ModelForge's current `upsert_track` honours `ON CONFLICT DO UPDATE`,
so this rewrites every column from the script's defaults — including
`enabled`, `lora_*`, and `learning_rate`. **Only use when intentionally
resetting a track.**

### Rollback (delete a track)

```bash
python3 scripts/modelforge_register_tracks.py --delete trading-reflector
```

Deletes a single track row. Cascade removes its `track_generations` rows
(per `lineage_db.py:544`). Adapters on disk are NOT touched.

---

## Verify

After running, confirm all 6 are present:

```bash
curl -s http://localhost:8000/api/forge/tracks \
  | jq '.tracks[] | .track_id'
```

Expected output (order may differ):

```
"trading-reflector"
"trading-bull"
"trading-bear"
"trading-arbiter"
"trading-regime-tagger"
"trading-indicator-selector"
```

Full row inspection:

```bash
curl -s http://localhost:8000/api/forge/tracks | jq '.tracks[] | select(.track_id | startswith("trading-"))'
```

---

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `Connection refused` | ModelForge not running | `docker compose -f .../model-forge/docker-compose.yml ps` and bring up if needed |
| `401 Missing or invalid X-API-Key` | Prod ModelForge with auth on; script saw no key | Export `MODELFORGE_API_KEY` or pass `--api-key` |
| `405 Method Not Allowed` on POST | ModelForge hasn't shipped `POST /api/forge/tracks` yet | See "Known gap" below |
| `404` on every GET, then `created` | Tracks really weren't there — expected on first run | None — that's the happy path |
| Track shows but rows differ from spec | Someone seeded a different row | Run with `--force` to reset to script defaults |

---

## Known gap — ModelForge POST endpoint

As of the most recent inspection of
`/home/saijayanthai/Documents/spark/workspace/model-forge/apps/api/src/api/routes/forge.py`,
ModelForge ships:

* `GET  /api/forge/tracks` — list (200 → `{tracks: [...]}`)
* `POST /api/forge/query`, `POST /api/forge/classify`, `POST /api/forge/compare`
* `POST /api/forge/sync_tracks`
* `POST /api/adapters/{id}/promote_to_track`

It does **not** yet expose `POST /api/forge/tracks` for creating rows.
`LineageDB.upsert_track()` is the underlying operation (`lineage_db.py:814`)
but is only called from `services/track_seed.py` on app startup with the 4
default tracks.

**Until ModelForge ships the POST endpoint**, this script will fall back
through one of these paths:

* **405** → the GET-existence check uses a fallback list-scan against
  `GET /api/forge/tracks` to determine idempotency.
* **The POST itself will 405** until ModelForge adds the route.

Two ways to seed the rows today, before the POST lands upstream:

1. **In-process seed (preferred)** — add this snippet to
   `services/track_seed.py:DEFAULT_TRACKS` (one entry per row from the table
   above) and bounce the API container. Idempotent: `seed_default_tracks`
   skips ids that already exist.

2. **Direct SQL** (escape hatch — operator only):
   ```sql
   INSERT INTO evolution_tracks (track_id, name, description, base_model,
       target_benchmarks, lora_rank, lora_alpha, learning_rate, max_samples,
       enabled)
   VALUES (...); -- one row per track
   ```

When ModelForge adds `POST /api/forge/tracks`, this script becomes the
authoritative path with no code changes.

---

## What runs next

Once the 6 rows exist:

* **trading-bot nightly cron** (already shipped in
  `scripts/modelforge_ingest.py` + `scripts/modelforge_curate.py`) writes
  HF Arrow shards to `~/.dgx-train/datasets/<track-id>/curated/gen-N/`.
* **ModelForge weekly cron** (Sunday 02:00 ET, internal to ModelForge)
  picks up new generations, trains a LoRA, evaluates against the track's
  `target_benchmarks`, and promotes via Pareto rules.
* **trading-bot inference path** calls `POST /api/forge/query` with
  `track_id` pinned to consume the promoted adapter.

No further manual registration steps. The script is one-shot by design.
