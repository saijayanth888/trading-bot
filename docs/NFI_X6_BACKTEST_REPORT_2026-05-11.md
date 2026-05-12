# NFI X6 Backtest Report — Gate-3 Quality Evaluation

**Date:** 2026-05-11 (UTC 2026-05-12 02:03Z run)
**Branch:** `stage/22-nfi-backtest-and-activate`
**Strategy:** `NostalgiaForInfinityX6` (upstream `iterativv/NostalgiaForInfinity@main`, version `v16.8.800`)
**Config:** `user_data/strategies/nfi_x6_config.json`
**Result archive:** `user_data/backtest_results/backtest-result-2026-05-12_02-03-47.zip`
**Result trades export:** `user_data/backtest_results/nfi_x6_2y.json`
**Runbook:** [`docs/NFI_X6_ACTIVATION_2026-05-11.md`](./NFI_X6_ACTIVATION_2026-05-11.md)
**Activation log:** [`docs/NFI_X6_ACTIVATION_LOG.md`](./NFI_X6_ACTIVATION_LOG.md)

---

## TL;DR — VERDICT: GATES 1-3 PASS · ACTIVATION ROLLED BACK (live-mode 4h data gap)

| Gate | Threshold | Measured | Result |
|---|---|---|---|
| **Gate 1** — byte-identical to upstream | `diff = 0 bytes` | identical to `iterativv@main` (`sha256 0791763a…ad6f`) | **PASS** |
| **Gate 2** — `rapidjson + pandas_ta` importable | importable in `trading-bot/freqtrade:local` | rapidjson 1.23, pandas_ta 0.3.16, talib 0.6.8 | **PASS** |
| **Gate 3** — 2-year backtest quality | Sharpe>1.4 / DD<12% / PF>1.4 / ≥30 trades | Sharpe 1.90, DD 8.33%, PF 18.72, 94 trades | **PASS** |
| **Gate 4 (paper-soak preflight)** | bot starts cleanly, fetches data, populates indicators | KeyError 'date' on every pair every cycle (live-fetched 4h DataFrame is empty — Coinbase REST has no 4h candle) | **BLOCKED** |

**Activation attempt:** `docker compose --profile nfi up -d --no-deps freqtrade-nfi` was invoked successfully. The container came up healthy, the REST API responded on `127.0.0.1:8090`, dry_run was confirmed True, and the bot was in `state=running` with `bot_name=freqtrade-nfi-x6-paper-soak`. However, the **first indicator-population pass failed for all 8 pairs** with `KeyError('date')` because the live DataProvider returned an empty 4h DataFrame for every pair — Coinbase REST exposes 5m/15m/1h/6h/1d only, and NFI X6 hard-requires 4h. The container was rolled back (`docker compose --profile nfi stop && rm -f freqtrade-nfi`) within 2 minutes, no orders were placed, no DB writes occurred.

**Status:** NFI is **OFF**. `docker ps | grep freqtrade-nfi` is empty. The main bot was untouched throughout.

---

## Gate 1 — File integrity (byte-identical to upstream)

```
sha256(local user_data/strategies/NostalgiaForInfinityX6.py)
  = 0791763a0f4292dd6452081b446cebe6bab395c5adfa3c33cc7041befc02ad6f

sha256(iterativv/NostalgiaForInfinity@main:NostalgiaForInfinityX6.py)
  = 0791763a0f4292dd6452081b446cebe6bab395c5adfa3c33cc7041befc02ad6f
```

`diff -u` returned exit 0 (zero-byte diff). The local file declares
`def version(self) -> str: return "v16.8.800"` and is byte-identical to
upstream HEAD as of this run. **Note:** The runbook references tag
`v16.8.800` but no such *git tag* exists upstream — the highest v16.8
tag is `v16.8.260`. The author appears to bump the in-file `version()`
ahead of tagging. Treating "byte-identical to upstream main" as the
operational definition of Gate 1, it passes.

## Gate 2 — Dependencies importable in the freqtrade image

```
$ docker run --rm --entrypoint python trading-bot/freqtrade:local \
      -c "import rapidjson, pandas_ta, talib; \
          print('rapidjson', rapidjson.__version__); \
          print('pandas_ta', pandas_ta.version); \
          print('talib', talib.__version__)"

rapidjson 1.23
pandas_ta 0.3.16
talib    0.6.8
```

All three deps importable in `trading-bot/freqtrade:local`. Matches
the runbook's expected versions exactly.

## Gate 3 — 2-year backtest

```
$ docker compose --profile nfi run --rm --no-deps freqtrade-nfi \
      backtesting \
        --config /freqtrade/user_data/strategies/nfi_x6_config.json \
        --strategy NostalgiaForInfinityX6 \
        --timerange 20240501-20260501 \
        --export trades \
        --export-filename /freqtrade/user_data/backtest_results/nfi_x6_2y.json
```

