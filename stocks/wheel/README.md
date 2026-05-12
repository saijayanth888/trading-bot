# wheel — cash-secured-put + covered-call income strategy

Sister to `shark/` inside `trading-bot/stocks/`. Lives outside the shark
package because shark's CLAUDE.md hard-bans options ("NO OPTIONS. EVER.")
— this module respects that boundary.

## Why this exists

Per the research synthesis the operator and Claude did on 2026-05-10, the
wheel is the realistic income engine for this account. Crypto grid bots
on Coinbase don't work in current bear regime + Intro 1 fee tier. Shark's
momentum trading is a strategic bet, not income. The wheel is mechanical,
delta-neutral-ish, and pays weekly premium.

Realistic APR per the research: **15–30%** on deployed wheel capital.
Reference: CBOE PUT Index = 9–18%/yr historical.
Pilot target: **$5K → ~$130–200/month** (single-ticker SOFI), then scale to
a 4–5-ticker basket → $300–500/month total.

## Files

| File | Purpose |
|---|---|
| `__init__.py` | Auto-loads `trading-bot/.env` on import — same pattern as `shark/run.py`. |
| `config.py` | Typed `WheelConfig` + env loader. Every knob is `WHEEL_*`-prefixed. |
| `strategy.py` | Pure functions: `filter_puts`, `filter_calls`, `score_contract`, `select_best`, `is_earnings_blackout`, `profit_take_threshold`. Zero IO. |
| `broker.py` | Alpaca client wrapper. Uses `OptionSnapshotRequest` (greeks + quotes in one call). |
| `state.py` | Local journal: `state/positions.json` (open puts/calls/shares), `state/trades.jsonl` (closed cycles), `state/kill_flags.json` (90-day per-ticker bans). Atomic writes via `shark.memory.atomic`. |
| `runner.py` | Three one-shot entry points: `sell_csps()`, `profit_take_check()`, `sell_covered_calls()`. Cron-friendly. |
| `cli.py` | `python -m wheel.cli {sell-csps,profit-take,sell-covered-calls,status,cancel-stale,kill-ticker}` |

Tests: `stocks/tests/test_wheel_strategy.py` — 26 unit tests, no Alpaca.

## Pilot config (current defaults)

```
WHEEL_SYMBOLS              SOFI                  (start single-ticker)
WHEEL_DELTA_MIN/MAX        0.25 / 0.35           (target ~30 delta)
WHEEL_DTE_MIN/MAX          7 / 10                (weekly cycle)
WHEEL_MIN_OI               500                   (liquidity floor)
WHEEL_MIN_YIELD_PCT_WEEK   0.008                 (0.8% per week min)
WHEEL_MAX_RISK_PER_TICKER  $1700                 (1 SOFI contract)
WHEEL_MAX_TOTAL_COLLATERAL $5000                 (pilot cap)
WHEEL_PROFIT_TAKE_PCT      0.50                  (close at 50% of credit)
WHEEL_DELTA_ROLL_TRIGGER   0.50                  (roll when delta past 50)
WHEEL_KILL_LOSS_PER_CYCLE  $500                  (cycle stop-out)
WHEEL_EARNINGS_BLACKOUT    3 days                (skip CSPs near earnings)
```

Override any knob via env var; defaults in `wheel/config.py`.

## How it runs

Three cron jobs, each a one-shot. Wired via Hermes cron with `--no-agent`
mode (no LLM tokens consumed; the Python runner makes all decisions).

| Cron | Schedule (ET) | Action |
|---|---|---|
| **Sell CSPs** | Friday 11:00 AM | `python -m wheel.cli sell-csps` — pick best put per allowed ticker, STO at mid |
| **Profit-take** | Mon–Fri 10:00 AM, 2:00 PM | `python -m wheel.cli profit-take` — BTC any short put at 50% gain |
| **Covered calls** | Monday 11:00 AM | `python -m wheel.cli sell-covered-calls` — for each assigned ticker, sell 30-delta CC ≥ cost basis |

