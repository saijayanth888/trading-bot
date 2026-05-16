# Trading Operator Console — Unified Redesign

**Status**: DRAFT — awaiting debate-team stress-test and operator final approval
**Author**: Claude Opus 4.7 (1M context) (team lead)
**Date**: 2026-05-16
**Deadline**: 2026-05-16 18:00 ET (22:00 UTC) — go-live target

---

## 1. Goals

Replace the legacy `/ops` SPA (single 6,577-line React-via-UMD page) and the React-19 `/v4` SPA (8 routed pages, mostly rendering `—`) with **one unified operator console** organized around the operator's actual workflow:

> **monitor** (am I OK?) → **detect** (what needs attention?) → **intervene** (one click)

Native integration with the **Hermes agent scheduler** (cron schedule, recent run history, agent health) inline as first-class panels. **ModelForge** stays as a side-link only (its 95 mf-api endpoints get a v2 surface, not today's). Bake in fixes for the 15 verified bugs in the inventory (`bugs-and-pain-points.md`). Hard data-preservation constraint per operator: zero `DROP`, zero `rm`, zero `--force-recreate`.

## 2. Non-goals

- Replacing ModelForge frontend marketing page at `:3001` — out of scope for v1. The 95 mf-api endpoints get a dedicated `mf-console` build in v2.
- Mobile/tablet responsive — operator is on a 1440-1920px desktop.
- Multi-tenant or multi-user auth — single operator, loopback + Tailscale, `require_mcp_key` for mutating endpoints stays as-is.
- Custom chart engines — reuse `recharts` (already in v4) for the few sparklines / equity-curve panels. No new TradingView-style chart widget.
- Replacing the trading engine — `quanta-core` and `shark` are untouched.

## 3. Design direction (operator-chosen)

**Workflow-zoned: monitor / detect / intervene.** Three vertical zones, top to bottom, each scaled by importance:

```
┌─ MONITOR ─────────────────────────────────────────────────────┐
│  status banner · capital · day P&L · drawdown · sparkline · regime │
├─ DETECT (N) ──────────────────────────────────────────────────┤
│  priority feed of items needing attention, sorted by urgency      │
│  (stale feeds · gate breaches · risk-cap violations · errors)     │
├─ STRATEGIES ──────────────────────────────────────────────────┤
│  per-strategy strips (crypto V4 · stocks/wheel · shark) — flash-on-change │
├─ INTEGRATIONS ────────────────────────────────────────────────┤
│  Hermes cron schedule · recent agent runs · ModelForge side-link  │
├─ INTERVENE ───────────────────────────────────────────────────┤
│  KILL · PAUSE · FLATTEN · ModelForge promote · Hermes retrigger   │
└────────────────────────────────────────────────────────────────┘
```

Per the market-research output, this maps directly to Hudson River Trading's monitoring decomposition + the Datadog/PagerDuty/NASA-MCT pattern: aggregate state up top, attention-sorted detail below, isolated dangerous actions at the bottom or top-right.

## 4. Frontend architecture

### 4.1 Stack (reuse where it earns its keep)

- **Build**: Vite + React 19 + TypeScript 5 — same as current `frontend-v4`. We keep the existing build pipeline and shadcn/Tailwind 4/Geist Mono/Geist Sans typography. The market researcher confirmed Geist Mono for numeric columns is appropriate.
- **State**: TanStack Query v5 for server state (already in v4). No global store needed beyond `useUi` (theme, density).
- **Realtime**: Native WebSocket from a new `/api/v5/stream` endpoint pushing diffs. Initial render is a REST snapshot via TanStack Query; WS keeps it fresh. Falls back to 10s polling if WS fails.
- **Routing**: Single page, no router. The whole console is one scroll surface. (Sidebar nav from `/v4` is removed — it was anchor-scroll, not real routing, and the redesign collapses content into one priority-sorted feed anyway.)
- **Path**: served from `/` (replaces `/ops`). The old `/v4/` redirects to `/` for transition. The old `/ops` URL also redirects.

### 4.2 Component tree

```
<App>
├── <TopBar>              status banner · clock · KILL button (isolated)
├── <Monitor>             aggregate state, always visible (sticky)
│   ├── <CapitalCard>     equity · day P&L · sparkline · staleness chip
│   ├── <DrawdownRibbon>  current DD vs pause/kill thresholds
│   └── <RegimeChip>      crypto · stocks regimes (unified — bug B12 fixed)
├── <DetectFeed>          priority feed, sorted by severity
│   ├── <AlertItem>       stale-feed alerts (with NYSE-closed awareness — bug B7)
│   ├── <AlertItem>       gate breaches (sharpe disagreement — bug B3)
│   ├── <AlertItem>       risk-cap violations (BTC stake 34× — bug B8)
│   ├── <AlertItem>       inconsistent stats (shark wins=0 losses=0 — bug B2)
│   └── ...
├── <Strategies>          per-strategy strips, flash-on-change
│   ├── <StrategyStrip kind="crypto-v4">
│   ├── <StrategyStrip kind="stocks-wheel">
│   └── <StrategyStrip kind="shark">
├── <Integrations>
│   ├── <HermesPanel>     cron table · last-N runs · agent health
│   └── <ModelForgeSideLink>  links to mf-api endpoints; v2 native panel
└── <Intervene>           isolated bottom-right dock
    ├── <KillAllButton>   single-confirm modal (NNGroup pattern)
    ├── <PauseButton>     per-strategy
    └── <FlattenButton>   per-position
```

### 4.3 Design principles applied (from market research)

| Principle | How we apply it |
|---|---|
| Dense, not sparse | 5 zones × multiple cards each, ~15-20 components in 1440-1920px viewport |
| Sticky status banner | `<TopBar>` is `position: sticky; top: 0`, green/amber/red rollup |
| Color: dual-encoded, Wong palette | P&L: muted green/red kept (operator convention) but always with `+/-` sign + arrow icons. Status banner uses blue/orange/red with explicit text labels. |
| Saturation ramps with urgency | `<AlertItem>` severity → opacity: 0.5 (info) → 0.8 (warning) → 1.0 (danger). Border-left color also encodes. |
| Flash-on-change, NOT NumberRoll | Cell background fades to accent color over 200-400ms then returns. `NumberRoll` only on the single hero capital number and total-day-PnL number. |
| Data staleness first-class | Every card has a freshness footer `feed: 12s ago` or `feed: STALE 17h (NYSE closed)`. Border tint shifts when >threshold. |
| Kill switch isolated, single-confirm | `<KillAllButton>` lives bottom-right, never adjacent to routine buttons. Click → modal: "This will pause crypto AND flatten all stocks positions. Confirm." → action. |
| Dark theme default | Tailwind 4 + CSS vars; theme toggle preserved from `useUi` store. |

### 4.4 What we kill from the existing surface

- The 24-card `/ops` grid (it's the wall-of-cards anti-pattern).
- The 8-page `/v4` sidebar nav (anchor-scroll, not real routing).
- The big `NumberRoll` on every numeric tile (continuous animation across 12 pairs is perceptual noise — research confirmed).
- The two separate "regime" pills (crypto regime + stock regime were both rendered with the same source, mislabeling stocks — bug B12).
- The bare ANSI status pills (`QUANTA OK`, `STALLED`, etc.) without composite context.

## 5. Backend API redesign — `/api/v5/*`

### 5.1 Principles

- **One truth per metric.** Sharpe is computed in one place (`producers.metrics`); every consumer reads from there. Same for max-DD, win rate, total-PnL. Kills bugs B2 and B3.
- **Union producers at the API layer, not the UI.** `/api/v5/positions` unions `quanta_schema.fills` AND `wheel-state/account_snapshot.json` (kills bug B6, B9).
- **Explicit per-side day-P&L.** `/api/v5/portfolio` emits `{combined, crypto, stocks}` with `day_pnl_usd` per side (kills bug B1).
- **Staleness as a first-class field.** Every endpoint includes `_meta: {age_s, stale, snapshot_ts, market_open_now}`. UI consumes consistently.
- **Envelope retired.** v5 endpoints return data directly (no `{status, data, error}` wrapper). Errors → HTTP status + RFC 7807 problem-detail.
- **WebSocket.** `/api/v5/stream` pushes a diff payload `{path, op, value, ts}` per change. Front loads via REST then upgrades.

### 5.2 Endpoint inventory (v5 surface)

| Endpoint | Purpose | Replaces |
|---|---|---|
| `GET /api/v5/status` | Aggregate operator state (green/amber/red + counts of each in detect-feed) | new |
| `GET /api/v5/portfolio` | Capital, equity, peak, drawdown, day-PnL per side, all with `_meta` | `/api/ops/combined_portfolio` |
| `GET /api/v5/positions` | UNION of crypto fills + wheel state + shark holdings | `/api/v4/positions` (partial), `/api/ops/stocks` (partial) |
| `GET /api/v5/alerts` | Priority feed: stale feeds, gate breaches, risk-cap violations, errors | new (aggregates many ops endpoints) |
| `GET /api/v5/strategies/{kind}` | Per-strategy strip data (crypto-v4 / stocks-wheel / shark) | `/api/ops/regime`, `/api/ops/stocks`, `/api/ops/flash_status` |
| `GET /api/v5/metrics` | Sharpe, max-DD, win rate — single producer | `/api/ops/readiness`, `/api/ops/backtest_gates` (fixed) |
| `GET /api/v5/hermes/schedule` | Cron jobs.json with next-fire timestamps | new (read `~/.hermes/cron/jobs.json` directly) |
| `GET /api/v5/hermes/runs?limit=20` | Recent agent runs with status + duration + output snippet | new (parse `~/.hermes/cron/output/*.md` and log files) |
| `GET /api/v5/hermes/health` | Per-service heartbeat (hermes_gateway / mcp / dashboard) | `/api/ops/services` (subset) |
| `WS /api/v5/stream` | Diff stream of all v5 GET surfaces | new |
| `POST /api/v5/actions/kill` | Kill-all (pause crypto + flatten stocks) | `/api/ops/pause` (composite) |
| `POST /api/v5/actions/pause/{kind}` | Pause a strategy | `/api/ops/pause` |
| `POST /api/v5/actions/flatten/{symbol}` | Flatten a position | new |
| `POST /api/v5/actions/hermes/retrigger/{job}` | Manually re-fire a Hermes cron entry | new |

### 5.3 Legacy endpoints

Per the operator's "refactor freely" call, the old surfaces are **removed in v1**:

- `/api/ops/*` (40 endpoints) — sunset. The dashboard `ops_routes.py` will delete each route as v5 absorbs its responsibility.
- `/api/v4/*` (8 endpoints) — sunset.
- The `/v4/` static SPA — sunset. `frontend-v4/dist` rebuilds replace the old assets.

**BEFORE deletion**, the improvement team will run a `grep` audit across:
- `scripts/` (host-side cron, auto_rollback.py, etc.)
- `~/.hermes/scripts/`
- `user_data/modules/` (notifier, slack_alerts, monitoring_mixin)
- `mf-api/` (if any cross-call exists)

Any hit gets one of: (a) migrated to v5, (b) preserved with a thin compat shim. **No silent breakage.**

### 5.4 Data preservation guarantees

Per the operator's "don't lose the data" constraint:

- **No `DROP TABLE`, no `ALTER TABLE ... DROP COLUMN`** on TimescaleDB. Schema is additive only. The existing hypertables (`trade_journal`, `regime_log`, `meta_signal_log`, `sentiment_log`, `derivatives_features`, `macro_features`, `news_headlines`, `mvrv_ratio`, `exchange_netflow`, `fear_greed_log`, `whale_transactions`, `regime_model_meta`, `classifier_config`, `classifier_log`) stay.
- **No file deletions** on host. `~/Documents/.dgx-train/shark/memory/*`, `~/Documents/.dgx-train/shark/wheel-state/*`, `~/.hermes/cron/jobs.json`, `~/.hermes/scripts/*`, `user_data/data/*.json`, `stocks/memory/*.md` all preserved.
- **No docker volume `rm`** — `tradebot-postgres-data`, `mf-postgres-data`, `mf-redis-data` untouched.
- **No `graphify-out/`** reset — we may rebuild after the redesign lands, but only after the user explicitly asks.
- Improvement team will be told in its prompt: edits to source files only. The verifier role checks the workspace for any of: `rm -rf`, `DROP`, `ALTER ... DROP`, `--force-recreate`, `docker volume rm`, `git reset --hard` and fails the diff if found.

## 6. Bug fixes baked into v1

Each numbered ID below maps to `bugs-and-pain-points.md`:

| Bug | Fix location | Owner |
|---|---|---|
| **B1** `stocksMove` poisons day-PnL | Producer emits `stocks.day_pnl_usd` from Alpaca `last_equity`; UI consumes it directly | backend-engineer |
| **B2** shark stats `wins=0/losses=0` despite 5 trades | Single classifier in `producers.shark_stats`; daily-summary writes both counters together | backend-engineer |
| **B3** Sharpe 10.58 vs −306 contradiction | Single `producers.metrics` module; guards against zero-mean walk-forward windows; annualization correct | backend-engineer + database-engineer (audit math) |
| **B4** v4 `CombinedHeader` type understates | The v5 schema is TypeScript-codegen'd from FastAPI Pydantic; no manual type drift | frontend-engineer |
| **B6/B9** `/api/v4/positions` ignores wheel | `/api/v5/positions` unions postgres `quanta_schema.fills` + `wheel-state/account_snapshot.json` + shark holdings | backend-engineer |
| **B7** stocks-snapshot-stale gate confusion | All cards show `feed: STALE 17h (NYSE closed)` explicitly; ribbon stays muted | frontend-designer + backend-engineer |
| **B8** BTC 34× single-name-cap violation | `producers.risk` enforces cap at ENTRY and emits `risk_alerts` to `/api/v5/alerts`; UI surfaces it in detect feed | backend-engineer + critic (the risk-governor itself needs root-cause review — likely separate ticket; v1 ensures the operator can SEE it) |
| **B10** `hermes_mcp` stuck `activating` | Hermes health producer reports composite (heartbeat + last-fire age); detect-feed alert if `activating` >30 min | backend-engineer |
| **B12** Per-stock regime label uses crypto regime | `/api/v5/strategies/stocks-wheel` reads `/api/ops/stock_regime` correctly | backend-engineer |
| **B13** `num` collisions in `/ops` | Moot — `/ops` is retired | n/a |
| **B14** override-health snapshot 28h stale unflagged | `_meta.stale` propagates to UI; card shows STALE chip | frontend-engineer |
| **B15** v4 HeroScoreboard renders `—` | Moot — `/v4` Overview replaced by new `<Monitor>` zone | n/a |

## 7. Hermes integration (first-class)

The new dashboard reads three Hermes sources directly:

1. **Schedule**: `~/.hermes/cron/jobs.json` parsed into a table (job name, cron expr, next fire, last fire, last status).
2. **Run history**: `~/.hermes/cron/output/<job_id>/*.md` (each file is one run's output, named `YYYY-MM-DD_HH-MM-SS.md`). Display newest-first, parse `# Cron Job: <name>` header + tail for status/output snippet.
3. **Health**: heartbeat files (gateway, mcp, dashboard) — composite health rollup feeds the top-bar status banner.

Operator actions exposed in v1:
- Re-fire a job manually (`POST /api/v5/actions/hermes/retrigger/{job}`)
- View full run output (modal, no leave-page)
- Acknowledge an alert (writes to `~/.hermes/cron/acks.json` — additive file)

## 8. Migration plan (cutover)

This is one-shot — no rolling migration, no feature flag, no canary. The operator's "refactor freely" approval covers this. **Order matters** to satisfy the no-data-loss constraint:

1. Build the v5 backend producers + endpoints (additive — both old and new exist briefly).
2. Build the v5 frontend (in `frontend-v5/` — fresh dir).
3. Wire `dashboard` container to serve `/` from `frontend-v5/dist`; `/api/v5/*` mounted.
4. Audit hidden callers of `/api/ops/*` and `/api/v4/*`. Migrate or shim.
5. Switch the live dashboard image to serve `/` from v5; keep `/api/ops/*` deprecated routes returning `410 Gone` with a Location pointer for 7 days, then delete the route handlers.
6. Verify all containers healthy. Verify `quanta-core` cycle still emits to postgres. Verify Hermes crons fire on schedule.
7. Operator end-to-end sanity check: scoreboard tiles all populate, kill switch works, Hermes panel shows the next scheduled job.

If any step fails, `git revert` the latest commits and `docker compose up -d --no-build dashboard` restores the previous image.

## 9. Out of scope for v1 (v2 tickets)

- Full ModelForge native panel (adapters · campaigns · EPT lineage · evals) — the 95 mf-api endpoints. v1 ships a side-link only.
- Multi-monitor / detachable panels.
- Custom alert routing (Slack / Telegram beyond what `notifier` already does).
- Historical replay mode ("show me the dashboard as of 2026-05-15 16:00").
- Charting beyond sparklines + small equity curves.
- Mobile responsiveness.

## 10. Team plan + timeline (one-day)

- **Phase E — 3 parallel debate teams** (15 min wall each, in parallel): frontend-design / backend-design / functional-coverage. Surface verdicts to operator inline.
- **Phase F — improvement team** (target 90-120 min): scout maps existing code, builder splits into frontend-designer + frontend-engineer + backend-engineer + database-engineer with pipeline dependencies, verifier validates each fix. Hard data-preservation guards.

Total runway from 14:25 ET (now) to 18:00 ET = **3h 35min**. Phase E ~30 min, Phase F ~120 min, buffer ~45 min for integration smoke + verification.

## 11. Open questions for operator

(asked inline as needed; none blocking spec approval)

1. The BTC 34× single-name-cap violation (B8) — is the producer-side fix in scope for v1, or only the dashboard surfacing? (Default: surface in v1, root-cause in v2.)
2. Is a top-right `KILL ALL` button acceptable, or do you want the kill action behind a sidebar toggle? (Default: top-right, single-confirm modal.)
3. Color: keep the operator-trained green/red for P&L (with sign + arrow), or migrate fully to Wong blue/orange? (Default: keep green/red, add arrows and signs for colorblind safety.)
