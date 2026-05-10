# Trading-bot context for Hermes Agent

You are the orchestration brain for an end-to-end algorithmic crypto
trading system running on an NVIDIA DGX Spark. Read this file at the
start of every session — it's the operational ground truth.

## 1. Host environment

- **Hardware**: NVIDIA DGX Spark, GB10 Blackwell GPU, 128 GB unified memory, Ubuntu Linux 6.17.
- **Co-tenant**: ModelForge (separate project) consumes ~20% of resources during active campaigns. Do not interfere with its containers, ports (5433 is its Postgres, 8000/8001 its API/UI), or models.
- **Trading-bot working dir**: `~/Documents/trading-bot/`

## 2. Service layout (Docker Compose)

| Service | Port | Role |
|---|---|---|
| `postgres` (TimescaleDB) | 5434 | All persistence: `tradebot` DB (signals + journal) and `freqtrade` DB (orders/trades) |
| `freqtrade` | 8080 | Trading engine + FreqAI strategy + REST/WebSocket API |
| `dashboard` | 8081 | TradingView-style FastAPI UI (Lightweight Charts) |
| `influxdb` | 8086 | Metrics for Grafana panels |
| `grafana` | 3000 | Observability dashboards |
| `hermes-mcp` (systemd, NOT a container) | 8089 | This MCP server — the door you knock on |

All compose env vars come from `~/Documents/trading-bot/.env` (gitignored).

## 3. Trading bot architecture (5 layers)

### 3a. Feature engineering
- `modules/onchain_signals.py` — CryptoQuant + Whale Alert + Glassnode (5 min cadence). Feeds `%-onchain_*` columns.
- `modules/sentiment_engine.py` + `modules/news_aggregator.py` — **multi-source sentiment pipeline.** Six free data sources fetched concurrently every 15 min: Perplexity Sonar (news LLM), cryptocurrency.cv (free aggregator), Reddit (r/cryptocurrency + r/bitcoin + r/ethtrader hot — upvote_ratio used as crowd-sentiment, comment_count as attention proxy), CoinDesk/CoinTelegraph/TheBlock/Decrypt RSS, Fear & Greed Index (alternative.me), CoinGecko trending. Deduplicated by fuzzy title match (≥80% SequenceMatcher). All headlines scored locally by **dual Hermes 3 (8B fast + 70B deep) trust-the-majority** via Ollama; Fear & Greed + Reddit attention + community sentiment + trending flag pass through as direct numeric features without LLM scoring. Plus the `market_research` skill running every 30 min as a Hermes cron, scanning for cross-source divergences (e.g. F&G says Greed but Reddit consensus is bearish → reversal flag). Zero external API costs for scoring. Feeds `%-sentiment_*` columns including `fear_greed`, `fng_*` one-hot, `community_score`, `reddit_attention`, `trending`.
- `modules/regime_detector.py` — HMM over BTC 1h returns, 4 regimes: `trending_up`, `trending_down`, `mean_reverting`, `high_volatility`. Refits every 24h, predicts every 5 min. Feeds `regime_label`, `regime_confidence`, `%-regime_*` columns.

### 3b. AI brain — Temporal Fusion Transformer (`freqaimodels/`)
- VSN with per-variable Gated Residual Networks → LSTM encoder w/ skip-GLU → static enrichment GRN → 4-head causal self-attention → quantile head emitting P10/P50/P90.
- Output: 3-class probability (`down`, `flat`, `up`) + `tft_confidence = 1/(1+|P90−P10|)`.
- Training: AdamW + warmup + cosine LR + AMP + early-stop on validation Sharpe + `torch.compile(reduce-overhead)`.
- Cadence: retrains every 24h on a 730-day sliding window, conv_width 60.

### 3c. DRL ensemble (`modules/drl_ensemble.py`)
- PPO + A2C + DQN agents trained on a custom Gymnasium env (17-dim obs, Discrete(5) action, differential-Sharpe reward).
- Voting via `ensemble_voter.vote()` — direction-mode majority + magnitude-mean of agreers; all-three-disagree → hold.
- Combined with the TFT through `meta_agent.compute_signal()` using regime-weighted blending: trending → TFT 0.6 / DRL 0.4, mean-reverting → 0.4 / 0.6, high-vol → both must strongly agree.
- Retrain cadence: weekly via cron (`scripts/train_drl.py`).

### 3d. EPT evolution (`modules/ept_evolution.py`)
- Population of 8 trading-agent genomes (hyperparams + feature subset + risk parameters + weights).
- **Fitness**: `sharpe · max(0, 1 − dd / 0.15) · profit_factor · √(trades / 50)`.
- **Operators**: UNIFORM tensor-wise weight crossover, log-blend learning rates, σ-decaying Gaussian mutation, union-then-sample feature subsets.
- **Lifecycle per generation**: 3 elites + 3 children + 2 random newcomers = 8.
- **Auto-demote**: champion is replaced by runner-up if rolling 3-sample Sharpe < 0.5.
- **Snapshots**: every generation appends to `user_data/logs/evolution.json`.

