# 04 — RESEARCH: Exchange Connectivity (Drop CCXT + Freqtrade)

**Branch:** `feat/quanta-core-v4-design-r4`
**Date:** 2026-05-12
**Status:** Research, no code
**Audience:** quanta-core v4 design reviewers
**Scope:** Replace Freqtrade + CCXT with direct, official SDKs for Alpaca (stocks + options) and Coinbase Advanced (crypto), plus Polygon.io evaluation for options chain depth.

---

## 1. Executive Recommendation

**Adopt the following stack for v4:**

| Concern | Choice | Why |
|---|---|---|
| Stock + options execution + WebSocket | **alpaca-py** (official) | One SDK covers TradingClient, OptionTradingClient, all 4 DataStream classes (Stock/Crypto/News/Option). Paper and live differ only by `paper=True/False`. OPRA real-time options included in Algo Trader Plus ($99/mo). |
| Crypto execution + WebSocket | **coinbase-advanced-py** (official) | CDP API keys + JWT auth built-in; both sync and async (`*_async()`) variants for every channel; automatic reconnect with exponential backoff + subscription replay. |
| Options chain depth (Greeks / IV ticks) | **Alpaca OPRA feed first; Polygon.io Options Starter ($79/mo) only if Alpaca gaps appear** | Alpaca already streams OPRA trades+quotes; Polygon adds tick-level greeks + chain snapshot but costs an extra $79+/mo and duplicates execution-side data. Defer to Phase 2. |
| Async runtime | **AnyIO on the asyncio backend** | Same code runs on Trio or asyncio backends. Level-cancellation + structured task groups eliminate the cleanup bugs we hit in the Freqtrade exit path. Native compatibility with both alpaca-py and coinbase-advanced-py (both use `asyncio`). |
| WebSocket library | **`websockets` 16.x** (already a transitive dep of both SDKs) | Battle-tested, native ping/pong, native exponential backoff retry in v15+, RFC-7692 compression. |
| Idempotency | **`client_order_id` UUIDv7 namespaced per leg** (see §6) | Alpaca enforces uniqueness per account (HTTP 422 on duplicate); Coinbase accepts `client_order_id` and surfaces it on the user channel. UUIDv7 is monotonic-ish so we can sort + dedupe locally. |
| Order-update transport | **WebSocket-first; REST `GET /orders/{id}` only as reconciliation backstop every 60 s** | Both SDKs ship a dedicated order-events channel (Alpaca `trade_updates`, Coinbase `user`). REST polling is purely a safety net. |

**Why drop CCXT + Freqtrade:**

1. **No US-broker symmetry.** CCXT is crypto-only by design; Freqtrade is crypto-only. Anything stock/options has to be bolted on with a parallel stack — we already hit this and the result is brittle.
2. **Latency and signing overhead.** CCXT's pure-Python ECDSA signing was measured at 45 ms vs. <0.05 ms with `coincurve`; CCXT does support `orjson` and `coincurve` but neither is enabled by default. Freqtrade's WebSocket support is OHLCV-only and falls back to REST polling after 24 h on Binance — for our crypto pairs that's a known bug class.
3. **Trade-event fidelity.** Freqtrade fires `trade_open`/`trade_close` callbacks at strategy-tick boundaries, not on actual broker fills. Partial fills, replaces, and rejects are flattened. We want exchange-native fill events feeding a P&L ledger directly.
4. **Order-type surface.** Bracket / OCO / OTOCO / trailing-stop / extended-hours all exist natively on Alpaca but are exposed unevenly (or not at all) through CCXT-style unified APIs. We've been writing strategy-side workarounds; v4 deletes them.
5. **Single-event-loop.** Freqtrade owns its own event loop and per-pair worker threads. Trying to add a non-CCXT venue (e.g., Alpaca options) means living outside that loop, which is exactly the architecture we already regret.

---

## 2. SDK Comparison Table

