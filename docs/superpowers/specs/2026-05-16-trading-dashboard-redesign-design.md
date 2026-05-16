# Trading Operator Console — Unified Redesign

**Status**: APPROVED v2 — debate-revised, operator scope-confirmed, ready for impl team
**Author**: Claude Opus 4.7 (1M context) (team lead)
**Date**: 2026-05-16
**Deadline**: 2026-05-16 18:00 ET (22:00 UTC) — go-live target
**Revision history**: v1 draft (commit `651e1a6`) → v2 (this file) integrates verdicts from 3 debate teams (`debate/{frontend,backend,functional}/summary.md`) + operator scope answers.

---

## 1. Goals

Replace the legacy `/ops` SPA (6,577-line React-via-UMD) and the React-19 `/v4` SPA (8 routed pages, mostly `—`) with **one unified operator console** organized around the operator's workflow:

> **monitor** (am I OK?) → **detect** (what needs attention?) → **intervene** (one click)

Native **Hermes** integration (cron schedule, recent run history, agent health, retrigger action). **ModelForge** stays as a side-link only (95 mf-api endpoints get v2 surface). Bake in fixes for all 15 inventory bugs. Hard data-preservation constraint.

## 2. Non-goals

- Replacing ModelForge frontend at `:3001` — v2.
- Mobile/tablet responsive — operator is on 1440-1920px desktop.
- Multi-tenant auth — single operator, loopback + Tailscale.
- Custom chart engines — reuse `recharts`.
- Replacing the trading engine — `quanta-core` and `shark` untouched.

## 3. Design direction (operator-chosen)

**Workflow-zoned: monitor / detect / intervene.** TopBar sticky; Monitor scrolls; Intervene is a fixed bottom-right dock (NOT a band). Per frontend-debate G2 (`debate/frontend/summary.md`):

```
┌─ TOPBAR (sticky) ─────────────────────────────── KILL · ARM ┐
│  ●all clear  $118,292.03  +0.00% day  WS▰  10:47 ET         │
├─ MONITOR (scrolls) ─────────────────────────────────────────┤
│  capital · day P&L · drawdown ribbon · sparkline · regimes  │
├─ DETECT (N) ────────────────────────────────────────────────┤
│  priority feed: stale feeds, gate breaches, risk violations │
├─ STRATEGIES ────────────────────────────────────────────────┤
│  per-strategy strips — crypto-v4, stocks-wheel, shark       │
├─ INTEGRATIONS ──────────────────────────────────────────────┤
│  Hermes cron + run history + health · ModelForge side-link  │
├─ REGIME CONFIG · DECISION AUDIT · MCP TOOL CONSOLE (collapsed) ┤
│  (these three stay in v1 per operator scope call on G3)     │
└─────────────────────────────────────────────────────────────┘
                                            ┌─[fixed]──────┐
                                            │ INTERVENE    │
                                            │ ⛔ KILL ALL   │
                                            │ ⏸ pause      │
                                            │ ▣ flatten    │
                                            └──────────────┘
```

## 4. Frontend architecture

### 4.1 Stack

- **Build**: Vite + React 19 + TypeScript 5 (reuses `frontend-v4` toolchain).
- **State**: TanStack Query v5; per-component `refetchInterval` (per frontend-debate rebuttal — solves "can't pause heavy cards" without abandoning one-page topology).
- **Realtime**: WebSocket from `/api/v5/stream` for diffs. **Visible degradation indicator**: when WS is down, TopBar shows amber `polling 10s` chip AND every `<StaleChip>` footer reads `feed: 10s (polling)`. Per frontend-debate G6.
- **Routing**: single page, no router (per frontend-debate rebuttal); Vite code-splitting at `<Strategies>`, `<HermesPanel>`, `<RegimeConfigEditor>`, `<DecisionAudit>`, `<MCPConsole>` for first-load weight.
- **Path**: served from `/` (replaces `/ops`).
- **Type codegen**: `openapi-typescript` consumes FastAPI's `/openapi.json` at build time. The `<Monitor>` build is gated on codegen output landing in `frontend-v5/src/types/api.ts` (closes B4→B15 chain per frontend-debate G7).

