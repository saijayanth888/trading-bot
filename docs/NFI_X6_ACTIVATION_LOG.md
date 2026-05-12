# NFI X6 Activation Log

Append-only ledger of every NFI X6 gate run + activation/deactivation event.
Format: one line per event, ISO-UTC timestamp + summary. See
`docs/NFI_X6_ACTIVATION_2026-05-11.md` for the activation runbook and
`docs/NFI_X6_BACKTEST_REPORT_*.md` for the most recent gate-3 measurements.

| Timestamp (UTC) | Branch | Event | Summary |
|---|---|---|---|
| 2026-05-12T01:51Z | `stage/22-nfi-backtest-and-activate` | gate-1 PASS | sha256 `0791763a…ad6f` matches `iterativv@main:NostalgiaForInfinityX6.py` |
| 2026-05-12T01:51Z | `stage/22-nfi-backtest-and-activate` | gate-2 PASS | rapidjson 1.23 / pandas_ta 0.3.16 / talib 0.6.8 importable in `trading-bot/freqtrade:local` |
| 2026-05-12T02:03Z | `stage/22-nfi-backtest-and-activate` | gate-3 PASS | 730d backtest: Sharpe 1.90, DD 8.33%, PF 18.72, 94 trades. Result: `user_data/backtest_results/backtest-result-2026-05-12_02-03-47.zip` |
| 2026-05-12T02:13Z | `stage/22-nfi-backtest-and-activate` | activation-up | `docker compose --profile nfi up -d --no-deps freqtrade-nfi` — container healthy, REST 8090 returned 200 to /ping, dry_run=true, state=running |
| 2026-05-12T02:14Z | `stage/22-nfi-backtest-and-activate` | gate-4 BLOCKED | live DataProvider returned empty 4h DataFrames for all 8 pairs (Coinbase REST has no 4h candle); strategy threw `KeyError('date')` every cycle |
| 2026-05-12T02:14Z | `stage/22-nfi-backtest-and-activate` | activation-down | rolled back (`docker compose --profile nfi stop && rm -f freqtrade-nfi`); 0 trades opened, 0 DB writes |
