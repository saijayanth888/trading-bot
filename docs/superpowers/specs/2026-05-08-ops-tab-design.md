# Ops Tab — Unified operations panel for the trading-bot dashboard

**Date:** 2026-05-08
**Status:** Approved (operator review pending)
**Lives in:** existing FastAPI dashboard at `localhost:8081`, new `/ops` route

## 1. Goal

Give the operator one screen that answers, at a glance:

- Are all services healthy?
- What is the system learning right now (TFT epoch, DRL status, EPT generation)?
- What regime are we in, and what does sentiment say?
- Is the MCP wire alive?
- What's open, what's the drawdown, is the risk governor calm?

And lets the operator pause / resume paper trading with one click.

Read-only for everything else. The dashboard is the eyes; controls stay on the CLI / Hermes Agent for now.

## 2. Non-goals

- **Not** a service installer / starter for ollama, hermes-mcp, hermes-gateway, or docker compose. Those have their own systemd units; the Ops tab does not bring them up. (User declined this scope.)
- **Not** a trigger surface for EPT cycles or model retrains. (User declined; crons handle this.)
- **Not** authenticated beyond the existing dashboard auth. The dashboard binds to loopback only; pause/resume are protected by the existing freqtrade API credentials.
- **Not** a WebSocket dashboard. Polling is sufficient at the cadences chosen (5–30 s per panel).

## 3. Architecture

```
   Browser (loopback only)
        │  HTTP polling (5–30 s per panel)
        ▼
   FastAPI dashboard (port 8081, container "dashboard")
   ├─ existing routes: / /api/pairs /api/mode /api/candles /api/trades /api/state
   └─ NEW routes:
       ├─ GET /ops                      (HTML page)
       ├─ GET /api/ops/services         (TCP/HTTP probes + heartbeat file)
       ├─ GET /api/ops/training         (parse freqtrade logs + EPT json)
       ├─ GET /api/ops/regime           (Postgres regime_log)
       ├─ GET /api/ops/sentiment        (Postgres sentiment_log)
       ├─ GET /api/ops/mcp              (probe :8089/mcp + tail mcp log)
       ├─ GET /api/ops/trades_risk      (freqtrade API + Postgres)
       │   (also surfaces the "live tape" — last 5 trade-journal rows)
       ├─ POST /api/ops/pause           (calls freqtrade /api/v1/stop, journals reason)
       └─ POST /api/ops/resume          (calls freqtrade /api/v1/start, requires confirm)
```

The dashboard container needs **no new permissions**. All data sources are reachable on the existing docker network or are file-system reads of `user_data/logs/`.

For the one service without an HTTP port (`hermes-gateway`), the gateway writes a heartbeat file `/tmp/hermes-gateway.alive` every 30 s (small wrapper around its event loop); the dashboard checks `mtime` on that file. If we cannot patch the gateway, fallback is `systemctl is-active hermes-gateway` invoked via a one-line shell helper that writes its result to `/tmp/hermes-gateway.alive` from a tiny systemd timer. Either path keeps the dashboard container free of host-systemd access.

## 4. Layout (single screen, no scrolling)

```
┌─ /ops ─────────────────────────────────────────────────────┐
│ ╔══════════════════════════════════════════════════════╗   │
│ ║ REGIME (hero tile, color-coded by regime)            ║   │
│ ║ trending_up ↑   prob 0.78   ·   active 4h 12m        ║   │
│ ║ sentiment: -0.35 (bearish · conf 0.60 · 2/2 agree)   ║   │
│ ╚══════════════════════════════════════════════════════╝   │
│ ┌─ services ──┐ ┌─ training ─┐ ┌─ MCP ────┐                │
│ │ ✓ ollama    │ │ TFT 12/25  │ │ ✓ /mcp   │                │
│ │ ✓ mcp       │ │ DRL  n/a   │ │ tools 15 │                │
│ │ ✓ gateway   │ │ EPT gen 1  │ │ last tx  │                │
│ │ ✓ freqtrade │ │ champ #4   │ │  14:12   │                │
│ │ ✓ postgres  │ └────────────┘ └──────────┘                │
│ │ ✓ dashboard │                                            │
│ │ ✓ influxdb  │                                            │
│ │ ✓ grafana   │                                            │
│ └─────────────┘                                            │
│ ┌─ trades + risk ─────────────────────────────────────┐    │
│ │ open: 0   ·   daily P&L: $0.00   ·   DD: 0%         │    │
│ │ breaker: clear   ·   positions: 0/6                 │    │
│ │ live tape: — none yet — (TFT not ready)             │    │
│ └─────────────────────────────────────────────────────┘    │
│ [ ⏸ Pause Trading ]   [ ▶ Resume Trading (confirm) ]       │
└─────────────────────────────────────────────────────────────┘
```

