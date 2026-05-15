## Scope

118 `trade_journal` rows with `regime IS NULL` spanning `opened_at 2026-05-13 15:09:25 UTC → 2026-05-14 16:41:54 UTC` (closed_at window: `2026-05-13 15:14:26 → 2026-05-14 17:00:58 UTC`); all rows carry `exit_reason` matching `V4 SELL signal (...)`.

---

## Writer

**Two writers** insert `regime` into `trade_journal`, and they behave differently:

### Writer 1 — FreqTrade MonitoringMixin (pre-cutover)
- File: `user_data/modules/monitoring_mixin.py:208`
- Path: `_record_trade_entry()` → `self._journal.log_entry(..., regime=latest.get("regime"), ...)`
- The `regime` value originates from the FreqAI strategy's `_latest_signals_for(pair)` dict, which populates it from the HMM classifier output.
- These rows exist before 2026-05-13 15:09 UTC and have populated regime values.

### Writer 2 — V4 paper engine (`run_v4_shadow.py`)
- File: `scripts/run_v4_shadow.py:1110–1115`
- Path: `fill_pending_proposals()` → `_write_trade_journal_row(cur, ..., regime=..., confidence=...)`
- Commit `68758a1` (2026-05-13 10:10 EDT = 14:10 UTC) introduced this writer. Its initial implementation hardcoded `NULL, NULL` for `confidence` and `regime` in the BUY INSERT:

```sql
-- ORIGINAL (68758a1): hardcoded NULLs — regime never stamped
INSERT INTO public.trade_journal
    (external_id, pair, direction, opened_at, entry_price, stake,
     confidence, regime, reasoning)
VALUES (%s, %s, 'long', NOW(), %s, %s, NULL, NULL,
        'V4 paper fill from quanta-core (' || %s || ')')
```

- The V4 engine's first paper fills arrived ~15:09 UTC on 2026-05-13, matching the start of the NULL gap exactly.

**Yes — the "V4 paper engine" is the sole writer responsible for the NULL gap.** FreqTrade was retired at cutover; all fills from 2026-05-13 15:09 UTC onward come from `run_v4_shadow.py`.

---

## Hypothesis Check

### (a) HMM regime classifier was down during the gap window
**REJECTED.**

`regime_log` has continuous rows from `2026-05-13 12:00 UTC` through `2026-05-14 18:00+ UTC` with no gap — 46 rows across the window, all successfully written:

```
2026-05-13 15:54:38  trending_up    prob=1.0
2026-05-13 16:55:04  trending_up    prob=1.0
...
2026-05-14 16:06:51  mean_reverting prob=0.73
2026-05-14 16:45:55  trending_up    prob=1.0
```

`user_data/logs/cron-hmm-refit.log` shows a successful refit at `2026-05-14 18:00:45`. The in-process regime detector in `run_v4_shadow.py` (started at `2026-05-14 12:58:17` per `regime.log`) was also writing continuously.

The `/api/ops/regime` endpoint was serving live regime labels throughout. The `fetch_regime()` function at line 1289–1290 defaults to `"unknown"` on HTTP failure — which would still be a non-NULL string — but this path is not what caused the gap.

### (b) V4 paper engine was passing `regime=None` because its lookup returned None
**CONFIRMED — but the root cause is a code bug, not a lookup failure.**

Commit `68758a1` introduced `_write_trade_journal_row()` with the `regime` and `confidence` parameters **not yet existing** on the function signature. The BUY INSERT was hardcoded to `NULL, NULL`:

```python
# 68758a1 — _write_trade_journal_row signature had NO regime/confidence args
async def _write_trade_journal_row(cur, *, coid, symbol, side, qty, price) -> None:
    ...
    VALUES (%s, %s, 'long', NOW(), %s, %s, NULL, NULL, ...)
```

The `fill_pending_proposals()` caller did not pass regime at all:

```python
# 68758a1 call site — no regime passed
await _write_trade_journal_row(
    cur, coid=coid, symbol=symbol, side=side,
    qty=qty, price=price,
)
```

The `regime_label` variable WAS in scope at the outer cycle loop (`run_v4_shadow.py:1290`) and was being passed into the `intent` JSON of proposals at line 1467 — but the fill-side journal write did not read back from `intent`. So regime was available at proposal time but never threaded into the journal write.

### (c) Schema/code change landed during the window and reverted
**REJECTED as a primary cause; a CODE change is the correct framing.**

