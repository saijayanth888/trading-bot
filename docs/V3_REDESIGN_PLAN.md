# V3 REDESIGN PLAN — Quanta Operator Console

> **Branch:** `feature/v3-frontend` (off `main @ 63ded54`)
> **Date:** 2026-05-12
> **Status:** ✅ Plan locked · operator sign-off received · ready for Wave 0 (Token Smith) to start writing code
> **Mandate from operator:** *"radical design… columns cards popping pop out refresh buttons kill everything… not breaking any stuff… big plan with multi agent"*

## Operator decisions (locked, 2026-05-12)

| Decision | Choice |
| - | - |
| §3 design thesis | ✅ **Approved as-is** — "control surface that physically feels like a trading desk — depth, gravitas, kill-everything-now-prominent, recognizable on sight from across the room." |
| §5 signature moves | ✅ **All 7 ship in V3.0**: DD Ribbon · Debate Floor · Gates Matrix · Kill Bar · Heartbeat · Sparkline Strip · Cmd-K palette |
| §7 multi-agent parallelism | ✅ **Balanced — 4 subagents in parallel**, wave-by-wave (cleaner diffs over fastest) |
| Deploy strategy | ✅ **Ship in-place to live `/ops`** with `?v=` cache-busting (no A/B, no `/ops/v3` route, no feature flag). Forces commitment. |
| Backend changes | ✅ **Frontend-only** — every V3 card derives from the existing 43 `/api/ops/*` endpoints; no new endpoints, no rename, no removal |

All §11 (later in this doc) is now historical context — these are the locked answers.

This is the master plan for the V3 redesign of the Quanta dashboard. It is grounded in:

1. **A live audit** of the production dashboard at `http://192.168.1.49:8081/` — every page, every card, every theme, every density, every state.
2. **31 captured `/api/ops/*` response payloads** at `Project-Doze/api-samples/*.json` (286 KB total) — real production data shapes, not invented JSON.
3. **A cross-checked compatibility contract** with `Project-Doze/TRADING_BOT_PROMPT.md` so the redesign cannot accidentally break the 4 locked operator decisions, the 8 risk gates, the 6 LLM roles, or the 43 dashboard endpoints.
4. **A radical-design research dossier** (run in parallel as a subagent — appended in §9 once it returns).

---

## §0 · Where we are right now

| Item | State |
| - | - |
| Repo cloned | `Project-Doze/trading-bot/` (351 commits, `main @ 63ded54`) |
| Branch | `feature/v3-frontend` created off `main`, clean tree |
| Live dashboard reachable | ✅ `http://192.168.1.49:8081/ops` returns 200, full SPA renders |
| `/api/ops/*` endpoints reachable | ✅ all 31 endpoints sampled |
| Screenshots captured | 12 PNGs at `Project-Doze/v3-research-*.png` (full pages + viewport zones + theme variants + density variants + topbar detail) |
| Existing design tokens dumped | 47 CSS custom-property values captured from live system |
| Existing component count | 51 React components in `user_data/dashboard/static/js/ops_spa.js` |
| Existing themes | `control` (default · purple-tinted dark) · `geist` (clean dark) · `bloomberg` (pure black + orange) |
| Existing densities | `compact` · `default` · `roomy` |
| Existing decimal-index | Cards numbered `00 · 00b · 00c · 00d · 03 · 04 · 05 · 06 · 07 · 07a · 08 · 09 · 10 · 11 · 12 · 13 · 13c · 14 · 15 · 16 · 16b · 17 · 18 · 19 · 20 · 21 · 21a · 22 · 23` (30 cards on /ops) |
| Test baseline (per `TRADING_BOT_PROMPT.md`) | `pytest tests/` → 251 passed · 3 skipped · 254 collected |
| Kill switch already exists | ✅ Top-right `kill-wrap` with `<button class="kill-btn" aria-pressed="false" aria-label="Arm pause (then hold to confirm)">ARM</button>` — hold-to-confirm pattern |
| AgentLogsDrawer present | ✅ `.ald-backdrop` + `.ald-drawer` in DOM (Cmd-L / Cmd-K presumed wire-up) |
| Refresh cadence selector | ✅ Topbar has `5s / 10s / 30s / 1m / Off · ↻` — already operator-configurable |

So the existing dashboard is **not bad**. It's already React-on-CDN with Geist typography, a three-theme system, a hold-to-confirm kill switch, custom SVG icons, regime-tinted topbar, and 30 carefully-numbered decimal-indexed cards. The README's claim of *"plain JS SPA"* undersells the current state by a factor of three.

**The redesign is therefore a depth-and-density upgrade, not a rewrite.** Half the value of V3 is in *not* throwing away what works.

---

## §1 · Live audit findings

### 1.1 What's good and stays

