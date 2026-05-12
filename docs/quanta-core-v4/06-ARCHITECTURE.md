# quanta-core/ — Architecture (v4, r6)

**Status:** design only — no code in this PR
**Branch:** `feat/quanta-core-v4-design-r6`
**Replaces:** Freqtrade + ad-hoc `stocks/shark/` runners
**Target host:** DGX Spark (128 GB unified memory, single GB10 Blackwell GPU)
**Author of design pass:** R6 design agent, 2026-05-12

---

## 0. Why a rewrite (one paragraph; rest of doc is structural)

Freqtrade ships an event loop that polls candles every N seconds, runs the strategy in lockstep, and pushes orders through CCXT. That model is fine for backtesting and for a single venue but the trading bot has outgrown it on three axes: (1) we now run crypto (Coinbase Advanced Trade) **and** US equities + options (Alpaca) **and** wheel covered-calls + cash-secured-puts that have nothing to do with the candle clock; (2) the model layer (TFT + Hermes 8B + Hermes 70B + LoRA adapters) wants to stay resident in GPU memory and answer multi-asset queries in parallel rather than be reloaded per-pair-per-loop; (3) the risk governor, monte-carlo gate and online-LoRA reflector all need to **see the same fill stream** and **write to the same ledger** — that's hard to bolt onto Freqtrade because Freqtrade owns the trade journal. quanta-core/ flips the model: one event loop, two venue adapters, one strategy ABC that the backtester replays bit-for-bit, one risk gate, one ledger, one config file.

The rest of this doc is the structural design — no algorithms, no research, no benchmarks. Function signatures and module boundaries only.

---

## 1. High-level architecture (ASCII)

```
                                +---------------------------------------------------+
                                |                   Operator (UI)                   |
                                |   FastAPI dashboard · Grafana · psql · Slack DM   |
                                +-----------+---------------------+-----------------+
                                            |                     |
                                            v                     v
+--------------------+         +----------- +---------+    +------+---------+
|  Alpaca WebSocket  |         |  Coinbase Advanced   |    |  News / X API  |
|   (stocks + opt)   |         |   Trade WebSocket    |    | (sentiment fd) |
+----------+---------+         +-----------+----------+    +------+---------+
           | trades+quotes                 | ticker+matches        | text events
           v                               v                       v
+------------------------------------------------------------------+--------+
|                quanta_core.live.engine  (asyncio loop)                    |
|                                                                           |
|   tick_aggregator -> on_tick() -> on_candle()  on_news()                  |
|        |                |             |             |                    |
|        +----------------+-+-----------+-------------+                    |
|                           |                                              |
|                           v                                              |
|             +-------------+--------------+   parallel fan-out             |
|             |  StrategyRouter (per symbol)                                |
|             |  - MeanRevTFT  (crypto + equities)                          |
|             |  - WheelCSP    (equities options)                           |
|             |  - SharkDebate (equities single-name)                       |
|             +-------------+--------------+                                |
|                           |                                              |
|         signal: Order proposal                                            |
|                           v                                              |
|     +---------------------+------------------------+                      |
|     |   quanta_core.agents.debate                  |  (optional gate)     |
|     |   bull || bear || reflector -> arbiter       |                      |
|     +---------------------+------------------------+                      |
|                           v                                              |
|     +---------------------+------------------------+                      |
|     |   quanta_core.risk.governor                  |                      |
|     |     hard gates: DD / DLL / corr / circuit    |                      |
|     |   quanta_core.risk.monte_carlo               |                      |
|     |     real-time VaR / ES                       |                      |
|     +---------------------+------------------------+                      |
|                           v                                              |
|     +---------------------+------------------------+                      |
|     |   quanta_core.execution.engine               |                      |
|     |     idempotent client_order_id (UUID5)       |                      |
|     |     slippage check + circuit breaker         |                      |
|     +-------+---------------------+----------------+                      |
|             |                     |                                       |
|     send order                send order                                  |
+-------------|---------------------|---------------------------------------+
              v                     v
   +----------+--------+   +--------+--------+
   | Alpaca REST       |   | Coinbase REST   |
   +----------+--------+   +--------+--------+
              |                     |
              +-----+---------+-----+
                    |         |
                    v         v
   fills_stream  +-------------------+
   ------------> |  Postgres ledger  |  <----  observability.metrics  ---->  Prometheus / Grafana
                 |  trades / fills /  |
                 |  decisions tables  |
                 +---------+----------+
                           |
                           v
                +----------+-----------+
                |  lora.online trigger |  (Reflector queues training batches)
                |  -> models.registry  |  (hot-swaps adapter weights)
                +----------------------+
```

Three primary planes are kept loosely coupled:

* **Data plane** (left, top): venue websockets feed `live.engine`.
* **Decision plane** (centre): strategy + debate + risk + execution. Stateless w.r.t. the network; talks only to the registry and ledger.
* **State plane** (right, bottom): Postgres ledger is the single source of truth. Metrics and LoRA training derive from it.

Backtesting reuses the **same decision plane**. The data plane is swapped for `backtest.engine` which replays OHLCV out of the ledger or a parquet store; the execution plane is swapped for a paper-fill simulator. Loose coupling means the swap is two import lines, not a fork of the strategy.

---

## 2. Complete file tree for `quanta-core/`

```
quanta-core/
├── pyproject.toml                        # poetry / uv-managed; pinned versions
├── README.md
├── HANDOFF.md                            # session-to-session notes (existing convention)
├── Dockerfile                            # CUDA 12.6 + py 3.12 + torch 2.6 + cudnn
├── docker-compose.yml                    # postgres · prometheus · grafana · quanta-core
├── config/
│   ├── default.toml                      # full config; live=false by default
│   ├── example.live.toml                 # ops template; gitignored secrets via env
│   └── schema.json                       # JSON schema for config validation
├── secrets/                              # gitignored
│   ├── alpaca.env
│   └── coinbase.env
├── docs/                                 # design docs live OUTSIDE this tree; see /docs/quanta-core-v4/
├── scripts/
│   ├── migrate_db.py                     # apply ledger SQL migrations
│   ├── backfill_ohlcv.py                 # one-shot history loader
│   ├── train_tft.py                      # weekly retrain entrypoint
│   ├── seed_lora_adapters.py             # cold-start LoRA from saved adapters
│   ├── replay_session.py                 # backtest CLI
│   ├── shadow_compare.py                 # live vs shadow strategy diff
│   └── emergency_stop.sh                 # tombstone file + SIGTERM
├── sql/
│   ├── 0001_init.sql                     # trades · fills · decisions · positions
│   ├── 0002_lora_runs.sql                # lora training audit
│   ├── 0003_metrics_views.sql            # materialised views for Grafana
│   └── 0004_idempotency.sql              # client_order_id unique index
├── tests/
│   ├── conftest.py
│   ├── unit/
│   │   ├── exchanges/
│   │   │   ├── test_alpaca_client.py
│   │   │   └── test_coinbase_client.py
│   │   ├── live/
│   │   │   ├── test_tick_aggregator.py
│   │   │   └── test_engine_dispatch.py
│   │   ├── backtest/
│   │   │   ├── test_replay_engine.py
│   │   │   └── test_walk_forward.py
│   │   ├── strategy/
│   │   │   ├── test_strategy_abc.py
│   │   │   └── test_mean_rev_tft.py
│   │   ├── models/
│   │   │   ├── test_registry.py
│   │   │   └── test_tft.py
│   │   ├── agents/
│   │   │   └── test_debate.py
│   │   ├── risk/
│   │   │   ├── test_governor.py
│   │   │   └── test_monte_carlo.py
│   │   ├── execution/
│   │   │   ├── test_engine.py
│   │   │   └── test_idempotency.py
│   │   ├── lora/
│   │   │   └── test_online.py
│   │   ├── ledger/
│   │   │   └── test_postgres.py
│   │   ├── observability/
│   │   │   └── test_metrics.py
│   │   └── test_config.py
│   ├── integration/
│   │   ├── test_tick_to_fill.py          # full path with fake venues
│   │   ├── test_backtest_matches_live.py # strategy parity oracle
│   │   ├── test_idempotency_replay.py    # restart mid-flight
│   │   └── test_shadow_mode.py
│   └── fixtures/
│       ├── ohlcv/                        # canned 1m + 5m bars for replay tests
│       ├── ticks/                        # canned tick streams
│       └── decisions/                    # canned arbiter JSON
└── quanta_core/
    ├── __init__.py
    ├── __main__.py                       # `python -m quanta_core` -> live.engine
    ├── version.py
    ├── config/
    │   ├── __init__.py
    │   ├── loader.py                     # TOML -> dataclass + env interpolation
    │   ├── schema.py                     # pydantic models
    │   └── live_paper_toggle.py          # the one flag — see §5
    ├── exchanges/
    │   ├── __init__.py
    │   ├── base.py                       # ExchangeAdapter ABC
    │   ├── alpaca.py                     # TradingClient wrapper
    │   ├── alpaca_stream.py              # WS consumer for stocks + options
    │   ├── coinbase.py                   # Advanced Trade wrapper
    │   ├── coinbase_stream.py            # WS consumer for crypto
    │   ├── paper.py                      # simulated venue (backtest + shadow)
    │   └── symbology.py                  # BTC/USD <-> BTC-USD <-> AAPL · option OCC
    ├── live/
    │   ├── __init__.py
    │   ├── engine.py                     # asyncio orchestration loop
    │   ├── tick_aggregator.py            # ticks -> bars at any timeframe
    │   ├── candle_buffer.py              # ring buffer per (symbol,tf)
    │   ├── dispatcher.py                 # fan-out to strategies by symbol
    │   ├── heartbeat.py                  # liveness probe + watchdog
    │   └── kill_switch.py                # operator tombstone file
    ├── backtest/
    │   ├── __init__.py
    │   ├── engine.py                     # replay OHLCV through Strategy ABC
    │   ├── walk_forward.py               # rolling train/test
    │   ├── slippage_model.py             # configurable fill simulation
    │   ├── reports.py                    # JSON + markdown summary
    │   └── parity_check.py               # live vs backtest divergence detector
    ├── strategy/
    │   ├── __init__.py
    │   ├── base.py                       # Strategy ABC (the contract)
    │   ├── context.py                    # immutable bundle handed to each hook
    │   ├── signal.py                     # Signal / OrderProposal dataclasses
    │   ├── mean_rev_tft.py               # port of FreqAIMeanRevV1
    │   ├── wheel_csp.py                  # port of stocks/wheel/strategy.py
    │   ├── shark_debate.py               # port of stocks/shark debate flow
    │   └── nfi_x6.py                     # port of NostalgiaForInfinityX6 (optional)
    ├── models/
    │   ├── __init__.py
    │   ├── registry.py                   # resident-in-memory model index
    │   ├── tft.py                        # port of TFTModel.py
    │   ├── tft_architecture.py           # port of tft_architecture.py
    │   ├── tft_serde.py                  # safe serde for TFT artifacts
    │   ├── sentiment.py                  # Hermes 8B wrapper (resident)
    │   ├── sentiment_70b.py              # Hermes 70B wrapper (evictable)
    │   ├── microstructure.py             # tick-level micro-price + OFI
    │   ├── lora_adapter.py               # PEFT adapter loader
    │   ├── inference_pool.py             # batched GPU inference queue
    │   └── memory_budget.py              # 128 GB unified accounting
    ├── agents/
    │   ├── __init__.py
    │   ├── debate.py                     # bull || bear || reflector -> arbiter
    │   ├── bull.py                       # port of analyst_bull.py
    │   ├── bear.py                       # port of analyst_bear.py
    │   ├── arbiter.py                    # port of decision_arbiter.py
    │   ├── reflector.py                  # port of trade_reviewer.py + outcome_resolver.py
    │   ├── prompts/                      # plain text; loaded at import
    │   │   ├── bull.md
    │   │   ├── bear.md
    │   │   ├── arbiter.md
    │   │   └── reflector.md
    │   └── schemas.py                    # port of stocks/shark/agents/schemas.py
    ├── risk/
    │   ├── __init__.py
    │   ├── governor.py                   # port of risk_governor.py
    │   ├── monte_carlo.py                # real-time VaR/ES gate
    │   ├── correlation.py                # rolling Pearson cache
    │   ├── kelly.py                      # fractional-Kelly sizer
    │   ├── circuit_breaker.py            # consecutive-loss trip
    │   └── anchors.py                    # daily anchor + DD pause persistence
    ├── execution/
    │   ├── __init__.py
    │   ├── engine.py                     # the only place that calls send_order
    │   ├── idempotency.py                # client_order_id SHA256 -> UUID5
    │   ├── slippage.py                   # pre-flight price drift check
    │   ├── timeout.py                    # cancel-after-N-seconds
    │   ├── partial_fill.py               # cumulative fill tracker
    │   ├── audit_log.py                  # rotating execution.log
    │   └── reconciliation.py             # on-restart sweep of open orders
    ├── lora/
    │   ├── __init__.py
    │   ├── online.py                     # reflector-triggered training
    │   ├── trainer.py                    # PEFT/LoRA loop, single GPU
    │   ├── dataset.py                    # build from decisions table
    │   ├── sampler.py                    # importance-sample by outcome
    │   ├── adapter_store.py              # disk + registry hot-swap
    │   └── policy.py                     # when-to-train rules
    ├── ledger/
    │   ├── __init__.py
    │   ├── postgres.py                   # port of modules/db.py
    │   ├── schemas.py                    # row dataclasses
    │   ├── trades.py                     # trade table CRUD
    │   ├── fills.py                      # fill table CRUD
    │   ├── decisions.py                  # decisions / debates CRUD
    │   ├── positions.py                  # materialised current positions
    │   ├── migrations.py                 # apply sql/ in order
    │   └── pool.py                       # async asyncpg pool
    ├── observability/
    │   ├── __init__.py
    │   ├── metrics.py                    # prometheus_client registry
    │   ├── tracing.py                    # opentelemetry spans
    │   ├── logging_config.py             # structured JSON logs
    │   ├── dashboard.py                  # FastAPI HTML + JSON endpoints
    │   ├── static/                       # CSS / JS (no SPA fluff)
    │   └── templates/                    # jinja2 page templates
    ├── data/
    │   ├── __init__.py
    │   ├── ohlcv_store.py                # parquet + duckdb cache
    │   ├── news_feed.py                  # news/x sentiment aggregator
    │   ├── universe.py                   # universe.json loader (existing)
    │   └── calendar.py                   # earnings + macro events
    └── util/
        ├── __init__.py
        ├── time.py                       # UTC-only helpers
        ├── async_io.py                   # gather-with-bounded-concurrency
        ├── retry.py                      # exponential backoff decorator
        ├── hashing.py                    # SHA256 helpers for idempotency
        └── errors.py                     # exception hierarchy
```

