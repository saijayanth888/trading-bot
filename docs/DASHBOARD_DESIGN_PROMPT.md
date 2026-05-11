# Trading-bot dashboard — design refresh prompt

**Audience:** Claude Code (or another file-aware design agent) — paste the
PROMPT block below verbatim. It contains exact absolute file paths so the
agent can `Read` / `Edit` / `Bash` against the live codebase, plus every
API endpoint, every JS function, every CSS token, and the build/deploy
mechanics (rebuilding is required — files are baked into the image, not
bind-mounted at runtime).

---

## PROMPT BEGINS

You are leading a **production-grade redesign** of a self-hosted
algorithmic trading dashboard. The codebase below is a **working
prototype** — every data path is wired and the bot is paper-trading
live, but the UI was built feature-by-feature without a unifying design
strategy. Your job is to propose and implement a **radical,
opinionated, production-ready redesign**: information architecture,
component library, interaction patterns, and visuals. This is **not**
a paint-job. You may rework layout, restructure cards, introduce new
component primitives, change groupings, kill duplicate functionality,
and propose new UX flows. Behavior changes are welcome when they make
the operator faster.

**The operator** is a single technical user monitoring a bot that
paper-trades crypto on Coinbase ($19 k starting equity) and options on
Alpaca (SOFI wheel, $100 k). The console is the **only** view into a
live trading system; clarity, speed, and trustworthiness beat novelty.

**Reference tone**: institutional, monospaced, dense, fast — closer to
Bloomberg / dYdX / Linear / Vercel-internal-tools than a consumer SaaS.
Reference aesthetic: dYdX trading interface × Geist design language.

**What you should deliver, conceptually**:
1. A short **design strategy** — what you're solving for, what you're
   killing, what you're keeping, the one mental model the operator will
   carry away from the redesigned console.
2. A new **component primitive set** (cards, metric tiles, status
   rows, action buttons, gate matrices, etc.) — define them once, apply
   everywhere.
3. A new **information architecture** for `/ops` — what belongs
   above-the-fold, what becomes secondary, what gets absorbed into other
   cards or removed.
4. **The actual code** — patches to CSS / templates / JS that ship the
   redesign, not mockups. Treat the listed file paths as the work surface.

## 0 · Where everything lives — exact paths

**All paths in this prompt are relative to the repository root** (the
directory containing this `docs/` folder, `user_data/`, `stocks/`,
`docker-compose.yml`, etc.). Claude Code's working directory should
already be the repo root; just `Read user_data/dashboard/app.py`
directly. No need for absolute paths.

### Dashboard source (all under `user_data/dashboard/`)

