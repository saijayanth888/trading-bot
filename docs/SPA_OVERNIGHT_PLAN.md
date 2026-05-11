# SPA overnight wiring plan · 2026-05-11

**Author:** Claude Opus 4.7 · planning at 01:00 ET, hand-off to 3 agents
**Operator review:** 08:00 ET — `/ops_spa` and `/dashboard_spa` must be
production-grade with real data everywhere; legacy `/ops` and `/`
untouched (operator reviews both side-by-side in the morning).

## Current state — what's WORKING vs BROKEN

### `/ops_spa` (986 LOC ops_spa.js + 25 LOC template)

| Card | Status | Notes |
|---|---|---|
| Hero · combined equity | ❌ BROKEN | Shows `$0.00`. Should be real `combined_portfolio.total_equity` ($118,933.61). |
| Hero · regime cells | ⚠ PARTIAL | Shows "100" / "66" without %. Probability is 0-1 ratio, must ×100 for display. |
| Agent timeline · 24h | ✅ LIVE | Mock cron data, but visual works |
| Research stream | ❌ MOCK DATA | "Garrett: dn-linked wallet sends 8,358 ETH" — Claude Code Design mock. Replace with real feed. |
| Entry gates table | ✅ LIVE | All 8 pairs from `/api/ops/gates`, real PASS/BLOCK chips |
| Pair telemetry sparklines | ✅ LIVE | 8 pairs, /api/ops/sparklines |
| Service health · 8 probes | ✅ LIVE | freqtrade, hermes_*, ollama, etc. |
| LLM providers | ✅ LIVE | ollama/anthropic stats |
| Open positions | ✅ LIVE | "no open positions" — correct |
| Stocks · Shark TFT | ✅ LIVE | val_acc 0.3810, best epoch 6 |
| Stocks · Wheel + Shark | ✅ LIVE | Alpaca paper data |
| MCP wire status | ✅ LIVE | `/api/ops/mcp` last_call |
| Trades & risk · 24h | ⚠ PARTIAL | Numbers present; verify mapping |
| Circuit breakers | ✅ LIVE | none registered yet |
| Quick actions · control panel | ⚠ PARTIAL | 6 buttons, kill switch not yet wired |
| EPT · champion genome | ✅ LIVE | gen0-011, fitness 0.754 |
| Sentiment aggregate | ⚠ PARTIAL | Sentiment 0.00 conf 0.00 (real — sentiment_log empty) |

