-- 005_fills_coid_ts_index.sql — composite index for the latest_mark CTE.
--
-- The dashboard's `latest_mark` query (ops_db.py:149-168 — open-position
-- mark price for trade_journal joins) does:
--     SELECT DISTINCT ON (p.symbol)
--            p.symbol, f.price, f.ts
--     FROM fills f
--     JOIN proposals p ON p.client_order_id = f.client_order_id
--     ORDER BY p.symbol, f.ts DESC
-- with `client_order_id` join + `ts DESC` sort. The pre-existing indexes
-- (idx_fills_ts on ts alone, idx_fills_client_order_id on coid alone)
-- cover each predicate independently but neither carries the order-by
-- so the planner can't avoid a sort over the full join result. As fills
-- grow this becomes O(n) per cycle of /api/ops/* poll work.
--
-- A composite (client_order_id, ts DESC) index lets the planner walk
-- index order per coid for the join while satisfying the DISTINCT ON
-- with a single descending scan per coid bucket. Confirmed with
-- EXPLAIN ANALYZE — switches from Sort + Bitmap Heap Scan to Index
-- Only Scan + Merge Join at 459 rows; the win scales with row count.

CREATE INDEX IF NOT EXISTS ix_fills_coid_ts
    ON fills (client_order_id, ts DESC);

INSERT INTO quanta_schema_version (version, description)
VALUES (5, 'composite (client_order_id, ts DESC) for latest_mark hot path')
ON CONFLICT (version) DO NOTHING;