| Capability | alpaca-py (v0.43.x, Py 3.8+) | coinbase-advanced-py (latest) | Polygon.io (python-client) | CCXT + Freqtrade (baseline) |
|---|---|---|---|---|
| Asset classes | Stocks, options, crypto, news | Spot crypto + perp + futures | Stocks, options, crypto, fx, indices (data only) | Crypto only (no stocks/options) |
| **Trading client** | `TradingClient(paper=True/False, url_override=...)` | `RESTClient(api_key, api_secret, rate_limit_headers=True)` | None (data-only) | Freqtrade exchange adapter + CCXT |
| **Paper / sandbox** | One bool flag (`paper=True`) — same SDK | Sandbox not really supported anymore (legacy Pro sandbox deprecated); use small live amounts | N/A (data-only) | Per-exchange ad hoc; many sandboxes broken |
| **Auth** | API key + secret, **or** OAuth2 token, **or** Basic, **or** `auth` message | **CDP API keys → JWT (ES256), 2-min expiry, regenerated per request** | API key in URL or header | Per-exchange |
| **Trading rate limit** | 200 calls/min/account (uniform across plans) | ~30 req/s private quota; rate-limit headers via `rate_limit_headers=True` | 5 req/s on starter, scales with tier | Inherits exchange's |
| **Market-data rate limit** | Free: 200 calls/min, 30 WS symbols. Algo Trader Plus ($99/mo): 10 000 calls/min, unlimited symbols | Same envelope as trading | Tier-dependent (5/100/unlimited req/s) | Inherits exchange's |
| **WebSocket** | `StockDataStream`, `CryptoDataStream`, `OptionDataStream` (msgpack), `NewsDataStream`, plus `TradingStream` for order events | `WSClient` (public/market) + `WSUserClient` (user). All channels have `_async` variant. | `wss://socket.polygon.io/{cluster}` — separate clusters per asset class | CCXT-Pro WebSocket (paid extension) or REST polling |
| **WebSocket auto-reconnect** | Internal reconnect loop with `ping_interval=10`, `ping_timeout=180`, `max_queue=1024`, indefinite retry | Built-in exponential backoff via `backoff` decorator, **max 5 retries**, auto resubscribe to prior channels (`_resubscribe`) | Manual; client offers `Reconnect=True` flag | Freqtrade wraps in retry loop, REST fallback after WS failure |
| **Sequence numbers / gap detection** | Not exposed; relies on TCP + WS ordering | **Yes** — every message has `sequence_num`; consumer must detect gaps | Polygon uses its own sequence on Q/T messages | Exchange-specific, mostly hidden |
| **Order types** | market, limit, stop, stop-limit, **trailing-stop**, **bracket**, **OCO**, **OTO**, **OTOCO** | market, limit IOC/GTC/GTD/FOK, **stop-limit GTC/GTD**, **trigger-bracket GTC/GTD** | N/A | Limited by CCXT unified API; per-exchange |
| **Multi-leg options** | **Level 3: spreads, straddles, condors, butterflies — up to 4 legs**, paper since 2024, live since 2025 | N/A | N/A | N/A |
| **Extended hours** | `extended_hours=True` flag (stocks only; **not options**) | N/A (crypto 24/7) | N/A | Per-exchange |
| **TIFs** | day, gtc, opg, cls, ioc, fok | IOC, GTC, GTD, FOK | N/A | per-exchange |
| **Fees: maker/taker** | $0 commission stocks; $0 commission options + per-contract reg fees (~$0.056/ctr) | 0.40% / 0.60% (Tier 1) → 0.00% / 0.05% (top tier). Stable pairs 0.00% / 0.001%. Stable-pair vol does NOT count toward tier | N/A | Per-exchange |
| **Greeks / IV** | Not in WS stream; available via REST snapshot endpoints | N/A | **Yes** — option chain snapshot includes Δ, Γ, Θ, V, IV; also via WS | None |
| **Idempotency key** | `client_order_id` (unique per account; **HTTP 422 on duplicate** — not Stripe-style replay-safe) | `client_order_id` (UUID-style; surfaces on user channel) | N/A | Per-exchange |
| **Python typing** | Pydantic-style request models (`MarketOrderRequest`, etc.) | Dataclasses + dot-notation responses | Typed responses | Loose dicts |
| **Async/sync** | Async streams, sync REST (use `asyncio.to_thread` or `httpx` directly for async REST) | **Both sync and async** REST + WS first-class | Both | Mixed |

---

## 3. Single-Event-Loop Async Architecture

We run **one** asyncio event loop on top of AnyIO task groups. Three venue connectors live inside one supervisor; each connector owns its WebSocket(s) and shares no state with siblings except through bounded channels.

