# Trading bot operations checklist

The day-to-day playbook. If something is broken, work top-down.

> **Source-of-truth files**
> - Architecture: `.hermes/context.md`
> - Setup record: `HERMES_SETUP_REPORT.md`
> - Strategy: `user_data/strategies/FreqAIMeanRevV1.py`
> - Config: `user_data/config.json`
> - Secrets: `.env` (gitignored)

---

## A. After-reboot startup check (≤ 2 min)

Run these in order. Stop at the first FAIL and fix before continuing.

```bash
# 1. System services up?
systemctl is-active ollama hermes-mcp hermes-gateway trading-bot.service
systemctl --user is-active hermes-dashboard.service
# All five should print: active

# 2. Containers healthy?
docker compose -f ~/Documents/trading-bot/docker-compose.yml ps
# postgres, freqtrade, dashboard, influxdb, grafana → all "healthy"

# 3. Memory in budget?
free -h | head -2
# Available should be ≥ 30 GiB. If < 15 GiB, see Section F.

# 4. Ports listening?
ss -tlnp | grep -E ':(5434|8080|8081|8086|8089|9119|3000|11434) '
# Expect 8 lines

# 5. Hermes can reach MCP?
hermes mcp list | grep trading-bot
# trading-bot ✓ enabled

# 6. Cron registered + gateway running?
hermes cron list | grep -c '\[active\]'   # → 6
systemctl is-active hermes-gateway        # → active

# 7. Paper trading is on?
grep '"dry_run"' user_data/config.json | head -1
# "dry_run": true,  ← must be true until validate_readiness.py passes
```

If 1–7 are green, you're operational. Open `http://localhost:9119` (Hermes UI), `http://localhost:8081` (TradingView dashboard), `http://localhost:3000` (Grafana).

---

## B. Daily monitoring (5 min, takes the morning coffee)

| Where | What to check | Threshold |
|---|---|---|
| Slack `:bar_chart:` daily report | Net P&L, Sharpe-30d, MaxDD-30d | DD < 8%, Sharpe trending up |
| `http://localhost:8081` | TFT confidence per pair, regime label | No pair stuck "no model ready" > 1h |
| `http://localhost:3000` | Grafana panels: order latency, sentiment lag, regime transitions | order_latency < 2s p95 |
| `hermes cron list` | All 6 jobs `[active]`, no `[paused]` | All 6 active |
| `tail -50 user_data/logs/hermes_mcp.log` | MCP tool calls succeeding | No repeated 5xx or auth errors |
| `docker compose logs --tail=100 freqtrade \| grep -i error` | Strategy errors | Empty (no recurring exception) |

If the daily Slack report hasn't arrived by 00:15 UTC, the gateway probably died. `systemctl restart hermes-gateway` and check `journalctl -u hermes-gateway --since '1 hour ago'`.

---

## C. Weekly tasks (Sunday)

1. **Read the weekly evolution Slack report** (fires Sun 00:00 UTC).
2. `python scripts/validate_readiness.py` — check progress toward go-live gate (Sharpe>1.5, MaxDD<12%, PF>1.4, WinRate>55%, ≥200 trades).
3. **Backup**: `bash scripts/backup.sh` — dumps Postgres + config snapshot to `~/trading-bot-backups/<date>/`.
4. **Review skills directory** `.hermes/skills/` — any new skills Hermes auto-created? Read them, keep the good ones, delete noise.
5. **Disk check**: `df -h /var/lib/docker /home` — Postgres hypertables grow ~50 MB/day; act if < 20 GB free.
6. **Model retrain pause window**: TFT auto-retrains every 24h, EPT every 2 days. If you see a stale model age > 36h, restart freqtrade.

---

## D. Emergency response

### D1. The bot opened a position you don't like / something feels wrong

Pause immediately — no questions, no debugging first:

```bash
# Via Hermes (preferred — logs the reason)
hermes chat -m hermes3:8b -q "Call pause_trading with reason='operator manual intervention'." -Q --yolo

# OR direct (if Hermes is down)
docker compose exec freqtrade curl -u $(grep -E '^FREQTRADE__API_SERVER__USERNAME' .env | cut -d= -f2):$(grep -E '^FREQTRADE__API_SERVER__PASSWORD' .env | cut -d= -f2) -X POST http://localhost:8080/api/v1/stop
```