Counts:

* 18 packages
* ~110 Python files
* 1 TOML config (single source of truth)
* 4 SQL migrations
* Test mirror covers every package

---

## 3. Module API spec

Every signature below is the **public contract** — the body is left blank.
Private helpers prefixed `_` are not part of the contract.

All async functions are real coroutines (`asyncio` native).
Type aliases used throughout:

```python
# quanta_core/util/types.py (referenced by every module)
from decimal import Decimal
from datetime import datetime
from typing import Literal, NewType, TypedDict

Symbol = NewType("Symbol", str)           # canonical "BTC/USD" or "AAPL"
Venue = Literal["alpaca", "coinbase", "paper"]
Side = Literal["BUY", "SELL"]
Timeframe = Literal["1m", "5m", "15m", "1h", "4h", "1d"]
ClientOrderId = NewType("ClientOrderId", str)   # UUID5 string
VenueOrderId = NewType("VenueOrderId", str)
```

### 3.1 `quanta_core.exchanges.alpaca` — TradingClient wrapper

```python
# quanta_core/exchanges/alpaca.py

class AlpacaAdapter(ExchangeAdapter):
    """Alpaca TradingClient wrapper for US equities + options + crypto.

    Reads keys from environment (ALPACA_API_KEY / ALPACA_SECRET_KEY) or
    from the secrets/alpaca.env file referenced in config[venues.alpaca].
    Honours the global live/paper toggle (config.runtime.mode); when paper
    the SDK is pointed at https://paper-api.alpaca.markets and no real
    money is touched.

    Single-responsibility:
      * place / cancel / replace orders
      * fetch positions / orders / clock
      * stream subscription is handled by alpaca_stream.AlpacaStream

    NOT responsible for: idempotency (execution.engine owns that),
    slippage gating (execution.slippage owns that), retry logic
    (util.retry decorates calls here).
    """

    def __init__(self, cfg: AlpacaConfig, mode: Literal["live", "paper"]) -> None:
        """Construct with validated config and resolved mode."""

    async def submit_order(
        self,
        symbol: Symbol,
        side: Side,
        qty: Decimal,
        order_type: Literal["limit", "market", "stop_limit"],
        limit_price: Decimal | None,
        time_in_force: Literal["day", "gtc", "ioc", "fok"],
        client_order_id: ClientOrderId,
        extended_hours: bool = False,
    ) -> AlpacaOrderAck:
        """Submit a new order. Idempotent by client_order_id (Alpaca-enforced
        for 24h). Raises AlpacaRejected on broker rejection (e.g. PDT, BP).
        """

    async def cancel_order(self, venue_order_id: VenueOrderId) -> None:
        """Cancel; no-op if already filled or already cancelled."""

    async def replace_order(
        self,
        venue_order_id: VenueOrderId,
        new_qty: Decimal | None = None,
        new_limit_price: Decimal | None = None,
        new_time_in_force: Literal["day", "gtc"] | None = None,
    ) -> AlpacaOrderAck:
        """Modify an open order in place. Falls back to cancel+resubmit if
        the venue does not support modify for this order type."""

    async def get_open_orders(self, symbol: Symbol | None = None) -> list[AlpacaOrder]:
        """List open orders; optionally filter by symbol."""

    async def get_positions(self) -> list[AlpacaPosition]:
        """Current positions across the account."""

    async def get_account(self) -> AlpacaAccount:
        """Account snapshot — equity, BP, day_trades_remaining, pdt flag."""

    async def get_clock(self) -> AlpacaClock:
        """Market clock — is_open, next_open, next_close (UTC)."""

    # ----------- options-specific -----------

    async def submit_multileg_option_order(
        self,
        legs: list[OptionLeg],
        net_price: Decimal,
        time_in_force: Literal["day", "gtc"],
        client_order_id: ClientOrderId,
    ) -> AlpacaOrderAck:
        """Single ticket multi-leg option order. Wheel CSP/CC strategies use
        this for vertical/diagonal spreads. Validates leg count <= 4 (Alpaca
        limit) and net_price signed correctly."""

    async def get_option_chain(
        self,
        underlying: Symbol,
        expiration: datetime | None = None,
        moneyness: tuple[Decimal, Decimal] | None = None,
    ) -> OptionChain:
        """Fetch option chain for the underlying. Filtering done client-side
        for moneyness so the venue returns a stable payload."""

    async def health_check(self) -> AdapterHealth:
        """Lightweight ping — returns latency_ms and last_error."""
```

The stream side:

```python
# quanta_core/exchanges/alpaca_stream.py

class AlpacaStream:
    """WebSocket consumer for Alpaca market data (stocks + options + crypto).

    Yields normalised Tick events on .ticks() and Bar events on .bars().
    Auto-reconnects with exponential backoff; resubscribes on reconnect.
    Subscribes are additive — call .subscribe(symbols, channels) at any time.
    """

    def __init__(self, cfg: AlpacaStreamConfig, mode: Literal["live", "paper"]) -> None: ...

    async def connect(self) -> None:
        """Open WS, authenticate. Idempotent if already connected."""

    async def disconnect(self) -> None:
        """Close WS, cancel reader tasks."""

    async def subscribe(
        self,
        symbols: list[Symbol],
        channels: list[Literal["trades", "quotes", "bars", "options"]],
    ) -> None:
        """Subscribe additively. Resent on reconnect."""

    def ticks(self) -> AsyncIterator[Tick]:
        """Async iterator over normalised trade ticks."""

    def quotes(self) -> AsyncIterator[Quote]:
        """Async iterator over normalised quote (bbo) updates."""

    def bars(self) -> AsyncIterator[Bar]:
        """Async iterator over venue-aggregated 1m bars (used for reconciliation
        against the local tick_aggregator output)."""
```

### 3.2 `quanta_core.exchanges.coinbase` — Advanced Trade wrapper

```python
# quanta_core/exchanges/coinbase.py

class CoinbaseAdapter(ExchangeAdapter):
    """Coinbase Advanced Trade wrapper.

    Auth via ECDSA key (CB_ADV_KEY_FILE pointing to the JSON key the
    Coinbase portal exports). Live and paper share the same REST surface;
    paper mode short-circuits at submit_order and returns a synthetic
    fill via exchanges.paper.PaperVenue.

    Mirrors AlpacaAdapter's surface so the strategy layer can dispatch on
    Venue without branching on type.
    """

    def __init__(self, cfg: CoinbaseConfig, mode: Literal["live", "paper"]) -> None: ...

    async def submit_order(
        self,
        symbol: Symbol,
        side: Side,
        base_size: Decimal,
        order_type: Literal["limit", "market"],
        limit_price: Decimal | None,
        time_in_force: Literal["gtc", "ioc", "fok"],
        client_order_id: ClientOrderId,
        post_only: bool = False,
    ) -> CoinbaseOrderAck:
        """Submit limit or market order on a spot product. base_size in base
        currency (BTC, not USD). Coinbase enforces client_order_id uniqueness
        for 90 days — execution.idempotency stays inside that window."""

    async def cancel_order(self, venue_order_id: VenueOrderId) -> None: ...
    async def cancel_all(self, symbol: Symbol | None = None) -> int:
        """Cancel every open order (optionally by symbol). Returns count
        cancelled. Used by execution.engine on circuit-breaker trip."""

    async def get_open_orders(self, symbol: Symbol | None = None) -> list[CoinbaseOrder]: ...
    async def get_fills(self, since: datetime | None = None) -> list[CoinbaseFill]:
        """Fill history; reconciliation.on_startup() uses this to backfill
        any fills that the WS missed during a restart."""

    async def get_accounts(self) -> list[CoinbaseAccount]: ...
    async def get_product(self, symbol: Symbol) -> CoinbaseProduct:
        """Tick size / step size / quote_increment — strategy rounds to these."""

    async def get_best_bid_ask(self, symbol: Symbol) -> tuple[Decimal, Decimal]:
        """Direct REST poll. Used by execution.slippage as a sanity check
        when the WS book is stale."""

    async def health_check(self) -> AdapterHealth: ...
```

And the stream:

```python
# quanta_core/exchanges/coinbase_stream.py

class CoinbaseStream:
    """Advanced Trade WebSocket consumer.

    Channels: ticker, ticker_batch, level2, matches, user (auth required).
    user channel emits fills the moment Coinbase posts them — these are
    the canonical fills written to ledger.fills (not the REST poll).
    """

    def __init__(self, cfg: CoinbaseStreamConfig, mode: Literal["live", "paper"]) -> None: ...
    async def connect(self) -> None: ...
    async def disconnect(self) -> None: ...
    async def subscribe(self, symbols: list[Symbol], channels: list[str]) -> None: ...
    def ticks(self) -> AsyncIterator[Tick]: ...
    def quotes(self) -> AsyncIterator[Quote]: ...
    def user_fills(self) -> AsyncIterator[Fill]:
        """Authenticated 'user' channel — every fill on this account."""
```

### 3.3 `quanta_core.live.engine` — WebSocket consumer → candle aggregator → signal dispatcher

```python
# quanta_core/live/engine.py

class LiveEngine:
    """The main event loop.

    Composes:
      - one or more ExchangeAdapter (alpaca, coinbase, paper)
      - one or more *Stream consumers
      - per-symbol tick_aggregator
      - StrategyRouter (dispatcher)
      - risk.governor + risk.monte_carlo (gates)
      - execution.engine
      - ledger.postgres (durability)
      - observability.metrics (always on)

    Lifecycle:
      __init__ -> setup_complete (validate config, build adapters)
      start()  -> connect streams, register strategies, run main loop
      stop()   -> drain in-flight, cancel orders if configured, close ledger

    Top-level error policy: any uncaught exception inside a strategy hook
    is logged + metric-bumped, but does NOT take down the engine. An
    exception inside the risk or execution path DOES take down the engine
    (fail-loud, fail-fast).
    """

    def __init__(self, cfg: Config) -> None:
        """Resolve config; build adapters; build strategy router; do NOT
        open network connections yet."""

    async def start(self) -> None:
        """Open all streams; warm models.registry; replay open positions
        from ledger; enter main loop. Returns only on stop()."""

    async def stop(self, *, cancel_open_orders: bool = False) -> None:
        """Graceful shutdown. If cancel_open_orders, sends cancel_all() to
        each venue first."""

    async def health(self) -> EngineHealth:
        """Aggregate of every adapter's health_check + ledger ping + GPU
        memory free. Exposed by observability.dashboard."""

    # ------------- internal coroutines (one task each, kept here for clarity) -------------

    async def _tick_pump(self, stream: Stream) -> None:
        """Pull ticks off a single stream; feed tick_aggregator + dispatcher."""

    async def _fill_pump(self, stream: Stream) -> None:
        """Pull fills off the user channel; persist to ledger; notify strategies."""

    async def _heartbeat(self) -> None:
        """Periodic liveness probe; writes to metrics; trips kill_switch on
        stream death + N reconnect failures."""

    async def _reconcile_on_start(self) -> None:
        """On boot: list open orders/positions from every venue and reconcile
        against ledger. Mismatches raise — operator decides."""
```

The dispatcher is its own small class:

```python
# quanta_core/live/dispatcher.py

class StrategyRouter:
    """Maps (symbol -> [Strategy]) and invokes on_candle / on_tick / on_fill
    hooks under bounded concurrency (one task per (strategy, symbol)).

    A symbol can be subscribed by multiple strategies (e.g. MeanRevTFT and
    SharkDebate on AAPL); each gets the event independently. Strategies
    never see each other's state — coupling is via the ledger only.
    """

    def __init__(self, registry: ModelRegistry, risk: RiskGovernor,
                 monte_carlo: MonteCarloGate, execution: ExecutionEngine,
                 ledger: PostgresLedger) -> None: ...

    def register(self, strategy: Strategy) -> None:
        """Add a strategy. Idempotent on (strategy.name, strategy.symbols)."""

    def unregister(self, strategy_name: str) -> None: ...

    async def dispatch_tick(self, tick: Tick) -> None: ...
    async def dispatch_candle(self, bar: Bar) -> None: ...
    async def dispatch_fill(self, fill: Fill) -> None: ...
```

### 3.4 `quanta_core.live.tick_aggregator` — ticks → bars at any timeframe

```python
# quanta_core/live/tick_aggregator.py

class TickAggregator:
    """Per-symbol tick -> bar aggregator.

    Supports arbitrary timeframes from 1s up to 1d (boundary clock is UTC).
    Emits a closed Bar event the first tick AFTER the boundary, so callers
    receive bars exactly once and at most one timeframe behind real time.

    On a venue reconnect the aggregator does NOT extrapolate — it waits for
    the next tick. Reconciliation against venue-aggregated 1m bars is done
    by live.engine and a mismatch counter is exposed via metrics.
    """

    def __init__(self, symbol: Symbol, timeframes: list[Timeframe]) -> None:
        """Create per-symbol aggregator; allocates one OHLCV state per tf."""

    def on_tick(self, tick: Tick) -> list[Bar]:
        """Ingest a single tick. Returns the list of bars (possibly empty)
        that closed on this tick — one per timeframe whose boundary fell
        between the previous tick and this one. Caller dispatches each."""

    def on_bar_replace(self, bar: Bar) -> None:
        """Force-overwrite a bar (used by reconciliation when the venue's
        1m bar disagrees with the locally aggregated 1m bar by > epsilon).
        Increments metrics.tick_aggregator_corrections."""

    def latest(self, tf: Timeframe) -> Bar | None:
        """Return the most recent CLOSED bar at this timeframe (or None
        if not enough ticks yet)."""

    def in_progress(self, tf: Timeframe) -> Bar | None:
        """The currently-open (unclosed) bar. Strategies should NOT consume
        this; it's exposed for the dashboard only."""
```

### 3.5 `quanta_core.backtest.engine` — replay OHLCV through the SAME strategy class as live

```python
# quanta_core/backtest/engine.py

class BacktestEngine:
    """Replays historical OHLCV through the exact same Strategy class that
    runs live. The strategy cannot tell the difference: same on_candle hook,
    same Context bundle, same Order proposal API. Difference is:

      - exchange adapter is exchanges.paper.PaperVenue
      - fills are simulated by backtest.slippage_model
      - clock is the bar clock, not wall clock
      - no WebSocket; bars are read from data.ohlcv_store

    This is the parity oracle: tests/integration/test_backtest_matches_live.py
    feeds the same strategy a real recorded tick stream through live.engine
    AND a replay of the OHLCV through backtest.engine; the orders proposed
    must match exactly (modulo configured slippage tolerance).
    """

    def __init__(
        self,
        strategy: Strategy,
        ohlcv: OhlcvSource,
        slippage: SlippageModel,
        risk: RiskGovernor,
        monte_carlo: MonteCarloGate,
        starting_equity: Decimal,
        start: datetime,
        end: datetime,
    ) -> None: ...

    async def run(self) -> BacktestResult:
        """Run the full replay. Returns trades, fills, equity curve,
        drawdown stats, and the decisions the arbiter made (so debate
        prompts can be replayed offline)."""

    async def step(self) -> BacktestStep | None:
        """Single-bar step; returns None when ohlcv exhausted. Useful for
        walk_forward which controls the clock externally."""

    def reset(self, start: datetime | None = None) -> None:
        """Re-arm the engine for a fresh run without rebuilding strategy."""
```

```python
# quanta_core/backtest/slippage_model.py

class SlippageModel(Protocol):
    """A pluggable model that turns an OrderProposal + current bar into a
    Fill (or None for unfilled).

    Two built-ins:
      - PessimisticFixed(bps=5): fill at limit_price + bps for buys, -bps for sells.
      - HistoricalSpread(): sample fills from the bar's OHLC spread.
    """

    def fill(self, proposal: OrderProposal, bar: Bar, book: Book | None) -> Fill | None: ...
```

### 3.6 `quanta_core.backtest.walk_forward` — rolling train/test split

```python
# quanta_core/backtest/walk_forward.py

class WalkForward:
    """Rolling-origin train/test runner.

    Splits the historical window into N folds. For each fold:
      1. Train phase: feed train slice to strategy.train_hook (no orders).
      2. Evaluate phase: replay test slice through BacktestEngine; collect
         BacktestResult.
    Aggregates per-fold metrics into a WalkForwardReport.

    The train phase is what calls models.tft.train() with the in-sample
    data — so the same WalkForward driver covers both classical and ML
    strategies without branching.
    """

    def __init__(
        self,
        strategy_factory: Callable[[], Strategy],
        ohlcv: OhlcvSource,
        train_window: timedelta,
        test_window: timedelta,
        step: timedelta,
        slippage: SlippageModel,
        risk_cfg: RiskConfig,
    ) -> None: ...

    async def run(self, start: datetime, end: datetime) -> WalkForwardReport:
        """Execute every fold in sequence. Returns a list of
        (train_start, train_end, test_start, test_end, BacktestResult) plus
        aggregated stats (sharpe, calmar, win rate, MAR)."""

    async def run_fold(self, fold: WalkForwardFold) -> BacktestResult:
        """Run a single fold. Public so callers can parallelise across
        folds with asyncio.gather + bounded semaphore."""
```

### 3.7 `quanta_core.strategy.base` — Strategy ABC with `on_candle/on_fill/on_tick` hooks

This is the load-bearing contract. Full text in §5; signatures repeated here:

```python
# quanta_core/strategy/base.py

class Strategy(ABC):
    """Abstract base class. Every concrete strategy implements at least
    on_candle. on_tick and on_fill are optional (default: no-op).

    The framework guarantees:
      - hook calls are serialised per (strategy, symbol)
      - Context.now() always returns deterministic time (wall clock live,
        bar clock backtest)
      - any OrderProposal returned is routed through risk + execution
        BEFORE the next hook fires
      - on_fill is called after the ledger.fills row is committed
    """

    name: str
    symbols: list[Symbol]
    timeframes: list[Timeframe]

    @abstractmethod
    async def on_candle(self, bar: Bar, ctx: Context) -> list[OrderProposal]:
        """Called once per closed bar. Return proposed orders (may be empty)."""

    async def on_tick(self, tick: Tick, ctx: Context) -> list[OrderProposal]:
        """Optional. Default no-op. Called for every tick BEFORE
        aggregation."""
        return []

    async def on_fill(self, fill: Fill, ctx: Context) -> list[OrderProposal]:
        """Optional. Default no-op. Called after a fill is committed to
        the ledger. Use for trailing stops, scale-outs, etc."""
        return []

    async def on_start(self, ctx: Context) -> None:
        """Called once before the first event. Use for warm-up only."""

    async def on_stop(self, ctx: Context) -> None:
        """Called once on graceful shutdown."""

    def train_hook(self, train_slice: OhlcvSlice) -> None:
        """Called by walk_forward in train phase. Default no-op. ML
        strategies override to call models.tft.train(...) etc."""
```

### 3.8 `quanta_core.strategy.mean_rev_tft` — port of FreqAIMeanRevV1