```
                       ┌──────────────────────────────────────────────────────┐
                       │                  AnyIO Supervisor                    │
                       │           (single asyncio loop, structured)          │
                       │                                                      │
   ┌──────────────┐    │   ┌──────────────┐  ┌──────────────┐  ┌───────────┐  │   ┌───────────────┐
   │ alpaca-py    │◀──▶│   │ AlpacaConn   │  │ CoinbaseConn │  │ PolygonConn│  │──▶│ EventBus      │
   │ WS+REST      │    │   │  - stocks WS │  │  - market WS │  │  - opt WS  │  │   │ (anyio.Memory │
   └──────────────┘    │   │  - opts WS   │  │  - user  WS  │  │  - REST    │  │   │  ObjectStream)│
                       │   │  - trade_updt│  │  - REST      │  │            │  │   └───────┬───────┘
   ┌──────────────┐    │   │  - REST      │  └──────────────┘  └───────────┘  │           │
   │ coinbase-    │◀──▶│   └──────────────┘                                   │           ▼
   │ advanced-py  │    │           │              │              │            │   ┌───────────────┐
   │ WS+REST      │    │           ▼              ▼              ▼            │   │ Strategy      │
   └──────────────┘    │   ┌──────────────────────────────────────────────┐   │   │ + Risk        │
                       │   │   In-process router (per-symbol affinity)    │   │   │ + PnL ledger  │
   ┌──────────────┐    │   └──────────────────────────────────────────────┘   │   └───────┬───────┘
   │ polygon-api  │◀──▶│           │                                          │           │
   │ (optional)   │    │           ▼                                          │           ▼
   └──────────────┘    │   ┌──────────────────┐                              │   ┌───────────────┐
                       │   │ OrderRouter      │──── client_order_id ────────▶│   │ Order intent  │
                       │   │ (idempotent)     │                              │   │ queue         │
                       │   └──────────────────┘                              │   └───────────────┘
                       └──────────────────────────────────────────────────────┘
```

### Properties

- **One loop, three connectors, zero locks.** Each connector is an `async with anyio.create_task_group()` block under the supervisor. Cancelling the supervisor cancels every child cleanly — no orphans (this is the AnyIO/Trio level-cancellation guarantee, vs. asyncio's edge-cancellation which leaks tasks on shielded paths — the same class of bug that caused the well-known ChatGPT Redis outage).
- **Backpressure first.** All connector → strategy hops use `anyio.create_memory_object_stream(max_buffer_size=1024)`. If strategy stalls, sender's `send()` blocks instead of OOMing on a runaway queue (which is what alpaca-py's internal `max_queue=1024` already does at the SDK boundary).
- **Per-symbol ordering.** Inside one connector, the WS gives us monotonic ordering. The router preserves it by hashing symbol → consumer queue. **No cross-symbol ordering is promised** (and we don't need it).
- **Ordering across venues is explicitly NOT a guarantee.** Cross-venue arb is out of scope for v4.
- **Slow consumer policy.** If strategy is the slow consumer and the bounded channel fills, we drop oldest L2 updates (level2 is recoverable from next snapshot) but **never drop** order-event messages (those go on an unbounded channel because they are low-volume and load-bearing).

### Why AnyIO and not raw asyncio

- `asyncio.TaskGroup` (Py 3.11+) is the same idea but its cancel-scope API is narrow; `shield()` is famously hard to use right.
- Trio's nurseries pioneered the model; AnyIO gives us **portability across asyncio and trio backends** so we are not betting the system on one runtime.
- Both alpaca-py and coinbase-advanced-py use asyncio under the hood, so we run AnyIO with `backend="asyncio"` and stay native.

---

## 4. Reconnect + Gap-Recovery Pseudocode

The two venues differ in failure semantics, so we model both.

### 4.1 Coinbase (sequence-numbered, gap-aware)

```text
on_message(msg):
    expected = state.last_seq[msg.product_id] + 1
    if msg.sequence_num > expected:
        # Gap. We are missing (msg.sequence_num - expected) messages.
        state.gap_count++
        if msg.channel == "level2":
            # level2 guarantees delivery, so a gap means we lost frames —
            # discard local book, re-subscribe to force fresh snapshot.
            await unsubscribe("level2", [msg.product_id])
            await subscribe("level2", [msg.product_id])  # new snapshot follows
            return
        elif msg.channel in ("ticker", "market_trades"):
            # Lossy by design. Log and continue with the message in hand.
            emit_gap_metric(msg.product_id, gap=msg.sequence_num - expected)
        elif msg.channel == "user":
            # Order events lost. Reconcile via REST GET /orders?limit=N.
            await reconcile_open_orders()
    elif msg.sequence_num < expected:
        # Out-of-order/duplicate. Ignore.
        return
    state.last_seq[msg.product_id] = msg.sequence_num
    dispatch(msg)

on_disconnect():
    backoff = initial_delay  # 1 s
    while not connected:
        try:
            await connect()
            await replay_subscriptions(state.subscriptions)   # SDK does this
            await reconcile_open_orders()                     # we add this
            await fetch_account_snapshot()                    # we add this
            backoff = initial_delay
        except Exception:
            jitter = random(0, 0.25 * backoff)
            await sleep(min(backoff + jitter, max_delay=30))
            backoff *= 2
```

**Notes**
- The SDK's built-in `@backoff.on_exception(backoff.expo, ..., max_tries=5)` is *too short* for a 24/7 production bot. We wrap it: catch the final raise, log, then enter our own indefinite loop with capped exponential backoff + 0-25% jitter (prevents thundering herd if every shard's WS dies at once).
- Heartbeats: subscribe to `heartbeats` channel alongside any other subscriptions — Coinbase closes idle subs within 60-90 s.
- JWT expires every 2 minutes; the SDK refreshes automatically. If we hand-roll, refresh at the 90-s mark.

