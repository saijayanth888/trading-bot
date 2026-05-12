# Tier C — Frontend perf / fetch-state fixes

**Branch:** `fix/frontend-tier-c-perf`
**Worktree:** `/home/saijayanthai/Documents/trading-bot/.claude/worktrees/agent-afbf04def2a91f5de`
**Status:** complete, NOT pushed
**Cache-bust:** `?v=20260512-tier-c-perf` on `qc_react.js`, `components.js`, `ops_spa.js`, `dashboard_spa.js` in BOTH templates
**Source audit:** `FRONTEND_AUDIT_2026-05-12.md` section 2 (P1 defects) + section 4 (performance posture)

## Commit map (one atomic commit per fix, in landing order)

| Commit | Fix | Files touched |
|---|---|---|
| `ee8ebc0` | P1-1 Topbar dedup | `qc_react.js`, `ops_spa.js` |
| `e31601f` | P1-2 AbortController on every useEffect fetch | `ops_spa.js`, `dashboard_spa.js` |
| `ce9a3ed` | P1-3 batch useOpsData setState (22 → 1) | `ops_spa.js` |
| `0be2f02` | P1-4 sidebar hotkey whitelist | `qc_react.js` |
| `f080ae5` | P1-5 KillSwitch touch safety | `qc_react.js` |
| `2804ec4` | P1-6 Sparkline stable-key | `qc_react.js` |
| `2ad7673` | P1-7 NumberRoll flash-id token | `qc_react.js` |
| `4a2c619` | P1-8 uptime stale-value guard | `qc_react.js` |
| `93615e1` | P1-9 fetchOne per-endpoint token | `ops_spa.js` |
| `3b0f8df` | cache-bust bump | both templates |

Each commit message describes the invariant being preserved and the
revert procedure.

---

## P1-1 — Topbar triple-fetch dedup
**File:** `user_data/dashboard/static/js/qc_react.js` (lines ~862-1035) + `user_data/dashboard/static/js/ops_spa.js` (the `<Topbar>` call inside `OpsApp`).

**Before:** Topbar polled `/api/ops/uptime`, `/api/ops/combined_portfolio`, `/api/mode`, `/api/ops/services` every 30 s. `useOpsData` polled the latter three every 10 s. Three of the four endpoints were double-polled.

**After:** Topbar accepts optional `combinedPortfolio`, `mode`, `services` envelope props. When passed, Topbar reads from props (no fetch). `OpsApp` now passes `data.combined_portfolio` / `data.mode` / `data.services` from `useOpsData` state. Only `/api/ops/uptime` (the one endpoint NOT in `FAST_ENDPOINTS`) still polls locally. Local-fallback fetch path remains for any future direct mount of `Topbar` without `useOpsData`.

**Risk:** medium — the resolution helpers at the bottom of `Topbar` synthesize the `equity / mode / ftUp` view from envelope-shaped props OR local state. If `useOpsData` shape changes, those helpers must be updated.

**Revert:** `git revert ee8ebc0` OR remove the three new prop names from the `h(Topbar, {…})` call in `ops_spa.js:3634`; Topbar then falls through to its local-fallback poll exactly as before.

---

## P1-2 — AbortController on every useEffect-mounted fetch
**Files:** `user_data/dashboard/static/js/ops_spa.js` (useOpsData + LLMCallsLive modal), `user_data/dashboard/static/js/dashboard_spa.js` (universe-fallback + fetchState/fetchTopbar/fetchCandles polling cluster).

**Before:** zero of ~25 useEffect-mounted fetches used `AbortController`. Tab-switch, unmount, and dep-change re-mount all leaked in-flight requests (orange "stalled" entries in DevTools Network).

**After:** every useEffect-mounted fetch site receives a `signal` from a useEffect-scoped `AbortController`. Cleanup calls `ctrl.abort()`. A shared `isAbortError(e)` helper swallows the expected AbortError in catch. LLM modal's `closeModal` (including ESC) now aborts the modal's in-flight drilldown; rapid-click on different rows cancels the previous fetch.

