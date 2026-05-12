# HANDOFF — Exchange API Gap Audit (2026-05-12)

**Branch:** `audit/exchange-api-gaps` (NOT pushed)
**Deliverable:** `docs/audit/EXCHANGE_API_GAPS_2026-05-12.md` (~30 KB)
**Verdict:** **NO-GO / CONDITIONAL** — paper-mode OK, fix the five P0s before live.

---

## Doc-URL coverage

### Alpaca

| URL | Status |
|-----|--------|
| docs.alpaca.markets/docs/getting-started | partial (nav hub) |
| docs.alpaca.markets/docs/trading-api | partial |
| docs.alpaca.markets/reference/getaccount | partial (supplemented via SDK) |
| docs.alpaca.markets/reference/getallpositions | 404 → moved to `/getallopenpositions` |
| docs.alpaca.markets/reference/postorder | FULL |
| docs.alpaca.markets/docs/options-trading | FULL |
| docs.alpaca.markets/reference/get-options-contracts | FULL |
| docs.alpaca.markets/docs/paper-trading | FULL |
| docs.alpaca.markets/docs/streaming-real-time-data | 404 (supplemented via WebSearch) |
| docs.alpaca.markets/docs/working-with-trading-api | 404 (supplemented via WebSearch) |
| docs.alpaca.markets/docs/historical-market-data | 404 (supplemented) |
| docs.alpaca.markets/docs/sdks-and-tools | partial |
| docs.alpaca.markets/reference/getallorders | 404 (supplemented) |
| docs.alpaca.markets/docs/mandatory-corporate-actions | FULL via WebSearch |

### Coinbase Advanced Trade

| URL | Status |
|-----|--------|
| docs.cdp.coinbase.com/advanced-trade/docs/welcome | partial |
| docs.cdp.coinbase.com/advanced-trade/docs/rest-api-overview | sparse |
| docs.cdp.coinbase.com/advanced-trade/reference/retailbrokerageapi_postorder | 404 (supplemented via SDK + WebSearch) |
| docs.cdp.coinbase.com/advanced-trade/docs/rest-api-authentication | sparse |
| docs.cdp.coinbase.com/get-started/authentication/jwt-authentication | FULL |
| docs.cdp.coinbase.com/advanced-trade/docs/rest-api-rate-limits | sparse (supplemented: 30 req/s priv, 10 req/s pub) |
| docs.cdp.coinbase.com/advanced-trade/docs/ws-channels | partial (supplemented) |
| docs.cdp.coinbase.com/advanced-trade/docs/sandbox | 404 → real URL: `/coinbase-app/advanced-trade-apis/sandbox` |
| docs.cdp.coinbase.com/coinbase-app/advanced-trade-apis/sandbox | FULL |
| docs.cdp.coinbase.com/advanced-trade/docs/rest-api-fee-tiers | sparse |

### Freqtrade context

| URL | Status |
|-----|--------|
| freqtrade.io/en/stable/configuration/ | FULL |
| freqtrade.io/en/stable/exchanges/ | FULL — Coinbase absent from supported list |
| github.com/freqtrade/freqtrade/issues/9606 | FULL — Coinbase integration closed "not planned" |

---

## Codebase coverage (files audited fully)

### Alpaca stack
- `stocks/shark/data/alpaca_data.py` (647 lines)
- `stocks/shark/execution/orders.py` (711 lines)
- `stocks/shark/execution/stops.py` (216 lines)
- `stocks/shark/execution/exit_manager.py` (282 lines)
- `stocks/shark/execution/guardrails.py` (442 lines)
- `stocks/wheel/broker.py` (385 lines)
- `stocks/wheel/runner.py` (602 lines)
- `stocks/shark/config.py` (paper/live toggle logic)
- `stocks/scripts/alpaca.sh` (curl wrapper)
- `.env.example` (auth shape)

### Coinbase stack
- `user_data/config.json` (freqtrade root config)
- `user_data/strategies/nfi_x6_config.json`
- `user_data/modules/execution_engine.py` (665 lines — standalone Coinbase Advanced Trade engine)
- `user_data/modules/regime_detector.py` (public OHLCV)
- `user_data/dashboard/data_sources.py` (public candles cache)
- `user_data/strategies/NostalgiaForInfinityX6.py:735-758` (ccxt_config injection)

---

## Top 5 P0 findings — one-line each

1. **`user_data/config.json:36`** — `stoploss_on_exchange: false`; bot crash leaves Coinbase positions unprotected. No dead-man-switch.
2. **`stocks/shark/data/alpaca_data.py:285-300`** — `get_positions()` drops `asset_class`; options + equities indistinguishable. Caused 2026-05-11 bug; regression risk.
3. **`stocks/shark/execution/orders.py:108`** — `_retry_order` retries on every `APIError` including 4xx auth/validation; wastes rate budget and obscures errors.
4. **`user_data/modules/execution_engine.py:566-570`** — `_cancel` ignores response; partial-fill + cancel race silently mis-records position state.
5. **Cross-cutting** — Four independent flags govern paper/live (`TRADING_MODE`, `ALPACA_BASE_URL`, `config.json#dry_run`, `execution.dry_run`); soft warnings only. Split-brain risk.

---

## Full report

See `docs/audit/EXCHANGE_API_GAPS_2026-05-12.md` for:
- Executive verdict + rationale
- 16-row Alpaca audit (priorities P0-P2)
- 10-row Coinbase audit
- 6-row cross-cutting audit
- Top 5 P0 fix descriptions (no code)
- 14-item operator readiness checklist
- Canonical-URL appendix
