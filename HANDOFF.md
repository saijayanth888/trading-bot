# HANDOFF · `feat/v4-wave2-frontend-v2` · V4 frontend wave-2

**Branch:** `feat/v4-wave2-frontend-v2` (off `feat/v4-wave2-quality` HEAD `791308b`,
which itself is off `main`'s wave-2 plan commit — no divergent code, only
docs above the cut point).
**Worktree:** `.claude/worktrees/agent-a8e1a0734cecef701`
**Date:** 2026-05-12
**Commits:**
- `e52feb3` — `v4(wave-2/frontend): greenfield Vite + React 19 + shadcn/ui operator console`
- `b1f4b88` — `HANDOFF.md · V4 wave-2 frontend session`

> **Branch-name note:** the dispatch asked for `feat/v4-wave2-frontend`, but a
> branch by that exact name already existed locally (carrying the pre-existing
> bug-fix work `merge: fix/pre-existing-bugs — risk_governor dedup …`). To
> avoid clobbering that, this work landed on `feat/v4-wave2-frontend-v2`
> instead. Operator should rename or pick whichever they prefer at merge time
> — the commit graph is clean and re-pointable with `git branch -m`.

---

## 1 · V3 hunt — what was found

Agent G's `V3-FRONTEND-HUNT.md` was not on disk at commit time (15-minute
wait expired with no doc landing). The only V3-era artifact discovered:

- Worktree `feat/quanta-next-shell` carries `quanta-next/index.html` —
  shell-only (TopBar, sidebar, no `app.js`, no `data.js`, no `styles.css`).
  Branched off `gpu-reservation-phase1`.
- The accompanying `scripts/PROMPT.md` (~1100 lines) documents the full
  design system that *would have* been V3 — Inter + JetBrains Mono,
  decimal-tagged card heads, custom SVG icons for the 5 debate roles,
  debate-row-floor layout, `el()`/`s()` DOM helpers, frozen `window.QUANTA`
  schema.

**V3 contribution to V4:**

- TopBar/sidebar IA preserved verbatim (brand mark, chip strip, equity
  stat, ET clock, KILL · ARM button, ◐ theme toggle, MONITOR/ANALYSIS/
  SYSTEM/REFERENCE rail groups with numeric chips).
- Design token names (`--bg-page`, `--stroke-1..3`, `--success-bg` etc.)
  carried over from `user_data/dashboard/static/css/quanta.css`.
- Type-rank discipline: Geist replaces Inter as primary sans; Geist Mono
  retained as the universal number face; numbers always tabular.

**Greenfield (no V3 precedent):**

- All six V4-unique surfaces (debate SSE, Monte Carlo viewer, adapter
  Pareto + rollback, weekly Markdown preview, backtest parity, 27-name
  screening).
- Component framework: React 19 + shadcn/ui + Tailwind 4 + Vite 6
  (V3 was vanilla HTML/CSS/JS, no build step, no JSX).
- State: Zustand stores (ui · debate) — V3 was IIFE-scoped.
- Data: TanStack Query v5 + raw EventSource — V3 read a static
  `window.QUANTA` snapshot.
- Routing: react-router 7 with `basename="/v4"` — V3 was hashchange-driven.

---

## 2 · Component map