### 4.2 Alpaca (no sequence numbers, rely on connection state + REST reconcile)

```text
on_connect():
    if first_connect:
        await auth({"key": KEY, "secret": SECRET})         # or OAuth
        await send({"action": "listen", "data": {"streams": ["trade_updates"]}})
        await subscribe(market_streams, symbols)
    else:
        await auth(...)
        await replay_subscriptions(state.subscriptions)
        await reconcile_open_orders()    # GET /v2/orders?status=open
        await reconcile_positions()      # GET /v2/positions
        await fetch_account()            # GET /v2/account

on_message(msg):
    if msg.stream == "trade_updates":
        event = msg.data.event  # new | fill | partial_fill | canceled | replaced | rejected | expired
        update_local_order_state(event)
        if event in ("fill", "partial_fill"):
            update_pnl_ledger(msg.data)
    dispatch(msg)

on_disconnect():
    # mirrors §4.1 — exponential backoff with jitter, indefinite,
    # cap at 30 s, never give up
```

**Notes**
- Alpaca does *not* expose sequence numbers, so we use a **dual-stream reconciliation**: trust `trade_updates` for live state, run a 60-s tick that does `GET /v2/orders?status=open&after=last_check` and warns on divergence. Differences point to a bug — alert, do not auto-correct.
- Trade-update WebSocket uses **binary frames** on `/stream`; the SDK handles this but a hand-rolled client would need to flag it.
- `partial_fill` events carry per-fill `qty`, `price`, and `position_qty` (post-fill position size). Use those for the per-fill fee/P&L entry (see §7).

### 4.3 Generic reconnect (both venues share this skeleton)

```text
async def supervised_ws(venue, build_client, on_message):
    backoff = 1
    while not shutdown:
        async with anyio.create_task_group() as tg:
            try:
                client = await build_client()
                tg.start_soon(client.run, on_message)
                tg.start_soon(heartbeat_watchdog, client)  # kills connection if no msg in 30s
                backoff = 1                                 # success path
            except CancelledError:
                raise                                       # shutdown
            except Exception as e:
                log.warning("WS %s lost: %s", venue, e)
        await anyio.sleep(min(backoff + random(0, 0.25*backoff), 30))
        backoff *= 2
```

---

## 5. Order-Type Capability Matrix

| Order type | Alpaca stocks | Alpaca options | Coinbase crypto | Notes |
|---|---|---|---|---|
| Market | Yes | Yes | Yes (market_market_ioc) | Coinbase markets are effectively IOC |
| Limit | Yes | Yes | Yes (GTC / GTD / IOC / FOK) | |
| Stop | Yes | No | No | Coinbase has stop-limit, not pure stop |
| Stop-limit | Yes | Yes | Yes (GTC / GTD) | |
| Trailing-stop | Yes (`trail_price` or `trail_percent`, DAY/GTC only) | No | No | |
| Bracket (parent + TP + SL) | Yes | No | Yes (trigger_bracket GTC/GTD) | Coinbase bracket is single-leg; Alpaca is OTOCO |
| OCO (exit pair) | Yes | No | No (use trigger_bracket) | |
| OTO (one triggers other) | Yes | No | No | |
| TIF: DAY | Yes | Yes | N/A | |
| TIF: GTC | Yes | Yes | Yes | |
| TIF: GTD | No | No | Yes | Coinbase only |
| TIF: OPG (at-open) | Yes | No | N/A | |
| TIF: CLS (at-close) | Yes | No | N/A | |
| TIF: IOC | Yes | No | Yes | |
| TIF: FOK | Yes | No | Yes | |
| Extended hours | Yes (limit + DAY/GTC + `extended_hours=True`) | **No** | N/A | |
| Multi-leg (up to 4) | N/A | Yes (Level 3, paper since 2024-12, live since 2025) | No | |
| Fractional sizes | Yes (stocks) | No | Yes (crypto natural) | |
| Replace / modify | Yes | Yes | Yes (`edit_order`, with `preview_edit_order` for dry-run) | |
| Cancel-all | Yes | Yes | Yes (`cancel_orders`) | |
| Close position | Yes (`DELETE /v2/positions/{symbol}`) | Yes | Yes (`close_position`) | |
| Exercise / DNE | N/A | Yes (`POST /v2/positions/{symbol}/exercise`) | N/A | |

