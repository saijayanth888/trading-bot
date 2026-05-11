# Wheel pilot pre-conditions audit — 2026-05-11

Agent D, sprint §8 / §6 / §9.5 of `POST_CUTOVER_FIXES_2026-05-11.md`.

Audited each cited file before patching; some line numbers in the source
spec drifted post-SPA-cutover. Per-finding verdict + change summary below.

---

## P0-EE — `assignment_check()` (already fixed)

**File:** `stocks/wheel/runner.py:212-331` (function), `:352-364`
(wired into `profit_take_check`).

**Verdict:** REAL bug was fixed in an earlier sprint; logic is correct.

**Evidence:**
- `assignment_check()` exists at runner.py:212 with the full three-step
  flow: option-still-open guard, expected_assigned_shares = 100 × qty
  matching, and the stale-row cleanup.
- On assignment it writes `Position(kind="long_shares",
  source="wheel_assignment", entry_price=pos.strike)` and marks the
  short_put as `status="assigned"` (kept on file for audit).
- The function IS called from `profit_take_check()` at runner.py:352-364
  before the profit-take pass; `open_csps` filters out
  `status="assigned"` rows after.
- `sell_covered_calls()` consults `kind == "long_shares"` rows (via
  `shares_held()` at state.py:134-138) so the assignment bridge connects
  the CSP leg → CC leg correctly.

**Action:** none. Document and move on.

---

## P1-S4 — `total_collateral_usd` cap not enforced (REAL — fixed this sprint)

**File:** `stocks/wheel/runner.py:140-206` (the `_try_sell_csp` body).

**Verdict:** REAL bug. `WheelConfig.max_total_collateral_usd` (default
$5000) was defined in config but never consulted. The runner enforced
`max_risk_per_ticker_usd` ($1700/symbol) and `buying_power`, but nothing
stopped the cycle from opening N symbols × $1700 = >$5000 collateral.

**Fix (commit 6b75ea9):**
- Added `_open_csp_collateral_total(positions)` helper that sums
  `strike × 100 × qty` over all `kind == "short_put"` rows whose status
  is not `"assigned"`.
- `sell_csps()` snapshots the running total ONCE per cycle, then refreshes
  after each successful entry so subsequent symbols in the loop see the
  updated total.
- `_try_sell_csp` now skips when `open_collateral_running + new_collateral
  > cfg.max_total_collateral_usd`. The skip line carries the math so the
  operator can see exactly which symbol tripped the cap.
- Summary envelope exposes `open_collateral_usd_pre`,
  `open_collateral_usd_post`, and `max_total_collateral_usd` so the
  Telegram digest + dashboard can render live cap occupancy.

**Test:** `tests/test_wheel_preconditions.py::test_total_collateral_cap_*`
(2 tests — trip and no-trip paths).

---

## P1-S5 — earnings blackout + kill_loss_per_cycle dead config (REAL — fixed this sprint)

**File:** `stocks/wheel/strategy.py:138-151` (`is_earnings_blackout`),
`stocks/wheel/config.py:55-58` (config fields).

**Verdict:** REAL bug — two pieces of dead config:

1. `is_earnings_blackout()` was implemented and unit-tested in
   strategy.py but never imported into runner.py.
2. `kill_loss_per_cycle_usd` (default $500) was defined in WheelConfig
   but never consulted before CSP entry.

**Fix (commit 6b75ea9):**

### P1-S5a — earnings blackout
- Added `_next_earnings_for(symbol)` helper that reads
  `stocks/wheel/state/earnings.json` (operator-written
  `{"SYMBOL": "YYYY-MM-DD"}` format). Missing file or missing symbol →
  returns None → no blackout enforced. Future work (post-pilot): wire
  the Shark analyst pipeline to write this file automatically.
- `_try_sell_csp` calls `is_earnings_blackout(next_earn,
  blackout_days=cfg.earnings_blackout_days)` and skips with a
  descriptive reason when within the window.

### P1-S5b — per-cycle kill
- Added `cumulative_pnl_for(underlying, since)` to wheel.state — same
  signature as the existing `cumulative_pnl` but filters by symbol.
- `_try_sell_csp` reads the rolling-30-day realized P&L per symbol; if
  below `-cfg.kill_loss_per_cycle_usd` it calls the existing
  `kill_ticker(sym, days=90)` and skips the entry.
- Window: 30 days. Picked so a single bad week early in the pilot can't
  game the gate, while still being short enough that the bot doesn't
  carry baggage from old losing streaks past the original 90-day cooldown.

**Test:** `tests/test_wheel_preconditions.py::test_earnings_blackout_*`
+ `::test_kill_loss_per_cycle_*` (4 tests).

---

## P1-S6 — broker enum (REAL — fixed this sprint; runtime-equivalent)

**File:** `stocks/wheel/broker.py:354-356` (line drifted from the spec's
:319-320).

**Verdict:** SOFT bug. `list_open_orders` passed `status="open"` as a
raw string. Runtime behavior is currently identical to
`QueryOrderStatus.OPEN` because the enum is a `str, Enum` (so
`"open" == QueryOrderStatus.OPEN`). Future alpaca-py releases could
tighten request validation; the enum is the convention the SDK ships
with.

**Fix (commit 1659bfb):**
- Imported `QueryOrderStatus` from `alpaca.trading.enums`.
- `list_open_orders` now passes `status=QueryOrderStatus.OPEN`.
- No behavior change in the current SDK version (verified that
  `QueryOrderStatus.OPEN.value == "open"`).

---

## §6 — `pre_execute` orphan + `stocks_day_runner.sh`

### pre_execute invokability — verified

**File:** `stocks/shark/phases/pre_execute.py` (exists, 209 LOC) plus
`stocks/shark/run.py:91` (PHASES registry).

