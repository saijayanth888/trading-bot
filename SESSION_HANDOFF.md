# Session Handoff — 2026-05-11 EOD

> **Purpose of this file.** Tomorrow's session may run via Claude Code, Cursor, or cloud Claude — none of which auto-load each other's memory. Read this file FIRST. It captures everything an incoming AI needs to be useful without re-discovering the whole stack.
>
> **Operator's goal.** $2,000 in 4 weeks of *paper* trading. Not production-ready bulletproofing — paper-trading effectiveness. The bot is in paper mode (`dry_run: true`) and stays there for the full 4 weeks.

---

## 1. How to orient — read these first (in order)

| File | Why |
|---|---|
| `SESSION_HANDOFF.md` (this file) | Today's state + tomorrow's plan |
| `PRODUCTION_READINESS_AUDIT_2026-05-11.md` | Architecture overview + scored gaps (72/125). Reference, not a to-do list |
| `CHECKLIST.md` | Emergency response playbook |
| `user_data/universe.json` | Single source of truth for every traded symbol (12 crypto + 14 wheel + 15 dashboard basket) |
| `README.md` | Stack overview (5 layers) |
| `~/.claude/projects/-home-saijayanthai-Documents-trading-bot/memory/` | Claude Code's auto-memory (operator preferences, lessons). MEMORY.md index there is canonical for behavioral guidance |

**Don't dump TRADE-LOG.md or RESEARCH-LOG.md into context.** Use `git log` for recent changes, `docker logs` for live state.

---

## 2. Current state (snapshot 2026-05-11 23:30 UTC)

### Bot

```
mode:           paper · running · dry_run: true
total equity:   ~$118,860
peak (combined): $19,000 crypto + $99,940 stocks paper
open:           BCH/USD long @ $450.26  ·  stake $936.81  ·  ~-0.55% unrealized
                Entry tag: meta_up_regime  ·  Conf 0.865  ·  Regime trending_down
closed today:   2 crypto (both losers, both whip-saw entries)
combined DD:    ~0.42% — well under 8% threshold
circuit breaker: OFF
```

### Infrastructure

```
containers (5/5 healthy):
  dashboard      0.0.0.0:8081   →  Dashboard SPA + FastAPI /api/ops/*
  freqtrade      127.0.0.1:8080 →  Freqtrade w/ FreqAIMeanRevV1
  postgres       127.0.0.1:5434 →  Trade store + regime log + journal
  influxdb       127.0.0.1:8086 →  Metrics
  grafana        127.0.0.1:3000 →  Dashboards
```

### Training queue

Freqtrade is **mid-retrain** post-restart (12 crypto pairs, ~6 min each at `n_epochs=50` with early stopping). Expected completion: 60-70 min from 23:25 UTC. Don't disrupt it.

### Hermes crons (26 active)

All scripts run via `hermes cron list`. Key ones:

| Cron | Schedule | Purpose |
|---|---|---|
| `wheel_snapshot` | `*/1 9-16 * * 1-5` | Alpaca equity snapshot every minute during market hours |
| `wheel_candles` | `*/5 9-16 * * 1-5` | Refresh 5-min OHLC for dashboard sparklines |
| `stocks_tft_smoke` | `30 8 * * 1-5` | **NEW** Daily TFT inference smoke test (8:30am ET) |
| `shark_pre_market` | `... 8 * * 1-5` | Generate trade candidates |
| `shark_market_open` | `30 9 * * 1-5` | Execute trades (with new TFT veto gate) |
| `risk_monitor_15min` | `*/15 * * * *` | Risk dashboard refresh |
| `stocks_ml_train` | `0 23 * * 0` | Sunday TFT retrain |
| `ept_training_daily` | `0 2 * * *` | EPT generation daily |

---

## 3. What was done today (most recent first)

Six commits since the universe.json refactor:

```
be68c7a  Paper-trading effectiveness pack: 5 fixes targeting the $2k/4w P&L target
1366bfd  EOD hardening: ft_authed_get migration + outcome_resolver to chat_json
5f023c7  fix: /api/universe path lookup
7fe8b22  universe.json: single source of truth for all tracked symbols
57a7b02  fix: ops_routes.py DEFAULT_STOCK_SYMBOLS duplicate default arg
369a1ee  ui: expand hardcoded symbol arrays
```

### The 5 paper-effectiveness fixes (commit `be68c7a`)

