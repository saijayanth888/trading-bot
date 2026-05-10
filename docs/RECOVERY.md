# Production Recovery Procedures

Last updated: 2026-05-10. Pilot start: 2026-05-11. Real-money go-live target: 2026-06-07.

This file lists every production failure mode the trading bot has guards for,
and the exact recovery steps. Skim it once now; come back when something fires.

---

## 1. Ollama unreachable (LLM provider down)

**Symptoms**
- Slack `[CRITICAL] Ollama unreachable` alert
- Dashboard `/ops` LLM-health card shows red `UNHEALTHY` pill
- `/api/ops/circuit_breakers` shows `failover_active: true`
- Slack `LLM failover active — paying for Anthropic` alerts
- Anthropic spend appears on the LLM stats card

**Diagnose**
```bash
sudo systemctl status ollama
curl http://localhost:11434/api/tags                 # endpoint alive?
nvidia-smi                                            # GPU memory free?
journalctl -u ollama --since "10 minutes ago"        # tail logs
ollama list                                           # all required models pulled?
```

**Fix**
```bash
sudo systemctl restart ollama
# Wait ~20s for warm-up
ollama list                                           # confirm models present
ollama pull hermes3:70b                               # if missing
ollama pull hermes3:8b                                # if missing
ollama pull qwen2.5:72b-instruct                      # if currently the deep model
```

**Auto-recovery**: the circuit breaker auto-transitions OPEN → HALF_OPEN
60s after the last failure. The next shark call probes Ollama; success
closes the breaker. **Do nothing else** unless the alerts continue past
the next cron tick.

---

## 2. Combined kill switch tripped

**Symptoms**
- `stocks/memory/KILL.flag` exists
- Crypto trading paused (config `dry_run` flipped if it was `false`)
- Dashboard hero shows `BREAKER ACTIVE` on the combined-portfolio card
- Slack `[CRITICAL] combined_drawdown ≥ 10%` alert

**Investigate first** (do NOT clear without checking)
```bash
cat ~/Documents/trading-bot/stocks/memory/KILL.flag           # the reason
PYTHONPATH=~/Documents/trading-bot python3 -c '
from user_data.modules.unified_risk import get_combined_risk_status
import json; print(json.dumps(get_combined_risk_status(), indent=2))'
```

Check the dashboard combined-portfolio card. If the drawdown is real
(>10% combined), DO NOT manually resume — let positions close out and
re-evaluate strategy. If it's a false alarm (e.g. data feed glitch
moved peak artificially), proceed.

**Manual reset (after confirming false alarm)**
```bash
rm ~/Documents/trading-bot/stocks/memory/KILL.flag
curl -X POST http://localhost:8081/api/ops/resume \
     -H 'Content-Type: application/json' -d '{"confirm": true}'
```

---

## 3. Both LLM providers failing

**Symptoms**
- Slack: `BOTH PROVIDERS DOWN. Ollama: open, Anthropic: open` alerts
- shark phases raising `RuntimeError: BOTH PROVIDERS FAILED`

**Action**: there is nothing to fall back to. Manual intervention required:

```bash
# 1. Check Anthropic side
curl -fsS https://api.anthropic.com/v1/health 2>&1 | head    # external status
echo "Status page: https://status.anthropic.com"

# 2. Check Ollama (per Section 1)

# 3. If both genuinely down: pause trading until at least one recovers
curl -X POST http://localhost:8081/api/ops/pause \
     -H 'Content-Type: application/json' \
     -d '{"reason":"both_llm_providers_down"}'
```

When at least one provider recovers, the breakers auto-close on the next
successful probe. Resume trading with the same `/api/ops/resume` call as
Section 2.

---

## 4. Circuit breaker stuck OPEN despite Ollama looking healthy

**Symptoms**
- `/api/ops/ollama_health` shows healthy=true, latency reasonable
- But `/api/ops/circuit_breakers` shows `state: open` for `ollama:fast` or `:deep`
- Anthropic still receiving traffic

**Why this happens**: the breaker is in OPEN state with the recovery
timer running. The HALF_OPEN probe hasn't fired yet because no shark
phase has run since the timeout elapsed.

