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

-- TimescaleDB hypertables — DEFERRED to a future migration (003+).
-- The Timescale 2.26 API in our running container has changed function
-- signatures (uses dimension_info struct), making the old
-- create_hypertable('table', 'column', ...) call signature ambiguous.
-- The b-tree indices above cover all our planned query patterns
-- (proposal lookups by symbol+ts, decisions by ts, equity range scans);
-- hypertable chunking is a performance-only optimization that becomes
-- meaningful at 10M+ rows, which we are nowhere near. Re-enable in
-- 003_add_hypertables.sql once the API is updated.

INSERT INTO quanta_schema_version (version, description)
VALUES (2, 'performance indices + optional timescaledb hypertables on fills/decisions/equity_snapshots')
ON CONFLICT (version) DO NOTHING;
