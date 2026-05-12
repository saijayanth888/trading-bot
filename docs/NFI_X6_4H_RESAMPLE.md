# NFI X6 4h-resample bridge — operator runbook

**Branch:** `stage/nfi-4h-resample`
**Status:** ready for operator review; **NFI X6 is NOT activated by this branch.**

## Why this exists

NFI X6 hard-requires 4h informative candles. Coinbase Advanced REST exposes
only `5m / 15m / 1h / 6h / 1d` — there is no native 4h endpoint. Without 4h
candles, NFI X6's first indicator pass fails with `KeyError('date')` for
every pair (this is exactly what happened on `stage/22` yesterday).

This branch implements the **cron-resample** remediation from
[`docs/NFI_X6_HANDOFF.md`](NFI_X6_HANDOFF.md) (option #2): fetch 1h candles
from Coinbase, resample to 4h on the host every 4 hours, and write the
result to JSON files in the format Freqtrade's `JsonDataHandler` reads.

## The pipeline

```
Coinbase REST (1h endpoint)
        │
        ▼  ccxt.coinbase.fetch_ohlcv(timeframe='1h')
        │
   scripts/resample_1h_to_4h.py
   pandas resample('4h', label='left', closed='left', origin='epoch')
        │
        ▼  bars anchored on UTC 00,04,08,12,16,20
        │
   user_data/data/coinbase/<PAIR>-4h.json
   format: [[ts_ms, open, high, low, close, volume], ...]
        │
        ▼  read on backtest startup + as seed for live mode
   freqtrade-nfi (NostalgiaForInfinityX6)
```

The cron wrapper `~/.hermes/scripts/resample_4h.sh` invokes the resampler
**inside the `trading-bot/freqtrade:local` docker image** so it always uses
the exact same `ccxt` / `pandas` versions Freqtrade itself uses. Host
Python is not required.

## Files this branch touches

| File | Kind | Purpose |
|---|---|---|
| `scripts/resample_1h_to_4h.py` | new | The resampler. Reads `nfi_x6_config.json` pair_whitelist, fetches 1h via ccxt-coinbase (or falls back to the cached `.feather` if `--no-feather` is omitted), resamples to 4h anchored on UTC 00:00, writes `<PAIR>-4h.json` atomically. Idempotent, fail-soft per pair. |
| `scripts/nfi_x6_4h_smoke.sh` | new | Gate 2.5 verifier. Asserts every whitelist pair has a 4h JSON file with ≥ `MIN_BARS` rows (default 100 ≈ 16.7 days). Exit 0 PASS, 1 FAIL, 2 error. |
| `scripts/install_nfi_x6.sh` | new | One-command operator activation. Runs gates 1+2 (via existing `nfi_x6_gate_check.sh --dry-run`) + gate 2.5, then enables the cron job and brings up `freqtrade-nfi`. Supports `--dry-run` (gates only) and `--uninstall` (reverse). |
| `~/.hermes/scripts/resample_4h.sh` | new (out-of-tree) | Hermes cron wrapper. Schedule `5 */4 * * *`. Logs to `stocks/memory/cron-resample-4h.log`. |
| `~/.hermes/cron/jobs.json` | edited (out-of-tree) | Appended entry `resample_4h_b1`, **`enabled: false`** until operator runs `install_nfi_x6.sh`. Pre-edit backup at `jobs.json.backup-pre-resample-4h-<ts>`. |
| `user_data/strategies/nfi_x6_config.json` | edited | Added `dataformat_ohlcv: "json"`, `dataformat_trades: "json"`, `datadir: /freqtrade/user_data/data`. Documented the 4h gap in `_doc_`. |
| `tests/test_resample_4h.py` | new | 13 unit tests: OHLCV math, anchor, idempotency, gap-handling, output-format round-trip with Freqtrade. All PASS. |
| `user_data/data/coinbase/<PAIR>-4h.json` × 8 | new | Seeded 90 days (541 bars per pair). Operator may regenerate any time. |

## First-run instructions (operator)

```bash
cd /home/saijayanthai/Documents/trading-bot   # NOT the worktree; merge first

# 1. (Optional) Re-seed the 4h JSON files fresh from Coinbase. The branch
#    already shipped seeds for 8 pairs covering 2026-02-11 → 2026-05-12.
#    Run this when the seed is older than ~24h:
REPO=/home/saijayanthai/Documents/trading-bot \
RESAMPLE_4H_DAYS=90 \
bash ~/.hermes/scripts/resample_4h.sh

# 2. Verify Freqtrade discovers them:
docker run --rm \
    -v "$PWD/user_data:/freqtrade/user_data" \
    --entrypoint freqtrade trading-bot/freqtrade:local \
    list-data --config /freqtrade/user_data/strategies/nfi_x6_config.json
# Expect: 8 rows, all '4h spot'.

# 3. Dry-run the installer — verifies all gates without activating:
bash scripts/install_nfi_x6.sh --dry-run

# 4. Activate (gates → enable cron → bring up container → wait healthy):
bash scripts/install_nfi_x6.sh
```

## What this branch does NOT do

1. **Does NOT activate NFI X6.** The container is up only if the operator
   explicitly runs `install_nfi_x6.sh` without `--dry-run`.
2. **Does NOT solve the live-mode 4h fetch problem.** This branch makes 4h
   candles **available on disk** in the format Freqtrade expects, and it
   makes Freqtrade **discover them** (verified via `list-data`). But in
   `dry_run`/`live` mode, `DataProvider.get_pair_dataframe()` ultimately
   calls `Exchange._build_ohlcv_dl_jobs`, which **explicitly refuses to
   download 4h from Coinbase** because `"4h" not in coinbase.timeframes`.
   The result is that disk-cached 4h JSON files are read **at startup**
   for the initial history seed, but won't be refreshed by Freqtrade itself
   on the cycle loop. That's an open follow-up — see "Outstanding gap"
   below.
3. **Does NOT merge to main.** This worktree must stay on
   `stage/nfi-4h-resample`. Operator decides when/whether to merge.

## Outstanding gap (do NOT skip when activating)

When NFI X6 actually goes live, it needs the **disk-cached 4h candles to
be injected into `Exchange._klines`** at strategy startup, OR Coinbase's
`Exchange.timeframes` patched to include `"4h"` with a custom downloader
fall-through. Both options were flagged in
`docs/NFI_X6_HANDOFF.md::Next-session-decisions` (item #2 monkey-patch,
item #3 DataProvider shim).

The cleanest path appears to be a **wrapper strategy** (e.g. `NFIX6Coinbase4H`
that subclasses `NostalgiaForInfinityX6`) which overrides `bot_start()` to
load `<PAIR>-4h.json` via `JsonDataHandler` and stamp into
`self.dp._exchange._klines[(pair, "4h", SPOT)]`. Gate 1 (byte-identical
upstream) still passes because the upstream file isn't modified.

Until that wrapper exists, running `install_nfi_x6.sh` will bring up the
container but the strategy will likely emit `Empty candle (OHLCV) data` for
4h on the first cycle. The smoke verifier guarantees the **data files** are
present; it cannot guarantee the **runtime injection** path works. The
rollback hook below catches this within 2-5 minutes.

## Cron schedule

```
5 */4 * * *   resample_4h.sh   (00:05, 04:05, 08:05, 12:05, 16:05, 20:05 UTC)
```

The `:05` offset gives Coinbase ~5 minutes to publish the just-closed 4h
bar. The job is registered in `~/.hermes/cron/jobs.json` with
**`enabled: false`** so it does NOT fire until the operator activates via
`install_nfi_x6.sh` (which flips `enabled: true`).

## Test command

```bash
# Unit tests (13 cases — OHLCV math, anchor, idempotency, gaps, format):
python3 -m pytest tests/test_resample_4h.py -v

# End-to-end manual run of the cron wrapper:
REPO=/home/saijayanthai/Documents/trading-bot \
RESAMPLE_4H_LOG=/tmp/test.log \
bash ~/.hermes/scripts/resample_4h.sh
# Then:
bash scripts/nfi_x6_4h_smoke.sh
```

## Rollback

```bash
# Stops the freqtrade-nfi container AND disables the cron job:
bash scripts/install_nfi_x6.sh --uninstall

# To also delete the 4h JSON files:
rm -f user_data/data/coinbase/*-4h.json

# To remove the cron job entry entirely from jobs.json:
python3 - <<'PY'
import json
from pathlib import Path
p = Path.home() / ".hermes" / "cron" / "jobs.json"
d = json.loads(p.read_text())
d["jobs"] = [j for j in d["jobs"] if j["id"] != "resample_4h_b1"]
p.write_text(json.dumps(d, indent=2))
print("removed resample_4h_b1")
PY
```

The pre-edit backup of `jobs.json` lives at
`~/.hermes/cron/jobs.json.backup-pre-resample-4h-*` if a full revert is
needed.

## Activation: one-liner

```bash
bash scripts/install_nfi_x6.sh
```
