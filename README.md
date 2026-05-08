# Trading bot — TFT + DRL ensemble + EPT evolution

End-to-end algorithmic trading system built on top of
[Freqtrade](https://www.freqtrade.io/) with a custom FreqAI Temporal Fusion
Transformer model, a Deep-RL ensemble (PPO / A2C / DQN) coordinated by a
meta-agent, evolutionary population training of full agent genomes, a
hard-gating risk governor, a Coinbase Advanced Trade execution engine,
Slack + Grafana monitoring, a TradingView-style web dashboard, and
graduated go-live automation.

```
                            ┌─ on-chain ─────┐
                            │  sentiment      │  regime detector (HMM)
                            │  features       │
                            └────────┬───────┘
                                     ▼
                  ┌─────────── FreqAI ──────────────┐
                  │  TFT classifier  →  up/flat/down │
                  │  + quantile head → tft_confidence│
                  └────────┬─────────────────────────┘
                           ▼
              DRL ensemble (PPO + A2C + DQN)
              voting → meta-agent (regime-weighted)
                           ▼
              risk governor → execution engine
                           ▼
        Coinbase Advanced Trade  │  Slack / Grafana / dashboard
```

---

## Repo layout

```
trading-bot/
├── docker-compose.yml          # freqtrade + influxdb + grafana + dashboard
├── start.sh                    # one-shot up
├── requirements-extra.txt      # extra pip deps for the freqtrade container
├── README.md
├── .env.example                # template — copy to .env and fill in keys
├── grafana/                    # provisioned datasource + dashboards
├── scripts/                    # operational scripts (validate / go-live / backup …)
├── tests/                      # pure-python smoke tests for every layer
└── user_data/                  # mounted into the freqtrade container
    ├── config.json             # bot config + risk_management + execution blocks
    ├── strategies/
    │   └── FreqAIMeanRevV1.py  # entry/exit, regime gating, meta-agent wiring
    ├── freqaimodels/           # custom PyTorch FreqAI model
    │   ├── tft_architecture.py # TFT (VSN, GRN, multi-head attention, quantile head)
    │   └── TFTModel.py         # FreqAI BasePyTorchClassifier wrapper
    ├── modules/                # signal modules + AI layers
    │   ├── onchain_signals.py
    │   ├── sentiment_engine.py
    │   ├── sentiment_prompts.py
    │   ├── regime_detector.py
    │   ├── trading_env.py      # gym env for the DRL ensemble
    │   ├── drl_ensemble.py     # PPO + A2C + DQN training / persisting / voting
    │   ├── ensemble_voter.py
    │   ├── meta_agent.py       # regime-weighted TFT + DRL combiner
    │   ├── ept_evolution.py    # evolutionary population training
    │   ├── risk_governor.py    # 7-rule pre-trade gate + Kelly sizing
    │   ├── execution_engine.py # Coinbase limit-only order wrapper
    │   ├── slack_alerts.py
    │   ├── trade_journal.py    # SQLite trade ledger
    │   └── metrics_writer.py   # InfluxDB writer for Grafana
    ├── scripts/
    │   └── train_drl.py        # cron-friendly DRL retrain entry point
    └── dashboard/              # standalone FastAPI charting app (port 8081)
```

---

## Architectural layers

### 1. Feature engineering

Three independent signal sources merge into the candle dataframe via
`feature_engineering_expand_*` hooks in `FreqAIMeanRevV1`:

| Module | Source | Cadence | Columns produced |
|---|---|---|---|
| `onchain_signals.py` | CryptoQuant + Whale Alert + Glassnode | 5 min refresh | `%-onchain_netflow_z`, `%-onchain_mvrv`, `%-onchain_whale_count_1h`, `%-onchain_whale_volume_1h` |
| `sentiment_engine.py` | Anthropic Claude + Ollama (LLM dual-pass) | 15 min | `%-sentiment_score`, `%-sentiment_confidence`, `%-sentiment_bullish/bearish/agreement` |
| `regime_detector.py` | HMM over multi-timeframe returns | 1 h | `regime_label`, `regime_confidence`, `%-regime_is_*` (one-hot), `%-regime_prob_*` |

Each module fails open: if its API is down or unconfigured, neutral
columns are filled in so the bot keeps running on whatever sources are
available.

### 2. Temporal Fusion Transformer

Custom FreqAI model swapping the default LightGBM classifier:

* `freqaimodels/tft_architecture.py` — VSN with per-variable Gated
  Residual Networks; LSTM encoder with skip-connection GLU; static
  enrichment GRN; interpretable 4-head causal self-attention; quantile
  head emitting P10/P50/P90.
* `freqaimodels/TFTModel.py` — `BasePyTorchClassifier`-derived wrapper:
  AdamW + warmup + cosine LR, mixed precision (`torch.amp`), early stop
  on validation Sharpe, optional `torch.compile`, batch 256, sliding-
  window predict that emits a `tft_confidence = 1 / (1 + |P90 − P10|)`
  column alongside the usual class probabilities.
* `tests/test_tft.py` — GPU smoke test (forward + backward + AMP +
  save/reload).

Activated in `config.json` via `"freqaimodel": "TFTModel"`. Two-year
lookback, 24h retrain, 120-candle conv width, 1h horizon (configurable).

### 3. Deep RL ensemble

A trio of agents trained on the same gym env vote on every candle:

* `modules/trading_env.py` — 17-dim observation (TFT 3 + on-chain 5 +
  sentiment 2 + regime 4 + portfolio 3), `Discrete(5)` action
  (strong_buy / buy / hold / sell / strong_sell), reward = differential
  Sharpe (Moody/Saffell, unit-return scaled, 20-step warmup) − 10 bps
  cost − drawdown²; 1000-step episodes.
* `modules/drl_ensemble.py` — PPO + A2C + DQN trained independently
  with separate seeds; persisted as zips in `user_data/models/drl/`
  with a `meta.json` for the weekly retrain gate.
* `modules/ensemble_voter.py` — direction-mode majority + magnitude-
  mean of agreers; all-three-disagree falls back to hold.
* `modules/meta_agent.py` — combines TFT class probs with the DRL vote
  weighted by regime: trending → TFT 0.6 / DRL 0.4, mean-reverting →
  0.4 / 0.6, high-vol → trade only when both agree on the same
  non-flat direction with halved size.

`tests/test_drl.py` exercises everything end-to-end with mock
training/eval.

### 4. EPT evolution

Population of 8 trading agents each with their own genome
(hyperparams + feature subset + risk parameters + weights):

* `modules/ept_evolution.py` — UNIFORM tensor-wise weight crossover
  (lifted from
  [ModelForge](https://github.com/saijayanth888/modelforge)'s LoRA-EPT),
  log-blend learning rates, union-then-sample feature subsets,
  Gaussian-noise mutation with σ-decay, 3 elites + 3 children +
  2 random newcomers per generation, auto-demotion when the champion's
  3-sample rolling Sharpe falls below 0.5.
* Fitness: `sharpe · (1 − dd / 0.15) · pf · √(trades / 50)` with a 15%
  drawdown clip.
* Snapshots every generation to `user_data/logs/evolution.json` with
  full lineage (`parent_a`, `parent_b`, `crossover_alpha`).

### 5. Risk governor + execution

* `modules/risk_governor.py` — a single `approve_entry()` call returns
  PASS/FAIL across:
  1. Portfolio drawdown ≥ 8% → trading paused (hysteresis at 4%)
  2. Realised daily loss ≥ 3% → blocked until next UTC midnight
  3. ≥ 6 concurrent positions
  4. Position size > 10% of portfolio
  5. Pearson correlation > 0.70 with any open-position pair
     (returns over 30 days)
  6. Circuit breaker: 5 consecutive losses → 4h cooldown
  7. Kelly-Criterion suggested position size from confidence + the last
     100 trades' empirical avg win/loss ratio (with safety scaling).
* All limits live under `config.json[risk_management]`.
* `modules/execution_engine.py` — Coinbase Advanced Trade limit-orders-
  only wrapper with a slippage gate (refuse if drift > 0.30%), 3-attempt
  exponential-backoff retry, 60-second order timeout, partial-fill
  tracking, structured order log.

### 6. Monitoring

* `modules/slack_alerts.py` — Block Kit notifications for trade entry/
  exit, daily P&L summary at UTC midnight, weekly evolution leaderboard
  + lineage, risk warning at 5% drawdown, critical at 8%, and any
  system error with traceback. 60-second dedup, dry-run mode.
* `modules/trade_journal.py` — `trade_journal` table inside
  `onchain.db` storing TFT probs + DRL votes + sentiment + regime +
  features used + reasoning. CSV / Markdown export.
* `modules/metrics_writer.py` — background-batched InfluxDB writer
  (7 measurements: pnl, trades, sharpe, win_rate, regime, sentiment,
  evolution); never blocks the trading loop.
* `grafana/` — auto-provisioned Flux datasource + 6-panel dashboard
  (cumulative P&L, rolling 30-day Sharpe, win rate, agent fitness over
  generations, regime distribution donut, sentiment-vs-price scatter).

### 7. Dashboard (TradingView-style)

`user_data/dashboard/` — standalone FastAPI app on port `8081`:

* Lightweight Charts v4 with 3 synced panes (candles + Bollinger +
  volume, RSI, MACD)
* Live trade markers (entry/exit arrows with P&L labels) from the
  trade journal
* Regime background shading + colored ribbon
* Sidebar: current regime, sentiment, on-chain, TFT signal, open
  positions, daily P&L history, champion agent ID, recent trades
* WebSocket push every 30 s, mobile-responsive layout

### 8. Go-live automation

Scripts under `scripts/`:

| Script | Role |
|---|---|
| `validate_readiness.py` | Reads `trade_journal`, exits 0 only if Sharpe>1.5, max-DD<12%, profit factor>1.4, win rate>55%, ≥200 trades |
| `go_live.sh init/advance/status/set` | Graduated capital exposure: stage 1 (10%) → 2 (30%) → 3 (50%) → 4 (99%), each transition gated on time-elapsed AND prior-window PnL > 0 |
| `emergency_stop.sh` | Flip `dry_run=true`, cancel all open Coinbase orders, snapshot state, alert Slack, restart container |
| `auto_rollback.py` | Hourly cron — emergency-stops on >3% daily loss, halves `tradable_balance_ratio` on negative weekly Sharpe |
| `backup.sh daily/weekly` | tar.gz of weights + config + journal (30 daily / 12 weekly retention) |
| `install_crontab.sh` | Idempotent crontab installer (replaces only the `trading-bot` block) |

---

## Quickstart

### 1. Prereqs

* Linux host with Docker + Docker Compose
* (Optional) NVIDIA GPU + nvidia-container-toolkit for TFT GPU training
* Python 3.12 on the host for the operator scripts (`scripts/*.py`,
  `scripts/*.sh`)

### 2. Configure secrets

```bash
cp .env.example .env
# edit .env — at minimum:
#   COINBASE_API_KEY / COINBASE_API_SECRET   (only required to go live)
#   ANTHROPIC_API_KEY                         (sentiment engine)
#   SLACK_WEBHOOK_URL                         (alerts)
#   INFLUX_TOKEN, INFLUX_ADMIN_PASSWORD       (metrics + Grafana)
#   GRAFANA_ADMIN_PASSWORD
```

Edit `user_data/config.json` to set:

| Field | Value |
|---|---|
| `exchange.key` / `exchange.secret` | Coinbase Advanced Trade keys |
| `api_server.username` / `password` | UI login |
| `api_server.jwt_secret_key` | `openssl rand -hex 32` |
| `api_server.ws_token` | `openssl rand -hex 16` |

`dry_run: true` is the default — no real money trades until you flip it.

### 3. Bring up the stack

```bash
docker compose up -d --build
```

This starts:

| Service | Port | What it does |
|---|---|---|
| `freqtrade` | 8080 | trading bot (FreqAI + TFT + DRL + risk + execution) |
| `influxdb` | 8086 | time-series store for Grafana panels |
| `grafana` | 3000 | observability dashboards (auto-provisioned) |
| `dashboard` | 8081 | TradingView-style live trade dashboard |

### 4. Paper-trade until ready

The bot runs `dry_run=true` from the day you start it. Watch the
journal fill up via:

```bash
python3 scripts/validate_readiness.py
```

When it reports READY (Sharpe > 1.5, max-DD < 12%, PF > 1.4, win-rate
> 55%, ≥ 200 trades), you can graduate.

### 5. Go live (graduated)

```bash
./scripts/go_live.sh init      # validate → dry_run=false → 10% exposure
./scripts/install_crontab.sh   # arm hourly safety net + nightly backups
```

Each subsequent week, run `./scripts/go_live.sh advance` — it refuses
unless both the time gate AND PnL gate are green.

### 6. Emergency stop

```bash
./scripts/emergency_stop.sh "reason text"
```

Flips dry-run, cancels open orders, snapshots state, alerts Slack.

---

## Tests

Pure-Python smoke tests with no external dependencies (no live keys, no
network requirement except where noted):

```bash
python3 tests/test_tft.py             # TFT GPU smoke
python3 tests/test_drl.py             # env + 3-agent training + voter + meta-agent
python3 tests/test_ept_evolution.py   # crossover/mutation/2-gen evolution
python3 tests/test_risk_execution.py  # all 7 risk rules + execution engine
python3 tests/test_monitoring.py      # slack + journal + metrics
python3 tests/test_go_live.py         # validate/auto-rollback/backup/cron
python3 tests/test_dashboard.py       # FastAPI app + websocket + indicators
```

Each prints PASS/FAIL per section and exits non-zero on any failure —
suitable for CI.

---

## Configuration reference

All knobs live under named blocks in `user_data/config.json`:

* `freqai.feature_parameters` — timeframes, indicator periods, label
  horizon, deadband for the `flat` class
* `freqai.model_training_parameters` — TFT hidden size, heads, dropout,
  AMP, compile, batch, epochs, early stop
* `risk_management` — every limit the governor checks
* `execution` — slippage / retry / timeout knobs
* `order_types`, `order_time_in_force`, `unfilledtimeout` — Freqtrade's
  native execution settings, kept consistent with `execution`

---

## License + acknowledgements

* Built on top of [Freqtrade](https://github.com/freqtrade/freqtrade)
* TFT architecture inspired by Lim et al. 2019, *"Temporal Fusion
  Transformers for Interpretable Multi-horizon Time Series Forecasting"*
* DRL implementations from
  [Stable-Baselines3](https://github.com/DLR-RM/stable-baselines3)
* EPT evolution operators ported from
  [ModelForge](https://github.com/saijayanth888/modelforge)
* Charting via TradingView's
  [Lightweight Charts](https://github.com/tradingview/lightweight-charts)

This is **research code for personal use**. Trade live at your own
risk; the authors take no responsibility for losses.