**Strategy guard-rail:** the v4 strategy layer must declare every order type it uses; the OrderRouter rejects any type the venue does not support and logs `UNSUPPORTED_ORDER_TYPE` instead of silently degrading. The v3/Freqtrade silently-degrade behaviour was the root cause of two of last week's misfires.

---

## 6. Idempotency Strategy

### 6.1 Constraint

- Alpaca: `client_order_id` must be unique per account; second submission returns **HTTP 422** ("client_order_id must be unique"). It is **not** a Stripe-style replay-safe key — duplicate request does not return the original order; it errors.
- Coinbase: `client_order_id` (a.k.a. `client_oid`) must be a UUID-ish string; duplicates also rejected.
- Neither venue offers a "create-or-return-existing" idempotency header.

### 6.2 Implication

Retry safety must be **client-side**: before resubmitting, we must distinguish "the network ate my response" from "the broker never saw the order". The procedure:

```text
submit(intent):
    coid = build_client_order_id(intent)        # deterministic — see schema below
    try:
        resp = POST /orders {client_order_id: coid, ...}
        return resp
    except NetworkError:
        # Don't know if broker received. DO NOT generate a new coid.
        for attempt in 1..5:
            await sleep(backoff(attempt))
            existing = GET /orders?client_order_id=coid     # Alpaca: /v2/orders:by_client_order_id
                                                            # Coinbase: list_orders(client_order_id=coid)
            if existing: return existing                    # broker has it; we're good
            try: return POST /orders {client_order_id: coid, ...}
            except DuplicateClientOrderId:                  # 422 from Alpaca, similar on CB
                continue                                    # next loop iteration looks up
        raise SubmitFailedAfterRetries
```

This makes the operation idempotent against **transient network errors** even though neither broker is idempotent natively.

### 6.3 `client_order_id` schema

We compose a 36-char deterministic ID per leg:

```
{prefix}-{venue}-{strategy_id}-{leg_uuidv7}

prefix      = "qc4"                       (Quanta Core v4 — identifies the system)
venue       = "alp" | "cbx" | "alpo"      (alp=alpaca stocks, alpo=alpaca options, cbx=coinbase)
strategy_id = lowercase ASCII, ≤ 8 chars  (e.g., "wheel", "mr01", "tftblnd")
leg_uuidv7  = 32-char hex (or 36 with dashes) — UUIDv7 so it is monotonic-ish for log sort
```

Example: `qc4-alpo-wheel-018f2d1c9f4a7b6e9d0c1a2b3c4d5e6f`

**Properties**

- **Unique** by construction (UUIDv7).
- **Searchable** — `prefix` lets us list every v4 order; `strategy_id` segments per strategy; `venue` aids cross-broker dedupe in our PnL ledger.
- **Sortable** — UUIDv7 leading 48 bits are millisecond timestamps, so `sort_by(coid)` ≈ chronological.
- **Stable on retry** — built from `(strategy_id, leg_intent_hash, intent_timestamp_floor_seconds)`. The same intent within the same second produces the same coid → safe to retry; intent two seconds later is a fresh order.
- **Bracket-aware** — for OTOCO we generate one parent coid and derive child coids via `parent + "-tp"` / `parent + "-sl"` (Alpaca lets you set them per leg).

---

## 7. Fee Handling and the P&L Ledger

### 7.1 What the venues report

**Alpaca** charges $0 commission on stocks and options but passes through:

| Fee | Side | Amount (per Alpaca disclosure) |
|---|---|---|
| TAF (FINRA) | Sells only | $0.000166 / share equities; $0.00329 / contract options |
| ORF (Options Reg Fee) | Buy + sell | ~$0.02295 / contract |
| OCC clearing | Buy + sell, capped 2 750 ctr | $0.025 / contract |
| CAT (FINRA-CAT) | Buy + sell | tiny, varies |
| SEC Section 31 | Sells only equities | rate-set per FY |

Total options regulatory cost ≈ **$0.056 per contract round-trip**.

**Alpaca aggregates fees end-of-day** (per the regulatory-fees doc: "Alpaca's trading system keeps track of the accrued FEE amounts intraday and deducts the pending amounts from account balances", charged EOD). The `trade_updates` partial-fill event therefore does **not** carry the regulatory fee field per fill — fees show up later in `account/activities` (TAF/ORF/OCC codes).

**Coinbase Advanced** charges maker/taker:

| 30-d vol | Maker | Taker | Stable pairs maker/taker |
|---|---|---|---|
| <$10 k (Tier 1) | 0.40% | 0.60% | 0.00% / 0.001% |
| $10 k-50 k (Tier 2) | 0.25% | 0.40% | 0.00% / 0.001% |
| $50 k-100 k | 0.20% | 0.35% | 0.00% / 0.001% |
| … | … | … | … |
| >$250 M | 0.00% | 0.05% | 0.00% / 0.001% |