**Regime tile color mapping:**
- `trending_up` → green
- `trending_down` → red
- `mean_reverting` → amber
- `high_volatility` → purple

**State badges per panel:** green = ok, amber = degraded, red = down. A panel is amber if its data is stale (regime > 15 min, sentiment > 30 min, training log > 60 min, services last-probe > 30 s) or if a sub-source is missing. Red only if the primary source is unreachable.

## 5. Endpoint contracts

Every endpoint returns:

```json
{
  "status": "ok" | "degraded" | "down",
  "data": { ... },
  "error": null | "human-readable string",
  "checked_at": "2026-05-08T14:12:00Z"
}
```

### 5.1 GET /api/ops/services
```json
{
  "status": "ok",
  "data": {
    "ollama":           {"up": true, "via": "tcp", "endpoint": "ollama:11434"},
    "hermes_mcp":       {"up": true, "via": "http", "endpoint": "http://hermes-mcp:8089/mcp", "code": 406},
    "hermes_gateway":   {"up": true, "via": "heartbeat", "age_s": 12},
    "hermes_dashboard": {"up": true, "via": "http", "endpoint": "http://host.docker.internal:9119/", "code": 200},
    "freqtrade":        {"up": true, "via": "http", "endpoint": "http://freqtrade:8080/api/v1/ping", "code": 200},
    "postgres":         {"up": true, "via": "tcp", "endpoint": "postgres:5432"},
    "influxdb":         {"up": true, "via": "http", "endpoint": "http://influxdb:8086/health"},
    "grafana":          {"up": true, "via": "http", "endpoint": "http://grafana:3000/api/health"}
  }
}
```

### 5.2 GET /api/ops/training
```json
{
  "status": "ok",
  "data": {
    "tft": {"epoch": 12, "max_epoch": 25, "val_sharpe": 0.91, "loss": 1.10, "log_age_s": 45},
    "drl": {"status": "n/a", "note": "no drl_status.json yet"},
    "ept": {"generation": 1, "champion_id": "agent-4", "champion_sharpe": 1.42, "ts_age_s": 1200}
  }
}
```

TFT data: tail `docker compose logs --tail=200 freqtrade` for `TFTModel - INFO - epoch N/M loss=X val_sharpe=Y`. EPT data: read last entry of `user_data/logs/evolution.json`. DRL: optional file `user_data/logs/drl_status.json`; if missing, return `n/a`.

### 5.3 GET /api/ops/regime
```json
{
  "status": "ok",
  "data": {
    "current": "trending_up",
    "probability": 0.78,
    "duration_hours": 4.2,
    "ts": "2026-05-08T16:00:00Z",
    "transitions_24h": [
      {"ts": "...", "regime": "mean_reverting", "duration_h": 2.1},
      {"ts": "...", "regime": "trending_up",    "duration_h": 4.2}
    ]
  }
}
```

Reads `regime_log` table — column names: `regime`, `probability`, `regime_duration_hours`, `ts`. Note: schema uses `probability` not `confidence` (the latter exists in the spec doc but not the actual DDL).

### 5.4 GET /api/ops/sentiment
```json
{
  "status": "ok",
  "data": {
    "score": -0.35,
    "confidence": 0.60,
    "agreement": true,
    "n_headlines": 4,
    "ts": "2026-05-08T21:09:45Z",
    "hourly_24h": [{"hour": "2026-05-08T20:00:00Z", "score": 0.12, "n": 3}, ...]
  }
}
```

### 5.5 GET /api/ops/mcp
```json
{
  "status": "ok",
  "data": {
    "endpoint":  "http://hermes-mcp:8089/mcp",
    "transport": "streamable-http",
    "probe":     {"code": 406, "ok_for_streamable_http": true},
    "tools_count": 15,
    "last_call":   {"tool": "get_risk_status", "ts": "2026-05-08T14:12:00Z", "result": "ok"}
  }
}
```

`tools_count` and `last_call` come from tailing `user_data/logs/hermes_mcp.log`. If the log is absent, those fields are null and the panel renders amber.

### 5.6 GET /api/ops/trades_risk
```json
{
  "status": "ok",
  "data": {
    "open_trades": [
      {"pair": "BTC/USD", "side": "long", "entry": 67410.0, "current": 67890.0, "pnl_pct": 0.71, "duration_min": 22}
    ],
    "open_count": 1,
    "max_open": 6,
    "daily_pnl_usd": 14.20,
    "daily_pnl_pct": 0.07,
    "drawdown_pct": 0.0,
    "circuit_breaker": {"active": false, "cooldown_remaining_min": 0},
    "dry_run": true
  }
}
```