**Missing cards (prototype has, SPA doesn't):**
- Training (`/api/ops/training`) — FreqAI model retrain status
- Readiness (`/api/ops/readiness`) — validation gate status
- Regime Config Editor (`/api/ops/regime_config` GET + POST)
- Slack Preview (`/api/ops/slack_preview`) — next daily report
- MCP Tool Console (`/api/ops/tools` + POST `/api/ops/mcp/{tool}`)

### `/dashboard_spa` (392 LOC dashboard_spa.js + 25 LOC template)

| Element | Status | Notes |
|---|---|---|
| Topbar equity | ❌ MOCK | Shows `$119,842.42 +1.84%`. Should be real total_equity. |
| Pair selector / venue tabs | ✅ LIVE | Crypto/Stocks tabs work |
| Big price · BTC/USD | ✅ LIVE | $80,864.99 — real |
| Day delta | ⚠ WRONG | `-23.37% · day` — looks like a wrong ratio calc |
| Source / Regime / Conf / Gate / Bars / Markers strip | ✅ LIVE | All real |
| BTC/USD 5m candle chart | ✅ LIVE | Drawing beautifully with volume + wheel-zoom legend |
| Model view (TFT probs) | ✅ LIVE | P(UP) 0.37, P(FLAT) 0.28, P(DOWN) 0.35 |
| Market context (regime/sentiment/on-chain) | ✅ LIVE | Real values |
| Open positions | ✅ LIVE | "no open positions" |
| Recent trades · last 10 | ✅ LIVE | 2 rows visible (BTC, SOL) |
| Wheel-zoom around cursor | ⚠ NOT VERIFIED | Needs interactive test |
| Drag-pan with bar bounds | ⚠ NOT VERIFIED | Needs interactive test |
| Double-click reset | ⚠ NOT VERIFIED | Needs interactive test |
| Hover OHLC tag | ⚠ NOT VERIFIED | Needs interactive test |
| `?pair=` URL parse | ✅ LIVE | Reads on mount |
| `?venue=` URL parse | ⚠ NOT VERIFIED | Needs probe |

## The 20-step plan, grouped into 3 agents

### Agent A · /ops_spa critical data bugs (the operator sees these first)

1. **Fix `/ops_spa` hero equity** — currently $0.00, must read `combined_portfolio.total_equity`. Likely a field-name typo or wrong nested .data extraction.
2. **Fix `/ops_spa` regime cells** — probabilities are ratios (0–1); multiply by 100 for the % display. Show "TRENDING UP" / "TRENDING DOWN" with the right color.
3. **Replace Research stream mock data with real activity feed** — port the multi-source feed from `templates/ops.html` (the legacy one I wrote tonight: regime transitions + open trades + MCP last_call + sentiment + ollama health + circuit breakers).
4. **Day P&L on hero** — compute `total_equity - combined_peak_equity` for the day's delta; format as `+$X.XX  +X.XX% · day`.
5. **Drawdown bar on hero** — fill bar = `abs(combined_drawdown_pct) / 10 * 100%`; color is up/warn/down at thresholds 0/5/8.
6. **TimeSince in every card header** — each refresh should set `data-checked-at` on the card; a single `<TimeSince>` per card auto-ticks "Ns ago".
7. **Empty + error state** — each card renders a clear "endpoint unavailable" message when the envelope status is "down" or fetch errors, instead of a perpetual "Loading…".
8. **Verification** — headless playwright probe captures a full-page screenshot + `placeholder_count` per card. Any card with `placeholder_count > 3` is broken; fix before claiming done.

### Agent B · `/dashboard_spa` critical bugs + interaction verification

9. **Fix `/dashboard_spa` topbar equity** — currently shows mock `$119,842.42 +1.84%`. Wire to `/api/ops/combined_portfolio.total_equity` with the same NumberRoll treatment as `/ops_spa` topbar.
10. **Fix day delta on hero** — currently `-23.37% · day`, almost certainly reading the wrong field. Should match `/api/ops/combined_portfolio.combined_drawdown_pct` or a true daily delta.
11. **Verify CandleChart interactions** — headless playwright probe: scroll wheel → view changes; mousedown+move → view changes; dblclick → reset; mousemove → hover state shows OHLC. Each of these is the non-negotiable from the operator's prompt.
12. **Verify `?pair=` URL switch** — load `/dashboard_spa?pair=ETH/USD` and confirm ETH is selected + chart loads ETH candles.
13. **Verify `?venue=` URL switch** — load `/dashboard_spa?venue=stocks` and confirm STOCKS tab is auto-selected + pair dropdown contains SOFI / PLTR / NVDA / AMD / SPY.
14. **Verify timeframe switching** — click 1m/5m/15m/1h/4h/1d → confirm a fresh fetch fires (URL changes or fetch network call).

### Agent C · Port missing cards + Quick Actions / Kill switch wiring

15. **Port Training card** — wires to `/api/ops/training` — show FreqAI model retrain status (last retrain ts, val_sharpe, n_samples). Spec: prototype's Training card pattern.
16. **Port Readiness card** — wires to `/api/ops/readiness` — validation gate status (Sharpe, MaxDD, PF, trades, win rate vs thresholds). Spec: validation gate matrix.
17. **Port Regime Config Editor** — wires to `/api/ops/regime_config` GET + POST. Inline form, atomic write, surface diff in operator confirmation.
18. **Port Slack Preview** — wires to `/api/ops/slack_preview` — render the daily report preview the operator will see at 00:00 UTC.
19. **Port MCP Tool Console** — `/api/ops/tools` for the tool list, `POST /api/ops/mcp/{tool_name}` for execution. JSON args editor + result viewer.
20. **Wire Kill Switch** — already in qc_react.js (1500ms hold-to-confirm). Wire it on `/ops_spa` topbar to `POST /api/ops/pause` and on Quick Actions card. Verify pointermove-cancel works.
21. **Wire Quick Actions** — PAUSE → `/api/ops/pause`, RESUME → `/api/ops/resume`, RELOAD → freqtrade API, TRIGGER EVOLUTION → MCP `trigger_evolution_cycle`, REBALANCE → `/api/ops/rebalance` GET (preview) + POST (confirm), DAILY SLACK BRIEF → no-op for now (Hermes cron fires it).

## Constraints (CARRIED FORWARD FROM EVERY PRIOR SESSION)

- **DO NOT touch** `templates/index.html`, `templates/ops.html`, `static/js/app.js`, `static/js/ops.js`, `static/js/effects.js`, `static/js/components.js`, `static/js/utils.js`, `static/css/app.css` — these power the legacy `/` and `/ops` pages that the operator reviews at 08:00.
- **DO NOT touch** `user_data/dashboard/ops_routes.py` or `app.py` — backend stays exactly as the legacy pages need it.
- **DO NOT touch** anything under `user_data/strategies/` or `user_data/freqaimodels/` or `user_data/modules/` or `stocks/` — the bot must keep trading.
- **DO use** `docker compose build dashboard && docker compose up -d dashboard` — not file-copy into the container.
- **DO cache-bust** version stamps after each ship: `?v=20260512-N` → `?v=20260512-N+1` on every change.
- **DO verify** in a headless playwright probe before claiming done — capture a screenshot to `/tmp/agent_<A|B|C>_proof.png`.

## Verification checklist (final, all three agents)

```bash
docker compose build dashboard 2>&1 | tail -3
docker compose up -d dashboard 2>&1 | tail -3
until curl -sf http://localhost:8081/ops_spa >/dev/null; do sleep 2; done

# Each SPA page should render with id="root" and zero pageerrors
curl -sf http://localhost:8081/ops_spa | grep -c 'id="root"'        # == 1
curl -sf http://localhost:8081/dashboard_spa | grep -c 'id="root"'  # == 1

# Headless screenshot probe — no PAGEERROR; hero shows real equity
node /tmp/screenshot.mjs http://localhost:8081/ops_spa /tmp/morning_ops_spa.png
node /tmp/screenshot.mjs http://localhost:8081/dashboard_spa /tmp/morning_dashboard_spa.png

# Confirm hero equity is REAL (not $0.00 and not $119,842.42 mock)
# by probing the rendered DOM:
node -e '...read window.document.getElementById("hero-equity-value").textContent...'
```

## What the operator reviews at 08:00 ET

1. **`/ops`** (legacy, untouched) — confirm still working: live trade visible if any, gates panel, all cards.
2. **`/ops_spa`** (new SPA) — A/B compare against `/ops`. Real data everywhere. Every card populated. Hero shows real equity. Research stream shows real events.
3. **`/dashboard_spa`** (new SPA) — chart drawing, wheel-zoom-around-cursor works, drag-pan works, dbl-click resets, hover shows OHLC.
4. **Cutover decision** — operator decides which SPA replaces the legacy page (or keep both for now).

## Commit policy

Each agent commits one PR-style commit when done, prefixed with `[A]`,
`[B]`, or `[C]` so the operator can diff them. Body lists every fix.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
