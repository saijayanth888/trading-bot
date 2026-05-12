# TFT-blind fallback mode for FreqAIMeanRevV1

Branch: `feat/tft-blind-fallback` (worktree-local, NOT pushed)
Base:   `main` @ 8d7aceb (Saturday 2026-05-12 morning state)
Worktree: `/home/saijayanthai/Documents/trading-bot/.claude/worktrees/agent-a650ce52a2a879bb4`

## What this branch does

When FreqAI's prediction columns (`up`, `down`) are missing from the
dataframe — either during the 5-10 min freqai cold-init window after a
freqtrade restart, or because the pair is quarantined behind a stub
`model.zip` — the strategy NO LONGER goes dark by default. Instead it
can fall through to a pure BollingerRSI mean-reversion entry path at
degraded position sizing.

This is **opt-in** (`strategy_overrides.tft_blind_fallback.enabled =
false` by default). On the safety side, every non-TFT gate the full
TFT path uses (capital_allocation, regime_confidence, regime_label
trending_down / high_volatility, regime_min_stable_hours, risk_governor
in `custom_stake_amount`) still applies to TFT-blind entries.

## Design decisions

- **Fallback signal = BB lower + RSI <= 30.** Identical thresholds to
  the existing `bb_oversold_revert` branch in the TFT-driven entry
  pipeline. The two paths now share named class constants
  (`BBRSI_OVERSOLD_RSI = 30.0`, `BBRSI_OVERBOUGHT_RSI = 70.0`) so they
  cannot silently drift. Exit signal mirrors: BB upper + RSI >= 70.

- **50% position multiplier (default).** Applied multiplicatively on
  top of the meta-agent size and `HIGH_VOL_STAKE_FACTOR`, so every
  conservative layer compounds. Clamped to `[0, 1]` — a misconfigured
  `>1.0` cannot accidentally grow a blind trade. Multiplier is read
  from `strategy_overrides.tft_blind_fallback.position_size_multiplier`
  on every call so the operator can tune live without a restart (the
  freqtrade `reload_config` flow picks up the change).

- **Safety gates that ALWAYS apply on the blind path:**
  - `capital_allocation.pair_weight <= 0` -> pair-level kill
  - `capital_allocation.min_sharpe_for_trading` -> pair-level kill
  - `regime_confidence < 0` (DB-down sentinel) -> per-row block
  - `regime_label in (trending_down, high_volatility)` -> per-row block
    (TFT-blind has no model confidence to override the trend, so we
    stay categorical instead of probability-weighting)
  - `%-regime_duration_h < REGIME_MIN_STABLE_HOURS` (default 2.0h) ->
    per-row block; absent column -> 0 -> block (safer default)
  - `custom_stake_amount` still consults `risk_governor`, the
    capital-allocation pair-cap, and `min_stake / max_stake`.

- **Gates explicitly SKIPPED on the blind path:**
  - `up >= entry_threshold + regime_delta` (no `up` column to read)
  - `tft_confidence >= TFT_MIN_CONFIDENCE` (no column)
  - `meta_signal == +1` and `meta_confidence` floor (no meta-agent)

- **Per-pair log latch.** First time the fallback fires on a pair we
  emit one `[strategy] PAIR TFT-blind fallback ACTIVE - trading on
  BollingerRSI MR signal at 50% size` line and then go quiet — same
  pattern as `_missing_pred_cols_logged` so log volume stays bounded.

