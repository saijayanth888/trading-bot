# Quanta V4 · Frontend

Greenfield Vite + React 19 + shadcn/ui + Tailwind + Geist operator console.
Lives at `frontend-v4/` (this directory) and ships to `/v4/*` on the existing
FastAPI dashboard (`user_data/dashboard/app.py`, port 8081). The legacy React
UMD SPA at `/` and the Jinja `/ops` page stay running unchanged.

## Quick start

```bash
cd frontend-v4
npm install          # first time
npm run dev          # → http://localhost:5173 (proxies /api/* to :8081)
```

Production:

```bash
npm run build        # → dist/
# Restart the FastAPI dashboard once dist/ exists; it auto-mounts /v4/*.
```

Lint + typecheck:

```bash
npm run lint
npm run typecheck
```

## Stack

| Layer | Choice |
|---|---|
| Bundler | Vite 6 |
| UI | React 19 + react-router 7 |
| Components | shadcn/ui primitives (radix-ui under the hood) |
| Styling | Tailwind 3.4 with custom token system mapped to CSS vars |
| Fonts | Geist Sans + Geist Mono via `@fontsource/geist-*` |
| Data | TanStack Query v5 for REST; raw EventSource for SSE |
| Charts | Recharts |
| State | Zustand (ui + debate stores) |
| Markdown | marked + DOMPurify |

## Pages

| Path | What it shows |
|---|---|
| `/` (Overview) | Hero scoreboard + live debate + screening grid + service health |
| `/debate` | Full-width live debate transcript with SSE replay |
| `/risk` | Monte Carlo path viewer; deep-link via `?trade=<id>` |
| `/adapters` | LoRA promotion timeline · Pareto chart · 1-click rollback |
| `/parity` | Backtest ↔ live divergence dashboard + DG-2 gate progress |
| `/screening` | 27-name universe grid · convergence funnel |
| `/weekly` | Live Markdown preview of Friday's `docs/weekly/YYYY-WW.md` |
| `/diagnostics` | Probes + regime |
| `/ops` (external) | Link back to the legacy SPA |

## Backend touchpoints

Existing endpoints reused:

- `GET /api/ops/combined_portfolio` — hero scoreboard
- `GET /api/ops/regime` — regime strip
- `GET /api/ops/services` — health probes

NEW endpoints (`user_data/dashboard/v4_routes.py` — auto-mounted from `app.py`):

- `GET /api/v4/debate/history`
- `GET /api/v4/debate/stream/{session_id}` (Server-Sent Events)
- `GET /api/v4/montecarlo/{trade_id}`
- `GET /api/v4/adapters`
- `POST /api/v4/adapters/{id}/rollback`
- `GET /api/v4/weekly/preview`
- `GET /api/v4/parity`
- `GET /api/v4/screening`

All eight return deterministic stub payloads today. Each handler is one swap
away from calling the real V4 module once those land in `quanta_core/`.

## File tree

```
frontend-v4/
├── eslint.config.js     # ESLint 9 flat config
├── index.html
├── package.json
├── postcss.config.js
├── public/favicon.svg
├── src/
│   ├── App.tsx                                 # top-level layout
│   ├── main.tsx                                # bootstrap + Router + QueryClient
│   ├── components/
│   │   ├── layout/{TopBar,Sidebar}.tsx
│   │   ├── quanta/                             # domain components
│   │   │   ├── AdapterVersionTimeline.tsx
│   │   │   ├── BacktestParityDashboard.tsx
│   │   │   ├── DebateTranscriptLive.tsx
│   │   │   ├── HeroScoreboard.tsx
│   │   │   ├── MonteCarloPathViewer.tsx
│   │   │   ├── RegimeStrip.tsx
│   │   │   ├── ScreeningGrid.tsx
│   │   │   ├── SystemHealthStrip.tsx
│   │   │   └── WeeklyPreviewLive.tsx
│   │   └── ui/                                 # shadcn-style primitives
│   ├── hooks/useDebateStream.ts
│   ├── lib/{api,cn,format,query,seeded,sse}.ts
│   ├── pages/
│   ├── store/{ui,debate}.ts
│   ├── styles/globals.css                      # tokens + Geist
│   └── types/v4.ts                             # the V4 contracts
├── tailwind.config.ts
├── tsconfig.json
└── vite.config.ts
```

## Design tokens

Names match `user_data/dashboard/static/css/quanta.css` so the legacy SPA
and V4 share a palette:

```
--bg-page · --bg-card · --bg-card-2 · --bg-inset · --bg-overlay · --bg-rail
--stroke-1..3 · --text-1..4
--success/-bg/-line · --danger/-bg/-line · --warn/-bg/-line
--accent/-bg/-line · --info/-bg/-line
--font-sans (Geist) · --font-mono (Geist Mono)
```

Light theme toggles `data-theme="light"` on `<html>` and re-runs the same
variable set. Persisted via localStorage `quanta_v4_theme`.

## Aesthetic rules (preserved from `memory/feedback_dashboard_design.md`)

- No drop shadows. Use 1 px borders + bg layers.
- No gradients (except the explicit MC envelope and the bull→bear divider).
- No serif-italic. Everything sans or mono.
- Numbers always tabular — the `.num` utility class enforces it.

## Notes

- `npm run build` runs `tsc --noEmit` first; if you want a faster iteration
  loop use `npm run build:nocheck`.
- The SSE stream sends a deterministic dummy debate (~5 seconds) per
  session ID so reloads replay the same dialogue. Hot-swap the `v4_routes`
  handler for the real `quanta_core.agents.debate.events` subscription
  when that module lands.
- The screening grid reads `user_data/universe.json` directly so adding a
  symbol there shows up in the V4 grid after a dashboard restart (same
  contract as `/api/universe`).