```python
# quanta_core/strategy/mean_rev_tft.py

class MeanRevTFT(Strategy):
    """Mean-reversion strategy with TFT confidence overlay and BollingerRSI
    fallback. 1:1 functional port of user_data/strategies/FreqAIMeanRevV1.py:

      Indicators: RSI(14), MACD, Bollinger(20,2), volume SMA ratio, ATR(14).
      Entry: bb_oversold_revert AND rsi<=30 AND (tft_up OR blind_fallback).
      Exit:  bb_upper touch OR rsi>=70 OR ATR-trail OR risk.governor force-exit.
      TFT-blind fallback (config.strategy.tft_blind_fallback.enabled, paper-mode
      default True): when models.registry has no TFT artifact for the symbol,
      trade at position_size_multiplier (default 0.5) of normal stake; all
      other gates still apply.

    Resident TFT prediction is fetched via models.registry.infer(symbol, bar)
    so the GPU pool batches across strategies.
    """

    name: str = "mean_rev_tft"

    def __init__(self, cfg: MeanRevTFTConfig) -> None: ...

    async def on_start(self, ctx: Context) -> None:
        """Warm RSI/MACD/BB/ATR buffers from ledger or ohlcv_store."""

    async def on_candle(self, bar: Bar, ctx: Context) -> list[OrderProposal]:
        """Compute indicators; query models.registry for TFT confidence;
        decide entry/exit; return at most one OrderProposal."""

    async def on_fill(self, fill: Fill, ctx: Context) -> list[OrderProposal]:
        """Update internal position state; arm ATR trailing stop."""

    def train_hook(self, train_slice: OhlcvSlice) -> None:
        """Delegate to models.tft.TFT.train(train_slice, symbol=self.symbol)."""

    # ------- internal helpers -------

    def _entry_signal(self, df: pd.DataFrame) -> bool: ...
    def _exit_signal(self, df: pd.DataFrame, position: Position) -> bool: ...
    def _stake(self, ctx: Context, confidence: float) -> Decimal:
        """Return Kelly-suggested stake, scaled by 0.5 if blind fallback."""
```

### 3.9 `quanta_core.models.registry` — TFT + sentiment + microstructure + LoRA adapters resident in memory

```python
# quanta_core/models/registry.py

class ModelRegistry:
    """Single index of every loaded model. Held in memory for the lifetime
    of the engine. Models are loaded lazily on first use; eviction policy
    is enforced by memory_budget.MemoryBudget.

    GPU memory plan (128 GB unified, DGX Spark):
      Hermes 3 70B (evictable) : ~40 GB
      Hermes 3 8B  (resident)  : ~5 GB
      TFT (per-symbol, pooled) : ~38 GB cap shared across symbols
      LoRA adapters            : 100-500 MB each, hot-swappable
      Microstructure model     : ~2 GB
      Headroom                 : ~20 GB

    Inference is batched: callers .infer() and get back a future; the pool
    in inference_pool.py forms micro-batches to amortise kernel launch.
    """

    def __init__(self, cfg: ModelsConfig, budget: MemoryBudget) -> None: ...

    async def load(self, model_id: ModelId) -> None:
        """Load a model artifact from disk. Idempotent if already resident.
        Raises NoBudgetError if the budget can't accommodate it (caller
        decides whether to evict first)."""

    async def unload(self, model_id: ModelId) -> None:
        """Evict the model; free GPU+host memory."""

    async def infer(self, model_id: ModelId, payload: InferencePayload) -> InferenceResult:
        """Submit a single inference; awaits batched response. The pool
        handles fairness across callers."""

    async def hot_swap_lora(self, base_model_id: ModelId, adapter_path: Path) -> None:
        """Apply a new LoRA adapter to an already-loaded base model
        WITHOUT re-loading the base weights. Called by lora.online when
        a training run completes."""

    def list_loaded(self) -> list[LoadedModel]: ...
    def memory_snapshot(self) -> MemorySnapshot: ...

    async def health(self) -> RegistryHealth: ...
```

### 3.10 `quanta_core.models.tft` — port of TFTModel.py

```python
# quanta_core/models/tft.py

class TFT:
    """Temporal Fusion Transformer. 1:1 port of user_data/freqaimodels/TFTModel.py
    minus the Freqtrade-specific BasePyTorchClassifier glue.

    Public surface:
      - train(train_slice, symbol): fit once; persist artifact to disk and
        register with ModelRegistry.
      - predict(bar_window, symbol): return up_prob, down_prob, quantile spread.
      - quantile_spread is exposed as tft_confidence (matches the existing
        Freqtrade column name).

    GPU budget: set_per_process_memory_fraction enforced via models.memory_budget.
    Training uses AMP + torch.compile by default; toggle via config.
    """

    def __init__(self, cfg: TFTConfig) -> None: ...

    def train(self, train_slice: OhlcvSlice, symbol: Symbol) -> TFTTrainResult:
        """Synchronous train (single GPU lock). Caller runs in executor."""

    def predict(self, window: np.ndarray, symbol: Symbol) -> TFTPrediction:
        """Inference on a single bar window. Returns dataclass with up_prob,
        down_prob, quantile_low, quantile_high, tft_confidence."""

    def save(self, path: Path) -> None: ...

    @classmethod
    def load(cls, path: Path) -> "TFT": ...
```

### 3.11 `quanta_core.agents.debate` — parallel bull/bear/arbiter/reflector

```python
# quanta_core/agents/debate.py

class DebateOrchestrator:
    """Runs adversarial bull <-> bear debate, then a deciding arbiter, then
    (asynchronously, post-trade) a reflector for outcome resolution.

    Parallelism:
      - bull and bear run in PARALLEL within each round (asyncio.gather).
      - arbiter is sequential after the last round.
      - reflector runs OUT-OF-BAND on trade close; never blocks decisions.

    Config:
      debate.rounds : 0 disables debate; 1+ enables.
      debate.max_concurrency : cap on simultaneous (symbol) debates.
      debate.model_bull / model_bear / model_arbiter : ModelRegistry ids.

    The orchestrator does NOT decide trades by itself; it returns an
    ArbiterDecision that the strategy then converts (or not) into an
    OrderProposal. The strategy retains veto power.
    """

    def __init__(
        self,
        registry: ModelRegistry,
        bull: BullAnalyst,
        bear: BearAnalyst,
        arbiter: Arbiter,
        reflector: Reflector,
        cfg: DebateConfig,
    ) -> None: ...

    async def deliberate(
        self,
        symbol: Symbol,
        context: DebateContext,
    ) -> ArbiterDecision:
        """Run N rounds; return ArbiterDecision (action + confidence +
        full transcript). Transcript is persisted to ledger.decisions."""

    async def reflect(self, trade: ClosedTrade) -> ReflectionResult:
        """Post-trade outcome resolution. Writes to memory + may enqueue
        a lora.online training batch."""
```

Sub-agents are thin wrappers around `ModelRegistry.infer`:

```python
# quanta_core/agents/bull.py
class BullAnalyst:
    async def argue(self, symbol: Symbol, ctx: DebateContext,
                    rebuttal_to: str | None) -> AgentTurn: ...

# quanta_core/agents/bear.py
class BearAnalyst:
    async def argue(self, symbol: Symbol, ctx: DebateContext,
                    rebuttal_to: str | None) -> AgentTurn: ...

# quanta_core/agents/arbiter.py
class Arbiter:
    async def decide(self, symbol: Symbol, transcript: list[AgentTurn],
                     ctx: DebateContext) -> ArbiterDecision: ...

# quanta_core/agents/reflector.py
class Reflector:
    async def review(self, trade: ClosedTrade) -> ReflectionResult: ...
```

### 3.12 `quanta_core.risk.governor` — port of risk_governor.py

```python
# quanta_core/risk/governor.py

class RiskGovernor:
    """Pre-trade hard gates. Single point of approval for every order.

    Direct port of user_data/modules/risk_governor.py. Reads its limits
    from config.risk.* so runtime config reload is enough to relax/tighten.

    Hard gates (any failing -> block trade):
      1. Portfolio drawdown >= max_portfolio_drawdown_pct -> trading_paused.
      2. Daily realised PnL <= -daily_loss_limit_pct of starting equity.
      3. Open positions >= max_concurrent_positions.
      4. Position size > max_position_size_pct.
      5. Pair correlation > correlation_threshold against any open position.
      6. Circuit breaker: >= N consecutive losses -> ban for cooldown_hours.

    Soft outputs:
      - kelly_fraction (fractional Kelly, scaled by safety factor)
      - reason string

    Anchor persistence (P0-G fix): daily anchor + DD-pause flag persist to
    risk_governor_anchors.json so process restarts mid-loss don't reset
    the loss budget. Backtest runmodes use a per-process transient anchor
    under /tmp (never touches live anchor).
    """

    def __init__(self, cfg: RiskConfig, ledger: PostgresLedger,
                 mode: Literal["live", "paper", "backtest"]) -> None: ...

    async def approve(self, proposal: OrderProposal,
                      portfolio: PortfolioSnapshot) -> RiskDecision:
        """The one method execution.engine calls before every send. Returns
        Approved(kelly_fraction, reason) or Blocked(reason)."""

    async def on_fill(self, fill: Fill) -> None:
        """Update internal pnl/drawdown/loss-streak state. Called after
        ledger.fills commit so the next approve() reflects new state."""

    async def force_flat(self, reason: str) -> list[OrderProposal]:
        """Generate exit orders for every open position. Used when a hard
        gate trips after positions are already open (e.g. drawdown breached
        on an open position re-mark)."""

    def snapshot(self) -> RiskSnapshot:
        """Read-only view for the dashboard."""

    async def reload_config(self, new_cfg: RiskConfig) -> None:
        """Hot-reload limits without restart."""
```

### 3.13 `quanta_core.risk.monte_carlo` — real-time VaR/ES gate

```python
# quanta_core/risk/monte_carlo.py

class MonteCarloGate:
    """Real-time Value-at-Risk + Expected-Shortfall gate.

    On every OrderProposal, simulates N forward paths of the WHOLE
    portfolio (existing positions + proposed addition) using bootstrap or
    parametric (Cornish-Fisher) draws over the recent return window. Blocks
    the trade if 1-day 95% VaR exceeds var_limit_pct OR if 1-day 97.5%
    Expected Shortfall exceeds es_limit_pct.

    Separate from RiskGovernor because:
      - it's expensive (10k paths) -> runs on a dedicated thread/executor
      - it sees the full portfolio, not just this trade
      - operator can disable it independently (paper-mode warm-up)
    """

    def __init__(self, cfg: MonteCarloConfig, ledger: PostgresLedger) -> None: ...

    async def evaluate(self, proposal: OrderProposal,
                       portfolio: PortfolioSnapshot) -> MCDecision:
        """Returns Approved(var, es) or Blocked(reason, var, es)."""

    async def evaluate_existing(self, portfolio: PortfolioSnapshot) -> MCSnapshot:
        """Compute VaR/ES on the current portfolio (no new trade). Called
        periodically by observability so the dashboard shows the risk
        envelope independently of new orders."""

    def snapshot(self) -> MCSnapshot: ...
```

### 3.14 `quanta_core.execution.engine` — port of execution_engine.py with slippage + circuit breaker

```python
# quanta_core/execution/engine.py

class ExecutionEngine:
    """The single chokepoint where orders leave the process.

    Responsibilities:
      - assemble final client_order_id via execution.idempotency.
      - pre-flight slippage check (execution.slippage).
      - submit via the correct ExchangeAdapter.
      - track partial fills (execution.partial_fill).
      - cancel-after-timeout (execution.timeout).
      - structured audit log (execution.audit_log).
      - reconciliation on startup (execution.reconciliation).
      - circuit breaker: stop submitting if N consecutive submit failures.

    Honours the live/paper toggle by dispatching to the paper venue when
    config.runtime.mode == "paper" without touching strategy code.
    """

    def __init__(
        self,
        adapters: Mapping[Venue, ExchangeAdapter],
        idempotency: IdempotencyService,
        slippage: SlippageGate,
        risk: RiskGovernor,
        ledger: PostgresLedger,
        cfg: ExecutionConfig,
    ) -> None: ...

    async def submit(self, proposal: OrderProposal,
                     portfolio: PortfolioSnapshot) -> OrderReport:
        """End-to-end: idempotent id -> slippage check -> risk -> venue
        submit -> persist -> return OrderReport. Raises only on programmer
        error; broker rejections are returned in OrderReport.status."""

    async def cancel(self, client_order_id: ClientOrderId) -> None: ...
    async def cancel_all(self, venue: Venue | None = None) -> int: ...

    async def on_fill(self, fill: Fill) -> None:
        """WebSocket fill arrival path. Persist + notify strategies via
        dispatcher.dispatch_fill."""

    async def reconcile(self) -> ReconcileReport:
        """Sweep open orders + positions on every venue, diff against
        ledger, return mismatches. Called by LiveEngine._reconcile_on_start."""

    def snapshot(self) -> ExecutionSnapshot: ...
```

