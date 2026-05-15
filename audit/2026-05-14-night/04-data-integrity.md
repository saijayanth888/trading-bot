# Data-Integrity Audit — Postgres
**Run:** 2026-05-14 ~01:00 UTC night session  
**Scope:** READ-ONLY SELECT-only; `public.*` (legacy/dashboard) + `quanta_schema.*` (V4 paper engine)  
**Tables audited:** 19 (14 public + 5 quanta)  
**Source-of-truth schema:** `user_data/data/schema.sql` (DDL applied by `modules/db.ensure_schema()`)

---

## Headline counts

| Severity | Count |
|---|---|
| P0 (data corruption / unrecoverable) | 0 |
| P1 (active stale signal feeding live decisions) | 3 |
| P2 (stale historical column / quality drift) | 4 |
| P3 (cosmetic / dead-code / cleanup) | 4 |

## TL;DR

* The V4 ledger (`quanta_schema.proposals` ↔ `orders` ↔ `fills`) is **clean** — 354/354/354, zero orphans, sub-15-min lag, every closed trade in `trade_journal` (last 24 h, 109 with `external_id`) joins to a fill.
* `regime_log` gap that ended ~16:46 UTC has **NOT reopened** — every `trade_journal` row from 2026-05-14 16:46 UTC onward carries a regime label. ✅ Memory note `2026-05-14-regime-null-gap` still accurate.
* **P1 #1 — sentiment scorer collapsed since 2026-05-14 ~03:30 UTC.** All 56/121 rows in last 24 h have `sentiment_score=0`, `confidence=0`, `llama_score=NULL`, even though `n_headlines=60` (headlines flow). Recent 4 h: **100 % zero.** Live agents reading sentiment will see permanent neutral.
* **P1 #2 — three on-chain hypertables are empty.** `exchange_netflow`, `mvrv_ratio`, `whale_transactions` all have 0 rows. If any feature consumer expects them, it silently degrades.
* **P1 #3 — `quanta_schema.equity_snapshots` empty.** No daily equity recorded → no drawdown tracking, no scoreboard backfill.
* **Open trade liability:** 1 row in `trade_journal` with no close (trade_id 123, ETH/USD long, opened 2026-05-14 14:48 UTC — 10.2 h, stake $908.84). Only one position open in V4 history, looks intentional but worth verifying it is genuinely live.

---

## P0 — Data corruption / unrecoverable

*None.*

No rows with impossible primary-key state, no duplicate `(pair, opened_at)` in `trade_journal` (0), no duplicate `external_id` (0), no orphaned fills/orders/proposals (0/0/0/0).

---

## P1 — Active stale signal feeding live decisions

### P1-1 · Sentiment scorer is silently zeroed since 2026-05-14 03:30 UTC
**Symptom:** `sentiment_log` continues to insert ~4 rows/hr (correct cadence) **but** `sentiment_score`, `confidence`, and `llama_score` are all `0` / `NULL`; `market_impact` is hard-coded to `"neutral"`. `n_headlines` is non-zero (60), so the upstream news fetcher works — the LLM scorer (Ollama on the Spark) is producing nothing useful or being short-circuited.

```sql
-- evidence
SELECT ts, sentiment_score, confidence, market_impact, llama_score, n_headlines
FROM sentiment_log
ORDER BY ts DESC LIMIT 8;
-- last 8 rows: all 0/0/neutral/NULL/60

-- pattern start
SELECT MIN(ts), MAX(ts) FROM sentiment_log
WHERE ts > NOW() - INTERVAL '24 hours'
  AND sentiment_score=0 AND confidence=0 AND market_impact='neutral';
-- 2026-05-14 03:31:39 → 2026-05-15 00:46:48 (continuous)

-- last 24 h zero-rate
SELECT date_trunc('hour', ts) AS hr,
       COUNT(*) FILTER (WHERE sentiment_score=0 AND confidence=0) AS zeros,
       COUNT(*) AS total
FROM sentiment_log WHERE ts > NOW() - INTERVAL '24 hours'
GROUP BY 1 ORDER BY 1;
```

**Impact:** any agent / strategy that conditions on sentiment is reading a permanent flat zero.  
**Fix direction:** check Ollama `hermes3:8b` health on the Spark; check `sentiment_engine` cron logs since 03:30 UTC for fetcher/parse exceptions.

