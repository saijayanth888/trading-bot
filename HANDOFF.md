# Tier-B frontend design moves — HANDOFF

**Branch:** `fix/frontend-tier-b-design` (NOT pushed)
**Worktree:** `.claude/worktrees/agent-af55511ccccebb09a` (this directory)
**Cache-bust:** `?v=20260512-tier-b-design` — bumped on `quanta.css` (both
templates), `qc_react.js` (both templates), and `ops_spa.js` (ops template).
**Scope:** PURE ADDITIVE — no existing card behavior, fetch logic, polling
cadence, state management, or endpoint was modified.

5 commits on the branch, one per move:

```
006ab07 Tier-B bonus #4: Latency dot with backpressure
10a5a6a Tier-B bonus #1: Row flash on new fill
9eb9cba Tier-B #9: Regime-aware page chrome
0b24094 Tier-B #7: Global blocker banner
a526284 Tier-B #6: Traffic-light pill row for entry gates
```

Verification (operator path):

1. Open `/ops`, hard-refresh.
2. Topbar shows the 6px latency dot to the right of the FREQTRADE pill +
   the bottom border tints with the BTC regime color.
3. If blockers exist (today: `regime=trending_down` is hard-blocking
   most pairs), the blocker banner appears under the topbar, above
   `TodayScoreboard`.
4. Scroll to the Entry Gates card — each per-pair row shows the pill row
   by default. To revert to the legacy dot grid:
   `localStorage.setItem("quanta.entry_gates_v2","0"); location.reload()`
5. When a new trade lands, its row in `PositionsLive` flashes green
   (buy/long) or red (sell/short) for 200ms.

---

## Move #6 — Traffic-light pill row

ASCII mockup:

```
BTC/USDT  trending_up  [ regime ✓ 14m ][ vol ✓ 8m ][ tft ✗ now ]  8/9 pass  tft_confidence  ▸
```

- New component: `TrafficLightPillRow` in `user_data/dashboard/static/js/ops_spa.js`.
- Module-scope ring buffer `__gateFlipTracker` (same file) derives the
  "time since last flip" timestamp by diffing successive `/api/ops/gates`
  payloads. First-render falls through to "now" since there's no flip
  history yet; subsequent polls accumulate it.
- Feature flag: `localStorage["quanta.entry_gates_v2"]`
  - `null` or any value other than `"0"` → pill row (DEFAULT ON)
  - `"0"` → legacy `GateDot` grid
  - Operator disable: `localStorage.setItem("quanta.entry_gates_v2","0"); location.reload();`
  - Operator re-enable: `localStorage.removeItem("quanta.entry_gates_v2"); location.reload();`
- Click a pill to expand and see `gate.detail` (value vs threshold).
- Legacy `GateDot` component is **NOT removed** — both code paths live
  side-by-side. Same `p.gates` data feeds both.

Files changed:

- `user_data/dashboard/static/js/ops_spa.js` — `TrafficLightPillRow`,
  `__gateFlipTracker`, `fmtAgoShort`, branch in `EntryGatesLive`.
- `user_data/dashboard/static/css/quanta.css` — `.tlpill*` rules.

---

## Move #7 — Global blocker banner

ASCII mockup (visible only when blockers exist):

```
┌────────────────────────────────────────────────────────────────────────────┐
│ 🚦  6/8 pairs blocked  ·  6/8 on regime  ·  2/8 on vol_floor  ·  newest    │
│      blocker: tft_confidence (12m ago)                                   ▸ │
└────────────────────────────────────────────────────────────────────────────┘
   TodayScoreboard card …
```

- New component: `BlockerBanner` in `ops_spa.js`, mounted in `OpsApp`
  immediately under the `page-title` block and immediately above
  `TodayScoreboard`.
- Reads from the SAME `data.gates` slot the SPA already polls every 10s.
  No new endpoint, no new fetch.
- Renders `null` when zero blockers exist — zero footprint at rest.
- Click toggles a per-pair breakdown grid (uses `p.first_blocker` from
  the same payload the `EntryGatesLive` row already consumes).
- Color tint: `--warn` background-alpha 8% per spec; border `--warn-line`.

Files changed:

- `user_data/dashboard/static/js/ops_spa.js` — `BlockerBanner` component
  + mount in `OpsApp.main`.
- `user_data/dashboard/static/css/quanta.css` — `.blocker-banner*` rules.

---

## Move #9 — Regime-aware page chrome

