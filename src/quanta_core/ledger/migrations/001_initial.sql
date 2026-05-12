-- 001_initial.sql — initial quanta_core ledger schema
--
-- The ledger is the single source of truth for trade proposals, exchange
-- acknowledgements, fills, decisions, equity snapshots and idempotency
-- reservations. Every write is parameterised; no string concatenation is
-- performed in application code (psycopg 3 binds at the protocol level).
--
-- TimescaleDB hypertables are added in a separate migration (002) so this
-- migration applies cleanly on a stock Postgres server too — useful for the
-- test container which does NOT have the timescaledb extension installed.
--
-- All ``ts`` columns are stored as ``TIMESTAMPTZ`` — Quanta Core operates in
-- UTC end-to-end and the application layer refuses naive datetimes.
--
-- Idempotency rules:
--   * ``proposals.client_order_id`` is the canonical idempotency key.
--   * ``reservations.client_order_id`` carries the UNIQUE constraint that
--     ``PostgresLedger.reserve()`` relies on; a second ``reserve()`` with
--     the same id raises ``ReservationConflictError``.
--   * ``orders.client_order_id`` is a FK back into ``proposals`` so an ack
--     cannot exist without a proposal.
--   * ``fills.client_order_id`` is a FK back into ``proposals`` for the same
--     reason. (A venue may emit a fill before the ack on rare paths, so we
--     deliberately do NOT FK into ``orders``.)

CREATE TABLE IF NOT EXISTS quanta_schema_version (
    version     INTEGER PRIMARY KEY,
    applied_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    description TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS reservations (
    client_order_id TEXT PRIMARY KEY,
    intent          JSONB NOT NULL,
    reserved_at     TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS proposals (
    client_order_id TEXT PRIMARY KEY,
    venue           TEXT NOT NULL,
    symbol          TEXT NOT NULL,
    side            TEXT NOT NULL CHECK (side IN ('BUY', 'SELL')),
    qty             NUMERIC(38, 18) NOT NULL CHECK (qty > 0),
    limit_price     NUMERIC(38, 18),
    strategy        TEXT NOT NULL,
    intent          JSONB NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS orders (
    client_order_id   TEXT PRIMARY KEY REFERENCES proposals(client_order_id) ON DELETE RESTRICT,
    exchange_order_id TEXT,
    status            TEXT NOT NULL CHECK (
        status IN ('PROPOSED', 'ACKED', 'PARTIAL', 'FILLED', 'CANCELLED', 'REJECTED')
    ),
    cancel_reason     TEXT,
    last_update       TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS fills (
    id              BIGSERIAL,
    client_order_id TEXT NOT NULL REFERENCES proposals(client_order_id) ON DELETE RESTRICT,
    venue_fill_id   TEXT,
    qty             NUMERIC(38, 18) NOT NULL CHECK (qty > 0),
    price           NUMERIC(38, 18) NOT NULL CHECK (price > 0),
    fee             NUMERIC(38, 18) NOT NULL DEFAULT 0,
    fee_currency    TEXT,
    side            TEXT NOT NULL CHECK (side IN ('BUY', 'SELL')),
    ts              TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (id, ts)
);

CREATE TABLE IF NOT EXISTS decisions (
    id        BIGSERIAL,
    ts        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    symbol    TEXT,
    strategy  TEXT,
    debate    JSONB NOT NULL,
    outcome   TEXT NOT NULL,
    rationale TEXT,
    PRIMARY KEY (id, ts)
);

CREATE TABLE IF NOT EXISTS equity_snapshots (
    ts           TIMESTAMPTZ NOT NULL,
    equity       NUMERIC(38, 18) NOT NULL,
    unrealized   NUMERIC(38, 18) NOT NULL DEFAULT 0,
    drawdown_pct NUMERIC(10, 6) NOT NULL DEFAULT 0,
    cash         NUMERIC(38, 18),
    PRIMARY KEY (ts)
);

INSERT INTO quanta_schema_version (version, description)
VALUES (1, 'initial ledger schema (reservations, proposals, orders, fills, decisions, equity_snapshots)')
ON CONFLICT (version) DO NOTHING;
