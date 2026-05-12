# HANDOFF — `stage/nfi-4h-resample`

**Branch goal:** build the cron-resample 1h → 4h pipeline that unblocks
NFI X6 live-paper-trading on Coinbase. The branch is review-ready but
**does NOT activate NFI X6** — operator runs `scripts/install_nfi_x6.sh`
after reviewing.

**Read first:** [`docs/NFI_X6_4H_RESAMPLE.md`](docs/NFI_X6_4H_RESAMPLE.md)
(full operator runbook), then this file for status.

---

## Why we need 4h on Coinbase

NFI X6 hard-requires 4h informative candles. Coinbase Advanced REST exposes
only `5m / 15m / 1h / 6h / 1d` — no native 4h endpoint. When the prior
agent (`stage/22`) brought up `freqtrade-nfi` against the live Coinbase
exchange, the first indicator pass failed with `KeyError('date')` for every
pair because `DataProvider.get_pair_dataframe(timeframe="4h")` returned an
empty DataFrame.

NFI X6 was rolled back in 2 minutes, the main bot was unaffected, and the
prior agent left three remediation paths documented in
[`docs/NFI_X6_HANDOFF.md`](docs/NFI_X6_HANDOFF.md):

  1. Switch NFI X6 to Binance/Kraken (different exchange).
  2. **Cron-resample 1h → 4h on the host** — this branch.
  3. DataProvider shim that resamples on the fly.

This branch implements (2): keep Coinbase, no exchange migration, ~1 day
of work.

## The pipeline

```
Coinbase REST  ──1h candles──▶  ~/.hermes/scripts/resample_4h.sh  ──▶  user_data/data/coinbase/<PAIR>-4h.json
                                  (cron 5 */4 * * *)
                                                                                 │
                                                                                 ▼
                                                      freqtrade-nfi (reads JSON 4h files at startup)
```

Output JSON files match `JsonDataHandler.ohlcv_store` format exactly
(`[[ts_ms, open, high, low, close, volume], ...]`), confirmed via round-trip
load through `JsonDataHandler.ohlcv_load`.

## What landed

| Artifact | Path | Status |
|---|---|---|
| Resampler | `scripts/resample_1h_to_4h.py` | new — produces JSON in Freqtrade's `orient="values"` format; idempotent; fail-soft per pair |
| Cron wrapper | `~/.hermes/scripts/resample_4h.sh` | new — invokes resampler inside `trading-bot/freqtrade:local` docker image so it always uses the same `ccxt`/`pandas` as Freqtrade |
| Cron registration | `~/.hermes/cron/jobs.json` (entry `resample_4h_b1`) | added with `enabled: false`; pre-edit backup at `jobs.json.backup-pre-resample-4h-20260512T122449Z` |
| Gate 2.5 verifier | `scripts/nfi_x6_4h_smoke.sh` | new — asserts every whitelist pair has a 4h JSON file with ≥ 100 bars (~16.7 days) |
| Operator installer | `scripts/install_nfi_x6.sh` | new — runs gates 1+2+2.5, then enables cron + brings up container; supports `--dry-run` and `--uninstall` |
| Strategy config | `user_data/strategies/nfi_x6_config.json` | edited — added `dataformat_ohlcv: "json"`, `dataformat_trades: "json"`, `datadir: /freqtrade/user_data/data`; documented the 4h gap in `_doc_` |
| Tests | `tests/test_resample_4h.py` | new — 13 cases; all PASS |
| Seed data | `user_data/data/coinbase/<PAIR>-4h.json` × 8 | seeded 90 days (541 bars per pair); 2026-02-11 → 2026-05-12 |
| Operator runbook | `docs/NFI_X6_4H_RESAMPLE.md` | new — first-run instructions, rollback, outstanding gap |

## First-run instructions (operator)