1. **Fees recalibrated** `0.0025 → 0.005` (Coinbase Advanced blended). Backtests no longer flatter themselves.
2. **B-22 regime-stability gate** — new `regime_min_stable_hours: 2.0` in `regime_gating` config. Strategy blocks entries within 2h of an HMM regime flip (`%-regime_duration_h >= 2.0` condition added to long_conditions in `populate_entry_trend`). Today's 3-for-3 losses all entered minutes after a flip.
3. **Stocks TFT inference gate** in `market_open.py::_execute` — `_tft_gate(symbol)` runs `predict_direction` on each BUY candidate; vetoes when `down > up` with ≥0.05 conf or when `up < 0.40`. **Tested live: vetoes 6/8 watchlist tickers right now (only SOFI + SPY pass).** Also fixed dtype bug in `tft_stock.py:331-336` (np.zeros defaulted to float64, model expects float32) that silently broke inference for any ticker without trained-time norms.
4. **Stocks notifier wired** — new `stocks/shark/notify.py` shim re-exposes the crypto-side `modules.notifier.notify` singleton. Imported in `market_open.py` (trade_entry), `daily_summary.py` (daily_summary), `execution/orders.py` (error). Operator now gets Slack pings on stocks events.
5. **Daily TFT smoke cron** registered (`hermes cron create '30 8 * * 1-5' --name stocks_tft_smoke --script stocks_tft_smoke.sh --no-agent`). Runs `python -m shark.ml.cli infer` on SPY + NVDA + SOFI each weekday morning. Catches model corruption before market open.

Plus a **circuit-breaker false-alarm fix** (user-reported): "stocks data stale/untrusted" was lighting up red every evening/weekend because wheel_snapshot cron only runs Mon-Fri 9-16 ET. `unified_risk.py` now exposes `market_open_now: bool`; `ops_spa.js` hides those rows ("market closed — gate inactive") outside market hours.

### Earlier today

- **Universe refactor** — `user_data/universe.json` is the single source of truth. `scripts/sync_universe.sh` mirrors it into `.env` (WHEEL_SYMBOLS / DASHBOARD_STOCK_SYMBOLS / DASHBOARD_PAIRS) and `user_data/config.json` (pair_whitelist).
- **`ft_authed_get` migration** at 2 sites in `ops_routes.py` (line 2102, line 2528). JWT 401s on these endpoints now refresh-and-retry instead of bubbling.
- **outcome_resolver.py migrated** from raw `anthropic.Anthropic()` to `shark.llm.client.chat_json` router (honors `SHARK_LLM_PROVIDER` routing).
- **Hermes switched to `hermes3:8b-trader`** (custom Modelfile, 8k context, ~6GB GPU, same 16 tok/s throughput as full hermes3:8b). Operator chose this over qwen2.5:7b after benchmark.
- **wheel_candles.sh hardened** — reads fallback ticker list from `user_data/universe.json` when env vars unset.

---

## 4. What to monitor tomorrow

### Morning before market open (8:30am ET)

Tomorrow at 8:30 ET, the new `stocks_tft_smoke` cron fires. Check it succeeded silently (no Slack noise = good). If it Slacks a 🚨, the TFT inference path broke overnight — likely cause: model file corruption or dtype regression (the bug we fixed today was that exact pattern).

```bash
tail -20 /home/saijayanthai/Documents/trading-bot/stocks/memory/cron-stocks-tft-smoke.log
```

### Market open (9:30am ET → 4pm ET)

**Key question: did the TFT gate block any LLM BUY recommendations?** Watch `market_open` cron output for lines like:

```
NVDA rejected — tft-veto-down: up=0.17 down=0.38 conf=0.07
```

That's the new behavior working as intended.

**Watch for the regime-stability gate** in freqtrade logs:

```bash
docker logs --since=1h freqtrade | grep -iE "regime_duration|enter_long"
```

Entries during a freshly-flipped regime should be blocked. If you see entries during regime duration < 2h, something's wrong with the gate.

### Live position

BCH/USD long open at $450.26. Watch for exit signal (regime flip + exit_threshold + regime_exit_delta math). Currently ~-0.55%.

### Sparklines (operator UX)

Reload dashboard with `Cmd+Shift+R` to bust cache. Cache-bust marker is `cutover19`. All 15 stocks should have populated sparklines. The "stocks data stale" row in Circuit Breakers should show "market closed — gate inactive" (not red) when market is closed.

---

## 5. Known quirks (don't be surprised by these)

1. **Freqtrade restart kicks off 12-pair retrain.** Each pair ~6 min with `n_epochs=50` + early stopping. Don't restart freqtrade casually. Postgres trade store survives restarts — sqlite is unused.

2. **HMM regime is BTC-driven and broadcast to all 12 pairs.** ETH or SOL might genuinely be in a different regime, but we apply BTC's view to all. Architecturally limited.

3. **Sparkline cron only runs market hours.** Crypto sparklines populate during market-hours cron tick. Outside market hours, `bars_count` may be small.