Action-button POSTs (kill switch, regime config write, MCP tool console, rebalance, Slack brief) were NOT wrapped — they're one-shot event handlers, not useEffect polls, so no unmount-leak path exists.

**Risk:** medium — `useOpsData` now plumbs the signal through to every fetch and tracks `ctrlRef.current.signal`. If the abort handling is removed in any one place, that endpoint silently re-introduces the leak.

**Revert:** `git revert e31601f`; behaviour reverts to leaking in-flight fetches on unmount (audit-confirmed baseline).

---

## P1-3 — Batch useOpsData setState (22 renders/tick → 1)
**File:** `user_data/dashboard/static/js/ops_spa.js:179-260`.

**Before:** each of 22 `FAST_ENDPOINTS` fetches resolved at different times and each called its own `setState`. React's auto-batching does not always coalesce across microtask boundaries → up to 22 ops-page renders per 10 s tick.

**After:** each fetch resolves a plain `{ key, ok, env|err }` record. `Promise.allSettled` gathers the batch. `flushBatch` builds one patch object and calls `setState` exactly once. Same pattern for `SLOW_ENDPOINTS` (60 s tick, 7 endpoints).

**Risk:** medium — if a future edit moves `setState` back inside a per-fetch `then/catch` (or adds a new endpoint that does), the storm re-appears. Source comment block explicitly calls out the invariant.

**Revert:** `git revert ce9a3ed`.

---

## P1-4 — Sidebar hotkey 1-9 whitelist
**File:** `user_data/dashboard/static/js/qc_react.js:1054-1090`.

**Before:** whitelist was `INPUT / TEXTAREA / SELECT / contentEditable`. Pressing `2` with a `<button>`, `<a>`, or `role=button` focused (e.g. mid-hold on a KillSwitch confirm button) nav-jumped the page.

**After:** whitelist extended with `BUTTON`, `A`, ARIA roles (`button`, `textbox`, `combobox`, `searchbox`, `spinbutton`), and an explicit `data-no-hotkey` opt-in attribute (closest-match) for custom widgets that want to absorb digits.

**Risk:** low — purely additive blocking conditions. Cannot break any currently-working nav case.

**Revert:** `git revert 0be2f02`.

---

## P1-5 — KillSwitch touch safety
**File:** `user_data/dashboard/static/js/qc_react.js:719-840`.

**Before:** the React KillSwitch had `onMouseUp / onMouseLeave / onTouchEnd` cancel paths but NOT `onPointerLeave / onPointerCancel / onTouchCancel / onTouchMove`. Touch users could press → drift finger off the button → still trigger destructive kill at 1500 ms. The DOM version at `components.js:462` already handled these correctly — ported the behaviour over.

**After:** four cancel paths now wired: `onPointerLeave`, `onPointerCancel`, `onTouchCancel`, and `onTouchMove` with a 20-px drift threshold (squared-distance comparison; captures initial touch `{x,y}` on touchstart). Also `onContextMenu` is `preventDefault`'d so iOS long-press doesn't steal focus mid-hold.

**Risk:** low-medium — operators should now find the KillSwitch SAFER, not less responsive. Mouse users see no behavioural change (mouseup / mouseleave still cancel).

**Revert:** `git revert f080ae5`.

---

## P1-6 — Sparkline `useEffect` deps stabilization
**File:** `user_data/dashboard/static/js/qc_react.js:113-205`.

**Before:** deps `[data, color, fill, animate]` — parent passes a fresh array literal on every render, so the effect re-runs every 10-s tick → canvas redraw + 500 ms intro animation restart. Sparklines visibly stuttered.

**After:** `sparkKey(data)` computes an O(1) hash (length + first/last/min/max + three mid-series samples). Effect deps changed to `[key, color, fill, animate]`. Effect only re-runs when the visible plot would actually change.