Then investigate. To resume after fixing:
```bash
hermes chat -m hermes3:8b -q "Call resume_trading with confirm=True." -Q --yolo
```

### D2. Drawdown approaching 8% (risk governor will pause at 8%)

The `risk_monitor_15min` cron should have alerted on Slack/Telegram already. If you're investigating manually:

```bash
hermes chat -m hermes3:8b -q "Call get_risk_status. Then call get_open_trades. List the worst losing position." -Q --yolo
```

If the governor hasn't paused at 8% but DD is past it, **assume the governor failed** and pause manually (D1).

### D3. Flash crash detected (>5% in 60s on any pair)

`flash_crash_defense` skill kicks in automatically. If you got the Telegram CRITICAL alert and want to verify:

```bash
hermes chat -m hermes3:8b -q "Call get_open_trades and get_current_regime. For each open position, report whether emergency stops are set." -Q --yolo
```

### D4. Ollama OOM / system thrashing

Memory caps should prevent it (`MemoryMax=85G`). If they don't:
```bash
sudo systemctl restart ollama
ollama ps                    # both models should evict
free -h                      # available should jump back up
```

If recurring, lower `MemoryMax` in `/etc/systemd/system/ollama.service.d/override.conf` to e.g. `75G` and restart. ModelForge competing for memory? Pause its campaign.

### D5. MCP wire dead (Hermes can't reach trading-bot tools)

```bash
systemctl restart hermes-mcp
journalctl -u hermes-mcp --since '5 min ago' --no-pager | tail -20
hermes mcp list      # trading-bot must show ✓ enabled, all tools
```

If still broken: check `HERMES_MCP_TRANSPORT=streamable-http` in `systemctl show hermes-mcp -p Environment`. The endpoint is `/mcp` (NOT `/sse`).

### D6. Container unhealthy

```bash
docker compose ps                                        # which one?
docker compose logs --tail=200 <service> | grep -iE 'error|fatal'
docker compose restart <service>                         # try restart first
docker compose down && docker compose up -d              # nuclear option
```

`postgres` is the one to never lose — the trade journal is its only home. Backup before any nuclear action: `docker compose exec postgres pg_dump -U tradebot tradebot > /tmp/trade-bot-emergency-$(date +%s).sql`.

---

## E. Common operations

| What | Command |
|---|---|
| Restart strategy with new config | `docker compose restart freqtrade` (loses TFT training in-progress) |
| Restart sentiment poll only | not separable — sentiment runs inside freqtrade |
| Trigger EPT cycle on demand | `hermes chat -m hermes3:8b -q "Call trigger_evolution_cycle." -Q --yolo` |
| Read trade journal directly | `docker compose exec postgres psql -U tradebot -d tradebot -c "SELECT * FROM trade_journal ORDER BY exit_time DESC LIMIT 10;"` |
| View live freqtrade output | `docker compose logs -f freqtrade` |
| Stop the whole stack | `sudo systemctl stop trading-bot.service` (clean: runs `compose down`) |
| Start fresh after bug fix | `sudo systemctl restart trading-bot.service` |
| Update Hermes Agent | `hermes update` (NOT during market-active hours) |

---

## F. Going live (paper → real money)

**Do not bypass these steps.**

1. `python scripts/validate_readiness.py` returns `READY`. Required: Sharpe>1.5, MaxDD<12%, PF>1.4, WinRate>55%, ≥200 trades.
2. Manual review of the last 50 trades — read 10 random ones in `trade_journal`. Are entries / exits making sense per the regime + sentiment context logged?
3. EPT champion lineage stable for ≥7 days (no champion flip in the last week).
4. Backup the paper-trade dataset (`bash scripts/backup.sh`) — this is your reference distribution.
5. **Graduated deployment**, time-gated AND PnL-gated:
   - Stage 1: `tradable_balance_ratio: 0.10` — run for 7 days. Net P&L must be ≥ 0.
   - Stage 2: `tradable_balance_ratio: 0.30` — run for 14 days. Sharpe must hold ≥ 1.0.
   - Stage 3: `tradable_balance_ratio: 0.50` — run for 21 days. MaxDD must stay < 8%.
   - Stage 4: `tradable_balance_ratio: 0.99` — only if all prior stages passed without governor pauses.
