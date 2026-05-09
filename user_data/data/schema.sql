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
-- Derivatives features (per-pair, free public APIs only).
-- Replaces dead CryptoQuant netflow + Whale Alert with derivatives-side
-- positioning: funding rate, open interest, taker buy/sell volume,
-- long/short account ratio. All sources are no-key, US-accessible, regulated
-- exchanges or DEX indexers (OKX, dYdX, Coinbase International, Kraken Futures).
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS derivatives_features (
    pair                TEXT             NOT NULL,    -- "BTC/USD" form
    ts                  TIMESTAMPTZ      NOT NULL,
    funding_rate        DOUBLE PRECISION,             -- raw, e.g. 0.0001
    next_funding_rate   DOUBLE PRECISION,             -- predicted next
    open_interest_usd   DOUBLE PRECISION,             -- USD notional
    long_short_ratio    DOUBLE PRECISION,             -- accounts; >1 = more longs
    taker_buy_vol_usd   DOUBLE PRECISION,             -- 5m bucket
    taker_sell_vol_usd  DOUBLE PRECISION,             -- 5m bucket
    source              TEXT             NOT NULL,    -- 'okx' | 'dydx' | ...
    PRIMARY KEY (pair, source, ts)
);
SELECT create_hypertable('derivatives_features', 'ts', if_not_exists => TRUE);
CREATE INDEX IF NOT EXISTS ix_deriv_pair_ts ON derivatives_features(pair, ts DESC);

-- ---------------------------------------------------------------------------
-- Macro features (global, single row per poll, all pairs share).
-- Sources: DefiLlama (stablecoin mcap), alternative.me (fear/greed),
-- CoinGecko (BTC dominance), bitcoin-data.com (BTC MVRV), mempool.space.
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS macro_features (
    ts                          TIMESTAMPTZ PRIMARY KEY,
    stablecoin_mcap_usd         DOUBLE PRECISION,    -- total USDT+USDC+...
    stablecoin_mcap_chg_24h     DOUBLE PRECISION,    -- delta vs 24h ago, USD
    fear_greed_index            DOUBLE PRECISION,    -- 0..100
    btc_dominance_pct           DOUBLE PRECISION,    -- 0..100
    btc_mvrv                    DOUBLE PRECISION,    -- BTC only; ~1.0 neutral
    btc_mempool_fastest_fee     DOUBLE PRECISION     -- sat/vB
);
SELECT create_hypertable('macro_features', 'ts', if_not_exists => TRUE);

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

-- ── Multi-source news aggregator (Task: expand sentiment sources) ───────
CREATE TABLE IF NOT EXISTS news_headlines (
    ts                   TIMESTAMPTZ NOT NULL,
    source               TEXT NOT NULL,
    title                TEXT NOT NULL,
    summary              TEXT,
    url                  TEXT,
    pair_mentions        JSONB,
    community_sentiment  DOUBLE PRECISION,
    attention_score      DOUBLE PRECISION,
    PRIMARY KEY (ts, source, title)
);
SELECT create_hypertable('news_headlines', 'ts', if_not_exists => TRUE);
CREATE INDEX IF NOT EXISTS ix_news_headlines_source ON news_headlines(source, ts DESC);
CREATE INDEX IF NOT EXISTS ix_news_headlines_pair_mentions ON news_headlines USING GIN (pair_mentions jsonb_path_ops);

CREATE TABLE IF NOT EXISTS fear_greed_log (
    ts             TIMESTAMPTZ PRIMARY KEY,
    value          INTEGER NOT NULL,
    classification TEXT NOT NULL,
    history_7d     JSONB
);
SELECT create_hypertable('fear_greed_log', 'ts', if_not_exists => TRUE);

-- Adjacent columns the sentiment_engine adds to sentiment_log so the
-- multi-source signals are persistent alongside the LLM scores.
ALTER TABLE sentiment_log
    ADD COLUMN IF NOT EXISTS fear_greed_value          INTEGER,
    ADD COLUMN IF NOT EXISTS fear_greed_classification TEXT,
    ADD COLUMN IF NOT EXISTS community_score_avg       DOUBLE PRECISION,
    ADD COLUMN IF NOT EXISTS reddit_attention_avg      DOUBLE PRECISION,
    ADD COLUMN IF NOT EXISTS trending_pairs            JSONB,
    ADD COLUMN IF NOT EXISTS sources_ok                JSONB,
    ADD COLUMN IF NOT EXISTS sources_failed            JSONB;
