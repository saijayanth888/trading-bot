# Backend API Audit — 2026-05-14 night

## Scope
- 45 routes probed (37 `/api/ops/*` + 8 `/api/v4/*`, including 2 path-param drill-downs and 1 SSE)
- Read-only: GET endpoints only, no mutating POSTs invoked, no LLM/paid endpoints called
- Total wall time for main probe sweep: **4.88 s** (43 routes), plus 3 follow-up gates re-probes + SSE + drill-downs
- Dashboard container: `dashboard` (no 5xx in `docker logs --since 5m` during audit)
- Probe artifacts: `/tmp/api_audit/*.json`, `/tmp/api_audit/results.txt`, `/tmp/api_audit/analysis.json`

## Findings (sorted by severity)

### P0 (live-broken)
*None.* Every route returned HTTP 200, no 5xx, no tracebacks observed in dashboard logs during the probe window.

### P1 (silent corruption / latency)

- **`GET /api/ops/gates` — latency 3.7–4.0 s (consistently)**
  Evidence:
  ```
  ops_gates|200|3.704025|33184|/api/ops/gates
  gates run1: 3.866731s
  gates run2: 3.981406s
  gates run3: 4.012566s
  ```
  Three consecutive re-probes all 3.8–4.0 s. Above the 2 s P1 threshold but below the 5 s P0 threshold. Payload is a fully-populated 33 KB list of crypto+stocks gate evaluations (`status=ok`), so the slowness is computational, not failure-mode. Dashboard polls this on the gates card.

### P2 (cosmetic / sentinel values / staleness)

- **`GET /api/ops/stocks_ml` — `data.training_finished_at='2026-05-11T21:35:11Z'` (age ≈ 75.4 h)**
  Evidence: `"weights_age_seconds": 271312` in payload corroborates (~75 h). Weekly-training cadence is plausibly correct (champion `stock_tft_v1` from week ending 2026-05-11), but the field reads stale by the 12 h non-market-hours threshold. Likely intentional given weekly retrain cadence — flag for confirmation, not breakage.

- **`GET /api/v4/screening` — 2 of 5 sampled rows have `regime: "unknown"`**
  Evidence:
  ```
  names[1].regime='unknown'
  names[4].regime='unknown'
  names[1].last_setup_ts='2026-05-11T21:56:10.159851+00:00'  (age 75.0 h)
  ```
  Mixed-tenancy screening output: some symbols have valid regime, others land on the `unknown` sentinel. Last-setup timestamp on the `unknown` row is 75 h old, suggesting screener didn't re-tag those symbols this week.

- **`GET /api/ops/stocks` — `data.wheel.open_positions[0].opened_at='2026-05-13T15:00:20.913168+00:00'` (age 33.9 h)**
  Evidence: timestamp simply records when the wheel position was opened. Not actually stale — the field semantically means "open since". False positive from the generic age check; flagging for completeness.

- **`GET /api/ops/live_trades` — `data.trades[0].opened_at='2026-05-13T15:00:20.913168+00:00'` (age 33.9 h)**
  Same wheel position surfaced through the live-trades endpoint. Same false-positive characterisation.

### P3 (nit)

- **`/api/v4/*` routes do not use the standard `{status,data,error,checked_at}` envelope.**
  All 7 audited v4 routes return raw payloads keyed by domain (`positions`, `trades`, `rows`, `weeks`, etc.):
  ```
  v4_positions     keys: ['positions', 'source']
  v4_trades        keys: [...raw...]
  v4_debate_history (raw)
  v4_adapters      (raw)
  v4_weekly_preview (raw)
  v4_parity        keys: ['rows', 'weeks', 'consecutive_days_ok', 'cutover_threshold_days']
  v4_screening     (raw)
  ```
  This appears intentional (`docs.js` describes `/api/v4/*` as a separate "V4 trade tape" surface); v4 routes never set the envelope. Not a bug, but if the dashboard contract claims a uniform envelope, callers must special-case `/api/v4/*`. No fix recommended without operator decision.

- **SSE drill-down `/api/v4/debate/stream/{session_id}` returns frames against synthetic session id `test_session`.** Probe: `200 in 2.000s` (forced timeout). First frame is `session_start` for `SOL/USD` with `setup_ts=2026-05-15T00:57:51`, then `vote_partial` tokens stream — endpoint is alive and willing to stream against any id (no 4xx for unknown session). Cosmetic only.

## Healthy routes (one-line each)