| File | Purpose |
|---|---|
| `user_data/dashboard/app.py` | FastAPI app entrypoint — / and /api/* routes for the **Charts** page |
| `user_data/dashboard/ops_routes.py` | All `/api/ops/*` routes for the **/ops** page (35+ endpoints) |
| `user_data/dashboard/data_sources.py` | Freqtrade API client + trade-marker fetcher + state aggregator |
| `user_data/dashboard/indicators.py` | TA computations (BB, EMA, VWAP, RSI, MACD) |
| `user_data/dashboard/ops_db.py` | Postgres direct queries (trade_journal etc.) |
| `user_data/dashboard/ops_probes.py` | Service-health probes (freqtrade, ollama, hermes-mcp, etc.) |
| `user_data/dashboard/mcp_local.py` | Local MCP tool registry — 40+ tools exposed via /api/ops/mcp/* |
| `user_data/dashboard/templates/index.html` | **Charts page** (282 lines) — /  |
| `user_data/dashboard/templates/ops.html` | **Ops console** (1183 lines) — /ops |
| `user_data/dashboard/static/css/app.css` | **The only stylesheet** (~480 LOC) |
| `user_data/dashboard/static/js/app.js` | Charts page logic (TradingView candles + sidebar) |
| `user_data/dashboard/static/js/ops.js` | Ops page logic (15 cards, see §6) |
| `user_data/dashboard/static/js/effects.js` | Shared visual helpers |
| `user_data/dashboard/Dockerfile` | Container build (see §1 — *source is baked in*) |
| `user_data/dashboard/requirements.txt` | Python deps |

### Files outside the dashboard module the design touches

| File | Purpose |
|---|---|
| `docker-compose.yml` | `dashboard:` service definition (port 8081, build context `./user_data/dashboard`) |
| `user_data/config.json` | Live config — the `regime_gating` editor on /ops writes here |
| `user_data/data/config-backup-*.json` | Auto-snapshots on every config write (you don't touch these) |
| `scripts/monitor.sh` | Single-pane terminal mirror of the dashboard — useful reference for "what data exists" |
| `docs/RECOVERY.md` | 10-scenario operator playbook the dashboard links to (when relevant) |
| `README.md` §3 | Deployment topology mermaid — ports, bind mounts |
| `README.md` §10.1 | Hermes cron schedule (drives several Ops cards) |

## 1 · Build + deploy mechanics — CRITICAL

The dashboard runs as a Docker container. **`Dockerfile` COPYs the source
into `/app/dashboard` at build time**, so editing files on the host does
NOT take effect until you rebuild. The `user_data` directory is bind-
mounted at `/freqtrade/user_data` (RW) for trade-journal access, but **the
template + CSS + JS the running container serves are the baked-in copies
under `/app/dashboard/`**, not the host files.

To ship CSS / JS / template changes:

```bash
# Run from the repo root (your current working dir).
docker compose up -d --build dashboard      # rebuild + recreate
# CSS / JS / template changes all use the same command — no hot reload.
```

**Cache-busting**: the templates include `?v=<timestamp>` on static asset
URLs (search `ops.html` for `v=20260510-11` and bump the value). After a
visual change, edit that version stamp in BOTH templates so the operator's
browser refetches.

Verify the change is live:

```bash
docker exec dashboard grep -c "<thing-you-changed>" /app/dashboard/static/css/app.css
```

If the count is 0, the rebuild didn't pick up your edit. Run the rebuild
again and re-verify before reporting "done".

## 2 · Routes (only two pages)

| Path | Template | Backend | JS module |
|---|---|---|---|
| `/` | `templates/index.html` | `app.py:88` | `static/js/app.js` |
| `/ops` | `templates/ops.html` | `ops_routes.py:74` | `static/js/ops.js` |

## 3 · API endpoints used by the frontend

### Charts page (`/`) — defined in `app.py`

| Path | Line | What it returns |
|---|---|---|
| `GET /api/pairs` | 109 | Whitelisted pair list + default timeframe |
| `GET /api/mode` | 114 | `{mode, state, dry_run}` for the topbar mode pill |
| `GET /api/candles/{base}/{quote}?timeframe=` | 151 | OHLCV + indicator overlays + regime segments |
| `GET /api/trades/{base}/{quote}` | 206 | Entry/exit markers (`time/position/color/shape/text`) |
| `GET /api/state` | 275 | Right-sidebar payload (regime, TFT, sentiment, on-chain, positions, recent_trades, champion) |

### Ops page (`/ops`) — defined in `ops_routes.py`

| Path | Line | Card # it feeds |
|---|---|---|
| `GET /api/ops/services` | 90 | #01 |
| `GET /api/ops/training` | 113 | #02 |
| `GET /api/ops/regime` | 138 | Hero strip (crypto regime) |
| `GET /api/ops/sentiment` | 187 | (currently not on a visible card; data available) |
| `GET /api/ops/mcp` | 250 | #03 |
| `GET /api/ops/trades_risk` | 270 | #05 |
| `POST /api/ops/pause` | 341 | #07 Quick Actions |
| `GET /api/ops/sparklines` | 361 | #04 |
| `GET /api/ops/regime_config` | 482 | Regime parameters editor |
| `POST /api/ops/regime_config` | 503 | Atomic-writes `config.json` |
| `POST /api/ops/resume` | 623 | #07 Quick Actions |
| `GET /api/ops/config` | 680 | Config overview viewer |
| `GET /api/ops/readiness` | 846 | Validation gate status |
| `GET /api/ops/rebalance` | 978 | #07 — preview pair_weights change |
| `POST /api/ops/rebalance` | 997 | #07 — atomic-write pair_weights |
| `GET /api/ops/tools` | 1048 | #10 MCP tool console — list |
| `POST /api/ops/mcp/{tool_name}` | 1054 | #10 — execute a tool |
| `GET /api/ops/explainability/{base}/{quote}` | 1084 | #09 Decision audit drill-down |
| `GET /api/ops/timeline/{base}/{quote}` | 1184 | Trade timeline view |
| `GET /api/ops/slack_preview` | 1259 | #08 |
| `GET /api/ops/stocks` | 1387 | #11 |
| `GET /api/ops/stock_candles/{symbol}` | 1497 | Stocks chart fetch |
| `GET /api/ops/gates` | 1536 | #06 — per-pair gate matrix |
| `GET /api/ops/market_hours` | 1890 | "NYSE closed/open" pill |
| `GET /api/ops/live_trades` | 1975 | Live-trades horizontal strip |
| `GET /api/ops/ollama_health` | 2050 | #14 (top half) |
| `GET /api/ops/circuit_breakers` | 2070 | #14 (bottom half) |
| `GET /api/ops/llm_stats` | 2104 | #13 |
| `GET /api/ops/combined_portfolio` | 2222 | #12 |
| `GET /api/ops/stocks_ml` | 2256 | #15 |
| `GET /api/ops/stock_regime` | 2321 | Hero strip (stocks regime) |

All envelopes: `{status: "ok"|"down"|"degraded", data: {...}, error: null, checked_at: ISO}`.

## 4 · JavaScript refresh functions → cards (in `static/js/ops.js`)

| Function | Line | Drives |
|---|---|---|
| `refreshServices` | 150 | #01 Services |
| `refreshTraining` | 187 | #02 Training |
| `refreshMcp` | 266 | #03 MCP wire |
| `refreshSparklines` | 1060 | #04 Pair telemetry |
| `refreshTrades` | 291 | #05 Trades & risk |
| `refreshGates` | 774 | #06 Entry gates |
| `refreshLiveTrades` | 800 | Live-trades strip |
| `refreshRegime` | 95 | Hero (crypto regime) |
| `refreshStockRegime` | 857 | Hero (stocks regime) |
| `refreshSentiment` | 131 | (data available — unused currently) |
| `refreshStocks` | 895 | #11 Stocks venue |
| `refreshCombined` | 527 | #12 Combined portfolio |
| `refreshLLMStats` | 591 | #13 LLM inference $ saved |
| `refreshCircuitBreakers` | 429 | #14 (circuit breakers half) |
| `refreshStocksML` | 352 | #15 Stocks ML |
| `drawSparkline` | 1026 | per-pair canvas sparkline (helper) |
| `drawRegimeBar` | 1122 | regime ribbon (helper) |

Master refresh controller is in `ops.js` near the top — single
`setInterval`, dropdown values `5s / 10s / 30s / 1m / Off`, persisted in
`localStorage` (key: `dashboard_refresh_interval`). Force-refresh button
exists. **Do not add per-card refresh intervals** — keep the master.

## 5 · Design tokens (current — refine, don't replace wholesale)

Defined at `static/css/app.css:9-46`:

```css
:root {
  /* surfaces */
  --bg-page:        #08080c;
  --bg-card:        #111114;
  --bg-card-hover:  #16161b;
  --bg-inset:       #1c1c22;
  --bg-overlay:     #22222b;

  /* borders */
  --border-subtle:  rgba(255, 255, 255, 0.06);
  --border-default: rgba(255, 255, 255, 0.10);
  --border-strong:  rgba(255, 255, 255, 0.16);

  /* text */
  --text-primary:   #ededed;
  --text-secondary: #a1a1a6;
  --text-muted:     #6e6e78;
  --text-disabled:  #3f3f46;

  /* status */
  --up:        #3fb950;          --up-bg:     rgba(63, 185, 80, 0.10);  --up-border: rgba(63, 185, 80, 0.35);
  --down:      #f85149;          --down-bg:   rgba(248, 81, 73, 0.10);  --down-border:rgba(248, 81, 73, 0.35);
  --warning:   #f5a623;          --warning-bg:rgba(245, 166, 35, 0.10); --warning-border:rgba(245, 166, 35, 0.35);
  --accent:    #7c5cff;          --accent-bg: rgba(124, 92, 255, 0.12); --accent-border: rgba(124, 92, 255, 0.5);

  /* fonts */
  --sans: 'Geist', 'Inter', system-ui, -apple-system, sans-serif;
  --mono: 'Geist Mono', 'JetBrains Mono', 'IBM Plex Mono', 'Menlo', monospace;
}
```

Typography baseline (currently hand-picked — please replace with a real scale):
- body: 14px / 1.5 sans
- card heads: 15px @ 600 sans
- pills: 11px mono @ 500
- form inputs / buttons: 13px @ 500 sans
- hero-headline: see `templates/ops.html:100` (`hero-headline` class)

Status semantics — **do not change colors here, only refine**:
- Green `--up` = profit / pass / healthy / ELIGIBLE
- Red `--down` = loss / fail / BLOCKED / kill-switch / error
- Amber `--warning` = paper mode (operator must never forget) / regime warning / latency creeping
- Purple `--accent` = high-volatility regime / brand
- Muted gray = missing data, `—` placeholders

## 6 · /ops cards inventory — title, line in template, data source

Find the cards in `templates/ops.html` by searching `card-head`. The
`data-num` attribute on each `<h3>` is the human card number used in
deep-links and operator instructions — **preserve every number**.

| # | Title (verbatim from template) | data-num | Template line | JS refresh fn | API |
|---|---|---|---|---|---|
| HERO 1 | Crypto regime — BTC HMM | — | ~740 | refreshRegime | /api/ops/regime |
| HERO 2 | Stocks regime — SPY 50/200 | — | ~746 | refreshStockRegime | /api/ops/stock_regime |
| HERO 3 | Bot status | — | ~750 | (inline in template) | /api/mode |
| STRIP | Live trades · all venues | — | (top of body) | refreshLiveTrades | /api/ops/live_trades |
| 1 | Services | 01 | 772 | refreshServices | /api/ops/services |
| 2 | Training | 02 | 781 | refreshTraining | /api/ops/training |
| 3 | MCP wire | 03 | 790 | refreshMcp | /api/ops/mcp |
| 4 | Pair telemetry · 5m closes · trailing 24h | 04 | 799 | refreshSparklines | /api/ops/sparklines |
| 5 | Trades & risk | 05 | 810 | refreshTrades | /api/ops/trades_risk |
| 6 | Entry gates · why isn't anything trading? | 06 | 819 | refreshGates | /api/ops/gates |
| 7 | Regime parameters (editor form) | **06** ⚠ DUP | 879 | (form submit) | /api/ops/regime_config |
| 8 | Quick actions | 07 | 888 | (button handlers in `effects.js`) | /api/ops/{pause,resume,rebalance} |
| 9 | Slack preview · next daily report | 08 | 927 | (preview fetch on load) | /api/ops/slack_preview |
| 10 | Decision audit | 09 | 936 | (lazy) | /api/ops/explainability/* |
| 11 | MCP tool console | 10 | 987 | refreshMcp + tool form | /api/ops/tools + /api/ops/mcp/{tool} |
| 12 | Stocks · shark + wheel · Alpaca paper | 11 | 996 | refreshStocks | /api/ops/stocks + /api/ops/market_hours |
| 13 | Combined portfolio · crypto + stocks | 12 | 1087 | refreshCombined | /api/ops/combined_portfolio |
| 14 | LLM inference · cost saved vs Anthropic | 13 | 1107 | refreshLLMStats | /api/ops/llm_stats |
| 15 | LLM provider health · Ollama primary · Anthropic fallback | 14 | 1116 | refreshCircuitBreakers + inline | /api/ops/ollama_health + /api/ops/circuit_breakers |
| 16 | Stocks ML · TFT predictor — ALPHA | 15 | 1128 | refreshStocksML | /api/ops/stocks_ml |

**Bug to fix in the redesign**: cards #06 (Entry gates) and #06 (Regime
parameters) share a `data-num`. Renumber the Regime-parameters card —
suggested `16` or shift everything starting from the second `06`. Update
any documentation pointing at the duplicate numbers.

## 7 · /charts page topbar + sidebar

Topbar (search `templates/index.html` line 22 onwards):

| Element | Line | Class / id |
|---|---|---|
| `[Crypto] [Stocks]` venue tabs | 24-25 | `.venue-tab` |
| Pair `<select>` | 27 | `#pair-select` |
| Timeframe `<select>` | 35 | `#timeframe-select` (1m/5m/15m/1h/4h/1d) |
| Auto-refresh `<select>` | 44 | `#refresh-interval-select` `.refresh-select` |
| Force-refresh button | 52 | `#refresh-now-btn` `.mode-pill` |
| Mode pill | (in body) | `.mode-pill` |
| OPS CONSOLE ↗ link | 22 | `.mode-pill` |

