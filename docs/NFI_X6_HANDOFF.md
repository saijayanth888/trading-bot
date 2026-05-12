# HANDOFF — `stage/22-nfi-backtest-and-activate`

**Owner of next session:** whoever resumes the NFI X6 paper-soak push.
**Read first:** [`docs/NFI_X6_ACTIVATION_2026-05-11.md`](docs/NFI_X6_ACTIVATION_2026-05-11.md) (the runbook), then [`docs/NFI_X6_BACKTEST_REPORT_2026-05-11.md`](docs/NFI_X6_BACKTEST_REPORT_2026-05-11.md) (this run's measured result).

---

## Bottom line

NFI X6's 2-year backtest **passed** with strong margins. The
activation `docker compose --profile nfi up -d --no-deps freqtrade-nfi`
**ran successfully** but exposed a previously-undocumented blocker:
**Coinbase REST has no native 4h candle**, and NFI X6 hard-requires
4h informative pairs. The container came up healthy and accepted
REST traffic, but the very first indicator pass failed for all 8
pairs with `KeyError('date')`. **NFI was rolled back within 2 minutes
of activation. No trades opened. No DB writes occurred. The main
bot was never affected.**

Next session: pick a remediation for the 4h gap (see
"Next-session decisions" below), re-attempt activation, then run
the actual 7-day paper soak.

---

## Result of each gate

| Gate | Status | Measured | Notes |
|---|---|---|---|
| **1 — Byte-identical to upstream** | PASS | sha256 `0791763a0f4292dd6452081b446cebe6bab395c5adfa3c33cc7041befc02ad6f` matches `iterativv/NostalgiaForInfinity@main:NostalgiaForInfinityX6.py` | Tag `v16.8.800` referenced in the runbook does NOT exist as a git tag (highest v16.8 tag upstream is `v16.8.260`); the version string is set in-file ahead of tagging. The diff against `main` is what matters. |
| **2 — Deps importable** | PASS | rapidjson 1.23, pandas_ta 0.3.16, talib 0.6.8 in `trading-bot/freqtrade:local` | Matches runbook's expected versions. |
| **3 — 2-year backtest quality** | PASS (4/4 task-spec gates) | Sharpe 1.90 (>1.4), max-DD 8.33% (<12%), PF 18.72 (>1.4), 94 trades (≥30) | Backtest needed offline 1h→4h resampling first (Coinbase has no 4h). 24-month timerange 20240501-20260501. +40.05% return, CAGR 18.34%, Sortino 3.49, Calmar 12.62. 98.9% win-rate (93W/1L) — the single loss is a boundary force-exit, not real signal degradation. |
| **3 (runbook extras)** | 1/2 PASS | Win rate 98.9% (PASS); trades/month 3.92 (FAIL >30) | The trades/month threshold is wrong for a swing-style strategy with 7-day average winner duration. Task spec correctly drops it. |
| **4 — Paper soak preflight** | BLOCKED | Live DataProvider returns empty 4h DataFrames for all 8 pairs → strategy `KeyError('date')` every cycle | Discovered during the activation step. Coinbase REST exposes 5m/15m/1h/6h/1d only; ccxt confirms `coinbase().timeframes` does not include `4h`. Strategy hard-requires 4h. |
| **5 — Operator GO** | NOT REACHED | — | Cannot be evaluated until Gate 4 paper-soak yields meaningful telemetry. |

## Activation status: NFI is OFF

```
$ docker ps --filter name=freqtrade-nfi --format '{{.Names}}: {{.Status}}'
(empty)
```

The activation attempt + rollback timeline is recorded in
[`docs/NFI_X6_ACTIVATION_LOG.md`](docs/NFI_X6_ACTIVATION_LOG.md).

## Files this branch touched

| File | Change | Why |
|---|---|---|
| `scripts/nfi_x6_gate_check.sh` | new | One-command runner for gates 1+2 (`--dry-run`) or 1+2+3 (default). Re-runnable; the `--dry-run` mode skips the 12-min backtest. |
| `scripts/nfi_x6_parse_backtest.py` | new | Parses the backtest result zip → markdown gate evaluation or `--json`. Uses wallet-based daily-balance metrics for Sharpe/Sortino/DD (closed-trades versions degenerate when win rate ≈ 100%). |
| `docs/NFI_X6_BACKTEST_REPORT_2026-05-11.md` | new | This run's full quantitative report — gate measurements, monthly P&L, risk diagnostics, recommendation. |
| `docs/NFI_X6_ACTIVATION_LOG.md` | new | Append-only event ledger for every gate run + activation event. |
| `user_data/backtest_results/backtest-result-2026-05-12_02-03-47.zip` | new | Raw backtest output (1.0 MB; freqtrade-format). The `nfi_x6_2y.json` requested in the task command is the same data with a different filename — both committed. |
| `user_data/backtest_results/nfi_x6_2y.json` | new (alias) | Trades export per the task spec. |
| `user_data/strategies/nfi_x6_config.json` | edited | Set `initial_state=running` (was `stopped` — bot was alive but not trading), enabled `api_server` (was disabled — healthcheck failed), bumped `bot_name`. NO change to dry_run, wallet, pair whitelist, or risk knobs. |
| `docker-compose.yml` (`freqtrade-nfi` block only) | edited | Added `FREQTRADE__DB_URL=sqlite:////freqtrade/user_data/tradesv3_nfi.sqlite` to keep NFI's trade DB **isolated** from the main bot's shared `freqtrade` Postgres DB. The entrypoint script was overriding the JSON `db_url` with a Postgres DSN. Runbook §6.3 flagged this as a known scaffold gap; this fix actually closes it. NO change to the main `freqtrade` service block. |
| `user_data/data/coinbase/*-4h.feather` × 8 (committed in main repo's data dir, hard-linked into worktree) | new | 1h candles resampled to 4h offline so the backtest could run. **Helper script lives at `/tmp/nfi-backtest-prep/resample_1h_to_4h.py` — should be promoted into `scripts/` if anyone re-runs this.** |

## Next-session decisions

To unblock Gate 4, one of these three remediations must land. Listed
in increasing operator effort:

  1. **Switch to Binance (or Kraken) for NFI.** Both expose native 4h.
     This is a config-only change to `nfi_x6_config.json`
     (`exchange.name = binance`, swap pair_whitelist). Operator must
     decide if NFI lives on a different exchange than the main bot.
     Time: 30 min. Risk: needs new API key.
  2. **Pre-resample 1h → 4h on the host via cron, force file-only
     load.** Hourly cron runs the resampler; freqtrade is told to
     prefer cached data over live-fetch for 4h. Time: 2-4 h. Risk:
     freqtrade does not natively support per-timeframe data sources
     in live mode; may require a monkey-patch of
     `freqtrade.exchange.Exchange.get_historic_ohlcv`.
  3. **Resample-on-fly DataProvider shim.** Subclass DataProvider
     with a `get_pair_dataframe()` override; when a 4h request
     returns empty, resample the cached 1h on the fly. Time: 1-2 d.
     Risk: depends on freqtrade's extension points; worst case needs
     a patched freqtrade install.

The task report recommends adding **Gate 2.5** to the runbook — a
30-second smoke-fetch of the live whitelist via `download-data` —
to catch this kind of data-source mismatch before activation:

```bash
docker compose --profile nfi run --rm --no-deps freqtrade-nfi \
    download-data --exchange coinbase --pairs BTC/USD --timeframes 4h \
    --days 1 || { echo "4h not available — abort activation"; exit 1; }
```

## Re-running the gates (cheap)

```bash
cd /home/saijayanthai/Documents/trading-bot/.claude/worktrees/agent-a0b3456dc62d76d28
./scripts/nfi_x6_gate_check.sh --dry-run        # gates 1+2 only, ~10 sec
./scripts/nfi_x6_gate_check.sh                  # gates 1+2+3, ~12 min
python3 scripts/nfi_x6_parse_backtest.py user_data/backtest_results/   # latest result → markdown
python3 scripts/nfi_x6_parse_backtest.py --json user_data/backtest_results/  # machine-readable
```

## Re-attempting activation (after Gate 4 is unblocked)

```bash
# From the main repo (NOT the worktree) so the container lives in the
# main compose project for day-to-day operator commands:
cd /home/saijayanthai/Documents/trading-bot
docker compose --profile nfi up -d --no-deps freqtrade-nfi

# Wait ≤120 sec for the healthcheck:
until docker ps --filter "name=^freqtrade-nfi$" --format '{{.Status}}' | grep -q healthy; do sleep 5; done

# Sanity-poll:
curl -fsS -u "$FREQTRADE_API_USER:$FREQTRADE_API_PASS" http://127.0.0.1:8090/api/v1/ping     # → {"status":"pong"}
curl -fsS -u "$FREQTRADE_API_USER:$FREQTRADE_API_PASS" http://127.0.0.1:8090/api/v1/show_config | jq '{strategy_version, state, dry_run, bot_name}'

# Watch the first 5 minutes for indicator-pass errors:
docker logs -f freqtrade-nfi 2>&1 | grep -iE "error|warn|trade|signal"
```

**If you see ANY `KeyError('date')` or `Empty candle (OHLCV) data` lines**,
the 4h gap is still there — stop immediately:

```bash
docker compose --profile nfi stop freqtrade-nfi
docker compose --profile nfi rm -f freqtrade-nfi
```

## Deactivation reference (runbook §5)

```bash
docker compose --profile nfi stop freqtrade-nfi
docker compose --profile nfi rm -f freqtrade-nfi
# Remove the sqlite trade DB if you want to reset paper-trade history:
rm -f user_data/tradesv3_nfi.sqlite
```

The main bot (`freqtrade` container, port 8080) and dashboard (8081)
are unaffected by `--profile nfi` operations.

## Pointers

- Runbook (full activation plan): [`docs/NFI_X6_ACTIVATION_2026-05-11.md`](docs/NFI_X6_ACTIVATION_2026-05-11.md)
- Quantitative report: [`docs/NFI_X6_BACKTEST_REPORT_2026-05-11.md`](docs/NFI_X6_BACKTEST_REPORT_2026-05-11.md)
- Activation event ledger: [`docs/NFI_X6_ACTIVATION_LOG.md`](docs/NFI_X6_ACTIVATION_LOG.md)
- Backtest result archive: `user_data/backtest_results/backtest-result-2026-05-12_02-03-47.zip`
- Trades export (task spec name): `user_data/backtest_results/nfi_x6_2y.json`