### 4.2 Component tree

```
<App>
├── <TopBar>              [sticky top:0] status banner · clock · WS/poll chip · KILL button
├── <Monitor>             [scrolls; NOT sticky] aggregate state, top of fold
│   ├── <CapitalCard>     equity · day P&L · sparkline · <StaleChip>
│   ├── <DrawdownRibbon>  current DD vs pause/kill thresholds
│   ├── <RegimeChip kind="crypto">    reads /api/v5/strategies/crypto-v4.regime
│   └── <RegimeChip kind="stocks">    reads /api/v5/strategies/stocks-wheel.regime
├── <DetectFeed>          priority feed, sorted by severity
│   └── <AlertItem>       (stale, gate-breach, risk-violation, B2-class) + <StaleChip>
├── <Strategies>          per-strategy strips, flash-on-change
│   ├── <StrategyStrip kind="crypto-v4">  + <StaleChip>
│   ├── <StrategyStrip kind="stocks-wheel"> + <StaleChip>
│   └── <StrategyStrip kind="shark">     + <StaleChip>
├── <Integrations>
│   ├── <HermesPanel>     schedule table + recent runs + health + retrigger button
│   └── <ModelForgeSideLink>  link to mf-api endpoint list
├── <RegimeConfigEditor>  [collapsed by default; expand-to-edit] writes /api/v5/regime_config
├── <DecisionAudit>       [collapsed] explainability for every fill; B8 forensic surface
├── <MCPConsole>          [collapsed] 8+ tool manual invocation
└── <Intervene>           [position: fixed; bottom-right; spatially isolated]
    ├── <KillAllButton>   modal with no default focus on Confirm; type "KILL" to enable
    ├── <PauseButton>     per-strategy
    └── <FlattenButton>   per-position
```

Shared leaf component: `<StaleChip meta={card.meta}/>` rendered uniformly on every data-bearing card. Reads `_meta.{age_s, stale, market_open_now, snapshot_ts}` from the producer; renders `feed: 12s` (fresh), `feed: STALE 17h (NYSE closed)` (intentional freeze), or `feed: STALE 4m ⚠` (unintentional). Closes B14 by codification (per frontend-debate G4).

### 4.3 Design principles applied

| Principle | How we apply it |
|---|---|
| Dense, not sparse | 15-20 components in 1440-1920px viewport. Reviewed at Phase F post-dogfood; pull back to 10-12 if operator complains. |
| Sticky banner | ONLY `<TopBar>` is sticky; `<Monitor>` scrolls. `<Intervene>` is `position: fixed; bottom-right`. (Per frontend-debate G2.) |
| Color (PRIMARY: Wong blue/orange) | Status banners, alert severity ramps, sparkline strokes use Wong palette (`#0072B2` blue / `#E69F00` orange / `#D55E00` vermillion) as PRIMARY hue. P&L numerals keep operator-trained muted green/red as SECONDARY signal, always with sign + `▲`/`▼` arrow + monospace. Per frontend-debate G1. |
| Saturation ramps with urgency | `<AlertItem>` severity → opacity 0.5 (info) → 0.8 (warning) → 1.0 (danger). Border-left color also encodes. |
| Flash-on-change, NOT NumberRoll | Default for all cells: 200-400ms accent flash on diff. **NumberRoll only on three specific triggers**: (a) day-boundary rollover of cumulative day P&L, (b) capital crossing a $1k tier, (c) DD crossing pause or kill threshold. Routine ticks always flash. Per frontend-debate G3. |
| Data staleness first-class | Every card embeds `<StaleChip>`. Border tint shifts when `_meta.stale=true`. WS-down state also surfaces here. |
| Kill switch UX | `<KillAllButton>` bottom-right fixed dock, never adjacent to routine actions. Click → modal "This will pause crypto AND flatten all stocks positions". **No default focus on Confirm**; operator must type `KILL` into a textbox before Confirm enables. Single confirm (not 3-step), no auditory alarm. (Per frontend-debate G8 + NNGroup pattern.) |
| Dark theme default | Tailwind 4 + CSS vars; justified as preference continuity (`quanta.theme` localStorage + existing production). |