**Risk:** medium — if future data shapes produce a sparkKey collision, real updates could be skipped. Mitigated by sampling 5 positions + min/max; collisions require all 7 to match.

**Revert:** `git revert 2804ec4`.

---

## P1-7 — NumberRoll flash overlap
**File:** `user_data/dashboard/static/js/qc_react.js:83-128`.

**Before:** rapid value-changes overlapped: A→B's 600 ms timer was still pending when B→C fired; the cleanup function returned by the effect cleared A's timeout when C's effect ran, but the new flash got cleared immediately by A's old setFlash(null) racing.

**After:** `flashIdRef` token increments per value-change; setTimeout only calls `setFlash(null)` if the ref still matches its captured id. Stale timeouts become no-ops.

**Risk:** low — purely defensive; touches only the timing of the 600-ms flash, no other rendering.

**Revert:** `git revert 2ad7673`.

---

## P1-8 — `/api/ops/uptime` stale-value pill
**File:** `user_data/dashboard/static/js/qc_react.js:862-940` (Topbar uptime fetch + render).

**Before:** Topbar read `j.data.freqtrade.uptime_s` blindly. When freqtrade reported `{ status: "down" }`, the pill kept rendering the last numeric value indefinitely.

**After:** uptime fetcher checks `ft.status === "down"` (or `ft.up === false`) FIRST. New state slots `ftDown` and `uptimeFetchedAt`. When `ftDown`, the BOT-UP pill renders an explicit red "FT down" pill (dot pulse + `--down` color) and the surrounding `tb-group` shows a tooltip with the last-good timestamp so operators know how stale the previous reading is.

**Risk:** low — purely additive rendering branch; the happy path is unchanged.

**Revert:** `git revert 4a2c619`.

---

## P1-9 — `fetchOne` race
**File:** `user_data/dashboard/static/js/ops_spa.js:179-280` (inside the same `useOpsData` block as P1-3).

**Before:** two refreshes for the same key could be in flight at once. The slower call's result silently overwrote the faster (and fresher) call's result whenever it resolved second.

**After:** each request captures an incrementing per-key token (`tokensRef.current[key]`). `flushBatch` drops the response if the captured token doesn't match the latest token for that key. Standard stale-while-revalidate pattern.

**Risk:** low — token bookkeeping is internal to `useOpsData`. No public-API change.

**Revert:** `git revert 93615e1`; reverts to last-write-wins racing.

---

## Net request-count math (before / after)

**Ops page at rest (operator idle, no tab switch):**

| Source | Before | After | Delta |
|---|---:|---:|---:|
| `useOpsData` FAST (22 endpoints x 6 ticks/min) | 132 | 132 | 0 |
| `useOpsData` SLOW (7 endpoints x 1 tick/min) | 7 | 7 | 0 |
| Topbar duplicate polls (3 endpoints x 2 ticks/min) | 6 | 0 | -6 |
| Topbar uptime (1 endpoint x 2 ticks/min) | 2 | 2 | 0 |
| **TOTAL** | **147 req/min** | **141 req/min** | **-6 req/min (-4 %)** |

Audit-stated ~140 req/min baseline matches. The 40 % drop targeted by
the spec is achievable only by replacing the 22-endpoint poll with the
existing `/ws` push (Tier C scope's `useOpsData` batching does not
reduce request count, only render count). The actual request-side win
here is the ~6 dedup'd polls/min from P1-1; the bulk of Tier C's win
is in render count and leak elimination.

**Tab-switch / unmount (one-off):**

| | Before | After |
|---|---:|---:|
| In-flight fetches abort on unmount | 0 | All useEffect-issued (~30 at peak tick) |
| Orange "stalled" Network entries lingering after switch | yes | no |

---

## Net render-count math

| Surface | Before | After |
|---|---:|---:|
| Ops page React renders per 10s `useOpsData` tick | ~22 | ~1 |
| Sparkline canvas-redraws per 10s tick (per sparkline) | 1 (always re-runs) | 0 unless the visible plot changed |
| Topbar render per 30s | 1 (own poll) + 1 (parent re-render) | 1 (parent re-render only when ops data changes) |