Open trades from freqtrade API `/api/v1/status`. Daily P&L from `trade_journal` aggregation. Drawdown + breaker from existing `risk_governor.get_status()` accessed via direct DB read.

### 5.7 POST /api/ops/pause
Body: `{"reason": "operator note (optional)"}`
- Calls freqtrade `/api/v1/stop`
- Writes a `trade_journal` entry: `event="ops_pause"`, `reason="ops-tab-manual: <note>"`
- Returns `{"status": "ok", "data": {"dry_run": true, "stopped_at": "..."}}`

### 5.8 POST /api/ops/resume
Body: `{"confirm": true, "reason": "..."}` — `confirm` MUST be true.
- Re-checks risk_governor.get_status() — if drawdown > 6% or circuit breaker active, refuses with HTTP 409.
- Calls freqtrade `/api/v1/start`
- Writes journal entry `event="ops_resume"`
- Returns `{"status": "ok", "data": {"resumed_at": "..."}}`

## 6. Data flow (single panel example: regime)

1. Frontend `setInterval(30_000)` fires fetch on `/api/ops/regime`.
2. FastAPI handler runs SQL: `SELECT regime, probability, regime_duration_hours, ts FROM regime_log ORDER BY ts DESC LIMIT 1` (with a 2 s `statement_timeout`).
3. Plus a second query for last-24h transitions (window function: `regime != LAG(regime) OVER (ORDER BY ts)`).
4. If both succeed → `status=ok`. If primary returns 0 rows → `status=degraded` ("regime detector hasn't written yet"). If query times out / connection refused → `status=down`.
5. Frontend renders the hero tile: regime label (large), probability (gauge), duration (countdown), and applies the regime-color background.
6. If `status=degraded`, hero tile gets an amber border and tooltip with `error`.

Same pattern for every panel.

## 7. Error handling

- **Per-endpoint timeout**: 2 s hard. Beyond that, return `{"status": "down", "error": "timeout"}`.
- **Frontend timeout**: 3 s on each fetch (1 s grace beyond the backend timeout). Panel goes amber with "no response in 3s".
- **Postgres connection refused**: regime, sentiment, trades_risk panels all go red simultaneously; not retriable from the dashboard side. The user investigates `docker compose ps` / `journalctl -u trading-bot.service`.
- **Pause/resume failure**: any non-2xx from freqtrade API returns the freqtrade error verbatim to the operator with `status=409` (conflict) or `503` (service unavailable). No silent swallowing.
- **Double-click protection**: the Pause button disables itself on click, re-enables only after the response (success or failure) lands. Same for Resume.
- **Resume confirmation**: Resume opens a modal showing current `tradable_balance_ratio`, last drawdown, last pause reason. Confirm requires typing the word `RESUME` (mirrors `pg_dump`-style guards).

## 8. Testing

1. **Endpoint unit tests** (`tests/test_ops_dashboard.py`):
   - mock postgres + freqtrade API; for each endpoint, assert envelope shape for ok / degraded / down inputs.
   - assert `/api/ops/pause` writes a `trade_journal` row and only calls freqtrade API once.
   - assert `/api/ops/resume` rejects when `confirm=false`, when drawdown > 6%, when breaker active.

2. **Frontend smoke** (single Playwright test, gated on chrome being installed): open `/ops`, verify all six panels render, click pause-then-resume against a stubbed backend, assert state transitions.

3. **Manual degraded-mode check** (documented in `CHECKLIST.md` later, not automated): `docker compose stop hermes-mcp` → MCP panel turns red within 30 s without crashing the page.

## 9. Open items / decided defaults

- **Heartbeat for hermes-gateway**: prefer the in-process heartbeat (small patch to gateway). If gateway internals are out of scope to patch, fall back to a 30 s systemd timer that writes `is-active` to the heartbeat file.
- **Live trade tape font**: monospace, terminal-style. No fancy charts in the trades panel — there's a separate Charts tab for that.
- **Backwards compat**: existing dashboard endpoints unchanged. `/ops` is purely additive.

## 10. Out of scope (deferred)

- Service start/stop buttons (operator chose not to take this on)
- EPT/training trigger buttons
- Telegram/Slack send-from-dashboard
- Multi-user auth or role separation
- Mobile / responsive layout (desktop-only is fine for now)

## 11. Implementation order (preview for the writing-plans phase)

1. Backend: data-source helper modules (`ops_probes.py`, `ops_db.py`) — pure functions, easy to test.
2. Backend: 8 new FastAPI endpoints, each one tested via `tests/test_ops_dashboard.py`.
3. Frontend: new `/ops` HTML template + JS that fetches and renders.
4. Polishing: CSS color states, regime hero tile colors, confirm modal.
5. Docs: update `CHECKLIST.md` to point operators at `/ops` for at-a-glance status.
