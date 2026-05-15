# Cron + Notification Surface Audit
**Date:** 2026-05-14 night (audit ts ~2026-05-15T00:55Z)
**Mode:** READ-ONLY — no jobs triggered, no locks cleared, no scripts edited.
**Scope:** host crontab (3 entries) + Hermes scheduler (`~/.hermes/cron/jobs.json`, 34 jobs) + 17 logs + 7 state files + GPU gate.

---

## P0 — fix tonight (production-impacting)

**None.** No job is failing in a way that has hard-broken trading or alerting in the last 24h. All "stale" jobs are either weekly cadences (haven't fired yet) or known-disabled (freqtrade decommission).

## P1 — fix soon (degraded but operating)

1. **`stocks_day_runner.cron.log` directory log path is missing.** New crontab entry (added today) writes to `/home/saijayanthai/Documents/trading-bot/stocks/memory/stocks_day_runner.cron.log`, but that file does not exist yet (`ls: cannot access` — confirmed). The dir `stocks/memory/` exists; cron will create the file on first 09:15 ET fire (Mon 2026-05-18, since today is Thu past 09:15). Not broken, just unverified end-to-end. The script itself (`scripts/stocks_day_runner.sh`, 4610 bytes, exec bit set) is in place; `/tmp/stocks_day_runner.lock` is not present (good — no stale lock).

2. **`stocks snapshot UNTRUSTED` (age=13615s ≈ 3.78h) on `risk_monitor_15min` last 2 ticks.** The ACT line literally says *"CHECK wheel_snapshot cron — stocks data dark."* `wheel_snapshot` last ran 2026-05-14T16:59:41 ET = 3.96h stale. Its schedule is `*/1 9-16 * * 1-5` so it stopped on schedule at the 16:00 ET market close — this is **expected** outside US session, but the risk_monitor downstream alert reads it as `UNTRUSTED`. Either: (a) widen the trust window to allow stale-after-close, or (b) suppress the UNTRUSTED tag outside 09:00–16:30 ET. Operator gets a SOFT-pinged Slack at every state-change after close.

3. **`weekly_evolution_report` (age 115h) and `post_mortem_weekly` (age 114h)** — both schedule `0 0–1 * * 0` (Sunday). Today is Thursday → next fire Sun 2026-05-17. Stale-by-design but flagged because it brushes the >2× cadence flag (168h cadence ÷ 2 = 84h; we're at 115h, so >2/3 of one full week). Nothing to do; will self-heal Sunday.

## P2 — hygiene

1. **`ept_training_daily` is `enabled: false`** (last fire 2026-05-12, 67h stale). Confirms session-2026-05-12 EOD note that EPT is parked. Job sits in jobs.json disabled — leave or remove.

2. **Three jobs have never run** (`last_run_at: null`):
   - `capital_rebalance_14d` — schedule expr is `null` (no cron expression). Lives only as a manual-trigger job. Either give it a cron or delete.
   - `shark_kb_refresh` — `0 11 * * 6` (Sat). Was added since last Saturday → expected.
   - `wheel_sell_calls` — `0 11 * * 1` (Mon). Expected Mon.
   - `shark_weekly_review` — `0 10 * * 6` (Sat). Expected Sat.

3. **Historical psycopg ImportError block** in `cron-daily-pnl.log`, `cron-market-research.log`, `cron-sentiment-audit.log`. Pattern was: cron Python interpreter missing the trading-bot venv. Fixed today (`daily_pnl_report.sh.bak-pre-FIX-F-20260514T164739Z` is the pre-fix backup; current script picks up `ML_ENV_PYTHON`). Subsequent runs all show `slack status=200` or clean output. **Not actionable** — historical residue.

4. **`<stdin>:113: DeprecationWarning datetime.utcnow()`** repeated in every market_research run and most risk_monitor runs. Not breaking — Python 3.13 will. Cosmetic noise in cron logs.

5. **`cryptocurrency_cv: HTTP 403`** — recurring in `cron-sentiment-refresh.log` every 15min. One source dead; aggregator shrugs and continues with reddit/stocktwits/hackernews. Either remove the source or add a back-off.

6. **`_jobs_zsynyjvf.tmp` (0 bytes)** in `~/.hermes/cron/`. Looks like an orphan from an interrupted save. Safe to delete (don't auto-do it).

## P3 — observation only

- **GPU gate is idle** since 2026-05-12T14:47 (yield log untouched 2.5d). Nothing scheduled mid-week; next reservation Sun 14:00–18:00 ET as designed. `gpu_gate.sh` is in place, exec bit set.
- **Hermes errors.log** had a `telegram.error.NetworkError` burst at 2026-05-14 11:58 (DNS hiccup), self-healed via fallback IP `149.154.166.110`. Three MCP-trading-bot reconnect retries at 14:51 also recovered. No looping errors.
- **Backup** ran 2026-05-14 03:00, verified OK (4.9G). Next daily at 03:00 tomorrow.
- **Sentiment engine** `score=+0.00 conf=0.00` for hours — Perplexity returns items but downstream scoring is flatlined. Possibly intentional during quiet market, possibly a model-init issue. Outside this audit's scope; flag for sentiment audit.
- **Telemetry counts (last 24h, cron-risk-monitor.log):** 75 Slack posts, 248 suppressed → suppression saving ~77% of would-be noise. **(cron-market-research.log)**: 23 Slack posts, 8 suppressed → ~26% suppression (this one is mostly actionable).

---

## Notification suppression gates — VERIFIED

Both scripts re-read; gates intact:

- **`risk_monitor_15min.sh`** line 189: `print("*[risk_monitor_15min]* state OK and unchanged — skipping Slack post", file=sys.stderr)` — uses `file=sys.stderr` to keep stdout silent so Hermes does not auto-deliver. State file `~/.hermes/state-snapshots/risk_monitor_last.json` written, current content `{"ts":"2026-05-15T00:46:36.716807Z","combined_drawdown_pct":2.193,"circuit_breaker_active":false,"sev":"SOFT"}` — sane.
- **`market_research_30min.sh`** line 210: `print("*[market_research_30min]* non-actionable — skipping Slack", file=sys.stderr)` — same stderr trick. State file content `{"ts":"2026-05-15T00:47:09.156611Z","fg_value":43,"regime":"mean_reverting","llm_score":0.0}` — sane.

Both gates are firing in production (counts above). The fix shipped today is working.

## Webhook env var presence (no values printed)

- **`SLACK_WEBHOOK_URL`** present in `/home/saijayanthai/Documents/trading-bot/.env` (1 occurrence). All cron scripts source this env via `source $REPO/.env`.
- **`TELEGRAM_BOT_TOKEN`**, **`TELEGRAM_CHAT_ID`**, **`TELEGRAM_HOME_CHANNEL`** present in trading-bot `.env`.
- **`TELEGRAM_BOT_TOKEN`**, **`TELEGRAM_CHAT_ID`** also present in `~/.hermes/.env` (Hermes side). Plus `ANTHROPIC_API_KEY`, `ANTHROPIC_TOKEN`, `HF_TOKEN`, `MCP_TRADING_BOT_API_KEY`.
- All `deliver: telegram` jobs have credentials; all scripts that post Slack have `SLACK_WEBHOOK_URL` available.

## Cross-check: new wheel_snapshot crontab entry

```
15 9 * * 1-5 /usr/bin/flock -n /tmp/stocks_day_runner.lock \
  /home/saijayanthai/Documents/trading-bot/scripts/stocks_day_runner.sh \
  >> /home/saijayanthai/Documents/trading-bot/stocks/memory/stocks_day_runner.cron.log 2>&1
```

- Crontab entry **CONFIRMED present** (`crontab -l`).
- Script **EXISTS** (`/home/saijayanthai/Documents/trading-bot/scripts/stocks_day_runner.sh`, 4610 bytes, exec bit `-rwxrwxr-x`).
- Lock dir `/tmp/` exists; no stale `stocks_day_runner.lock` present.
- Log dir `/home/saijayanthai/Documents/trading-bot/stocks/memory/` exists (writeable). Log file does **not** exist yet — will be created on first 09:15 ET fire.
- Note: there's also a Hermes-side `wheel_snapshot` job (id `e540b1aa4a1b`, schedule `*/1 9-16 * * 1-5`, fires every minute during US session) — this is the "live snapshot" job. The new crontab entry is the **day-runner orchestrator** that runs once at open. Two different things, named confusingly.

---

## Job health matrix (34 jobs)

| Name | Enabled | Schedule | Last run (age) | Status | Deliver | Notes |
|---|---|---|---|---|---|---|
| ept_training_daily | NO | `0 2 * * *` | 67h | ok | telegram | Disabled (parked) |
| risk_monitor_15min | yes | `*/15 * * * *` | 11min | ok | telegram | Suppression gate working |
| daily_pnl_report | yes | `0 0 * * *` | 8h | ok | telegram | psycopg fix shipped today |
| weekly_evolution_report | yes | `0 0 * * 0` | 115h | ok | telegram | Sunday cadence — expected |
| sentiment_accuracy_audit | yes | `0 6 * * *` | 15h | ok | telegram | psycopg fix shipped today |
| ept_eval_breeding | NO | (none) | 71h | ok | local | Manual-only |
| capital_rebalance_14d | yes | (none) | never | — | telegram | **No cron expr — never fires** |
| post_mortem_weekly | yes | `0 1 * * 0` | 114h | ok | telegram | Sunday cadence |
| market_research_30min | yes | `*/30 * * * *` | 26min | ok | telegram | Suppression gate working |
| shark_kb_update | yes | `30 21 * * 1-5` | 23h | ok | local | Healthy |
| shark_kb_refresh | yes | `0 11 * * 6` | never | — | local | Saturday cadence |
| wheel_snapshot (Hermes) | yes | `*/1 9-16 * * 1-5` | 4h | ok | local | Off-hours — expected |
| wheel_candles | yes | `*/5 9-16 * * 1-5` | 4h | ok | local | Off-hours — expected |
| wheel_sell_csps | yes | `0 11 * * 1-5` | 10h | ok | telegram | Healthy |
| wheel_profit_take | yes | `0 10,14 * * 1-5` | 7h | ok | telegram | Healthy |
| wheel_sell_calls | yes | `0 11 * * 1` | never | — | telegram | Monday cadence |
| shark_pre_market | yes | `0 9 * * 1-5` | 12h | ok | telegram | Healthy |
| shark_market_open | yes | `35 9 * * 1-5` | 11h | ok | telegram | Healthy |
| shark_midday | yes | `0 13 * * 1-5` | 8h | ok | telegram | Healthy |
| shark_daily_summary | yes | `30 17 * * 1-5` | 3h | ok | telegram | Healthy |
| shark_weekly_review | yes | `0 10 * * 6` | never | — | telegram | Saturday cadence |
| ollama_health | yes | `*/5 * * * *` | 2min | ok | local | Healthy |
| stocks_ml_train | yes | `0 23 * * 0` | 75h | ok | telegram | Sunday cadence |
| shark_briefing_alerts | yes | `15 9 * * 1-5` | 8h | ok | telegram | Healthy |
| shark_pre_execute | yes | `30 9 * * 1-5` | 11h | ok | telegram | Healthy |
| stocks_tft_smoke | yes | `30 8 * * 1-5` | 12h | ok | local | Healthy |
| shark_override_verify | yes | `45 9 * * 1-5` | 11h | ok | telegram | Healthy (3 stalled-runs noted in state) |
| nightly_reflector | yes | `0 21 * * *` | 24h | ok | telegram | Healthy |
| modelforge_ingest | yes | `30 21 * * *` | 23h | ok | local | Healthy |
| modelforge_curate | yes | `0 22 * * *` | 23h | ok | local | Healthy |
| sentiment_refresh | yes | `*/15 * * * *` | 11min | ok | none | cryptocurrency_cv 403s repeating |
| archive_shark_memory | yes | `0 21 * * *` | 24h | ok | none | Healthy |
| hmm_refit_daily | yes | `0 22 * * *` | 3h | ok | telegram | Healthy (ran ahead at 18:00) |
| parity_oracle_5min | yes | `*/5 * * * *` | 2min | ok | telegram | Healthy (~22 written / tick) |

**Host crontab (3 entries):**

| Schedule | Job | Status |
|---|---|---|
| `0 3 * * *` | trading-bot/scripts/backup.sh daily | last 2026-05-14 03:00, verify OK |
| `0 4 * * 0` | trading-bot/scripts/backup.sh weekly | Sunday cadence |
| `15 9 * * 1-5` | stocks_day_runner.sh (NEW) | First fire Mon 2026-05-18 09:15 ET |
| (disabled) | ~~`*/5 * * * *` auto_rollback.py~~ | Commented out 2026-05-14 (freqtrade decommission) |

---

## Summary counts

- **Jobs total:** 34 (Hermes) + 3 (host crontab, 1 disabled) = 37
- **Enabled & healthy:** 28
- **Enabled but never-run (cadence pending):** 4 (`capital_rebalance_14d` is the only one with `null` expr — others are weekend cadences)
- **Disabled:** 2 (`ept_training_daily`, `ept_eval_breeding`)
- **Logs audited:** 17 (10 cron-* + backup, ept_cron, regime, regime_refit, sentiment, ollama_health, agent.log/errors.log/gateway.log)
- **State files audited:** 5 active + 3 pre-update snapshot dirs
- **P0/P1/P2/P3 totals:** 0 / 3 / 6 / 5