```bash
cd /home/saijayanthai/Documents/trading-bot   # after merging the branch

# 1. (Optional) Re-seed 4h candles from Coinbase. Already shipped 90 days.
REPO=/home/saijayanthai/Documents/trading-bot \
RESAMPLE_4H_DAYS=90 \
bash ~/.hermes/scripts/resample_4h.sh

# 2. Confirm Freqtrade discovers the files (sanity):
docker run --rm \
    -v "$PWD/user_data:/freqtrade/user_data" \
    --entrypoint freqtrade trading-bot/freqtrade:local \
    list-data --config /freqtrade/user_data/strategies/nfi_x6_config.json
# Expect 8 rows, all '4h spot'.

# 3. Dry-run the installer — verifies all 3 gates without activating:
bash scripts/install_nfi_x6.sh --dry-run

# 4. Activate:
bash scripts/install_nfi_x6.sh
```

## Test command

```bash
# Unit tests (13 cases, ~0.2 s):
python3 -m pytest tests/test_resample_4h.py -v

# Smoke (Gate 2.5):
bash scripts/nfi_x6_4h_smoke.sh

# End-to-end cron wrapper:
REPO=/home/saijayanthai/Documents/trading-bot/.claude/worktrees/agent-a31057114e6a1d5a8 \
RESAMPLE_4H_LOG=/tmp/r4h.log \
bash ~/.hermes/scripts/resample_4h.sh
tail -20 /tmp/r4h.log
```

## Cron schedule

```
5 */4 * * *  ~/.hermes/scripts/resample_4h.sh   (00:05, 04:05, 08:05, 12:05, 16:05, 20:05 UTC)
```

The `:05` offset lets Coinbase publish the just-closed 4h bar before we
fetch. The job is registered in `~/.hermes/cron/jobs.json` but
**`enabled: false`** — it will NOT fire until the operator runs
`install_nfi_x6.sh`, which atomically flips `enabled` to true and brings
up the container.

## Activation: one-liner

```bash
bash scripts/install_nfi_x6.sh           # full activation
bash scripts/install_nfi_x6.sh --dry-run # verify gates only (no activation)
```

## Rollback

```bash
# Stops the freqtrade-nfi container AND disables the cron job:
bash scripts/install_nfi_x6.sh --uninstall

# (Optional) full revert of the jobs.json mutation:
cp ~/.hermes/cron/jobs.json.backup-pre-resample-4h-20260512T122449Z \
   ~/.hermes/cron/jobs.json
```

## What this branch deliberately does NOT do

  1. **Does not enable NFI X6.** Operator must run `scripts/install_nfi_x6.sh`.
  2. **Does not resolve the live-mode 4h fetch.** This branch makes 4h candles
     available on disk and gets Freqtrade to **discover them** (confirmed via
     `freqtrade list-data` — found 8 pair/4h/spot files). But at runtime
     `Exchange._build_ohlcv_dl_jobs` refuses to download 4h because Coinbase's
     `timeframes` list lacks `"4h"`. The disk files seed the initial history
     but won't be live-refreshed by Freqtrade's cycle loop — that's an open
     follow-up documented in `docs/NFI_X6_4H_RESAMPLE.md::Outstanding-gap`.
  3. **Does not touch the main `freqtrade` container** or any non-NFI
     strategy. The `freqtrade-nfi` service uses `profile: nfi` — it only
     comes up when the profile is explicitly named.
  4. **Does not push to remote.** Leave it on the local branch.

## Verification log

```
$ python3 -m pytest tests/test_resample_4h.py -v
13 passed in 0.20s

$ bash scripts/install_nfi_x6.sh --dry-run
✓ gates 1+2 PASS  (NFI X6 sha256 byte-identical to upstream; rapidjson+pandas_ta+talib OK)
✓ gate 2.5 PASS   (8/8 pairs, 541 bars each, min_bars=100)
✓ all gates PASS; not activating

$ freqtrade list-data --config user_data/strategies/nfi_x6_config.json
Found 8 pair / timeframe combinations: ADA, AVAX, BTC, DOGE, ETH, LINK, SOL, XRP (all 4h spot)
```