### 4.4 What we kill from existing surface

- The 24-card `/ops` grid layout (wall-of-cards anti-pattern). **Underlying data preserved**; surfaces re-housed.
- The 8-page `/v4` sidebar nav (anchor-scroll, not real routing).
- Continuous `NumberRoll` on every numeric tile (perceptual noise across 12 pairs).
- Bare ANSI status pills without composite context.
- The stocks/crypto regime confusion (per B12) — now two distinct `<RegimeChip>` instances bound to distinct producers.

## 5. Backend API redesign — `/api/v5/*`

### 5.1 Principles

- **One truth per metric.** Sharpe + max-DD + win-rate in `producers.metrics`; every consumer reads from there. Kills B2/B3.
- **Union producers at API layer.** `/api/v5/positions` unions `quanta_schema.fills` + `wheel-state/account_snapshot.json` + shark holdings. Kills B6/B9.
- **Per-side day-PnL.** `/api/v5/portfolio` returns `{combined, crypto, stocks}` each with `day_pnl_usd`. Kills B1.
- **Staleness as first-class field.** Every endpoint emits `_meta: {age_s, stale, snapshot_ts, market_open_now}`. UI consumes uniformly via `<StaleChip>`.
- **Envelope strategy** (per backend-debate G1):
  - **v5 endpoints**: return raw data directly + RFC 7807 problem-detail on error.
  - **Legacy `/api/ops/*` and `/api/v4/*`**: **retain the `{status, data, error, checked_at}` envelope shape verbatim** for ≥7 days post-cutover. Closes the `run_v4_shadow.py:603-604` + `monitor.sh:43` fail-closed silent-break window.
- **WebSocket** at `/api/v5/stream` pushes `{path, op, value, ts}` diffs. REST initial load + WS upgrade. Polling fallback at 10s with visible degradation indicator.

### 5.2 Endpoint inventory

| Endpoint | Purpose |
|---|---|
| `GET /api/v5/status` | Aggregate operator state (green/amber/red + detect-feed counts) |
| `GET /api/v5/portfolio` | Capital, equity, peak, DD, day-PnL per side, with `_meta` |
| `GET /api/v5/positions` | UNION of crypto fills + wheel state + shark holdings |
| `GET /api/v5/alerts` | Priority feed |
| `GET /api/v5/strategies/{kind}` | crypto-v4 / stocks-wheel / shark — includes per-side `regime` field |
| `GET /api/v5/metrics` | Single-truth Sharpe + max-DD + win rate |
| `GET /api/v5/hermes/schedule` | jobs.json with next-fire timestamps |
| `GET /api/v5/hermes/runs?limit=20` | Parsed `~/.hermes/cron/output/*.md` |
| `GET /api/v5/hermes/health` | Composite heartbeat |
| `GET /api/v5/regime_config` / `POST` | Regime detector params (replaces /ops card 19) |
| `GET /api/v5/decisions?limit=N` | Explainability + entry/exit reasoning (replaces /ops card 22) |
| `POST /api/v5/mcp/{tool}` | Manual MCP tool invocation (replaces /ops card 21b) |
| `WS /api/v5/stream` | Diff stream |
| `POST /api/v5/actions/kill` | Composite: pause crypto + flatten stocks |
| `POST /api/v5/actions/pause/{kind}` | Per-strategy pause |
| `POST /api/v5/actions/flatten/{symbol}` | Per-position flatten |
| `POST /api/v5/actions/hermes/retrigger/{job}` | Manual re-fire of a Hermes cron entry |