**Verdict:** The phase IS dispatcher-registered. `python shark/run.py
pre-execute` works (the `_TRADING_PHASES` set at run.py:107 includes
`pre-execute`, and the kill-switch set at :117 lists it as well).

The helper script `/home/saijayanthai/.hermes/scripts/shark_pre_execute.sh`
exists and is wired correctly (cd into `$STOCKS`, activate venv, source
unified `.env`, exec `python shark/run.py pre-execute`, Slack-mirror
on DECISION/ENTRY/ERROR lines).

**What's still missing:** the Hermes cron registration at
`~/.hermes/cron/jobs.json` — verified no entry for `shark_pre_execute`.
That registration is **Agent B's** task per the dispatch brief, not
mine. No action here.

Recommended cron when Agent B wires it (per the doc §6 spec):
```json
{
  "id": "shark_pre_execute",
  "name": "shark_pre_execute",
  "schedule": "30 9 * * 1-5",
  "script": "shark_pre_execute.sh",
  "workdir": "/home/saijayanthai/Documents/trading-bot/stocks",
  "deliver": "telegram",
  "no_agent": true
}
```

### `scripts/stocks_day_runner.sh` audit

**File:** `scripts/stocks_day_runner.sh` (105 LOC, mtime 09:30 ET today).

**Verdict:** REDUNDANT in the steady-state flow, but **worth keeping as a
manual fire-everything tool**. Recommend: **keep, document, don't
schedule.**

#### What it does today

A bash loop that sleeps until each listed `HH:MM` ET, then fires the
matching helper script in `~/.hermes/scripts/`. Today's hardcoded slate:

| Slot | Script | Hermes cron equivalent |
|---|---|---|
| 09:00 | `shark_pre_market.sh` | `shark_pre_market` (00 9 * * 1-5) |
| 09:05 | `wheel_candles.sh` | `wheel_candles` (every 5 min during session) |
| 09:30 | `wheel_snapshot.sh` | `wheel_snapshot` |
| 09:35 | `shark_market_open.sh` | `shark_market_open` |
| 10:00 | `wheel_profit_take.sh` | `wheel_profit_take` |
| 11:00 | `wheel_sell_calls.sh` | `wheel_sell_covered_calls` |
| 13:00 | `shark_midday.sh` | `shark_midday` |
| 14:00 | `wheel_profit_take.sh` | `wheel_profit_take` |
| 15:30 | `wheel_candles.sh` | `wheel_candles` |
| 16:00 | `wheel_snapshot.sh` | `wheel_snapshot` |
| 17:30 | `shark_daily_summary.sh` | `shark_daily_summary` |
| 21:30 | `shark_kb_update.sh` | `shark_kb_update` |

**Every slot has a Hermes cron equivalent.** The day-runner adds zero
unique scheduling logic.

#### Why keep it anyway

The day-runner is a *manual* lever for the operator. Use cases that
Hermes crons don't cover:

1. **Hermes scheduler stuck.** Per the file's own comment (`# 09:24:11
   fast-forward evidence in ~/.hermes/logs/agent.log`), there's a known
   failure mode where Hermes holds `.tick.lock` during slow LLM crons,
   silently fast-forwarding script-only crons past their grace windows.
   When that happens, `nohup bash scripts/stocks_day_runner.sh &` is
   the no-cost recovery.

2. **Same-day backfill.** If the bot was off all morning (e.g. the
   Hermes restart that happened earlier today), the day-runner re-fires
   only the slots that haven't passed yet — `[[ "$target" < "$now_hm"
   ]]` skip at line 76. Cleaner than re-running each cron by hand.

3. **Pilot weekend testing.** The day-runner can be invoked with the
   weekday guard removed (manual edit) to drive the full pipeline against
   paper-mode Alpaca during a weekend dry-run.

#### Recommendation

**Keep `scripts/stocks_day_runner.sh`** in the repo. Add a header banner
documenting its manual-only role (already mostly there in the existing
docstring; could be tightened).

**Do NOT register it as a cron / systemd service.** Hermes is the
scheduler of record. The day-runner is an in-case-of-emergency shim.

No file changes needed beyond optionally tightening the header. Operator
preference flag — not auto-applied.

---

## Summary

| Finding | Verdict | Commit |
|---|---|---|
| P0-EE assignment_check | ALREADY FIXED | — |
| P1-S4 total_collateral cap | REAL — fixed | 6b75ea9 |
| P1-S5a earnings blackout | REAL — fixed | 6b75ea9 |
| P1-S5b kill_loss_per_cycle | REAL — fixed | 6b75ea9 |
| P1-S6 broker enum | SOFT — fixed | 1659bfb |
| pre_execute invokability | VERIFIED ok | — |
| stocks_day_runner.sh | KEEP as manual shim | — |

Tests: `tests/test_wheel_preconditions.py` adds 6 tests (3 fixtures × 2
paths each). Full wheel suite: 39 pass, 0 fail.

**Wheel pilot activation gates per the doc §8:**

- [x] P0-EE `assignment_check()` exists, is wired into `profit_take_check`
- [x] P1-S6 broker enum fix applied
- [x] P1-S4 `total_collateral_usd` cap enforced
- [x] P1-S5 `is_earnings_blackout()` consulted before each CSP sell
- [ ] **Pending operator step:** populate
  `stocks/wheel/state/earnings.json` with `{"SOFI": "<next-earnings-date>"}`
  before this Friday's pilot fire. Without this file the blackout gate
  silently allows the entry (by design — missing data ≠ blackout).
- [ ] **Pending operator step:** confirm `secrets/alpaca-options-paper.json`
  or env equivalent has options-trading entitlement on the paper account.