`git log --since="2026-05-12" --until="2026-05-15"` shows:

- `68758a1` (2026-05-13 14:10 UTC): introduced `_write_trade_journal_row()` with NULL regime — **this is the bug origin**
- `fc8ee26` (2026-05-14 16:48 UTC): fixed both the `pnl_pct` fraction unit bug AND added `regime`/`confidence` parameters to `_write_trade_journal_row()`, reading from `intent` JSON

No schema change occurred. The `trade_journal` schema allowed NULL in the `regime` column throughout. This was purely a code omission in the initial Track A implementation.

---

## Why It Self-Healed

**Commit `fc8ee26` was deployed at approximately 2026-05-14 16:48 UTC (authored 12:48 EDT).**

The fix:
1. Added `regime: str | None = None` and `confidence: float | None = None` to `_write_trade_journal_row()`
2. Changed the BUY INSERT from hardcoded `NULL, NULL` to `%s, %s` with the actual values
3. Updated `fill_pending_proposals()` to extract `regime_val` from the `intent` JSON of proposals, then pass it through

After deployment + engine restart, the first BUY INSERT with `opened_at = 2026-05-14 16:45:55 UTC` carries `regime=trending_up, confidence=0.4`. The last NULL-regime BUY row opened at `16:41:54 UTC`, confirming the fix took effect within one ~5-minute cycle after the restart.

The `intent` JSON column on `proposals` had always stored the regime label (line 1467 was present from `68758a1`). The fix simply wired the read-back path.

**DB evidence of exact boundary:**
```
opened_at 2026-05-14 16:41:53 UTC  →  regime=NULL  (last NULL row)
opened_at 2026-05-14 16:45:55 UTC  →  regime=trending_up  (first fixed row)
```

---

## Recommended Action

**Backfill is possible and low-risk; evidence supports it.**

The `intent` JSONB column on `quanta_schema.proposals` stores the regime label at proposal write-time for every affected BUY. You can join on `external_id = client_order_id` to recover the correct regime for each NULL row:

```sql
-- PREVIEW before running
SELECT tj.trade_id, tj.pair, tj.opened_at,
       p.intent->>'regime' AS recovered_regime,
       (p.intent->>'conviction')::float AS recovered_confidence
FROM public.trade_journal tj
JOIN quanta_schema.proposals p
  ON p.client_order_id = tj.external_id
WHERE tj.regime IS NULL
  AND p.intent->>'regime' IS NOT NULL
LIMIT 20;

-- APPLY backfill (run after verifying preview)
UPDATE public.trade_journal tj
SET    regime     = p.intent->>'regime',
       confidence = (p.intent->>'conviction')::float,
       updated_at = NOW()
FROM   quanta_schema.proposals p
WHERE  p.client_order_id = tj.external_id
  AND  tj.regime IS NULL
  AND  p.intent->>'regime' IS NOT NULL;
```

**Caveats:**
- `external_id` is the `client_order_id` from the BUY fill; SELL-side rows close the open row but do not have their own `external_id` set — regime on the closed row comes from the BUY's INSERT, so the BUY backfill covers both open and close.
- Rows where `external_id IS NULL` (pre-V4 MonitoringMixin rows) are unaffected — they already have regime.
- Run in a transaction, check `COUNT(*)` from the preview before committing.

If Sharpe / drawdown / readiness calculations are regime-stratified, backfilling will materially improve those metrics for the 118 affected rows. If the dashboard only reads current regime for gating purposes (not historical), leaving it as-is is also safe.

---

## Confidence

**HIGH** for the root cause (code bug in 68758a1 + fix in fc8ee26).

Evidence chain is complete:
- Git diffs show exact before/after state of `_write_trade_journal_row()`
- DB query confirms NULL gap aligns precisely with `opened_at` of first V4 BUY fill (2026-05-13 15:09 UTC ≈ 5h after 68758a1 deploy)
- First populated row's `opened_at` (16:45 UTC) aligns with engine restart after fc8ee26 (committed 16:48 UTC)
- `regime_log` shows continuous writes throughout — HMM was never down
- `intent` column on proposals preserved the regime label throughout, confirming data existed but was not wired through

**What would raise confidence further:** Confirm that `quanta_schema.proposals` rows for the 118 affected trades have `intent->>'regime'` populated (preview query above). If they do, backfill is fully recoverable.