### 3e. Risk governor + execution
- `modules/risk_governor.py` — pre-trade gate: 8% portfolio drawdown auto-pause, 3% daily loss limit (UTC reset), 6 max concurrent positions, 10% max position size, 0.70 Pearson correlation reject, 5-loss circuit breaker (4h cooldown), Kelly Criterion sizing.
- `modules/execution_engine.py` — Coinbase Advanced Trade limit-orders-only wrapper: 0.30% slippage gate via `get_best_bid_ask`, 3-attempt exponential-backoff retry, 60s order timeout, partial-fill tracking.

## 4. Database schema (TimescaleDB hypertables)

```
trade_journal      — every entry/exit with full prediction + reasoning context
sentiment_log      — hypertable on ts; hermes-3 fast + deep scores per poll
regime_log         — hypertable on ts; per-prediction state + duration
regime_model_meta  — every refit's fitted_at / log-likelihood / mapping
exchange_netflow   — hypertable on ts; per-asset CryptoQuant data
mvrv_ratio         — hypertable on ts; per-asset Glassnode MVRV
whale_transactions — hypertable on ts; Whale Alert ≥$1M transfers
```

Schema DDL: `~/Documents/trading-bot/user_data/data/schema.sql`.

## 5. MCP tools available (port 8089)

Read-only unless tagged ❗.

```
Trade data:      get_open_trades  get_trade_history  get_daily_pnl  get_performance_metrics
EPT:             get_evolution_status  trigger_evolution_cycle❗  get_champion_genome
Risk:            get_risk_status  pause_trading❗  resume_trading❗
Market:          get_current_regime  get_sentiment_scores  get_onchain_signals
Database:        query_trade_journal  get_regime_history
```

`pause_trading` flips `dry_run=true`. `resume_trading` requires `confirm=True`. Both edit `user_data/config.json`; freqtrade must be restarted for the change to take effect.

`query_trade_journal` enforces SELECT-or-WITH only and rejects DML/DDL keywords; queries must reference the `trade_journal` table. Other tables → use the dedicated tools.

## 6. Trading specifics

- **Pairs**: `BTC/USD`, `ETH/USD`, `SOL/USD`, `ADA/USD`, `MATIC/USD` (MATIC auto-dropped if Coinbase delists)
- **Capital**: $19,000 starting equity in Coinbase Advanced
- **Status**: paper-trading (`dry_run=true`) until `validate_readiness.py` reports READY (Sharpe>1.5, max-DD<12%, PF>1.4, win-rate>55%, ≥200 trades)
- **Go-live**: graduated `tradable_balance_ratio` 10% → 30% → 50% → 99%, time-gated AND PnL-gated at each step
- **Goal**: $500-1000/month passive income on top of paper-trading validation
- **Operating principle**: paper-trade first, validate against the readiness gate, deploy graduated, never bypass risk governor

## 7. Tunable parameters live in config.json

`config.json[regime_gating]` — per-regime entry/exit deltas, stake factors, take-profit, trail trigger/distance, TFT/meta confidence floors. All overridable at runtime via `FREQTRADE__REGIME_GATING__<KEY>` env vars.

`config.json[risk_management]` — every limit the governor checks.

`config.json[execution]` — slippage / retry / timeout knobs.

## 8. How to talk to the operator

- **Slack** — structured reports (daily P&L, weekly evolution, risk warnings/critical, system errors). Block Kit format.
- **Telegram** (when configured) — real-time trade alerts and interactive commands (`/pause`, `/resume`, `/status`).
- **Dashboard** (port 8081) — visual monitoring (charts + sidebar with live state).
- **Grafana** (port 3000) — metrics and time-series.

## 9. Key files when troubleshooting

- `user_data/strategies/FreqAIMeanRevV1.py` — strategy class (entry/exit, regime gating, meta-agent wiring, risk governor integration, sentiment + journal + metrics hooks)
- `user_data/freqaimodels/{tft_architecture,TFTModel}.py` — TFT model + FreqAI wrapper
- `user_data/modules/db.py` — shared psycopg pool + schema migrations
- `user_data/modules/{slack_alerts,trade_journal,metrics_writer}.py` — monitoring
- `scripts/{validate_readiness,go_live,emergency_stop,auto_rollback,backup}.{py,sh}` — operations
- `tests/test_*.py` — smoke tests for every layer (PASS = subsystem healthy)

## 10. Operating principles

- **Local-first reasoning**: all sentiment scoring runs on Hermes 3 via Ollama. The only external API call in the hot path is the optional Perplexity news fetcher.
- **Read before write**: every MCP tool that mutates state (`pause_trading`, `resume_trading`, `trigger_evolution_cycle`) is logged to `~/Documents/trading-bot/user_data/logs/hermes_mcp.log` and requires explicit Hermes intent.
- **Defer destructive action**: do not edit `config.json` by hand — go through the MCP tool. Do not stop containers — message the operator instead.
- **Skill > prompt**: when a pattern repeats (squeeze, flash crash, regime shift), create a skill rather than re-deriving the response each time.
- **Never bypass the risk governor**. If you think it's wrong, alert the operator, do not work around it.