### 5.3 Legacy endpoint strategy (REVISED per backend-debate G2 + functional-debate G1)

**Old surfaces remain mounted in v1 returning live data.** Once §8 step 4 (hidden-caller migration) verifies clean per the audit in `inventory/hidden-callers-audit.md`:

- **GET routes** switch to `410 Gone` with `Location: /api/v5/<successor>` headers AND `Deprecation: true` header.
- **POST/PUT/PATCH/DELETE routes** (e.g. `/api/ops/pause`, `/resume`, `/rebalance`, `/regime_config`, `/risk_gates`, `/mcp/{tool_name}`) **proxy** to v5 equivalents (200 OK + `Deprecation: true` + `Link: <v5>; rel="successor-version"` per RFC 8594). **Never 410 a mutating route** — `unified_risk.py:802` POSTs `/api/ops/pause` to fire the circuit breaker; a 410 would silently break the safety brake (backend-debate Gap 2).
- Handlers stay in-tree **≥7 days AND ≥ one execution cycle of every Hermes cron entry** in `~/.hermes/cron/jobs.json` (covers `weekly_evolution_report` Sundays, `post_mortem_weekly`, etc.). Deletion is a separate v1.1 ticket.

### 5.4 Data preservation guarantees (EXTENDED per functional-debate G2)

The verifier role greps the workspace diff for ANY of these patterns and **fails the change**:

**Shell / system**:
- `rm -rf`, `rm --recursive`, `rm -fr`
- `find ... -delete`, `find ... -exec rm`
- `git reset --hard`, `git clean -fd`, `git clean -fX`, `git checkout -- <file>`
- `> /home/saijayanthai/Documents/.dgx-train/`, `> ~/Documents/.dgx-train/`
- `> ~/.hermes/`, `> /home/saijayanthai/.hermes/`
- `> user_data/data/`, `> stocks/memory/`, `> wheel-state/`
- `docker volume rm`, `docker volume prune`, `docker-compose down -v`
- `--force-recreate`, `--renew-anon-volumes`

**SQL**:
- `DROP TABLE`, `DROP SCHEMA`, `DROP INDEX`, `DROP DATABASE`, `DROP VIEW`
- `ALTER TABLE ... DROP COLUMN`, `ALTER TABLE ... DROP CONSTRAINT`
- `TRUNCATE TABLE`, `TRUNCATE` (any form)
- `DELETE FROM trade_journal`, `DELETE FROM regime_log`, `DELETE FROM meta_signal_log`, `DELETE FROM sentiment_log`, `DELETE FROM derivatives_features`, `DELETE FROM macro_features`, `DELETE FROM news_headlines`, `DELETE FROM whale_transactions`, `DELETE FROM mvrv_ratio`, `DELETE FROM exchange_netflow`, `DELETE FROM fear_greed_log`

**Python**:
- `shutil.rmtree`, `shutil.move` (when destination is `/dev/null` or `/tmp`)
- `pathlib.Path.unlink`, `Path.rmdir`
- `os.remove`, `os.unlink`, `os.rmdir`
- `alembic ... op.drop_table`, `op.drop_column`, `op.drop_index`, `op.drop_constraint`
- File open with mode `"w"` on paths under the preserved-roots list

**Preserved roots (any write/delete targeting these = fail)**:
- `~/Documents/.dgx-train/shark/memory/`
- `~/Documents/.dgx-train/shark/wheel-state/`
- `~/.hermes/cron/output/`
- `~/.hermes/cron/jobs.json` (modifications other than adding new cron entries)
- `user_data/data/*.json`
- `stocks/memory/TRADE-LOG.md`, `RESEARCH-LOG.md`, `WEEKLY-REVIEW.md`, `LESSONS-LEARNED.md`, `PROJECT-CONTEXT.md`
- `graphify-out/`