Main candle area: `<div id="main">` ~line 159, plus sub-charts
`<div id="rsi">` and `<div id="macd">`. TradingView Lightweight Charts
created in `app.js:134-162` (3 chart instances on one page). Markers
applied at `app.js:382` via `candleSeries.setMarkers(tradeData.markers)`.

Right sidebar cards (`templates/index.html:186-264`):

1. `#stock-regime-card` — SPY regime (visible when venue=stocks)
2. Regime + confidence + duration
3. TFT prob/conf + meta_signal
4. On-chain (netflow_z / MVRV / whale_1h)
5. Sentiment (6-source aggregate)
6. Open positions for the pair
7. Recent trades (last 5)
8. Champion genome one-line

## 8 · Information-density principles to preserve

- One page = one purpose. `/charts` is "drill into a pair"; `/ops` is
  "whole system at a glance".
- **Hero strip above-the-fold on 1440×900** — never let it scroll off.
- **Mono for numbers** — every price, PnL, latency, percentage.
- **Status before label** — colored dot/pill on the left, text right;
  the eye scans status first.
- **Δ + Action** pattern (the alert format used in
  `user_data/modules/slack_alerts.py:_blocks`) — every state-bearing
  card should answer "what changed?" and "what to do?" in one line.
  Currently inconsistent across cards; harmonise.