**Force-reset (only if you've confirmed Ollama is genuinely healthy)**
```bash
ls /tmp/shark-cb-*.json                                # see which breakers exist
rm /tmp/shark-cb-ollama_fast.json                      # clears fast tier
rm /tmp/shark-cb-ollama_deep.json                      # clears deep tier
# Next shark call starts fresh CLOSED.
```

---

## 5. Postgres / TimescaleDB connection lost

**Symptoms**
- Dashboard `/ops` shows red badges across most cards
- `dashboard.ops_routes` ERROR logs: `psycopg.errors.OperationalError`
- /api/ops/trades_risk returns 500 or empty data

**Diagnose**
```bash
docker ps | grep postgres
docker logs --tail 100 tradebot-postgres
docker exec tradebot-postgres psql -U tradebot -d tradebot -c "SELECT 1"
```

**Fix**
```bash
docker compose restart postgres
# Wait for healthy:
docker ps --filter name=tradebot-postgres --format '{{.Status}}'
# Then restart things that depend on it:
docker compose restart freqtrade dashboard
```

---

## 6. Freqtrade regime detector silently wedged (covariance bug)

**Symptoms**
- `/api/ops/regime` row stays at the same `ts` for hours (>2-3 hours mid-day)
- Bot blocks all entries because regime feature is stale or stuck

**This was caught and fixed 2026-05-10** (commit `6b8327a`) but listing
in case the model file gets corrupted again.

**Force-refit**
```bash
docker exec freqtrade python3 -c "
import sys; sys.path.insert(0, '/freqtrade/user_data')
from modules.regime_detector import RegimeDetector
det = RegimeDetector.instance()
det.refit()
print('refit OK')
"
docker restart freqtrade   # so the running thread reloads from fresh disk model
```

---

## 7. Hermes-MCP service crash-looping

**Symptoms**
- `systemctl status hermes-mcp` shows `activating (auto-restart)` repeatedly
- `journalctl -u hermes-mcp` shows `status=203/EXEC` (binary missing) or stack traces

**Most common cause**: the venv at `~/Documents/trading-bot/hermes-mcp/venv/`
got deleted (e.g. accidental `rm -rf`).

**Fix**
```bash
cd ~/Documents/trading-bot/hermes-mcp
python3 -m venv venv
./venv/bin/pip install -r requirements.txt
sudo systemctl restart hermes-mcp
sudo systemctl status hermes-mcp
```

---

## 8. Wheel CSP cron didn't fire on Friday

**Symptoms**
- Friday 11:30 AM ET passed, `/api/ops/stocks` shows no new wheel position
- Slack quiet (no Telegram delivery from the cron)
- `~/.hermes/cron-shark-sell-csps.log` shows last run as last week

**Diagnose**
```bash
hermes cron list | grep -A 4 wheel_sell_csps
# Expected: Schedule "0 11 * * 5", Last run within last hour
```

**Common causes**
- Hermes cron daemon paused or restarted recently
- Kill switch active (check `stocks/memory/KILL.flag`)
- Per-ticker kill flag (check `stocks/wheel/state/kill_flags.json`)
- Insufficient buying power (check Alpaca account)

**Manual fire** (only after confirming none of the above)
```bash
cd ~/Documents/trading-bot/stocks
source venv/bin/activate
python -m wheel.cli sell-csps
```

---

## 9. shark phase fired but produced no entries despite green gates

**Symptoms**
- `cron-shark-market_open.log` shows the phase ran
- LLM-stats card shows shark calls happened
- But no entries placed; live-trades hero strip empty

**Diagnose**
- Check the gates matrix `/ops` card for the specific pair
- Read `stocks/memory/SIGNAL-LOG.md` for the agent decisions
- Check `stocks/memory/LESSONS-LEARNED.md` for blocking rules

**Most common reason**: the bull/bear/risk debate produced low confidence
or NO_TRADE consensus. That's the system working as designed — agents
declined the trade. Do not override.

---

## 10. Real-money go-live decision (2026-06-07)

Before flipping `dry_run: false` in `user_data/config-private.json`:

```bash
# Run the full verification suite — must be all green
bash ~/Documents/trading-bot/scripts/verify_production.sh

# Sanity-check the 4-week paper data
docker exec tradebot-postgres psql -U tradebot -d tradebot -c "
  SELECT COUNT(*) AS trades,
         COUNT(*) FILTER (WHERE pnl > 0) AS wins,
         AVG(pnl) AS avg_pnl,
         MIN(closed_at) AS first, MAX(closed_at) AS last
  FROM trade_journal WHERE closed_at IS NOT NULL"

# Confirm no silent failures
docker logs freqtrade --since 28d 2>&1 | grep -iE "predict cycle crashed|hmm|silent" | head
```

**Hard gates** (any RED → DO NOT GO LIVE):
- ≥4 weeks paper trading completed
- ≥10 closed trades total (not just signals — actually executed)
- Win rate ≥40%
- No HMM crashes / silent failures in last 7 days
- Combined drawdown stayed under 8% the whole pilot
- Wheel completed at least 4 weekly cycles without manual intervention
- All circuit breakers showed expected behaviour (opened on real Ollama
  hiccups, recovered automatically)
