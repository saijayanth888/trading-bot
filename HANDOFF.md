# HANDOFF — fix/frontend-tier-d-agent-flow

**Cache-bust version:** `20260512-tier-d-agent-flow`

## What you built

A new `AgentFlow` strip on the ops dashboard, sitting directly above the existing LLM Activity list. It renders 5–6 horizontally-arranged boxes for the canonical trading-bot pipeline (`regime_tagger → indicator_selector? → bull_debater → bear_debater → arbiter → reflector`), each showing the live count, success/fail tally, avg + p95 latency, last-fired age, top model, and one-line gist of the most recent response for that role. Box borders encode freshness via existing CSS tokens (`up-line` < 5 min, `warn-line` 5–60 min, `line-2` > 60 min / empty, `down-line` when the latest call failed — red wins regardless of freshness). Clicking a box dispatches a `quanta:agent-flow-pick` CustomEvent that the unchanged `LLMCallsLive` component listens for, scrolling its table to the newest matching row and pulsing it for 800 ms via the new `af-pulse-row` class. No new poll, no new endpoint — the strip piggybacks on the existing 10 s `useOpsData` tick by consuming a new `summary.by_role_detail` block added to `/api/ops/llm_calls`.

## ASCII mockup

```
+-------------------------------------------------------------------------------------------------+
| 21a  Agent flow                                          3 of 5 roles active * 47 calls in 24h  |
+-------------------------------------------------------------------------------------------------+
| +-------------+ 2.4s   +-------------+ 1.8s   +-------------+ 3.1s   +-------------+   +------+ |
| |regime_tagger|--->--->|bull_debater |--->--->|bear_debater |--->--->|   arbiter   |-->|refle.| |
| | -           |        | hermes3:8b  |        | hermes3:8b  |        | hermes3:8b  |   |   -  | |
| | no calls    |        | * 2m ago    |        | o 4m ago    |        | * 1m ago    |   | no.. | |
| | -           |        | 18 v * 0 x  |        | 17 v * 1 x  |        | 12 v * 0 x  |   |   -  | |
| | -           |        | avg 1.8s    |        | avg 2.3s    |        | avg 3.1s    |   |   -  | |
| | no calls    |        | p95 4.2s    |        | p95 6.1s    |        | p95 7.4s    |   | no.. | |
| +-------------+        +-------------+        +-------------+        +-------------+   +------+ |
|  (dim border)            (green)               (green)                (green)            (dim)  |
+-------------------------------------------------------------------------------------------------+
+-------------------------------------------------------------------------------------------------+
| 21   LLM activity * last 24h                                       (unchanged - existing list)  |
| ...                                                                                             |
+-------------------------------------------------------------------------------------------------+
```

Border colors at a glance:

- **green** border (`var(--up-line)`) — last call within 5 minutes
- **amber** border (`var(--warn-line)`) — 5–60 minutes since last call
- **dim** border (`var(--line-2)`) — > 60 minutes or zero calls
- **red** border (`var(--down-line)`) — last call failed (wins regardless of freshness)
- **empty** background (`var(--bg-rail)`) — zero calls in 24h window

## Files changed

| File | Lines | Purpose |
|---|---|---|
| `user_data/dashboard/ops_routes.py` | +118 / -4 | `_canonical_role()` + `by_role_detail` in `_summarise_llm_window()` |
| `user_data/dashboard/static/css/quanta.css` | +135 / -0 | `/* AgentFlow */` section + `@keyframes pulse-row` |
| `user_data/dashboard/static/js/ops_spa.js` | +255 / -0 | `AgentFlow` component, helpers, pick-event listener in `LLMCallsLive`, integration |
| `user_data/dashboard/templates/ops_spa.html` | +2 / -2 | Cache-bust `quanta.css` + `ops_spa.js` to `20260512-tier-d-agent-flow` |

Total: **510 insertions, 6 deletions** across 4 files.

## Endpoint extension — before / after

The existing `/api/ops/llm_calls` payload is unchanged in shape EXCEPT for one new key inside `data.summary`:

**Before:**

```json
{
  "status": "ok",
  "data": {
    "calls": [...],
    "summary": {
      "total_calls": 47,
      "avg_latency_s": 2.4,
      "p95_latency_s": 5.1,
      "by_agent": {"analyst_bull": 18, "analyst_bear": 17, "...": "..."},
      "by_model": {"hermes3:8b": 47},
      "by_tier": {"fast": 0, "deep": 47},
      "ollama_pct": 100.0,
      "success_pct": 100.0
    }
  }
}
```

**After (new key shown):**

```json
{
  "status": "ok",
  "data": {
    "calls": ["..."],
    "summary": {
      "total_calls": 47, "avg_latency_s": 2.4, "...": "...",
      "by_role_detail": {
        "bull_debater": {
          "count": 18, "success": 18, "fail": 0,
          "avg_latency_s": 1.8, "p95_latency_s": 4.2,
          "last_ts": "2026-05-12T14:45:18.105182+00:00",
          "last_success": true,
          "last_gist": "AAPL presents an attractive high-reward opportunity with strong upside potential...",
          "last_agent": "analyst_bull",
          "model": "hermes3:8b",
          "raw_agents": {"analyst_bull": 12, "debate.bull.r1": 6}
        },
        "bear_debater": {"...": "..."},
        "arbiter":      {"...": "..."}
      }
    }
  }
}
```

