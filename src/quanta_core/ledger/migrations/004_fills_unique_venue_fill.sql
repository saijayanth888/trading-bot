-- 004_fills_unique_venue_fill.sql — DB-level dedup for exchange-side fills.
--
-- Audit 2026-05-16 (DB): the original 001 schema declared
-- fills.venue_fill_id as a nullable TEXT with no uniqueness constraint.
-- Exchanges occasionally re-deliver fill events (websocket reconnects,
-- duplicate webhooks, retry-after-timeout). The application layer asserts
-- at query time, but a torn write or a race between two consumers would
-- silently double-insert a paper fill, double-counting PnL.
--
-- Fix: partial UNIQUE index on (client_order_id, venue_fill_id) WHERE
-- venue_fill_id IS NOT NULL. The partial predicate is essential — the
-- shadow paper-fill path used to write venue_fill_id=NULL until 2026-05-13
-- and historical rows must remain valid.
--
-- A composite over (client_order_id, venue_fill_id) is the right key:
-- the same venue may emit the same fill id under different orders (e.g.,
-- internal retry across separate client_order_ids), so neither column
-- alone is unique. The pair is.

CREATE UNIQUE INDEX IF NOT EXISTS ux_fills_coid_venue
    ON fills (client_order_id, venue_fill_id)
    WHERE venue_fill_id IS NOT NULL;

INSERT INTO quanta_schema_version (version, description)
VALUES (4, 'unique (client_order_id, venue_fill_id) partial index — DB-level fill dedup')
ON CONFLICT (version) DO NOTHING;