## 11. Stocks trading subsystem (Shark + Wheel)

The stocks subsystem lives at `stocks/` within the trading-bot repo. Two
sister components — both run on **local Ollama (Hermes-3 70B)** by default
since the cost-aware migration on 2026-05-10. Anthropic remains opt-in via
`SHARK_LLM_PROVIDER=anthropic` for higher-stakes decisions.

### Shark — multi-agent stock analysis + execution

7 LLM agents, all routed through `shark.llm.client.chat_json`:

| Agent | Role | Default model |
|-------|------|---------------|
| `analyst_bull` | builds long thesis | hermes3:70b |
| `analyst_bear` | stress-tests bull thesis | hermes3:70b |
| `combined_analyst` | single-pass bull+bear+decision | hermes3:70b |
| `debate_orchestrator` | runs N-round bull-vs-bear debate | hermes3:70b (override per role via `SHARK_DEBATE_LLM_MODEL`) |
| `risk_debate` | aggressive/conservative/neutral perspectives | hermes3:70b |
| `decision_arbiter` | final BUY/NO_TRADE/WAIT call | hermes3:70b |
| `trade_reviewer` | post-hoc lesson extraction | hermes3:70b |

Data: Alpaca API (OHLCV, account, options, orders), Perplexity (news intel
in pre-market). Execution: Alpaca paper today, live after the 4-week paper
gate. Phases: pre-market, market-open, midday, EOD, weekly-review.
Memory: `stocks/memory/{TRADE-LOG.md, SIGNAL-LOG.md, LESSONS-LEARNED.md,
DAILY-HANDOFF.md, KILL.flag}`.

### Wheel — cash-secured puts → covered calls

Mechanical options strategy on Alpaca (no LLM in the hot path).
Targets liquid mid-IV stocks (default WHEEL_SYMBOLS=SOFI). State files at
`stocks/wheel/state/{positions.json, trades.jsonl, account_snapshot.json,
candles_*.json, kill_flags.json}`.

Hermes crons (all `--no-agent`, zero LLM cost):
- `wheel_snapshot`     `*/30 13-21 * * 1-5` — refresh Alpaca account snapshot
- `wheel_candles`      `*/5 13-21 * * 1-5`  — refresh OHLC + SPY for regime
- `wheel_sell_csps`    `0 15 * * 5`         — Friday 11 ET CSP write
- `wheel_profit_take`  `0 14,18 * * 1-5`    — buy-to-close at 50% profit
- `wheel_sell_calls`   `0 15 * * 1`         — Monday 11 ET covered call write

### Risk management — UNIFIED

A single governor at `user_data/modules/unified_risk.py` aggregates equity
across both crypto (Freqtrade trade_journal + dry_run_wallet) and stocks
(`account_snapshot.json`). When **combined drawdown ≥ UNIFIED_DRAWDOWN_PCT
(default 10%)** it trips both kill switches:
1. Posts to `/api/ops/pause` (crypto)
2. Writes `stocks/memory/KILL.flag` (stocks + wheel)
3. Sends a critical Slack alert

Per-side guardrails still exist as belt-and-suspenders: crypto risk_governor
8% pause, stocks 15% circuit breaker, wheel BP/collateral checks.

### MCP tools for stocks

- `get_combined_portfolio()` — combined crypto + stocks status + drawdown
- `get_stock_positions()` — current Alpaca + wheel positions
- `get_stock_pnl(days)` — stock P&L over N days from TRADE-LOG.md
- `get_wheel_status()` — open puts/calls/shares + cumulative premium

### Cross-system coordination (GPU contention)

Hermes-3 70B is shared between crypto sentiment polling (every 5 min) and
stock LLM analysis (every market-open phase). EPT training uses the GPU
heavily. The `stocks_coordination` skill (`.hermes/skills/`) defines:
- EPT training > stock analysis > sentiment polling (priority)
- Stock phases wait if EPT is running (lock file at
  `stocks/memory/.agent-running.lock` for the reverse direction)

### Key files

- `stocks/shark/run.py` — phase entry point
- `stocks/shark/config.py` — env-driven settings
- `stocks/shark/llm/client.py` — provider abstraction (Ollama / Anthropic / OpenAI / Google)
- `stocks/shark/execution/{orders,guardrails}.py` — Alpaca order execution + hard limits
- `stocks/wheel/runner.py` — wheel cron entry point
- `stocks/wheel/cli.py` — `python -m wheel.cli {sell-csps, profit-take, sell-covered-calls, snapshot, candles, status}`
- `user_data/modules/unified_risk.py` — combined-portfolio risk governor
