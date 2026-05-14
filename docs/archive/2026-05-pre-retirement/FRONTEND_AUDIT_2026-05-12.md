# Quanta Dashboard — Frontend Audit & Design Recovery Plan

**Date:** 2026-05-12
**Target:** `http://localhost:8081` (container `dashboard`, healthy, up ~4h)
**Scope:** Full source-level audit of `user_data/dashboard/` + public benchmark against May-2026 trading UIs.
**Method:** Parallel agents — one read every line of CSS/JS/templates/routes; one researched dYdX v4, Hyperliquid, Drift, GMX, Bloomberg, TradingView, Linear, Vercel. Live API responses sampled for ground truth.

---

## 0. State of play (live verification)

- All 22 fast-polled `/api/ops/*` endpoints return `200 OK`. **Zero backend errors in last 200 log lines.**
- Live request mix (last ~10 min, top 25): every fast endpoint hit 19–28×. **~140 requests/min at rest.**
- One stale 404 confirmed: `/api/portfolio/summary` (no consumer in code; was my own probe). No real FE→BE drift.
- Equity: $118,562.24 · DD 0.37% · 5 open positions (all stocks) · combined CB inactive.
- BTC regime stuck `trending_down` 1h (p=0.97) — gates correctly hard-blocking. The *bot* is healthy; the *dashboard* has bugs that hide that.

---

## 1. Critical defects (P0 — ship invisible/broken UI today)

### P0-1 · The `--c-up / --c-down / --c-warn` typo bug (11 sites)

Tokens are defined in `quanta.css` as `--up`, `--down`, `--warn`. `ops_spa.js` references the non-existent `--c-*` aliases. CSS resolves them to nothing → those cells render with inherited/unset color. Confirmed by grep:

```
ops_spa.js:1279  GateDot pass   var(--c-up)
ops_spa.js:1280  GateDot fail   var(--c-down)
ops_spa.js:1336  EntryGates blocker banner border + bg
ops_spa.js:1368  first-blocker text
ops_spa.js:2768  CircuitBreakers tripped condition rows
ops_spa.js:2893  Backtest gates pass/fail color
ops_spa.js:3129  LLM modal copy-prompt success
ops_spa.js:3150  LLM modal copy-response success
ops_spa.js:3492  LLM modal error toast
```

**Impact:** The signal you most need on a trading screen — pass/fail color — is silently off. Operators have been reading uncolored gate dots for an unknown period.

**Fix:** sed-replace `var(--c-up)` → `var(--up)`, same for `down`/`warn`. ~10 min change.

### P0-2 · `warn-strong` class referenced, never defined

`ops_spa.js:2941` returns `"warn-strong"` for LLM latencies 5–15 s. No matching CSS rule anywhere. Renders plain text.

**Fix:** add to `quanta.css` (`.warn-strong { color: var(--warn); font-weight: 600; }`) or change the JS to `"warn"`.

### P0-3 · `app.css` is dead — 1,333 lines / ~41 KB shipped to no template

`grep -r 'app.css' templates/` → empty. Only `quanta.css` is loaded. The dead file imports Google Fonts (render-blocking) and defines `mode-pill`, `ws-pill`, `hero`, `kpi-*`, `tape`, `ks-grid` etc. that no JS uses.

**Fix:** confirm with one last `grep -r 'mode-pill\|kpi-' static/js/` → if empty, delete `app.css`. Otherwise port the still-used rules into `quanta.css`.

---

## 2. P1 defects (broken in subtle ways or wasting resources)

| # | Defect | File:Line | Effort |
|---|---|---|---|
| P1-1 | qc_react `Topbar` polls `/api/mode`, `/api/ops/services`, `/api/ops/combined_portfolio` every 30 s **on top of** `useOpsData`'s 10 s polling of the same endpoints — triple-fetch race | `qc_react.js:874-925` | M |
| P1-2 | No `AbortController` on any fetch. In-flight requests outlive component unmount; tab-switch leaks. Only 2 sites of ~50 use a cancelled flag. | All fetch sites | M |
| P1-3 | `useOpsData` fires 22 parallel fetches and 22 `setState` calls in the same animation frame → 22 React renders per 10 s tick | `ops_spa.js:179-224` | M |
| P1-4 | Sidebar 1–9 hotkey handler whitelists INPUT/TEXTAREA/SELECT/contentEditable but NOT `<button>`, `<a>`, `[role=button]`. Pressing `2` with a button focused nav-jumps the page mid-action. | `qc_react.js:1056-1071` | S |
| P1-5 | KillSwitch React version has no `pointerleave`/`touchmove` cancel. Touch users can press → drift finger → still trigger destructive kill at 1500 ms. DOM version (`components.js:462`) does it correctly. | `qc_react.js:719-830` | S |
| P1-6 | `Sparkline` `useEffect` deps `[data, color, fill, animate]` — every parent render passes a fresh `[]` reference → redraw + 500 ms intro animation restarts every 10 s tick on every sparkline | `qc_react.js:114-167` | M |
| P1-7 | `NumberRoll` flash logic: rapid successive value changes overlap, the older `clearTimeout` cancels the newer flash | `qc_react.js:91-101` | S |
| P1-8 | `/api/ops/uptime` reads `j.data.freqtrade.uptime_s` directly — when freqtrade is down BE returns `{status:"down"}` → pill stuck on stale value forever, no error state | `qc_react.js:878-890` | S |
| P1-9 | `fetchOne` race: two refreshes back-to-back, slower call's `setState` overwrites the faster one if it lands second | `ops_spa.js:184-206` | S |
| P1-10 | Hardcoded LAN IP `192.168.1.49:8081` in sidebar footer — wrong on any other host | `qc_react.js:1116` | XS |