The topbar's 2px bottom border tints with the current BTC regime:

```
trending_up      → --up      (green hairline)
trending_down    → --down    (red hairline)
mean_reverting   → --warn    (orange hairline)
high_volatility  → --accent  (blue hairline)
unknown / null   → --line-2  (default, no tint)
```

ASCII mockup (red border under topbar = BTC trending_down):

```
┌──────────────────────────────────────────────────────────────┐
│ Q  QUANTA   PAPER · DRY-RUN   FREQTRADE OK ·   …             │
├══════════════════════════════════════════════════════════════┤    ← 2px red
│                                                                │
│  🚦 6/8 pairs blocked  …                                       │
```

- CSS variable used: **`--regime-tint`** (set on the `.topbar` element).
- Implementation: a `useEffect` in `OpsApp` (`ops_spa.js`) reads
  `data.regime.current` and sets `--regime-tint` + `data-regime` on the
  topbar element. ZERO new endpoint calls (reuses the existing 10s
  regime poll).
- Default fallback in CSS is `--line-2`, so removing the JS hook is a
  no-op visually.
- **Coordination with Tier-A WCAG glyph work:** Tier-A's pass/fail
  glyphs are unrelated to this CSS variable. The only place this PR
  touches the topbar's `border-bottom` style is the override here;
  Tier-A can freely add `::after` overlays or content insertions to the
  topbar without colliding. Reserve `--regime-tint` and the
  `data-regime` attribute for this move only.

Files changed:

- `user_data/dashboard/static/js/ops_spa.js` — `regimeCurrent` +
  `useEffect` in `OpsApp`.
- `user_data/dashboard/static/css/quanta.css` — `.topbar { --regime-tint
  … border-bottom-color: var(--regime-tint) … }`.

---

## Bonus #1 — Row flash on new fill

ASCII (a new buy fill lands during a poll tick):

```
   Symbol   Venue     Side   Qty   Entry    Mark    uPnL%   Note
   BTC/USDT Coinbase  LONG   0.12  64231.0  64405.2 +0.27   regime@entry=…
░░ ETH/USDT Coinbase  LONG   1.85   3214.0   3220.1 +0.19   …   ░░  ← 200ms green flash
   SOL/USDT Coinbase  LONG   ...
```

- 200ms one-shot CSS animation; classes `flash-buy` / `flash-sell`.
- Detection: a `useMemo` in `PositionsLive` diffs the current render's
  trade-key set against the previous render's. The key is a synthetic
  composite — `tradeRowKey(t) = [t.label, t.kind, t.subkind, t.opened_at, t.entry].join("|")`
  — because `/api/ops/live_trades` doesn't surface a stable `trade_id`.
- First-ever render seeds the previous-keys set without flashing —
  hard-refresh does NOT light up every existing row.
- Easing: `cubic-bezier(0.2, 0, 0, 1)` over 200ms.
- Animation colors: `rgba(74,246,195,0.18)` (up) → transparent;
  `rgba(255,67,61,0.18)` (down) → transparent. Matches the operator's
  locked `--up` / `--down` semantics.

Files changed:

- `user_data/dashboard/static/js/ops_spa.js` — `tradeRowKey()` helper,
  `prevKeysRef` + `useMemo` diff in `PositionsLive`, `flashCls` on `<tr>`.
- `user_data/dashboard/static/css/quanta.css` — `@keyframes flash-buy`
  and `flash-sell` + `table.t tbody tr.flash-*` rules.

---

## Bonus #4 — Latency dot with backpressure

ASCII (topbar, right of FREQTRADE pill):

```
[PAPER · DRY-RUN] [FREQTRADE OK] ● ← 6px dot
                                 │
                                 └ tooltip: "freqtrade latency: 67 ms (round-trip /api/mode)"
```

Color & pulse-speed binding:

```
<100ms       --up   solid, no pulse        lat-fast
100-250ms    --up   slow pulse 2s          lat-ok
250-1000ms   --warn fast pulse 1s          lat-slow
>1000ms      --down solid, no pulse        lat-dead
exception    --down solid, no pulse        lat-dead (latencyMs = -1)
```

- Measurement: `performance.now()` before/after the EXISTING `/api/mode`
  fetch in the `Topbar` component (`qc_react.js`). No new URL, no new
  options, no new cadence.
- Tooltip on hover (`title=` attribute) shows actual latency value or
  the "measuring…" / "feed unreachable" state.

