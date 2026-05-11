# NostalgiaForInfinity X6 — Activation Runbook

**Status:** Scaffold complete. Service is in `docker-compose.yml` but
**gated OFF** behind the `nfi` profile so `docker compose up` will not
start it. Five activation gates must pass before flipping `dry_run=false`.

**Expected $-impact:** $1,000-2,000 over 4 weeks at 1/4 of NFI's historical
performance (conservative baseline). Upside if it tracks history:
$4,000-8,000 over 4 weeks. Singularly hits the post-cutover 4-week target.

**Owner of this doc:** Whoever flips the activation. Update the gate
checkboxes as you go.

---

## 1. What's scaffolded today (2026-05-11)

| Component | Status | Notes |
|---|---|---|
| `user_data/strategies/NostalgiaForInfinityX6.py` | Present, byte-identical to upstream `iterativv/NostalgiaForInfinityX6.py` at clone time | `diff -u` returned 0 bytes. Tag v16.8.800 per `version()`. |
| `user_data/strategies/nfi_x6_config.json` | Patched for Coinbase USD | `dry_run=true`, wallet $50k, port 8090, sqlite trade DB, 12 USD pairs. |
| `docker-compose.yml` service `freqtrade-nfi` | Present, `profiles: ["nfi"]` (OFF) | Same image as main freqtrade. CPU-only (no GPU reservation). |
| MonitoringMixin wiring into the strategy | **DEFERRED** | See §4 below — wiring requires 5+ injection points in upstream code which would break the byte-identical activation gate. |
| Historical OHLCV download for the 12-pair whitelist | **Partial** | 5m/15m/1h/6h exist for 8 pairs. 4h + 1d missing. DOT, POL, LTC, ATOM have no data. Required before first backtest. |
| Smoke backtest | Ran clean, 0 trades (data unavailable) | Strategy class loads, deps import, no crash. Gate #3 not yet evaluable. |

## 2. The 5 activation gates (must ALL pass)

```
[ ] Gate 1 — File integrity
    diff -u /tmp/nfi-upstream/NostalgiaForInfinityX6.py \
            user_data/strategies/NostalgiaForInfinityX6.py
    Expected: 0 bytes. Any diff is a tampering risk — investigate before
    proceeding.  Re-run before every activation; tracks upstream changes
    so we know whether we're shipping a recent or stale revision.

[ ] Gate 2 — Dependencies importable in the freqtrade image
    docker exec freqtrade python3 -c "import rapidjson, pandas_ta, talib; \
        print(rapidjson.__version__, pandas_ta.version, talib.__version__)"
    Expected: rapidjson 1.23, pandas_ta 0.3.16, talib 0.6.8 (verified 2026-05-11).

[ ] Gate 3 — 2-year backtest passes thresholds
    docker exec freqtrade freqtrade download-data \
        --exchange coinbase \
        --pairs BTC/USD ETH/USD SOL/USD ADA/USD XRP/USD DOGE/USD \
                AVAX/USD LINK/USD DOT/USD POL/USD LTC/USD ATOM/USD \
        --timeframes 5m 15m 1h 4h 1d \
        --timerange 20240101-20260501

    docker exec freqtrade freqtrade backtesting \
        --strategy NostalgiaForInfinityX6 \
        --config /freqtrade/user_data/strategies/nfi_x6_config.json \
        --timerange 20240101-20260501 \
        --fee 0.003

    Required:
      Sharpe          > 1.4
      Max drawdown    < 12 %
      Profit factor   > 1.4
      Win rate        > 38 %
      Trades / month  > 30

    If fail: tune protections / signal flags / pair_whitelist. Do NOT
    paper-soak a failing backtest.

[ ] Gate 4 — Paper-soak 7 days minimum
    docker compose --profile nfi up -d freqtrade-nfi
    # Wait 7 days. dry_run stays true. Watch:
    #   - /api/ops/trades_risk (once dashboard wiring lands — see §4)
    #   - freqtrade REST API on 127.0.0.1:8090/api/v1/status
    #   - user_data/logs/freqtrade-nfi.log for errors
    Required: ≥ 1 trade / day average, no class errors in logs.

[ ] Gate 5 — Operator verbal GO
    Subjective but required. Operator reviews the soak metrics and
    explicitly authorises the dry_run → false flip.
```