### P1-2 · Three on-chain hypertables are empty
Tables defined in `schema.sql` lines 13-39 but `COUNT(*)=0`:

| Table | Rows | Last ts |
|---|---|---|
| `public.exchange_netflow` | 0 | — |
| `public.mvrv_ratio` | 0 | — |
| `public.whale_transactions` | 0 | — |

```sql
SELECT 'exchange_netflow', COUNT(*) FROM exchange_netflow
UNION ALL SELECT 'mvrv_ratio', COUNT(*) FROM mvrv_ratio
UNION ALL SELECT 'whale_transactions', COUNT(*) FROM whale_transactions;
```

**Impact:** depends on whether `features_used` JSONB or the V4 strategy stack reads them. Per session-2026-05-09 EOD note these were deprecated in favour of `derivatives_features` (which is healthy: 8 232 rows, fresh `okx` source, 6 h lag — see P2-2). If they're truly retired, the tables should be dropped or this should be flagged as P3 schema-drift; if any code still references them, degradation is silent.  
**Fix direction:** `grep -r "exchange_netflow\|mvrv_ratio\|whale_transactions" modules/ user_data/strategies/` and either delete tables or restore feeders.

### P1-3 · `quanta_schema.equity_snapshots` empty
Table is created (see schema-v2 description: "performance indices + optional timescaledb hypertables on fills/decisions/equity_snapshots") but `COUNT(*)=0`.

```sql
SELECT COUNT(*) FROM quanta_schema.equity_snapshots;
-- 0
```

**Impact:** no daily equity / drawdown timeseries; `TodayScoreboard` and any P&L dashboards must compute live from fills+open positions every render. Drawdown auto-pause logic (if any) has nothing to read.  
**Fix direction:** confirm equity-snapshot cron exists; if missing, add daily 23:59 UTC writer.

---

## P2 — Stale historical column / quality drift

### P2-1 · 118 / 199 (59 %) historical `trade_journal` rows have `regime IS NULL`
All NULL trades sit between 2026-05-13 15:00 UTC and 2026-05-14 16:30 UTC. From 2026-05-14 16:46 UTC onward, every trade is stamped (verified — 3 NULL rows after 16:00 are the gap-tail at 16:31/16:36/16:41 UTC).

```sql
-- post-fix verification
SELECT COUNT(*) FROM trade_journal
WHERE opened_at > '2026-05-14 16:46:00+00' AND regime IS NULL;
-- 0
```

Also `confidence IS NULL` for the same 118 rows (correlates 1:1 with `regime IS NULL`) — both come from the same regime/meta-signal lookup that was failing.  
**Status:** **fix in production**, gap closed; this is recorded for auditing only. **Do not back-fill** without an offline regime classifier replay.

### P2-2 · `derivatives_features` + `macro_features` 6 h stale
Both stop at `2026-05-14 18:57:22 UTC` (≈6 h ago). Single source `okx` only.

```sql
SELECT source, MAX(ts), EXTRACT(EPOCH FROM (NOW()-MAX(ts)))/3600 AS hrs_old
FROM derivatives_features GROUP BY 1;
-- okx | 2026-05-14 18:57:22 | 6.02
```

Schema lists `okx | dydx | coinbase_intl | kraken_futures` — only OKX feeding. 6 h staleness exceeds the implicit 5-min cadence; either the cron stopped at 18:57 or the OKX poll is throttled.  
**Fix direction:** check `derivatives_poller` cron; verify OKX endpoint is responding; consider adding fallback source.

### P2-3 · `regime_log.state IS NULL` for 44 of 2 320 rows (1.9 %)
Older rows (pre-feb 2026 likely) are missing `state` integer. `regime` and `probability` are populated — the missing column is `state` (HMM internal state index) which is non-critical for downstream readers. Cosmetic.

### P2-4 · Sentiment auxiliary columns sparse
* `sentiment_log.llama_score IS NULL` for 124 / 687 rows (18 %) — overlaps with the active P1-1 outage and older bootstrap rows.
* `sentiment_log.fear_greed_value IS NULL` for 44 / 687 (6 %) — rows before fear-greed integration was added.
* `sentiment_log.n_headlines = 0` for 8 / 687 (1.2 %) — bootstrap.