4. **`hermes3:70b` may load briefly.** Some shark agents use tier="deep" → resolves to 70b. It loads (~43GB GPU), runs, then OLLAMA_KEEP_ALIVE unloads it. Not a leak.

5. **Ollama KEEP_ALIVE=0s at systemd level.** Per-call `keep_alive: 60m` overrides. To make warm-load default, `sudo systemctl edit ollama` and add `Environment=OLLAMA_KEEP_ALIVE=60m`.

6. **NFI X6 strategy scaffolded but disabled.** Coinbase doesn't support 4h timeframe; activating requires either an in-strategy resampler or a true rewrite. Don't touch unless explicitly asked.

7. **Wheel CSPs never traded.** First attempt is scheduled for Friday's 11am ET cron. Tests assignment_check path live.

---

## 6. Operator preferences (DO follow)

- **Cost-averse.** Don't propose Anthropic-routed crons / agents without explicit opt-in (operator rolled them back over auto-billing concerns 2026-05-10).
- **UI-over-CLI.** Operator prefers fixes that surface in the dashboard, not buried in logs.
- **Config-over-hardcoded.** Every symbol list, threshold, regime knob should be config-driven. The `user_data/universe.json` + `scripts/sync_universe.sh` pattern is the gold standard.
- **Production-grade dYdX/Geist aesthetic** on dashboard — no shadows, gradients, or serif-italic. See `~/.claude/.../memory/feedback_dashboard_design.md`.
- **Reviews changes before pushing.** Don't `git push` without explicit approval.
- **Paper for 4 weeks, no live capital flip.** Don't suggest flipping `dry_run: false`.

---

## 7. Operator preferences (DON'T do)

- **Don't restart freqtrade casually** — kicks off 60-min retrain queue.
- **Don't bypass git hooks** (`--no-verify`, etc.) — fix the underlying issue.
- **Don't force-push** — operator reviews before merging.
- **Don't add unnecessary abstractions** — three similar lines beats a premature helper.
- **Don't write multi-line comments** explaining what the code does. WHY comments only.
- **Don't create planning docs unless asked.** Work from conversation context.
- **Don't dispatch parallel agents for small work** — operator is cost-sensitive. Sequential inline preferred.

---

## 8. The audit's deferred items (NOT for paper trading)

These are in `PRODUCTION_READINESS_AUDIT_2026-05-11.md` but **explicitly skipped** for the 4-week paper run because they don't move P&L:

- Fat-finger `max_order_size_usd` check (no real money at risk)
- Kill-switch HTTP retry (paper survives transient inconsistency)
- Minimal CI workflow (process hygiene)
- Correlation cap (paper risk only)
- Remove InfluxDB (operational cleanup)
- 5 unit test files (additive; doesn't move P&L)
- Purged-CV walk-forward analysis (multi-week effort)

If/when the operator decides to flip `dry_run: false`, ALL of these become blockers again. Until then: skip.

---

## 9. Quick-reference commands

```bash
# Bot mode + open positions
curl -s http://localhost:8081/api/mode | python3 -m json.tool
curl -s http://localhost:8081/api/ops/combined_portfolio | python3 -m json.tool | head -20

# Live freqtrade trade view
docker exec freqtrade curl -s -m 3 "http://localhost:8080/api/v1/status" \
  -u $(grep "^FREQTRADE_API_USER" .env | cut -d= -f2):$(grep "^FREQTRADE_API_PASS" .env | cut -d= -f2) \
  | python3 -m json.tool

# Retrain queue progress
docker logs --since=2m freqtrade 2>&1 | grep -iE "epoch.*\/50" | tail -3

# Hermes crons
hermes cron list

# Daily TFT smoke (manually fire)
bash /home/saijayanthai/.hermes/scripts/stocks_tft_smoke.sh

# Sync universe.json → .env + config.json
bash scripts/sync_universe.sh

# Emergency stop
bash scripts/emergency_stop.sh --dry-run     # rehearse
bash scripts/emergency_stop.sh               # real
```

---

## 10. End-of-day note from today's session

Operator paper-traded ~$120k all day. Closed 2 losers on whip-saw regime flips. Today's effort focused on stopping that pattern + activating the dormant stocks-side ML. The TFT gate is the biggest single bet — it's vetoing 6/8 of today's stock watchlist, which means tomorrow's stocks signals will be far more selective than yesterday's.

Bot is healthy, retraining 12 pairs (~70 min to complete), all crons green, all containers healthy. Reload your laptop browser tomorrow morning (`Cmd+Shift+R` on the dashboard) to pick up cutover19.

Bullish on the next 4 weeks. 🚀

*— Claude Opus 4.7, 2026-05-11 EOD*
