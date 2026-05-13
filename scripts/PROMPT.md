# QUANTA · NEXT — work-on-existing-code prompt for Claude Code

> **Mode: incremental modification of an existing repo. NOT greenfield.**

There is already a working single-page operator console for a multi-asset algorithmic trading bot called **Quanta** at `quanta-next/` (this directory). The bot paper-trades 12 USD crypto pairs through Freqtrade and runs a cash-secured-put wheel on Alpaca paper. The console lets an operator answer three questions in under 5 seconds: **Where is my money? Why isn't anything trading? What is the LLM stack doing right now?**

Your job is to **read the existing code first**, then make the changes the user asks for **without breaking the running design, the data contract, or the helper API**. The wireframes and component specs below describe the design that is **currently shipped** — they are documentation, not a build brief.

---

## A · READ THIS FIRST — existing repo

```
quanta-next/
├── index.html        ~ 90 lines     shell, top-bar, left-rail, footer, script tags
├── styles.css        ~ 25 KB        design system + every component class
├── data.js           ~ 24 KB        window.QUANTA — all live values
├── app.js            ~ 50 KB        helpers + 4 page renderers + routing
└── PROMPT.md         this file
```

### How to run / refresh

The app is already served by Python's built-in http server on port 8090 (background process).
Open `http://127.0.0.1:8090/index.html?v=N#/ops` — bump the `?v=` query each time you change CSS/JS so browsers fetch the new build (current is `v=5`).

### Boot order (do not reorder)

```
index.html  loads  styles.css?v=N  →  data.js?v=N  →  app.js?v=N
```

`data.js` must run before `app.js` because `app.js` reads `window.QUANTA` at IIFE init time.

### Top-level shape of `app.js`

The whole file is one IIFE: `(() => { … })();`.
Inside, in this order:

```
const D = window.QUANTA;            // alias used everywhere
const main = document.getElementById("main");
const SVG_NS = "http://www.w3.org/2000/svg";

// utilities
$, $$, el, s, setText, clear, fmtMoney, fmtPct, fmtPx, stat, cardHead, seededRand

// chart factories
sparkline, candleChart, donut, rsiChart, macdChart, makeTable

// agent flow
agentIcon, AGENT_META, agentCard, renderAgentPipeline,
renderLlmActivity, renderSentiment, renderLlmProvider

// page renderers
renderOps, renderPair, renderDocs, renderDocBody

// routing + globals
setActive, route, hashchange listener, tickClock, theme toggle, Cmd-F handler
```

---

## B · COMPATIBILITY CONTRACT — DO NOT BREAK

These are the **public surfaces** of this codebase. Any code, any new card, any refactor must honor them.

### B.1 · Frozen `window.QUANTA` schema

The top-level keys below are **read by existing renderers**. Treat them as a published API. You may **add** new top-level keys; you must **not rename, remove, or change the type/shape** of these:

```
meta                    object  { capturedAt, operator, source, version }
scoreboard              object  capital · livePnL · realizedToday · unrealized · drawdown · peak · open{total,crypto,stocks} · closedToday · dayPct · pauseThreshold · killThreshold
combined                object  equity · dayPnL · dayPct · crypto{equity,deltaPct} · stocks{equity,deltaPct} · breaker
crypto                  object  regime · confidence · holdTime · pairs[{sym,px,delta}]
stocks                  object  regime · confidence · tickers[{sym,px,delta}]
stocksTicker            array   [{sym,side,ref,pnl,venue}]
botState                object  state · engine · mode · champion · strategy
liveResearch            object  aggregate · headline · headlines · fearGreed{score,label}
pair                    object  selected · timeframe · px · deltaPct · dayUSD · regime · confidence · gate · bars · options · timeframes · model · context · champion · pnlHistory · net14d · recentTrades
gates                   object  blocked · total · mostCommon · columns · rows[{pair,regime,states,passes,blocker}]
openPositions           array   [{sym,venue,side,qty,entry,mark,upnl,note}]
wheel                   object  portfolio · cash · buyingPower · age · open · premium · collateral · contracts · shark
agentFlow               object  active · total · calls24h · updated · stages[{role,model,timeAgo,alert,calls,avg,p95,lastSnippet}] · arrowLatencies
llmActivity             object  calls · tokens · avgLat · p95Lat · ollamaPct · successPct · diskKB · updated · agentFilters · rows[{time,agent,model,lat,tokens,success}]
researchStream          array   [{when,source,title,detail}]
services                array   [{name,probe,endpoint}]
breakers                object  portfolio[] · service[] both with {status,name,meta}
readiness               object  mode · trades · buckets · startEquity · gates[{name,current,threshold,status,direction,unit?}]
ept                     object  id · fitness · sharpe · maxDD · profitFactor · nTrades · stop · tp · features
decisions               array   [{ts,dir,metaSignal,conf,regime,tftUp}]
trades24h               object  dayPnL · dayPct · dd30d · open · tape[]
sentiment               object  label · net · deep · fast · fearGreed · agreement · headlines · age
llmProviders            object  callsCrypto24h · saved24hUSD · primary{name,state,latency} · breakers[]
configEditor            object  entryDelta{} · exitDelta{} · scalars{}
slack                   object  head · lines[]
mcpTools                array   string names; entries starting with "❗" are state-mutating
training                object  currentPair · epoch · valSharpe · loss · avgEpoch · eta · dictReady · eptGen · subRows[]
sharkTFT                object  bestValAcc · bestEpoch · nTrain · nTickers · device · age · nextCron
sharkBriefing           object  date · regime · macro · phases[] · note
docs                    array   [{id,num,title,body}]
```

**Rules:**

1. Adding new keys at any level is fine.
2. Adding new entries to any array is fine.
3. **Renaming, removing, retyping, or reordering required fields is not fine.** If you really need to change a shape, add the new shape under a new key and migrate readers in the same change.
4. All string fields are display-ready (already formatted). Don't reformat in JS unless you also update every consumer.
5. The `agentFlow.stages` array order is meaningful — `[regime_tagger, bull_debater, bear_debater, arbiter, reflector]`. Don't reorder.

### B.2 · Frozen helper API in `app.js`

These functions are used by many call sites. Preserve their signatures and behavior:

| Helper | Signature | Notes |
| - | - | - |
| `el(tag, attrs?, ...children)` | element factory | `attrs.class` sets `className`; `attrs.text` sets `textContent`; `on*` keys add listeners; falsy children skipped |
| `s(tag, attrs?, ...children)` | SVG element factory | uses `createElementNS` |
| `setText(node, text)` | clear node then append text | |
| `clear(node)` | remove all children | |
| `fmtMoney(v, opts?)` | `−$1,234.56` style | `opts.decimals` |
| `fmtPct(v, opts?)` | `+1.23%` / `−1.23%` | `opts.decimals` |
| `fmtPx(px)` | auto-precision price | $80,383 / $94.48 / $0.2704 |
| `stat(label, value, opts?)` | returns `.stat` block | `opts.tone` ∈ `pos / neg / warn / acc`; `opts.sub` |
| `cardHead(tag, title, trailing?)` | returns `.card-head` | tag = decimal index string |
| `makeTable(headers, rows, columnAlign)` | returns generic `.table` | rows are arrays of strings or Nodes |
| `sparkline(symbol, deltaPct, w?, h?)` | deterministic seeded SVG | seed = symbol — stays stable across renders |
| `seededRand(seed)` | returns `() => 0..1` | LCG; do not replace with `Math.random` |
| `agentIcon(role)` | returns SVG node | only 5 roles known; pass the canonical role string |
| `agentCard(stage)` | full agent card | reads `AGENT_META[stage.role]` for color/stance |