Schema-drift but historical only.

---

## P3 — Cosmetic / dead-code / cleanup

* **P3-1** — `quanta_schema.reservations` always 0 rows. Designed-but-unused table (lifetime-of-an-order is so short reservations never persist). No bug, but if the design intent is "permanent reservations record" then a writer is missing.
* **P3-2** — `public.classifier_config` has no recorded write timestamps in this audit; small enough to skip but worth confirming there's a current row.
* **P3-3** — `public.fear_greed_log` only 7 rows total (one per day since 2026-05-09). Cadence correct (daily). No issue, just naming the cadence so future audits don't flag it.
* **P3-4** — `quanta_schema.proposals` defines `intent jsonb NOT NULL` but the audit didn't sample `intent` content. Worth a follow-up to make sure `intent` is parseable JSON for every row.

---

## Per-table health matrix

Legend: ✅ healthy · ⚠️ degraded · ❌ broken · ➖ unused/empty by design

| Schema.Table | Rows | Last ts | Lag | Cadence | Health | Notes |
|---|---|---|---|---|---|---|
| public.trade_journal | 199 | 2026-05-14 23:57 | 1 h | event-driven | ✅ | 1 open trade (id 123, ETH/USD, 10 h); 118 historical regime NULLs (P2-1) |
| public.regime_log | 2 320 | 2026-05-14 23:57 | 1 h | hourly | ✅ | Largest gap last 7 d = 2 h 39 m on 13 May (<2× expected) |
| public.sentiment_log | 687 | 2026-05-15 00:46 | 10 m | ~15 m | ⚠️ | **P1-1: 24 h all-zero** |
| public.news_headlines | 5 023 | 2026-05-15 00:45 | 12 m | event | ✅ | 3 845 rows ingested today — healthy volume |
| public.fear_greed_log | 7 | 2026-05-15 00:00 | 1 h | daily | ✅ | One row/day since 09 May |
| public.classifier_log | 1 272 | 2026-05-15 00:53 | 4 m | per-symbol | ✅ | 144 rows already today |
| public.meta_signal_log | 1 296 | 2026-05-15 00:53 | 4 m | per-symbol | ✅ | tracks classifier 1:1 |
| public.macro_features | 1 029 | 2026-05-14 18:57 | 6 h | ~5 m | ⚠️ | **P2-2: stale** |
| public.derivatives_features | 8 232 | 2026-05-14 18:57 | 6 h | ~5 m | ⚠️ | **P2-2: stale, single source `okx`** |
| public.exchange_netflow | 0 | — | — | — | ❌ / ➖ | **P1-2: empty** |
| public.mvrv_ratio | 0 | — | — | — | ❌ / ➖ | **P1-2: empty** |
| public.whale_transactions | 0 | — | — | — | ❌ / ➖ | **P1-2: empty** |
| public.regime_model_meta | n/a | n/a | n/a | per-fit | ➖ | Audit didn't query — small metadata table |
| public.classifier_config | n/a | n/a | n/a | per-edit | ➖ | Tiny config table |
| quanta_schema.proposals | 354 | 2026-05-15 00:38 | 19 m | event | ✅ | Healthy |
| quanta_schema.fills | 354 | 2026-05-15 00:43 | 14 m | event | ✅ | 1:1 with proposals |
| quanta_schema.orders | 354 | 2026-05-15 00:43 | 14 m | event | ✅ | 1:1 with proposals |
| quanta_schema.decisions | 9 936 | 2026-05-15 00:53 | 4 m | per-tick | ✅ | 288 rows today already |
| quanta_schema.equity_snapshots | 0 | — | — | daily | ❌ | **P1-3: empty** |
| quanta_schema.reservations | 0 | — | — | event | ➖ | **P3-1: empty by design?** |
| quanta_schema.run_state | 1 | 2026-05-13 14:56 | n/a | singleton | ✅ | `paused=f`, set_by=dashboard |

---

## Cross-table joins

All checks pass:

