# Frontend Functional Validation — 2026-05-14 Night

**Tooling used:** Playwright MCP (`mcp__plugin_playwright_playwright__*`) — real headless Chromium navigation, console-message capture, network-request capture, full-page screenshots, and DOM `innerText` introspection.

**Pages visited:** 11 (4 on `:8081` legacy/SPA + 4 sub-routes on `/v4` + 3 on `:3001` ModelForge).

**Mode:** READ-ONLY. Only navigations and `evaluate()` reads were performed. No buttons clicked beyond implicit page hydration. No form input. No modal interactions.

---

## Findings

### P0 — Service-down or data-corruption blocking trading

**None.** All 11 pages returned HTTP 200. All API responses observed across pages returned 200 OK. No console errors captured at the `error` level on any page (the `error`-level filter includes only true `console.error`/page errors). No "Failed to fetch" messages. No error overlays. No `NaN` or `undefined` strings rendered in any page body across all 11 pages.

### P1 — Visible UX defect or stale data on a card the operator relies on

**None observed.**

- The previously-shipped "STOCKS UNTRUSTED" gate is working as intended: pill is **NOT** rendered on `/` while `market_open_now=false`. Confirmed via `document.body.innerText.includes('STOCKS UNTRUSTED') === false`. Gate ships correctly.
- Em-dash (`—`) counts on pages are normal — they are placeholder/empty-state glyphs in the design system (e.g. "Adapters: —" on the home V4 panel when no adapters loaded, em-dashes in glossary copy on `/docs`). None correspond to a card that should have shown a number — verified by inspecting the card label adjacent to each `—` via screenshots.
- All `:3001` ModelForge dashboard cards rendered numerical values: `GPU 6% · 41°C`, `44.6/122GB shared`, `Gen 1 · 0.125`, evolution status `IDLE`, etc. Champion card present.

### P2 — Cosmetic / minor / non-blocking

1. **`/api/ops/gates` shows `[FAILED] net::ERR_ABORTED` when navigating away from `/ops`.** This is a Playwright-side artifact, not a server bug — the `/ops` SPA fires `/api/ops/gates` on mount, and when we navigated to `/v4` the in-flight request was cancelled by the browser. Direct curl of the endpoint returns HTTP 200. No action required, just noting why it appears in the network log on `/v4`.
2. **ModelForge landing (`:3001/`) has no in-page navigation links.** The marketing landing only exposes `Dashboard →` buttons that route via JS click handlers. Sub-routes (`/dashboard`, `/adapters`, `/lineage`, `/models`, `/jobs`, `/forge`) all return HTTP 200 when probed directly. Operators reach the dashboard via the button; this is by-design but worth noting as it makes route discovery slightly opaque.
3. **ModelForge dashboard polls heavily.** On a single load of `:3001/dashboard`, we observed ~38 distinct API calls within seconds — `/api/evolve/status`, `/api/campaigns/status`, `/api/system/gpu`, `/api/lineage/activity` etc. fire in repeating cycles. All return 200 quickly so no failure, but if cost or load ever becomes a concern this is the place to look at polling intervals.
4. **`/docs` has 124 em-dashes in body text.** All are intentional copy/glossary punctuation, not empty-state placeholders. Verified by inspection of the screenshot.

### P3 — Nits

- ModelForge `/api/health`, `/api/models` (the bare `/api/models` without trailing slash differs from the `/api/models/champion` used by the dashboard), `/api/jobs`, `/api/status` return **401 Unauthorized** when probed directly without auth — this is expected for the unauthenticated probe; the dashboard SPA reaches them through an authenticated bearer/session.

---

## Page health matrix

| # | URL | Console errors | Console warnings | Network failures (true) | Screenshot | Notable |
|---|-----|---|---|---|---|---|
| 1 | `http://localhost:8081/` | 0 | 0 | 0 | `audit/2026-05-14-night/shots/01-dashboard.png` | UNTRUSTED pill correctly hidden; 0 NaN/undefined |
| 2 | `http://localhost:8081/ops` | 0 | 0 | 0 | `audit/2026-05-14-night/shots/02-ops.png` | 41 distinct ops API calls all 200 |
| 3 | `http://localhost:8081/v4` | 0 | 0 | 0 (1 ABORTED carry-over from /ops nav) | `audit/2026-05-14-night/shots/03-v4.png` | nav exposes 8 sub-routes |
| 4 | `http://localhost:8081/v4/debate` | 0 | 0 | 0 | `audit/2026-05-14-night/shots/04-v4-debate.png` | SSE stream subscribed |
| 5 | `http://localhost:8081/v4/risk` | 0 | 0 | 0 | `audit/2026-05-14-night/shots/05-v4-risk.png` | montecarlo/preview 200 |
| 6 | `http://localhost:8081/v4/parity` | 0 | 0 | 0 | `audit/2026-05-14-night/shots/06-v4-parity.png` | parity API 200 |
| 7 | `http://localhost:8081/v4/screening` | 0 | 0 | 0 | `audit/2026-05-14-night/shots/12-v4-screening.png` | screening API 200 |
| 8 | `http://localhost:8081/docs` | 0 | 0 | 0 | `audit/2026-05-14-night/shots/07-docs.png` | em-dashes are glossary copy |
| 9 | `http://localhost:3001/` | 0 | 0 | 0 | `audit/2026-05-14-night/shots/08-modelforge.png` | landing page; nav via JS button |
| 10 | `http://localhost:3001/dashboard` | 0 | 0 | 0 | `audit/2026-05-14-night/shots/09-modelforge-dashboard.png` | high-frequency polling, all 200 |
| 11 | `http://localhost:3001/adapters` | 0 | 0 | 0 | `audit/2026-05-14-night/shots/10-modelforge-adapters.png` | adapters API 200 |
| 12 | `http://localhost:3001/lineage` | 0 | 0 | 0 | `audit/2026-05-14-night/shots/11-modelforge-lineage.png` | lineage/tree API 200 |

(12 rows because we visited 12 pages — counting `/v4` landing + 4 v4 sub-routes as 5, plus 3 on 8081 legacy + 3 on 3001.)

---

## Methodology notes

For each page:
1. `browser_navigate(url)` — single navigation, no reloads.
2. `browser_console_messages(level=warning)` — captures `error` and `warning` (warning-level includes errors per the tool's "more severe levels" semantic).
3. `browser_network_requests(static=false)` — non-static requests only (filters out images/fonts/JS bundles, keeps API calls).
4. `browser_take_screenshot(fullPage=true)` — saved into `audit/2026-05-14-night/shots/`.
5. `browser_evaluate(...)` — counts of `—`, `NaN`, `undefined` in `document.body.innerText`; presence of error overlay; presence of `STOCKS UNTRUSTED` pill.

For the `/` UNTRUSTED gate verification specifically: confirmed `document.body.innerText.includes('STOCKS UNTRUSTED') === false` while `/api/ops/market_hours` reports `market_open_now=false`. Gate is correct.

No buttons clicked. No modals opened. No forms touched. No reload/refresh storms. All ABSOLUTE CONSTRAINTS in the task brief honoured.

---

## Summary

**P0: 0 · P1: 0 · P2: 4 · P3: 1.** All 11 (12 incl. `/v4` landing) pages render cleanly with zero console errors, zero network failures, zero NaN/undefined leakage. The recently-shipped STOCKS-UNTRUSTED gate works correctly. No regressions visible from prior dashboard work.