- **Quarantine is informational, NEVER excluding.** Confirmed during
  this work: no code path consults the quarantine set to skip a pair
  during training. FreqAI's queue selection is driven solely by
  `pair_whitelist` + `live_retrain_hours`. Quarantined pairs stay in
  rotation and self-rehabilitate the moment their next training cycle
  produces a clean `model.zip` (validation gate is on the
  `TFTTrainerWrapper.save` path from a prior branch). New
  `quarantine_rehab_summary()` helper makes the heal cycle visible:
  one INFO line per quarantined pair at boot ("WILL rehabilitate on
  next training cycle") and a `REHABILITATED` line the first time the
  scan flips a pair back to `ok`.

## Commits (6 atomic)

| # | SHA      | Title |
|---|----------|-------|
| 1 | `9df6db1` | config: add `strategy_overrides.tft_blind_fallback` block |
| 2 | `b377363` | strategy: extract BollingerRSI MR signal into named helpers |
| 3 | `3283441` | strategy: wire TFT-blind fallback entry/exit paths |
| 4 | `59d64b8` | strategy: cut stake on TFT-blind entries by multiplier |
| 5 | `2663d10` | quarantine: auto-rehab + bot_start rehab banner |
| 6 | `95980ad` | dashboard: TFT-blind chip on TrainingHealthLive card |

### File-level diff (vs `main`)

| File | + | - | Notes |
|------|---|---|------|
| `user_data/config.json`                      |   8 | 0   | New OPTIONAL `strategy_overrides` block (default OFF) |
| `user_data/strategies/FreqAIMeanRevV1.py`    | 271 | 27  | Helpers + entry/exit branch + stake-amount multiplier + bot_start rehab call |
| `user_data/freqaimodels/tft_pickle.py`       | 108 | 0   | `quarantine_rehab_summary` + docstring contract |
| `user_data/dashboard/ops_routes.py`          |  39 | 0   | Per-pair `tft_blind_eligible`/`tft_blind_active` + envelope summary |
| `user_data/dashboard/static/js/ops_spa.js`   |  53 | 1   | `[blind]` / `[dark]` chip + footer banner |
| `user_data/dashboard/templates/ops_spa.html` |   4 | 4   | Cache-bust -> `?v=20260512-tft-blind-fallback` |

## How to enable

Edit `user_data/config.json`:

```
"strategy_overrides": {
    "tft_blind_fallback": {
        "enabled": true,
        "position_size_multiplier": 0.5,
        "log_per_pair_once": true
    }
}
```

Then either send freqtrade a `reload_config` (HTTP POST
`/api/v1/reload_config`, already wired via /ops Reload-config button)
OR `docker compose restart freqtrade`. The strategy reads the block
fresh on every candle, so no rebuild is needed for tuning.

Dashboard rebuild to pick up the chip:

```
docker compose build dashboard && docker compose up -d dashboard
```

## How to disable

Three options, in increasing finality:

1. **Live tune to 0% size:** set
   `"position_size_multiplier": 0.0` — fallback still fires (you will
   see the log line + chip) but every blind entry gets sized to zero
   and freqtrade rejects it. Useful for canary-style debug.

2. **Toggle the flag off:** set `"enabled": false` and reload_config.
   Strategy reverts to the original safe no-op behaviour — pairs
   without TFT predictions go dark, no signal emitted.

3. **Remove the override block:** delete the entire
   `"strategy_overrides": {...}` object. The strategy's
   `_TFT_BLIND_DEFAULTS` (enabled=False) takes over and the dashboard
   stops showing the footer banner.

## Expected trade behavior — realistic example

The four pairs that have been DARK since 2026-05-11 (DOGE/USD,
XRP/USD, AVAX/USD, LINK/USD) all hold quarantined `model.zip` stubs
in `pair_dictionary.json`. With the fallback enabled:

- On a 5m candle where DOGE/USD prints `close < bb_lower` and
  `rsi_14 <= 30` with `volume > 0`:
  - `enter_long = 1, enter_tag = "tft_blind_bbrsi"` IF the row also
    passes the safety gates (regime not trending_down /
    high_volatility, regime_duration_h >= 2.0h, regime_confidence >= 0,
    pair_weight > 0, live Sharpe >= floor).
  - `custom_stake_amount` returns
    `proposed_stake * meta_size * 0.5 * HIGH_VOL_STAKE_FACTOR` —
    typically lands at ~25-35% of the freqtrade-default stake.

- The pair is also in the FreqAI training queue (live_retrain_hours
  rotation). When its next training cycle produces a validated
  `model.zip`, `trained_timestamp` is bumped, `up`/`down` columns
  start appearing on the dataframe, and the strategy automatically
  flips back to the full TFT path with normal sizing. The
  `_QUARANTINE_LOGGED` set + `_REHAB_STATUS_SEEN` snapshot ensure
  the operator sees a `[tft-rehab] DOGE/USD REHABILITATED` log line
  on the first scan after the heal.

- Dashboard: while fallback is running, DOGE/USD row in
  TrainingHealthLive shows `[blind]` (warn pill) instead of just
  `STUB`. Once rehabilitated, the status flips to `OK` and the chip
  disappears.

A casual estimate for the 2026-05-11 -> 2026-05-12 window: 4 pairs *
~6 BB-oversold candles per pair per 24h * half size ~ 12 small blind
entries worth of exposure that would otherwise have been zero. Run
the backtest TODO below for a real number.

## Known limits

- **No Sharpe filter applied to blind trades specifically.** The
  pair-level `capital_allocation.min_sharpe_for_trading` gate fires
  on the pair rolling Sharpe, not on the blind path contribution.
  If blind trades drag a pair below the floor over 14d, the whole
  pair (including future TFT trades when it rehabilitates) gets
  gated until the Sharpe recovers. Acceptable trade-off for v1;
  refine later if blind P&L attribution warrants a separate Sharpe
  pool.

- **No separate blind-vs-TFT P&L attribution in trade_journal.**
  `enter_tag` is set to `tft_blind_bbrsi` (entry) and
  `tft_blind_bbrsi_exit` (exit) so the data is there in
  `trade_journal.entry_tag`. A follow-up could aggregate
  `entry_tag IN ('tft_blind_bbrsi')` separately in the dashboard
  Day P&L / Weekly P&L cards. Not done here to keep this change
  surgical.

- **`tft_blind` column is set per-call, not persisted.** When
  freqai eventually delivers `up`/`down` for a pair, the strategy
  takes the full-TFT branch and `tft_blind` is NOT written that
  candle. `_is_tft_blind_trade` reads only the most-recent
  analyzed row, so size cuts only on candles that actually
  entered blind. A previously-open blind trade does NOT get
  re-sized — that's a freqtrade-API constraint; `custom_stake_amount`
  fires on entry only.

- **Categorical trending_down / high_volatility block on blind.**
  This is conservative — the full TFT path probability-weights
  these regimes (`TRENDING_DOWN_MIN_CONFIDENCE = 0.70`). The blind
  path has no probability to weight, so it stays out entirely. May
  miss the strong-reversal candle. Acceptable on a "fallback when
  TFT is broken" path; the cure is to fix TFT.

## Backtest TODO (operator)

Before enabling on live-paper, validate the historical behaviour:

```
docker compose run --rm freqtrade \
    freqtrade backtesting \
        --strategy FreqAIMeanRevV1 \
        --timerange 20260401-20260512 \
        --userdir /freqtrade/user_data
```

with `strategy_overrides.tft_blind_fallback.enabled = true` in the
config. Compare against the same range with `enabled = false` to see
the marginal effect of the fallback path. Expect: more trades on
DOGE/XRP/AVAX/LINK (the historically-quarantined pairs), at ~half
size, with the same hit-rate as the existing `bb_oversold_revert`
branch (which is what the blind path resolves to when
quarantine + safety gates filter as expected).

## Verification checklist (post-restart)

1. `strategy_overrides.tft_blind_fallback.enabled = true` in
   `user_data/config.json`
2. `docker compose restart freqtrade && docker compose build dashboard
   && docker compose up -d dashboard`
3. Within 1 min, expect log lines:
   - `[tft-rehab] N/M pair(s) quarantined; will rehabilitate on next
     successful training cycle: ...`
   - `[strategy] DOGE/USD TFT-blind fallback ACTIVE - trading on
     BollingerRSI MR signal at 50% size` (or whichever pair triggers
     first)
4. /ops -> TrainingHealth card shows `[blind]` (warn) chip next to
   quarantined / stale pairs and a footer line
   `tft-blind fallback ON * N pair(s) trading on BollingerRSI MR at
   50% size * auto-disables when TFT retrains clean`.
5. Open paper trades start firing on those pairs at half size when
   BollingerRSI signal triggers AND safety gates pass.
6. After successful retrain (24h cycle by default), the `[blind]` chip
   disappears, `[tft-rehab] PAIR REHABILITATED` log line emits, normal
   sizing resumes on next entry.

## Constraints honoured

- Did NOT touch `~/.hermes/`, ModelForge, or Ollama config.
- Did NOT push to remote.
- Did NOT restart freqtrade or rebuild containers (operator does that).
- 6 atomic commits.
- Every commit passes `python3 -m py_compile`; final JS passes
  `node -c`; `config.json` parses as valid JSON.
- Default keeps the safe behaviour (`enabled: false`); operator must
  opt in by editing config.