### 3.15 `quanta_core.execution.idempotency` — client_order_id schema (SHA256 → UUID5)

```python
# quanta_core/execution/idempotency.py

# Single canonical namespace UUID for the whole stack. Never change this.
QUANTA_NAMESPACE: Final = uuid.UUID("c6a4...-deadbeef-...0001")

@dataclass(frozen=True)
class IdempotencyKey:
    """The five fields that determine identity. Any change to any of these
    yields a different client_order_id."""
    strategy_name: str
    symbol: Symbol
    side: Side
    intent_timestamp_ms: int       # the bar-close timestamp the decision was made on
    qty_str: str                   # canonicalised decimal (no trailing zeros)


class IdempotencyService:
    """Deterministic, restart-safe order id generation.

    Algorithm:
      1. canonical = json.dumps(IdempotencyKey, sort_keys=True, separators=...)
      2. sha = sha256(canonical).digest()                       # 32 bytes
      3. uid = uuid.uuid5(QUANTA_NAMESPACE, hex(sha[:16]))      # deterministic UUID
      4. client_order_id = str(uid)                             # 36 chars

    Properties guaranteed:
      - Same key -> same id (deterministic).
      - Different keys -> different id (collision-free in practice).
      - Replayable: a restart mid-decision regenerates the same id, so a
        retry of submit() is rejected by the venue (Alpaca + Coinbase both
        enforce client_order_id uniqueness for >= 24h).
      - DB-enforced: sql/0004_idempotency.sql adds a unique index on
        trades.client_order_id, so an in-process bug can't double-submit.
    """

    def __init__(self, ledger: PostgresLedger) -> None: ...

    def make_key(
        self,
        strategy_name: str,
        symbol: Symbol,
        side: Side,
        intent_timestamp_ms: int,
        qty: Decimal,
    ) -> IdempotencyKey: ...

    def derive_client_order_id(self, key: IdempotencyKey) -> ClientOrderId:
        """SHA256 -> UUID5. Pure function."""

    async def reserve(self, client_order_id: ClientOrderId,
                      proposal: OrderProposal) -> ReservationResult:
        """INSERT into trades with status='reserved'. If a row with this
        id already exists -> ReservationResult.kind in {'replay', 'duplicate'}
        and execution.engine skips the venue call. Belt-and-braces: even if
        the venue forgets the id, the DB unique constraint stops a double-fire."""

    async def commit(self, client_order_id: ClientOrderId, ack: VenueAck) -> None:
        """Transition trades.status from 'reserved' to 'acked'."""

    async def abandon(self, client_order_id: ClientOrderId, reason: str) -> None:
        """Transition to 'failed'."""
```

### 3.16 `quanta_core.lora.online` — continuous LoRA training trigger per Reflector

```python
# quanta_core/lora/online.py

class OnlineLoRATrigger:
    """Watches ledger.decisions + ledger.fills, builds training batches from
    the reflector's reviewed trades, and kicks off PEFT/LoRA training on a
    schedule. Hot-swaps the resulting adapter into ModelRegistry.

    Policy (configurable):
      - Train when N closed trades have been reflected since last run.
      - OR when M hours have elapsed.
      - Never run two trainings in parallel (single-GPU lock).
      - Pause if available GPU memory < threshold (memory_budget signal).
    """

    def __init__(
        self,
        registry: ModelRegistry,
        ledger: PostgresLedger,
        trainer: LoRATrainer,
        adapter_store: AdapterStore,
        cfg: OnlineLoRAConfig,
    ) -> None: ...

    async def start(self) -> None:
        """Begin watch loop as a background task."""

    async def stop(self) -> None: ...

    async def maybe_train(self) -> LoRATrainResult | None:
        """Check policy; if satisfied, run trainer.fit + hot_swap_lora."""

    def snapshot(self) -> OnlineLoRASnapshot: ...
```

And the helper modules:

```python
# quanta_core/lora/trainer.py
class LoRATrainer:
    def fit(self, base_model_id: ModelId, dataset: LoRADataset,
            cfg: LoRATrainConfig) -> Path:
        """Train and return path to the saved adapter."""

# quanta_core/lora/dataset.py
class LoRADataset:
    """Build training pairs from decisions table:
       (debate_transcript, arbiter_decision, realised_outcome) -> training row."""
    def __init__(self, ledger: PostgresLedger, cfg: LoRADatasetConfig) -> None: ...
    async def build(self, since: datetime) -> "LoRADataset": ...

# quanta_core/lora/adapter_store.py
class AdapterStore:
    def save(self, base_model_id: ModelId, adapter_path: Path,
             metadata: dict) -> AdapterRecord: ...
    def latest(self, base_model_id: ModelId) -> AdapterRecord | None: ...
    def list(self, base_model_id: ModelId) -> list[AdapterRecord]: ...
```

### 3.17 `quanta_core.ledger.postgres` — single source of truth (trades, fills, decisions)

```python
# quanta_core/ledger/postgres.py

class PostgresLedger:
    """Async postgres adapter; single source of truth for trades, fills,
    decisions, positions, lora_runs. Replaces user_data/modules/db.py.

    Connection: asyncpg pool, DSN from DATABASE_URL env var.
    Schema: sql/0001_init.sql .. sql/0004_idempotency.sql applied via
    ledger.migrations on first connect (idempotent).

    No module other than this one is allowed to import asyncpg.
    """

    def __init__(self, dsn: str, pool_size: int = 10) -> None: ...

    async def connect(self) -> None: ...
    async def close(self) -> None: ...
    async def migrate(self) -> None: ...

    # ---- trades ----
    async def insert_trade(self, trade: TradeRow) -> None: ...
    async def update_trade_status(self, client_order_id: ClientOrderId,
                                  status: TradeStatus, **kwargs: Any) -> None: ...
    async def get_trade(self, client_order_id: ClientOrderId) -> TradeRow | None: ...
    async def list_open_trades(self) -> list[TradeRow]: ...

    # ---- fills ----
    async def insert_fill(self, fill: FillRow) -> None: ...
    async def fills_for_trade(self, client_order_id: ClientOrderId) -> list[FillRow]: ...

    # ---- decisions ----
    async def insert_decision(self, decision: DecisionRow) -> None: ...
    async def decisions_since(self, since: datetime,
                              symbol: Symbol | None = None) -> list[DecisionRow]: ...

    # ---- positions ----
    async def positions(self) -> list[PositionRow]:
        """Materialised by sql/0003_metrics_views.sql for O(1) reads."""
    async def portfolio_snapshot(self) -> PortfolioSnapshot: ...

    # ---- lora runs ----
    async def insert_lora_run(self, run: LoRARunRow) -> None: ...

    # ---- ops ----
    async def ping(self) -> float:
        """Returns round-trip latency in ms."""
```

### 3.18 `quanta_core.observability.metrics` — prometheus + dashboard

```python
# quanta_core/observability/metrics.py

# Module-level registry; every other module imports counters/gauges from here.
# Naming convention: quanta_core_<area>_<metric>.

# Examples (NOT exhaustive):
ticks_received_total = Counter(
    "quanta_core_ticks_received_total", "Ticks received per venue/symbol",
    ["venue", "symbol"],
)
bars_emitted_total = Counter(
    "quanta_core_bars_emitted_total", "Closed bars emitted by tick_aggregator",
    ["symbol", "tf"],
)
orders_submitted_total = Counter(
    "quanta_core_orders_submitted_total", "Orders sent to a venue",
    ["venue", "symbol", "side", "strategy"],
)
orders_blocked_total = Counter(
    "quanta_core_orders_blocked_total", "Orders blocked by a gate",
    ["gate", "reason"],
)
fills_received_total = Counter(
    "quanta_core_fills_received_total", "Fills received",
    ["venue", "symbol", "side"],
)
risk_drawdown_pct = Gauge(
    "quanta_core_risk_drawdown_pct", "Current portfolio drawdown",
)
risk_paused = Gauge(
    "quanta_core_risk_paused", "1 if trading is paused by governor",
)
ledger_latency_ms = Histogram(
    "quanta_core_ledger_latency_ms", "Postgres round-trip latency",
)
gpu_memory_used_gb = Gauge(
    "quanta_core_gpu_memory_used_gb", "GPU memory used (unified)",
    ["model_id"],
)
lora_runs_total = Counter("quanta_core_lora_runs_total", "LoRA training runs")

def start_http_server(port: int = 9100) -> None:
    """Start the prometheus_client HTTP server on the metrics port."""
```

The dashboard module exposes a FastAPI app:

```python
# quanta_core/observability/dashboard.py

def build_app(engine: LiveEngine) -> FastAPI:
    """Wire up:
      GET  /                   -> overview HTML
      GET  /strategy/{name}    -> strategy detail HTML
      GET  /api/health         -> JSON
      GET  /api/positions      -> JSON
      GET  /api/decisions      -> JSON (latest 100)
      GET  /api/risk           -> JSON
      GET  /metrics            -> Prometheus exposition
      POST /api/kill           -> drop tombstone file (auth required)
      POST /api/resume         -> remove tombstone (auth required)
    """
```

### 3.19 `quanta_core.config` — single TOML config; live/paper toggle is ONE flag

```toml
# config/default.toml (excerpt; full schema in config/schema.json)

[runtime]
mode = "paper"                # "paper" | "live"  <- THE ONE FLAG
log_level = "INFO"
tombstone_path = "/var/run/quanta-core.killed"

[ledger]
dsn = "env:DATABASE_URL"

[venues.alpaca]
enabled = true
api_key = "env:ALPACA_API_KEY"
api_secret = "env:ALPACA_SECRET_KEY"
base_url_live = "https://api.alpaca.markets"
base_url_paper = "https://paper-api.alpaca.markets"

[venues.coinbase]
enabled = true
key_file = "secrets/coinbase.json"

[universe]
path = "config/universe.json"

[strategies.mean_rev_tft]
enabled = true
symbols = ["BTC/USD", "ETH/USD", "SOL/USD", "BCH/USD"]
timeframes = ["5m"]
tft_blind_fallback.enabled = true
tft_blind_fallback.position_size_multiplier = 0.5

[risk]
max_portfolio_drawdown_pct = 0.08
daily_loss_limit_pct = 0.03
max_position_size_pct = 0.10
max_concurrent_positions = 6
correlation_threshold = 0.70
correlation_lookback_days = 30
circuit_breaker_consecutive_losses = 5
circuit_breaker_cooldown_hours = 4
kelly_enabled = true
kelly_lookback_trades = 100
kelly_safety_factor = 0.5
kelly_max_fraction = 0.25

[monte_carlo]
enabled = false               # off in paper warm-up; flip after 30d of clean data
n_paths = 10000
var_pct = 0.95
es_pct = 0.975
var_limit_pct = 0.03
es_limit_pct = 0.05

[execution]
slippage_pct = 0.003
order_timeout_sec = 60
circuit_breaker_consecutive_submit_failures = 3

[models]
tft_artifact_dir = "models/tft"
hermes_8b_id = "hermes3:8b"
hermes_70b_id = "hermes3:70b"
gpu_memory_limit_gb = 110     # 128 - 18 headroom

[debate]
rounds = 1
max_concurrency = 4
model_bull = "hermes3:8b"
model_bear = "hermes3:8b"
model_arbiter = "hermes3:70b"

[lora]
enabled = true
min_reflected_trades = 50
min_hours_between_runs = 24
base_model = "hermes3:8b"

[observability]
prometheus_port = 9100
dashboard_port = 8088
log_format = "json"
```

