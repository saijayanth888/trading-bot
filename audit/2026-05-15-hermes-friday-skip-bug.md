# Hermes Scheduler Friday-Skip Bug — 2026-05-15

**Discovered:** 2026-05-15 09:01 ET while running pre-market battle checklist.

## Symptom

Two Hermes cron jobs whose last run was Thursday 2026-05-14 had their
`next_run_at` set to **Monday 2026-05-18**, skipping Friday entirely:

| Job | Schedule | last_run_at | next_run_at | Status |
|---|---|---|---|---|
| `shark_pre_market` | `0 9 * * 1-5` | 2026-05-14 09:02:57 ET | 2026-05-18 09:00:00 ET | **Skipped today** |
| `stocks_tft_smoke` | `30 8 * * 1-5` | 2026-05-14 08:30 ET (assumed) | 2026-05-18 08:30:00 ET | **Skipped today** |

Other jobs with `1-5` schedules that ran *today already* (e.g.,
`risk_monitor_15min` at `*/15 * * * *`) advanced their `next_run_at`
correctly. The bug only manifested on jobs whose last successful run
was Thursday — Hermes appears to compute Friday as a weekend day under
some condition.

## Impact

Today's session (Friday 2026-05-15):
- No fresh `shark_pre_market` candidate evaluation. Dashboard shows
  yesterday's BEAR_VOLATILE classification and yesterday's skip list
  (NVDA confirmed, AMD/GOOGL/CRDO/AVGO/ORCL skipped). The new STALE
  pill on the SharkBriefing card correctly flags this.
- No TFT model smoke test today.
- All other phases fire normally (wheel_*, shark_market_open, shark_midday,
  shark_daily_summary, risk_monitor, sentiment, market_research).

## Mitigation Applied (2026-05-15 09:03 ET)

Host crontab entry added as a bypass:

```
0 9 * * 1-5 /usr/bin/flock -n /tmp/shark_pre_market.lock \
  /home/saijayanthai/.hermes/scripts/shark_pre_market.sh \
  >> /home/saijayanthai/Documents/trading-bot/user_data/logs/cron-shark-pre-market.log 2>&1
```

`flock` prevents double-fire if Hermes ever recovers and fires the same
slot. Backup of pre-bypass crontab: `/tmp/crontab.bak.pre-shark-bypass.*`.

`stocks_tft_smoke` has no host-crontab bypass yet — its impact is non-
critical (smoke test only). Operator can decide whether to add a
parallel entry.

## Root Cause — NOT YET DIAGNOSED

This is a Hermes-internal scheduler bug. Possible causes:
1. Day-of-week math in Hermes's croniter equivalent that mis-handles
   Friday after a Thursday run (off-by-one in the day-of-week iteration).
2. Timezone parsing issue between Hermes scheduler's clock and the
   cron expression evaluation context.
3. Hermes scheduler restart between Thursday's run and today, where the
   on-disk `next_run_at` was loaded as the literal future date without
   recomputation against the current wall clock.

## Reproduction

Wait until the next Thursday a `1-5` job runs. If the same pattern
recurs on the following Friday, the bug is reproducible. If not, it
may have been a transient state corruption from a scheduler restart.

## Follow-up Work

- [ ] Hermes scheduler source dive — find the cron iteration code,
      identify the day-of-week handling bug.
- [ ] Add a Hermes self-health-check job that scans all enabled jobs
      and alerts if any have `next_run_at` more than 1 cron period in
      the future.
- [ ] Consider migrating critical jobs (shark_*, wheel_*) to host
      crontab permanently, with Hermes used only for non-trading
      automation. Reduces dependency on a single scheduler.

## Files Modified

- Host crontab: +1 entry (shark_pre_market bypass)
- `audit/2026-05-15-hermes-friday-skip-bug.md` (this file)

## Files NOT Modified

- `~/.hermes/cron/jobs.json` — left intact. Scheduler state will
  self-correct when Hermes next ticks Monday.
- Hermes source / install — out of scope for the 30-min pre-market sprint.