### `/api/ops/*` — envelope `status=ok` unless noted
- `/api/ops/services` — 200 in 0.012 s, status=ok, 715 B
- `/api/ops/uptime` — 200 in 0.001 s, status=ok, 155 B
- `/api/ops/training` — 200 in 0.001 s, status=ok, 254 B
- `/api/ops/training_health` — 200 in 0.002 s, status=ok, 311 B
- `/api/ops/regime` — 200 in 0.031 s, status=ok, 977 B
- `/api/ops/sentiment` — 200 in 0.025 s, status=ok, 2.4 KB
- `/api/ops/mcp` — 200 in 0.001 s, status=ok, 559 B
- `/api/ops/trades_risk` — 200 in 0.027 s, status=ok, 1.5 KB
- `/api/ops/sparklines` — 200 in 0.298 s, status=ok, 36 KB (largest healthy payload)
- `/api/ops/regime_config` — 200 in 0.002 s, status=ok, 1.0 KB
- `/api/ops/risk_gates` — 200 in 0.002 s, status=ok, 1.0 KB
- `/api/ops/config` — 200 in 0.002 s, status=ok, 5.6 KB
- `/api/ops/readiness` — 200 in 0.012 s, status=ok, 740 B
- `/api/ops/rebalance` — 200 in 0.016 s, status=ok, 1.2 KB
- `/api/ops/tools` — 200 in 0.002 s, status=ok, 3.1 KB
- `/api/ops/mcp/tools` (alias) — 200 in 0.001 s, status=ok, 3.1 KB (matches above)
- `/api/ops/explainability/BTC/USDT` — 200 in 0.013 s, **status=degraded** with non-null error: `"no entries or blocked-decisions in window"` (envelope contract correctly honoured)
- `/api/ops/timeline/BTC/USDT` — 200 in 0.211 s, status=ok, 29 KB
- `/api/ops/slack_preview` — 200 in 0.081 s, status=ok, 619 B
- `/api/ops/stock_candles/NVDA` — 200 in 0.005 s, status=ok, 26 KB
- `/api/ops/stocks_sparklines` — 200 in 0.011 s, status=ok, 9.9 KB
- `/api/ops/market_hours` — 200 in 0.001 s, status=ok, 414 B
- `/api/ops/flash_status` — 200 in 0.015 s, status=ok, 435 B
- `/api/ops/ollama_health` — 200 in 0.001 s, status=ok, 444 B
- `/api/ops/circuit_breakers` — 200 in 0.001 s, status=ok, 172 B
- `/api/ops/llm_stats` — 200 in 0.016 s, status=ok, 1.5 KB
- `/api/ops/combined_portfolio` — 200 in 0.063 s, status=ok, 1.2 KB
- `/api/ops/shark_briefing` — 200 in 0.001 s, status=ok, 1.5 KB
- `/api/ops/stock_regime` — 200 in 0.001 s, status=ok, 328 B
- `/api/ops/shark_override_health` — 200 in 0.001 s, **status=degraded** with non-null error: `"override stalled — 3 consecutive run(s) with candidates but no trades"`. Embedded `data.checked_at=2026-05-14T13:47:12` (`age_s=40137` ≈ 11.1 h). Envelope contract honoured; payload itself reports the stall correctly.
- `/api/ops/backtest_gates` — 200 in 0.001 s, **status=degraded** with non-null error: `"no gates_report_*_latest.json files yet — cron has not run, or wrong results dir"`. Envelope contract honoured.
- `/api/ops/weekly_training` — 200 in 0.009 s, status=ok, 2.2 KB
- `/api/ops/llm_calls` — 200 in 0.002 s, status=ok, 9.6 KB (50 calls, 24h totals present)
- `/api/ops/llm_calls/{ts}` (drill-down) — 200 in 0.003 s, status=ok, single record returned with full prompt+response

### `/api/v4/*` — raw payloads (envelope by-design absent — see P3 note)
- `/api/v4/positions` — 200 in 0.015 s, `{"positions":[],"source":"quanta_schema.fills"}`
- `/api/v4/trades` — 200 in 0.010 s, 9.3 KB tape
- `/api/v4/debate/history` — 200 in 0.013 s, 3.0 KB
- `/api/v4/debate/stream/{session_id}` — 200 (SSE), streams session_start + vote_partial frames
- `/api/v4/montecarlo/1` — 200 in 0.029 s, full path payload (10000 paths × 48 bars sample)
- `/api/v4/adapters` — 200 in 0.002 s, 5.0 KB
- `/api/v4/weekly/preview` — 200 in 0.001 s, 2.1 KB
- `/api/v4/parity` — 200 in 0.011 s, 10.7 KB
- `/api/v4/screening` — 200 in 0.001 s, 5.0 KB (see P2 above for `regime=unknown` rows)

## Mutating routes (NOT probed — read-only audit)
For inventory completeness, the following POST endpoints exist but were intentionally skipped:
- `POST /api/ops/pause`, `POST /api/ops/resume` (kill-switch)
- `POST /api/ops/regime_config`, `POST /api/ops/risk_gates` (config writers)
- `POST /api/ops/rebalance` (rebalancer)
- `POST /api/ops/mcp/{tool_name}` (generic MCP tool dispatch)
- `POST /api/v4/adapters/{adapter_id}/rollback` (LoRA rollback)