```python
# quanta_core/config/loader.py

def load(path: Path | str | None = None) -> Config:
    """Load TOML, interpolate env:VAR refs, validate against pydantic
    schema, return a Config dataclass. The single flag is config.runtime.mode."""

def reload(current: Config, path: Path) -> Config:
    """Re-read TOML and return a new Config; callers decide which
    sub-config to hot-swap (risk.reload_config, etc.)."""
```

```python
# quanta_core/config/live_paper_toggle.py

def is_live(cfg: Config) -> bool:
    return cfg.runtime.mode == "live"

def assert_live_safety(cfg: Config) -> None:
    """Refuse to start in live mode unless:
      - tombstone path is writable
      - postgres ledger is reachable
      - both venues' health_check passes
      - monte_carlo.enabled is True
      - risk anchors file exists OR explicit cfg.allow_fresh_anchor is True
    """
```

---

## 4. Tick → decision → fill flow (sequence diagram)

```
WS-stream      tick_agg        dispatcher     strategy       agents.debate    risk.governor    monte_carlo    execution.engine    venue            postgres
   |               |                |              |                |                |                |                |              |                  |
   |  Tick(t)      |                |              |                |                |                |                |              |                  |
   |-------------->|                |              |                |                |                |                |              |                  |
   |               | on_tick        |              |                |                |                |                |              |                  |
   |               |--------------->| dispatch_tick|                |                |                |                |              |                  |
   |               |                |------------->| on_tick(t)     |                |                |                |              |                  |
   |               |                |              | -> []          |                |                |                |              |                  |
   |               |                |              |                |                |                |                |              |                  |
   |               | bar closes     |              |                |                |                |                |              |                  |
   |               |--------------->| dispatch_candle|              |                |                |                |              |                  |
   |               |                |------------->| on_candle(bar) |                |                |                |              |                  |
   |               |                |              |--registry.infer (TFT)----------------------------------+              |              |                  |
   |               |                |              |<-confidence-----------------------------------------+   |              |              |                  |
   |               |                |              |   (optional) ->| deliberate     |                |   |              |              |                  |
   |               |                |              |                |--bull||bear--->|                |   |              |              |                  |
   |               |                |              |                |<--turns--------|                |   |              |              |                  |
   |               |                |              |                |--arbiter------>|                |   |              |              |                  |
   |               |                |              |                |<-ArbiterDecision                |   |              |              |                  |
   |               |                |              |                | persist decision -------------------------------------------------------> INSERT     |
   |               |                |              |<- OrderProposal|                |                |   |              |              |                  |
   |               |                |              |---risk.approve(proposal, portfolio)------------->|   |              |              |                  |
   |               |                |              |<--RiskDecision (Approved | Blocked)--------------|   |              |              |                  |
   |               |                |              |---monte_carlo.evaluate(proposal, portfolio)---------->|              |              |                  |
   |               |                |              |<--MCDecision-------------------------------------------|              |              |                  |
   |               |                |              |---execution.submit(proposal)------------------------------------------>| reserve --|----------------> INSERT trades (status=reserved)
   |               |                |              |                |                |                |                |               |                  |
   |               |                |              |                |                |                |                |--slippage check                  |
   |               |                |              |                |                |                |                |--adapter.submit_order            |
   |               |                |              |                |                |                |                |-------------->|                  |
   |               |                |              |                |                |                |                |<--VenueAck----|                  |
   |               |                |              |                |                |                |                |---commit------|----------------> UPDATE trades (status=acked)
   |               |                |              |<- OrderReport--|                |                |                |               |                  |
   |               |                |              |                |                |                |                |               |                  |
   ... wall clock passes ...                                                                                                                              |
   |               |                |              |                |                |                |                |               |                  |
   | Fill(co_id)   |                |              |                |                |                |                |               |                  |
   |-------------->|                |              |                |                |                |                |               |                  |
   |  (fill_pump)                                                                                                                                         |
   |-------------------------------> on_fill ----->|                |                |                |                |              | insert_fill ---->INSERT fills
   |                                              |-->governor.on_fill (update DD/PnL/streak)         |                |              |                  |
   |                                              |--dispatch_fill->| on_fill(fill)                  |                |              |                  |
   |                                              |                | -> []                          |                |              |                  |
   |                                              |--reflector.review (async, off-path)             |                |              |                  |
   |                                              |               (post-trade outcome resolution)    |                |              |                  |
   |                                                          (later, on schedule)                                                                       |
   |                                                          lora.online.maybe_train -> adapter -> registry.hot_swap_lora                               |
```

Key invariants the diagram encodes:

1. **Strategy never talks to venue.** Only `execution.engine` does.
2. **Risk gates always run, even in paper mode.** The toggle flips the *venue*, not the gates.
3. **Idempotency is a DB row, not just a UUID.** The `reserve` step writes `trades` with `status=reserved` BEFORE the venue is touched. A crash between reserve and submit replays cleanly because the same key regenerates the same id and the unique index in `sql/0004_idempotency.sql` rejects a double-insert.
4. **Reflector is out-of-band.** It does not block on-tick latency.
5. **LoRA training is opportunistic.** It runs only when policy allows AND GPU memory budget allows.

---

## 5. Strategy ABC — full Python interface

```python
# quanta_core/strategy/base.py

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any, Protocol

from quanta_core.util.types import Symbol, Timeframe, Side, Venue


# ---------- domain objects strategies see ----------

@dataclass(frozen=True)
class Bar:
    """A closed OHLCV bar. Strategies receive these via on_candle.

    Timestamp convention: `close_ts` is the inclusive close time UTC; a 5m
    bar spanning [09:00, 09:05) has close_ts=09:05:00.
    """
    symbol: Symbol
    timeframe: Timeframe
    open_ts: datetime
    close_ts: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal


@dataclass(frozen=True)
class Tick:
    """A single trade print (not a quote)."""
    symbol: Symbol
    ts: datetime
    price: Decimal
    size: Decimal
    side: Side | None        # None if venue doesn't disclose aggressor


@dataclass(frozen=True)
class Fill:
    """A confirmed fill. Strategies receive these via on_fill."""
    symbol: Symbol
    side: Side
    qty: Decimal
    price: Decimal
    ts: datetime
    client_order_id: str
    venue_order_id: str
    venue: Venue
    fee: Decimal


@dataclass(frozen=True)
class Position:
    """Net position after fills."""
    symbol: Symbol
    qty: Decimal             # signed (positive long, negative short)
    avg_price: Decimal
    unrealised_pnl: Decimal
    realised_pnl: Decimal
    opened_at: datetime


@dataclass(frozen=True)
class OrderProposal:
    """What a strategy returns from its hooks.

    The strategy DOES NOT generate the client_order_id itself — execution
    .idempotency derives it deterministically from (strategy_name, symbol,
    side, intent_timestamp_ms, qty). This means a re-run of the same hook
    in the same bar will produce the same id and be rejected as a duplicate.
    """
    strategy_name: str
    symbol: Symbol
    venue: Venue
    side: Side
    qty: Decimal
    order_type: str                          # "limit" | "market" | "stop_limit"
    limit_price: Decimal | None
    stop_price: Decimal | None
    time_in_force: str                       # "day" | "gtc" | "ioc" | "fok"
    intent_timestamp_ms: int                 # bar close ts in epoch ms
    metadata: dict[str, Any]                 # free-form; debate transcript ref, etc.
    extended_hours: bool = False


class Context(Protocol):
    """Immutable bundle handed to each hook. Implementations live in
    strategy/context.py. Strategies must NOT mutate anything here.
    """

    def now(self) -> datetime:
        """Wall clock live; bar clock backtest. Use this — never datetime.now()."""

    def portfolio(self) -> PortfolioSnapshot:
        """Current portfolio (positions + equity + DD) at the time of this hook."""

    def position(self, symbol: Symbol) -> Position | None:
        """Convenience: just this symbol's position, if any."""

    def history(self, symbol: Symbol, tf: Timeframe, n: int) -> list[Bar]:
        """Last N closed bars including the just-closed one. Reads from
        candle_buffer; falls back to ohlcv_store if insufficient."""

    def predict(self, model_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        """Sync wrapper around ModelRegistry.infer. Strategies should use
        this rather than calling registry directly — it lets the framework
        batch + meter."""

    def debate(self, payload: dict[str, Any]) -> ArbiterDecision | None:
        """Optional adversarial debate. Returns None if debate is disabled
        in config. Strategies ALWAYS retain veto power over the decision."""

    def journal(self, event: str, payload: dict[str, Any]) -> None:
        """Free-form structured log entry. Persisted to ledger.decisions
        with a row_type='journal'."""

    def kill_switch_engaged(self) -> bool:
        """True if operator has dropped the tombstone file."""


class Strategy(ABC):
    """The contract every concrete strategy implements.

    Lifecycle:
      __init__         construct from config; no I/O.
      on_start         (await) one-time warm-up; called before any event.
      on_tick          (await) optional. Default no-op. Receives every tick.
      on_candle        (await) MANDATORY. Receives every closed bar.
      on_fill          (await) optional. Default no-op. After ledger commit.
      on_stop          (await) graceful shutdown.
      train_hook       optional. Called by walk_forward in train phase.

    Each hook returns a list[OrderProposal]. The framework will route each
    proposal through risk + execution. A strategy that returns [] is a
    no-op for that event.

    Threading model:
      hook calls for the same (strategy, symbol) are serialised. Calls for
      different symbols within the same strategy may run concurrently.
      Strategies must therefore be reentrant per-symbol but may share state
      across symbols (e.g. a global cooldown counter) using their own locks.

    Determinism:
      A strategy MUST produce the same OrderProposals when given the same
      stream of Ticks/Bars/Fills and the same Context. This is what makes
      backtest.engine the parity oracle. Any non-determinism (random
      sampling, time.time(), datetime.now()) is a bug.
    """

    name: str                                # class attribute, must be unique
    symbols: list[Symbol]                    # what this instance trades
    timeframes: list[Timeframe]              # what bars it wants
    wants_ticks: bool = False                # opt-in to on_tick (perf)
    wants_news: bool = False                 # opt-in to on_news (future)

    @abstractmethod
    async def on_candle(self, bar: Bar, ctx: Context) -> list[OrderProposal]:
        """REQUIRED. Process a closed bar; return proposed orders."""

    async def on_tick(self, tick: Tick, ctx: Context) -> list[OrderProposal]:
        """Optional. Default no-op."""
        return []

    async def on_fill(self, fill: Fill, ctx: Context) -> list[OrderProposal]:
        """Optional. Default no-op. Useful for trailing stops / scale-outs."""
        return []

    async def on_start(self, ctx: Context) -> None:
        """Optional. Default no-op. Warm-up + buffer hydration."""
        return None

    async def on_stop(self, ctx: Context) -> None:
        """Optional. Default no-op. Persist any in-memory state."""
        return None

    def train_hook(self, train_slice: OhlcvSlice) -> None:
        """Optional. Default no-op. Walk-forward calls this in train phase
        for ML strategies."""
        return None

    # ---------- introspection helpers (default impls in base.py) ----------

    def describe(self) -> StrategyDescription:
        """Return name, symbols, timeframes, version, model dependencies.
        Used by the dashboard."""
```

