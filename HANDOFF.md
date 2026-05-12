# HANDOFF — fix/frontend-tier-e-agent-logs

**Branch:** `fix/frontend-tier-e-agent-logs` (off `fix/frontend-tier-d-agent-flow`)
**Cache-bust version:** `20260512-tier-e-agent-logs`

## What you built

A new `AgentLogsDrawer` component that replaces the Tier-D scroll-and-pulse behavior when an operator clicks an agent box in the AgentFlow strip. The drawer slides in from the right (480 px on desktop, 90 vw on mobile), renders the last 50 calls for that one canonical role with FULL prompt + response, and adds filter chips (`all / success / failures / slow`) + a 200 ms debounced search across prompt + response. Each entry collapses prompt + response to a 120-char snippet by default — click the caret or snippet to expand to a scrollable `<pre>` block (max-height 400 px); copy buttons on each pulse green for 600 ms on success. Mounts via `ReactDOM.createPortal` to `document.body` so the AgentFlow strip layout never reflows. Backdrop click + ESC close the drawer and return focus to the originating box. Each AgentFlow box also gains one new inline row at the bottom — `last: "..."` — showing 60 chars of the most recent response, render-skipped entirely when the role has no calls today. The Tier-D scroll-and-pulse behavior is preserved as an opt-out fallback via a new localStorage flag. No new poll loop; the drawer fetches `/api/ops/llm_calls?role=ROLE&include_text=1&limit=50` exactly once per open.

## ASCII mockup

```
+------------------------------ AGENT LOGS - bull_debater . hermes3:8b ----[x]+
|                                                                             |
|  today: 12 v . 1 x . avg 5.1s . p95 8.3s . 47 tokens/call avg              |
|                                                                             |
|  [ all (13) ]  [ success (12) ]  [ failures (1) ]  [ slow (2) ]            |
|  [ search prompt + response...                                       ]     |
|                                                                             |
|  +-------------------------------------------------------------------+      |
|  | 14:32:11  hermes3:8b  v  5.2s  340 tok                            |     |
|  | > prompt: "Given regime=trending_up and BTC..."          [copy]   |     |
|  | > response: "bull lean, confidence 0.72..."              [copy]   |     |
|  +-------------------------------------------------------------------+      |
|  | 14:21:08  hermes3:8b  v  4.8s  295 tok                            |     |
|  | > prompt: "..."                                          [copy]   |     |
|  | > response: "..."                                        [copy]   |     |
|  +-------------------------------------------------------------------+      |
|  | 13:09:47  hermes3:8b  x  0.6s  0 tok      [SLOW]                  |     |
|  | > prompt: "..."                                          [copy]   |     |
|  | > response: "(empty - request timed out)"                [copy]   |     |
|  +-------------------------------------------------------------------+      |
|                                                                             |
|  [ show 30 more (27 hidden) ]                                              |
|                                                                             |
+-----------------------------------------------------------------------------+

Backdrop dim layer (rgba 0,0,0,.35) behind drawer; click or press ESC to close.
```

Each AgentFlow box also gains a fourth row below the gist:

```
+-----------------+
| bull_debater    |
| hermes3:8b      |
| * 2m ago        |
| 18 v . 0 x      |
| avg 1.8s        |
| AAPL presents.. |  <- existing .af-gist
| last: "trending |  <- NEW Tier-E .af-last (11 px mono, render-skipped if empty)
|  _up, conf 0.74"|
+-----------------+
```

## Files changed

| File | Lines | Purpose |
|---|---|---|
| `user_data/dashboard/ops_routes.py` | +26 / -0 | `?role=` query filter + `tokens_avg` + `last_response_gist` in `by_role_detail` |
| `user_data/dashboard/static/css/quanta.css` | +285 / -0 | `.ald-*` drawer family + `.af-box .af-last` inline row |
| `user_data/dashboard/static/js/ops_spa.js` | +438 / -9 | `AgentLogsDrawer` + `_AldEntry` + helpers + `AgentFlowBox` ref/last-line + dispatch event + mount |
| `user_data/dashboard/templates/ops_spa.html` | +2 / -2 | Cache-bust to `20260512-tier-e-agent-logs` |

Total: **751 insertions, 11 deletions** across 4 files, 5 commits.

## Endpoint extension — before / after

The `/api/ops/llm_calls` endpoint gained ONE new query parameter and TWO new keys in the existing `summary.by_role_detail` shape. All previously-shipped fields are unchanged.

### Query parameters — before

```
GET /api/ops/llm_calls?limit=50&agent=&since=&q=&include_text=0
                     &model=&min_latency=&max_latency=
```

### Query parameters — after (one new key)

```
GET /api/ops/llm_calls?limit=50&agent=&since=&q=&include_text=0
                     &model=&min_latency=&max_latency=
                     &role=bull_debater          <-- NEW
```

When `role` is set, only records whose raw `agent` field maps to that canonical role (via `_canonical_role()`) are returned. Substring filter on `agent` is orthogonal — operator can still pass either.

The drawer always opens with `?role={role}&include_text=1&limit=50` so the prompt + response are present in each row.

### `summary.by_role_detail[role]` shape — before