Walltime: ~12 minutes. 8 pairs × 5 timeframes (5m / 15m / 1h / 4h / 1d) ×
730 days. The 4h timeframe was *not* available natively from Coinbase
(its REST exposes 5m / 15m / 1h / 6h / 1d only — `ccxt.coinbase().timeframes`
confirmed this), so 1h candles were resampled to 4h with the helper
`/tmp/nfi-backtest-prep/resample_1h_to_4h.py` (committed-equivalent: same
logic anyone re-running this can apply). All other timeframes were
already on disk and intact.

### Wallet/state at start vs end

| | |
|---|---|
| Starting balance | $50,000.00 |
| Final balance    | $70,027.48 |
| Absolute profit  | **+$20,027.48** |
| Total return     | **+40.05%** over 730 days |
| CAGR             | **+18.34%** |
| Total trades     | **94** (94 long, 0 short) |
| Winners / Losers / Draws | 93 / 1 / 0 |
| Trades / month avg | 3.92 |
| Best pair        | ADA/USD +10.47% |
| Worst pair       | BTC/USD +0.31% |
| Best day         | +$4,193.83 |
| Worst day        | -$1,130.23 |

### Task-spec activation gates (decide activation)

| Gate | Threshold | Measured | Pass? |
|---|---|---|---|
| Sharpe (annualized, daily wallet returns) | > 1.4 | **1.899** | **PASS** |
| Max drawdown (account, wallet basis)      | < 12% | **8.33%** | **PASS** |
| Profit factor                              | > 1.4 | **18.72** | **PASS** |
| Min 30 trades (sanity)                     | ≥ 30  | **94**    | **PASS** |

**Overall: PASS — all 4 task-spec gates clear.**

### Runbook extras (informational, not part of the activation decision)

| Extra | Threshold | Measured | Pass? |
|---|---|---|---|
| Win rate         | > 38% | **98.9%** | PASS |
| Trades / month   | > 30  | **3.92**  | **FAIL** |

NFI X6 is a swing/long-hold strategy (avg holding **7d 0h 45m** for
winners), so the runbook's `Trades/month > 30` heuristic is the wrong
shape for this strategy. The author inherited the threshold from a
template aimed at higher-frequency mean-reversion. The task spec
correctly drops it — Sharpe/DD/PF/min-trades is the right gate set.
For visibility, the runbook should be edited to mark this threshold
"strategy-class-specific" or remove it entirely.

### Risk diagnostics (the bits the operator should re-read before Gate 5)

| | Wallet (daily-balance) | Closed-trades |
|---|---|---|
| Sharpe    | **1.90** | 2.32 |
| Sortino   | **3.49** | -100.00 (degenerate; only 1 closing loss) |
| Calmar    | **12.62** | 65.99 |
| Max DD    | **8.33%** | 1.59% |
| Drawdown duration | 25d 09h | 83d 23h |
| Drawdown start → end | 2026-01-11 → 2026-02-06 | 2026-02-06 → 2026-05-01 |
| Profit at DD start / end | +$20,589 → +$14,706 | +$21,158 → +$20,027 |
| SQN | 9.08 | — |
| Expectancy | $213.06 / 0.19 R | — |

The wallet-based numbers are the operative ones (they include
unrealized peaks and match what a paper-trading dashboard would
display in real time). The closed-trades Sortino of -100 is a
freqtrade artifact — when the strategy has only 1 losing trade out
of 94, the downside-deviation denominator collapses and the formula
returns the floor. Use the wallet Sortino (3.49) for any dashboard
display.

### Concentration warning — read before live activation

The 1 losing trade is the **only force-exit** in the entire 2y window:
SOL/USD entered late and was held 134 days through the test boundary,
then force-closed at -3.26% (-$1,130.23) when the backtest ended. **In
live trading there is no force-exit at the boundary**, so the actual
realized loss could be larger or the position could recover — either
way the "98.9% win rate" headline overstates real-world reliability.
This is a structural quirk of the long-hold style: the test cuts off
before slow positions resolve. Operator should weight the Sharpe and
DD numbers more heavily than the win-rate when reasoning about edge.

### Sparseness warning

7 of 24 months had **0 trades** (Jul/Sep 2024, Jun/Jul/Sep 2025, Jan/Mar/Apr 2026).
NFI X6 is highly selective — it waits for specific multi-timeframe
confluence and skips conditions it doesn't trust. This is by design,
but means the *expected throughput* is low. The 4-week post-cutover
P&L target ($1k–$2k conservative, $4k–$8k optimistic from the
runbook §0) is consistent with this density, but the operator should
be ready for stretches with no fills.

### Trade streaks

- Longest winning streak: **93** consecutive wins
- Longest losing streak: **1** (the boundary force-exit)
- Best winning trade: ADA/USD +10.35%
- Worst trade: SOL/USD -3.26% (force-exit at boundary, see above)
- Avg winner duration: **5d 16h**
- Avg loser duration: 134d (n=1; not statistically meaningful)

### Monthly returns distribution