This contract is identical in live and backtest. The framework can swap
ExchangeAdapter, OhlcvSource, and clock without touching the strategy.

---

## 6. Reuse map — existing files that port directly into `quanta-core/`

| Existing path | New path under `quanta_core/` | Port nature |
|---|---|---|
| `user_data/modules/risk_governor.py` | `quanta_core/risk/governor.py` + `quanta_core/risk/anchors.py` + `quanta_core/risk/circuit_breaker.py` + `quanta_core/risk/kelly.py` + `quanta_core/risk/correlation.py` | Split a 32-KB monolith into single-responsibility files; same algorithms, same gate semantics. |
| `user_data/modules/execution_engine.py` | `quanta_core/execution/engine.py` + `quanta_core/execution/slippage.py` + `quanta_core/execution/timeout.py` + `quanta_core/execution/audit_log.py` + `quanta_core/execution/partial_fill.py` | Same. Coinbase-specific bits become the Coinbase adapter; common bits become the generic engine. |
| `user_data/modules/db.py` | `quanta_core/ledger/postgres.py` + `quanta_core/ledger/pool.py` + `quanta_core/ledger/migrations.py` | psycopg-sync → asyncpg; same schema, same DSN env var. |
| `user_data/freqaimodels/TFTModel.py` | `quanta_core/models/tft.py` | Strip the Freqtrade `BasePyTorchClassifier` glue; keep the TFT itself. |
| `user_data/freqaimodels/tft_architecture.py` | `quanta_core/models/tft_architecture.py` | Verbatim. |
| `user_data/freqaimodels/tft_pickle.py` | `quanta_core/models/tft_serde.py` (or fold into `tft.py`) | Verbatim safe-serde helper. |
| `user_data/strategies/FreqAIMeanRevV1.py` | `quanta_core/strategy/mean_rev_tft.py` | Re-express against the Strategy ABC; indicators identical. |
| `user_data/strategies/NostalgiaForInfinityX6.py` | `quanta_core/strategy/nfi_x6.py` | Optional, large; only if operator wants it ported. |
| `user_data/modules/regime_detector.py` | `quanta_core/strategy/context.py` (regime hooks) + `quanta_core/data/calendar.py` | Regime classification becomes a Context method exposed to every strategy. |
| `user_data/modules/sentiment_engine.py` | `quanta_core/models/sentiment.py` | Wraps Hermes 8B via registry. |
| `user_data/modules/sentiment_prompts.py` | `quanta_core/agents/prompts/` (split per agent) | One markdown per agent. |
| `user_data/modules/news_aggregator.py` | `quanta_core/data/news_feed.py` | Verbatim, dependency-inversed (yields events on a queue). |
| `user_data/modules/onchain_signals.py` | `quanta_core/data/onchain.py` (new file) | Verbatim move. |
| `user_data/modules/unified_risk.py` | Folded into `quanta_core/risk/monte_carlo.py` | The MC simulation already lives here; just move it. |
| `user_data/modules/drl_ensemble.py` | `quanta_core/models/drl_ensemble.py` (new file) | Verbatim if/when DRL strategy is ported. |
| `user_data/modules/ensemble_voter.py` | `quanta_core/models/ensemble_voter.py` (new file) | Verbatim. |
| `user_data/modules/notifier.py`, `telegram_alerts.py`, `slack_alerts.py` | `quanta_core/observability/notifiers/{telegram,slack}.py` | Same code; new home. |
| `user_data/modules/monitoring_mixin.py` | `quanta_core/observability/metrics.py` (style; not mixin) | Convert mixin into module-level Prometheus counters/gauges. |
| `user_data/modules/metrics_writer.py` | Same | |
| `user_data/modules/trade_journal.py` | `quanta_core/ledger/trades.py` + `quanta_core/ledger/decisions.py` | Split into per-table CRUDs. |
| `user_data/modules/ept_evolution.py` | `quanta_core/lora/policy.py` (renamed) | The evolutionary policy heuristics become the lora.online policy. |
| `user_data/modules/meta_agent.py` | `quanta_core/agents/arbiter.py` | The meta-agent becomes the arbiter. |
| `user_data/modules/trading_env.py` | Dropped (Freqtrade-RL glue, no longer needed) | — |
| `user_data/modules/ollama_health.py` | `quanta_core/models/inference_pool.py` (health check method) | Subsumed. |
| `stocks/shark/agents/analyst_bull.py` | `quanta_core/agents/bull.py` | Verbatim. |
| `stocks/shark/agents/analyst_bear.py` | `quanta_core/agents/bear.py` | Verbatim. |
| `stocks/shark/agents/decision_arbiter.py` | `quanta_core/agents/arbiter.py` | Verbatim (merge with meta_agent). |
| `stocks/shark/agents/debate_orchestrator.py` | `quanta_core/agents/debate.py` | Verbatim. |
| `stocks/shark/agents/trade_reviewer.py` + `outcome_resolver.py` | `quanta_core/agents/reflector.py` | Merged. |
| `stocks/shark/agents/schemas.py` | `quanta_core/agents/schemas.py` | Verbatim. |
| `stocks/shark/agents/combined_analyst.py` | dropped (legacy single-call path; debate is now the default) | — |
| `stocks/shark/agents/sentiment_analyst.py` | `quanta_core/agents/sentiment.py` | Verbatim. |
| `stocks/shark/agents/market_analyst.py` | `quanta_core/agents/market.py` | Verbatim. |
| `stocks/shark/agents/risk_debate.py` + `risk_manager.py` | `quanta_core/risk/governor.py` (integrated) | The risk debate becomes one extra step inside `RiskGovernor.approve`. |
| `stocks/shark/data/alpaca_data.py` | `quanta_core/exchanges/alpaca.py` (data-fetch methods) | Folded into adapter. |
| `stocks/shark/data/market_regime.py` | `quanta_core/strategy/context.py` (regime helper) | Re-exposed via Context. |
| `stocks/shark/data/sentiment*.py` | `quanta_core/data/sentiment/` (one file each) | Verbatim moves. |
| `stocks/shark/data/macro_calendar.py` + `pead.py` | `quanta_core/data/calendar.py` | Merged. |
| `stocks/shark/data/sp500.py` + `watchlist*.py` + `universe`-ish | `quanta_core/data/universe.py` | Single universe loader; `config/universe.json` is the source of truth (carrying forward existing convention). |
| `stocks/shark/data/knowledge_base.py` + `kb_scoring.py` | `quanta_core/data/knowledge_base.py` | Verbatim. |
| `stocks/shark/data/technical.py` | `quanta_core/data/indicators.py` | TA-lib wrappers shared across strategies. |
| `stocks/shark/data/relative_strength.py` | `quanta_core/data/indicators.py` (RS function) | Folded. |
| `stocks/shark/data/perplexity.py` | `quanta_core/data/perplexity.py` | Verbatim. |
| `stocks/shark/data/indicator_selection.py` | `quanta_core/data/indicator_selection.py` | Verbatim. |
| `stocks/shark/execution/orders.py` | folded into `quanta_core/execution/engine.py` | — |
| `stocks/shark/execution/exit_manager.py` | `quanta_core/strategy/exit_manager.py` (strategy-side helper) | — |
| `stocks/shark/execution/position_sizer.py` | `quanta_core/risk/kelly.py` (+ Strategy.\_stake helpers) | — |
| `stocks/shark/execution/stops.py` | `quanta_core/strategy/stops.py` (strategy-side helpers) | — |
| `stocks/shark/execution/guardrails.py` | `quanta_core/risk/governor.py` (additional gates) | — |
| `stocks/shark/ml/tft_stock.py` | `quanta_core/models/tft.py` (symbol-agnostic) | The crypto and stock TFTs collapse into one symbol-parametrised class. |
| `stocks/shark/ml/dataset_stock.py` | `quanta_core/models/datasets.py` | Verbatim. |
| `stocks/shark/ml/features_stock.py` | `quanta_core/data/features.py` | Verbatim. |
| `stocks/shark/ml/drl_ensemble_stocks.py` | `quanta_core/models/drl_ensemble.py` (merged) | — |
| `stocks/shark/ml/ept_evolution_stocks.py` | `quanta_core/lora/policy.py` (merged) | — |
| `stocks/shark/ml/cli.py` | `scripts/train_tft.py` + `scripts/seed_lora_adapters.py` | Split by purpose. |
| `stocks/shark/phases/*.py` (market_open, midday, pre_execute, pre_market, daily_summary, weekly_review) | `quanta_core/scripts/phases/*.py` (or routines) | Same logic; invoked by cron, replace today's `stocks_day_runner.sh`. |
| `stocks/shark/phases/backtest.py` | `quanta_core/backtest/engine.py` (CLI side under `scripts/replay_session.py`) | — |
| `stocks/shark/phases/kb_refresh.py` + `kb_update.py` | `scripts/refresh_kb.py` | Merged. |
| `stocks/shark/context/context_manager.py` | `quanta_core/strategy/context.py` | The Context Protocol implementation. |
| `stocks/wheel/strategy.py` | `quanta_core/strategy/wheel_csp.py` | Re-expressed against Strategy ABC. |
| `stocks/wheel/broker.py` | folded into `quanta_core/exchanges/alpaca.py` | — |
| `stocks/wheel/runner.py` + `cli.py` | folded into the live engine + `scripts/` | — |
| `stocks/wheel/state.py` | `quanta_core/ledger/positions.py` (option-aware columns) | The wheel state moves into the canonical ledger. |
| `stocks/api/main.py` | `quanta_core/observability/dashboard.py` | Merged into the one dashboard. |
| `scripts/auto_rollback.py` | `scripts/auto_rollback.py` | Verbatim (cron-driven). |
| `scripts/nightly_reflector.py` | `scripts/nightly_reflector.py` | Calls `quanta_core.agents.reflector` instead of the legacy module. |
| `scripts/modelforge_*.py` | `scripts/modelforge_*.py` | Verbatim. |
| `scripts/retrain_tft_pairs.py` | `scripts/train_tft.py` | Renamed; same semantics. |
| `scripts/rebalance_capital.py` | `scripts/rebalance_capital.py` | Verbatim; reads new ledger. |
| `scripts/sync_universe.sh` | `scripts/sync_universe.sh` | Verbatim. |
| `user_data/freqtrade_entrypoint.py` | dropped (replaced by `python -m quanta_core`) | — |
| `Dockerfile.freqtrade` | replaced by `quanta-core/Dockerfile` | — |

That covers 100% of the live-relevant Python in `user_data/modules/`, `user_data/freqaimodels/`, `user_data/strategies/`, `stocks/shark/`, `stocks/wheel/`, and the relevant top-level `scripts/`. Anything not on this table is documentation, fixtures, or one-off backfill scripts that don't need migrating.

---

## 7. Loose-coupling boundaries — what talks to what

The dependency rule: **arrows go DOWN the stack only**. A module may import from layers strictly below it; never from above; never sideways within the same layer except through narrow ABCs.