```sql
-- closed trades (24 h) → fills
SELECT COUNT(*) FROM trade_journal tj
WHERE tj.closed_at > NOW() - INTERVAL '24 hours'
  AND tj.external_id IS NOT NULL
  AND NOT EXISTS (SELECT 1 FROM quanta_schema.fills f
                  WHERE f.client_order_id = tj.external_id);
-- 0  ✅  (out of 109 with extid)

-- fills with no proposal
SELECT COUNT(*) FROM quanta_schema.fills f
WHERE NOT EXISTS (SELECT 1 FROM quanta_schema.proposals p
                  WHERE p.client_order_id = f.client_order_id);
-- 0  ✅

-- orders with no proposal
-- 0  ✅

-- proposals with no fill (last 24h)
-- 0  ✅
```

The V4 ledger is internally consistent and joins cleanly to `trade_journal.external_id`.

---

## Schema drift vs `user_data/data/schema.sql`

Compared every column declared in `schema.sql` against `information_schema.columns`. **No drift detected for the tables in scope.** All declared columns exist with correct types. The `ALTER TABLE sentiment_log ADD COLUMN IF NOT EXISTS …` block (lines 192-199) has been applied — `fear_greed_value`, `fear_greed_classification`, `community_score_avg`, `reddit_attention_avg`, `trending_pairs`, `sources_ok`, `sources_failed` are all present.

**Tables present in DB but NOT in `schema.sql`** — out-of-tree DDL:
* `public.classifier_log`, `public.meta_signal_log`, `public.classifier_config` (all populated, healthy) — likely created by a separate `classifier/meta_signal` module migration.
* All `quanta_schema.*` (created by V4 migration runner; tracked in `quanta_schema.quanta_schema_version` — currently at v3 "singleton run_state").

**Hypertables (TimescaleDB):** 11 — `classifier_log`, `derivatives_features`, `exchange_netflow`, `fear_greed_log`, `macro_features`, `meta_signal_log`, `mvrv_ratio`, `news_headlines`, `regime_log`, `sentiment_log`, `whale_transactions`. `trade_journal` is intentionally **not** a hypertable (ok per design — it's a denormalised event log). `quanta_schema.fills/decisions/equity_snapshots` were declared "optional hypertables" in schema-v2 but are **not** showing up under `timescaledb_information.hypertables` — they are plain tables. Worth verifying intent.

---

## Daily volume — last 14 days (key tables)

```
src   day        rows
qdec  2026-05-13 2 712     V4 decisions
qdec  2026-05-14 6 960
qdec  2026-05-15   288     (partial, day just rolled)

qprop 2026-05-13   134
qprop 2026-05-14   208
qprop 2026-05-15    12

qfill 2026-05-13   134     1:1 with proposals
qfill 2026-05-14   208
qfill 2026-05-15    12

tj    2026-05-10     1
tj    2026-05-11     3
tj    2026-05-12    15
tj    2026-05-13    67
tj    2026-05-14   113

rl    2026-05-08-14 ≈ 24/day   ✅ hourly cadence holding
sl    2026-05-13-14 ≈ 80/day   ⚠️ but content zeroed (P1-1)
nh    2026-05-14   3 845       ✅ heavy ingest
fg    one row per day          ✅
cl    2026-05-14   1 140
msl   2026-05-14   1 164
```

V4 trade volume is climbing (15 → 67 → 113); proposals and fills track 1:1; decisions volume is healthy. No data-pipeline droughts in the last 7 days for any actively-used table.

---

## Immediate operator actions (in priority order)

1. **P1-1 sentiment outage** — check Ollama (`hermes3:8b`) health on the Spark and the `sentiment_engine` cron logs from 2026-05-14 03:30 UTC. The LLM call is not raising, just returning zero — look for silent JSON parse failure.
2. **P1-3 equity snapshots** — confirm whether the daily cron is wired; if not, `INSERT INTO quanta_schema.equity_snapshots` once before midnight UTC.
3. **P1-2 empty on-chain tables** — `grep -r exchange_netflow modules/ user_data/strategies/`. If unused, drop them or move to a `deprecated/` schema. If used, restore feeders.
4. **P2-2 derivatives 6 h stale** — check the derivatives poller cron for a 19:00 UTC exception.
5. **Open trade #123 (ETH/USD, 10 h)** — verify intentional; otherwise this is an unattended position.

---

*End of report. ~310 lines.*