| Month | Trades | Profit ($) | Win % |
|---|---|---|---|
| 2024-06 | 4  | +565 | 100 |
| 2024-07 | 0  | 0 | — |
| 2024-08 | 11 | +1,963 | 100 |
| 2024-09 | 0  | 0 | — |
| 2024-10 | 1  | +133 | 100 |
| 2024-11 | 8  | +1,021 | 100 |
| 2024-12 | 21 | +4,179 | 100 |
| 2025-01 | 10 | +2,343 | 100 |
| 2025-02 | 13 | +4,482 | 100 |
| 2025-03 | 8  | +1,075 | 100 |
| 2025-04 | 1  | +201 | 100 |
| 2025-05 | 1  | +493 | 100 |
| 2025-06 | 0  | 0 | — |
| 2025-07 | 0  | 0 | — |
| 2025-08 | 6  | +1,510 | 100 |
| 2025-09 | 0  | 0 | — |
| 2025-10 | 4  | +1,554 | 100 |
| 2025-11 | 2  | +756 | 100 |
| 2025-12 | 1  | +144 | 100 |
| 2026-01 | 0  | 0 | — |
| 2026-02 | 2  | +740 | 100 |
| 2026-03 | 0  | 0 | — |
| 2026-04 | 0  | 0 | — |
| 2026-05 | 1  | -1,130 | 0 (boundary force-exit) |

---

## Recommendation

**DO NOT activate yet — Gate 4 (paper soak) is blocked by a live-mode
4h data gap.** The strategy backtest passed convincingly on resampled
data (Sharpe 1.36× threshold, DD 1.44× under threshold, PF 13.4×
threshold), but the live DataProvider cannot fetch 4h candles from
Coinbase, and the strategy errors out before generating a single
indicator value. The first paper-soak attempt (this session) saw the
bot heartbeat alive but throwing `KeyError('date')` every cycle on
all 8 pairs.

### What needs to happen before Gate 4 can re-attempt

Pick **one** of these three remediations and re-run the activation:

  1. **Resample-on-fly shim (preferred, ~30 LOC, no strategy edit).**
     Subclass `freqtrade.data.dataprovider.DataProvider` with a
     `get_pair_dataframe()` override that, when asked for the 4h
     timeframe and given an empty result, resamples 1h candles into
     4h on the fly. Wire it into the freqtrade-nfi service via a
     custom dataformat / patch. Risk: depends on freqtrade extension
     points; may need a freqtrade fork. **Best long-term**.
  2. **Cron pre-resample 1h → 4h every hour, force file-only data
     load.** Have a host cron run `scripts/resample_1h_to_4h.py`
     every hour (it writes the feather), and configure freqtrade
     to prefer cached data over live-fetch for the 4h timeframe.
     Risk: freqtrade does not natively support per-timeframe data
     sources in live mode — would require a config hack or
     monkey-patch. **Lower-quality but simplest if it works.**
  3. **Switch to an exchange that has native 4h.** Binance and
     Kraken both expose 4h. This is a config-only change to
     `nfi_x6_config.json` (`exchange.name = binance` and a Binance
     pair_whitelist). Operator must add a Binance API key and
     decide if that fits the live-trading roadmap. **Cleanest but
     requires operator buy-in beyond Coinbase USD.**

**The backtest result remains valid as a *strategy-quality* signal,
but Gate 4 cannot meaningfully run on Coinbase USD without one of
the above.** The 7-day paper-soak should target after the chosen
remediation lands.

### Why activation was tried then rolled back, not skipped

The task spec says "IF backtest passes, run `docker compose --profile
nfi up -d freqtrade-nfi`" — Gate 3 passed, so the activation step
ran. The 4h gap was only discoverable *post*-up because no smoke
test in the runbook exercises the live data path. Container ran for
~120 seconds, no trades opened, no DB writes (sqlite was empty),
process rolled back cleanly.

The smarter, *new* Gate 4 preflight that needs to land in the runbook:

```bash
# Smoke-fetch the 4h whitelist via CCXT before activating.
docker compose --profile nfi run --rm --no-deps freqtrade-nfi \
    download-data --exchange coinbase --pairs BTC/USD --timeframes 4h \
    --days 1 || { echo "4h not available — abort activation"; exit 1; }
```

Add this as Gate 2.5 in `docs/NFI_X6_ACTIVATION_2026-05-11.md` to
catch the live-data-source mismatch before the up command runs.

---

## Reproducibility

```bash
# From repo root, on branch stage/22-nfi-backtest-and-activate
./scripts/nfi_x6_gate_check.sh --dry-run          # gates 1+2 only (~10s)
./scripts/nfi_x6_gate_check.sh                    # gates 1+2+3 (~12 min)
python3 scripts/nfi_x6_parse_backtest.py user_data/backtest_results/   # latest result
python3 scripts/nfi_x6_parse_backtest.py --json …   # machine-readable
```

The Coinbase 4h gap is patched at backtest time by resampling 1h →
4h locally (Coinbase REST has no native 4h candle). Re-run the
helper if you re-download data:

```bash
python3 /tmp/nfi-backtest-prep/resample_1h_to_4h.py \
    /home/saijayanthai/Documents/trading-bot/user_data/data/coinbase
```