```
LAYER 0  util/                                                     (no deps)
            |
LAYER 1  config/                                                   (uses util)
            |
LAYER 2  ledger/                                                   (uses util + config)
            |
LAYER 3  exchanges/                                                (uses util + config)
LAYER 3  models/                                                   (uses util + config + ledger)
LAYER 3  data/                                                     (uses util + config + ledger)
            |
LAYER 4  risk/             observability/                          (uses ledger + config)
            |
LAYER 5  agents/                                                   (uses models + ledger)
            |
LAYER 6  strategy/                                                 (uses exchanges + models + risk + agents + data via Context only)
            |
LAYER 7  execution/        lora/                                   (uses exchanges + risk + ledger + models)
            |
LAYER 8  backtest/         live/                                   (uses everything below)
            |
LAYER 9  __main__.py + scripts/                                    (entry points)
```

Explicit rules:

1. **Strategy does not import exchanges.** It returns `OrderProposal`. Routing belongs to `execution.engine`. (Bug-prevention: a strategy that mocks an exchange in tests is a 30-minute rabbit hole — the ABC forbids it.)
2. **Strategy does not import ledger.** It reads state via `Context`. The Context implementation owns the ledger.
3. **Strategy does not import models.registry directly.** It calls `ctx.predict(model_id, payload)`. This lets backtest swap the registry for a deterministic cache.
4. **execution.engine is the only writer for trades + fills tables.** Everyone else reads.
5. **agents/ does not depend on strategy/.** Strategy *uses* an agent (DebateOrchestrator); the agent doesn't know what strategy called it.
6. **risk/ does not depend on agents/.** Risk is mechanical (gates) not deliberative. If we want LLM-flavoured risk debate, that lives behind `agents/risk_debate.py` and is OPTIONALLY composed by `risk.governor` via an injected interface — not a hard import.
7. **observability/ depends on nothing except util.** Counters are imported by the modules that increment them, not the other way around. This keeps the metric registry from cycling.
8. **lora/ depends on ledger + models + agents.reflector** but NOT on strategy or execution. Training is fed by the ledger; it doesn't care what produced the rows.
9. **backtest/ and live/ are siblings.** Neither imports the other. They share Strategy ABC + RiskGovernor + ExecutionEngine (with venue swapped to paper for backtest).

One narrow exception, justified: `live.engine` imports both `live.dispatcher` and `execution.engine` because it's the orchestrator. That's its job; it doesn't violate the rule because everything `live.engine` touches is below it.

---

## 8. Testing strategy

Three concentric rings.

### 8.1 Unit tests (`tests/unit/`)

* One test file per source file.
* Every public function/method has at least one test.
* External dependencies (HTTP, WebSocket, DB, GPU) are mocked.
* Run in CI on every push; budget < 60s wall-clock total.
* `pytest -m unit` selects this ring.

Coverage targets:

| Module | Coverage floor | Notes |
|---|---|---|
| `risk/` | 95% | Hard gates must be airtight; mutation testing recommended. |
| `execution/idempotency.py` | 100% | SHA256→UUID5 must be byte-stable across pythons. |
| `execution/engine.py` | 90% | Includes reconciliation paths. |
| `live/tick_aggregator.py` | 95% | Edge cases: boundary ticks, missing ticks, timeframe transitions. |
| `strategy/base.py` | 100% | The contract itself. |
| `strategy/mean_rev_tft.py` | 85% | Indicator math; entry/exit branches. |
| `models/registry.py` | 80% | Memory-budget logic, hot-swap; mock the GPU. |
| `ledger/postgres.py` | 85% | Use `pytest-asyncio` + a real postgres test container. |
| Everything else | 70% | Don't let a module ship under 70%. |

### 8.2 Integration tests (`tests/integration/`)

End-to-end through the decision plane with fake venues. Each is its own scenario.

1. **`test_tick_to_fill.py`** — full path. Fake WS pushes 200 ticks; strategy proposes 1 order; risk approves; paper venue fills it; ledger row count == expected; metrics show 1 fill. Asserts the entire sequence diagram from §4.

2. **`test_backtest_matches_live.py`** — *the parity oracle*. Record a real tick stream (from sandbox venue or a fixture). Run twice:
   * **Path A**: through `live.engine` with a paper venue.
   * **Path B**: aggregate the ticks into bars, run `backtest.engine` against the same strategy class.
   Assert the lists of OrderProposals are identical (modulo a slippage tolerance the test declares up front). If this test fails, the strategy is non-deterministic — that's a hard release blocker.

3. **`test_idempotency_replay.py`** — boot, propose an order, crash *between* `idempotency.reserve` and `exchanges.submit_order`. Reboot. Assert: same client_order_id is regenerated; venue receives at most one submit; ledger ends with status=acked exactly once.

4. **`test_shadow_mode.py`** — run two strategies on the same WS stream simultaneously. Strategy A is live (uses real paper-venue). Strategy B is shadow (uses `exchanges.paper.PaperVenue` regardless of mode, even if cfg.runtime.mode == "live"). Assert: shadow's proposed orders are written to `ledger.decisions` with row_type='shadow'; no real orders are sent for B; metrics distinguish A and B.

5. **`test_risk_pause_persists.py`** — simulate a 4% intraday loss; assert `RiskGovernor` flips `paused_for_drawdown=true` and writes the anchor file. Restart the engine; assert the next `approve()` call returns Blocked with the same reason, no orders sent. This is the regression test for the 2026-05-12 anchor-persistence bug.

6. **`test_monte_carlo_gate_blocks.py`** — load a portfolio that already has 5 correlated positions; propose a 6th; assert MC's VaR exceeds limit; assert `execution.submit` returns Blocked; assert no DB row.

7. **`test_lora_hot_swap.py`** — train a tiny LoRA adapter on a fixture; call `registry.hot_swap_lora`; assert subsequent `registry.infer` returns adapter-influenced outputs; assert no other resident model was reloaded.

CI budget: < 5 min wall-clock total. `pytest -m integration` selects this ring.

### 8.3 Shadow-mode (production gate)

Before flipping `config.runtime.mode = "live"`, the strategy must spend N days in shadow on the live data feed:

* `exchanges.paper.PaperVenue` is wired in alongside the real venue.
* Strategy proposes orders against the real tick stream.
* Orders are NOT sent to a real venue; they go to the paper venue.
* `ledger.decisions` rows carry `row_type='shadow'` and are surfaced in the dashboard side-by-side with live decisions.
* `scripts/shadow_compare.py` computes daily summary: # orders proposed, hypothetical PnL, slippage assumption error vs the paper fill, max drawdown.

Promotion criteria (operator-tunable; defaults):
* >= 14 trading days of shadow.
* hypothetical Sharpe > 0.5.
* max shadow drawdown < risk.max_portfolio_drawdown_pct.
* parity oracle (`test_backtest_matches_live.py`) green on the same period.

Only then is the operator prompted to flip the one flag in `config/default.toml`. The flip itself is a single commit; `quanta_core.config.live_paper_toggle.assert_live_safety` runs at start-up and will refuse to boot live if any precondition is missing.

### 8.4 Test-time test fixtures (`tests/fixtures/`)

* `ohlcv/` — canned bars for replay tests, partitioned by symbol+timeframe.
* `ticks/` — canned tick streams (timestamped JSONL).
* `decisions/` — canned arbiter outputs so debate tests don't need a live LLM.

Fixtures are checked into git. Total fixture size budget: < 50 MB (use parquet for ohlcv).

### 8.5 What is NOT tested in CI

* Real venue connectivity — operator runs `scripts/health_check.sh` against sandbox manually.
* GPU performance — separate `scripts/bench_tft.py`, run on the DGX Spark host only.
* Long backtests — `scripts/replay_session.py --year 2024` runs nightly out-of-band.

---

## 9. What this design intentionally does NOT decide

Listed so the next pass / other agents can fill in:

* **Specific WS message schemas** — Alpaca and Coinbase publish their own; the adapters normalise to `Tick`/`Quote`/`Fill`.
* **Specific risk thresholds** — config-driven; today's defaults port verbatim.
* **Specific TFT hyperparameters** — port verbatim from `config.json[freqai.model_training_parameters]`.
* **Specific LoRA training schedule** — config-driven; default in §3.
* **Specific debate prompts** — `quanta_core/agents/prompts/*.md`; port verbatim from `stocks/shark/agents/`.
* **Slack / Telegram message formats** — port verbatim from existing notifiers.
* **Universe composition** — `config/universe.json` is the source of truth, identical convention to today.
* **Cron-job scheduling** — `scripts/install_crontab.sh` ports to the new paths; cron table is operations, not architecture.
* **Auth / RBAC for the dashboard** — bearer-token env var; spell-out in deployment doc, not here.

---

## 10. Migration ordering (for the executor)

This is the design doc, so only the order — not the work. The R6 executor agent will get its own plan in `docs/quanta-core-v4/07-EXECUTION_PLAN.md`.

1. Scaffolding: pyproject.toml, package skeletons, empty modules + tests, CI green.
2. `util/`, `config/`, `ledger/` (with sql migrations). Land first; nothing depends on its peers.
3. `exchanges/` (alpaca + coinbase + paper). Live behind a feature flag.
4. `models/registry.py` + `models/tft.py`. Validate parity vs Freqtrade's TFTModel on a held-out window.
5. `risk/` (governor + monte_carlo). Port + parity-test against `user_data/modules/risk_governor.py`.
6. `execution/` (engine + idempotency + slippage + audit). Standalone unit tests; integration with paper venue.
7. `strategy/base.py` + `strategy/mean_rev_tft.py`. Parity test against Freqtrade's signals on the same OHLCV window.
8. `live/` + `backtest/`. Run shadow against today's live deployment for 14 days.
9. `agents/` + `lora/`. Wire on top of working engine; reflector + LoRA run out-of-band first, then close the loop.
10. `observability/` (dashboard + metrics + tracing). Build to feature parity with today's UI; THEN flip cutover.

---

## 11. Open questions for the operator

These are non-blocking but need a decision before code starts:

1. **Wheel options orchestrator** — keep wheel cron-driven (`scripts/phases/`) or fold into the event loop as a Strategy? Recommendation: Strategy (single event loop, one ledger).
2. **Sentiment polling cadence** — today the 70B sentiment runs every 15 min. Keep that or move to an event-driven trigger (news event arrives → 8B classifies → escalate to 70B if borderline)? Recommendation: event-driven; saves GPU; described in §3.9 but not enforced here.
3. **Multi-account support** — design assumes one Alpaca + one Coinbase account. If we ever need multi-account, the adapter constructor signature already supports it (each adapter instance is one account). Cluster topology decision; not blocking.
4. **Deterministic LLM** — debate is not deterministic across temperature>0 runs, so the parity oracle exempts ArbiterDecisions and only diffs OrderProposals. If we want full determinism we must seed/cache LLM responses. Recommendation: cache in test fixtures only; accept non-determinism in live.

---

## 12. Glossary

* **Bar** — closed OHLCV at a timeframe boundary.
* **Tick** — single trade print (not a quote).
* **Quote** — best bid/ask snapshot.
* **Fill** — broker-confirmed execution slice.
* **Order proposal** — strategy-side intent before risk + execution.
* **Client order id** — operator-side deterministic UUID5 derived from intent.
* **Anchor** — daily starting equity / DD reference, persisted to disk.
* **Hot-swap LoRA** — replace adapter weights without unloading the base model.
* **Parity oracle** — the assertion that backtest ≡ live for the same input.
* **Shadow mode** — strategy runs against live data but submits to a paper venue.
* **Tombstone** — operator-dropped file that flips `kill_switch_engaged()` to True.

---

*End of `06-ARCHITECTURE.md`.*