```
frontend-v4/src/
├── App.tsx                                # layout shell + <Routes>
├── main.tsx                               # bootstrap + QueryClient + Router
├── components/
│   ├── layout/
│   │   ├── TopBar.tsx                     # brand · chips · equity · clock · KILL · theme
│   │   └── Sidebar.tsx                    # MONITOR / MODELS / UNIVERSE / SYSTEM
│   ├── quanta/                            # domain components
│   │   ├── HeroScoreboard.tsx             # capital · live P&L · DD ribbon
│   │   ├── RegimeStrip.tsx
│   │   ├── SystemHealthStrip.tsx
│   │   ├── DebateTranscriptLive.tsx       # SSE-driven 5-role bubbles
│   │   ├── MonteCarloPathViewer.tsx       # animated 10k paths + p05/p95 cones
│   │   ├── AdapterVersionTimeline.tsx     # 6 roles tabs · Pareto · rollback
│   │   ├── BacktestParityDashboard.tsx    # weekly divergence bars + cutover gate
│   │   ├── WeeklyPreviewLive.tsx          # marked + DOMPurify of Friday post
│   │   └── ScreeningGrid.tsx              # 27 names with traded highlight
│   └── ui/                                # shadcn primitives
│       └── {button,card,chip,dialog,progress,scroll-area,select,
│            separator,stat,tabs,tooltip}.tsx
├── hooks/useDebateStream.ts               # EventSource → debate store
├── lib/
│   ├── api.ts                             # fetch wrapper + endpoint catalog
│   ├── cn.ts                              # tailwind-merge + clsx
│   ├── format.ts                          # fmtMoney/fmtPct/fmtPx/fmtAgo
│   ├── query.ts                           # QueryClient defaults
│   ├── seeded.ts                          # deterministic RNG (chart fallbacks)
│   └── sse.ts                             # EventSource wrapper
├── pages/
│   └── {Overview,Debate,Risk,Adapters,Parity,Screening,Weekly,
│         Diagnostics,NotFound}.tsx
├── store/
│   ├── ui.ts                              # theme · density · pair
│   └── debate.ts                          # current session + partial tokens
├── styles/globals.css                     # tokens + Geist imports
└── types/v4.ts                            # AgentVote / MontecarloRun / etc.
```

**LOC:** 2728 TS/TSX across 43 source files (target was 2.5-3.5k).

---

## 3 · Routing + page layout

```
/v4/                  Overview     Hero + Debate + Regime + Screening + Health
/v4/debate            Debate       Full-width DebateTranscriptLive
/v4/risk?trade=<id>   Risk         MonteCarloPathViewer + selector
/v4/adapters          Adapters     AdapterVersionTimeline (6 role tabs)
/v4/parity            Parity       BacktestParityDashboard
/v4/screening         Screening    ScreeningGrid (27 names)
/v4/weekly            Weekly       WeeklyPreviewLive (Markdown)
/v4/diagnostics       Diagnostics  Probes + regime
/ops (external)       Legacy SPA — links out
```

The Router uses `basename="/v4"` so production deep links work natively.
In dev (port 5173), `basename` still applies — open `http://localhost:5173/v4/`.

Top-bar is sticky · sidebar is sticky `(top-14, calc(100vh-3.5rem))` ·
content is `max-w-[1500px]` centered.

---

## 4 · How to dev / build / serve

```bash
cd frontend-v4
npm install            # first time — 456 packages, ~12s
npm run dev            # → http://localhost:5173/v4/   (vite proxies /api/* → :8081)
npm run build          # tsc --noEmit + vite build → dist/
npm run preview        # serve dist/ on :4173
npm run lint           # eslint --max-warnings=0
npm run typecheck      # tsc --noEmit only
```

After `npm run build`, restart the FastAPI dashboard:
`v4_routes.mount(app)` auto-detects `frontend-v4/dist/` and mounts it
at `/v4/*` on port 8081. No manual route definition needed.

To target a non-default dashboard:

```bash
VITE_PROXY_TARGET=http://192.168.1.49:8081 npm run dev
```

---

## 5 · Integration touchpoints

### Existing endpoints (read-only · reused as-is)

- `GET /api/ops/combined_portfolio` — hero scoreboard + top-bar equity
- `GET /api/ops/regime` — RegimeStrip
- `GET /api/ops/services` — SystemHealthStrip

(Catalog lives in `frontend-v4/src/lib/api.ts` for future expansion.)

### NEW endpoints (`user_data/dashboard/v4_routes.py` — 8 routes)

| Endpoint | Returns |
|---|---|
| `GET  /api/v4/debate/history` | recent debate sessions |
| `GET  /api/v4/debate/stream/{session_id}` | SSE — `session_start` → `vote_partial`* → `vote_complete` → `arbiter` → `decision` |
| `GET  /api/v4/montecarlo/{trade_id}` | 10k path summary + 120 sample paths + p05/p25/p50/p75/p95 envelopes |
| `GET  /api/v4/adapters` | 24 recent LoRA promotions across 6 roles |
| `POST /api/v4/adapters/{id}/rollback` | stub — returns 200 + timestamp |
| `GET  /api/v4/weekly/preview` | Markdown of "what would publish if Friday were now" |
| `GET  /api/v4/parity` | rows × weeks + `consecutive_days_ok` / `cutover_threshold_days` |
| `GET  /api/v4/screening` | 27-name snapshot reading `user_data/universe.json` |

