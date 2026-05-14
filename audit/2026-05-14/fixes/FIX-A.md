# FIX-A — `scripts/run_v4_shadow.py`

**Agent:** FIX-A (parallel wave, 2026-05-14)
**Owned file:** `scripts/run_v4_shadow.py` (only)
**Fixes applied:** C-1 (CRIT) and H-D2 (HIGH) from `audit/2026-05-14/verdict.md`

---

## C-1 — `pnl_pct` unit drift (CRIT)

**Symptom:** Slack preview shows +567% daily P&L; MaxDD = 430%; same dollar P&L renders as 4 different percentages.

**Root cause:** `_write_trade_journal_row` for V4 SELL wrote
`pnl_pct = ((exit - entry) / entry) * 100.0`, i.e. **percent**.
Every dashboard reader (`ops_db.py:11-14` comment, `data_sources.fetch_recent_trades`,
`ops_routes.{trades_risk_summary, slack_preview, _evaluate_readiness_inline,
_compute_rebalance, timeline}`, `dashboard_spa.RecentTrades`) treats it as a
**fraction** and multiplies × 100 again → 100× inflation.

**Fix:** Dropped the `* 100.0` in the UPDATE so the column holds a fraction.
Stamped the canonical unit convention in the docstring so the next producer
doesn't drift again.

```diff
-                   pnl_pct      = ((%s - entry_price) / NULLIF(entry_price, 0)) * 100.0,
+                   pnl_pct      = (%s - entry_price) / NULLIF(entry_price, 0),
```

Grep confirms `run_v4_shadow.py` does not read its own `pnl_pct` anywhere
(no double-multiplier risk inside the producer file).

---

## H-D2 — V4 SELL writes NULL `regime` + NULL `confidence` (HIGH)

**Symptom:** 137 trade_journal rows total, only 22 had regime/confidence
(the pre-cutover freqtrade rows). All 115 V4 rows since freqtrade
decommission were NULL/NULL → poisoned Tape card, nightly_reflector,
readiness gates.

**Root cause:** `_write_trade_journal_row` BUY-branch INSERT hardcoded
`NULL, NULL` for `confidence, regime` despite both values being available
in the calling scope (proposal `intent` JSON already contains `regime` and
`conviction`, written by `write_proposal_and_order` upstream).

**Fix:**
1. `_write_trade_journal_row` gained two keyword args:
   `regime: str | None = None, confidence: float | None = None`
   (default `None` so non-cycle callers — tests, migrations — still work).
2. The BUY INSERT now binds `%s, %s` for those columns.
3. `fill_pending_proposals` was updated to (a) SELECT `p.intent` along with
   the other proposal fields, (b) parse it as JSON (tolerating dict/str/None),
   (c) extract `intent.regime` + `intent.conviction`, (d) pass them to
   `_write_trade_journal_row`.
4. SELL UPDATE deliberately does NOT touch `regime`/`confidence` — the BUY
   row's values are preserved on close (they represent entry conditions,
   not exit). The audit only asked for entry-time stamping; that's
   semantically correct.

---

## Verification

```bash
cd /home/saijayanthai/Documents/trading-bot
python3 -m py_compile scripts/run_v4_shadow.py && echo SYNTAX_OK   # SYNTAX_OK
docker cp scripts/run_v4_shadow.py quanta-core:/app/run_v4_shadow.py
docker compose restart quanta-core
# Wait one 5-min cycle, then query trade_journal
docker exec tradebot-postgres psql -U tradebot -d tradebot -c "
SELECT pair, direction, pnl_pct, regime, confidence, opened_at, closed_at
FROM trade_journal
WHERE GREATEST(opened_at, COALESCE(closed_at, opened_at)) > NOW() - INTERVAL '10 min'
ORDER BY GREATEST(opened_at, COALESCE(closed_at, opened_at)) DESC LIMIT 10;"
```

### Before-fix sample (pre-restart fills at 16:41:53)
| pair | pnl_pct | regime | confidence |
|---|---|---|---|
| SOL/USD | **0.6591** | NULL | NULL |
| LTC/USD | -0.1549 | NULL | NULL |
| ETH/USD | 0.4545 | NULL | NULL |

`0.6591` is the percent-stored bug — would render as +65.9% if read
as a fraction. SOL did not move 65% in 48 min.

### After-fix sample (16:45:55, post-restart)
| pair | pnl_pct | regime | confidence | opened | closed |
|---|---|---|---|---|---|
| ETH/USD | NULL (open) | **trending_up** | **0.4** | 16:45:55 | — |
| LINK/USD | 0.0308 | NULL | NULL | 14:48:26 | 16:45:55 |
| DOT/USD | 0.0095 | NULL | NULL | 15:53:51 | 16:45:55 |
| ATOM/USD | -0.0063 | NULL | NULL | 16:31:51 | 16:45:55 |
| AVAX/USD | 0.0030 | NULL | NULL | 16:41:53 | 16:45:55 |

Fractional pnl_pct in [-0.05, +0.05] — realistic 5-min crypto moves.
ETH BUY opened POST-fix shows regime + confidence populated.

Closed SELL rows above kept NULL regime/confidence because their BUY
INSERT predates this fix — old open rows had NULL stored already and
the SELL UPDATE intentionally doesn't overwrite. New BUY→SELL pairs
opened after the deploy will carry the regime/confidence through close.

---

## Files touched
- `scripts/run_v4_shadow.py` (only) — ~50 lines changed across two functions

## Files NOT touched (other agents own these)
- Dashboard readers (`ops_routes.py`, `ops_db.py`, `data_sources.py`)
- Frontend SPAs (`dashboard_spa.js`, `ops_spa.js`, `qc_react.js`)
- Wheel, shark, hermes, modelforge subsystems

Downstream readers do NOT need code changes — they already assume
`pnl_pct` is a fraction and × 100 at display time. The producer fix
restores the convention across all ~7 surfaces in one shot.