---

## 3. P2 — polish / dead code

- `holdToConfirm` exported in `components.js:241`, never called anywhere. Delete.
- `/ws` WebSocket builds & pushes state every 30 s — **no SPA connects to it.** Dead 25-line keep-alive in `app.py:312-335`.
- `window.QU` shim in `qc_react.js:32-44` looks for `window.QuantaData?.fmtClock?.()` — never set anywhere. Falls through every call.
- `AgentTimeline` cron list is hardcoded fictional times (`ops_spa.js:969-980`). Pull from `/api/ops/training` or new endpoint.
- `Topbar` refresh-interval dropdown rendered but never wired up on `/ops` — `ops_spa.js:3634` mounts `<Topbar>` without `onRefreshIntervalChange`.
- Color-only signaling on gate dots → fails WCAG 1.4.1. Add ✓/✕ glyphs.
- `@import url(fonts.googleapis.com)` at top of `quanta.css` is render-blocking; templates already `preconnect` but `@import` still blocks paint. Convert to a `<link rel=stylesheet>` in the template.
- `anchor` `scroll-margin-top: 80px` (quanta.css:404), topbar is 52 px — hash jumps park section 28 px under the topbar.

---

## 4. Performance posture

| Metric | Current | Notes |
|---|---|---|
| JS shipped (uncompressed) | ~415 KB | `ops_spa.js` alone is 193 KB |
| Gzip estimate | ~135 KB | No minify step |
| React source | unpkg.com CDN (UMD) | **Third-party dependency on every page load** — supply-chain risk |
| Polling load | ~140 req/min @ rest | 22 endpoints × 10 s |
| `React.memo` count | 0 cards | every card re-renders on every tick |
| `useMemo` deps audit | 1 site uses it correctly (`ResearchFeedLive`) | rest re-compute every render |
| `AbortController` | 0 | every poll site leaks in-flight on unmount |
| Specificity wars / `!important` | clean (0 `!important`) | one healthy thing |

**Quick wins:** (a) pin React UMD to a local file under `static/vendor/`. (b) Add esbuild `--minify` step in the dashboard Dockerfile. (c) Replace 22-endpoint polling with the existing-but-unused `/ws` push.

---

## 5. Design verdict — where Quanta stands today

### What's working
- 11-step type scale + `tabular-nums` discipline — the strongest asset. Reads institutional.
- `NumberRoll` per-digit odometer + 600 ms flash — substantive, not gimmicky.
- KillSwitch `armed` breathing border + 1500 ms hold-to-confirm is genuinely good UX.
- Themes (Control / Geist / Bloomberg) and densities (Compact / Default / Roomy) with `localStorage` boot-before-paint — operator preferences survive nav with no flash.
- Zero `!important`, shallow selectors, semantic tokens (when not mis-spelled — see P0-1).

### What hurts
- **Information density too airy.** 16 px gaps between cards + 12–20 px card padding. Bloomberg uses 0 px between cells, single-pixel dividers. On a wide monitor Quanta looks more like Stripe than a trading terminal.
- **Color system not enforced.** The `--c-*` typo bug is screaming evidence — 8+ inline hex codes in `qc_react.js`, no central "severity tier" token.
- **`mountIn` 220 ms fade overused.** Every card animates in. Reserve motion for meaningful state changes (fills, regime flips, new blockers).
- **Mobile broken.** One `@media (max-width: 1200px)` collapses the sidebar; below 768 px, the 12-col grid overflows, topbar wraps badly.
- **Empty/loading/error states inconsistent.** Ops page uses `EmptyState`/`LoadingState`/`RetryCountdown` everywhere; pair page (`dashboard_spa.js`) just shows dim text.
- **No iconography on pass/fail** — color-only. WCAG fail.