(These crons aren't yet registered with Hermes — that's "Phase 1.6"
following the operator's `kb-only` choice. Wire them when you're ready
for the pilot to fire automatically.)

## Manual usage

```bash
cd stocks && source venv/bin/activate

# Read-only: show config, account, positions, P&L
python -m wheel.cli status

# Probe what we'd buy/sell without placing orders (dry-run not yet implemented)
WHEEL_DTE_MIN=5 WHEEL_DTE_MAX=14 python -c "
from wheel.broker import from_env
from wheel.config import load_config
from wheel.strategy import filter_puts, select_best
b = from_env()
raw = b.list_put_contracts('SOFI', 5, 14)
print(select_best(filter_puts(raw, load_config()), n=1))
"

# Place orders for real (paper account)
python -m wheel.cli sell-csps          # Friday morning
python -m wheel.cli profit-take         # any weekday
python -m wheel.cli sell-covered-calls  # after assignment

# Cancel stale DAY orders that didn't fill by EOD
python -m wheel.cli cancel-stale --max-age 240

# Manually kill a ticker for 90 days (e.g. after a structural-break loss)
python -m wheel.cli kill-ticker SOFI
```

## Safety rails

- **Kill switch shared with shark** — if `stocks/memory/KILL.flag` exists,
  `sell_csps` and `sell_covered_calls` refuse. (Profit-take still runs —
  closing positions is always allowed.)
- **Per-ticker 90-day ban** — `kill-ticker SOFI` writes `state/kill_flags.json`,
  blocks new CSPs on that name for 90 days. Use after a -$500 cycle.
- **Buying-power check** before every CSP — won't STO if collateral exceeds
  account buying power.
- **Cost-basis floor on covered calls** — never sells a CC below the
  assigned cost basis (would lock in a loss).
- **Earnings blackout** — `is_earnings_blackout()` helper; runner skips
  CSPs within 3 days of earnings. (Earnings calendar feed wiring is
  pending — currently `next_earnings=None` falls through.)

## Pilot pass / fail criteria (30 days)

**PASS** = 4 weekly cycles, ≥ +1.5% on collateral after fees ($75+ on $5K),
zero unhandled exceptions, one assignment cycle handled cleanly.
→ **Scale to 4–5 ticker basket** with $10–13K.

**FAIL** = -3% drawdown in a single week, OR cron job missed twice, OR
unhandled exception leaves a position dangling >24h.
→ **Stop, debug, do not scale.**

## What's NOT yet wired

- Earnings calendar feed (currently `is_earnings_blackout` always returns
  False because `next_earnings=None`). Roadmap: pull from
  `shark.data.earnings` (which already exists) or hit Alpaca's earnings
  endpoint.
- Hermes cron job registration — when ready, run:
  ```
  hermes cron create '0 15 * * 5' --name wheel_sell_csps --script wheel_sell_csps.sh --no-agent --workdir $HOME/Documents/trading-bot/stocks
  hermes cron create '0 14,18 * * 1-5' --name wheel_profit_take --script wheel_profit_take.sh --no-agent --workdir $HOME/Documents/trading-bot/stocks
  hermes cron create '0 15 * * 1' --name wheel_sell_calls --script wheel_sell_covered_calls.sh --no-agent --workdir $HOME/Documents/trading-bot/stocks
  ```
  (Wrapper scripts to be written under `~/.hermes/scripts/`.)
- Telegram delivery — `runner.*` returns a summary dict ready for piping
  to `notify.sh`; not yet wired.
- Multi-leg orders (vertical spreads, iron condors) — Alpaca options
  Level 3 supports them but the wheel only needs single-leg.

## References

- alpacahq/options-wheel — official Alpaca template (we adapted the
  filter/score/select pattern from `core/strategy.py`).
- alpaca-py SDK — `OptionSnapshotRequest` is the right call for greeks
  + quotes in one round-trip; `get_option_contracts` alone returns
  delta=0 always.
- Cboe PUT Index — long-run benchmark for cash-secured put strategies
  (9–18% APR historical depending on volatility regime).
