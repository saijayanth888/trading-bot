# HANDOFF — `stage/weekly-training-card`

**Date**: 2026-05-12
**Branch**: `stage/weekly-training-card` (isolated worktree — DO NOT merge yet)
**Scope**: WeeklyTrainingLive dashboard card for the trading-bot ops page.

---

## What landed

Five files touched:

| File | Change |
|---|---|
| `user_data/dashboard/ops_routes.py` | **+ ~270 lines** at the bottom — new `GET /api/ops/weekly_training` endpoint + 6 helper functions. |
| `user_data/dashboard/static/js/ops_spa.js` | **+ ~250 lines** — new `WeeklyTrainingLive` + 3 sub-components (`WeeklyTrainingSummary`, `WeeklyTrainingTrackRow`, `WeeklyTrainingRelTime`); registered in `FAST_ENDPOINTS`; mounted under SharkOverrideHealthLive. |
| `user_data/dashboard/templates/ops_spa.html` | cache-bust bumped: `?v=20260512-merged-cutover20` → `?v=20260512-weekly-training` |
| `tests/test_weekly_training_endpoint.py` | **NEW** — 16 unit tests, all passing |
| `docs/WEEKLY_TRAINING_CARD.md` | **NEW** — operator runbook with screenshot description |

No other ops cards touched. No trading-running paths touched. The endpoint is read-only, no auth dep.

---

## ASCII sketch of the card layout

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

When model-forge is offline: header pip turns **orange** with label `MODEL-FORGE OFFLINE`, all 6 track rows show gray `NO DATA` badges, but the **REFLECTIONS THIS WEEK** counter + the **NEXT TRAINING** countdown still work (pure local-file + clock-math).

---

## Endpoint contract (envelope shape)

```jsonc
GET /api/ops/weekly_training

{
  "status": "ok" | "degraded",
  "data": {
    "tracks": [
      {
        "track_id":                  "trading-reflector",
        "role":                      "Reflector",
        "headline_metric":           "predictive_hit_rate_30d",
        "current_adapter":           "run-abc__gen3" | null,
        "current_adapter_version":   "v20260512" | null,
        "last_train_ts":             "2026-05-12T06:14:00+00:00" | null,
        "last_eval_scores":          { "faithfulness_regex": 0.81, ... },
        "headline_score":            0.62 | null,
        "eligibility":               "promoted" | "shadow" | "regressed" | "no-data",
        "examples_trained_this_week": 47
      }
      // ... 6 tracks total, in canonical order
    ],
    "summary": {
      "n_tracks_registered":   6,
      "n_tracks_trained":      4,
      "n_promoted_this_week":  2
    },
    "reflections_this_week":   12,
    "lessons_injected":        41,         // null if llm-calls.jsonl absent
    "next_training_ts":        "2026-05-18T06:00:00+00:00",
    "model_forge_url":         "http://localhost:8000",
    "model_forge_reachable":   true,
    "model_forge_error":       null,
    "week_started":            "2026-05-12T00:00:00+00:00"
  },
  "error":      null | "human-readable string",
  "checked_at": "2026-05-12T08:30:00+00:00"
}
```

**Status logic**:

- `"degraded"` when model-forge unreachable OR no track has been trained yet (early build-up week — card still renders with a clear "training pipeline starting up" message)
- `"ok"` otherwise

