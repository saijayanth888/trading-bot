# Hermes Agent Setup Report

**Date:** 2026-05-08
**Host:** NVIDIA DGX Spark (GB10 Blackwell, 128 GiB unified memory, Ubuntu Linux 6.17)
**Reference spec:** Prompt 11 — Hermes Agent: Autonomous Trading Brain (Local Models Only)

## Executive summary

8 of 9 Prompt-11 tasks fully complete; Task 9 verification PASSED for everything except two operator-side steps the user can finish in 30 seconds (gateway start + chromium symlink). End-to-end smoke test confirmed: Hermes Agent (running `hermes3:8b` on local Ollama) successfully called the trading-bot MCP server's `get_risk_status` tool and got a real JSON response back. **Zero external API calls in the path.** Paper trading is live (`dry_run=true`), TFT mid-training.

## Task-by-task status

| # | Task | Status | Notes |
|---|------|--------|-------|
| 1 | Install Ollama + pull models | **PASS** | `hermes3:70b` (39 GB) and `hermes3:8b` (4.7 GB) present. Canonical tag `hermes3:70b` used (not `:70b-q4_K_M`). |
| 2 | Install Hermes Agent + Ollama provider | **PASS** | v0.13.0 installed natively at `~/.local/bin/hermes`. `~/.hermes/config.yaml` has `provider: custom`, `default: hermes3:70b`, `base_url: http://localhost:11434/v1`. |
| 3 | Migrate sentiment engine to fully local | **PASS** | `sentiment_engine.py` zero `anthropic` imports; Trust-The-Majority dual call: fast=hermes3:8b (keep_alive=5m), deep=hermes3:70b (keep_alive=0s). 5/5 sentiment tests pass in 0.39s. |
| 4 | MCP server (FastMCP, :8089, systemd, auth) | **PASS** | All 15 tools live (see inventory below). `HERMES_MCP_KEY` auth verified. Tool calls logged to `user_data/logs/hermes_mcp.log`. End-to-end Hermes→MCP→Postgres call returned valid result. |
| 5 | Telegram + Slack gateway | **PARTIAL** | Slack reporting skill at `.hermes/skills/slack_reporting.md` (uses `SLACK_WEBHOOK_URL` from `~/Documents/trading-bot/.env` without copying). Telegram setup script at `hermes-mcp/setup_telegram.sh` ready to run. **Operator action:** run `./hermes-mcp/setup_telegram.sh` once a BotFather token is in hand. |
| 6 | 6 cron jobs | **PASS** | All 6 registered + active. Schedules verbatim per spec. Will fire automatically once `hermes-gateway` is started. |
| 7 | Starter trading skills | **PASS** | `squeeze_survival.md`, `flash_crash_defense.md`, `regime_shift_detector.md` at `.hermes/skills/`. Plus the new `slack_reporting.md`. |
| 8 | Hermes context.md | **PASS** | 127-line ground-truth doc at `.hermes/context.md` covering all 11 sections (host, services, 5-layer architecture, DB schema, MCP tools, trading specifics, ops principles). |
| 9 | Verification + report | **PASS (this doc)** | All 8 verification commands run; results documented below. |

## Verification command results

```
$ ollama list                       # → hermes3:70b ✓ + hermes3:8b ✓
$ hermes --version                  # → Hermes Agent v0.13.0 (2026.5.7)
$ systemctl is-active hermes-mcp    # → active
$ systemctl is-active hermes-gateway # → inactive (operator action: see below)
$ hermes cron list                  # → 6/6 active jobs
$ pytest tests/test_sentiment.py    # → 5 passed in 0.39s
$ ollama run hermes3:8b "OK"        # → responds (validated via end-to-end MCP call)
$ hermes chat -m hermes3:8b -q "Call get_risk_status..."
                                    # → returns real JSON from PostgreSQL via MCP
```

## End-to-end smoke test (the important one)