---

## Cache-bust

All four scripts now ship with `?v=20260512-tier-c-perf` in BOTH templates:
- `user_data/dashboard/templates/ops_spa.html`
- `user_data/dashboard/templates/dashboard_spa.html`

Browser will fetch fresh JS on next page load.

---

## Recommended test plan (operator)

1. **Hard-refresh both pages** (Cmd-Shift-R) — the new cache-bust should
   pull fresh JS. Confirm in DevTools Network that
   `qc_react.js?v=20260512-tier-c-perf` is the version being loaded.

2. **DevTools → Network**, throttled to "Fast 3G", leave `/ops` open 1
   minute.
   - Expect ~141 req/min at rest (down from ~147 — the 6 dedup'd
     Topbar polls).
   - The bigger win is the next item.

3. **DevTools → Network, ALL types, sort by Status.** Switch tabs away
   from `/ops` for 5 s and back. Before this branch the previous tick's
   22 in-flight fetches would show "(canceled)" status because the
   browser tore down the tab; after this branch they show "(canceled)"
   within < 10 ms of unmount because `ctrl.abort()` fired — no
   in-flight requests hang.

4. **React DevTools Profiler** (Chrome extension) → record 30 s on
   `/ops`. Inspect the highlighted renders per tick. Expect ~1 commit
   per useOpsData tick on the OpsApp root, instead of ~22.

5. **Sparkline twitch test.** Watch any sparkline card (e.g. equity
   sparkline in `combined_portfolio`). Before: every 10 s the line
   briefly disappeared and re-drew from the left over 500 ms. After:
   the line should only redraw when the data actually changes (and even
   then, animate only on the first paint per visible-plot-change).

6. **Hotkey test.** Click any button (e.g. ARM in the KillSwitch) so it
   has focus. Press `2`. Before: page navigates to `/`. After: nothing
   happens.

7. **KillSwitch touch drift.** On a touch device (or DevTools touch
   emulation): tap-and-hold the KillSwitch confirm button, then drag
   your finger > 20 px while still holding. Before: at 1500 ms, the
   kill still fires. After: the fill bar resets to 0 % the moment
   drift crosses 20 px.

8. **Uptime "FT down" pill.** Simulate freqtrade down by either
   stopping the freqtrade container OR mentally inspect
   `/api/ops/uptime` response when it returns
   `{ data: { freqtrade: { status: "down" } } }`. Before: BOT-UP pill
   keeps showing the stale value. After: it flips to a pulsing red "FT
   down" pill with the previous value's timestamp in a tooltip.

9. **NumberRoll rapid-change.** During a market open with high tick
   volume, watch the equity NumberRoll. Before: occasional missed
   flashes when two values arrive < 600 ms apart. After: every change
   flashes the full 600 ms.

10. **fetchOne race.** Manually force a slow response: in DevTools
    Network, throttle `/api/ops/combined_portfolio` only (or use a
    proxy). With ticks at 10 s but the endpoint taking 12 s, before
    this branch the value would visibly oscillate as the slower tick-A
    response overwrote tick-B's fresher data. After: the value
    monotonically advances; the stale token guard drops the late
    response.

---

## Rollback (entire Tier C)

```bash
cd /home/saijayanthai/Documents/trading-bot/.claude/worktrees/agent-afbf04def2a91f5de
git revert 3b0f8df 93615e1 4a2c619 2ad7673 2804ec4 f080ae5 0be2f02 ce9a3ed e31601f ee8ebc0
```

Or to revert just one fix, use the per-commit revert instructions in
the table above. Each commit is independent of the others (P1-9
depends on P1-3 only for its accumulator structure; revert P1-9 first
if you want to roll back P1-3 alone).

---

## Files not pushed

This branch is local-only. Push is the operator's choice after the
test plan above.