### B.3 · Frozen CSS tokens in `styles.css`

These CSS custom properties are referenced from JS (inline `style="background: var(--success)"`) and from many CSS rules. **Don't rename them.** You may add new tokens, you may tweak values, but don't drop any of:

```
--bg-0 --bg-1 --bg-2 --bg-3 --surface --surface-2
--stroke-1 --stroke-2 --stroke-3
--text-1 --text-2 --text-3 --text-4
--accent --accent-2 --accent-soft
--success --success-soft --danger --danger-soft
--warn --warn-soft --info --info-soft
--grid --radius-sm --radius --radius-lg --mono --sans
```

Light theme is activated by `<html data-theme="light">` — keep that switch working.

### B.4 · Frozen routing

- `#/ops` (default) → `renderOps()`
- `#/pair` → `renderPair()`
- `#/docs` (optionally `#/docs#section-id`) → `renderDocs()` + scrollIntoView the anchor

Left-rail links with `data-route="/ops|/pair|/docs"` are picked up by `setActive()` to toggle `.active`. Don't change those attributes.

### B.5 · Frozen security rules

These are enforced by hooks and by reviewers:

1. **No `.innerHTML` assignments anywhere.** Build DOM via `el()` and `s()`.
2. **No `RegExp.prototype.exec` calls.** Use `String.split`, `String.indexOf`, or a manual character walk.
3. **No inline `on*=""` handlers in HTML strings.** Attach with `addEventListener` or via `el({ onclick: fn })`.
4. **No HTML strings passed to `.outerHTML` either.** Same reason as `.innerHTML`.
5. The doc-body parser is a hand-written tag walker. If you extend the allowed tags, extend the walker — don't reach for regex.

### B.6 · File-touch policy

| File | Touch? | Notes |
| - | - | - |
| `index.html` | rarely | only when adding/removing top-bar items, rail items, or cache-busting `?v=`. Don't add inline scripts. |
| `styles.css` | freely (additive) | add new class blocks at the bottom, near the relevant section. Don't rename existing classes used by `app.js`. |
| `data.js` | freely (additive) | add new keys / entries. **Never delete or rename** the keys in §B.1. |
| `app.js` | freely (additive) | add new render functions next to similar existing ones; call them from `renderOps/renderPair/renderDocs`. Don't alter the helper signatures. |
| `PROMPT.md` | only when the contract changes | this file is documentation. |

---

## C · SAFE EXTENSION PATTERNS

### C.1 · Add a new card on `/ops`

1. Pick a decimal index that doesn't collide (look at §3.1 for the current map).
2. Define your data on `window.QUANTA` under a new top-level key.
3. Write a `renderFooCard()` function next to similar renderers (`renderSentiment`, `renderLlmProvider`).
4. Use `el("div", { class: "card" })` as the root and `cardHead("XX", "Title", chip?)` for the header.
5. Inside `renderOps`, append to the `rows` flow at the appropriate position. Stick to existing grid widths: `.grid-2`, `.grid-3`, `.grid-21`, `.grid-12`.

```js
function renderFooCard() {
  const c = el("div", { class: "card" });
  c.appendChild(cardHead("23", "Foo · subtitle",
    el("span", { class: "chip chip-success" }, "OK")));
  const body = el("div", { class: "card-body" });
  body.appendChild(stat("Foo", String(D.foo.value)));
  c.appendChild(body);
  return c;
}
```

### C.2 · Add a new agent to the debate floor

`renderAgentPipeline()` lays out exactly 5 roles in a fixed scout / floor / verdict structure. To add a sixth (e.g. a `risk_debate.neutral` voice) you have two choices:

1. **Add it as a second-row floor member.** Add `risk_debate_neutral` to `AGENT_META` with a `tone` (use the existing color tokens), add a corresponding SVG branch in `agentIcon()`, add the stage to `D.agentFlow.stages`, and adjust the `debate-row-floor` grid to `1fr 160px 1fr 1fr` (or wrap to two rows).
2. **Add a sub-debate card on the verdict row.** Keep the floor unchanged; render the extra agent next to `reflector` by extending `debate-row-verdict` to `2fr 1fr 1fr`.

**Don't** rename the canonical 5 roles (`regime_tagger`, `bull_debater`, `bear_debater`, `arbiter`, `reflector`). Other modules and the wireframe rely on them.

### C.3 · Add a new chart type

Follow the SVG factory pattern of `candleChart`, `donut`, `rsiChart`, `macdChart`:
- One pure function that takes data and returns an `s("svg", …)` node.
- Use `seededRand(symbol + "yourchart")` if you need deterministic randomness.
- Use the existing color tokens (`var(--success)`, `var(--danger)`, etc.). No new colors.

### C.4 · Add a new doc section

Append an object to `D.docs` with `{ id, num, title, body }`. The `body` parser supports `<b>`, `<code>`, `<br>` — do not invent new tags without extending the parser.

### C.5 · Bumping the cache version

When you change CSS/JS, bump `?v=N` in `index.html` to invalidate the browser cache:

```html
<link rel="stylesheet" href="./styles.css?v=6" />
<script src="./data.js?v=6"></script>
<script src="./app.js?v=6"></script>
```

---

## D · MODIFICATION ACCEPTANCE CHECKLIST

Before declaring any change done, verify all of:

1. **Page still routes:** `#/ops`, `#/pair`, `#/docs`, `#/docs#section-id` all render with no console errors.
2. **No frozen schema field was renamed, removed, or retyped** (§B.1).
3. **No frozen helper signature was changed** (§B.2).
4. **No frozen CSS token name was removed or renamed** (§B.3).
5. **No `.innerHTML` assignment was introduced** (§B.5).
6. **No `RegExp.prototype.exec` call was introduced** (§B.5).
7. **The clock still ticks once per second** (test by waiting 5 s on the page).
8. **The theme toggle still flips dark ↔ light.**
9. **The LLM search input still focuses on Cmd-F / Ctrl-F.**
10. **All other pre-existing cards still render** — visually confirm by scrolling through `/ops` end-to-end.
11. **`?v=N` was bumped** if CSS or JS changed.
12. **Lints clean** — no errors from `ReadLints` on any touched file.

---

The reference design (wireframes, component specs, data shape, design system) follows. It is the **current state** of the shipped app. Treat it as authoritative when in doubt about how a section should look or behave.

---

## 0 · Tech constraints

| Constraint | Value |
| - | - |
| Stack | Vanilla HTML + CSS + JS, no build step |
| Files | `index.html`, `styles.css`, `data.js` (window.QUANTA), `app.js` |
| Browser target | Chromium, Safari, Firefox (modern) |
| Fonts | Inter (UI) + JetBrains Mono (numbers / code) via Google Fonts |
| Charts | Inline SVG, no chart library, no canvas |
| Routing | `#/ops` (default) · `#/pair` · `#/docs#anchor` — hashchange-driven |
| Data | All baked into `data.js` as `window.QUANTA = { ... }` |
| Server | Servable by Python's built-in http server on port 8090 |
| Security | No direct `innerHTML` assignments. No use of `RegExp.prototype.exec`. No inline event handlers in HTML strings. |

---

## 1 · Design tokens