## 3. Activation steps (run AFTER all 5 gates pass)

```bash
# 1. Flip dry_run in nfi_x6_config.json
#    Replace "dry_run": true with "dry_run": false
#    Verify execution.dry_run flag matches
$EDITOR user_data/strategies/nfi_x6_config.json

# 2. Re-validate JSON
python3 -c "import json; json.load(open('user_data/strategies/nfi_x6_config.json'))"

# 3. Verify Coinbase credentials exist (the entrypoint reads this JSON)
ls -la secrets/coinbase.json
# Permissions should be 0600. The file is mounted read-only into the container.

# 4. Stop the paper-soak container if running
docker compose --profile nfi stop freqtrade-nfi
docker compose --profile nfi rm -f freqtrade-nfi

# 5. Reset the paper trade DB so live trades don't inherit paper P&L history
docker exec freqtrade rm -f /freqtrade/user_data/tradesv3_nfi.sqlite

# 6. (Recommended) Promote the service to "permanent" — remove the
#    profiles: ["nfi"] gate so subsequent `docker compose up` brings
#    it back automatically. Edit docker-compose.yml and delete the
#    `profiles: ["nfi"]` line. Commit. Tag the commit nfi-activation.

# 7. Bring it up live
docker compose up -d freqtrade-nfi

# 8. Confirm
curl -fsS -u "$FREQTRADE_API_USER:$FREQTRADE_API_PASS" \
     http://127.0.0.1:8090/api/v1/status | jq .
# Expect: bot_running=true, dry_run=false, strategy=NostalgiaForInfinityX6

# 9. Set a 24h calendar reminder to review the first day's live trades
#    against the paper-soak baseline. Drift > 30% on win-rate or
#    avg-profit warrants a rollback per §5.
```

## 4. MonitoringMixin / dashboard wiring — DEFERRED

The dashboard's `/api/ops/trades_risk` and `/api/ops/trades_risk_summary`
endpoints read from the Postgres `trade_journal` table. The main
strategy (`FreqAIMeanRevV1`) writes there via `MonitoringMixin`.

NFI X6's trades will **not** land in `trade_journal` out of the box.
Plan §7 Option A was to mix `MonitoringMixin` into the NFI class:

```python
from modules.monitoring_mixin import MonitoringMixin
class NostalgiaForInfinityX6(MonitoringMixin, IStrategy):
    ...
```

Plus injecting:
- `self._init_monitoring(self.config)` in a new `bot_start()` method
- `self._record_trade_entry(...)` in `confirm_trade_entry()`
- `self._record_trade_exit(t, gov=gov)` in `confirm_trade_exit()` and
  in `bot_loop_start()` for the closed-trade-drain pass
- `self._maybe_write_hourly_snapshot(...)` and
  `self._maybe_send_daily_summary(...)` in `bot_loop_start()`

**Why deferred:** That's 5+ injection points in upstream NFI X6 code,
which breaks **Gate 1 (file integrity)**. We'd be shipping a patched
strategy and would lose the cheap "diff vs upstream = 0" verification.

**Mitigations available, ranked by effort:**

  1. **Use freqtrade's native REST API.** NFI runs its own freqtrade
     REST on `127.0.0.1:8090`. The dashboard already polls
     `127.0.0.1:8080` for the main bot — add a parallel poller for
     8090. Same auth, same payload shape, ~30 LOC in
     `user_data/dashboard/api/freqtrade_client.py`. No strategy edits.

  2. **Subclass NFI without editing it.** Create
     `user_data/strategies/NostalgiaForInfinityX6Monitored.py`:
     ```python
     from strategies.NostalgiaForInfinityX6 import NostalgiaForInfinityX6
     from modules.monitoring_mixin import MonitoringMixin
     class NostalgiaForInfinityX6Monitored(MonitoringMixin, NostalgiaForInfinityX6):
         def bot_start(self): self._init_monitoring(self.config); super().bot_start()
         # override confirm_trade_entry / confirm_trade_exit to call
         # super() then self._record_trade_*
     ```
     ~50 LOC, byte-identical NFI preserved, full journal integration.
     Recommended for the post-Gate-3 step.

  3. **Add a Postgres trade_journal_nfi table** keyed by NFI's sqlite
     trade IDs. More invasive — requires schema migration and dashboard
     query updates.

  4. **Wait for upstream NFI to accept an optional monitoring hook.**
     Slowest, zero local effort. Won't happen this quarter.