Exception: opening with `"a"` (append) on the above is allowed; opening with `"w"` (truncate) is NOT, unless explicitly listed in the impl team's task as `EXPECTED_WRITE_TRUNCATE: <path>` and operator-acknowledged.

## 6. Bug fixes baked into v1

| Bug | Fix | Owner | Dependencies |
|---|---|---|---|
| **B1** stocksMove poisons day-PnL | Producer emits `stocks.day_pnl_usd` from Alpaca `last_equity`; UI consumes directly | backend-engineer | — |
| **B2** shark stats wins=0 losses=0 | **backend-engineer reads `cron-shark-daily_summary.log` + journal rows FIRST**, then implements fix; if root cause is historical (pre-flock), ships additive idempotent backfill in same change (per functional-debate G4) | backend-engineer | — |
| **B3** Sharpe 10.58 vs −306 | `producers.metrics` single source; guards against zero-mean walk-forward windows; unit tests on zero-mean + single-trade windows | backend-engineer + database-engineer | — |
| **B4** v4 `CombinedHeader` understates | `openapi-typescript` codegen from `/openapi.json` — no manual type drift | frontend-engineer | gates B15 (per frontend-debate G9) |
| **B6/B9** Wheel positions invisible to /v4 | `/api/v5/positions` unions postgres + wheel JSON + shark | backend-engineer | — |
| **B7** Stale gate confuses operator | `_meta.market_open_now` propagates; `<StaleChip>` renders intent | backend-engineer + frontend-engineer | backend emits `_meta` first |
| **B8** BTC 34× single-name-cap | **v1 PREVENTS at entry**: `producers.risk.enforce_single_name_cap()` rejects fills where `stake > single_name_cap_pct × sleeve_equity` AT ENTRY in the live trading path. ALSO surfaces 24h historical violations in `<DetectFeed>`. (Per operator scope call on G6 + functional-debate G6.) | backend-engineer | — |
| **B10** hermes_mcp stuck activating | Composite health: heartbeat + last-fire-age; detect-feed alert if `activating` >30 min | backend-engineer | — |
| **B12** Per-stock regime label wrong | Two distinct `<RegimeChip kind=...>` instances bound to per-side `/api/v5/strategies/{kind}.regime` (per frontend-debate G5) | backend-engineer + frontend-engineer | — |
| **B14** override-health 28h stale unflagged | Codified `<StaleChip>` on every card (no card hand-rolls staleness) | frontend-engineer | — |
| **B15** v4 HeroScoreboard renders `—` | Moot (Overview replaced by new `<Monitor>`) + B4 codegen | frontend-engineer | B4 codegen lands first |

## 7. Hermes integration

Reads three Hermes sources:
1. **Schedule**: `~/.hermes/cron/jobs.json` parsed → table (job, cron, next, last-status).
2. **Run history**: `~/.hermes/cron/output/<job_id>/*.md` newest-first; parse header + tail snippet.
3. **Health**: heartbeat files (gateway, mcp, dashboard) → composite for top-bar status.

Operator actions:
- Re-fire a job (`POST /api/v5/actions/hermes/retrigger/{job}`)
- View full run output (modal, no page leave)
- Acknowledge alert (writes to `~/.hermes/cron/acks.json` — append-only file)

## 8. Migration plan (REVISED per backend-debate G3 + functional-debate G1)