6. Set `dry_run: false` in `user_data/config.json`, then `sudo systemctl restart trading-bot.service`.
7. Send Slack message announcing go-live (manual, deliberate — not automated).

If any stage's exit gate fails, **rollback immediately**: `bash scripts/auto_rollback.sh` (sets `dry_run: true`, cancels open orders, reverts `tradable_balance_ratio` to the prior step's value).

---

## G. What everything is + where it lives

| Service | URL/path | Auto-starts | Owner |
|---|---|---|---|
| Trading-bot stack | `~/Documents/trading-bot/docker-compose.yml` | `trading-bot.service` (system) | docker compose v2 |
| Postgres / TimescaleDB | `localhost:5434`, db `tradebot` + `freqtrade` | yes (compose) | container |
| Freqtrade engine + FreqAI | `localhost:8080` (REST/WS) | yes (compose) | container |
| Trading dashboard | `http://localhost:8081` | yes (compose) | container |
| InfluxDB | `localhost:8086` | yes (compose) | container |
| Grafana | `http://localhost:3000` | yes (compose) | container |
| Ollama | `localhost:11434` | `ollama.service` (system) | host |
| Hermes MCP server | `http://localhost:8089/mcp` | `hermes-mcp.service` (system) | host venv |
| Hermes gateway | (no port — fires crons) | `hermes-gateway.service` (system) | host |
| Hermes UI / dashboard | `http://localhost:9119` | `hermes-dashboard.service` (user, lingered) | host |

| Logs | Location |
|---|---|
| Strategy + FreqAI | `docker compose logs freqtrade` |
| MCP tool calls | `~/Documents/trading-bot/user_data/logs/hermes_mcp.log` |
| Hermes MCP server | `journalctl -u hermes-mcp` |
| Hermes gateway | `journalctl -u hermes-gateway` |
| Hermes dashboard | `journalctl --user -u hermes-dashboard` |
| Hermes chat sessions | `~/.hermes/sessions/session_*.json` |
| EPT generations | `user_data/logs/evolution.json` |
| Slack alerts (the ones the bot sent) | `docker compose exec postgres psql -U tradebot -c "SELECT * FROM slack_alert_log ORDER BY ts DESC LIMIT 20;"` |

| Secret | Where it lives | Used by |
|---|---|---|
| `HERMES_MCP_KEY` | `.env` | hermes-mcp service auth |
| `MCP_TRADING_BOT_API_KEY` | `~/.hermes/.env` | Hermes Agent → MCP bearer |
| `SLACK_WEBHOOK_URL` | `.env` | bot alerts + Hermes cron Slack reports (sourced via skill) |
| `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID` | placeholder; `setup_telegram.sh` to populate | Hermes risk-monitor cron |
| `POSTGRES_PASSWORD` | `.env` | freqtrade + MCP server |
| Coinbase Advanced API key | `.env` (CCXT JSON or env vars) | execution_engine |
| `PERPLEXITY_API_KEY` | `.env` (optional) | sentiment news fetcher |

---

## H. Sanity rules (non-negotiable)

1. **Never bypass the risk governor.** If you think it's wrong, alert the operator (yourself) and pause; do not work around it.
2. **`dry_run: true` until the readiness gate.** No exceptions.
3. **Don't edit `config.json` by hand for live changes** — go through the MCP `pause_trading` / `resume_trading` tools so the change is logged and atomic.
4. **Never start a second freqtrade against the same Postgres.** It corrupts the journal. (`trading-bot.service` is the only one allowed to start it.)
5. **Keep ModelForge in mind.** It's a co-tenant on this box. If trading-bot is starving for memory, check whether ModelForge has a campaign running and coordinate.
6. **Never push secrets to git.** `.env` is gitignored. If you ever see a token in the repo, rotate it immediately.