```
COLORS · DARK
  --bg-0          #0a0b0e
  --bg-1          #0f1115
  --bg-2          #14171d
  --bg-3          #1a1e26
  --surface       #11141a
  --surface-2     #161a22
  --stroke-1      #1c2029
  --stroke-2      #252b36
  --stroke-3      #2f3645
  --text-1        #e8ecf2     primary
  --text-2        #aab2c0     secondary
  --text-3        #707a8a     tertiary
  --text-4        #4a5263     quaternary

SEMANTIC
  --accent        #7c9cff     interactive / brand
  --accent-2      #5b7cff     hover
  --accent-soft   rgba(124,156,255,0.12)
  --success       #4ec9b0     gains, healthy, BULL
  --danger        #f0788a     losses, breakers, BEAR
  --warn          #f5c674     alerts, P95 lat, alert dots
  --info          #7cc4ff     informational, SCOUT role

COLORS · LIGHT (toggleable by `data-theme="light"` on <html>)
  Invert bg + text shades, keep semantic the same.

TYPOGRAPHY
  Sans:  Inter 400 / 500 / 600 / 700
  Mono:  JetBrains Mono 400 / 500 / 600
  Scale: 9 · 10 · 11 · 12 · 13 · 15 · 22 · 38 (px)
  Body:  13 px / 1.5 line-height / -0.005em letter-spacing
  Numbers: ALWAYS mono.
  Labels: 10 / 11 px uppercase / letter-spacing 0.08-0.14em.

RADII
  --radius-sm 6px · --radius 10px · --radius-lg 14px · 999px pills

SPACING
  Grid gap 14 / 16 / 18 · Card padding 14 · Page padding 22-26
  Density: respect 13px base everywhere; never inflate.

BACKGROUND
  Page body has subtle radial blue + purple gradients top-left/top-right,
  visible only at 5-10% alpha, never noisy.

BORDERS
  All cards 1px var(--stroke-1) borders, radius 10px.
  Hover lifts border to var(--stroke-3); micro shadow optional.

SHADOWS
  Avoid heavy drop shadows. Use 1px inset highlight + 1px border only.

MOTION
  120-200ms transitions on background, border-color, transform.
  Sparingly animate (pulse dot for live indicators, debate-pulse 1.6s).
```

---

## 2 · Information architecture

### Routes
```
P1  #/ops    Operations console            (default)
P2  #/pair   Pair dashboard
P3  #/docs   Reference / glossary
```

### Top-bar (visible on all pages)
```
[Q] QUANTA · next                | CHIP-strip   | COMBINED EQUITY  | CLOCK · ET    | btns
    v3.0 redesign · op console   |  PAPER       |  $118,436.14     |  1:25:50 PM   | reload KILL theme
                                 |  FREQTRADE   |  −0.47% day      |  ET · UTC−4   |
                                 |  MCP OK      |                  |               |
                                 |  BOT UP …    |                  |               |
```

### Left rail (visible on all pages, sticky, 192 px wide)
```
MONITOR
  1 Ops          active when /ops
  2 Pair         active when /pair
ANALYSIS
  3 Agent        scrolls to #21a on /ops
  4 Risk         scrolls to #16 on /ops
  5 Research     scrolls to #04 on /ops
SYSTEM
  6 Evolution    scrolls to #14 on /ops
  7 LLM          scrolls to #21 on /ops
  8 Config       scrolls to #19 on /ops
REFERENCE
  9 Docs         active when /docs

[ Operator card · quant@quanta · 192.168.1.49:8081 ]
```

Each rail link has a 18×18 numeric chip on the left. Active state: accent-soft background with inset 2px accent border-left, the numeric chip flips to accent fill.

---

## 3 · Page wireframes