1. **v5 backend producers + endpoints land** (additive — both old and new co-exist).
2. **`frontend-v5/` builds** (fresh dir, parallel to `frontend-v4/`).
3. **Dashboard container** mounts `frontend-v5/dist` at `/`; `/api/v5/*` routed.
4. **BLOCKING checklist**: hidden-caller audit (`inventory/hidden-callers-audit.md`) — every caller passes through (a) migrated to v5, (b) shimmed, or (c) signed-off "envelope-mode against deprecated route". No GET-route 410 fires until checklist 100% clean. **Mutating routes never 410** — they proxy.
5. **Switch the live dashboard image** to serve `/` from v5. Legacy `/ops` and `/v4/` routes remain mounted returning live envelope-shaped data. GET routes transition to 410 with Location header per the v1.1 ticket; **POST/PUT/PATCH/DELETE routes proxy to v5 with `Deprecation: true` header, never 410**.
6. **Verify containers healthy**: `quanta-core` cycle emitting; Hermes crons firing; postgres untouched.
7. **Operator end-to-end check**: scoreboard tiles populate, kill switch arms via type-to-confirm, Hermes panel shows next schedule.

**Rollback**: `git revert <impl-commits>` + `docker compose up -d --no-build dashboard` restores prior image. Postgres + bind-mounts unaffected by image swap.

## 9. v2 deferrals (explicitly named per functional-debate G3)

These surfaces from `/ops` and `/v4` are not in v1's new UI. Operator retains access via legacy `/ops` and `/v4/` mounts which remain live per §5.3:

- Card 13 Sentiment aggregate
- Card 13c Shark briefing 5-phase strip
- /v4 Monte Carlo viewer
- /v4 Backtest parity
- /v4 Weekly preview
- /v4 Adapter timeline + rollback
- /v4 Debate transcript live SSE
- ModelForge native panel (95 mf-api endpoints; v2 ticket = `mf-console`)

**Kept in v1 per operator scope call on G3**:
- Card 19 Regime config editor → `<RegimeConfigEditor>` (collapsed; expand to edit)
- Card 22 Decision audit / explainability → `<DecisionAudit>` (collapsed; B8 forensic surface)
- Card 21b MCP tool console → `<MCPConsole>` (collapsed)

## 10. Phase F timeline (REVISED to 180 min per functional-debate G5 + operator scope call)

| Block | Wall | Parallel? |
|---|---|---|
| Scout: full caller audit + producer-side bug-root-cause reads (B2 log, B3 math, B8 risk-path) | 25 min | serial — gates everything |
| Backend builders (3 parallel): B3 math + producer | B6 union + B1 stocks day-pnl | B8 entry-cap + hermes endpoints | 60 min | parallel |
| Frontend builders (2 parallel): designer scaffolds components + styling tokens | engineer wires data + WebSocket | 60 min | parallel; starts after codegen lands (~30 min in) |
| Hidden-caller migration + 4 test rewrites | 30 min | parallel with frontend tail |
| Verifier: data-preservation grep (§5.4 patterns) + smoke tests + screenshot diff | 15 min | serial |
| **Total Phase F** | **180 min** | |
| **Buffer** | **25 min** | |

**Pre-authorized descope valve**: if at T+150 min the WebSocket stream isn't ready, ship `frontend-v5/` with 10s TanStack polling and visible `polling 10s` chip. WebSocket becomes a v1.1 ticket.

## 11. Resolved scope decisions

1. **B8 root-cause-in-v1** ✓ Entry-time risk-cap enforcement ships in v1 (operator-confirmed G6).
2. **KILL UX** ✓ Top-right fixed dock, single-confirm modal with type-to-confirm "KILL" textbox, no default keyboard focus on Confirm.
3. **Color palette** ✓ Wong blue/orange PRIMARY for status/alerts/sparklines; muted green/red SECONDARY only on P&L numerals with sign + arrow. (Per frontend-debate G1 + market-research §3.)
4. **Cutover strategy** ✓ Old routes stay live ≥7 days behind new UI; deletion is v1.1 (operator-confirmed G1).
5. **Phase F budget** ✓ 180 min + 25 buffer, keep WebSocket (operator-confirmed G5).
6. **v1 surfaces** ✓ regime_config + decision_audit + MCP console stay; others defer (operator-confirmed G3).
