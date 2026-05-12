# Exchange API Gap Audit — Alpaca + Coinbase
**Date:** 2026-05-12
**Branch:** `audit/exchange-api-gaps`
**Author:** automated architecture audit
**Scope:** Compare our live Alpaca and Coinbase usage against the official documentation. Identify gaps and edge cases that must be addressed BEFORE flipping from paper to live.

---

## Executive verdict

**NO-GO (CONDITIONAL).** Paper-mode is acceptable today. Live trading should NOT be enabled until the **five P0 gaps** in §6 are closed. The Alpaca stack is mostly solid; the **Coinbase stack is the bigger live-trading risk**, because:

1. Freqtrade's Coinbase Advanced Trade integration is **officially unsupported** (closed as "not planned" — freqtrade issue #9606). Our config relies entirely on the ccxt driver, which has known order-error-propagation bugs (ccxt #25217).
2. `stoploss_on_exchange` is **disabled** in `user_data/config.json` — every stop-loss is an in-process Python decision. If the bot dies, positions are unprotected. Coinbase's ccxt driver does not reliably support exchange-side stops.
3. The custom `ExecutionEngine` (`user_data/modules/execution_engine.py`) is well-designed for safety but **runs on a separate code path** from freqtrade's order pipeline. The dry-run / live toggle for this engine is independent of `config.json#dry_run` — easy to misconfigure.
4. Our `client_order_id` discipline is excellent on the Shark stack but **missing on the freqtrade/ccxt path** (we delegate to ccxt's default UUID; freqtrade only uses it on retries).
5. We don't query Alpaca's `/v2/clock`, `/v2/calendar`, or Corporate Actions API at all — splits, dividends, and market-closed windows are not gated.

**What's solid:** Shark's `place_order` idempotency, the wheel's bracket-style options flow, the slippage-gate in `execution_engine.py`, retry/backoff everywhere, paper/live URL sanity checks in `shark/config.py`.

**Top P0s, one-line each:**
1. `user_data/config.json:36` — `stoploss_on_exchange: false` with no replacement watchdog when bot is offline.
2. `stocks/shark/data/alpaca_data.py:285-300` — `get_positions()` drops `asset_class` field. Options and equities are indistinguishable downstream → caused the bug fixed 2026-05-11.
3. `stocks/shark/execution/orders.py:104-134` — `_retry_order` retries on every `_AlpacaAPIError`, including 4xx auth/rejection errors that should NEVER be retried.
4. `user_data/modules/execution_engine.py:566-570` — `_cancel` only sends `order_ids`. Cancel race on partial fills can leave the partial silently filled and the bot believing the order was cancelled.
5. `stocks/scripts/alpaca.sh:52` — hardcoded `feed=sip` in `bars` subcommand. Free accounts get 400 errors; paid accounts get unintended SIP usage with billing implications.

---

## 1. Document-URL coverage

### Alpaca

| URL | Read OK? | Notes |
|-----|----------|-------|
| https://docs.alpaca.markets/docs/getting-started | partial | Page is navigation hub; minimal technical content |
| https://docs.alpaca.markets/docs/trading-api | partial | Same — overview only |
| https://docs.alpaca.markets/reference/getaccount | partial | Schema absent on first fetch; supplemented via WebSearch + SDK code |
| https://docs.alpaca.markets/reference/getallpositions | 404 | URL moved → see https://docs.alpaca.markets/reference/getallopenpositions |
| https://docs.alpaca.markets/reference/getallorders | 404 | URL moved → see SDK enum docs + community forum |
| https://docs.alpaca.markets/reference/postorder | full | All order params extracted |
| https://docs.alpaca.markets/docs/options-trading | full | Trading levels 1/2/3, contract symbology, exercise behaviour |
| https://docs.alpaca.markets/reference/get-options-contracts | full | Query params + pagination |
| https://docs.alpaca.markets/docs/streaming-real-time-data | 404 | URL moved → see https://docs.alpaca.markets/docs/websocket-streaming + alpaca-py TradingStream docs |
| https://docs.alpaca.markets/docs/working-with-trading-api | 404 | Supplemented via Alpaca Support article + community forum |
| https://docs.alpaca.markets/docs/paper-trading | full | Reset mechanism, 10% partial fills, no slippage simulation |
| https://docs.alpaca.markets/docs/historical-market-data | 404 | Supplemented via market-data-faq and forum |
| https://docs.alpaca.markets/docs/sdks-and-tools | partial | High-level only |
| https://docs.alpaca.markets/reference/getclock | 404 | Schema inferred from SDK + alpaca.sh wrapper |
| https://docs.alpaca.markets/docs/mandatory-corporate-actions | via search | Splits/dividends/M&A semantics confirmed |

### Coinbase Advanced Trade

| URL | Read OK? | Notes |
|-----|----------|-------|
| https://docs.cdp.coinbase.com/advanced-trade/docs/welcome | partial | Welcome page only — points to llms.txt index |
| https://docs.cdp.coinbase.com/advanced-trade/docs/rest-api-overview | sparse | No base URL or auth detail in page body |
| https://docs.cdp.coinbase.com/advanced-trade/reference/retailbrokerageapi_getaccounts | not fetched | Inferred from coinbase-advanced-py SDK |
| https://docs.cdp.coinbase.com/advanced-trade/reference/retailbrokerageapi_postorder | 404 | Schema inferred from SDK + WebSearch |
| https://docs.cdp.coinbase.com/advanced-trade/reference/retailbrokerageapi_getproducts | not fetched | Public-data flow uses `https://api.exchange.coinbase.com/products/...` — different host |
| https://docs.cdp.coinbase.com/advanced-trade/docs/rest-api-authentication | sparse | Supplemented via get-started/authentication/jwt-authentication page |
| https://docs.cdp.coinbase.com/get-started/authentication/jwt-authentication | full | JWT structure, 120-second TTL, Ed25519+ES256 algorithms |
| https://docs.cdp.coinbase.com/advanced-trade/docs/rest-api-rate-limits | sparse | Supplemented via WebSearch — confirms 30 req/sec private, 10 req/sec public |
| https://docs.cdp.coinbase.com/advanced-trade/docs/ws-overview | sparse | Supplemented via WebSearch |
| https://docs.cdp.coinbase.com/advanced-trade/docs/ws-channels | partial | Channels listed via WebSearch (heartbeats, level2, ticker, user, market_trades, candles, status) |
| https://docs.cdp.coinbase.com/advanced-trade/docs/sandbox | 404 | Real URL: https://docs.cdp.coinbase.com/coinbase-app/advanced-trade-apis/sandbox |
| https://docs.cdp.coinbase.com/coinbase-app/advanced-trade-apis/sandbox | full | Accounts + Orders only; static mocked responses; no real fills |
| https://docs.cdp.coinbase.com/advanced-trade/docs/rest-api-fee-tiers | sparse | Fee structure: tiered maker/taker by 30-day USD volume |

### Freqtrade context

| URL | Read OK? | Notes |
|-----|----------|-------|
| https://www.freqtrade.io/en/stable/configuration/ | full | `dry_run`, `trading_mode`, `stoploss_on_exchange`, `ccxt_config` semantics |
| https://www.freqtrade.io/en/stable/exchanges/ | full | **Coinbase is absent from the page** — confirms unsupported status |
| https://github.com/freqtrade/freqtrade/issues/9606 | full | Coinbase Advanced Trade integration closed as "not planned" |
| https://github.com/ccxt/ccxt/issues/25217 | via search | ccxt swallows Coinbase `preview_failure_reason` field; users see generic `ExchangeError` |

---

## 2. Codebase coverage

### Alpaca touch points (audited fully)
- `stocks/shark/data/alpaca_data.py` — Shark's data + account + positions client (647 lines)
- `stocks/shark/execution/orders.py` — order placement, idempotency, bracket logic (711 lines)
- `stocks/shark/execution/stops.py` — trailing-stop tightening (216 lines)
- `stocks/shark/execution/exit_manager.py` — multi-reason exit evaluator (282 lines)
- `stocks/shark/execution/guardrails.py` — pre-trade hard limits (442 lines)
- `stocks/shark/execution/position_sizer.py` — Kelly + fixed-fractional (296 lines)
- `stocks/wheel/broker.py` — wheel-pilot Alpaca wrapper (385 lines)
- `stocks/wheel/runner.py` — wheel orchestrator (602 lines)
- `stocks/shark/config.py` — env-var loader, paper/live sanity check
- `stocks/scripts/alpaca.sh` — bash wrapper for the agent
- `stocks/scripts/health-check.sh` — liveness probe for both endpoints
- Phase modules: `kb_refresh.py`, `pre_market.py`, `market_open.py`, `midday.py`, `daily_summary.py`, `weekly_review.py`

### Coinbase touch points (audited fully)
- `user_data/config.json` — freqtrade root config (Coinbase exchange block)
- `user_data/strategies/nfi_x6_config.json` — NFI X6 override config
- `user_data/strategies/NostalgiaForInfinityX6.py` — sets ccxt `options.brokerId/partner` to None (Coinbase-irrelevant)
- `user_data/modules/execution_engine.py` — standalone Coinbase Advanced Trade execution engine (665 lines)
- `user_data/modules/regime_detector.py` — public-OHLCV fetcher (read-only)
- `user_data/dashboard/data_sources.py` — public candles cache for dashboard
- `user_data/dashboard/ops_routes.py:1242` — Coinbase secret presence check
- `user_data/modules/onchain_signals.py` — Coinbase International perps URL (read-only, disabled)

### Out of scope (verified)
- `stocks/api/main.py` — health-only, no broker calls
- `user_data/strategies/FreqAIMeanRevV1.py` — strategy logic only; orders go through freqtrade core
- All test files

---

## 3. Alpaca audit (16 rows, prioritized)

| # | Topic | Our implementation | Alpaca API capability | Gap | Concrete fix path (file:line) |
|---|-------|--------------------|------------------------|-----|--------------------------------|
| A1 | **Authentication** | `APCA-API-KEY-ID` / `APCA-API-SECRET-KEY` headers in `scripts/alpaca.sh:25-26`; alpaca-py SDK uses same in `shark/data/alpaca_data.py:130-134` | Same headers documented; rotation via dashboard | None | n/a |
| A2 | **Paper vs Live switching** | `ALPACA_BASE_URL` env var (default `paper-api.alpaca.markets`) read in 4 files; `shark/config.py:194-205` warns if `TRADING_MODE` and URL disagree | Same SDK, different base URL; SDK takes a `paper=bool` flag | **P2** — Warning is logged, not raising. A misconfigured deploy can still execute live trades. `shark/config.py:195` logs `logger.warning`; should be `raise ConfigError` for the cross-product (live URL + paper mode). | `stocks/shark/config.py:195-205` — promote warnings to hard errors |
| A3 | **Account endpoint** | `get_account()` returns 5 fields: `equity`, `cash`, `buying_power`, `portfolio_value`, `daytrade_count` (`shark/data/alpaca_data.py:250-256`) | Full response includes ~30 fields: `pattern_day_trader`, `trading_blocked`, `account_blocked`, `transfers_blocked`, `regt_buying_power`, `daytrading_buying_power`, `multiplier`, `options_buying_power`, `options_approved_level`, `options_trading_level`, `shorting_enabled`, `accrued_fees`, `pending_transfer_*` | **P1** — `account_blocked` / `trading_blocked` / `pattern_day_trader` are NEVER checked. A flagged account would silently fail every trade. Options buying power separate from cash buying power is also ignored — wheel can over-collateralize. | `stocks/shark/data/alpaca_data.py:250-256` — extend dict; `stocks/shark/execution/guardrails.py` — add a `check_account_status` precondition |
| A4 | **Positions endpoint** | `get_positions()` returns 8 fields per row (`shark/data/alpaca_data.py:285-300`). **Drops `asset_class`**. | Each position has `asset_id`, `symbol`, `exchange`, `asset_class` (`us_equity`/`us_option`/`crypto`), `qty_available` (qty minus held by open orders), `unrealized_intraday_pl`, `lastday_price`, `change_today`, `cost_basis`, `side` | **P0** — Missing `asset_class` is the regression fixed on 2026-05-11. Without it, options + stocks blur together; sizing/risk routines have wrong shape. Missing `qty_available` is a latent bug: stops or close-position calls can fail when shares are held by an open order. | `stocks/shark/data/alpaca_data.py:285-300` — add `asset_class`, `qty_available`, `cost_basis`, `asset_id` |
| A5 | **Orders POST — types/TIF** | Market + trailing_stop + bracket (`shark/execution/orders.py:265-285, 339-355, 533-561`). Wheel uses LimitOrderRequest for options (`wheel/broker.py:282-310`). TIF: `DAY` for stocks, `GTC` for stops/wheel. `client_order_id` deterministically derived (`orders.py:40-62`). | Order types: market, limit, stop, stop_limit, trailing_stop. TIF: day, gtc, opg, cls, ioc, fok. Order classes: simple, bracket, oco, oto, mleg. Options legs ≤ 4. `client_order_id` ≤ 128 chars, unique within same day. `extended_hours` boolean. `position_intent`: buy_to_open/buy_to_close/sell_to_open/sell_to_close. | **P1** — Multi-leg options (`mleg`) order class never used. Wheel CSP exit path uses single-leg buy-to-close but cannot do credit/debit spreads. `OCO` for adjacent stops never used. `IOC/FOK` never used — could reduce wheel limit-order slippage. `extended_hours=true` never used — could capture earnings-gap moves. | `stocks/shark/execution/orders.py` — consider adding LIMIT path with `time_in_force=IOC` for aggressive entries |
| A6 | **Order tracking** | Poll `get_order_by_id` every 0.5s for ≤ 10s after market submit (`orders.py:137-156`). No WebSocket. Wheel uses `submit_order` then trusts response. | `trade_updates` stream over `wss://paper-api.alpaca.markets/stream` (binary frames). Events: `new`, `accepted`, `partial_fill`, `fill`, `canceled`, `expired`, `replaced`, `pending_new`, `pending_cancel`, `pending_replace`, `done_for_day`, `stopped`, `rejected`, `suspended`, `calculated`. | **P1** — Polling for 10s catches only liquid market orders. The bracket child legs (stop/take-profit) are placed by Alpaca AFTER fill — we have **zero visibility** into when they're attached because we don't subscribe to `trade_updates`. A reject of the child leg would silently leave the parent fill unprotected. | `stocks/shark/execution/orders.py:570-580` — `stop_order_id: None` comment confirms we don't track child legs. Add a `TradingStream` listener |
| A7 | **Options API** | `GetOptionContractsRequest` with `underlying_symbols`, `status=ACTIVE`, expiry window, type, strike band (`wheel/broker.py:180-191`). Uses `get_option_snapshot` for greeks + quotes. Single-leg STO/BTC only (`wheel/broker.py:282-315`). | Multi-leg orders via `legs[]` (≤4) with `position_intent` per leg. Exercise via `POST /v2/positions/{symbol}/exercise`. ITM auto-exercise at $0.01. Insufficient BP → forced sell 1 hour before expiry. Contract symbology: `AAPL241220C00200000`. Options buying power separate from cash BP. | **P2** — Exercise endpoint never called; relying on Alpaca's auto-exercise. Auto-exercise + insufficient BP path is silently brittle — we never check options BP and never detect a forced-sell event. Also `option_snapshot` uses `get_option_snapshot()` which pulls greeks for EVERY contract in the chain → expensive (rate-limit risk on wide chains). | `stocks/wheel/broker.py:200-215` — gate the snapshot call by minimum chain size; check options BP before STO |
| A8 | **Rate limits + retries** | Custom `_retry_order` with 3 attempts × exponential backoff base 1s (`orders.py:104-134`). Catches `_AlpacaAPIError` broadly. | Alpaca: 200 req/min per API key. 429 returns `Retry-After` header. Errors with HTTP code; SDK exposes `code` + `message`. | **P0** — `_retry_order` retries on **every** `_AlpacaAPIError`, including 422 (validation), 403 (auth), 4xx rejection codes that should propagate immediately. A bad-input order retried 3× wastes the rate budget and adds latency. We also don't read the `Retry-After` header — backoff is blind exponential. | `stocks/shark/execution/orders.py:108` — refine the `retryable` exception filter; check the SDK exception's `status_code` and only retry 429/5xx |
| A9 | **WebSocket streaming** | **Not used at all.** No `TradingStream` or `StockDataStream` imports. | `wss://stream.data.alpaca.markets/v2/{iex|sip|test}` for market data. `wss://paper-api.alpaca.markets/stream` for trade_updates. Heartbeats, sequence numbers, auto-reconnect on drop in SDK. | **P1** — No live feed; everything is REST poll-based. For a 5-second polling rhythm this is acceptable; for a wheel pilot that needs same-day fill confirmation of CSP entries, this misses fill events for up to 10s after `submit_order`. | `stocks/wheel/runner.py` — consider a one-shot TradingStream listener spawned from cron-fired jobs that need fill confirmation |
| A10 | **Historical data** | `StockBarsRequest` with explicit start, `adjustment=ALL` (split+dividend), feed selected by `ALPACA_DATA_FEED` env var (default `iex`) (`alpaca_data.py:338-362`). Batch up to 100 symbols (`alpaca_data.py:418-512`). | IEX (free), SIP (paid, 15-min delay for free), OTC (broker-partners only). `adjustment`: raw/split/dividend/all. Max ~10000 bars per request. | **P1** — `scripts/alpaca.sh:52` hardcodes `feed=sip` for the `bars` subcommand. On a free account this returns 422; on a paid account this incurs SIP usage. The Python path correctly reads `ALPACA_DATA_FEED`. The bash and Python paths are inconsistent. | `stocks/scripts/alpaca.sh:52` — change to `feed=${ALPACA_DATA_FEED:-iex}` |
| A11 | **Corporate actions** | **Not handled.** No call to Corporate Actions API. Bars are `Adjustment.ALL` so historical OHLCV is split+div adjusted, but live position state is not reconciled. | `/v2/corporate_actions/announcements` exposes splits, dividends, mergers, spinoffs as soon as ingested (~T+1 from declaration). For forward splits, GTC buy limits and sell stops are auto-adjusted. For reverse splits, ALL GTC orders prior to the effective date are **canceled by Alpaca**. | **P1** — A reverse split silently cancels our wheel's GTC short-put STO if it's near-the-money on the underlying. We have no observer to detect this — wheel state shows position as "active" while broker shows it gone. Dividend payouts arrive as `DIV` activity events we never read. | `stocks/wheel/runner.py` — add a daily corporate-actions sweep on tracked underlyings; `stocks/shark/execution/orders.py` — wire `/v2/account/activities` to reconcile cash drift |
| A12 | **Calendar / market hours** | **Never checked.** `alpaca.sh:77-79` exposes `market-status` ≡ `/v2/clock` but no Python caller. No `/v2/calendar` integration. | `/v2/clock` returns `is_open`, `next_open`, `next_close`, `timestamp`. `/v2/calendar?start=&end=` returns `[{date, open, close, session_open, session_close}]` for half-days and holidays. | **P1** — Shark phases (`pre_market`, `market_open`, `midday`) trust their cron schedule for market hours. On a half-day (1pm close) or unscheduled holiday closure, the bot will submit orders that fail. Wheel cron also doesn't check — Friday 11am ET fires unconditionally. | Add a `/v2/clock` precheck at the top of every phase entry — refuse to place new orders if `is_open == false` or `next_close < now + cushion` |
| A13 | **Fractional shares** | **Not used.** Shark sizing rounds to whole shares (`position_sizer.py`). Wheel uses qty=1 per contract. | Fractional supported for market orders only, time-in-force=day only. Allowed via `notional` (USD) instead of `qty`. Not eligible for GTC, limit, or stop orders. | **P2** — Acceptable for now (whole shares is conservative). Note for record. | n/a |
| A14 | **Order rejection codes** | Generic exception catch + log + notify (`orders.py:304-313`). All errors stringified. | Common reject codes: `insufficient buying power`, `asset not tradable`, `pattern day trader limit`, `wash sale`, `account_blocked`, `position_intent_mismatch`. SDK exposes structured `code`+`message`. | **P1** — Operator sees `"Alpaca order failed for AAPL: <stringified>"`. No structured handling of "insufficient buying power" vs "asset not tradable". Wheel could retry with smaller qty automatically on BP error; today every reject is fatal for that cycle. | `stocks/shark/execution/orders.py:304-313` — parse SDK exception's `code` field; map known codes to recovery paths |
| A15 | **Margin / shorting** | `shorting_enabled` never read. Shark's hard rules forbid shorts (`stocks/CLAUDE.md`). Wheel CSP collateral is cash-secured (verified via `buying_power` check in `wheel/runner.py:268-272`). | Account has `shorting_enabled`, `short_market_value`, `regt_buying_power`. Short equity orders use `side=sell` on a flat position. | None (Shark explicitly forbids; wheel uses cash-secured puts) | n/a |
| A16 | **Position liquidation events** | **Not listened for.** `account_blocked` and `trade_suspended_by_user` never checked. | `trade_updates` stream includes `stopped`, `suspended`, `done_for_day`, `rejected` events that signal Alpaca-initiated liquidation. | **P2** — Low likelihood in paper; in live, an account flag → forced liquidation would be discovered hours later via `get_positions()` returning empty. | Same fix as A6 — subscribe to `trade_updates` |

---

## 4. Coinbase audit (via ccxt + standalone Advanced Trade SDK)

| # | Topic | Our implementation | Coinbase API capability | Gap | Concrete fix path (file:line) |
|---|-------|--------------------|--------------------------|-----|--------------------------------|
| C1 | **Authentication** | Two paths: (a) freqtrade/ccxt reads `FREQTRADE__EXCHANGE__KEY/SECRET` from env (legacy HMAC-like); (b) `execution_engine.py:213-224` prefers `COINBASE_KEY_FILE` JSON (CDP) → fallback to `COINBASE_API_KEY/SECRET`. | CDP API keys with JWT ES256 (PEM EC) or EdDSA (Ed25519). JWT lifetime **120 seconds**. Required claims: `sub`, `iss="cdp"`, `nbf`, `exp`, `uri="<METHOD> <host><path>"`. Each request needs a fresh JWT. | **P1** — Two different auth flows on two code paths. If operator rotates the CDP key file but forgets `COINBASE_API_KEY/SECRET`, the ccxt path may still work via legacy keys (if any). Risk of "shadow keys" in production. The 120-second JWT expiry means clock skew >1min breaks auth entirely; we don't monitor system clock. | Centralize on the CDP JSON key file. Document `key_file_env` precedence in `.env.example`. Add a chrony/ntp check to `scripts/health-check.sh` |
| C2 | **Rate limits** | freqtrade enables ccxt rate limiting by default. `execution_engine.py` uses exponential backoff retry × 3 (`execution_engine.py:297-317`). | **30 req/sec/auth-user for private endpoints; 10 req/sec/IP for public.** WebSocket: max channels per connection limited (not numerically documented). | **P2** — Per-endpoint ceilings are not enforced by us — we trust ccxt. ccxt's `enableRateLimit` is not explicitly set in `user_data/config.json:60` (empty `ccxt_config: {}`). On Coinbase's relatively generous 30 req/sec we are unlikely to hit limits at 12 pairs × 5m timeframe, but burst-mode (regime changes triggering 12 simultaneous cancellations) could. | `user_data/config.json:60` — add `"ccxt_config": {"enableRateLimit": true}` defensively |
| C3 | **Order types** | freqtrade: limit entry + limit exit + emergency_exit=limit + stoploss=limit (`config.json:30-37`). TIF=GTC. ExecutionEngine: limit-only with `post_only=True` (`execution_engine.py:242-269`). | Coinbase Advanced Trade order_configuration variants: `market_market_ioc`, `limit_limit_gtc`, `limit_limit_gtd`, `stop_limit_stop_limit_gtc`, `stop_limit_stop_limit_gtd`. `post_only` only on limit_limit_gtc/gtd. | **P0** — `config.json:35`: `"stoploss": "limit"` BUT `"stoploss_on_exchange": false`. Stop-loss is a **soft Python decision**, not a broker-side stop. If the freqtrade process dies, positions are unprotected. Coinbase ccxt's stop_limit support is buggy (ccxt #25217 swallows `preview_failure_reason`). | `user_data/config.json:30-37` — discuss whether `stoploss_on_exchange=true` is feasible on Coinbase ccxt; if not, document the operational risk and add a dead-man-switch external watchdog |
| C4 | **Crypto-specific** (fractional sizes, min notional, fees) | `fee: 0.005` (50 bps) hardcoded in `config.json:9`. No min-notional check before order placement. ExecutionEngine doesn't check min_size from `/api/v3/brokerage/products/{id}` either. | Fees are tiered by 30-day USD volume (taker tops out ~60bps, can fall to <20bps on high-volume tiers + special pairs). Each product has `base_min_size`, `base_increment`, `quote_increment`. Stablecoin pairs have different fee schedules. | **P1** — Our 50-bps fixed fee in P&L calc overstates costs at higher volumes (good) but underestimates at lower volumes for some assets. More importantly, **no product-precision validation**: a calculated size of 0.000001 BTC will be rejected by Coinbase if it's below `base_min_size`. ExecutionEngine has no precision rounding step. | `user_data/modules/execution_engine.py:491-526` — add a `_validate_size_increment` step using `client.get_product(product_id).base_min_size/base_increment` |
| C5 | **WebSocket** | **Not used.** All Coinbase data is REST polled. Dashboard fetches public candles via `https://api.exchange.coinbase.com/products/{id}/candles` (`dashboard/data_sources.py:294-322`). | wss://advanced-trade-ws.coinbase.com. Channels: `heartbeats` (1s, with sequence counter), `level2`, `ticker`, `ticker_batch`, `market_trades`, `candles`, `user`, `status`. Subscribing to **-USDC pairs** only works on the `user` channel. Most channels close after 60-90s of inactivity unless you subscribe to `heartbeats`. | **P1** — REST-only means our regime detector's BTC OHLCV polls Coinbase every 5 minutes (`regime_detector.py:132`), burning ~288 req/day. WebSocket would cut this to one connection. More critically, we have no `user` channel subscription, so we have no live order updates from Coinbase — purely poll-based confirmation. | `user_data/modules/execution_engine.py` — wire the SDK's `WSClient` for the `user` channel post-place, replacing the polling loop |
| C6 | **24/7 market scheduling** | freqtrade's `process_throttle_secs: 5` runs continuously (`config.json:158`). No US-market gating. | Coinbase is 24/7. Maintenance windows ≤1hr/month, posted on https://status.exchange.coinbase.com. | **P2** — Correct posture (24/7) but we have no maintenance-window detector. A scheduled maintenance during a regime shift could leave positions stuck in transition state. | Add `status.exchange.coinbase.com` to the health-check sweep |
| C7 | **Sandbox / paper** | We use `config.json:7 "dry_run": true`. freqtrade simulates fills in-process. Separately, `ExecutionEngine` has its own `cfg.dry_run` (synthetic order IDs, no network). | Coinbase Advanced Trade **does** have a sandbox at `https://api-sandbox.coinbase.com/api/v3/brokerage`, but it only supports Accounts + Orders endpoints with static mock responses. **It does not simulate matching.** Custom `X-Sandbox` headers trigger specific scenarios. | **P0** — There is no "paper mode" against real liquidity. Our `dry_run=true` simulates fills against historical/synthetic prices in freqtrade. **This is not equivalent to live**: real partial fills, slippage past the spread, post_only rejection, and fee-tier dynamics are absent. Going live = first time with real fills. | Document this explicitly in `MIGRATION_NOTES.md` as a known limitation. Consider running a tiny ($100-200) real-money smoke test on a single pair before scaling up |
| C8 | **Fees** | `fee: 0.005` in config (`config.json:9`) used for P&L estimation. ExecutionEngine doesn't log per-fill fee separately. | Fees are deducted from fills, reported in the `fills[].fee` field of `GET /api/v3/brokerage/orders/{order_id}/fills`. Taker fees come out of the quote currency on buys; from base on sells. | **P1** — Our P&L is computed against a flat 50bps assumption. The trade_journal / unified_risk module won't reconcile against actual fees. After 1000 trades the divergence could be material (~0.1% drift per trade in either direction). | `user_data/modules/execution_engine.py:343-416` — after FILLED, fetch `/orders/{order_id}/fills` and record the actual fee; update P&L journal with real basis |
| C9 | **Order cancellation race** | `_cancel` sends only `order_ids` (`execution_engine.py:566-570`). After timeout we cancel; we then check `filled_size`. | Coinbase `cancel_orders` is best-effort: an order may fill in the window between cancel request and confirmation. Response: `results[i].success bool` + `failure_reason`. Partial-fills + cancel response can collide. | **P0** — `_cancel` does not inspect the cancel response. If `success=false` with `failure_reason="UNKNOWN_CANCEL_ORDER"` (already filled), we miss the fill. Code path: `monitor()` → timeout branch (`execution_engine.py:401-416`) → records `status="CANCELLED"` even though it may have filled. | `user_data/modules/execution_engine.py:566-570` — read `result.results[].success`; if false, re-fetch the order to find out actual status |
| C10 | **Funding / deposits** | None — bot never moves funds. Dry-run currently. | Coinbase has separate Funding endpoints (deposit/withdraw). Not part of Advanced Trade `brokerage/orders`. | None (correct) — but no fence prevents `RESTClient.deposit_*` from being called accidentally if a new module imports the SDK. | Consider creating a CDP API key with **only** "View" + "Trade" scopes, no "Transfer" scope, so an accidental call is rejected at the API layer |

---

## 5. Cross-cutting audit (6 rows)

| # | Topic | Our implementation | What good looks like | Gap | Concrete fix path |
|---|-------|--------------------|-----------------------|-----|--------------------|
| X1 | **Paper vs live switching** | THREE flags govern this: `TRADING_MODE` env var (Shark/Wheel), `ALPACA_BASE_URL` (Shark/Wheel), and `config.json#dry_run` (freqtrade) + `config.json#execution.dry_run` (Coinbase ExecutionEngine). Soft warnings only when they disagree. | Single source of truth, fail-closed when inconsistent. | **P0** — Four interdependent flags. A deploy that flips `TRADING_MODE=live` but forgets to update `ALPACA_BASE_URL` will execute on paper anyway. Worse: a deploy that flips `ALPACA_BASE_URL` to live but leaves `config.json#dry_run=true` results in **stocks live, crypto still paper** — split-brain. | Add a top-level `IS_LIVE` env var consumed by ALL four subsystems; refuse boot if any subsystem's local flag disagrees. Promote `shark/config.py:195-205` warnings to fatals |
| X2 | **Secrets management** | `.env` file (gitignored, sourced by `scripts/alpaca.sh:12-17`); Coinbase JSON key in `./secrets/coinbase.json` (gitignored, mounted RO). No vault. `.env.example:65-72` documents the layout. | Vault or AWS Secrets Manager for live. Rotation procedure documented. No keys in env vars persisted in shell history. | **P1** — Env vars are visible in `/proc/<pid>/environ` to root. No documented rotation procedure. No alarm if the .env file mtime changes unexpectedly. | Document a rotation runbook. For live, consider migrating Alpaca keys to a file (`ALPACA_KEY_FILE=`) like the Coinbase one. Add filewatcher to alert on `.env` changes |
| X3 | **Idempotency** | Shark `place_order`: deterministic `client_order_id` derived from `SHA256(symbol|side|qty|tag|date|extra)` (`orders.py:40-62`). Wheel: uses alpaca-py defaults (random UUID per call). ExecutionEngine: random UUID per call (`execution_engine.py:269`). freqtrade: ccxt's UUID per call. | Every order has a deterministic, retry-safe client_order_id. | **P1** — Shark stack is excellent; everything else is best-effort. A wheel `sell_to_open` that times out then retries can result in TWO short puts. ExecutionEngine's UUIDs are random but the place_limit retry loop reuses the SAME UUID across attempts (good) — but a fresh call gets a fresh UUID (bad on cron-retry). | `stocks/wheel/broker.py:282-315` — accept an optional `client_order_id`, default to `SHA256(symbol|qty|date)`; `execution_engine.py:269` — make UUID stable across cron cycle (e.g. include date) |
| X4 | **Audit trail** | Shark: `memory/TRADE-LOG.md` (markdown, appended), `memory/decisions.md` (jsonl). Wheel: `wheel/state/trades.json`. Freqtrade: SQLite at `db_url`. Coinbase ExecutionEngine: dedicated rotating `user_data/logs/execution.log` (`execution_engine.py:66-79`). | Single ledger per asset class; reconcilable against broker fills. | **P1** — Four ledgers, no canonical reconciliation. No daily job to walk Alpaca/Coinbase fills and verify each appears in our logs. Wheel + Shark may disagree about a position's existence (Wheel writes its own JSON; Shark reads broker-side). | Daily reconciliation routine: fetch all Alpaca `/v2/account/activities?after=yesterday` and Coinbase `/api/v3/brokerage/orders/historical/fills`, cross-check against our local ledgers. Flag drift |
| X5 | **Logging — secrets redaction** | `stocks/shark/llm/redaction.py` has regex-based redaction for LLM-bound text (api_key, webhook, path). Used by LLM logger only. | All emitted log lines (stdout, files) pass through a redactor. Headers in HTTP debug logs are masked. | **P1** — Redaction is opt-in (LLM logger only). `scripts/alpaca.sh` uses curl with auth headers in `-H` — if `-v` is ever flipped on, the keys land in stdout. The ExecutionEngine audit log writes order_ids only (good); RESTClient SDK debug mode would print JWT (not blocked). | Add `set +x` enforcement in `alpaca.sh`. Wrap the ExecutionEngine SDK in a class that disables `RESTClient(debug=True)` |
| X6 | **Health probes** | `stocks/scripts/health-check.sh` pings Alpaca `/v2/account` and Coinbase via ??. `user_data/modules/monitoring_mixin.py` checks freqtrade status. | Two-tier: (a) auth + reachability against both broker APIs; (b) time-sync (NTP), disk, key-file freshness. Alerts on failure. | **P2** — Health check exists for Alpaca; Coinbase coverage is inferred. No clock-skew alarm (critical for JWT 120s expiry). No alarm on .env file age. | Extend `health-check.sh` to (a) call Coinbase `/api/v3/brokerage/accounts`; (b) verify `chronyc tracking` offset < 1s; (c) verify `COINBASE_KEY_FILE` exists and is readable |

---

## 6. Top 5 P0 gaps — must fix before live

These are the gaps that will cause **direct money loss or incorrect trades** if live mode is enabled today.

### P0-1 — `stoploss_on_exchange: false` with no offline-safe replacement
**Location:** `user_data/config.json:36`
**Risk:** freqtrade process dies (OOM, host reboot, container crash) → open Coinbase positions have NO stop-loss → arbitrary drawdown until process is restarted and re-syncs.
**Why it's P0:** This is a single point of failure with unbounded downside.
**Fix description (no code):**
1. Investigate whether Coinbase Advanced Trade via ccxt actually accepts a server-side stop_limit order with sufficient reliability for production. ccxt issue #25217 suggests partial.
2. If yes — flip `stoploss_on_exchange: true` and verify under load.
3. If no — implement an out-of-process dead-man-switch: a separate cron-fired script that polls open positions every 60s and force-closes any whose `unrealized_pl < -X%`. Place this in `user_data/modules/dead_man_switch.py`, scheduled outside the main freqtrade container.
4. Wire alerting (Slack/email) to fire if the freqtrade health probe misses 2 consecutive cycles.

### P0-2 — `get_positions()` drops `asset_class`
**Location:** `stocks/shark/data/alpaca_data.py:285-300`
**Risk:** Options and equities are indistinguishable in the position dict. Risk routines that assume equities (e.g. `exit_manager.evaluate_exits`) apply equity stop logic to options, leading to inappropriate close orders. Confirmed-bug fixed on 2026-05-11; risk of regression.
**Why it's P0:** Already caused a production issue. Going live without this field re-opens the same hole.
**Fix description:** Extend the dict comprehension to include `asset_class`, `qty_available`, `cost_basis`, `asset_id`. Update `exit_manager.evaluate_exits` and `stops.manage_stops` to skip rows with `asset_class != "us_equity"`. Add a unit test asserting both classes round-trip through the function.

### P0-3 — Broad retry catches all `APIError`
**Location:** `stocks/shark/execution/orders.py:108-134`
**Risk:** A 422 "insufficient buying power" or 403 "trading_blocked" is retried 3 times with exponential backoff (1s → 2s → 4s). On a live wheel cron we burn rate-limit headroom and add 7 seconds to error propagation. Worse: a 422 on an already-filled order (rare) plus a retry would produce a duplicate (the deterministic client_order_id partially saves us — but the second submission may return the original order, leading us to believe a fresh order was filled).
**Why it's P0:** Money-impacting on rejection paths. Idempotency is partial, not absolute.
**Fix description:** Inspect SDK exception attributes (`status_code`, `code`); whitelist retryable: `429`, `500-504`, network errors. Everything else propagates immediately. Mirror the change in `alpaca_data.py:_retry`.

### P0-4 — ExecutionEngine cancel race on partial fills
**Location:** `user_data/modules/execution_engine.py:566-570` (`_cancel`) + `401-416` (timeout branch)
**Risk:** On timeout, we call `client.cancel_orders(order_ids=[order_id])` and trust it. If the order partially filled in the window, we mark it `CANCELLED` and the bot's accounting thinks no position was opened — while Coinbase actually has the position. Next strategy decision is made on stale state.
**Why it's P0:** Direct accounting divergence vs broker. Can compound across multiple cycles.
**Fix description:** After `_cancel`, immediately re-fetch the order. If `filled_size > 0`, transition the report to `PARTIAL` not `CANCELLED`. Inspect the cancel response's `results[].success` and `failure_reason="UNKNOWN_CANCEL_ORDER"` (means "already filled") — promote to FILLED in that case.

### P0-5 — Paper/live mode split-brain across subsystems
**Location:** cross-cutting (`stocks/shark/config.py:195-205` + `user_data/config.json:7` + `user_data/config.json:184` + `stocks/wheel/broker.py:384`)
**Risk:** Four independent flags govern paper vs live. The single most likely deploy mistake is updating one but not all four (e.g. `TRADING_MODE=live` for Shark but forgetting `config.json#dry_run` for freqtrade). Result: stocks trade live while crypto is paper-mocked, or vice versa. The Shark/Wheel side has soft warnings; nothing is fatal.
**Why it's P0:** A single mis-deploy → real money executed against simulated portfolio state.
**Fix description:** Introduce a top-level `IS_LIVE=true|false` env var. Every subsystem reads it on boot and refuses to start if its local flag disagrees:
- Shark/Wheel: `TRADING_MODE` must match
- Freqtrade: `dry_run` must be the inverse of `IS_LIVE`
- ExecutionEngine: `execution.dry_run` must be the inverse of `IS_LIVE`
- `ALPACA_BASE_URL` must contain `paper-api` iff `IS_LIVE=false`

Promote `shark/config.py:196-205` `logger.warning` to `raise ConfigError`.

---

## 7. Going-live readiness checklist

Operator-readable. Tick all 14 before flipping any flag to live.

- [ ] **1.** P0-1 — `stoploss_on_exchange` decision: either enable it on Coinbase OR deploy the out-of-process dead-man-switch and verify it triggers correctly in a forced drawdown drill.
- [ ] **2.** P0-2 — `get_positions()` returns `asset_class`, `qty_available`, `cost_basis`. Unit test added.
- [ ] **3.** P0-3 — `_retry_order` and `_retry` whitelist retryable status codes. Verify with a deliberately bad order (e.g. qty=1000000) — it should fail in <1s, not 7s.
- [ ] **4.** P0-4 — ExecutionEngine cancel-race fix shipped. Integration test: place a tiny order with timeout=2s in sandbox; verify `PARTIAL` vs `CANCELLED` transitions correctly.
- [ ] **5.** P0-5 — Single `IS_LIVE` flag. All four subsystems hard-fail on disagreement. Verified by `pytest tests/test_live_mode_consistency.py`.
- [ ] **6.** P1 — Shark `get_account()` extended with `pattern_day_trader`, `account_blocked`, `trading_blocked`, `options_buying_power`. Guardrails refuse to trade if any block flag is set.
- [ ] **7.** P1 — Subscribe to Alpaca `trade_updates` stream from a long-lived daemon process. Confirm fills and child-leg rejects in real time. (Replaces the 10s poll.)
- [ ] **8.** P1 — Coinbase product-precision validation in ExecutionEngine. A size below `base_min_size` is rejected with a clear local error, never sent to Coinbase.
- [ ] **9.** P1 — Daily reconciliation cron: walks Alpaca `/v2/account/activities` and Coinbase fills, cross-checks against our ledgers. Alerts on any drift > $1 or 1 share/coin.
- [ ] **10.** P1 — `scripts/alpaca.sh` `bars` subcommand uses `${ALPACA_DATA_FEED:-iex}` instead of hardcoded `sip`.
- [ ] **11.** P1 — Corporate-actions awareness: a daily sweep of `/v2/corporate_actions/announcements` for our wheel underlyings. Alert on splits.
- [ ] **12.** P1 — Calendar precheck: every Shark phase calls `/v2/clock` before placing orders; refuses on `is_open=false` or `now > next_close - 5min`.
- [ ] **13.** P2 — Health check extended: NTP offset < 1s, Coinbase auth round-trip, key-file mtime sanity.
- [ ] **14.** P2 — CDP API key rescoped to only "View" + "Trade" — no "Transfer". Documented in `.env.example`.

**Plus operational drills:**
- [ ] Run a $100 real-money smoke test on a single Coinbase pair (e.g. BTC/USD with size $50) for 24h before scaling to full portfolio.
- [ ] Run a 1-share real-money smoke test on Alpaca for one Shark trade and one wheel CSP (single contract) before scaling.

---

## 8. Appendix — Sourced reference URLs (canonical, post-redirect)

### Alpaca
- Getting Started: https://docs.alpaca.markets/docs/getting-started
- Trading API: https://docs.alpaca.markets/docs/trading-api
- Place Order schema: https://docs.alpaca.markets/reference/postorder
- Options Trading: https://docs.alpaca.markets/docs/options-trading
- Options Contracts: https://docs.alpaca.markets/reference/get-options-contracts
- Paper Trading: https://docs.alpaca.markets/docs/paper-trading
- WebSocket trade_updates: https://docs.alpaca.markets/docs/websocket-streaming
- Corporate Actions: https://docs.alpaca.markets/docs/mandatory-corporate-actions
- Authentication: https://docs.alpaca.markets/docs/authentication
- TradingStream SDK ref: https://alpaca.markets/sdks/python/api_reference/trading/stream.html
- Rate limits Support article: https://alpaca.markets/support/usage-limit-api-calls

### Coinbase Advanced Trade
- Welcome: https://docs.cdp.coinbase.com/advanced-trade/docs/welcome
- JWT Auth: https://docs.cdp.coinbase.com/get-started/authentication/jwt-authentication
- WS Channels: https://docs.cdp.coinbase.com/coinbase-app/advanced-trade-apis/websocket/websocket-channels
- Sandbox: https://docs.cdp.coinbase.com/coinbase-app/advanced-trade-apis/sandbox
- Python SDK: https://github.com/coinbase/coinbase-advanced-py
- Order Management: https://docs.cdp.coinbase.com/coinbase-app/advanced-trade-apis/guides/orders

### Freqtrade & ccxt
- Freqtrade configuration: https://www.freqtrade.io/en/stable/configuration/
- Freqtrade exchanges (Coinbase absent): https://www.freqtrade.io/en/stable/exchanges/
- Coinbase integration tracker: https://github.com/freqtrade/freqtrade/issues/9606
- ccxt Coinbase error propagation bug: https://github.com/ccxt/ccxt/issues/25217

---

*End of audit. Next session: implement the top-5 P0s on `feat/exchange-api-hardening` branch (separate from this audit branch).*