Legend used throughout:
- `[ ]` = card
- `( )` = inline pill / chip
- `■` = visual element (chart, sparkline, dot matrix, icon)
- `(W:n)` = card spans `n` of `n` columns
- Decimal indices like `00`, `00a`, `21a` are the cell IDs (rendered as a small numeric tag in the top-left of each card's header).

---

### 3.1 · P1  /ops  — Operations console

```
+-----------------------------------------------------------------------------------+
| Page header                                                                       |
|                                                                                   |
| PAGE 1 · /ops                                                                     |
| Operations console                          (auto-refresh 5s) [Export] [Daily…]  |
+-----------------------------------------------------------------------------------+

+-----------------------------------------------------------------------------------+
| HERO (no decimal — global scoreboard)                                             |
|                                                                                   |
|  CAPITAL · COMBINED            LIVE P&L    REALIZED TODAY   UNREALIZED       DD   |
|  $118,436.14                   −$510.21    −$24.11          −$486.10       0.47% |
|  peak $119,000.83 · 5 open · 3 closed today                                       |
|  [==============|=================|=========]  ribbon                             |
|  DD 0.47%       pause 8%         kill 10%                                         |
+-----------------------------------------------------------------------------------+

+-----------------------------------------------------------------------------------+
| (yellow alert banner — full width, semi-translucent warn background)              |
| • 13/13 pairs blocked  ·  most common: regime (10x) · up_prob_threshold (10x)     |
|   · meta_signal_up (4x)  ·  newest blocker: regime (now)                          |
+-----------------------------------------------------------------------------------+

+----------------------------------+------------------------------------------------+
| 00 Combined equity               | Crypto + Stocks regime grids (2 col split)     |
|    (chip: PAPER)                 |                                                |
|  CRYPTO      STOCKS      BREAKER | 00a Crypto · BTC   (chip: BEAR · 62%)          |
|  $18,901     $99,535     armed   | +--------+--------+--------+                    |
|  −0.52%      −0.49%      8%      | | BTC    | ETH    | SOL    |  12 cells          |
|                                  | | $80383 | $2275  | $94.48 |  symbol            |
|  DAY P&L · COMBINED              | | -0.51% | -0.67% | -0.40% |  price · delta     |
|  −$24.11                         | | sprk   | sprk   | sprk   |  sparkline 28-pt   |
|  -2.11% day                      | +--------+--------+--------+  3-col grid        |
|                                  | | ADA  · XRP   · DOGE  ·                        |
|  [5 open · 0 cr · 5 st]          | | AVAX · LINK  · DOT   ·                        |
|  [closed 24h · −$24.11]          | | ATOM · LTC   · BCH                            |
|                                  | +--------+--------+--------+                    |
|                                  | 00b Stocks · SPY  (chip: BULL · 68%)            |
|                                  | (same 3-col grid, 15 tickers)                  |
+----------------------------------+------------------------------------------------+

+-----------------------------------------------------------------------------------+
| TICKER STRIP (full width, horizontal scroller of recent option fills)             |
|  SOFI SELL 15.50 +35.50 · Alpaca   > NVDA SELL 215.00 +593.50 · Alpaca   > ...    |
+-----------------------------------------------------------------------------------+

+------------------------------------------------+----------------------------------+
| 05 Entry gates · why isn't anything trading?   | 04 Research stream · 24h         |
|    (chip: 0/13 eligible · red)                 |    (chip: 12 events)             |
|                                                |                                  |
| 11 cols: capital_allocation · model_freshness  | 20s  HERMES MCP                  |
| · freqai_predict · volume · regime · …         |      Tool called · get_perf_..   |
|                                                |      trades 6 · sharpe -571.97   |
| Pair      Regime          Gates       Pass Blo |                                  |
| BTC/USD   trending_down   ■■■■■■■■■■■  8/11 reg| 1m   OLLAMA                      |
| ETH/USD   trending_down   ■■■■■■■■■■■  9/11 reg|      Health probe · OK           |
| SOL/USD   trending_down   ■■■■■■■■■■■  9/11 reg|      latency 0.63s · failures 0  |
| ADA/USD   trending_down   ■■■■■■■■■■■  8/11 reg|                                  |
| XRP/USD   unknown         ■■■■■■■■■■■ 10/11 mod| 8m   SENTIMENT                   |
| DOGE/USD  unknown         ■■■■■■■■■■■ 10/11 mod|      Aggregate bullish (+0.15)   |
| AVAX/USD  trending_down   ■■■■■■■■■■■  7/11 mod|      60 headlines · F&G 49       |
| LINK/USD  trending_down   ■■■■■■■■■■■  7/11 mod|                                  |
| DOT/USD   trending_down   ■■■■■■■■■■■  9/11 reg| 28m  BTC HMM                     |
| ATOM/USD  trending_down   ■■■■■■■■■■■  8/11 reg|      Regime -> trending_down     |
| LTC/USD   trending_down   ■■■■■■■■■■■  8/11 reg|      held 1.0h before transition |
| BCH/USD   trending_down   ■■■■■■■■■■■  8/11 reg|                                  |
|                                                | … 8 more rows                    |
| dot legend: pass=green · fail=red · unknown=mu |                                  |
+------------------------------------------------+----------------------------------+

+------------------------------------------------+----------------------------------+
| 10 Stocks · Wheel + Shark   (chip: NYSE OPEN)  | 14 EPT · champion genome         |
|  Portfolio $99,534.73   Cash $99,534.73        |  ID gen0-011  Fitness 0.754      |
|  BP $199,069.46  Premium $1108  collateral $45 |  Sharpe 0.88  MaxDD -8.22%       |
|                                                |  PF 1.64      Trades 66          |
|  Sym  Type        Qty  Strike   Expiry  Premiu |  Stop/TP -2.07%/+5.34%           |
|  SOFI SHORT PUT    1   $15.50   05-22   $35.50 |  Features 23                     |
|  NVDA SHORT PUT    1   $215.00  05-22   $593.5 |                                  |
|  PLTR SHORT PUT    1   $130.00  05-22   $211.0 | ─────────────────────────────── |
|  MARA SHORT PUT    1   $12.00   05-22   $35.50 |                                  |
|  HOOD SHORT PUT    1   $78.00   05-22   $232.5 | 22 Decision audit · BTC/USD      |
|                                                |  2026-05-12 01:40:18             |
|                                                |  ENTRY · long  pill(green)       |
|                                                |  meta_signal=+0 conf=0.00        |
|                                                |  regime=trending_down tft_up=0.37|
|                                                |                                  |
|                                                |  2026-05-11 00:20:20  ENTRY long |
+------------------------------------------------+----------------------------------+

+------------------------------------------------+----------------------------------+
| 08 Open positions          (chip: 5 active)    | 07a Service health · 8 probes    |
|                                                |     (chip: 8/8 up · green)       |
|  Symbol Venue   Side       Qty Entry Mark uPnL |                                  |
|  SOFI   Alpaca  SHORT_PUT   1  15.50  —    —   |  •  hermes_gateway    heartbeat… |
|  NVDA   Alpaca  SHORT_PUT   1 215.00  —    —   |  •  hermes_mcp        heartbeat… |
|  PLTR   Alpaca  SHORT_PUT   1 130.00  —    —   |  •  hermes_dashboard  heartbeat… |
|  MARA   Alpaca  SHORT_PUT   1  12.00  —    —   |  •  ollama            tcp        |
|  HOOD   Alpaca  SHORT_PUT   1  78.00  —    —   |  •  freqtrade         http 200   |
|                                                |  •  postgres          tcp        |
|  notes are tiny mono text:                     |  •  influxdb          http 200   |
|  expiry=2026-05-22 contract=SOFI260522P000155… |  •  grafana           http 200   |
+------------------------------------------------+----------------------------------+

+-----------------------------------------------------------------------------------+
| 21a Agent flow · multi-agent DEBATE FLOOR                                         |
|     meta:  3 of 5 roles active · 11 calls in 24h · just now                       |
|                                                                                   |
|     +-----------------------------------------+                                   |
|     | (o) regime_tagger   OBSERVER · SCOUT    |   <- centered, max 460px          |
|     |   • no calls today                      |                                   |
|     |   ┃ context: (no calls today)           |                                   |
|     +-----------------------------------------+                                   |
|                                                                                   |
|  +----------------------+  +--------+  +----------------------+                   |
|  | (bull)  bull_debater |  |  VS    |  |  BEARISH  bear_debat |                   |
|  |   BULLISH            |  | ====== |  |   3h ago · 2 ✓ 0 ✕   |                  |
|  |   3h ago · 3 ✓ 0 ✕   |  | 102.6s |  |   ┃ "From a conserv- |                  |
|  |   ┃ argues UP        |  | ●LIVE  |  |     ative risk       |                  |
|  |   ┃ "AAPL presents…" |  |  rnd 3 |  |     perspective…"    |                  |
|  +----------------------+  +--------+  +----------------------+                   |
|  (icon LEFT, snippet     ) (gradient ) (icon RIGHT, snippet RIGHT,                |
|  ( left-bordered green   ) (line g→r ) ( right-bordered red, mirrored)            |
|                                                                                   |
|  +------------------------------------------+  +---------------------------------+|
|  | (scales) arbiter   VERDICT · GRADER      |  |(book) reflector  POST-MORTEM    ||
|  |   • 52m ago · 6 ✓ 0 ✕  (alert amber dot) |  |   no calls today                ||
|  |   ┃ verdict: { "grade":"C", "pattern":   |  |   waiting for trigger           ||
|  |   ┃   "stop_hunt", "action": "tighten…" }|  |                                 ||
|  +------------------------------------------+  +---------------------------------+|
|                                                                                   |
|  • flow · scout sets context -> bull and bear argue -> arbiter grades -> refl…    |
|                                            models · hermes3:8b on ollama · primary|
+-----------------------------------------------------------------------------------+

ICON SPEC (custom SVG, 24x24 viewBox, stroke 1.5, current-color)
  regime_tagger : compass — circle r=9 + needle (two triangles, blue fill emphasizes N)
  bull_debater  : bull head — two upward-curving horns, rounded jaw, two eye dots,
                              smile, nose ring
  bear_debater  : bear head — two ear circles top, big head circle, two eye dots,
                              snout ellipse, mouth
  arbiter       : scales of justice — vertical pole, horizontal beam, two triangular
                                       cups, base
  reflector     : open book — two mirrored pages with 3-4 horizontal lines each

AGENT CARD BUBBLE COLOR-CODING
  scout      info-blue left-border 3px + faint info gradient
  bull       success-green left-border 3px + faint success gradient
  bear       danger-red RIGHT-border 3px + faint danger gradient (mirrored)
  arbiter    warn-amber left-border 3px + faint warn gradient
  reflector  empty dashed bordered placeholder when idle

DEBATE DIVIDER (center column, 160 px wide)
  Row 1: VS pill 11px mono 700 letter-spacing 0.2em on bg-1 disc with stroke-3 ring
  Row 2: 2 px tall gradient line: success -> transparent -> danger
         with arrowheads pointing INWARD at each end (border-triangle hack)
  Row 3: "debate latency · 102.6s" 10px mono muted with bold number
  Row 4: pulsing green dot + "LIVE · round 3" 10px mono success
         (debate-pulse keyframe: scale 1 -> 1.4 -> 1, opacity 1 -> 0.5 -> 1,
          1.6s ease infinite)

+-----------------------------------------------------------------------------------+
| 21 LLM activity · last 24h                                                        |
|    meta: feed · 11 calls · 4.8k tokens · 29 KB on disk · just now (● 11 · 24H)    |
|                                                                                   |
|   STATS STRIP · 6 cells horizontally                                              |
|   CALLS   TOKENS   AVG LAT     P95 LAT      OLLAMA   SUCCESS                      |
|   11      4.8k     40.83s w    122.78s n    100% p   100.0% p                     |
|   (w=warn color, n=neg/red, p=pos/green)                                          |
|                                                                                   |
|   FILTERS ROW · bg-1 background, 1 px borders top & bottom                        |
|   +-------------------------+----------------------------------+-----------------+|
|   | AGENT  [all agents (4) v]| SEARCH  [regex over agent/model] |   11/11 rows   ||
|   +-------------------------+----------------------------------+-----------------+|
|                                                                                   |
|   TABLE · time  · agent  ·  model · tier            · lat   · tokens    · •       |
|   13:02:30  trade_reviewer           hermes3:8b · fast  17.94s    237/155     •   |
|   13:02:12  trade_reviewer           hermes3:8b · fast  19.81s w  240/177     •   |
|   13:01:53  trade_reviewer           hermes3:8b · fast  19.75s w  236/174     •   |
|   13:01:33  trade_reviewer           hermes3:8b · fast  17.92s    240/148     •   |
|   13:01:15  trade_reviewer           hermes3:8b · fast  19.59s w  236/140     •   |
|   10:45:30  risk_debate.conservative hermes3:8b · deep  12.07s    375/174     •   |
|   10:45:18  risk_debate.aggressive   hermes3:8b · deep  11.21s    234/164     •   |
|   22:52:13  risk_debate.neutral      hermes3:8b · deep  16.41s    432/146     •   |
|   22:51:57  risk_debate.conservative hermes3:8b · deep  17.73s    335/172     •   |
|   22:51:39  risk_debate.aggressive   hermes3:8b · deep 122.78s n  234/187     •   |
|   14:08:43  risk_debate.aggressive   qwen2.5:72b· deep 173.93s n  233/128     •   |
|                                                                                   |
|   FOOTER · keyboard hints                                                         |
|   click any row · [Esc] closes modal · [Cmd-F] focuses search                     |
|                                                                                   |
|   LAT COLORING:    < 18s   text-2          (neutral)                              |
|                  18-60s    warn amber                                             |
|                   > 60s    danger red                                             |
|   Cmd-F / Ctrl-F   focus #llm-search input globally on /ops                       |
|   Live search    filters table rows AND updates "n / n rows" counter              |
+-----------------------------------------------------------------------------------+

+------------------------------------------------+----------------------------------+
| 13 Sentiment aggregate  (chip: BULLISH · green)| 21b LLM provider · routing       |
|                                                |     (chip: OLLAMA PRIMARY)       |
|   NET      DEEP     FAST                       |  PRIMARY      PRIMARY LATENCY    |
|   +0.15    +0.30    +0.00                      |  Ollama       630ms              |
|           Claude    Llama                      |  11 models    (pos color)        |
|                                                |                                  |
|   F&G      AGREEM.  AGE                        |  CALLS · 24H  SAVED VS CLAUDE    |
|   49       yes      8m                         |  82           $0.04              |
|   Neutral           60 hdl                     |                                  |
|                                                |  SERVICE BREAKERS                |
|                                                |   PASS  ollama:deep  closed · 0  |
|                                                |   PASS  ollama:fast  closed · 0  |
+------------------------------------------------+----------------------------------+

+------------------------------------------------+----------------------------------+
| 16 Circuit breakers     (chip: ARMED · green)  | 18 Readiness · gate matrix       |
|                                                |    (chip: NOT READY · red)       |
|   PORTFOLIO · unified_risk                     |                                  |
|   PASS  combined drawdown    0.47% / 10.0%     |   Sharpe    ■■░░░░░░░  > 1.50    |
|   PASS  stocks data stale    snapshot fresh    |              −571.97    BLOCK    |
|   PASS  stocks data untrust. trust window OK   |   MaxDD     ■■■■■■■■■  < 12%     |
|                                                |              7.1%       PASS     |
|   SERVICE · LLM / MCP                          |   PF        ■░░░░░░░░  > 1.40    |
|   PASS  ollama:deep   failures 0 · closed      |              0.00       BLOCK    |
|   PASS  ollama:fast   failures 0 · closed      |   Win rate  ■░░░░░░░░  > 55%     |
|                                                |              0.0%       BLOCK    |
|                                                |   Trades    ■░░░░░░░░  >= 200    |
|                                                |              6          BLOCK    |
|                                                |                                  |
|                                                |   mode standard · 6 trades · …   |
+------------------------------------------------+----------------------------------+

+------------------------------------------------+----------------------------------+
| 19 Regime config editor                        | 20 Slack preview · next brief    |
|    (chip: atomic write)                        |    (chip: 00:00 UTC)             |
|                                                |                                  |
|   ENTRY DELTA · PER REGIME (2-col)             |   ┃ Quanta · daily P&L · …       |
|   trending_up     [-0.15]                      |   ┃                              |
|   trending_down   [+0.15]                      |   ┃ • Day P&L: −$24.11 (−2.11%)  |
|   mean_reverting  [ 0.00]                      |   ┃ • Trades: 3 · wins 0 · ...   |
|   high_volatility [+0.08]                      |   ┃ • Sharpe: −571.97 · MaxDD …  |
|                                                |   ┃ • Best pair: BTC/USD $-8.46  |
|   EXIT DELTA · PER REGIME (2-col)              |   ┃ • Worst pair: BCH/USD $-15…  |
|   trending_up     [+0.05]                      |   ┃ • Regime distribution …     |
|   trending_down   [-0.05]                      |                                  |
|   mean_reverting  [-0.10]                      |                                  |
|   high_volatility [ 0.00]                      | 12 Quick actions                 |
|                                                |    (chip: control panel)         |
|   SCALAR PARAMS (2-col)                        |                                  |
|   high_vol_stake_factor      [0.70]            |   [PAUSE TRADING]                |
|   high_vol_min_confidence    [0.65]            |     freezes new entries          |
|   mean_rev_take_profit       [0.012]           |   [RESUME] re-allows entries    |
|   trending_up_trail_trigger  [0.025]           |   [TRIGGER EVOLUTION]            |
|   trending_up_trail_distance [-0.020]          |   [REBALANCE WEIGHTS]            |
|   tft_min_confidence         [0.40]            |   [DAILY SLACK BRIEF]            |
|   meta_min_confidence        [0.35]            |   [KILL · ARM]   (red border)    |
|                                                |     hold 1.5s to flatten + halt  |
|   [APPLY (primary)]  [RESET]                   |                                  |
+------------------------------------------------+----------------------------------+

+-----------------------------------------------------------------------------------+
| 21 MCP tool console · 19 tools     (chip: POST /api/ops/mcp/{name})               |
|                                                                                   |
|   GRID of chips · auto-fit minmax(220px,1fr) · 6 px gap                           |
|   [get_open_trades]  [get_trade_history]  [get_daily_pnl]  [get_perf_metrics]     |
|   [get_evolution_status]  [! trigger_evolution_cycle]  <- danger-tinted chip      |
|   [get_champion_genome]  [get_risk_status]  [! pause_trading]  [! resume_trading] |
|   [get_current_regime]  [get_sentiment_scores]  [get_onchain_signals]             |
|   [query_trade_journal]  [get_regime_history]  …                                  |
|                                                                                   |
|   EXECUTE FRAME (with subtle gradient border)                                     |
|   +-----------------------------------------------------------------------------+ |
|   | TOOL   [get_open_trades v]   (full-width select)                            | |
|   | ARGS · JSON BODY                                                            | |
|   | +-----------------------------------------------------------------------+   | |
|   | | {}                                                                    |   | |
|   | +-----------------------------------------------------------------------+   | |
|   |                                                       [EXECUTE (primary)]   | |
|   +-----------------------------------------------------------------------------+ |
+-----------------------------------------------------------------------------------+
```

---

### 3.2 · P2  /pair  — Pair dashboard

```
PAGE 2 · /pair
Pair dashboard                                  [BTC/USD v]   [1m 5m 15m 1h 4h 1d]

+-----------------------------------------------------------------------------------+
| PAIR HEADER (in-card, replaces a normal card-head)                                |
| BTC/USD   $80,383   −0.52%   day P&L −$24.11   (300 bars · 5m) (REGIME            |
|                                                  trending_down · 62%) (GATE BLOCK)|
|                                                                                   |
| CANDLESTICK CHART · 980x320 · 80 candles                                          |
|   - up candles  green body + green wick                                           |
|   - down candles red body + red wick                                              |
|   - 5 horizontal gridlines, dashed, with right-aligned price labels               |
|   - 4 trade markers along the timeline: triangles labeled B/S at bottom/top       |
+-----------------------------------------------------------------------------------+

+------------------------------------------------+----------------------------------+
| 01a RSI · 14                                   | 01b MACD · 12 / 26 / 9           |
|                                                |                                  |
|  height 90 px · accent line                    |  height 90 px · histogram bars   |
|  shaded 30-70 band, dashed 70 amber & 30 green |  green for up, red for down      |
|  labels "70" / "30" at left                    |  MACD line accent · signal warn  |
+------------------------------------------------+----------------------------------+

+-------------------+-------------------+-------------------+-----------------------+
| 02 Model view     | 03 Context        | 06 Champion       | 07 P&L history        |
|                   |                   |                   |                       |
|  (o) donut 110px  |  REGIME           |  GEN     1        |  14d net  −$98.59     |
|  ↑ 39.7%          |  trending_down    |  CHAMP   gen0-    |                       |
|  → 23.7%          |  conf 62.4%       |          011      |  05-12  −$24.11       |
|  ↓ 36.6%          |                   |  FITNESS 0.754    |  05-11  −$31.46       |
|                   |  SENTIMENT        |  RUNNER-UP        |  05-10  −$43.02       |
|  legend           |  +0.150           |  gen1-r02         |                       |
|  P(UP)   39.7%    |  conf 0.0%        |                   |                       |
|  P(FLAT) 23.7%    |                   |                   |                       |
|  P(DOWN) 36.6%    |  ON-CHAIN         |                   |                       |
|                   |  netflow z -0.56  |                   |                       |
|  META AGENT ·     |  MVRV 1.52        |                   |                       |
|  HOLD ·           |  whale 1h 6.05    |                   |                       |
|  meta_signal=0    |                   |                   |                       |
+-------------------+-------------------+-------------------+-----------------------+

+---------------------+-------------------------------------------------------------+
| 04 Open positions   | 05 Recent trades · last 10                                  |
|   (chip: 0 on BTC)  |   (chip: 0 / 6 green · red)                                 |
|                     |                                                             |
|  no open positions  |  Pair    Side   Entry      Exit      PnL %   Closed   Reaso|
|  on this pair       |  BCH/USD LONG   444.25     445.56    -0.70%  05-12 …  bb_bo|
|   (centered mono    |  BTC/USD LONG   80822.21   81267.63  -0.45%  05-12 …  meta_|
|    placeholder)     |  BCH/USD LONG   450.26     449.29    -0.96%  05-12 …  meta_|
|                     |  SOL/USD LONG   97.66      97.22     -0.95%  05-11 …  meta_|
|                     |  BTC/USD LONG   81522.83   81690.09  -1.23%  05-11 …  freqa|
|                     |  SOL/USD LONG   96.20      96.31     -2.26%  05-10 …  freqa|
+---------------------+-------------------------------------------------------------+
```

---

### 3.3 · P3  /docs  — Reference / glossary

```
PAGE 3 · /docs
Reference · glossary                                            (chip: operator ref)

+--------------------+--------------------------------------------------------------+
| TOC · sticky 280px | SECTIONS · stacked 10 cards                                  |
|                    |                                                              |
|  01 · Overview     | +----------------------------------------------------------+ |
|  02 · Market reg.  | | SECTION 01                                               | |
|  03 · Entry gates  | | Overview · what this bot does                            | |
|  04 · Breakers     | |                                                          | |
|  05 · Strategy     | | Paper-trading multi-asset (crypto + stocks) with regime- | |
|  06 · Crypto vs    | | gated entries. `freqtrade` drives 12 USD-quote crypto …  | |
|       Stocks       | +----------------------------------------------------------+ |
|  07 · Risk vocab   |                                                              |
|  08 · Architecture | +----------------------------------------------------------+ |
|  09 · Operator     | | SECTION 02                                               | |
|       actions      | | Market regimes                                           | |
|  10 · Glossary     | | 5 states classified hourly by an HMM …                   | |
|                    | |  • trending_up    · easiest entries · entry_delta -0.15  | |
|                    | |  • trending_down  · HARD BLOCK by default                | |
|                    | |  • mean_reverting · mean_rev_take_profit 1.2%            | |
|                    | |  • high_volatility · stake 0.7x                          | |
|                    | |  • unknown        · soft block, very-high-confidence only| |
|                    | +----------------------------------------------------------+ |
|                    |                                                              |
|                    | … sections 03-10 follow same pattern.                        |
|                    |                                                              |
|                    | Body content supports a tiny whitelisted markup:             |
|                    |  <b> bold       <code> inline code      <br> paragraph break| |
|                    | Parse manually — no `innerHTML`, no `RegExp.prototype.exec`. | |
+--------------------+--------------------------------------------------------------+
```

---

## 4 · Component spec library

Build these as functions in `app.js`. Each accepts data, returns a DOM Node.

### 4.1 · `el(tag, attrs?, ...children)`
Generic element builder. `attrs.class` sets className. Strings/numbers become text nodes. Skip falsy children. Use `on*` keys for event listeners.

### 4.2 · `s(tag, attrs?, ...children)`
SVG element builder using `createElementNS` with the SVG namespace URI.

### 4.3 · `stat(label, value, opts?)`
```
.stat
  .stat-label       10px mono uppercase color text-3
  .stat-value       22px mono 600 (38px in hero)
                    .neg color danger
                    .pos color success
                    .warn color warn
                    .acc color accent
  .stat-sub         11px mono color text-3
```

### 4.4 · `cardHead(tag, title, trailing?)`
```
.card-head
  .tag        24-px wide right-aligned mono 10px color text-4 (decimal index)
  h2.h2       15px 600 letter-spacing -0.005em
  trailing    (chip / meta string) right-aligned
```

### 4.5 · `sparkline(symbol, deltaPct, w=120, h=22)`
Seeded RNG by symbol so the line is deterministic. 28 points. Fill path under line with 10% alpha. Color: green if delta ≥ 0, red otherwise. End-cap circle r=2 at the right edge.

### 4.6 · `candleChart(symbol, basePx, deltaPct)`
- 80 candles, 980×320 viewport
- 5 dashed gridlines + 5 right-aligned price labels
- 4 entry/exit triangles spaced at i=18, 32, 50, 68 with labels B/S
- Deterministic seed = `symbol + "candle"`

### 4.7 · `donut(parts, size=110)`
Multi-segment stroke-dasharray ring. Each `part = { label, value, color }`. Center text shows the largest segment's value + label.

### 4.8 · `rsiChart()` and `macdChart()`
Compact 460×90 indicator charts. RSI with 70/30 band + dashed thresholds. MACD with histogram bars (red/green) plus two line series (accent for MACD, warn for signal).

### 4.9 · `marketCell(symbol, price, delta, sparkColor)`
A single cell in the 3-col regime grid. Sym name top, px and delta% on a row, sparkline below.

### 4.10 · `gatesTable(rows, columns)`
- Header row with 11 column tooltips
- Each row: pair (mono bold), regime (mono small muted), 11 colored 9×9 squares (`pass` green, `fail` red, `unknown` muted), pass count `n/11`, first blocker (mono red)

### 4.11 · `agentCard(stage)`
- 52×52 round avatar with custom SVG icon (see icon spec above)
- Role name (13 mono 600) + stance pill
- Meta line: dot + time + model + calls + avg + p95
- Speech bubble: eyebrow label + quote text, color-bordered per stance
- Bear card mirrors layout: icon on right, body left, text-align right

### 4.12 · `debateDivider(latency, round)`
160 px column between bull and bear cards. Contains VS pill on top of a gradient line (success → danger) with inward arrowheads, "debate latency · X" line, pulsing green dot + "LIVE · round N".

### 4.13 · `feedItem(when, source, title, detail)`
Two-column row (56 px time | 1fr content). Hover state: bg-2 + 2 px left accent border.

### 4.14 · `breakerRow(status, name, meta)`
Three-column grid (72 / 1fr / auto). `status` pill: PASS green or FAIL red, mono 10px 700 letter-spacing 0.1em.

### 4.15 · `gateMatrixRow(name, current, threshold, direction, unit, status)`
- 110 / 1fr / 110 / 80 column grid
- `gm-bar` 6 px tall track with colored fill (success or danger)
- threshold label mono 11px text-3
- status pill (PASS green / BLOCK red)

### 4.16 · `chip(kind, text)`
Variants: default · success · info · warn · danger
- 11px mono 500 letter-spacing 0.02em
- 4×8 padding, 999px radius
- 6 px dot left (colored to match `kind`)

### 4.17 · `quickActionBtn(title, sub, danger=false)`
Tall flexible button: 12×14 padding, two-line content (title uppercase mono 12px 600, sub 11px text-3). Danger variant uses red border + faint red bg + red title.

### 4.18 · `mcpToolChip(name)`
Same as `chip` but full-width inside grid; if name starts with `!` apply `warn` variant.

---

## 5 · Routing & global behaviors

### 5.1 · Hash routing
```
#/ops            -> renderOps()    (default if no hash)
#/pair           -> renderPair()
#/docs           -> renderDocs()
#/docs#section02 -> renderDocs() then scrollIntoView the section
```
- Listen on `window.hashchange`
- Rail link `.active` reflects current route
- `window.scrollTo(0,0)` on every route change

### 5.2 · Live clock
Update every 1 s in ET (`America/New_York`), formatted `h:mm:ss A`. Use `Intl.DateTimeFormat` with `timeZone`.

### 5.3 · Theme toggle (top-right ◐)
Flip `<html data-theme="dark|light">`. Persist optional. CSS tokens swap.

### 5.4 · Keyboard
- `Cmd-F` / `Ctrl-F` → focus `#llm-search` input on /ops (only). `event.preventDefault()`.

### 5.5 · LLM filter behavior
Live filter `D.llmActivity.rows` by:
- AGENT dropdown (exact match unless `all agents`)
- SEARCH text (case-insensitive substring on agent+model+time)
Update the visible row count `"n / N rows"`.

### 5.6 · DOM safety
Build all DOM via `el()` and `s()`. Doc body markup parsed with a manual whitelist tokenizer (only `<b>`, `<code>`, `<br>`) — character-by-character scan instead of regex matching. No `innerHTML` assignment anywhere.

---

## 6 · `data.js` shape

`window.QUANTA = { …, }` with these keys (truncated for brevity; all data is real, scraped from a live SPA):

```js
{
  meta: { capturedAt, operator, source, version },

  scoreboard: {
    capital: 118436.14, livePnL: -510.21, realizedToday: -24.11,
    unrealized: -486.10, drawdown: 0.47, peak: 119000.83,
    open: { total: 5, crypto: 0, stocks: 5 },
    closedToday: 3, dayPct: -0.47, pauseThreshold: 8, killThreshold: 10
  },

  combined: { equity, dayPnL, dayPct,
              crypto: {equity, deltaPct}, stocks: {equity, deltaPct},
              breaker: "armed" },

  crypto: { regime: "BEAR", confidence: 62, holdTime: "1h 00m",
            pairs: [{ sym, px, delta }, …12] },
  stocks: { regime: "BULL", confidence: 68,
            tickers: [{ sym, px, delta }, …15] },
  stocksTicker: [{ sym, side, ref, pnl, venue }, …],

  liveResearch: { aggregate, headline, headlines,
                  fearGreed: { score, label } },

  pair: {
    selected: "BTC/USD", timeframe: "5m", px, deltaPct, dayUSD,
    regime: "TRENDING_DOWN", confidence: 62, gate: "BLOCK", bars: 300,
    options: [12 pairs], timeframes: ["1m","5m",…],
    model:    { pUp, pFlat, pDown, metaSignal, metaConf, tftConf, decision },
    context:  { regimeLabel, regimeConf, sentiment, sentimentConf,
                onchain: { netflowZ, mvrv, whale1h } },
    champion: { gen, id, fitness, runnerUp },
    pnlHistory: [{ date, value }, …],
    net14d,
    recentTrades: [{ pair, side, entry, exit, pnlPct, closed, reason }, …]
  },

  gates: {
    blocked: 13, total: 13,
    mostCommon: [["regime",10],["up_prob_threshold",10],["meta_signal_up",4]],
    columns: [11 names],
    rows: [{ pair, regime, states: [11 of pass|fail|unknown],
             passes, blocker }, …12]
  },

  openPositions: [{ sym, venue, side, qty, entry, mark, upnl, note }, …5],

  wheel: {
    portfolio, cash, buyingPower, age, open, premium, collateral,
    contracts: [{ sym, type, qty, strike, expiry, premium }, …5],
    shark: { mode, trades, winRate, breaker }
  },

  agentFlow: {
    active: 3, total: 5, calls24h: 11, updated: "just now",
    stages: [
      { role: "regime_tagger", model: null,
        timeAgo: "no calls today", alert: false,
        calls: null, avg: null, p95: null, lastSnippet: null },
      { role: "bull_debater",  model: "hermes3:8b",
        timeAgo: "3h ago", alert: false,
        calls: "3 ✓ 0 ✕", avg: "102.6s", p95: "122.8s",
        lastSnippet: '{ "assessment": "AAPL presents a strong upside scenario backed by services-margin expansion …" }' },
      { role: "bear_debater",  model: "hermes3:8b",
        timeAgo: "3h ago", alert: false,
        calls: "2 ✓ 0 ✕", avg: "14.9s",  p95: "12.1s",
        lastSnippet: '{ "assessment": "From a conservative risk perspective, AMD overhang and CHIPS funding risk argue down …" }' },
      { role: "arbiter",       model: "hermes3:8b",
        timeAgo: "52m ago", alert: true,
        calls: "6 ✓ 0 ✕", avg: "18.6s",  p95: "19.8s",
        lastSnippet: '{ "grade": "C", "pattern": "stop_hunt", "action": "tighten stops in bear regime" }' },
      { role: "reflector",     model: null,
        timeAgo: "no calls today", alert: false,
        calls: null, avg: null, p95: null, lastSnippet: null }
    ],
    arrowLatencies: ["102.6s","14.9s","18.6s","—"]
  },

  llmActivity: {
    calls: 11, tokens: "4.8k", avgLat: "40.83s", p95Lat: "122.78s",
    ollamaPct: 100, successPct: 100, diskKB: 29, updated: "just now",
    agentFilters: ["all agents (4)","trade_reviewer",
                   "risk_debate.aggressive","risk_debate.conservative",
                   "risk_debate.neutral"],
    rows: [{ time, agent, model, lat, tokens, success }, …11]
  },

  researchStream: [{ when, source, title, detail }, …12],
  services:       [{ name, probe, endpoint }, …8],
  breakers: { portfolio: [{ status, name, meta },…3],
              service:   [{ status, name, meta },…2] },

  readiness: {
    mode: "standard", trades: 6, buckets: 3, startEquity: 1392.59,
    gates: [
      { name:"Sharpe",   current:-571.97, threshold:1.50,
        status:"BLOCK", direction:">" },
      { name:"MaxDD",    current:7.1,     threshold:12,
        status:"PASS",  direction:"<",  unit:"%" },
      { name:"PF",       current:0.00,    threshold:1.40,
        status:"BLOCK", direction:">" },
      { name:"Win rate", current:0.0,     threshold:55,
        status:"BLOCK", direction:">",  unit:"%" },
      { name:"Trades",   current:6,       threshold:200,
        status:"BLOCK", direction:">=" }
    ]
  },

  ept: { id, fitness, sharpe, maxDD, profitFactor,
         nTrades, stop, tp, features },

  decisions: [{ ts, dir, metaSignal, conf, regime, tftUp }, …],

  sentiment: { label:"BULLISH", net:0.15, deep:"+0.30", fast:"+0.00",
               fearGreed:{score:49,label:"Neutral"},
               agreement:"yes", headlines:60, age:"8m" },

  llmProviders: {
    callsCrypto24h: 82, saved24hUSD: 0.04,
    primary: { name:"Ollama",
               state:"11 models · probed 97s ago", latency:"630ms" },
    breakers: [
      { name:"ollama:deep", state:"closed · failures 0" },
      { name:"ollama:fast", state:"closed · failures 0" }
    ]
  },

  configEditor: {
    entryDelta: { trending_up:-0.15, trending_down:0.15,
                  mean_reverting:0, high_volatility:0.08 },
    exitDelta:  { trending_up:0.05,  trending_down:-0.05,
                  mean_reverting:-0.1, high_volatility:0 },
    scalars: {
      high_vol_stake_factor: 0.7,
      high_vol_min_confidence: 0.65,
      mean_rev_take_profit: 0.012,
      trending_up_trail_trigger: 0.025,
      trending_up_trail_distance: -0.02,
      tft_min_confidence: 0.4,
      meta_min_confidence: 0.35
    }
  },

  slack: { head: "Quanta · daily P&L · …", lines: [6 strings] },

  mcpTools: [19 tool names — names starting with `!` are state-mutating],

  docs: [{ id, num, title, body }, …10]
}
```

---

## 7 · Reference acceptance criteria (current shipped state)

These describe what the **existing** app already satisfies. After your change, they must **still** all be true:

1. **No frameworks.** Plain HTML/CSS/JS only.
2. **DOM is built safely.** No assignments to the `innerHTML` property anywhere. No use of `RegExp.prototype.exec`. No inline `on*` handlers in HTML strings. All DOM is created via `createElement` + `createElementNS`.
3. **Hash routing works:** `#/ops`, `#/pair`, `#/docs`, `#/docs#section-id`. Rail link is `.active` based on route.
4. **Layout matches wireframes** at viewport ≥ 1280 px wide; collapses gracefully below.
5. **Top bar** sticks; contains brand, status chips, combined-equity stat, ET clock updating every second, refresh button, KILL · ARM (red), theme toggle.
6. **Hero scoreboard** shows capital, live P&L, realized today, unrealized, drawdown with the gradient DD ribbon + pause/kill markers.
7. **Markets** render as a 3-col grid of cells with seeded sparklines deterministic per symbol.
8. **Gates table** shows 13 rows × 11 colored dots + first blocker.
9. **Agent flow** is the debate-floor layout described in §3.1, with all 5 custom SVG icons and a pulsing LIVE indicator in the center divider.
10. **LLM activity** has stats / filters / table / footer; Cmd-F focuses search; the search and agent select live-filter the table and update the row count; latency cells get tier coloring.
11. **Theme toggle** flips dark ↔ light by toggling `data-theme`.
12. **Pair page** has a candlestick chart + RSI + MACD + donut model view + recent trades.
13. **Docs page** has sticky TOC and 10 sections; smooth-scroll to `#anchor` from TOC clicks.
14. **No console errors**; passes a hard refresh; all sections render with the data above.

> For the **change you are making**, also run the §D modification checklist at the top of this file.

---

## 8 · File layout (current)

```
quanta-next/
├── index.html        # shell, top-bar, left-rail, script tags (with ?v=N cache busting)
├── styles.css        # ~25 KB — design system + every component class
├── data.js           # window.QUANTA = {…} — single source of truth for all values
├── app.js            # ~50 KB — helpers + chart factories + page renderers + routing
└── PROMPT.md         # this document
```

A Python http server is already running on port 8090. Open `http://127.0.0.1:8090/?v=N#/ops`.

---

## 9 · Source of truth

All data values come from a real Quanta SPA at `192.168.1.49:8081` captured 2026-05-12 13:25 ET via Playwright. The values are already wired into `data.js`. **Do not invent numbers** when you add or extend a section — either copy them from §6 verbatim or pull them from `window.QUANTA`. The redesign's value is in the visual + IA + interaction, not in inventing data.

---

## 10 · TL;DR for the implementer

You are **modifying an existing app**, not rebuilding one. Before you write a single line:

1. Read `quanta-next/index.html`, then `data.js`, then `styles.css`, then `app.js` (top-down — read at least the helper section and one full page renderer).
2. Find the section closest to what the user is asking for. Most changes are 1–3 surgical additions, not rewrites.
3. Re-use existing helpers (`el`, `s`, `stat`, `cardHead`, `makeTable`, `sparkline`, etc.). Don't write new ones unless you've checked there isn't already one.
4. Add to `window.QUANTA`, never restructure it.
5. Add new CSS classes at the bottom of the relevant section in `styles.css`; never rename existing ones.
6. Bump `?v=N` in `index.html` if you touched CSS or JS.
7. Walk the §D checklist before saying "done."

End of prompt.