**Recommend option 2 once the operator wants dashboard visibility.**
Tracked separately; not a blocker for paper-soak Gate 4.

## 5. Rollback

If anything looks wrong during the first 48h live:

```bash
# Stop immediately, leave state on disk for forensics
docker compose stop freqtrade-nfi

# If trades opened that the operator doesn't want to keep:
#   1. Manually close them via /api/v1/forceexit or the Coinbase UI
#   2. THEN do the steps below

# Permanent rollback — restore profile gate, wipe trade DB
git revert <nfi-activation-commit>
docker compose --profile nfi rm -f freqtrade-nfi
rm user_data/tradesv3_nfi.sqlite

# Audit
cat user_data/logs/freqtrade-nfi.log | grep -iE "error|warning|failed"
```

If the issue is strategy-side (NFI itself misbehaving):
```bash
# Diff against the upstream we cloned today
diff -u /tmp/nfi-upstream/NostalgiaForInfinityX6.py \
        user_data/strategies/NostalgiaForInfinityX6.py
# If non-empty: someone edited the strategy file. Restore from upstream.
```

If the issue is infrastructure-side (Postgres / docker network):
- Main `freqtrade` keeps running; NFI is fully isolated. Stopping
  `freqtrade-nfi` cannot disrupt the main strategy.

## 6. Known gotchas captured during scaffolding

1. **MATIC/USD doesn't exist on Coinbase Advanced.** The Polygon ticker
   migrated to POL. The whitelist uses `POL/USD`. Doc §7 of the
   post-cutover plan still references MATIC — that's the doc, not us.

2. **NFI X6 derives the BTC informative pair from `stake_currency`.**
   With `"stake_currency": "USD"` in the scaffold config, the strategy
   automatically picks `BTC/USD` for the macro-risk-off detector
   (verified at lines 2543-2566 and 3462-3483 of the strategy). No
   strategy-file edit was needed for Coinbase USD.

3. **The Postgres `freqtrade` schema is shared.** Both freqtrade
   instances will attempt to write to the same Postgres user/database
   if `FREQTRADE__DB_URL` were used. The scaffold sidesteps this by
   using sqlite (`db_url` in the JSON points at
   `/freqtrade/user_data/tradesv3_nfi.sqlite`). When promoting to live,
   either keep sqlite or migrate to a new Postgres schema `nfi_trades`.

4. **NFI startup_candle_count = 800.** The first 4h * 800 = 133-day
   warm-up of indicators means the first backtest result is sparse
   for the first ~4 months. Use a longer `--timerange` start.

5. **Backtest needs 5 timeframes per pair: 5m, 15m, 1h, 4h, 1d.**
   The current dataset has only 5m, 15m, 1h, 6h for 8 pairs. Run
   `freqtrade download-data` for the missing 4 pairs (DOT, POL, LTC,
   ATOM) and the missing 2 timeframes (4h, 1d) before Gate 3.

## 7. Quick reference — port + path summary

| What | Where |
|---|---|
| NFI freqtrade REST API | `http://127.0.0.1:8090` |
| NFI log file | `user_data/logs/freqtrade-nfi.log` (host: `./user_data/logs/freqtrade-nfi.log`) |
| NFI trade DB | `user_data/tradesv3_nfi.sqlite` (host: `./user_data/tradesv3_nfi.sqlite`) |
| NFI config | `user_data/strategies/nfi_x6_config.json` |
| NFI strategy | `user_data/strategies/NostalgiaForInfinityX6.py` |
| Main freqtrade REST API | `http://127.0.0.1:8080` (unchanged) |
| Dashboard | `http://127.0.0.1:8081` (unchanged) |

---

*Scaffolded 2026-05-11 by Agent C as part of the post-cutover 4-week
strategy-stack expansion plan (POST_CUTOVER_FIXES_2026-05-11.md §7).*