- **Auto-refresh is global** — single setInterval, do not add per-card
  intervals.

## 9 · Anti-goals (operator preferences, do NOT violate)

- ❌ Drop-shadows, neumorphism, glassmorphism, frosted glass
- ❌ Light theme (operator wants permanent dark — no toggle)
- ❌ Animations longer than 100 ms
- ❌ Rounded-pill buttons (max 6 px radius, prefer 4 px)
- ❌ Serif fonts, italic body, gradient text
- ❌ Illustration-heavy UI
- ❌ Modal-heavy flows (prefer in-place editing — e.g. the regime-config
  form is inline and should stay inline)
- ❌ Bottom-screen toasts that linger (top-right corner, dismissable)
- ❌ Adding React/Vue/Svelte — keep plain ES modules
- ❌ Adding a CSS preprocessor — keep raw CSS
- ❌ Changing the auto-refresh cadence model

## 10 · What's working — preserve

- The monospaced numbers × Geist sans-serif headings mix
- The 3-slot hero strip (Crypto / Stocks / Bot status)
- `#08080c` page bg × `#111114` card bg (the depth ratio is correct)
- Green/red profit/loss convention
- `data-num` numbering for deep-links (after fixing the dup)
- Auto-refresh dropdown placement (top-right of each page)
- The venue-tab segmented control replacing the old optgroup dropdown
- Live-trades horizontal strip at the top of /ops