| Existing pattern | Why it stays | V3 use |
| - | - | - |
| Decimal index on every card (`00`, `21a`, `13c`…) | Operator's mental map; lets you say "card 21a hung" in Slack | Keep, make more prominent — bigger tag, monospace, tnum |
| Three themes (`control` · `geist` · `bloomberg`) | Operator switches mid-session; muscle memory matters | Keep names + slots; restyle each to be more *distinct* (current geist looks like control + 5px) |
| Three densities (`compact` · `default` · `roomy`) | Single-screen vs multi-monitor | Keep; widen the gap so each is visibly different (currently scrollH is identical across densities — broken) |
| NumberRoll animation on topbar equity (per-digit slot) | Live-feel without being noisy | Keep, extend to scoreboard P&L + cumulative loss meter |
| Regime tint on `<header>` (`--regime-tint`) | Subliminal mood-cue: red topbar when `trending_down` | Keep, extend to scoreboard ribbon + page border-top bar |
| Kill switch with hold-to-confirm 1500ms | Industrial control safety | Keep; make it bigger, add radial sweep progress, add `KILL · ARM` two-stage commit |
| Refresh cadence selector (5s/10s/30s/1m/Off) | Operator-configurable polling | Keep, move to lower-right corner like Datadog |
| Hash-routed left rail with numeric hotkeys (`1`, `2`, `3`…) | Linear-style keyboard nav | Keep, add Cmd-K command palette as primary nav |
| Trade reviewer / risk debate logs in `llm_calls.jsonl` | Real signal for operator | Keep; surface the *content* prominently (today it's just a count) |
| Decimal-named cards covering 30 distinct operator questions | Comprehensive | Keep all 30; redesign each individually (§6) |
| Hold-to-confirm `.kill-btn` aria-pressed pattern | Accessible destructive action | Keep, add 2-3 more (resume, force-flatten, pause-on-loss) using same primitive |

### 1.2 What's flat / generic / fixable

The current design is **professional but homogeneous**. Every card is the same dark-surface rectangle at the same elevation with the same border treatment. The operator can scan the page but nothing *commands* attention.

Top-15 concrete problems:

1. **Cards don't pop.** All 30 cards sit on the same `--bg-card #0c0c10` plane with the same `--line-1 rgba(255,255,255,.05)` border. No elevation hierarchy. Hero scoreboard (card 00) looks identical to MCP tool console (card 21).
2. **No focal P&L.** The cumulative $-81.36 loss is a small font size near the top right. On a losing day this should *dominate* the viewport — Bloomberg uses 96px digits for the headline number.
3. **Refresh affordance is invisible.** "just now" appears in 11px dim text. The user explicitly said *"refresh buttons kill everything right"* — they want refresh to be a satisfying, prominent, physical-feeling thing.
4. **Kill switch is small.** The `KILL ARM` button is ~28px tall, top-right. For a destructive action that pauses live paper trading, this should be *bigger and more dramatic* — NASA-mission-control vibe, not "submit" button.
5. **Sparklines are commodified.** 12 little gray lines in a row. No identity. Bloomberg-style sparklines with last-price chip + delta arrow + tnum-aligned price would carry far more signal.
6. **Agent flow is a flat pipeline.** The 5 roles (regime_tagger → bull_debater → bear_debater → arbiter → reflector) are boxes in a row connected by `→` arrows. This is one of the project's signature features (multi-agent LLM debate). It deserves a *debate floor* layout (we designed this in `quanta-next/` last week) — bull on left, bear on right, arbiter in the middle with scales.
7. **LLM activity table is undersigned.** 10 rows with no row stripes, no per-call grade chip, no per-agent color tag. A "stop_hunt grade C" call should *look* different from a "100% success" arbiter call.
8. **Entry gates · why isn't anything trading?** Has the right title but the wrong layout — the 11 gates per pair are dot rows. The first-blocker is buried in a sidebar. The operator question is "WHICH pair, WHICH gate, WHY" — should be a sortable matrix with the blocker pulled to a separate column.
9. **Pair telemetry strip blends.** 12 crypto pairs + 15 stocks = 27 mini-charts at the same size, no hierarchy. The 4 with positions should be 2× bigger. The 12 blocked-on-regime should be muted.
10. **No hover-reveal.** Cards have no hover state worth speaking of. Modern dashboards (Linear, Stripe, Vercel) reward hover with subtle elevation, line darkening, and reveal of secondary controls (refresh / pin / expand).
11. **No "you're losing money right now" cue.** Day P&L is `-$81.36` — that's an `up`/`down` colored chip. There's no escalating visual treatment when the loss approaches the daily-loss-halt at 3% (currently $-81 / $19k = -0.43%; the halt fires at -3% = $-570). A radial dial showing "0.43% of 3.00%" would be a much sharper operator signal.
12. **Decision audit is text.** 5 most-recent decisions are listed as paragraph blocks. A swimlane timeline like Linear's project timeline or Datadog's trace flame graph would be order-of-magnitude more legible.
13. **Sentiment "aggregate" is a single number.** `net -0.30 BEARISH` — the underlying data has `deep`, `fast`, `fear_greed`, `agreement`, `headlines`. A 4-channel mini-radar or stacked-divergence chart would carry the actual signal.
14. **No Cmd-K command palette.** Left rail has hotkeys 1/2/3, but no global search / command palette. Linear and Raycast have set the bar here.
15. **Density `compact` doesn't actually compact.** Tested `data-density="compact"` — scrollHeight remained 8130px (identical to `default`). The toggle is wired in JS but the CSS isn't shipping the spacing deltas.

### 1.3 Live data reality (from sampled `/api/ops/*`)

| Endpoint | Status | Operator-meaningful payload |
| - | - | - |
| `/api/ops/services` | 8/8 up | All healthy (hermes_gateway, hermes_mcp, hermes_dashboard, ollama, freqtrade, postgres, influxdb, grafana) |
| `/api/ops/gates` | 12/13 crypto pairs **BLOCKED**, 1 stocks pair (SOFI) clear | First-blocker is overwhelmingly `regime=trending_down` (hard block) or `model_freshness=no model registered in pair_dictionary` |
| `/api/ops/llm_calls` | 10 calls in 24h, 100% success, all `hermes3:8b` via Ollama | `trade_reviewer` (5×) · `risk_debate.conservative` (2×) · `risk_debate.aggressive` (2×) · `risk_debate.neutral` (1×). p95 latency 19.81s, max 122.78s outlier |
| `/api/ops/weekly_training` | `degraded` · 0 of 6 LoRA tracks trained | Pipeline still spinning up; ModelForge reachable at `host.docker.internal:8000` |
| `/api/state` | Day P&L `-$81.36` · BTC regime `trending_down` · TFT `up=0.369` vs threshold 0.77 | Real losing day with no edge — predictions are below threshold, regime is blocking, sentiment is bearish |
| `/api/ops/risk_gates` | All 8 gates armed, none tripped | Operator-editable thresholds: `daily_loss_halt_pct=0.03`, `single_name_cap_pct=0.10`, `correlation_cap=0.85`, etc. |
| `/api/ops/sentiment` | `net=-0.30` BEARISH | Underlying: deep/fast/fear_greed/agreement/headlines/age all in payload |
| `/api/ops/circuit_breakers` | portfolio armed, 0 service open, 2 total | ARMED state — no breakers tripped today |
| `/api/ops/readiness` | mode `standard`, 18 trades, NOT READY | Validation gates: Sharpe, MaxDD, PF, etc. — 18 trades is too few for stats |
| `/api/ops/shark_briefing` | 4 phases logged | Today's regime `BEAR_VOLATILE` with macro/phase note |

**Headline operator narrative the redesign must surface:** *"Bot is healthy. Markets are trending down. TFT predictions are well below threshold across the board. 12 of 13 crypto pairs are hard-blocked on regime. The only thing trading is the SOFI wheel. We've lost $81 today, $94 yesterday, $73 day-before. ModelForge training pipeline hasn't fired its first cycle yet. No emergency, but no edge — and the operator should know in 5 seconds."*

### 1.4 Existing token system (extracted live)

```
--bg-page         #050507     (true black-ish)
--bg-card         #0c0c10     (card surface)
--bg-card-2       #111116     (raised surface)
--bg-inset        #16161d     (inset / well)
--bg-overlay      #1d1d26     (modal / drawer)
--bg-rail         #08080c     (left rail / topbar)

--fg-1            #f4f4f6     (primary text)
--fg-2/3/4        unset in :root, set in [data-theme=…] blocks

--up              #2ec27e     (vibrant green)
--up-bg           rgba(46,194,126,.10)
--up-line         rgba(46,194,126,.35)

--down            #f04437     (saturated red)
--down-bg         rgba(240,68,55,.10)
--down-line       rgba(240,68,55,.4)

--warn            #f5a623     (amber)
--accent          #7c5cff     (purple)

--sans            'Geist', 'Inter', system-ui, sans-serif
--mono            'Geist Mono', 'JetBrains Mono', 'IBM Plex Mono', 'Menlo', monospace

type scale        10 · 11 · 12 · 13 · 14 · 16 · 20 · 28 · 40 · 64 · 96 px
spacing scale     4 · 8 · 12 · 16 · 20 · 24 · 32 · 40 · 48 px (4px grid)
radius            2 · 4 · 6 px (control-room — small)
duration          80ms · 120ms · 200ms
```

Verdict: **the token system is already strong.** V3 keeps every existing token name (frozen surface per `TRADING_BOT_PROMPT.md` §B.3), *adds* new tokens for depth/elevation/motion, and *tightens* the values that are currently loose.

---

## §2 · Diagnosis — why current design ≠ radical

A radical operator console has three things the current dashboard doesn't:

### 2.1 Depth — z-axis hierarchy

Right now everything is on the same plane. There is no "above the fold" feeling, no card that *floats*, no sense that the hero numbers are on a different layer than the supporting telemetry. Compare to:

- **Bloomberg Terminal** — uses inset wells, framed sections, and the orange-on-black palette to create a clear z-stack: command line at top, primary data in the middle frame, secondary context in side panes.
- **Linear** — primary content sits on `bg-base`, secondary on `bg-elevated`, modals on `bg-floating`. Each has a distinct shadow + border treatment.
- **Stripe Dashboard** — uses crisp single-pixel borders + soft elevation shadows to create a clear "card raises on hover" affordance.

V3 introduces a five-level z-stack:

```
z0  page         --bg-page  #050507    (canvas)
z1  card         --bg-card  #0c0c10    (default card surface)
z2  card-raised  --bg-card-2 #111116   (hero scoreboard, kill bar, active card)
z3  inset        --bg-inset #16161d    (input fields, table headers, code blocks)
z4  hover        gradient overlay      (card-on-hover, button-on-hover)
z5  overlay      --bg-overlay #1d1d26  (modal, drawer, command palette)
```

### 2.2 Signature — recognizable visual moves

Right now the dashboard reads as "competent dark dashboard". You'd guess Datadog or Grafana within two seconds and they'd both be wrong. The V3 should look like **only one thing on the internet**.

The signature moves (full list in §5):
1. **The DD Ribbon** — a horizontal gradient bar across the top of the scoreboard showing 0% → daily-loss-halt → kill, with a vertical needle on the current value (NASA throttle aesthetic).
2. **The Debate Floor** — agent flow rendered as bull-vs-bear courtroom with the arbiter scales in the middle, *not* a horizontal pipeline.
3. **The Gates Matrix** — 13 crypto pairs × 11 gates as a color-mapped grid (column-headers vertical, like a periodic-table chart), with the first-blocker pinned to a separate "WHY" column on the right.
4. **The Kill Bar** — bottom-pinned, full-width, drawer-style, that flares red and exposes `KILL · FLATTEN · PAUSE · RESUME` only on hover-or-Cmd-Shift-K. Industrial control room aesthetic.
5. **The Heartbeat** — a single pulsing dot in the top-left that turns red when any service is down, amber on degraded, green on healthy. Always-on, never away from the operator's peripheral vision.
6. **The Sparkline Strip** — 12 crypto pairs as Bloomberg-style mini-tickers (sym, px, Δ, ▲▼ arrow, sparkline). Positions get a 2× height. Hard-blocked pairs get a hairline strikethrough.
7. **Cmd-K command palette** — Linear/Raycast pattern. Type "pause", "kill", "BTC", "go to gates", "set risk daily-halt to 2.5%". Becomes the primary verb-input.

### 2.3 Motion — purposeful, not decorative

Right now the only motion is the topbar NumberRoll (good) and… that's it. V3 adds:

- **Refresh as physical action.** The "↻" button compresses on press, the cadence pill shows a swept progress ring, and the rotated icon springs back. 200ms ease-out, hardware-feel. Like Raycast's command launch animation.
- **Card-on-hover lift.** 4px translateY + soft shadow on cards; 80ms in, 120ms out. Communicates "this is interactive" without being noisy.
- **Hold-to-confirm sweep.** Kill button gets a radial-conic-gradient progress sweep that completes the circle at 1500ms; release before completion cancels. Visible commitment.
- **NumberRoll on day P&L** (in addition to equity). Per-digit slot machine; flashes green or red briefly when the digit changes.
- **Sparkline trailing fade.** The last 10% of every sparkline is rendered at 40% opacity with a 1px dashed segment for "still-forming bar." Datadog-style.
- **Regime crossfade.** When `--regime-tint` changes (e.g., trending_down → mean_reverting), the topbar tint fades through 600ms with a flash of brand-purple at the midpoint.

---

## §3 · Design thesis (one sentence)

> **V3 turns the dashboard from "a thing that shows numbers" into a control surface that physically feels like a trading desk — depth, gravitas, kill-everything-now-prominent, recognizable on sight from across the room.**

Reference family anchors:
1. **Bloomberg Terminal** — gravitas, density, mono+sans pair, the color logic
2. **Linear** — typography, command palette, keyboard-first, micro-motion
3. **Stripe** — financial-data clarity, tabular numbers, hover affordances
4. **Vercel Geist** — the actual font + the "premium minimal" hover-state language
5. **NASA / SCADA** — kill switch, system-health heartbeat, big readable headlines on emergencies

Anti-anchors (we will not look like):
- Material Design / Tailwind UI / generic SaaS card layouts
- Glassmorphism (no `backdrop-filter`)
- Neumorphism (no soft pillow shadows)
- Rainbow gradient hero numbers
- "AI-style" floating particles or animated background grids

---

## §4 · Design system upgrade

All existing token names are preserved (frozen surface — see `TRADING_BOT_PROMPT.md` §B.3). The upgrade is *additive*.

### 4.1 New tokens (added at the bottom of `user_data/dashboard/static/css/quanta.css`)

```css
/* Elevation — V3 adds explicit z-stack */
--z-card-raised:   0 1px 0 rgba(255,255,255,.04) inset, 0 8px 24px -12px rgba(0,0,0,.6);
--z-card-hover:    0 1px 0 rgba(255,255,255,.06) inset, 0 12px 32px -10px rgba(0,0,0,.7);
--z-overlay:       0 1px 0 rgba(255,255,255,.08) inset, 0 24px 64px -16px rgba(0,0,0,.85);
--z-flare-danger:  0 0 0 1px var(--down-line), 0 0 32px -8px rgba(240,68,55,.4);
--z-flare-warn:    0 0 0 1px var(--warn-line), 0 0 32px -8px rgba(245,166,35,.35);

/* Hairlines — V3 makes borders crisper */
--hairline:        0.5px solid rgba(255,255,255,.06);    /* default card border */
--hairline-strong: 1px solid rgba(255,255,255,.10);      /* divider */
--hairline-bold:   1px solid rgba(255,255,255,.18);      /* active card edge */

/* Mono ligatures + tabular numbers — applied to .mono and .num */
--mono-features:    'tnum' 1, 'zero' 1, 'cv11' 1, 'ss01' 1;

/* Motion — V3 adds spring + sweep durations */
--dur-sweep:        1500ms;   /* hold-to-confirm */
--dur-flare:        600ms;    /* regime crossfade */
--dur-spring:       240ms;
--ease-spring:      cubic-bezier(.34, 1.56, .64, 1);  /* slight overshoot */
--ease-decel:       cubic-bezier(.05, .8, .25, 1);

/* Heat-map ramp — V3 adds for the gates matrix */
--heat-0:           #0c0c10;   /* null / unknown */
--heat-1:           #1a3a2a;   /* pass-disabled */
--heat-2:           #2ec27e;   /* pass */
--heat-3:           #5a3a1a;   /* warn */
--heat-4:           #f5a623;   /* warn-bright */
--heat-5:           #4a1818;   /* fail-soft */
--heat-6:           #f04437;   /* fail-hard */

/* Status pulses — V3 adds for the heartbeat dot */
--pulse-up:         green 0% 60%, transparent 60% 100%;
--pulse-warn:       amber 0% 50%, transparent 50% 100%;
--pulse-down:       red 0% 70%, transparent 70% 100%;
```

### 4.2 Typography refinements

| What | Now | V3 | Why |
| - | - | - | - |
| Default body | Geist 13/1.5 | Geist 13/1.45 + `font-feature-settings: 'cv11'` | Cleaner `1` / `l` distinction in mono context |
| Numbers everywhere | `font-family: var(--mono)` | + `font-variant-numeric: tabular-nums slashed-zero` | Digits line up across rows; zeros visibly differ from `O` |
| Headline P&L | 28-40px Geist Mono | 64-96px Geist Mono, `letter-spacing: -0.025em`, weight 400 | Bloomberg-scale gravitas on the one number that matters |
| Card titles | 14px Geist | 13px Geist 600 + 1px tracking | Linear-style decisive labels |
| Decimal index | 11px in card head | 13px Geist Mono `bg-inset` chip + 0.5px hairline | More badge-like; readable from across the room |
| Pill labels | 10px uppercase | 10px uppercase + `letter-spacing: 0.12em` + tabular | Bloomberg pill discipline |

### 4.3 Color system additions

The existing `--up/--down/--warn/--accent` stay. Additions:

```css
--up-2:    #1a9c5a;    /* deeper green for elevated up state (e.g., champion adapter promoted) */
--down-2:  #c8342a;    /* deeper red for emergency / kill state */
--electric-yellow: #f5e63f;   /* one signature color — used ONLY for: in-debate-now indicator, refresh-in-flight ring */
--cold-blue:       #4dc4ff;   /* one signature color — used ONLY for: WebSocket live-stream indicator, AI-thinking pulse */
```

Total palette: **8 functional colors + 5 surface tones + 2 signature accents = 15 colors total**. Discipline. Compare to Bloomberg Terminal which uses ~7 colors and Linear which uses ~9.

### 4.4 Density actually working

Bug to fix: `data-density="compact"` doesn't change `scrollHeight` (verified empirically — same 8130px). V3 ships real spacing deltas:

```css
[data-density="compact"] {
  --s-2: 6px;  --s-3: 8px;  --s-4: 12px;  --s-5: 16px;  --s-6: 18px;
  --t-base: 12px;  --t-md: 13px;
  --card-pad-y: 10px;  --card-pad-x: 14px;
}
[data-density="default"] { /* tokens unchanged */ }
[data-density="roomy"] {
  --s-2: 12px;  --s-3: 18px;  --s-4: 24px;  --s-5: 30px;  --s-6: 36px;
  --t-base: 14px;  --t-md: 15px;
  --card-pad-y: 20px;  --card-pad-x: 24px;
}
```

Target: compact → 6500px scrollH, default → 8130px (unchanged), roomy → 10000px scrollH.

---

## §5 · Seven signature moves

The radical, prominent, can't-confuse-this-with-anything-else moves.

### 5.1 The DD Ribbon (headline drawdown / loss telemetry)

A horizontal gradient bar across the top of card `00 Today · scoreboard`, **600px wide × 12px tall**, showing four zones:

```
[ safe 0% ............. pause 1.5% ............ halt 3% .... kill 8% ]
       ↑ current 0.43%
```

- Linear gradient through `--up → --warn → --down → --down-2`.
- A 2px vertical needle at the current value with a 14px Geist Mono label above it (`-0.43%`).
- The needle is sticky to the actual `daily_pnl / scoreboard.capital * 100` value.
- When the needle crosses the pause zone, the ribbon flares orange (`var(--z-flare-warn)`).
- When the needle crosses halt, the ribbon flares red (`var(--z-flare-danger)`) and the kill bar at the bottom of the page expands automatically.
- Inspiration: NASA throttle gauges, Tesla regen-paddle visualization.

### 5.2 The Debate Floor (agent flow card `21a`)

The current "Agent flow" card renders `regime_tagger → bull_debater → bear_debater → arbiter → reflector` as a flat horizontal pipeline. V3 reframes it as a **debate**:

```
                ┌─────────────────┐
                │  REGIME TAGGER  │   (scout — top of arena)
                └─────────┬───────┘
                          │
        ┌─────────────────┴─────────────────┐
        │                                   │
   ┌────▼────┐      ┌─────────────┐    ┌────▼────┐
   │  BULL   │ ←──→ │  ARBITER ⚖  │ ←─→│  BEAR   │
   │ assesses│      │   scales    │    │assesses │
   │ upside  │      │             │    │downside │
   └────┬────┘      └──────┬──────┘    └────┬────┘
        │                  │                 │
        └──────────────────┼─────────────────┘
                           ▼
                ┌──────────────────┐
                │     REFLECTOR    │   (post-mortem writer)
                └──────────────────┘
```

- Each agent is a card with: name · model (`hermes3:8b` chip) · last call time · last response gist (truncated to 90 chars).
- Bull glows `--up`, bear glows `--down`, arbiter glows `--accent`, regime-tagger glows `--cold-blue`, reflector glows `--warn`.
- A horizontal "DEBATE LIVE" pulsing pill in the center of the floor when an inference is mid-flight.
- Click any agent → drawer opens with its last 10 responses (uses existing `AgentLogsDrawer`).
- Inspiration: courtroom diagrams, our own quanta-next/ prototype, Tldraw asymmetric layouts.

### 5.3 The Gates Matrix (card `05 entry gates`)

13 crypto pairs × 11 gates = 143 cells, rendered as a **periodic-table-style heat map**:

| pair    | cap | mdl | pred | vol | rgm | up≥0.77 | tft≥0.4 | hvol | meta_up | meta_c | open |
| ------- | --- | --- | ---- | --- | --- | ------- | ------- | ---- | ------- | ------ | ---- |
| BTC/USD | ●   | ●   | ○    | ○   | ✗   | ✗       | ✗       | ●    | ●       | ●      | ●    |
| ETH/USD | ●   | ●   | ○    | ○   | ✗   | ✗       | ●       | ●    | ✗       | ●      | ●    |
| …       | …   | …   | …    | …   | …   | …       | …       | …    | …       | …      | …    |

- Cells colored via the heat ramp: `--heat-2` (pass), `--heat-6` (fail), `--heat-0` (null), `--heat-1` (pass-disabled).
- Column headers rotated 90° to save horizontal real estate, with hover-tooltip giving the full gate description.
- Each row ends with a "WHY" column showing `first_blocker` + a 2-second hover-expand showing the full `gates[].detail` text.
- Rows sortable by `n_blocking` desc, with the next-eligible pair pinned to the top.
- Inspiration: Datadog heat-map dashboards, periodic-table of elements.

### 5.4 The Kill Bar (bottom-pinned drawer)

A 48px-tall bar pinned to the bottom of the page, only visible **on hover within the bottom 80px** OR via `Cmd-Shift-K`. When open:

```
┌──────────────────────────────────────────────────────────────────────────┐
│ ⚠ DESTRUCTIVE                  hold to confirm · 1500ms                  │
│ [ PAUSE all entries ]  [ FLATTEN open positions ]  [ KILL everything ]   │
│ [ RESUME after manual review ]                                          │
└──────────────────────────────────────────────────────────────────────────┘
```

- Each button is a hold-to-confirm with a radial-conic-gradient sweep progress.
- When sweep completes, the button fires `POST /api/ops/pause` (or equivalent), Slack pings the operator, and the bar shows a 3s receipt of what happened.
- Default state is collapsed — the bar shows only a 4px-tall danger-tinted strip at the very bottom of the page with no text. Inspiration: industrial fire-suppression panels (always there, only obvious when you reach for them).

### 5.5 The Heartbeat (always-on system health dot)

A 12px dot in the top-left corner, *above* the brand mark. Always-on. Pulse colors:

- Green pulse @ 1.5Hz when `services.up_count == services.total` AND `circuit_breakers.armed == true` AND `freqtrade.state == 'running'` AND `mode == 'paper' | 'live'`.
- Amber pulse @ 2Hz when any service degraded (e.g., `ollama latency p95 > 30s`, or `weekly_training.status == 'degraded'`).
- Red pulse @ 3Hz when any service down OR breaker active OR daily-loss-halt fired.
- Subtle. 12px is small. But it's the operator's peripheral-vision system status.
- Clicking opens the existing `card 07a Service health` drawer.
- Inspiration: Datadog Watchdog, Vercel deploy status indicator, OS-level network indicator.

### 5.6 The Sparkline Strip (card `06 pair telemetry`)

Replace the 12 commodity sparklines with **Bloomberg-style mini-tickers**:

```
BTC/USD   80,383.21  ▼ -1.21%   ╱╲╲╱╲╲╲    ◇ regime: trending_down
ETH/USD    2,259.44  ▲ +0.18%   ╲╱╱╲╱╲╱    ● position open · ETH long
SOL/USD       97.75  ▼ -0.84%   ╲╲╲╱╲╲╲    ◇ blocked: regime
...
```

- Each row is 32px tall (compact density 24px, roomy 40px).
- The sparkline is 200px wide, deterministic-seeded (existing pattern in `qc_react.js`).
- Pairs with open positions get a green left-edge accent bar 3px wide + 2× height.
- Blocked pairs get the gate-block reason as a chip on the right.
- Sortable by Δ%, by regime, by position-status.
- Inspiration: Bloomberg Terminal ticker rows, Hyperliquid market browser.

### 5.7 The Cmd-K Command Palette

Global keyboard shortcut. Opens a centered, 600px-wide overlay with a search input + ranked actions:

```
┌──────────────────────────────────────────────────┐
│ 🔍  type to search · ↑↓ navigate · ↵ run         │
├──────────────────────────────────────────────────┤
│ pause                                            │
│   ▸ Pause all entries (POST /api/ops/pause)     │
│ flat                                             │
│   ▸ Flatten all open positions                   │
│ btc                                              │
│   ▸ Go to BTC/USD pair view                      │
│   ▸ Show BTC/USD gates                           │
│ risk daily 2.5                                   │
│   ▸ Set risk_gates.daily_loss_halt_pct = 0.025  │
│ theme bloomberg                                  │
│   ▸ Switch theme to Bloomberg                    │
└──────────────────────────────────────────────────┘
```

- Fuzzy match across: every card title, every pair, every `risk_gates` key, every theme + density, every documented action.
- State-mutating actions require `X-Hermes-MCP-Key` header — the palette prompts for the key on first state-mutating use per session and caches it in memory.
- Inspiration: Linear, Raycast, GitHub Cmd-K.

---

## §6 · Card-by-card redesign brief (all 30 cards)

For each card: current state · operator question · V3 treatment · effort estimate (S/M/L).

### Group A — Hero (cards 00, 00b, 00c, 00d)

| # | Card | Current | V3 |
| - | - | - | - |
| 00 | Today · scoreboard | Single 110px row · 4 numbers in a line · `live · realized + unrealized · refreshes every 10s` | Expand to 200px hero. Cumulative day P&L at 64-96px center-stage. DD ribbon (§5.1) above. Three flanking stats: capital · realized today · unrealized. NumberRoll on the big number. Regime tint on the bottom-edge underline. **L** |
| 00b | Shark BEAR_VOLATILE override health | 132px row · verifier · cron 09:45 ET | Compact 96px row with traffic-light + last-verifier-timestamp + tap-to-expand. **S** |
| 00c | Weekly training · LoRA adapters | 395px tall · 6 tracks listed | Re-layout as a 2×3 grid of track cards. Each card: track name + headline metric + sparkline of last 4 weeks + current adapter chip. Highlight any track that promoted this week. Make `next training: Sun 02:00 ET` a count-down chip. **M** |
| 00d | TFT model health · per pair | 538px tall · per-pair list | Two-column compact layout: 12 crypto pair rows + 15 stock pair rows. Each row: pair · model age · stub/healthy chip · expand-to-show last validation. Auto-collapse healthy rows. **M** |

### Group B — Agent stack (cards 21a, 21, 03, 04)

| # | Card | Current | V3 |
| - | - | - | - |
| 21a | Agent flow | 222px · flat 5-stage pipeline | **Debate Floor (§5.2)** — courtroom layout, bull/bear/arbiter/regime/reflector with role tints, live-debate pulsing pill, click-to-drawer. **L** |
| 21 | LLM activity · last 24h | 494px · filterable table | Stripe-style stripe rows + per-call grade chip (A/B/C from `arbiter.last_gist`) + agent-role color tag in the left column + Cmd-F focus on search. Add filter pills above the table (`fast 5 · deep 5 · ollama 10 · anthropic 0`). **M** |
| 03 | Agent timeline · 24h | 253px · ribbon timeline | Swimlane: 5 swimlanes (RESEARCH · ML · EVO · RISK · REPORT), tick marks for each cron firing, hover shows last-output gist. **M** |
| 04 | Research stream · how the agent thinks | 494px · synthesises 6 endpoints | Editorial-feel: 2-line headlines + source chip + age + click-to-expand. NYT-Upshot-style with monospace timestamps. **M** |

### Group C — Pair view (cards 05, 06, 23)

| # | Card | Current | V3 |
| - | - | - | - |
| 05 | Entry gates · why isn't anything trading? | 552px · sortable rows | **Gates Matrix (§5.3)** — periodic-table heat map. **L** |
| 06 | Pair telemetry · 5m closes · trailing 24h | 401px · 12 sparklines | **Sparkline Strip (§5.6)** — Bloomberg ticker rows. **M** |
| 23 | Stocks pair telemetry · 5Min · session window | 515px · 15 sparklines | Same treatment as 06. **S** (template reuse) |

### Group D — Risk + breakers (cards 13, 13c, 15, 16, 16b, 18, 19)

| # | Card | Current | V3 |
| - | - | - | - |
| 13 | Sentiment aggregate | 200px · single number | **4-channel mini-radar:** deep / fast / fear-greed / agreement. Net score in the center 28px. Headlines as a single rolling chip. **M** |
| 13c | Shark briefing · 2026-05-12 | 257px · 4 phases | Phase pills (1→4) at the top, current phase highlighted with `--regime-tint`. Phase content below in editorial typography. **S** |
| 15 | Trades & risk · 24h | 247px · 0/6 open | Tape view: each closed trade as a row with entry-time · pair · exit-reason chip · pnl bar (proportional to magnitude, color by sign). Bloomberg-tape aesthetic. **M** |
| 16 | Circuit breakers | 277px · armed status | 8 dots in a row, one per gate. Green = armed/safe, amber = approaching threshold, red = tripped. Hover any dot → tooltip with current value + threshold. **S** |
| 16b | Backtest quality gates | 133px · no reports yet | Empty-state with NEXT-RUN countdown. When data arrives: 7-checkpoint waterfall (Sharpe, MaxDD, PF, etc.) with pass/fail dots. **S** |
| 18 | Readiness · validation gate matrix | 306px · NOT READY | Compact 7-row matrix: gate · current · threshold · direction · pass/fail. Highlight first failing gate. **M** |
| 19 | Regime config editor | 485px · 5 deltas + scalars | Live form. Sliders for percentage values, dropdowns for regime keys. Submit-on-blur with optimistic UI + receipt toast. **M** |

### Group E — Operations (cards 07, 07a, 08, 09, 10, 11, 12, 14, 17, 22)

| # | Card | Current | V3 |
| - | - | - | - |
| 07 | LLM providers · Ollama primary · Anthropic fallback | 169px · cost-saved | 2-tile: Ollama (calls 81 · saved $X) + Anthropic (status: DISABLED). Saved-$ counter NumberRolls. **S** |
| 07a | Service health · 8 probes | 326px · 8/8 up | Compact: heartbeat dot (§5.5) header + collapsed list of 8 probes (expand on click). **S** |
| 08 | Open positions | 137px · 0 active | Empty-state with last-trade time chip. When positions exist: row per position with live P&L NumberRoll. **S** |
| 09 | Stocks · Shark TFT | 303px · weights present | Compact: 1 line per stock TFT model with age + alpha chip. **S** |
| 10 | Stocks · Wheel + Shark | 234px · paper · NYSE OPEN | Two-stat: open contracts count + total premium collected. Market hours strip below (`NYSE OPEN 09:30-16:00 ET · 6h 5m remaining`). **S** |
| 11 | MCP · wire status | 178px · Hermes MCP reachable | Single big OK/DEGRADED/DOWN pill + last successful call timestamp. **S** |
| 12 | Quick actions · control panel | 239px · 4-6 buttons | Becomes the source-of-truth for control buttons. Each is a hold-to-confirm. Removes redundancy with the new Kill Bar (§5.4). **M** |
| 14 | EPT · champion genome | 240px · refresh 60s | DEPRECATED CARD per `TRADING_BOT_PROMPT.md` §B.5 (EPT retired 2026-05-12). V3 replaces with `ModelForge champion adapter` card — adapter name + version + age + promotion timestamp. **M** |
| 17 | Training · FreqAI / TFT retrain status | 213px · training XRP · epoch null | Progress bar + epoch counter + ETA. Glows `--cold-blue` when training is live. **S** |
| 22 | Decision audit | 396px · last 5 decisions for BTC | Vertical swimlane timeline. Each decision is a card with regime · TFT prediction · gates passed/blocked · final action chip. **M** |

### Group F — Comms + tools (cards 20, 21)

| # | Card | Current | V3 |
| - | - | - | - |
| 20 | Slack preview · next daily brief | 229px · fires 00:00 UTC | A Slack-style message bubble preview with the actual Hermes-formatted text. Countdown to send. **S** |
| 21 (MCP tool console) | 235px · 19 tools | Cmd-K-style searchable list. State-mutating tools (`❗`-prefixed) get a danger badge and require key prompt. **M** |

### Effort total: 7 × L, 14 × M, 9 × S → roughly 60-90 dev-hours across 30 cards.

---

## §7 · Multi-agent execution plan

The operator wants "multi-agent" execution. Here is the parallelization strategy.

### 7.1 Agent roster (subagent types)

| Agent | Subagent type | Scope | Why this one |
| - | - | - | - |
| **Design Lead** | the parent (me) | Plan + coordination + final review + commit | Holds the full mental model |
| **Token Smith** | generalPurpose | Adds new tokens to `quanta.css`, doesn't change existing tokens, ships density-actually-working bug fix | Pure CSS; small isolated diff |
| **Topbar + Hero** | generalPurpose | Cards 00 + 00b + DD Ribbon + Heartbeat + new Kill Bar component | Cohesive hero strip |
| **Debate Floor Architect** | generalPurpose | Card 21a — agent flow → debate floor | Self-contained signature move |
| **Gates Matrix Architect** | generalPurpose | Card 05 — gates table → heat-map matrix | Self-contained signature move |
| **Sparkline Strip Architect** | generalPurpose | Cards 06 + 23 — pair telemetry → ticker strip | Two cards, shared template |
| **LLM Stack Refiner** | generalPurpose | Cards 21 + 03 + 04 — LLM activity, agent timeline, research stream | LLM-domain expertise alignment |
| **Risk + Breakers Refiner** | generalPurpose | Cards 13 + 13c + 15 + 16 + 16b + 18 + 19 — sentiment, briefing, trades, breakers, readiness, regime config | Risk-domain alignment |
| **Ops Card Refiner** | generalPurpose | Cards 07 + 07a + 08 + 09 + 10 + 11 + 12 + 14 + 17 + 22 — service health, positions, stocks, MCP, training, decisions | High-volume but each is small |
| **Cmd-K Builder** | generalPurpose | The command palette overlay + keyboard wiring | Self-contained feature |
| **Comms Refiner** | generalPurpose | Cards 20 + 21 (MCP tool console) + Slack-preview · plus the Cmd-K integration | Tool-surface integration |
| **QA Verifier** | code-reviewer | Reads every diff, runs the §8 acceptance checklist, blocks merge | Final gate before commit |

12 subagents. ~30-90 hours of agent work → ~1 wall-clock day if 8 of them run in parallel, ~2-3 days if serialized for safety.

### 7.2 Parallel work-streams

```
                      ┌─────────────────────────────────────────────────┐
                      │  Wave 0 (serial)  ·  Token Smith                │
                      │  Adds new tokens + fixes density bug + commits  │
                      └────────────────────────┬────────────────────────┘
                                               │
            ┌────────────┬─────────────┬───────┴──────┬──────────────┬────────────┐
            │            │             │              │              │            │
            ▼            ▼             ▼              ▼              ▼            ▼
   ┌────────────┐┌─────────────┐┌──────────────┐┌─────────────┐┌────────────┐┌──────────┐
   │  Wave 1A   ││   Wave 1B   ││   Wave 1C    ││  Wave 1D    ││  Wave 1E   ││ Wave 1F  │
   │  Hero +    ││   Debate    ││   Gates      ││  Sparkline  ││  Cmd-K     ││  Comms   │
   │  Kill Bar  ││   Floor     ││   Matrix     ││  Strip      ││  Palette   ││  Refiner │
   └─────┬──────┘└──────┬──────┘└──────┬───────┘└──────┬──────┘└─────┬──────┘└─────┬────┘
         │              │              │               │              │             │
         └──────────────┴──────────────┴───────────────┴──────────────┴─────────────┘
                                       │
                                       ▼
                      ┌─────────────────────────────────────┐
                      │  Wave 2 (parallel · low-conflict)   │
                      │  LLM Stack · Risk · Ops Refiners    │
                      └────────────────────┬────────────────┘
                                           │
                                           ▼
                      ┌─────────────────────────────────────┐
                      │  Wave 3 (serial) · QA Verifier      │
                      │  Run §8 checklist · gate the merge  │
                      └─────────────────────────────────────┘
```

Wave 0 must land first (tokens) because every other agent reads them. Waves 1A-1F are *parallel* — they touch different cards. Wave 2 is parallel but lower-stakes. Wave 3 is the gate.

### 7.3 Conflict policy

Each agent owns specific files. The conflict matrix:

| File | Owner |
| - | - |
| `static/css/quanta.css` (new tokens at bottom) | Token Smith only in Wave 0; other agents append per-card class blocks |
| `static/js/qc_react.js` (shared primitives) | Hero+KillBar agent for new primitives (DDRibbon, HeartbeatDot, KillBar); others import only |
| `static/js/components.js` | Cmd-K Builder for the palette; others import only |
| `static/js/ops_spa.js` | Each card agent owns their card's function; no overlap |
| `static/js/dashboard_spa.js` | Hero+KillBar for top-strip parity; others read-only |
| `templates/ops_spa.html` | Token Smith bumps `?v=` cache-buster in Wave 0; no one else touches |
| `user_data/dashboard/ops_routes.py` | Read-only for all redesign agents — *no backend changes in V3* |
| `user_data/config.json` | Read-only for all redesign agents |

### 7.4 Subagent prompt template

Each card-level subagent gets a self-contained brief that includes:
1. The relevant section of this plan (§6 row for their card)
2. The relevant API endpoint payload (`Project-Doze/api-samples/<endpoint>.json`)
3. The relevant existing JS function (line range in `ops_spa.js`)
4. The relevant existing CSS class (or instruction to add new)
5. **Hard rules:** preserve `_envelope()` shape, no `require_mcp_key` regression, no token rename, no test regression, exact data flow from `useOpsData()` hook
6. A self-test script: `pytest tests/test_no_legacy_color_tokens.py && pytest tests/test_dashboard.py && pytest tests/test_ops_dashboard.py`

---

## §8 · Acceptance checklist — V3 ready to merge

A subagent declares its work done; the QA Verifier runs:

1. **All 30 cards render** at default density / control theme on a desktop 1920×1200 viewport. No console errors.
2. **All 3 themes work.** Switching `data-theme` between `control` / `geist` / `bloomberg` reflows without flicker. `bloomberg` theme reads orange-on-black; `geist` is cleaner / less saturated than `control`.
3. **All 3 densities ACTUALLY change layout.** `data-density="compact"` reduces scrollH by >15%; `roomy` increases by >15%. (Currently broken — verified empirically.)
4. **DD Ribbon needle moves with `daily_pnl`.** Test: mock `state.daily_pnl = -570` → ribbon should flare red and Kill Bar should auto-expand.
5. **Heartbeat dot turns red when any service drops.** Test: kill `ollama` probe → dot pulses red within 10s.
6. **Kill Bar requires 1500ms hold to fire.** Test: short-press releases before sweep completes; long-press fires POST and shows receipt toast.
7. **Cmd-K opens.** Test: anywhere on the page, Cmd-K opens the palette; Esc closes it.
8. **Debate floor renders bull-vs-bear-vs-arbiter.** Test: pre-recorded `llm_calls.json` fixture → page shows the 5 role cards with last-call gists.
9. **Gates Matrix shows 13 crypto rows + 1 stocks row** with column heat colors. First-blocker column populated.
10. **Sparkline strip uses live data from `/api/ops/sparklines`.** Test: each pair has a deterministic sparkline (seeded by symbol — existing pattern).
11. **`pytest tests/` is ≥251 passing (baseline maintained).** No test regression.
12. **No `.innerHTML` regression.** `git diff main feature/v3-frontend -- user_data/dashboard/static/` returns zero `innerHTML` assignments.
13. **No frozen schema rename** per `TRADING_BOT_PROMPT.md` §B (data keys, helper names, CSS tokens, routes, role names, endpoints).
14. **`?v=N` bumped** in `templates/ops_spa.html` and `templates/dashboard_spa.html`.
15. **Docker compose still comes up.** `docker compose up -d dashboard && curl -s http://localhost:8081/api/mode` returns `{"mode":"paper","state":"running","dry_run":true}`.
16. **Hard refresh works.** No FOUC, no theme flash, no console errors.
17. **Operator decisions §B.0 intact.** No added paid-LLM call. No raw operator data sent off-box.

---

## §9 · References — radical-design research dossier

> Compiled by parallel research subagent · 17 references with fetched URLs · 27 named design moves · 10 anti-patterns · 5 operator-decision questions. Every URL listed below was actually fetched in this session unless explicitly noted as 403/timeout.

### 9.0 Executive summary (research thesis)

**Thesis paragraph 1.** The strongest "operator console with gravitas" direction for Quanta is a **deliberate hybrid**: keep **Bloomberg-class signal-to-noise** (amber/black semantics, keyboard-first command grammar, multi-pane discipline) as the *spine*, but skin it with **2024-2026 product-engineering chrome** from Linear + Geist (LCH theming, layered surfaces, typed destructive gates, status dots, command surfaces) and **observability-native layout** from Datadog/Grafana (grid density modes, per-widget time windows, refresh semantics that cancel in-flight queries). That combination reads as "serious money + serious software," not "generic SaaS cards."

**Thesis paragraph 2.** Anchor the redesign on **five families**: (1) **Bloomberg Terminal + Bloomberg design ethos** for color logic, keyboard affordances, "everything visible is reachable"; (2) **Linear** for density without chaos, sidebar recession, softened separators, LCH theme generation, agent-era evolution; (3) **Vercel Geist** for *named* materials, typography scales, destructive modals, spinner/loading primitives; (4) **Datadog + Grafana** for operator-grade dashboards (high-density layouts, grouping, refresh cadence, drill-down discipline); (5) **Industrial HMI + Raycast** for "hardware truth" around kill/refresh. Use Reuters Graphics + FT chart-forward articles and TradingView's Supercharts feature set for trading-native density patterns. Award galleries (Awwwards, siteInspire) are useful for moodboard divergence, not engineering truth.

### 9.1 Reference families (17, ordered by load-bearing weight)

#### 1 · Bloomberg Terminal + Bloomberg design ethos

The flagship Bloomberg Professional workstation: proprietary GUI, custom keyboard, iconic dark UI, real-time market data. The cultural definition of "trading floor UI": **function over form**, **high-contrast encoding**, **muscle-memory navigation** — exactly the gravitas baseline Quanta's "Bloomberg" theme should inherit without cosplaying every clutter mistake.

Concrete techniques:
- **Amber-on-black as default text plane** (not neutral gray body copy) to cut through noise.
- **Color-coded keyboard semantics mapped to risk**: Esc as red "Cancel", Enter as green "GO", yellow sector hotkeys (F2 GOVT, F3 CORP) as *affordance memory*.
- **Multi-display discipline**: 2-6 displays typical; hardware story is part of the workflow model.
- **"Show the power" UX philosophy**: put functionality at fingertips even if first glance looks "busy."

URLs fetched: [Bloomberg design ethos interview](https://www.bloomberg.com/company/stories/bloombergs-customer-centric-design-ethos/) · [Wikipedia: Bloomberg Terminal](https://en.wikipedia.org/wiki/Bloomberg_Terminal)

#### 2 · Bloomberg.com (Bloomberg web)

Translation of "terminal seriousness" into **web-native scanning**: repeated module types, strong headline hierarchy, constant market framing.

Concrete techniques:
- **Ribbon-like "markets wrap" entry points** → Quanta's "session narrative" strip (what changed since last refresh).
- **Modular cards as story containers** (not abstract KPI tiles): each unit pairs a human headline with a market consequence.
- **Video + text parallelism** → Quanta's LLM debate stack should be transcript + tape + chart, *not* a single chat column.

URL fetched: [bloomberg.com](https://www.bloomberg.com/)

#### 3 · Hedge-fund / prop-shop wall-of-monitors (limited public surfaces — directional)

Mostly non-public ops consoles; public signals come from terminal training guides, market reporting, award-site concept dashboards. The aesthetic — "**columns of truth**" — maps to **NOC/trading-floor posture**: many independent channels, no single hero widget.

Concrete techniques:
- **Each monitor as a concern slice**: risk, execution, research, comms (mirrors Grafana "dashboard tells a story" guidance).
- **siteInspire finance theme** as institutional typography/layout culture sampler.
- **FT newsroom "in charts" framing** as public example of finance gravitas packaging.

URLs fetched: [siteInspire Business/Finance](https://siteinspire.net/showcase/category/theme/business_and_finance) · [FT article shell](https://www.ft.com/content/5e9008e6-75dc-438d-8eb0-1b507c426847) (body paywalled).

#### 4 · Linear (linear.app + Linear "Now" posts)

Keyboard-first product dev system whose UI is canonical reference for dense lists, fast navigation, modern theme math. Unusually explicit about LCH theme generation, contrast slider, density without overwhelm.

Concrete techniques:
- **LCH-based theme generation** with three control knobs (base, accent, contrast) plus accessibility high-contrast skins.
- **Elevation vocabulary**: background → foreground → panels → dialogs/modals as generated surfaces.
- **2026 refresh philosophy "attention you haven't earned"**: dim sidebar so main workspace wins; compact tabs; fewer icon treatments; soften borders "felt not seen."
- **Agent-era positioning**: explicit narrative permission to foreground agent state without clownish chat UI.

URLs fetched: [How we redesigned the Linear UI (part II)](https://linear.app/now/how-we-redesigned-the-linear-ui) · [A calmer interface for a product in motion](https://linear.app/now/behind-the-latest-design-refresh)

Tokens: LCH theme pipeline · Inter Display headings + Inter body.

#### 5 · Stripe Dashboard patterns (Stripe Apps docs)

Best public articulation of **financial-grade tables** and **state choreography** (loading, empty, progress) that still feels modern.

Concrete techniques:
- **Explicit numeric alignment API**: `TableCell`/`TableHeaderCell` support `align: "left" | "center" | "right"`.
- **Vertical alignment control** (`vAlign`) for mixed content rows (numbers + badges).
- **Patterns are compositions**: standardized loading = Spinner + skeleton templates.
- **Dedicated "Communicating state" + "Loading" + "Waiting screens" pattern pages.**

URLs fetched: [Stripe Apps Table](https://docs.stripe.com/stripe-apps/components/table) · [Design patterns for Stripe Apps](https://docs.stripe.com/stripe-apps/patterns)

#### 6 · Vercel Geist Design System

Vercel's public design system: typography presets, color scales, material elevations, Command Menu, Spinner, Destructive Action Modal, Status Dot. Gives **copy-pasteable class semantics** that directly support Quanta's existing "Geist" theme lane.

Concrete techniques:
- **Typography as Tailwind class recipes**: `text-heading-*`, `text-label-*`, `text-label-14-mono`, `text-label-13-mono`, `text-label-12-mono`.
- **Tabular numbers called out explicitly** for `text-label-13` ("Tabular is used when conveying numbers for consistent spacing").
- **Color system structure**: Background 1/2; Component backgrounds Color 1-3 (default/hover/active); Borders Color 4-6; High-contrast 7-8; Text/icons 9-10.
- **Materials as named elevation presets**: `material-base/small/medium/large` on-page; floating layers `material-tooltip/menu/modal/fullscreen` with specified radii (6/12/16).
- **Destructive Action Modal**: type-to-confirm gate; optional **red striped irreversibility band**; loading disables both buttons with spinner on primary; focus management + `aria-labelledby` pairing.

URLs fetched: [Geist Typography](https://vercel.com/geist/typography) · [Geist Colors](https://vercel.com/geist/colors) · [Geist Materials](https://vercel.com/geist/materials) · [Geist Destructive Action Modal](https://vercel.com/geist/destructive-action-modal)

Tokens: **Geist Mono** pairing; named scales (Gray, Gray alpha, Blue, Red, Amber, Green, Teal, Purple, Pink) with **P3** note on supported displays.

#### 7 · Datadog (new dashboards experience)

Best public example of **"dense but legible"** for mixed telemetry (metrics + logs + maps + heatmaps) in one surface — directly analogous to multi-asset + LLM + broker feeds.

Concrete techniques:
- **Responsive grid** with snapping, squeezing, swapping widgets; **per-widget timeframes** for correlation.
- **High density mode on ultrawide**: stacks top/bottom halves side-by-side; toggle in UI.
- **Bulk edit ergonomics**: delete without confirm dialog but **undo** at top + `cmd/ctrl+z`.
- **Widget grouping** with `cmd+G`; partial-width groups for side-by-side comparisons.
- **APM stats graph configuration** emphasizes choosing service/resource/span level detail.

URLs fetched: [Datadog blog: new dashboards experience](https://www.datadoghq.com/blog/datadog-dashboards/) · [Datadog docs: APM stats graph](https://docs.datadoghq.com/dashboards/guide/apm-stats-graph/)

#### 8 · Grafana (refresh + command palette + kiosk)

Open-source dashboarding standard that encodes refresh semantics, time-range mental models, and oncall navigation in ways web SPAs often get wrong.

Concrete techniques:
- **Manual refresh cancels pending requests** (spinner = work discarded/restarted).
- **Auto refresh is OFF by default**; auto interval adapts to query range + pixel budget.
- **Time picker**: relative, absolute, semi-relative ranges; semi-relative can show progressive zoom-out history.
- **Keyboard**: `Ctrl+K` command palette; `d+k` kiosk hides chrome for wall displays.
- **Observability strategy patterns**: USE vs RED vs Four Golden Signals.

URLs fetched: [Grafana Use dashboards](https://grafana.com/docs/grafana/latest/dashboards/use-dashboards/) · [Grafana best practices](https://grafana.com/docs/grafana/next/visualizations/dashboards/build-dashboards/best-practices/)

Tokens: meaningful color semantics (example: blue good / red bad) + thresholding.

#### 9 · Observable / Observable Framework + Plot

Playbook for **non-generic chart systems** — small multiples and non-candle chart idioms that break flat KPI monotony.

Concrete techniques:
- **CSS grid classes** like `grid grid-cols-2-3` + card spanning (`grid-colspan-2`, `grid-rowspan-2`).
- **`resize()`-driven chart sizing** so marks respond to real layout width.
- **Stepped series** (`curve: "step"`) for discrete events; **`markerEnd`** to emphasize "latest point."
- **Tick strip / micro-distribution** charts: thin ticks + **bold last tick** overlay using mark ordering + `strokeWidth`.

URL fetched: [Observable blog: dashboards with Framework + Plot](https://observablehq.com/blog/how-to-build-dashboards-observable-framework-plot)

#### 10 · Raycast (command surface + action panel)

Reference for **how to teach shortcuts without tutorials**: every item exposes an action panel with inline shortcuts and fuzzy search inside actions.

Concrete techniques:
- **Dual interaction**: `↵` primary action vs `K` to open full Action Panel for discovery.
- **Fuzzy search inside the action panel** to scale large command sets.
- **Destructive actions explicitly marked red** in extension-defined actions.
- **Submenus + inline recorders** (hotkey recorder) without leaving the panel.

URL fetched: [Raycast manual: Action Panel](https://manual.raycast.com/action-panel)

#### 11 · Arc Browser — spatial UI & split workflows

Reference for **breaking the default layout metaphor** while staying usable.

Concrete techniques:
- **Split View as a first-class object** saved as its own sidebar tab; horizontal vs vertical splits.
- **Keyboard-first split creation** (`Command-Shift-Plus`) plus command palette strings like "Add Right Split."
- **Separate tabs via context menu / X** without losing the split configuration mental model.

URL fetched: [Arc Help Center: Split View](https://resources.arc.net/hc/en-us/articles/19335393146775-Split-View-View-Multiple-Tabs-at-Once)

#### 12 · Excalidraw + tldraw — "hand-drawn" system diagrams

For Quanta's 6-role debate graph, hand-drawn marks signal **human intent** and break "flat corporate card" monotony without becoming messy clipart.

Concrete techniques:
- **Wobbly stroke + simple palette** as deliberate "low-fidelity truth layer" over high-fidelity market charts (contrast pairing).
- **Canvas-first navigation** (space/wheel panning).
- **Use sketch layer only for topology** (agents, queues, dependencies), never for numeric tables.

URL fetched: [Excalidraw docs](https://excalidraw.com/docs) · (tldraw fetch timed out)

#### 13 · Cursor — AI-era product chrome

Cursor's changelog is a good **feature taxonomy** for what users now expect from agent UIs.

Concrete techniques:
- **Quick-action pills** as a first-class navigation affordance.
- **Parallel worker metaphor** ("Build in Parallel" + async subagents).
- **Pin reusable "skills"** to quick actions.
- **Context usage breakdown** as a diagnosability panel pattern.

URL fetched: [Cursor Changelog](https://cursor.com/changelog)

#### 14 · TradingView (Supercharts)

Encodes trader muscle memory: synchronized multi-chart, command search, deep interval control, footprint language.

Concrete techniques:
- **Up to 16 charts per screen** + synchronized symbols/timeframes/drawings.
- **Global command search** to complete actions quickly.
- **Custom intervals** including seconds and range bars.
- **Order-flow adjacent visuals**: volume footprint, time price opportunity (TPO), session volume profile.

URL fetched: [TradingView Features](https://www.tradingview.com/features/)

> Note on Hyperliquid / dYdX / Drift / Coinbase Advanced / IBKR TWS / Thinkorswim: primarily product UIs behind accounts; this research did not authenticate into them. Treat as competitive moodboards via your own screenshots; for documented patterns, TradingView + Bloomberg above are the honest anchors.

#### 15 · Editorial / data journalism (Reuters Graphics + FT)

The "printed WSJ/FT gravity" lane: typographic authority, chart-led storytelling, interactive proof rather than dashboard widgets.

Concrete techniques:
- **Graphics as a section index** with dated "packages" (narrative arcs), not infinite feeds.
- **Headline + subdeck + chart promise** pattern.
- **FT "in charts" headline pattern** for market explainers.

URLs fetched: [Reuters Graphics hub](https://graphics.reuters.com/) · [FT article shell](https://www.ft.com/content/5e9008e6-75dc-438d-8eb0-1b507c426847)

> NYT Upshot returned 403 to the automated fetcher.

#### 16 · Industrial HMI + E-stop reality

Quanta's "kill everything" language is emotionally correct; industrial design clarifies what **must** feel hardware-real vs what can be software-mediated.

Concrete techniques:
- **E-stop must not be "a button on the HMI"**; safety practice expects **hardwired mushroom head** with push-pull or twist-to-release; yellow background sometimes used; red mushroom.
- **Separate E-stop from routine power-off**; red momentary power-off + green illuminated power-on as distinct controls.
- **Physical reset button** can beat touchscreen UX for repeated "bash reset" behaviors.
- **Stack light color semantics**: standardize red/amber/green/blue; solid vs slow flash vs fast flash to multiply states.

URL fetched: [Control Design: Why just an emergency stop button isn't enough](https://www.controldesign.com/displays/hmi/article/11319561/why-just-an-emergency-stop-button-isnt-enough)

#### 17 · Award / inspiration sites (Awwwards + siteInspire)

Good for radical visual divergence and discovering non-default compositions; bad as engineering constraints unless you reverse-engineer principles.

Concrete techniques:
- **Awwwards nominees as interaction moodboards** (motion, hero numerals, alternate nav) — not as accessibility baseline.
- **siteInspire finance theme** as broad institutional typography/layout culture sampler.
- **2026 Awwwards "Algorithmic Trading Dashboard" nominee** documents multi-screen layout explorations as explicit elements.

URLs fetched: [Awwwards nominee](https://www.awwwards.com/sites/algorithmic-trading-dashboard) · [siteInspire Business/Finance](https://siteinspire.net/showcase/category/theme/business_and_finance)

---

### 9.2 Synthesis — 27 named design moves we're stealing

Each implementable and tied to a fetched reference.

1. **Amber primary text plane + black field** for Bloomberg lane's body numerics and labels.
2. **Red "Cancel/stop"** and **green "Go/execute"** keyboard semantics translated into web buttons.
3. **Sector-colored F-key strip**: map Quanta modules (Futures · Equities · Crypto · LLM · Broker) to stable chroma blocks.
4. **2×2 and 2×3 "desk layouts"** presets echoing 2-6 monitor desk reality.
5. **LCH theme generation** with explicit **contrast** control for accessibility skins (Linear).
6. **Sidebar luminance recession** so the "tape" wins (Linear 2026 refresh).
7. **Soft separators**: fewer hairlines, more rounded corners on divider containers (Linear refresh).
8. **`text-label-13` + Tabular** for dense numeric columns (Geist).
9. **Geist mono ladder**: 14 mono for headers-to-numeric pairing, 13 mono for secondary metrics, 12 mono for meta.
10. **Border colors as named tokens** (Color 4 default / 5 hover / 6 active) instead of ad-hoc `border-white/10` (Geist).
11. **Material stack for "popping cards"**: base/small/medium/large for on-page depth; menu/modal/fullscreen for floating depth (Geist).
12. **Destructive modal pattern**: type-to-confirm + **striped irreversibility band** + loading locks + inline API error (Geist).
13. **Stripe-grade tables**: explicit **right-align** numerics + `vAlign` discipline for mixed rows.
14. **Stripe "patterns are compositions"**: standardize loading as Spinner + skeleton templates.
15. **Datadog-like high-density mode** for ultrawide ops desks.
16. **Widget grouping with lasso + `cmd+G`** mental model.
17. **Per-panel timeframe overrides** for correlation views.
18. **Refresh that cancels in-flight on manual refresh** (Grafana — prevents "stacked stale requests").
19. **Auto-refresh tied to pixel/time-range budget**, not vanity 1s polling.
20. **Kiosk mode** for wall-mounted ops displays (`d+k` equivalent).
21. **RED-dashboard semantics** for alerting surfaces (rate/errors/duration).
22. **Observable tick-strip microcharts** for "last 52 updates" telemetry (bold last tick).
23. **Raycast-style action panel**: `K` opens "all actions here" with fuzzy search + red destructive entries.
24. **Arc-style split tabs** for "LLM debate | execution | risk" persistent layouts.
25. **TradingView-grade multi-chart sync** language for market panes.
26. **Industrial honesty for kill**: software kill matches mushroom-head semantics (twist-to-arm / two-step / type-to-confirm), acknowledging hardware E-stop isn't in the browser.
27. **Stack-light vocabulary** for bot state: solid vs slow flash vs fast flash → degraded vs critical.

**Operator's load-bearing phrases mapped to references:**

- **"Columns cards popping pop out"** → Geist materials + Datadog grid/snapping + Observable card spanning.
- **"Refresh buttons"** → Grafana's cancel-pending + auto-interval discipline; pair with Geist Spinner + Stripe loading patterns.
- **"Kill everything"** → Geist destructive modal + industrial E-stop separation principles.
- **"Not generic / not flat"** → Bloomberg contrast philosophy + Observable non-standard marks + editorial graphics framing.
- **"Radical"** → Arc split metaphor + Cursor quick pills/parallelism + Linear agent-era chrome.

---

### 9.3 Anti-patterns we explicitly will not commit (10)

1. **Glassmorphism for primary numerics** — liquidity of text; conflicts with Bloomberg-like contrast.
2. **Neumorphism / soft UI** for dense tables — kills scan speed; fights tabular alignment.
3. **Material "dp elevation"** as the only depth model — reads consumer Android, not ops.
4. **Rainbow gradients on P&L** — destroys trust; violates "color means state."
5. **1-second global polling "because crypto"** — Grafana explicitly warns; contradicts Datadog perf culture.
6. **Fake hardware toggles** that don't reflect real exchange/broker state — industrial HMI emphasizes honest signaling.
7. **Center-aligned numbers in tables** — Stripe explicitly engineers alignment controls; center is a smell.
8. **Low-stakes `type DELETE` melodrama** on routine actions — Geist says typed gate reads melodramatic for low stakes.
9. **Monochrome icons everywhere** when icon noise was already identified as a problem.
10. **"One chat column rules them all"** for a trading ops stack — Bloomberg + Arc + TradingView all imply parallel channels.

---

### 9.4 Five additional open questions surfaced by research

These supplement the 5 in §11 and may merit a follow-up turn:

1. **Theme strategy**: Is "Bloomberg" a literal amber/black terminal skin, or a token translation of Bloomberg semantics onto Geist's Color 1-10 + Materials so Control / Geist / Bloomberg feel *related* rather than disjoint?
2. **Kill semantics**: Software-only emergency stops (type-to-confirm + API hard-stop), or external physical arming key / LAN-only relay for real-money later? Industrial guidance strongly separates these.
3. **Density default**: Default to Datadog-like high-density on ultrawide, or default calm + explicit "SRE mode" toggle?
4. **Motion budget**: How much continuous motion is acceptable on an ops wall? Linear worries about crowding; Grafana kiosk hides chrome.
5. **Agent visualization style**: Excalidraw-sketch (human-intent) or DAG node-link (engineering truth)? Observable/TradingView patterns push different instincts.

---

### 9.5 Research integrity notes

**Fetched directly in this session** (single attempt; live HTTP):
Bloomberg company story · Wikipedia Terminal page · bloomberg.com homepage · Linear posts (×2) · Geist Typography/Colors/Materials/Destructive-modal · Stripe Table + patterns · Datadog blog + APM stats doc · Grafana best practices + use-dashboards · Observable blog · Raycast manual · Arc help center · Excalidraw docs root · Cursor changelog · TradingView features · Reuters graphics hub · FT article shell · Control Design HMI article · Awwwards nominee · siteInspire finance theme.

**Did not successfully fetch** (timeouts / 403):
`raycast.com/blog/a-fresh-look-and-feel` (timeout) · `tldraw.dev` (timeout) · `bloomberg.com/graphics/` (timeout) · NYT Upshot section (403).

End of research dossier.

---

## §10 · What lands in the first commit on `feature/v3-frontend`

First commit (this one):
1. `docs/V3_REDESIGN_PLAN.md` — this document.
2. `docs/V3_AUDIT_EVIDENCE/` — 31 captured API responses + 12 screenshots + reproduction README.

Nothing else. Code lands in subsequent commits, one per subagent wave per §7.

## §11 · Operator decisions (historical — now resolved in §0)

These were the 5 questions asked of the operator before code started; all are answered in §0. Kept here for traceability.

1. ~~Approve / amend / reject §3 design thesis.~~ → **Approved as-is**.
2. ~~Approve / amend / reject the 7 signature moves.~~ → **All 7 approved for V3.0**.
3. ~~Multi-agent parallelism.~~ → **Balanced — 4 subagents in parallel**.
4. ~~Deploy strategy.~~ → **Ship in-place to live `/ops` with `?v=` cache-busting**.
5. ~~Backend changes allowed?~~ → **Frontend-only**.

## §12 · Next actions

Now that the plan is locked:

1. **Commit this document + audit evidence** on `feature/v3-frontend`. *(This turn.)*
2. **Wave 0 — Token Smith subagent.** Adds new tokens (§4.1), fixes the density-doesn't-compact bug (§4.4), bumps `?v=N` cache-buster, commits. Self-contained, isolated CSS diff. *(Next turn.)*
3. **Wave 1 — 4 parallel signature-move agents.** Hero+Kill-Bar+DD-Ribbon+Heartbeat (one agent) · Debate Floor · Gates Matrix · Sparkline Strip + Cmd-K. *(After Wave 0 lands.)*
4. **Wave 2 — 4 parallel card refiners** (LLM stack, Risk + breakers, Ops cards, Comms). *(After Wave 1 lands.)*
5. **Wave 3 — QA Verifier subagent.** Runs the §8 acceptance checklist. Blocks merge if any item fails.
6. **Merge `feature/v3-frontend` → `main`.** Operator presses the button.

End of plan v1.0 · locked 2026-05-12 · ready to execute.