Stable-pair volume does NOT count toward tier progression. Fees are **per fill, surfaced inline** on the `user` WS channel: each `fills` event carries `commission`, `total_fees`, `total_value_after_fees`. Partially filled orders pay taker rate on the immediate fill and maker rate on rested portions later matched — i.e. **fee per fill, not per order**.

### 7.2 Design implication for our P&L ledger

The ledger must support **two ingestion modes**:

1. **Per-fill ingestion (Coinbase, and Alpaca trade economics)** — every `fill` / `partial_fill` event lands one row in `pnl_ledger` with `gross_qty`, `gross_value`, and `fee` (zero for Alpaca live; the venue-reported `commission` for Coinbase).
2. **End-of-day fee accrual (Alpaca regulatory)** — a 16:05 ET nightly job pulls `GET /v2/account/activities?activity_types=TAF,ORF,OCC,CAT,FINRACAT,SECFEE` and reconciles those into the ledger as separate `regulatory_fee` rows linked to the originating fill by symbol + side + date.

The strategy module **must read net P&L** (gross − fees − reg_fees) when computing exit / risk decisions; using gross only is the bug we hit on wheel rolls last week.

### 7.3 Trade-event normalization

We define one normalized `FillEvent` for the strategy layer:

```
FillEvent {
    venue: "alpaca" | "coinbase"
    asset_class: "stock" | "option" | "crypto"
    symbol: str
    order_id: str         # broker side
    client_order_id: str  # ours
    qty: Decimal          # signed: + for buy, - for sell (or 'side' field)
    price: Decimal
    fee: Decimal          # venue-reported fee on THIS fill (0 for Alpaca, ≥0 for Coinbase)
    fee_accrued_later: bool   # true for Alpaca — flag for the nightly reconciler
    position_qty: Decimal     # post-fill (Alpaca gives this; Coinbase computed)
    ts: datetime
    raw: dict             # full WS payload for forensic replay
}
```

Both connectors emit `FillEvent`. The strategy layer never reads broker-specific schemas.

---

## 8. Migration Path FROM CCXT / Freqtrade

### 8.1 What we lose

- **Multi-exchange unification.** CCXT made it trivial to add a 4th crypto venue. With native SDKs we'd write a new connector each time. Acceptable trade-off given that v4 is locked to one US-broker (Alpaca) + one US-crypto venue (Coinbase) by regulatory requirement.
- **Freqtrade's built-in features:** strategy hot-reload, web UI, backtest engine, hyperopt, freqtradeUI dashboard. We replace with our own dashboard (already in progress) and decouple backtesting (already running outside Freqtrade in `user_data/scripts/`).
- **Freqtrade telegram bot.** Replace with our own thin notifier reading the same EventBus.
- **The `dry_run` mode.** Replace with alpaca-py's `paper=True` for stocks/options and Coinbase sandbox-style small-size live for crypto (Coinbase Advanced no longer has a true sandbox).
- **CCXT's unified market-loading.** Replace with one symbol-universe file (`universe.json` — already exists).

### 8.2 What we gain

| Gain | Concrete impact |
|---|---|
| **Native trade-fill events** | P&L ledger updates in <100 ms instead of strategy-tick-bounded |
| **Native multi-leg options** | Iron condors / vertical spreads as one bracket order, not 4 sequential legs (we lose the partial-fill leg-imbalance risk) |
| **Single event loop** | One stack trace on crash. No more "which Freqtrade thread blew up?" |
| **Idempotent retries** | The "double-fired BCH order" class of bug is impossible by construction |
| **OPRA real-time options for $99/mo** | Greeks stale-by-15-minutes problem disappears |
| **Bracket / OCO / trailing-stop native** | Removes strategy-side state machines we wrote to emulate them |
| **Sequence-numbered crypto book** | We can finally detect dropped L2 frames |
| **No 24-h Binance disconnect class** | Doesn't apply (we are on Coinbase) but the general "WS dies silently and falls to REST" pattern is gone — alpaca-py and coinbase-advanced-py both ping every 10 s |
| **Typed request/response** | Pydantic models in alpaca-py, dataclasses in coinbase-advanced-py — IDE catches field typos at edit time |

### 8.3 Migration steps (one-way; no shim layer)