```json
{
  "count": 18, "success": 18, "fail": 0,
  "avg_latency_s": 1.8, "p95_latency_s": 4.2,
  "last_ts": "...", "last_success": true,
  "last_gist": "...", "last_agent": "analyst_bull",
  "model": "hermes3:8b",
  "raw_agents": {"analyst_bull": 12, "debate.bull.r1": 6}
}
```

### `summary.by_role_detail[role]` shape — after (two new keys)

```json
{
  "count": 18, "success": 18, "fail": 0,
  "avg_latency_s": 1.8, "p95_latency_s": 4.2,
  "tokens_avg": 295.5,                    // NEW: mean completion_tokens
  "last_ts": "...", "last_success": true,
  "last_gist": "...",                      // unchanged
  "last_response_gist": "...",             // NEW: same value, drawer-spec name
  "last_agent": "analyst_bull",
  "model": "hermes3:8b",
  "raw_agents": {"analyst_bull": 12, "debate.bull.r1": 6}
}
```

`last_gist` is kept as a duplicate so the Tier-D AgentFlow strip's existing read path doesn't break; `last_response_gist` is the Tier-E spec name and is what the new inline preview line reads (with a fallback to `last_gist`).

## Commit SHAs

| SHA | Subject |
|---|---|
| `6528a7f` | ops: add ?role= filter + tokens_avg to /api/ops/llm_calls (Tier-E) |
| `74c90a5` | css: AgentLogsDrawer + inline last-preview rules (Tier-E) |
| `6f4fbfd` | ops_spa: add AgentLogsDrawer component (Tier-E) |
| `5d11c93` | ops_spa: wire AgentFlow box click to AgentLogsDrawer + inline last-line |
| `77bd781` | templates: cache-bust quanta.css + ops_spa.js to tier-e-agent-logs |

Branch is **NOT pushed**.

## Two opt-outs

Both opt-outs survive page reloads. Re-enable by `removeItem` then `location.reload()`.

| Flag | Effect |
|---|---|
| `localStorage.setItem("quanta.agent_flow", "0")` | **Inherited from Tier D.** Hides the entire AgentFlow strip — the LLM Activity card below renders identically to pre-Tier-D. The inline "last:" preview line goes away with the strip; the drawer is unreachable. |
| `localStorage.setItem("quanta.agent_logs_drawer", "0")` | **New in Tier E.** Disables the drawer but keeps the strip and the inline preview line. Clicking an agent box falls back to the Tier-D scroll-and-pulse behavior on the LLM Activity list below. |

## Known limits + future enhancements

**Known limits:**

1. **No live tailing.** The drawer fetches once on open. If a new call lands while the drawer is open, the operator has to close + re-open (or click a different role then back) to see it. Adding a small EventSource / SSE tail is feasible but explicitly out of scope for this tier.
2. **Search has no in-text highlight.** Matches narrow the rows but matched text inside expanded `<pre>` blocks is not visually highlighted. Highlighting would require DOM string surgery on the prompt/response; out of scope here.
3. **Records written before `SHARK_LLM_LOG_FULL_TEXT=1` have no prompt/response.** The entry still renders (timestamp + model + status + latency + tokens) with a one-line note: `no prompt/response captured (SHARK_LLM_LOG_FULL_TEXT was off)`. Same constraint that already affects the LLM Activity modal.
4. **Aggregate row reads from `by_role_detail`** (which uses the 24h window — same source the strip already reads). The 50 calls listed in the body may be a SUBSET of those 24h aggregates if a role fires more than 50 times per day; counts displayed in filter chips are computed only over the loaded 50.
5. **Pagination is one-direction.** `show 30 more` appends; there is no "show fewer". Operator can close + reopen to reset.
6. **Drawer always re-fetches on different role,** even if the new role was loaded earlier in the session. No client-side cache — keeps the implementation small and the data fresh. Trivial to add a `Map<role, calls>` cache later.

**Future enhancements:**

1. **Live tailing while open** — open an EventSource on `/api/ops/llm_calls_stream?role=…` (would need a new backend endpoint), prepend new rows as they arrive.
2. **In-text search highlight** — wrap matches in `<mark>` inside expanded `<pre>` blocks.
3. **Export to clipboard** — "copy all visible" button that dumps the filtered set as JSONL.
4. **Per-call jump** — clicking a row in the drawer opens the existing LLMCallModal for that timestamp.
5. **Sticky aggregate row** — pin the today: line at the top while scrolling the body.
6. **Keyboard nav** — `j`/`k` to move between entries, `Enter` to expand both prompt + response of the focused entry.

## Verification checklist

- [x] Branched from Tier D's tip; Tier-D commits present (`git log --oneline -10`)
- [x] Python compiles (`python3 -m py_compile ops_routes.py`)
- [x] JS parses (`node --check ops_spa.js`)
- [x] Cache-bust bumped on both CSS and JS
- [x] No new fetch URLs (drawer fetches `/api/ops/llm_calls` — same endpoint, new `role=` param)
- [x] Tier-D behavior preserved when `quanta.agent_logs_drawer === "0"`
- [x] One atomic commit per concern (5 commits)
- [ ] **Operator-side:** hard refresh `http://localhost:8081/ops`, click each role, walk the 10-step verification list from the task spec

## Cache-bust version

`?v=20260512-tier-e-agent-logs` on both `quanta.css` and `ops_spa.js` in `templates/ops_spa.html`.
