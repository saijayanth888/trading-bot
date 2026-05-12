# Weekly Training Card — operator runbook

**Card ID**: `00c` · mounts under TodayScoreboard (next to SharkOverrideHealthLive)
**Endpoint**: `GET /api/ops/weekly_training` (read-only, no auth)
**Refresh**: every 10s via `FAST_ENDPOINTS` polling group
**Source files**:
  - backend — `user_data/dashboard/ops_routes.py` (search `weekly_training`)
  - frontend — `user_data/dashboard/static/js/ops_spa.js` (search `WeeklyTrainingLive`)
  - template cache-bust — `user_data/dashboard/templates/ops_spa.html` → `?v=20260512-weekly-training`

---

## Why this card exists

Per `docs/4_WEEK_EXECUTION_PLAN.md` § "Per-role training cadence", every Sunday at 02:00 ET the [model-forge](https://github.com/saijayanthai/model-forge) repo trains 6 LoRA adapters — one per trading-bot LLM role — and promotes them via Pareto rules. The operator needs a single-glance view:

- Which adapters are promoted?
- When did this track last train?
- This-week's eval scores per role?
- How many reflections have been fed into next Sunday's training run?

This card is **also the viral screenshot** for the week 4 launch — "watch the AI learn" needs to be visible without scrolling.

---

## Card layout

```
┌──────────────────────────────────────────────────────────────────────────────────┐
│ 00c · Weekly training · LoRA adapters     [MODEL-FORGE LIVE · 3 PROMOTED] · 12s  │
│ model-forge @ http://localhost:8000 · Sun 02:00 ET refresh                       │
│                                                                                  │
│  REFLECTIONS THIS WEEK   LESSONS INJECTED   TRACKS TRAINED   NEXT TRAINING       │
│           12                    41               4 / 6             5d 14h        │
│      decisions.md          get_past_context  model-forge live   Sunday 02:00 ET  │
│  ─────────────────────────────────────────────────────────────────────────────── │
│  TRACK              ADAPTER / STATUS       LAST TRAIN     HEADLINE        EX     │
│  ─────────────────────────────────────────────────────────────────────────────── │
│  Reflector          v20260512 [PROMOTED]   Sun 06:14      0.620   pred-hit   47  │
│  Bull analyst       v20260512 [PROMOTED]   Sun 06:21      0.580   judge-pref 38  │
│  Bear analyst       v20260512 [SHADOW]     Sun 06:28      0.510   judge-pref 41  │
│  Portfolio mgr      v20260505 [ROLLED ⤺]   1w 0d ago      0.400   decisn-cns 22  │
│  Regime tagger      —         [NO DATA]    —              —       json-valid  —  │
│  Indicator selector —         [NO DATA]    —              —       sel-sharpe  —  │
│  ─────────────────────────────────────────────────────────────────────────────── │
│  promoted = Pareto-dominant on faithfulness + hit-rate · rolled back = regressed │
└──────────────────────────────────────────────────────────────────────────────────┘
```

When **model-forge is offline** the layout is identical, but:

- Header pip turns **orange** with label "MODEL-FORGE OFFLINE"
- Subhead reads "model-forge offline — local-only metrics shown"
- All 6 track rows render with `NO DATA` badges (gray)
- The **REFLECTIONS THIS WEEK** counter still works (reads `stocks/memory/decisions.md` directly)
- The **NEXT TRAINING** countdown still ticks (pure clock math)

This is the "degrade-soft" mode — landed BEFORE model-forge wires up so we have a visible "soon" UX during build-up.

---

## Color rules

| Eligibility | Pip | Badge | Meaning |
|---|---|---|---|
| `promoted` | green | `PROMOTED` | adapter promoted to champion this week, score improved |
| `shadow` | yellow | `SHADOW` | adapter promoted, eval flat — running in shadow mode |
| `regressed` | red | `ROLLED BACK` | adapter regressed; champion reverted to prior version |
| `no-data` | gray | `NO DATA` | track exists in registry; no training has happened yet |

Eligibility is derived server-side from each track's `last_run_status` field on `/api/forge/tracks` (search `_eligibility_for` in `ops_routes.py`).

---

## Envelope contract

```jsonc
{
  "status": "ok" | "degraded",
  "data": {
    "tracks": [
      {
        "track_id": "trading-reflector",
        "role": "Reflector",
        "headline_metric": "predictive_hit_rate_30d",
        "current_adapter": "run-abc__gen3" | null,
        "current_adapter_version": "v20260512" | null,
        "last_train_ts": "2026-05-12T06:14:00+00:00" | null,
        "last_eval_scores": {
          "faithfulness_regex": 0.81,
          "predictive_hit_rate_30d": 0.62,
          "judge_score": 0.74
        },
        "headline_score": 0.62 | null,
        "eligibility": "promoted" | "shadow" | "regressed" | "no-data",
        "examples_trained_this_week": 47
      }
      // ... 6 tracks total, in canonical order
    ],
    "summary": {
      "n_tracks_registered": 6,
      "n_tracks_trained": 4,
      "n_promoted_this_week": 2
    },
    "reflections_this_week": 12,
    "lessons_injected": 41,            // null if llm-calls.jsonl absent
    "next_training_ts": "2026-05-18T06:00:00+00:00",
    "model_forge_url": "http://localhost:8000",
    "model_forge_reachable": true,
    "model_forge_error": null,
    "week_started": "2026-05-12T00:00:00+00:00"
  },
  "error": null | "human-readable string",
  "checked_at": "2026-05-12T08:30:00+00:00"
}
```

Status logic:

- `"degraded"` when model-forge is unreachable OR no track has been trained yet (early build-up week)
- `"ok"` otherwise

Both 6 tracks always come back, in canonical order, even when model-forge is empty/offline.

---

## Canonical track order (don't change)

The frontend stability + viral-screenshot consistency depends on this order:

1. `trading-reflector` — headline metric `predictive_hit_rate_30d`
2. `trading-bull` — headline metric `judge_preference_pct`
3. `trading-bear` — headline metric `judge_preference_pct`
4. `trading-arbiter` — headline metric `decision_consistency`
5. `trading-regime-tagger` — headline metric `json_schema_validity_rate`
6. `trading-indicator-selector` — headline metric `selected_indicator_avg_sharpe`

This list lives in `_WEEKLY_TRAINING_TRACKS` at the top of the endpoint section in `ops_routes.py`. The headline metric is what the card's "Headline score" column shows — pick from `last_eval_scores` server-side so the frontend stays dumb.

---

## How to test locally

Two paths — pick one.

### Path 1: real model-forge

```bash
# in spark/workspace/model-forge/
docker compose up -d api
curl http://localhost:8000/api/forge/tracks  # confirm it answers
```

Then load `http://localhost:8002/ops_spa` — card should show all 6 tracks.

### Path 2: spoof model-forge with a static JSON server (recommended for the screenshot)

```bash
# create a temp dir with a fake tracks endpoint
mkdir -p /tmp/mf-spoof/api/forge
cat > /tmp/mf-spoof/api/forge/tracks <<'JSON'
{
  "tracks": [
    {
      "track_id": "trading-reflector",
      "champion_adapter_path": "data/adapters/run-abc/gen-3",
      "champion_run_id": "run-abc__gen3",
      "champion_promoted_at": "2026-05-12T06:14:00+00:00",
      "champion_scores": {
        "faithfulness_regex": 0.81,
        "predictive_hit_rate_30d": 0.62,
        "judge_score": 0.74
      },
      "last_train_num_samples": 47
    },
    {
      "track_id": "trading-bull",
      "champion_adapter_path": "data/adapters/run-xyz/gen-1",
      "champion_promoted_at": "2026-05-12T06:21:00+00:00",
      "champion_scores": {"judge_preference_pct": 0.58},
      "max_samples": 38
    },
    {
      "track_id": "trading-bear",
      "champion_adapter_path": "data/adapters/run-bear/gen-2",
      "champion_promoted_at": "2026-05-12T06:28:00+00:00",
      "last_run_status": "shadow_promoted",
      "champion_scores": {"judge_preference_pct": 0.51},
      "max_samples": 41
    },
    {
      "track_id": "trading-arbiter",
      "champion_adapter_path": "data/adapters/run-arb/gen-1",
      "champion_promoted_at": "2026-05-05T06:14:00+00:00",
      "last_run_status": "regressed_rollback",
      "champion_scores": {"decision_consistency": 0.40},
      "max_samples": 22
    }
  ]
}
JSON

# serve it on :8000
cd /tmp/mf-spoof && python -m http.server 8000
```

In another shell, point the dashboard at this spoof:

```bash
export MODELFORGE_API_URL=http://localhost:8000
# restart your dashboard container (or run uvicorn) — the env var is read at import time
```

Open `/ops_spa` and you should see Reflector + Bull green, Bear yellow, Arbiter red, RegimeTagger + IndicatorSelector gray.

### Path 3: pure offline (degrade-soft check)

Just make sure nothing is listening on `:8000` (or set `MODELFORGE_API_URL=http://localhost:9999`). Reload `/ops_spa`. The card should show an orange "MODEL-FORGE OFFLINE" pip, the reflections counter should still work if `stocks/memory/decisions.md` has any blocks from this week, and the Sunday 02:00 ET countdown should tick.

---

## Reflection counter — what it counts

The `reflections_this_week` field reads `stocks/memory/decisions.md` and counts `REFLECTION: <body>` lines whose enclosing block bears a date `>=` Monday 00:00 UTC of this week.

- "This week" rolls over Mondays so the count resets just after Sunday 02:00 ET training fires (Sunday 06:00–07:00 UTC). The operator sees "X reflections trained last night, Y new since" cleanly.
- Empty `REFLECTION:` lines (still-pending trades) are skipped — only filled-in reflections feed training.
- The file is missing on fresh worktrees; missing → 0 (not an error).

The card's headline summary stat reads this number — it's the **only** signal that works without model-forge being up, so it's the key "even when offline, you can still see the bot will train tomorrow" hook.

---

## Cache-bust mechanics

When you change `ops_spa.js`, you must also bump the version query in `ops_spa.html`:

```html
<script src="/static/js/ops_spa.js?v=20260512-weekly-training"></script>
```

Without the bump, browsers (and Cloudflare/the operator's iPad) serve the cached old JS and your new card silently fails to mount. See `MEMORY.md → reference_dashboard_deploy.md` for the full deploy mechanics.

---

## Future enhancements (not in this card)

Once model-forge is wired live we should:

1. Add an "open in model-forge" link per row that deep-links to that track's lineage page (e.g. `http://localhost:3001/tracks/trading-reflector`).
2. Surface the per-track delta vs prior champion (sparkline of last 4 generations) — this is the "watch it learn" GIF candidate.
3. Click a row → expandable detail with all eval scores + the curated dataset size + train log tail.
4. Slack alert when any track flips to `regressed` so the operator doesn't have to be staring at the dashboard.

Tracked in `docs/4_WEEK_EXECUTION_PLAN.md` week 3 ("compounding + launch prep") tasks.