---

## 6. Top-10 design moves to make Quanta "stand out" in 2026

Public research (sources at end) benchmarked against dYdX v4, Hyperliquid, Drift, GMX v2, Bloomberg, TradingView, Linear, Vercel.

| # | Move | Steal from | Why it punches | Effort |
|---|---|---|---|---|
| 1 | **Inline liquidation-distance bar** in every position row — centered entry, mark dot, liq tick, bleeds red as risk grows | GMX v2 + Chaos Labs | Risk visible without a page change | M |
| 2 | **`cmd+k` command palette + `g+x` chord nav** with every route addressable as a function code | Linear + Bloomberg | Power users 5× faster, one mental model | M |
| 3 | **Persistent net-equity strip at top** fusing crypto + stocks + options, hover for per-venue split | Drift unified account | Solo operator cares about one number | S |
| 4 | **Single-key trade ops** (A buy, D sell, X cancel-recent, P cancel-all, 1–6 size presets) — gated on no-input-focused | Tealstreet for Hyperliquid | Eliminates the modifier tax | M |
| 5 | **Persistent header ticker tape** with per-symbol flash on last-trade — port `LiveTicker` to the pair page | dYdX header tape | One always-on feed-alive signal | S |
| 6 | **Replace gate dots with "traffic-light pill row + last-flip ts"** (`[regime ✓ 14m] [vol ✓ 8m] [tft ✗ now]`) | bespoke | Today you see state without staleness — both matter | S |
| 7 | **"Why isn't anything trading?" banner** — single global blocker summary under the topbar (`regime_down 6/8 · vol_floor 2/8`) | bespoke | Buries the lede today | S |
| 8 | **Latency badge** beside price (`12ms · feed:ok`), amber > 250 ms, red > 1 s | Hyperliquid 0.2s philosophy | Latency is the product when you run a bot | S |
| 9 | **Regime-aware page chrome** — topbar bottom border tints `--up` on bull, `--down` on bear. Peripheral-vision regime tracking | bespoke | Regime is the most consequential variable, surface ambiently | S |
| 10 | **Per-card freshness ring** — convert "Xs ago" text labels into a 1px arc around card border that fills as the card ages toward refresh | dYdX | Removes ~20 textual labels; turns chrome into data | M |

### Bonus: 5 microinteractions <100 LOC each

1. **Row-flash on new fill** — 200 ms CSS keyframe `rgba(74,246,195,0.18)` → transparent (green buy / red sell).
2. **Color-bleeds-to-edge on outsize moves** — when |ΔP&L| > 2σ of session, 1 px inset shadow ramps to 6 px over 400 ms, decays 1.6 s.
3. **Numeric easing only on meaningful deltas** — `requestAnimationFrame` tween 180 ms; skip tween for deltas under one tick. Kills the "always-jiggling" feel.
4. **Latency dot with backpressure** — 6 px dot pulses at actual tick rate; freezes amber if ticks stop > 1 s. Visual proof the feed is alive.
5. **Linear triangle safe-area** on hover menus — diagonal mouse paths don't dismiss submenus. `clip-path: polygon()` overlay, ~60 LOC.

---

## 7. 5 anti-patterns to remove (or stay off)

1. **Purple-to-blue gradient hero cards** → flat `#0a0a0a` with a 1 px `#1f1f1f` hairline. Let data provide color. *(Already constrained by your `feedback_dashboard_design.md`. Apply same rule to `brand-mark` at quanta.css:223 — it still has a gradient.)*
2. **Uniform 16 px radius on every cell** → 0 px on data cells, 6 px on buttons, 12 px only on modals. Different radii encode role.
3. **Oversized icons next to KPIs** → drop icons on KPIs, let monospace digits be the visual.
4. **Donut/Apex placeholder charts** → horizontal stacked bar with values inline, no legend.
5. **Bounce / elastic easing on numeric updates** → `cubic-bezier(0.2, 0, 0, 1)` over 120 ms, only when delta is meaningful.

---

## 8. Color & typography reference

**Bloomberg theme (literal):** bg `#000000` · down `#ff433d` · up `#4af6c3` · warn `#fb8b1e` · info `#0068ff` · IBM Plex Mono everywhere.

**Geist / Control theme (Vercel scale + Bloomberg semantics):**
- bg `#0a0a0a` · panel `#111111` · border `#1f1f1f` · text-hi `#ededed` · text-lo `#8f8f8f`
- semantic up `#4af6c3` · down `#ff433d` · warn `#fb8b1e` · accent `#0070f3`
- Geist Mono (variable, **no ligatures**) for all numerics; Geist Sans for labels.

