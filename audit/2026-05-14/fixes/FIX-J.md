# FIX-J — dashboard frontend JS null-safe percentage rendering

**Lane:** dashboard frontend JS — `ops_spa.js` card 15 (`TradesRiskLive`).
**Wall-clock:** ~15 min.
**Status:** deployed + verified (image-baked path via `docker cp`).

## Bugs targeted

| # | symptom | file:line | root cause |
|---|---|---|---|
| 1 | `DAY %  +0.00%` on card 15 even though API returns `daily_pnl_pct: null` | `user_data/dashboard/static/js/ops_spa.js:4061` (pre-fix) | `Number(env.daily_pnl_pct \|\| 0) * 100` coerced null to 0 BEFORE `fmtPct`'s own null-guard could fire |
| 2 | `DD 30d  -360.16%` on card 15 | producer-side (FIX-I lane) — consumer at `:4062` already obeys the fraction-x100 convention correctly | not a consumer bug; documented and verified |

## Diffs (file:line)

### `user_data/dashboard/static/js/ops_spa.js:4054-4066` — Fix 1 source

Before:
```js
const dayPnl = Number(env.daily_pnl_usd || 0);
const dayPct = Number(env.daily_pnl_pct || 0) * 100;
const dd30 = env.drawdown_pct_30d != null ? Number(env.drawdown_pct_30d) * 100 : null;
```

After:
```js
// null-render fix (FIX-J 2026-05-14): never coerce null to 0 here; let
// the render branch decide between fmtPct(value) and "—". The old
// `Number(env.daily_pnl_pct || 0) * 100` produced a fake "+0.00%" when
// the API returned daily_pnl_pct: null (the producer leaves it null
// when day_start_equity is unavailable — see ops_db.py:389).
const dayPnl = Number(env.daily_pnl_usd || 0);
const dayPct = env.daily_pnl_pct != null ? Number(env.daily_pnl_pct) * 100 : null;
const dd30 = env.drawdown_pct_30d != null ? Number(env.drawdown_pct_30d) * 100 : null;
```

### `user_data/dashboard/static/js/ops_spa.js:4097-4098` — Fix 1 render

Before:
```js
h("div", { className: "v3-num " + (dayPct >= 0 ? "up" : "down") }, fmtPct(dayPct)),
```

After:
```js
h("div", { className: "v3-num " + (dayPct == null ? "dim" : (dayPct >= 0 ? "up" : "down")) }, dayPct != null ? fmtPct(dayPct) : "—"),
```

The `className` had to be guarded too — `null >= 0` is `false` in JS, so the old code would have applied the `down` (red) class to a `null` value even after fixing `fmtPct`.

### `dd_30d` audit — no consumer-side change needed

- Only one render callsite: `ops_spa.js:4062` (now `:4067` after the comment block grew).
- It already multiplies by `100` exactly once. No `* 1000`, no special-case workaround.
- When FIX-I lands the fraction at the API edge (currently `-3.6016`, will become `-0.0360`), this consumer will render `-3.60%` correctly. No further change required here.

### `daily_pnl_pct` second callsite — left alone intentionally

`ops_spa.js:4179` (now `:4184`) uses `Number(tr.daily_pnl_pct || 0)` inside `lossUtil = Math.abs(dayFrac) / haltPct`. This is math, not display — coercing null to 0 here means "no daily PnL contribution to halt utilization", which is the correct semantic.

## Cache-buster bump

`v4-cutover-016-fixes` -> `v4-cutover-017-fixes-2` in:
- `user_data/dashboard/templates/dashboard_spa.html` (css + qc_react + components + dashboard_spa.js)
- `user_data/dashboard/templates/ops_spa.html` (qc_react + ops_spa.js; css stayed at `v3-prod-005`, components stayed at `v3-prod-005` per their own convention)

## Verification (run after edits)

- JS syntax check on both files via node `-e` parser — both passed (`dashboard OK`, `ops OK`).
- `docker cp` of 4 files into container `dashboard`.
- Sentinel grep inside container: `docker exec dashboard grep -c 'null-render fix (FIX-J' /app/dashboard/static/js/ops_spa.js` -> `1`.
- Template cache-buster confirmed via curl:
  - `curl -sS http://127.0.0.1:8081/ops` -> `ops_spa.js?v=v4-cutover-017-fixes-2`
  - `curl -sS http://127.0.0.1:8081/` -> `dashboard_spa.js?v=v4-cutover-017-fixes-2`
- Source line confirmation via curl:
  - `curl -sS http://127.0.0.1:8081/static/js/ops_spa.js | grep 'daily_pnl_pct'`
    shows new `env.daily_pnl_pct != null ? Number(env.daily_pnl_pct) * 100 : null;` at line 4066.
- API state right now (FIX-I unlanded):
  - `daily_pnl_pct: null` (Fix 1 will render `—` instead of `+0.00%`)
  - `daily_pnl_usd: 86.92` (unchanged)
  - `drawdown_pct_30d: -3.6016` (FIX-I-dependent; consumer renders `-360.16%` until producer normalizes)

Browser refresh on `/ops` will now show:
- `DAY %  —`  (was lying `+0.00%`)
- `DD 30d  -360.16%`  (FIX-I-dependent: lands as `-3.60%` once producer normalizes to fraction)

## Files touched

- `user_data/dashboard/static/js/ops_spa.js` (Fix 1 source + render; `dd_30d` audit unchanged)
- `user_data/dashboard/templates/ops_spa.html` (cache-buster bump)
- `user_data/dashboard/templates/dashboard_spa.html` (cache-buster bump)

## Files NOT touched (per brief)

- `user_data/dashboard/ops_routes.py` — FIX-I lane
- `user_data/dashboard/ops_db.py` — FIX-I MAY touch
- `stocks/**` — FIX-H lane
- `qc_react.js` — NumberRoll wrapper null-guard already in place per FIX-D today
- `dashboard_spa.js` — no `daily_pnl_pct` or `drawdown_pct_30d` render callsites; cache-buster bump only via template

## Convention reaffirmed (post-fix)

Per `ops_db.py:11-14` and the 2026-05-14 audit memo: **all `_pct` fields are FRACTIONS at the API edge; the display layer multiplies by 100 exactly once at the render boundary**. The consumer-side audit found:

| field | callsite | x100 count | status |
|---|---|---|---|
| `daily_pnl_pct` (card 15) | `ops_spa.js:4066` | 1 (after fix) | OK |
| `daily_pnl_pct` (lossUtil math) | `ops_spa.js:4184` | 0 (math) | OK |
| `drawdown_pct_30d` | `ops_spa.js:4067` | 1 | OK |
| `pnl_pct` (live_tape row) | `ops_spa.js:4109` | 1 | OK |
| `pnl_pct` (positions) | `dashboard_spa.js:1009` | 1 | OK |

No double-multiplications, no `* 1000` workarounds, no special-cases.
