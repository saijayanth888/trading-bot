# FIX-I — Ops endpoints (gates, drawdown, wheel block)

**Lane:** `user_data/dashboard/ops_routes.py` (+ `user_data/dashboard/ops_db.py`
for the drawdown query — git was clean for both files so no overlap risk
with FIX-J).

**Date:** 2026-05-14

## Bug 1 — `/api/ops/gates` returned `n_gates=0` for 14/15 stock tickers

### Before
```
PLTR   n_gates=0 blocker=None venue=watchlist
NVDA   n_gates=0 blocker=None venue=watchlist
AMD    n_gates=0 blocker=None venue=watchlist
SPY    n_gates=0 blocker=None venue=watchlist
```
SOFI (the only wheel target) had its 8-gate chain wired; every other
ticker (NVDA/PLTR/AMD/SPY/TSLA/AAPL/GOOGL/MSTR/COIN/HOOD/MARA/F/QQQ/IWM)
was a watchlist row and emitted an empty `gates: []`. UI reads `n_gates=0`
as "fully permitted" — a confidently-displayed lie.

### Root cause
The `for sym in watchlist_symbols:` loop in `ops_routes.gates()` was
appending a placeholder row with `n_gates=0, gates=[]` for every
watchlist symbol. The shark phase decisions sitting in
`stocks/memory/DAILY-HANDOFF.md` were never threaded in.

### Fix
1. Added `_parse_shark_phase_decisions(handoff_file)` — parses the
   markdown handoff into per-symbol decisions, collapsing all phases
   (`pre-market`, `pre-execute`, `market-open`, `midday`, …) and all
   status keys (`confirmed`, `validated`, `traded`, `skipped`,
   `rejected`, `cuts`) into one `{pass, detail}` per ticker. Most-recent
   phase wins when a symbol has both positive and negative status.
2. Watchlist loop now emits a single `shark_phase_decision` gate per
   ticker. When the file is missing, stale, or doesn't mention the
   symbol, the gate reports `pass=None` with an explicit detail —
   honest, distinguishable from a `pass=False` block.

### After
```
NVDA   n_gates=1 blocker=None  | detail: 'confirmed@pre-market; validated@pre-execute'
AMD    n_gates=1 blocker=shark_phase_decision | detail: 'skipped@pre-market'
GOOGL  n_gates=1 blocker=shark_phase_decision | detail: 'skipped@pre-market'
PLTR   n_gates=1 blocker=None  | detail: 'no shark phase decision for this symbol today'
SPY    n_gates=1 blocker=None  | detail: 'no shark phase decision for this symbol today'
```

NVDA correctly resolves as confirmed today (matches DAILY-HANDOFF.md:
`confirmed: NVDA`, `validated: NVDA`). AMD/GOOGL correctly flag as
blocked (matches `skipped: AMD, GOOGL, CRDO, AVGO, ORCL`). Symbols not
mentioned today report it explicitly instead of pretending to be open.

---

## Bug 2 — `drawdown_pct_30d` was `-3.60` (unit drift → UI rendered `-360%`)

### Before
```
$ curl /api/ops/trades_risk | jq .data.drawdown_pct_30d
-3.6016019823392513         # UI: × 100 = -360.16%
```

### Root cause
`ops_db.trades_risk_summary()` computed:
```sql
WITH cum AS (
  SELECT closed_at, SUM(pnl_pct) OVER (ORDER BY closed_at) AS cum_pct
  FROM trade_journal WHERE closed_at IS NOT NULL
    AND closed_at > NOW() - INTERVAL '30 days'
)
SELECT MIN(cum_pct - max_cum) AS max_drawdown ...
```
`pnl_pct` is each trade's own-stake fractional return (e.g.
`-0.005225` = -0.5225%). Summing 50+ fractional returns across distinct
stakes is meaningless — same anti-pattern that produced the `+686%` day
P&L in H-1 / commit `00e6c85`. The result happened to land at -3.60
which the consumer's × 100 amplified to -360% on the UI.

### Fix
Rewrote the query to follow the module's canonical convention
(`ops_db.py:11-14`: every `_pct` is a FRACTION):

1. **Preferred path:** `quanta_schema.equity_snapshots` peak-to-trough:
   `MIN((equity − peak) / peak)` — exact when the table is populated.
2. **Fallback path:** synthesize equity curve from cumulative USD P&L,
   divide max-drawdown USD by `PAPER_ENGINE_START_EQUITY` (env, default
   $100,000). Every input is USD, output is a fraction — no unit drift.

### After
```
drawdown_pct_30d: -0.004302800803864112
  → if used as ×100 by UI: -0.4303%
```
Sensible — the paper engine has +$86 daily P&L and tiny trade sizes,
so a 0.43% trailing drawdown matches reality.

---

## Bug 3 — `/api/ops/stocks.wheel` returned zeros despite 4 open CSPs

### Before
The `wheel` block exposed `open_positions` (the row list) and
`cumulative_pnl_usd` (sum across the closed-trade ledger), but no
rolled-up KPIs. The user-facing card was reading roll-up fields that
didn't exist → all displayed as `0` / `null`.

`positions.json` had 4 short_put rows (NVDA, COIN, PLTR, MSTR)
totaling $1,745.50 in premium and $70,700 in collateral.

### Fix
Added 4 roll-up fields to the wheel block in `/api/ops/stocks`, derived
purely from `positions.json` (same source as `open_positions`):

* `open_csps` — count of `kind == "short_put"`
* `open_ccs` — count of `kind == "short_call"`
* `open_collateral_usd` — `Σ strike × 100 × |qty|` over short puts
  (matches `wheel/runner.py` pre-flight collateral math)
* `premium_collected_usd` — `Σ entry_credit` (premium booked on
  currently-open positions; lifetime closed-trade P&L stays in
  `cumulative_pnl_usd`)

### After
```
open_csps: 4
open_ccs: 0
open_collateral_usd: $70,700.00
premium_collected_usd: $1,745.50
cumulative_pnl_usd: $0.00
```
Exactly matches `positions.json`:
* NVDA 220p × 1 = $22,000 collateral, $616.00 premium
* COIN 190p × 1 = $19,000 collateral, $450.50 premium
* PLTR 127p × 1 = $12,700 collateral, $289.00 premium
* MSTR 170p × 1 = $17,000 collateral, $390.00 premium
* **Total: $70,700 collateral, $1,745.50 premium** ✓

---

## Files touched

* `user_data/dashboard/ops_routes.py` — `_parse_shark_phase_decisions`
  helper, watchlist gate emission, wheel roll-up fields.
* `user_data/dashboard/ops_db.py` — `trades_risk_summary` drawdown
  query reworked (equity_snapshots primary, USD-curve fallback).

## Verification

`python3 -m py_compile` clean on both files. `docker compose restart
dashboard` succeeded. All three live-curl checks (`/api/ops/gates`,
`/api/ops/trades_risk`, `/api/ops/stocks`) return the expected shapes
above.
