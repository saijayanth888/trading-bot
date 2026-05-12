-- 002_add_indices.sql — performance indices and (optional) TimescaleDB hypertables.
--
-- The CREATE INDEX statements run on stock Postgres. The TimescaleDB
-- ``create_hypertable`` calls are wrapped in a DO block that skips silently
-- if the extension is not installed — important for the pytest test runner
-- which uses a plain Postgres container.

-- Common lookups on proposals/orders by symbol + recency.
CREATE INDEX IF NOT EXISTS idx_proposals_symbol_created
    ON proposals (symbol, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_proposals_strategy_created
    ON proposals (strategy, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_orders_status_last_update
    ON orders (status, last_update DESC);

-- Fills are queried for trades-of-the-week and per-trade roll-ups.
CREATE INDEX IF NOT EXISTS idx_fills_client_order_id
    ON fills (client_order_id);

CREATE INDEX IF NOT EXISTS idx_fills_ts
    ON fills (ts DESC);

-- Decisions are queried for the nightly reflector + weekly publisher.
CREATE INDEX IF NOT EXISTS idx_decisions_ts
    ON decisions (ts DESC);

CREATE INDEX IF NOT EXISTS idx_decisions_symbol_ts
    ON decisions (symbol, ts DESC);

-- Equity curve queries are range-scans over ts.
CREATE INDEX IF NOT EXISTS idx_equity_snapshots_ts
    ON equity_snapshots (ts DESC);

-- TimescaleDB hypertables (optional — silently skipped without the extension).
-- Wrapped in DO/EXCEPTION so this migration is idempotent and CI-friendly.
DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM pg_extension WHERE extname = 'timescaledb'
    ) THEN
        PERFORM create_hypertable(
            'fills', 'ts',
            if_not_exists => TRUE,
            migrate_data => TRUE
        );
        PERFORM create_hypertable(
            'decisions', 'ts',
            if_not_exists => TRUE,
            migrate_data => TRUE
        );
        PERFORM create_hypertable(
            'equity_snapshots', 'ts',
            if_not_exists => TRUE,
            migrate_data => TRUE
        );
    END IF;
END;
$$;

INSERT INTO quanta_schema_version (version, description)
VALUES (2, 'performance indices + optional timescaledb hypertables on fills/decisions/equity_snapshots')
ON CONFLICT (version) DO NOTHING;