## 11 · Deliverables — production-grade, in this order

### A · Strategy doc (write this first, before any code)

Create `docs/DASHBOARD_REDESIGN.md` answering, in <= 600 words:

1. **What is the operator's mental model after the redesign?** One
   sentence. (e.g. *"At a glance: is the bot trading, is it safe, what
   changed since I last looked?"*).
2. **Above-the-fold hierarchy** — what the operator sees in the first
   second on 1440×900. Pick at most 5 things. Justify each.
3. **What gets killed / merged.** Today there are 15 cards + a hero
   strip + a live-trades strip. Several overlap (e.g. #14 LLM health
   and #13 LLM cost saved; #12 Combined portfolio and #05 Trades & risk).
   Propose merges. Be opinionated — if a card serves no critical use,
   say "delete" and we'll delete.
4. **Component primitives** — name them, define them, show one example
   each. Suggestions: `MetricTile` (single big number + Δ + sparkline),
   `StatusRow` (status-before-label pattern), `GateBadge` (PASS / BLOCK
   / N/A pill), `ActionButton` (with severity variants), `DataTable`
   (the gate matrix's eventual home), `Toast` (top-right, dismissable).
5. **Interaction patterns** — destructive-action confirmation flow,
   in-place edit pattern (regime config), drill-down pattern (entry
   gates summary → full matrix), refresh state indication.

### B · Design system — the new `static/css/app.css`

A rewritten stylesheet, not a patch:

- Explicit typography scale: `--text-2xs / xs / sm / base / lg / xl / 2xl / 3xl`
- Explicit spacing scale: `--space-1 ... --space-12` (4 px grid)
- Explicit radius scale: `--radius-sm / base / lg`, max 6 px
- Refined surface palette — propose **two** options:
  - **"Control room"** — even darker, more contrast (for the operator's
    primary monitor)
  - **"Trading floor"** — slightly warmer, easier on eyes for long sessions
  Default to control-room; trading-floor selectable via a data-attribute.
- A **state token layer** above the raw colors — `--state-success`,
  `--state-danger`, `--state-warning`, `--state-info`, `--state-neutral`
  — so cards don't reference raw `--up` / `--down` directly.
- Component classes for every primitive in (A.4). One source of truth.

### C · Redesigned `/ops` — `templates/ops.html` + `static/js/ops.js`

- New information architecture matching the strategy doc.
- Hero strip + a chosen above-the-fold set; everything else below or
  collapsed into expandable sections.
- Renumber cards so every `data-num` is unique (currently `06` is
  duplicated — Entry gates AND Regime parameters share it).
- The Quick Actions card becomes a real **control panel**: pause /
  resume / reload / kill switch / trigger evolution, with the kill
  switch visually severed from the rest and **two-step confirmation**
  on every destructive verb.
- Entry Gates card surfaces the **single most-blocking gate per pair**
  at glance + click-to-expand for the full 10-gate matrix.
- Live-trades strip becomes the operator's primary scan line — make it
  feel inevitable, like the top of a Bloomberg.

### D · Redesigned `/charts` — `templates/index.html` + `static/js/app.js`

- The sidebar today is 8 stacked cards. Consolidate into 3-4 modules
  with clearer purpose (e.g. *"Model view"*, *"Market context"*,
  *"Trade lineage"*).
- The TradingView chart must remain the dominant element — do not
  let the sidebar grow.
- Add entry/exit markers prominence — the operator should see at a
  glance "I bought here, exited here, why".
- Topbar matches `/ops` exactly — venue tabs, refresh dropdown, mode
  pill, OPS↗ — same positions, same hotkeys (if any).

### E · Cache-bust + ship

- Bump every `?v=…` static-asset version stamp in both templates.
- Ship via the §12 build command. Verify in-container before reporting
  done (the §12 grep step is mandatory — host edits don't auto-deploy).

### F · A short "what I didn't do" section in `DASHBOARD_REDESIGN.md`

Out-of-scope items you considered and explicitly punted (e.g. a real
component library extraction, light-theme support, mobile breakpoints).
Lets the operator decide whether to chase them next.

## 12 · How to ship

```bash
# All commands run from the repo root (Claude Code's CWD).

# 1) make your edits to user_data/dashboard/static/css/app.css and templates
# 2) rebuild + recreate
docker compose up -d --build dashboard
# 3) verify the change actually landed inside the container
docker exec dashboard grep -c "<unique-string-from-your-change>" /app/dashboard/static/css/app.css
# (must be > 0 — if 0, the build didn't pick up your edit)
# 4) check the live UI
curl -sf http://localhost:8081/ops | head -20
```

Keep all behavior intact. This is a **visual refresh, not a UX overhaul**.
If a change to behavior is required to enable a design move, flag it as
a separate proposal — don't ship it silently.

## PROMPT ENDS