1. **Universe re-anchor.** `universe.json` already canonical. Map each symbol to `(venue, asset_class)` once.
2. **Connectors.** Implement `AlpacaConn`, `CoinbaseConn`, optionally `PolygonConn`. Each is an AnyIO task group with one or two WS connections + REST client.
3. **EventBus + FillEvent normalization.** Single in-process `anyio.MemoryObjectStream` from connectors to strategy.
4. **OrderRouter.** Single async router; idempotent `submit(intent)` per §6.
5. **PnL ledger.** Two-mode ingestion per §7.2.
6. **Strategy layer port.** Rewrite each Freqtrade strategy as an async coroutine consuming `FillEvent` / `MarketEvent`. The TFT, BollingerRSI MR, and wheel strategies all become small files.
7. **Risk gates.** Same YAML config moves over; readers swap from Freqtrade's `dataframe.iloc[-1]` style to subscribing on the EventBus.
8. **Backtest harness.** Already separate; only the data adapter changes (read from `alpaca-py historical` clients instead of Freqtrade's `download_data`).
9. **Cutover.** Run v4 paper alongside Freqtrade for 1 week; compare daily fill logs + P&L; cut over when diff is structural-only (slippage timing, not strategy logic). Then *delete* Freqtrade.
10. **Decommission.** Remove Freqtrade Docker service, freqUI service, CCXT package, all wrappers. Net code reduction estimated 35-45% (Freqtrade is heavy).

---

## 9. Build Cost Estimate

Person-hours, solo developer, Claude-assisted, **excluding** strategy-logic rewrites (those are unavoidable either way).

| Component | Hours | Notes |
|---|---|---|
| AnyIO supervisor + task groups + shutdown handling | 8 | Reusable; vendor-neutral |
| `AlpacaConn` (stocks + options WS + REST + trade_updates) | 16 | Includes reconcile loop, OPRA feed wiring |
| `CoinbaseConn` (market + user WS, REST, JWT refresh) | 16 | Includes sequence-num gap detection per channel |
| OrderRouter + idempotent submit + replay-safe retry | 10 | Tested against fault-injected network errors |
| `FillEvent` normalizer + PnL ledger ingestion (two-mode) | 8 | EOD reconciler script + WS path |
| Multi-leg options helper (build Alpaca OTOCO from intent) | 6 | Spreads/condors/butterflies |
| `client_order_id` scheme + look-up-on-retry helper | 4 | |
| Universe → (venue, asset_class) router | 2 | Reads existing `universe.json` |
| Test rig: in-process fake WS + fault-injection harness | 12 | |
| Strategy ports (TFT, BollingerRSI MR, wheel, NFI X6) | 24 | One day each — they're all small |
| Backtest data-adapter swap (Freqtrade → alpaca-py historical) | 6 | |
| Dashboard wiring (replace freqUI panels) | 12 | Reuses existing dashboard |
| Paper run-in + reconciliation + cutover | 16 | 1 calendar week elapsed, 16 active hours |
| Decommission (delete Freqtrade, CCXT, wrappers) | 4 | |
| **Total** | **144 h** | ≈ 3.5 dev-weeks (40 h/wk) or 6 calendar weeks at 24 h/wk |

**Risk-adjusted estimate:** add 30% for surprises → **~190 h, ~5 dev-weeks** at full-time.

**Avoided cost (sunk in Freqtrade today):** roughly 200 h of accumulated patches + a maintenance overhead of ~4 h/wk for upstream Freqtrade chasing. Migration breaks even in **~12 months** even if v4 needs another 50 h of post-cutover fixes — and probably much sooner once the bug classes listed in §8.2 stop happening.

**External costs (USD/mo):**

| Line item | Cost |
|---|---|
| Alpaca Algo Trader Plus (OPRA + unlimited WS) | $99 |
| Coinbase Advanced (no SDK cost) | $0 |
| Polygon Options Starter (optional, Phase 2) | $79 |
| **Total monthly** | **$99–$178** |

No infra change vs. today (we already pay $0 for Freqtrade + spot rate-limited Alpaca free tier; the +$99/mo is the real-time OPRA unlock that we want anyway for the options pilot).

---

## 10. Sources

1. [alpacahq/alpaca-py — Official Python SDK (GitHub)](https://github.com/alpacahq/alpaca-py)
2. [Alpaca-py TradingClient API reference](https://alpaca.markets/sdks/python/api_reference/trading/trading-client.html)
3. [Alpaca Docs — Working with /orders (order types, TIFs, OTOCO, OCO)](https://docs.alpaca.markets/docs/working-with-orders)
4. [Alpaca Docs — Options Trading API](https://docs.alpaca.markets/docs/options-trading)
5. [Alpaca Docs — Options Trading Overview (approval levels)](https://docs.alpaca.markets/docs/options-trading-overview)
6. [Alpaca Docs — Real-time Option Data (OPRA vs indicative feed)](https://docs.alpaca.markets/docs/real-time-option-data)
7. [Alpaca Docs — Streaming Market Data WebSocket](https://docs.alpaca.markets/docs/streaming-market-data)
8. [Alpaca Docs — Trading WebSocket (trade_updates, partial fills)](https://docs.alpaca.markets/docs/websocket-streaming)
9. [Alpaca Docs — Market Data API plans (Free vs Algo Trader Plus)](https://docs.alpaca.markets/us/docs/about-market-data-api)
10. [Alpaca Docs — Regulatory Fees (TAF/ORF/OCC/CAT)](https://docs.alpaca.markets/docs/regulatory-fees)
11. [Alpaca Support — API rate limit (200/min/account)](https://alpaca.markets/support/usage-limit-api-calls)
12. [Alpaca Forum — 429 rate-limit + clarifications](https://forum.alpaca.markets/t/429-rate-limit-exceeded-when-creating-orders/14120)
13. [Alpaca Forum — Idempotency on order create + client_order_id 422 behavior](https://forum.alpaca.markets/t/idempotency-on-order-create/15801)
14. [Alpaca-py DataStream implementation (ping_interval=10, ping_timeout=180)](https://github.com/alpacahq/alpaca-py/blob/master/alpaca/data/live/websocket.py)
15. [coinbase/coinbase-advanced-py — Official SDK (GitHub)](https://github.com/coinbase/coinbase-advanced-py)
16. [Coinbase Advanced Trade SDK docs (REST + WS reference)](https://coinbase.github.io/coinbase-advanced-py/)
17. [Coinbase CDP — Advanced Trade WebSocket Channels (level2, user, sequence_num)](https://docs.cdp.coinbase.com/coinbase-app/advanced-trade-apis/websocket/websocket-channels)
18. [Coinbase CDP — WebSocket Setup & Authentication (JWT, 2-min expiry)](https://docs.cdp.coinbase.com/coinbase-app/advanced-trade-apis/guides/websocket)
19. [Coinbase CDP — Listen for Order Updates via SDK WebSocket](https://docs.cdp.coinbase.com/coinbase-app/advanced-trade-apis/guides/sdk-websocket)
20. [coinbase-advanced-py WebSocket base implementation (exp backoff, max_tries=5, _resubscribe)](https://github.com/coinbase/coinbase-advanced-py/blob/master/coinbase/websocket/websocket_base.py)
21. [Coinbase Advanced Trade Fee Tiers (maker/taker, stable pairs)](https://help.coinbase.com/en/coinbase/trading-and-funding/advanced-trade/advanced-trade-fees)
22. [Polygon.io — Options market data overview](https://polygon.io/options)
23. [Polygon.io — Option Chain Snapshot endpoint (Greeks + IV)](https://massive.com/docs/rest/options/snapshots/option-chain-snapshot)
24. [FlashAlpha — Best Options Data APIs 2026 (pricing comparison)](https://flashalpha.com/articles/best-options-data-apis-2026)
25. [AnyIO — Why use AnyIO over asyncio (structured concurrency, level cancellation)](https://anyio.readthedocs.io/en/stable/why.html)
26. [Trio documentation (nurseries, cancellation model)](https://trio.readthedocs.io/)
27. [Python asyncio TaskGroup (Py 3.11+) — discussion of structured concurrency adoption](https://discuss.python.org/t/adopt-proven-anyio-trio-patterns-natively-into-asyncio-multi-release-roadmap/106067)
28. [websockets (python) library — client reference (built-in retry)](https://websockets.readthedocs.io/en/stable/reference/asyncio/client.html)
29. [Robust WebSocket reconnection w/ exponential backoff + jitter (DEV)](https://dev.to/hexshift/robust-websocket-reconnection-strategies-in-javascript-with-exponential-backoff-40n1)
30. [Freqtrade GitHub issue: websocket fallback + 24-h Binance disconnect](https://github.com/freqtrade/freqtrade/issues/11821)
31. [CCXT.Pro (paid WebSocket extension) + signing-overhead discussion](https://github.com/ccxt/ccxt)

(31 sources cited; minimum was 12.)

---

## Appendix A — Open questions for design review

1. Do we want to keep the option to read **Polygon options chain** for Greeks the moment we go to live options trading, or stay Alpaca-only until we hit a real gap?
2. AnyIO vs. raw asyncio.TaskGroup — is portability across trio worth a small extra abstraction layer? (My take: yes, because it gives us better cancellation semantics regardless of backend.)
3. Coinbase has no real sandbox anymore. Do we cut over crypto by running very small live size for 1 week, or do we accept paper-only for crypto and validate via order shape only?
4. Should the OrderRouter expose a synchronous "blocking until ack" mode for backtests, or always async?
5. Do we lift `trade_updates` reconcile to 60 s, or push it to 30 s? (Lower is safer, more REST traffic against the 200/min cap.)