**Avoid:** Inter (top AI-slop tell per [925 Studios](https://www.925studios.co/blog/ai-slop-web-design-guide)). JetBrains Mono **with** ligatures in price cells (`==`/`->` lie about digit width).

Set `font-variant-numeric: tabular-nums` on every numeric cell (you mostly do — extend to all `.kv` value spans).

---

## 9. Suggested execution order (next 1–2 sessions)

1. **Tonight (S, ~30 min):** sed-fix P0-1, add `.warn-strong`, delete `app.css`, bump cache-bust `?v=`. Visual proof: gate dots and circuit breakers regain color.
2. **Next session (M):** P1-1 + P1-2 (kill duplicate Topbar polling, add `AbortController` to all fetch sites) + P1-3 (batch `useOpsData` setState). Cuts ~50% of dashboard request load and ~95% of wasted renders.
3. **Following session (M):** P1-4 / P1-5 / P1-8 — handler whitelists, KillSwitch touch safety, uptime pill error state.
4. **Design sprint #1 (M):** Implement moves #6 (traffic-light pill row) + #7 (global blocker banner) + #9 (regime-aware page chrome). High signal, no new infra.
5. **Design sprint #2 (M-L):** Move #2 (`cmd+k` palette + `g+x` nav) — adds the keyboard layer that turns this from a dashboard into a terminal.
6. **Hardening (M):** Pin React UMD locally, esbuild minify step, kill the dead `/ws` or wire it up to replace the 22-endpoint poll.

---

## 10. Sources cited (May 2026)

- [dYdX Docs — Funding](https://docs.dydx.xyz/concepts/trading/funding)
- [dYdX v4 Order Book Design](https://medium.com/@gwrx2005/decentralized-order-book-design-in-dydx-v4-625ac0152e80)
- [Hyperliquid technical deep dive](https://rocknblock.io/blog/how-does-hyperliquid-work-a-technical-deep-dive)
- [Tealstreet shortcuts docs](https://docs.tealstreet.io/docs/trade/shortcuts)
- [Drift Vaults launch](https://www.drift.trade/updates/introducing-drift-vaults-the-platform-for-structured-products-on-solana)
- [Chaos Labs GMX v2 Risk Portal](https://chaoslabs.xyz/posts/gmx-v2-risk-portal-product-launch)
- [Bloomberg accessibility post](https://www.bloomberg.com/company/stories/designing-the-terminal-for-color-accessibility/)
- [Bloomberg palette hex](https://www.color-hex.com/color-palette/111776)
- [TradingView Q1 2026 updates](https://chartwisehub.com/tradingview-updates-q1-2026/)
- [Vercel Geist intro](https://vercel.com/geist/introduction)
- [Vercel dashboard redesign changelog](https://vercel.com/changelog/dashboard-navigation-redesign-rollout)
- [Linear keyboard shortcuts](https://keycombiner.com/collections/linear/)
- [Linear "Invisible details"](https://medium.com/linear-app/invisible-details-2ca718b41a44)
- [925 Studios — AI slop web design](https://www.925studios.co/blog/ai-slop-web-design-guide)
- [Made Good — best monospace fonts 2026](https://madegooddesigns.com/coding-fonts/)

---

## Appendix: file map

| Path | Status | Lines |
|---|---|---|
| `templates/dashboard_spa.html` | live (pair page) | 42 |
| `templates/ops_spa.html` | live (ops console) | 42 |
| `templates/docs.html` | live (static docs) | 34 |
| `static/css/quanta.css` | live, primary | 733 |
| `static/css/app.css` | **DEAD — delete** | 1,333 |
| `static/js/qc_react.js` | shared React primitives | 1,236 |
| `static/js/components.js` | DOM lib + TweaksFab | 671 |
| `static/js/dashboard_spa.js` | pair page SPA | 886 |
| `static/js/ops_spa.js` | ops console SPA | 3,749 |
| `static/js/utils.js` | `QU` helpers (broken shim) | 60 |
| `static/js/docs.js` | docs page | 440 |
| `app.py` | Flask/FastAPI shell + dead `/ws` | — |
| `ops_routes.py` | 49 routes, all consumed | — |

**Browser sweep note:** Playwright MCP server is in a persistently stuck state (`Browser is already in use for /home/saijayanthai/.cache/ms-playwright/mcp-chrome-e5aa382`). Stale `playwright-mcp` node processes hold the profile lock. Restart the MCP server (`/mcp`) to capture screenshots. The source-level audit above is more rigorous than a visual sweep would be for the defects found.
