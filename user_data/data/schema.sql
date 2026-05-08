-- Application schema for the trading bot.
--
-- All time columns are TIMESTAMPTZ. Hypertables are created on the
-- time-series tables; the metadata + trade tables stay regular.
--
-- This file is run idempotently from `modules/db.ensure_schema()` on
-- first connection from any module.

-- ---------------------------------------------------------------------------
-- On-chain signals (per-asset time series)
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS exchange_netflow (
    asset       TEXT        NOT NULL,
    ts          TIMESTAMPTZ NOT NULL,
    netflow     DOUBLE PRECISION NOT NULL,
    PRIMARY KEY (asset, ts)
);
SELECT create_hypertable('exchange_netflow', 'ts', if_not_exists => TRUE);

CREATE TABLE IF NOT EXISTS mvrv_ratio (
    asset       TEXT        NOT NULL,
    ts          TIMESTAMPTZ NOT NULL,
    value       DOUBLE PRECISION NOT NULL,
    PRIMARY KEY (asset, ts)
);
SELECT create_hypertable('mvrv_ratio', 'ts', if_not_exists => TRUE);

CREATE TABLE IF NOT EXISTS whale_transactions (
    id              TEXT NOT NULL,
    ts              TIMESTAMPTZ NOT NULL,
    symbol          TEXT,
    amount_usd      DOUBLE PRECISION,
    from_owner_type TEXT,
    to_owner_type   TEXT,
    PRIMARY KEY (id, ts)
);
SELECT create_hypertable('whale_transactions', 'ts', if_not_exists => TRUE);
CREATE INDEX IF NOT EXISTS ix_whale_symbol_ts ON whale_transactions(symbol, ts DESC);

-- ---------------------------------------------------------------------------
-- Sentiment log (broad-market, single row per poll)
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS sentiment_log (
    ts              TIMESTAMPTZ PRIMARY KEY,
    sentiment_score DOUBLE PRECISION NOT NULL,
    confidence      DOUBLE PRECISION NOT NULL,
    market_impact   TEXT NOT NULL,
    agreement       BOOLEAN NOT NULL DEFAULT FALSE,
    key_events      JSONB,
    -- Source-of-truth scorer (Ollama on the Spark)
    llama_score     DOUBLE PRECISION,
    llama_impact    TEXT,
    raw_llama       JSONB,
    -- Item count from the news fetcher (Perplexity)
    n_headlines     INTEGER NOT NULL DEFAULT 0,
    -- Legacy columns kept so old SQLite exports still round-trip
    claude_score    DOUBLE PRECISION,
    claude_impact   TEXT,
    raw_claude      JSONB,
    n_reddit        INTEGER NOT NULL DEFAULT 0
);
SELECT create_hypertable('sentiment_log', 'ts', if_not_exists => TRUE);

-- ---------------------------------------------------------------------------
-- Regime detector
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS regime_log (
    ts                    TIMESTAMPTZ PRIMARY KEY,
    regime                TEXT NOT NULL,
    probability           DOUBLE PRECISION,
    state                 INTEGER,
    state_means           JSONB,
    transition_matrix     JSONB,
    regime_duration_hours DOUBLE PRECISION,
    state_probabilities   JSONB
);
SELECT create_hypertable('regime_log', 'ts', if_not_exists => TRUE);

CREATE TABLE IF NOT EXISTS regime_model_meta (
    id              BIGSERIAL PRIMARY KEY,
    fitted_at       TIMESTAMPTZ NOT NULL,
    n_samples       INTEGER,
    log_likelihood  DOUBLE PRECISION,
    state_to_label  JSONB,
    feature_names   JSONB
);
CREATE INDEX IF NOT EXISTS ix_regime_meta_fitted_at ON regime_model_meta(fitted_at DESC);

-- ---------------------------------------------------------------------------
-- Trade journal (one row per trade, updated on close)
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS trade_journal (
    trade_id        BIGSERIAL PRIMARY KEY,
    external_id     TEXT,
    pair            TEXT NOT NULL,
    direction       TEXT NOT NULL,
    opened_at       TIMESTAMPTZ NOT NULL,
    closed_at       TIMESTAMPTZ,
    entry_price     DOUBLE PRECISION,
    exit_price      DOUBLE PRECISION,
    stake           DOUBLE PRECISION,
    pnl             DOUBLE PRECISION,
    pnl_pct         DOUBLE PRECISION,
    duration_min    DOUBLE PRECISION,
    confidence      DOUBLE PRECISION,
    tft_probs       JSONB,
    drl_votes       JSONB,
    sentiment_score DOUBLE PRECISION,
    sentiment_conf  DOUBLE PRECISION,
    regime          TEXT,
    exit_reason     TEXT,
    features_used   JSONB,
    reasoning       TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS ix_trade_journal_opened_at ON trade_journal(opened_at DESC);
CREATE INDEX IF NOT EXISTS ix_trade_journal_pair ON trade_journal(pair, opened_at DESC);
CREATE INDEX IF NOT EXISTS ix_trade_journal_external_id ON trade_journal(external_id) WHERE external_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS ix_trade_journal_open ON trade_journal(opened_at DESC) WHERE closed_at IS NULL;