Raw-agent → canonical-role mapping (in `ops_routes.py`):

| Canonical role | Mapped raw agents |
|---|---|
| `regime_tagger` | `regime_tagger`, `trading-regime-tagger` |
| `indicator_selector` | `indicator_selector` |
| `bull_debater` | `analyst_bull`, `debate.bull.*` |
| `bear_debater` | `analyst_bear`, `debate.bear.*` |
| `arbiter` | `decision_arbiter`, `debate.arbiter`, `combined_analyst`, `risk_debate.judge`, `trade_reviewer` |
| `reflector` | `outcome_resolver`, `reflector` |

`risk_debate.aggressive`/`.conservative`/`.neutral` deliberately do NOT map to any flow role — they're parallel personalities, not pipeline stages. They still appear in the unchanged LLM activity list below.

## Commit SHAs

| SHA | Subject |
|---|---|
| `6c58006` | ops: extend /api/ops/llm_calls summary with by_role_detail |
| `10837fc` | css: add AgentFlow strip rules + pulse-row keyframe |
| `83ed9f9` | ops_spa: add AgentFlow component + pulse-row hook in LLMCallsLive |
| `a49ec98` | ops_spa: mount AgentFlow above LLM activity + bump cache-bust |

Branch: `fix/frontend-tier-d-agent-flow` — **NOT pushed**. Local worktree at `.claude/worktrees/agent-a97b2db35e5b9e9b1/`.

## How to disable in one console line

```js
localStorage.setItem("quanta.agent_flow", "0"); location.reload();
```

The strip disappears entirely (component returns `null`); the LLM Activity list below renders identically. To re-enable: `localStorage.removeItem("quanta.agent_flow"); location.reload();`.

## Known limits + future enhancements

**Known limits:**

1. **`regime_tagger` always shows "no calls today"** in the current bot configuration. The agent doesn't exist in the production codepath yet — only in `WeeklyTrainingLive` planning copy and an unrelated test file. The placeholder box is correct behavior per spec (operators see which stage isn't firing), but until the agent goes live the leftmost box will stay dim.
2. **`indicator_selector` is omitted entirely** when it has zero calls (also currently the case). This is per the spec's edge-case rule — when it goes live and starts logging, the strip will gain a sixth box automatically.
3. **Hop-latency chip between boxes** uses the destination role's avg latency as a proxy ("how long does the next stage take") — not literal hop time. The ledger doesn't currently log a "previous-call → this-call" delta, so a true inter-stage hop would need a new field (e.g. `decision_id` shared across the bull→bear→arbiter chain for a single trade).
4. **Strip re-renders every 30 s** for age-label freshness via a local `setInterval`. This is a NO-OP state tick (no fetch, no DOM thrash on stable data), but it does add one extra render every 30 s when the strip is on screen.
5. **Last-gist on stripped (metadata-only) payloads:** the backend computes `last_gist` from the FULL `window_24h` records (with `response_text` intact) — but if `SHARK_LLM_LOG_FULL_TEXT=0` was set when the call was logged, `response_text` is absent and `last_gist` falls back to `"—"`. Same constraint that already affects the modal.
6. **Single role with 50+ calls:** the box renders aggregates only (count, avg, p95, last) — no per-call list. Full list lives in the LLM Activity card below, per spec.

**Future enhancements:**

1. **Real hop latency** — add a `decision_id` to ledger records so we can compute the actual gap between bull → bear → arbiter calls for a single trade.
2. **Per-role sparkline** — a 12-bar histogram of latency over the last hour, mounted inside each box at the bottom edge.
3. **Click-to-pin** — Shift-click a box to filter the LLM activity list below to just that role, rather than only scrolling.
4. **Strip auto-hides** when all six roles show "no calls today" so the operator isn't staring at six empty boxes during off-hours.
5. **Failure-pulse on box** — when a box flips to `is-fail` between polls, briefly flash the border red (single 200 ms keyframe).
6. **Per-role token throughput** — a small `~N tok/s` chip in the corner so operators see model utilization, not just latency.

## Verification checklist

- [x] Strip renders ABOVE the LLM Activity list
- [x] LLM Activity list renders identically to before in every code path (only addition: `data-llm-ts` + `data-llm-agent` attributes on each row, used by the strip's click handler)
- [x] No new fetch URLs (strip + list share `/api/ops/llm_calls`)
- [x] Clean Python compile (`python3 -m py_compile ops_routes.py`)
- [x] Clean JS parse (`node --check ops_spa.js`)
- [x] Cache-bust bumped on both CSS and JS
- [x] Click on a box dispatches `quanta:agent-flow-pick`; listener scrolls + pulses
- [x] Opt-out flag honored (`localStorage.quanta.agent_flow === "0"` returns `null`)
- [ ] **Operator-side:** hard refresh `http://localhost:8081/ops`, confirm strip appears, click each role, confirm Network tab shows no extra requests at rest over 30 s before vs after

## Cache-bust version

`?v=20260512-tier-d-agent-flow` on both `quanta.css` and `ops_spa.js` in `templates/ops_spa.html`.