**All eight are deterministic stubs** — each handler is a one-line swap
away from calling the real V4 module. SSE handler uses
`StreamingResponse(_debate_stream(...), media_type="text/event-stream")`.

### Wiring (in `user_data/dashboard/app.py`)

```python
from . import v4_routes
...
v4_routes.mount(app)
```

That single call adds the eight routes + (if `frontend-v4/dist/` exists)
mounts the SPA at `/v4/*`. No other changes to `app.py`.

---

## 6 · Verification

```
✔ npm install              (456 packages, no errors)
✔ npm run typecheck        (tsc --noEmit, zero errors)
✔ npm run lint             (eslint --max-warnings=0, zero errors)
✔ npm run build            (vite build, 2.58s, ~444 KB total gzipped)
✔ python3 -c "from user_data.dashboard.app import app"   (imports clean)
✔ 27 names in screening grid (matches universe.json: 12 crypto + 15 stocks)
✔ /v4 static mount registered, all 8 /api/v4/* routes registered
```

No tests added — frontend coverage isn't the metric per spec; visual shape
+ TypeScript strict + ESLint clean is.

---

## 7 · Aesthetic compliance

Per `memory/feedback_dashboard_design.md`:

- ✓ No drop shadows (verified via grep — only `shadow-[inset_...]` for
  the active-row highlight on screening grid)
- ✓ No gradients except the explicit Monte Carlo p05/p95 cone fills
- ✓ No serif-italic — Geist Sans + Geist Mono everywhere
- ✓ Numbers tabular via `.num` utility (font-variant-numeric: tabular-nums)
- ✓ All UI text English; uppercase tracking for labels matches the legacy
  10px / 0.10em pattern

---

## 8 · Commit shas (this branch)

```
b1f4b88  HANDOFF.md · V4 wave-2 frontend session — V3 hunt result, component map, integration touchpoints
e52feb3  v4(wave-2/frontend): greenfield Vite + React 19 + shadcn/ui operator console
         ↑ frontend-v4/ + user_data/dashboard/v4_routes.py
           + user_data/dashboard/app.py +6 lines
```

Parent: `791308b docs(v4): wave-2 sprint plan — 10 agents`
Branch base: `main` (via parent-of-parent `c18acfa wave-1 FINAL`).

> **Note for the reviewer/merger:** the worktree's HEAD was on
> `feat/v4-wave2-quality` (a pre-existing local branch) at commit time, so
> those two SHAs are also reachable from that branch. Since neither was
> pushed, this is harmless — `git reset --hard 791308b` on
> `feat/v4-wave2-quality` (if anyone needs it clean) drops only the two V4
> commits, which are preserved on `feat/v4-wave2-frontend-v2`.

**Not pushed to remote** (per spec — operator-reviewed merge).

---

## 9 · Next-session pickup list

1. **Real debate wiring** — when `quanta_core.agents.debate.events` lands,
   replace `_debate_stream()` in `v4_routes.py` with the real generator.
   The client expects exactly the `DebateEvent` union from
   `frontend-v4/src/types/v4.ts`.

2. **Real Monte Carlo** — swap `montecarlo()` body for a call into
   `quanta_core.risk.monte_carlo.run(trade_id)`. The MontecarloRun
   contract is locked at the type level.

3. **Real adapter registry** — proxy `adapters()` to mf-api
   (`http://localhost:8000/api/adapters`); proxy `adapter_rollback()` to
   the same.

4. **Real weekly preview** — call into
   `quanta_core.hermes.weekly_publisher.render_preview()` and return its
   rendered Markdown verbatim. The Jinja template at
   `docs/quanta-core-v4-rev2/12-WEEKLY_PUBLISHER.md` §2 is the contract.

5. **Real screening** — read `convergence_log` from Postgres instead of
   the deterministic dummy classifier in `_screen_row()`. Universe.json
   already drives the list of names.

6. **Optional: e2e tests** — Playwright on `/v4/*` would be cheap insurance
   once the real surfaces wire in.

7. **Wave-2 morning review** — operator should grep `frontend-v4/` and
   decide whether the IA matches expectations before agent G's hunt
   (if it ever lands) introduces alternative patterns.