**Canonical order** (don't change — viral-screenshot stability):

1. `trading-reflector` (headline: `predictive_hit_rate_30d`)
2. `trading-bull` (headline: `judge_preference_pct`)
3. `trading-bear` (headline: `judge_preference_pct`)
4. `trading-arbiter` (headline: `decision_consistency`)
5. `trading-regime-tagger` (headline: `json_schema_validity_rate`)
6. `trading-indicator-selector` (headline: `selected_indicator_avg_sharpe`)

---

## Color rules

| Eligibility | Pip | Badge | Meaning |
|---|---|---|---|
| `promoted` | green | `PROMOTED` | adapter promoted to champion this week |
| `shadow` | yellow | `SHADOW` | adapter promoted, eval flat — shadow mode |
| `regressed` | red | `ROLLED BACK` | adapter regressed; champion reverted |
| `no-data` | gray | `NO DATA` | track registered, no training has happened |

Server-side mapping lives in `_eligibility_for()` in `ops_routes.py` — it inspects each track's `last_run_status` substring.

---

## How to test locally

### Option A: spoof model-forge with `python -m http.server`

```bash
mkdir -p /tmp/mf-spoof/api/forge

cat > /tmp/mf-spoof/api/forge/tracks <<'JSON'
{
  "tracks": [
    {
      "track_id": "trading-reflector",
      "champion_adapter_path": "data/adapters/run-abc/gen-3",
      "champion_promoted_at": "2026-05-12T06:14:00+00:00",
      "champion_scores": { "predictive_hit_rate_30d": 0.62, "faithfulness_regex": 0.81 },
      "last_train_num_samples": 47
    },
    {
      "track_id": "trading-bull",
      "champion_adapter_path": "data/adapters/run-xyz/gen-1",
      "champion_promoted_at": "2026-05-12T06:21:00+00:00",
      "champion_scores": { "judge_preference_pct": 0.58 },
      "max_samples": 38
    },
    {
      "track_id": "trading-bear",
      "champion_adapter_path": "data/adapters/run-bear/gen-2",
      "champion_promoted_at": "2026-05-12T06:28:00+00:00",
      "last_run_status": "shadow_promoted",
      "champion_scores": { "judge_preference_pct": 0.51 },
      "max_samples": 41
    },
    {
      "track_id": "trading-arbiter",
      "champion_adapter_path": "data/adapters/run-arb/gen-1",
      "champion_promoted_at": "2026-05-05T06:14:00+00:00",
      "last_run_status": "regressed_rollback",
      "champion_scores": { "decision_consistency": 0.40 },
      "max_samples": 22
    }
  ]
}
JSON

cd /tmp/mf-spoof && python -m http.server 8000
```

In another shell:

```bash
export MODELFORGE_API_URL=http://localhost:8000
# restart the dashboard container so it picks up the env var
# OR run uvicorn locally pointing at the same port
```

Then load `http://localhost:8002/ops_spa`. You should see:

- Reflector + Bull green (PROMOTED)
- Bear yellow (SHADOW)
- Arbiter red (ROLLED BACK)
- RegimeTagger + IndicatorSelector gray (NO DATA)

### Option B: pure offline (degrade-soft check)

Make sure nothing is listening on `:8000` (or set `MODELFORGE_API_URL=http://localhost:9999`). Reload `/ops_spa`. The card should still render with:

- Orange `MODEL-FORGE OFFLINE` pip
- All 6 track rows showing gray `NO DATA`
- Reflections counter working if `stocks/memory/decisions.md` has any blocks from this week
- Sunday 02:00 ET countdown ticking

### Option C: run the test suite

```bash
cd /home/saijayanthai/Documents/trading-bot/.claude/worktrees/agent-a19652a6c66305154
python -m pytest tests/test_weekly_training_endpoint.py -v
```

All 16 tests should pass (envelope shape · happy-path tracks · MF unreachable · MF timeout · MF 500 · empty/this-week/old reflections · skip-empty-reflections · lessons None vs counted · regressed mapping · shadow mapping · bare-list response · no-auth · next-training in future).

---

## Operator note — viral screenshot

This card is **the** viral screenshot for the week 4 launch. Per `docs/4_WEEK_EXECUTION_PLAN.md`:

> Week 4 viral moment: the launch itself. ... "Watch the AI learn."

Keep it pixel-perfect — matches the dYdX/Geist aesthetic the rest of the SPA uses:

- mono numerics with `font-variant-numeric: tabular-nums`
- no shadows, no gradients, no serif-italic
- subtle 1px hairlines via `var(--line-1)`
- header pip pulses only when state is "good" (green)
- badge colors use the shared `--c-up / --c-warn / --c-down / --c-info` palette

The card is mounted **above the fold** under TodayScoreboard / SharkOverrideHealthLive so it's visible without scrolling. Take the screenshot at 1920×1080 with the browser at 100% zoom, dark theme (default), Geist density.

---

## Cache-bust

Bumped:

```html
<!-- before -->
<script src="/static/js/ops_spa.js?v=20260512-merged-cutover20"></script>

<!-- after -->
<script src="/static/js/ops_spa.js?v=20260512-weekly-training"></script>
```

Without this bump, browsers serve the cached old JS and the new card silently fails to mount.

---

## Future work (NOT in this PR)

Once model-forge is live and producing adapters:

1. Add "open in model-forge" deep-link per row → `http://localhost:3001/tracks/<track_id>`
2. Per-track sparkline of last-N generation scores (the "watch it learn" GIF candidate)
3. Click-to-expand row → all eval scores + curated dataset size + train log tail
4. Slack alert on any track flipping to `regressed`

Tracked in `docs/4_WEEK_EXECUTION_PLAN.md` week 3 ("compounding + launch prep").

---

## Files referenced in this handoff

- `docs/4_WEEK_EXECUTION_PLAN.md` — § "Per-role training cadence"
- `docs/MODELFORGE_INTEGRATION_PLAN.md` — § "Architecture", § "Pipeline"
- `docs/WEEKLY_TRAINING_CARD.md` — operator runbook (this card)
- `user_data/dashboard/ops_routes.py` — `weekly_training()` at bottom of file
- `user_data/dashboard/static/js/ops_spa.js` — `WeeklyTrainingLive` component
- `tests/test_weekly_training_endpoint.py` — 16-test suite
- `stocks/memory/decisions.md` — reflection counter input source
- `stocks/memory/llm-calls.jsonl` — lessons-injected input source (optional)

---

## Pre-merge checklist (for whoever picks this up)

- [x] All 16 endpoint tests passing (`pytest tests/test_weekly_training_endpoint.py -v`)
- [x] No regression in existing tests (`pytest tests/test_ops_dashboard.py -v` — 20/20 pass)
- [x] JS syntax-clean (`node -c user_data/dashboard/static/js/ops_spa.js`)
- [x] Cache-bust bumped to unique value
- [x] Endpoint is read-only (no `Depends(require_mcp_key)` — test asserts this)
- [x] Card degrades soft when model-forge is offline
- [ ] Visual smoke against dev dashboard with spoofed model-forge (operator to confirm)
- [ ] Screenshot captured for week 4 launch deck (operator to capture)