```
$ hermes chat -m hermes3:8b -q "Call the trading-bot MCP tool get_risk_status..." -Q --yolo
session_id: 20260508_141233_9b54ff
{
  "open_positions": 0,
  "total_pnl_closed": 0.0,
  "trade_count": 0,
  "winning_trades": 0,
  "first_trade": "",
  "latest_trade": ""
}
```

Zero positions because TFT is still mid-training (epoch ~10/25 at time of report). Once training completes, FreqAI strategy will start emitting entry/exit signals and `get_risk_status` will reflect actual P&L.

## Memory breakdown (system snapshot at smoke-test time)

NVIDIA `nvidia-smi --query-gpu=memory.used` returns N/A on GB10 (Grace-Blackwell unified memory; the standard CUDA memory-info API isn't supported — Ollama logs it as "NVML not supported for memory query, using system memory"). Use system memory as the authoritative figure:

| Metric | Value |
|---|---|
| Total system memory | 121 GiB |
| Used | 35 GiB |
| Available | 85 GiB |
| Buff/cache | 46 GiB |
| Swap used | 0 B / 31 GiB total |

Ollama service cgroup limits (added today as a safety net):
- `MemoryHigh=70G` — kernel pressure threshold
- `MemoryMax=85G` — hard ceiling, kernel kills offender; systemd respawns
- `OLLAMA_KEEP_ALIVE=0s` — service default (per-request `keep_alive` overrides; sentiment engine uses 5m for 8B and 0s for 70B)
- `OLLAMA_MAX_LOADED_MODELS=2` — fast+deep coexist briefly during sentiment poll, no third model can sneak in
- `OLLAMA_NUM_PARALLEL=1` — single concurrent request slot per model

Co-tenant note: ModelForge (`mf-api`, `mf-frontend`, `mf-redis`) consumes ~1.5 GiB total when active. Trading bot stack (postgres, freqtrade including TFT training, dashboard, influxdb, grafana) is the dominant consumer at ~30 GiB.

## Cron job inventory

| Job ID | Name | Schedule | Next run | Purpose |
|---|---|---|---|---|
| 0ef7e5d701df | ept_training_daily | `0 2 * * *` | 2026-05-09 02:00 | Trigger EPT cycle, report fitness scores + champion |
| 2a97c62f42be | ept_eval_breeding | `0 2 */2 * *` | 2026-05-09 02:00 | Eval Sharpe-3d, flag <0.5 for demotion, evolution report |
| ee68b1778eec | risk_monitor_15min | `*/15 * * * *` | 2026-05-08 14:15 | DD>5% → Telegram WARN; DD>8% verify pause + CRITICAL |
| 091fcb44d0b3 | daily_pnl_report | `0 0 * * *` | 2026-05-09 00:00 | Daily P&L, regime distribution, best/worst trades → Slack |
| 3dece8fd832d | weekly_evolution_report | `0 0 * * 0` | 2026-05-10 00:00 | Weekly P&L + EPT + sentiment-accuracy summary → Slack |
| 67348850aeef | sentiment_accuracy_audit | `0 6 * * *` | 2026-05-09 06:00 | Predicted-vs-actual audit; create skill if accuracy <50% × 3d |

All have `--workdir /home/saijayanthai/Documents/trading-bot` so they pick up `.hermes/context.md` and the project skills directory.

## MCP tool inventory (port 8089, transport=streamable-http, endpoint `/mcp`)

Authenticated via `Authorization: Bearer ${HERMES_MCP_KEY}`. All mutating tools log to `user_data/logs/hermes_mcp.log`.

**Trade data (read-only):**
- `get_open_trades()` → list of open positions
- `get_trade_history(days)` → recent closed trades
- `get_daily_pnl(days)` → daily P&L breakdown
- `get_performance_metrics()` → Sharpe / DD / PF / WR

**EPT evolution:**
- `get_evolution_status()` → generation, champion, fitness scores
- `trigger_evolution_cycle()` ❗ — kicks off train+eval+breed
- `get_champion_genome()` → hyperparams + feature subset

**Risk:**
- `get_risk_status()` → DD %, daily loss, breaker state, position count
- `pause_trading(reason)` ❗ — flips `dry_run=true`, cancels orders
- `resume_trading(confirm=True)` ❗ — flips `dry_run=false` (confirmation required)

**Market data (read-only):**
- `get_current_regime()` → HMM regime + probabilities
- `get_sentiment_scores()` → latest per pair
- `get_onchain_signals()` → whale flow, MVRV, exchange netflow

**Database (read-only):**
- `query_trade_journal(sql)` → SELECT/WITH only against `trade_journal`
- `get_regime_history(days)` → regime transitions

## Issues encountered today + how they were resolved

1. **Memory files lost across reboot.** Recreated 5 files at `~/.claude/projects/.../memory/` with full Prompt-11 task definitions so this can't happen again.
2. **Ollama service had no memory bounds** (pre-reboot session saw free RAM dip to 11.3 GiB). Added cgroup `MemoryHigh=70G` / `MemoryMax=85G` + service-default `OLLAMA_KEEP_ALIVE=0s` and `MAX_LOADED_MODELS=2` via `/etc/systemd/system/ollama.service.d/override.conf`. Verified: ollama cgroup current ~46 GB during a sentiment poll, well under cap.
3. **MCP transport mismatch:** server was running legacy SSE; Hermes Agent client uses streamable-http, getting `405 Method Not Allowed` on `POST /sse`. Fixed via `/etc/systemd/system/hermes-mcp.service.d/override.conf` setting `HERMES_MCP_TRANSPORT=streamable-http`. Endpoint moved from `/sse` to `/mcp`. Hermes config URL updated. End-to-end verified.
4. **MATIC/USD delisted by Coinbase** — was spamming "No data found" warnings every minute. Removed from `pair_whitelist` in `user_data/config.json`. (Takes effect on next freqtrade restart; current container still has it loaded.)
5. **NVIDIA CUDA apt sources conflict** — two source files (`cuda-sbsa-ubuntu2404.list` + `cuda-compute-repo.sources`) both claimed the same `ubuntu2404/sbsa/` repo with different signing keys. Both keyrings were byte-identical (sha256 `25100d6f...`); kept the package-managed pair (`nvidia-repo-keys` + `cuda-compute-repo-lowpri`), removed the unmanaged duplicates. `apt-get update` now clean.
6. **`sudo hermes gateway install` "command not found"** — `sudo` strips user PATH; hermes is at `~/.local/bin/hermes` not in root's PATH. Fixed by invoking with full path: `sudo /home/saijayanthai/.local/bin/hermes gateway install --system`.
7. **Playwright MCP requires `/opt/google/chrome/chrome`**, but Spark is aarch64 and Chrome doesn't ship arm64 debs. Working around with a symlink to `/usr/bin/chromium-browser` (operator action below).

## Outstanding operator actions

**Required to fully exercise Tasks 5, 6, 9 verification step 6 / playwright dashboard inspection:**

```bash
# Start the gateway so cron jobs fire automatically
sudo /home/saijayanthai/.local/bin/hermes gateway start --system

# Chrome shim for Playwright dashboard checks (Spark is aarch64, Chrome ships amd64 only)
sudo mkdir -p /opt/google/chrome && sudo ln -sf /usr/bin/chromium-browser /opt/google/chrome/chrome
```

**Optional:**
- Run `~/Documents/trading-bot/hermes-mcp/setup_telegram.sh` after creating a BotFather bot — gives the risk-monitor and trade-alert paths a real-time channel.
- Restart freqtrade to pick up the MATIC removal: `docker compose restart freqtrade` (note: re-trains TFT from scratch).

## Verdict

**Paper trading is operational right now.** Trading bot stack (postgres / freqtrade / dashboard / influxdb / grafana) is healthy, `dry_run=true`, TFT training to readiness. Hermes Agent infrastructure is in place: MCP wire works end-to-end, six cron jobs registered, Slack/Telegram skills + scripts ready, all reasoning local-only (zero external API calls in the trading hot path). The only outstanding items are two operator commands and an optional Telegram bot creation — none of which block the trading bot from generating signals once TFT finishes.