Files changed:

- `user_data/dashboard/static/js/qc_react.js` — `latencyMs` state in
  `Topbar`, measurement around existing `/api/mode` fetch, new dot span
  in the FREQTRADE `tb-group`.
- `user_data/dashboard/static/css/quanta.css` — `.tb-latency` /
  `.ltd.lat-*` / `@keyframes ltdPulse`.
- `user_data/dashboard/templates/ops_spa.html` and
  `user_data/dashboard/templates/dashboard_spa.html` —
  `qc_react.js?v=20260512-tier-b-design` cache-bust.

---

## Cache-bust version

All three pieces of moved JS/CSS are versioned together:

```
?v=20260512-tier-b-design
```

Applied in:

- `user_data/dashboard/templates/ops_spa.html`:
  - `quanta.css?v=20260512-tier-b-design`
  - `qc_react.js?v=20260512-tier-b-design`
  - `ops_spa.js?v=20260512-tier-b-design`
- `user_data/dashboard/templates/dashboard_spa.html`:
  - `quanta.css?v=20260512-tier-b-design`
  - `qc_react.js?v=20260512-tier-b-design`
  - (`dashboard_spa.js` itself was NOT touched — its cache-bust stays at `?v=20260511-cutover19`.)

`components.js` was NOT touched either — its cache-bust stays at
`?v=20260511-cutover19`.

---

## Revert recipe

Two ways:

### A. Branch-level revert (fastest)

```bash
git checkout main
git branch -D fix/frontend-tier-b-design
```

### B. Per-file surgical revert (keep some moves, drop others)

The 5 commits are atomic and reversible in any order:

```bash
git revert 006ab07          # Bonus #4 — latency dot
git revert 10a5a6a          # Bonus #1 — row flash on new fill
git revert 9eb9cba          # Move #9 — regime-aware page chrome
git revert 0b24094          # Move #7 — global blocker banner
git revert a526284          # Move #6 — traffic-light pill row
```

### C. Manual cleanup (if you've squash-merged and can't revert)

Remove these symbols and the CSS sections that name them:

| Move | JS symbols (in `ops_spa.js` unless noted) | CSS section header to drop |
|------|-------------------------------------------|----------------------------|
| #6   | `TrafficLightPillRow`, `__gateFlipTracker`, `fmtAgoShort`, the IIFE branch in `EntryGatesLive` | `/* Move #6 — Traffic-light pill row …` |
| #7   | `BlockerBanner`, `h(BlockerBanner, { data })` mount in `OpsApp` | `/* Move #7 — Global "why isn't anything trading?" banner.` |
| #9   | `regimeCurrent` const + the `useEffect` that sets `--regime-tint` in `OpsApp` | `/* Move #9 — Regime-aware page chrome.` |
| Bonus #1 | `tradeRowKey()`, `prevKeysRef`, the `useMemo` in `PositionsLive`, `flashCls` on `<tr>` | `/* Bonus #1 — Row flash on new fill.` |
| Bonus #4 | `latencyMs` state + the `tStart`/`tEnd` measurement around `/api/mode` + the `.tb-latency` span in `qc_react.js` `Topbar` | `/* Bonus #4 — Latency dot with backpressure.` |

Then revert the cache-bust query strings in the two templates to whatever
the prior session used (`?v=20260511-1` for `quanta.css`,
`?v=20260511-cutover19` for `qc_react.js`, and
`?v=20260512-llm-calls-ux` for `ops_spa.js`).

---

## Constraints honored

- DO NOT modify existing fetch logic, polling cadence, or state management — **OK** (only added new state hooks + a `performance.now()` wrapper around the existing `/api/mode` call).
- DO NOT remove the legacy GateDot grid — **OK** (kept, gated on `localStorage["quanta.entry_gates_v2"]`).
- DO NOT alter any existing endpoint or add new endpoints — **OK** (zero new endpoints).
- All new components read from data already in scope — **OK**.
- All new CSS rules in `quanta.css` — **OK** (`app.css` not touched).
- Animations: `cubic-bezier(0.2, 0, 0, 1)` over 120–200 ms — **OK** (all five moves use it).
- Cache-bust at the end — **OK** (`?v=20260512-tier-b-design`).
- Each design move gets ONE commit — **OK** (5 commits on the branch).
- Branch `fix/frontend-tier-b-design` — **OK**.
- DO NOT push to remote — **OK** (no push performed).
