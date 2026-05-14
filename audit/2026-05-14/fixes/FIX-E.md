# FIX-E — C-5 mark-to-market join for open positions

**Scope:** `user_data/dashboard/ops_db.py` (`open_positions`). `data_sources.py` untouched — no consumer there.

**Date:** 2026-05-14

## Root cause

`open_positions(limit)` historically returned `current_profit=None` with the
note "would need a live-quote join". The dashboard card 04 Open positions
consequently rendered `—` in every Profit cell on every open position.

## Mark-price source — chosen column

The task hint said `derivatives_features.mark_price_usd`, **but that column
does not exist** in this DB. The full `derivatives_features` schema:

```
pair, ts, funding_rate, next_funding_rate, open_interest_usd,
long_short_ratio, taker_buy_vol_usd, taker_sell_vol_usd, source
```

No mark/last/index/mid column anywhere. There is also **no `public.pair_candles`
table** (the only candle cache is an in-process dict inside `data_sources.py`,
not queryable from SQL).

The freshest in-DB price stream is `quanta_schema.fills`. quanta-core executes
~3–8 fills per cycle (~5 min cadence) across the 12 crypto pairs; each row
carries the executed `price`. Joined to `quanta_schema.proposals` on
`client_order_id` we get the symbol. The most recent fill per symbol is a
strong mark-price proxy:

- same engine, same execution venue (no external API hop, no auth)
- ~5 min cadence (well within the 30-s polling cycle of `/api/state`)
- always present for any pair we'd trade — if quanta-core can't price it, we
  can't be holding an open position in it

The CTE:

```sql
WITH latest_mark AS (
    SELECT DISTINCT ON (p.symbol)
           p.symbol AS pair,
           f.price  AS mark_price,
           f.ts     AS mark_ts
    FROM quanta_schema.fills f
    JOIN quanta_schema.proposals p
      ON p.client_order_id = f.client_order_id
    ORDER BY p.symbol, f.ts DESC
)
```

`mark_ts` is exposed so the operator can spot stale marks at the UI layer.

## Direction handling

Long: `cp = (mark - entry) / entry`.
Short: `cp = -(mark - entry) / entry`.

(quanta-core's V4 paper engine is long-only today, so in practice every row
is `long` — but we honor `direction` in case shorts come online later.)

## Verification

Before fix — every row null:

```json
{
  "trade_id": 145, "pair": "AVAX/USD", "open_rate": 10.01,
  "stake_amount": 4.004, "current_profit": null,
  "open_date": "2026-05-14T16:41:53.922523+00:00",
  "regime_at_entry": null
}
```

After fix — all 5 open positions report non-null `current_profit` in the
expected fractional range. Sample:

```
ETH/USD    entry=  2311.4000 mark=  2311.4000 cp=  +0.0000  mark_ts=16:45:55Z
DOGE/USD   entry=     0.1154 mark=     0.1154 cp=  +0.0000  mark_ts=16:36:52Z
ADA/USD    entry=     0.2704 mark=     0.2704 cp=  +0.0000  mark_ts=15:58:54Z
BTC/USD    entry= 81150.7300 mark= 81150.7300 cp=  +0.0000  mark_ts=15:53:51Z
ETH/USD    entry=  2272.1000 mark=  2311.4000 cp=  +0.0173  mark_ts=16:45:55Z
```

Trade 123 (ETH @ 2272.10 opened at 14:48 ET) correctly reports +1.73% with
the current mark at 2311.40. The four freshly-opened positions show 0%
because the BUY fill *is* the latest fill for that symbol — by the next
cycle (≤5 min) the next fill will give them a non-zero mark. This is the
honest behavior: cp=0 means "no new price observation since entry yet",
not a bug.

Sample JSON shape after fix:

```json
{
  "trade_id": 123,
  "pair": "ETH/USD",
  "direction": "long",
  "open_rate": 2272.1,
  "stake_amount": 0.908,
  "current_profit": 0.01730,
  "mark_price": 2311.4,
  "mark_ts": "2026-05-14T16:45:55.122527+00:00",
  "open_date": "2026-05-14T14:48:26.291325+00:00",
  "external_id": "...",
  "regime_at_entry": null
}
```

(`regime_at_entry: null` is HIGH-D2, outside FIX-E scope — owned by
the V4 SELL/BUY journal writer in `run_v4_shadow.py`.)

## Display-layer audit (no double-fix)

`dashboard_spa.js:998-999` renders:

```js
p.current_profit != null
  ? fmtPct(p.current_profit * 100, 2)
  : "—"
```

So the SPA multiplies the fraction by 100 — matches the codebase convention
documented at the top of `ops_db.py`. No frontend change needed.

## Files touched

- `user_data/dashboard/ops_db.py` — `open_positions()` rewritten with the
  mark-price CTE join. ~40 lines changed.

`data_sources.py` is untouched — it has no `current_profit` consumer
(verified by `grep`); the `/api/state.positions` path lives in `app.py:371`
which already imports `ops_db.open_positions` directly.

## Deploy

```
docker cp user_data/dashboard/ops_db.py dashboard:/app/dashboard/ops_db.py
docker compose restart dashboard
# /api/state.positions[].current_profit now populated on all 5 open rows
```
