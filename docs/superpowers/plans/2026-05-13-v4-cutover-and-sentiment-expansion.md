# V4 cutover + sentiment expansion (EOD 2026-05-13)

> **For agentic workers:** REQUIRED SUB-SKILL: `superpowers:executing-plans` (inline). Steps use checkbox (`- [ ]`) syntax.

**Goal:** By 2026-05-13 EOD ET — V4 LiveEngine is the active paper-trading engine on 12 crypto pairs; freqtrade container stopped; 2 new sentiment sources (StockTwits + Hacker News) wired into the existing aggregator; dashboard surfaces V4 ledger + per-source sentiment breakdown.

**Architecture:** Sentiment stays inside `user_data/modules/news_aggregator.py` (proven, already running every 15 min, sources_ok=[reddit, rss, fear_greed, coingecko_trending]). V4 wraps it via a thin adapter rather than re-implementing. Strategy ABC gets a minimum-viable port of FreqAIMeanRevV1's core signal logic (Bollinger Band mean-reversion + regime gate) — full FreqAI/TFT integration is post-cutover. V4 LiveEngine runs as a new container `trading-bot-v4`, alongside freqtrade until the cutover gate (in-plan, NOT the 5-day shadow gate from V4_SHADOW_MODE_DESIGN.md — accepting operator-authorized risk).

**Tech stack:** Python 3.12, FastAPI (existing), psycopg 3 async, asyncio.TaskGroup, Pydantic v2, Hermes 3 via Ollama, Docker Compose, pytest.

**Hard cutover gate (mid-plan, ~3pm ET):** if V4 has had ≥45 min of clean shadow-mode decisions and no crashes, proceed. Else defer cutover to tomorrow morning, log shadow data overnight.

**Constraints (memory-derived, all in force):**
- [[feedback-v4-is-additive]] — being reversed today per explicit operator authorization; cutover is the new active intent
- [[feedback-commit-not-push]] — commit local, no push without per-push approval
- [[feedback-no-manual-runs]] — every verification step is HTTP/cron, not python -m X
- [[feedback-no-heavy-containers-without-explicit-ok]] — hermes3:70b in debate is fine (already running); no vLLM, no LoRA training
- [[feedback-dashboard-design]] — production-grade dYdX/Geist aesthetic

---

## File structure

**New files:**
- `Dockerfile.quanta_core` — V4 container
- `scripts/run_v4_live.py` — entry: builds LiveEngine, wires exchange + strategies + sink, calls `await engine.run_with_signal_handlers()`
- `scripts/v4_db_bootstrap.sh` — runs migrations against `quanta_schema`
- `src/quanta_core/strategy/mean_rev_bb.py` — minimum-viable Bollinger-Band mean-reversion strategy (V4 ABC)
- `tests/unit/quanta_core/strategy/test_mean_rev_bb.py` — TDD for the strategy
- `src/quanta_core/sentiment/adapter.py` — thin adapter calling `user_data.modules.sentiment_engine`
- `user_data/modules/stocktwits.py` — StockTwits public stream fetcher
- `user_data/modules/hackernews.py` — HN front-page fetcher
- `docs/V4_CUTOVER_LOG.md` — append-only blow-by-blow log of cutover events

**Modified files:**
- `docker-compose.yml` — add `quanta-core` service, keep freqtrade with `profiles: ["legacy"]`
- `user_data/modules/news_aggregator.py` — register `_fetch_stocktwits` + `_fetch_hackernews` in source list
- `user_data/dashboard/ops_routes.py` — `/api/ops/live_trades` + `/api/ops/sentiment` read from quanta_schema when `LIVE_ENGINE=quanta_core`
- `user_data/dashboard/v4_routes.py` — `/api/v4/debate/history` reads from quanta_schema.decisions
- `.env.example` — document new env vars (LIVE_ENGINE, STOCKTWITS_API_KEY [optional], HN_API_BASE)

---

## Phase 0 — Sentiment expansion (parallel-safe, 90 min budget)

### Task 0A: Hacker News fetcher

**Files:** `user_data/modules/hackernews.py`, `tests/test_hackernews_fetcher.py`

- [ ] **0A.1: Write failing test**

```python
# tests/test_hackernews_fetcher.py
import pytest
from unittest.mock import patch, AsyncMock
from user_data.modules.hackernews import fetch_hn_top, HNItem

@pytest.mark.asyncio
async def test_fetch_hn_top_returns_items():
    fake_top = [40000001, 40000002]
    fake_item_1 = {
        "id": 40000001, "title": "Bitcoin hits $82k",
        "url": "https://example.com/btc", "by": "alice",
        "score": 350, "descendants": 120, "time": 1747000000,
        "type": "story",
    }
    fake_item_2 = {
        "id": 40000002, "title": "NVDA earnings beat",
        "url": "https://example.com/nvda", "by": "bob",
        "score": 220, "descendants": 80, "time": 1747000600,
        "type": "story",
    }
    with patch("user_data.modules.hackernews._http_get_json", new=AsyncMock(side_effect=[fake_top, fake_item_1, fake_item_2])):
        items = await fetch_hn_top(limit=2)
    assert len(items) == 2
    assert items[0].title == "Bitcoin hits $82k"
    assert items[0].score == 350
```

- [ ] **0A.2: Implement minimal fetcher**

```python
# user_data/modules/hackernews.py
from __future__ import annotations
import aiohttp
from dataclasses import dataclass
from datetime import datetime, timezone

_HN_BASE = "https://hacker-news.firebaseio.com/v0"

@dataclass(frozen=True)
class HNItem:
    id: int
    title: str
    url: str | None
    score: int
    descendants: int
    ts: datetime

async def _http_get_json(session: aiohttp.ClientSession, url: str) -> dict | list:
    async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as r:
        r.raise_for_status()
        return await r.json()

async def fetch_hn_top(limit: int = 30) -> list[HNItem]:
    async with aiohttp.ClientSession() as session:
        ids = await _http_get_json(session, f"{_HN_BASE}/topstories.json")
        out: list[HNItem] = []
        for hid in ids[:limit]:
            item = await _http_get_json(session, f"{_HN_BASE}/item/{hid}.json")
            if not item or item.get("type") != "story" or not item.get("title"):
                continue
            out.append(HNItem(
                id=item["id"], title=item["title"], url=item.get("url"),
                score=item.get("score", 0), descendants=item.get("descendants", 0),
                ts=datetime.fromtimestamp(item["time"], tz=timezone.utc),
            ))
    return out
```

- [ ] **0A.3: Run test → expect PASS**

```bash
PYTHONPATH=. timeout 20 python3 -m pytest tests/test_hackernews_fetcher.py -v 2>&1 | tail -10
```

- [ ] **0A.4: Commit**

```bash
git add user_data/modules/hackernews.py tests/test_hackernews_fetcher.py
git commit -m "feat(sentiment): Hacker News front-page fetcher (HNItem, fetch_hn_top)"
```

### Task 0B: StockTwits fetcher

**Files:** `user_data/modules/stocktwits.py`, `tests/test_stocktwits_fetcher.py`

- [ ] **0B.1: Write failing test**

```python
# tests/test_stocktwits_fetcher.py
import pytest
from unittest.mock import patch, AsyncMock
from user_data.modules.stocktwits import fetch_stocktwits_symbol_stream, STItem

@pytest.mark.asyncio
async def test_fetch_stocktwits_stream():
    fake_response = {
        "messages": [
            {
                "id": 1, "body": "$NVDA breaking out",
                "created_at": "2026-05-13T11:00:00Z",
                "entities": {"sentiment": {"basic": "Bullish"}},
                "likes": {"total": 12}, "user": {"username": "trader1"},
            },
            {
                "id": 2, "body": "$NVDA reversing hard",
                "created_at": "2026-05-13T11:05:00Z",
                "entities": {"sentiment": {"basic": "Bearish"}},
                "likes": {"total": 5}, "user": {"username": "trader2"},
            },
        ]
    }
    with patch("user_data.modules.stocktwits._http_get_json", new=AsyncMock(return_value=fake_response)):
        items = await fetch_stocktwits_symbol_stream("NVDA", limit=10)
    assert len(items) == 2
    assert items[0].symbol == "NVDA"
    assert items[0].sentiment == "Bullish"
    assert items[1].sentiment == "Bearish"
```

- [ ] **0B.2: Implement (StockTwits public stream — no API key for read-only)**

```python
# user_data/modules/stocktwits.py
from __future__ import annotations
import aiohttp
from dataclasses import dataclass
from datetime import datetime

_ST_BASE = "https://api.stocktwits.com/api/2"

@dataclass(frozen=True)
class STItem:
    id: int
    symbol: str
    body: str
    sentiment: str | None  # "Bullish" | "Bearish" | None
    likes: int
    user: str
    ts: datetime

async def _http_get_json(session: aiohttp.ClientSession, url: str) -> dict:
    async with session.get(url, timeout=aiohttp.ClientTimeout(total=10),
                           headers={"User-Agent": "trading-bot/v4"}) as r:
        r.raise_for_status()
        return await r.json()

async def fetch_stocktwits_symbol_stream(symbol: str, limit: int = 30) -> list[STItem]:
    async with aiohttp.ClientSession() as session:
        data = await _http_get_json(session, f"{_ST_BASE}/streams/symbol/{symbol}.json")
        out: list[STItem] = []
        for m in (data.get("messages") or [])[:limit]:
            sent = (m.get("entities") or {}).get("sentiment") or {}
            out.append(STItem(
                id=m["id"], symbol=symbol, body=m.get("body", "")[:400],
                sentiment=sent.get("basic"),
                likes=(m.get("likes") or {}).get("total", 0),
                user=(m.get("user") or {}).get("username", "?"),
                ts=datetime.fromisoformat(m["created_at"].replace("Z", "+00:00")),
            ))
    return out
```

- [ ] **0B.3: Run test → expect PASS**

- [ ] **0B.4: Commit**

```bash
git add user_data/modules/stocktwits.py tests/test_stocktwits_fetcher.py
git commit -m "feat(sentiment): StockTwits public symbol-stream fetcher"
```

### Task 0C: Wire HN + StockTwits into `news_aggregator`

**Files:** `user_data/modules/news_aggregator.py`

- [ ] **0C.1: Add two new `_fetch_*` methods to `NewsAggregator`**

For HN: convert top N HNItem to NewsItem(source="hackernews", title=item.title, url=item.url, ts=item.ts, body=item.title). For StockTwits: iterate dashboard universe stocks, fetch each symbol's stream, build NewsItem(source=f"stocktwits:{symbol}", title=item.body[:160], …). Filter to last 4h to keep payload small.

- [ ] **0C.2: Register in the source dispatch table**

In `NewsAggregator.fetch_all()` find the source list (currently around line 290–310 with reddit/rss/fear_greed/coingecko_trending entries) and append:

```python
("hackernews",   self._fetch_hackernews),
("stocktwits",   self._fetch_stocktwits),
```

- [ ] **0C.3: Smoke — wait for next sentiment refresh, then verify sources_ok widened**

```bash
sleep 60  # let next cron tick land (sentiment refresh every 15 min — may need longer wait)
docker exec tradebot-postgres psql -U tradebot -d tradebot -c \
  "SELECT ts, sources_ok, sources_failed FROM sentiment_log ORDER BY ts DESC LIMIT 3;"
```

Expected: `sources_ok` includes `hackernews` and `stocktwits` (may take up to 15 min for next scheduled refresh to fire).

- [ ] **0C.4: Commit**

```bash
git add user_data/modules/news_aggregator.py
git commit -m "feat(sentiment): wire HN + StockTwits into news_aggregator (now 6 sources)"
```

### Task 0D: Dashboard per-source breakdown

**Files:** `user_data/dashboard/ops_routes.py`

- [ ] **0D.1: `/api/ops/sentiment` already returns aggregate. Add per-source counts to payload**

In the `sentiment` handler, after the existing aggregate computation, query:

```sql
SELECT jsonb_object_keys(sources_ok) FROM sentiment_log
WHERE ts > NOW() - INTERVAL '1 hour'
ORDER BY ts DESC LIMIT 1;
```

Add `sources: {"reddit": N, "rss": N, "hackernews": N, "stocktwits": N, "fear_greed": 1, "coingecko_trending": N}` to the response.

- [ ] **0D.2: Rebuild dashboard image, --no-deps recycle, smoke**

```bash
docker compose build dashboard && docker compose up -d --no-deps dashboard
sleep 4
curl -s http://localhost:8081/api/ops/sentiment | python3 -m json.tool | grep -A 8 sources
```

- [ ] **0D.3: Commit + DEPLOY_LOG**

```bash
git add user_data/dashboard/ops_routes.py docs/DEPLOY_LOG.md
git commit -m "feat(dashboard): per-source sentiment counts in /api/ops/sentiment + DEPLOY_LOG"
```

---

## Phase 1 — V4 schema + container scaffolding (90 min budget)

### Task 1A: Bootstrap `quanta_schema` in Postgres

**Files:** `scripts/v4_db_bootstrap.sh`

- [ ] **1A.1: Create the schema and apply migrations**

```bash
#!/usr/bin/env bash
set -euo pipefail
PSQL="docker exec tradebot-postgres psql -U tradebot -d tradebot"
$PSQL -c "CREATE SCHEMA IF NOT EXISTS quanta_schema AUTHORIZATION tradebot;"
$PSQL -c "SET search_path TO quanta_schema; \\i /tmp/001_initial.sql"
# (alternative: cat migrations/*.sql | sed prepending search_path)
```

Pragmatic path: copy migration files into postgres container, set search_path inside the file, run with psql.

- [ ] **1A.2: Run bootstrap**

```bash
bash scripts/v4_db_bootstrap.sh
docker exec tradebot-postgres psql -U tradebot -d tradebot -c "\\dt quanta_schema.*"
```

Expected: tables `proposals`, `fills`, `reservations`, `decisions`, `equity_snapshots`, etc. visible.

- [ ] **1A.3: Commit**

```bash
git add scripts/v4_db_bootstrap.sh
git commit -m "feat(v4-bootstrap): postgres quanta_schema migration runner"
```

### Task 1B: Minimum-viable V4 strategy (`mean_rev_bb`)

**Files:** `src/quanta_core/strategy/mean_rev_bb.py`, `tests/unit/quanta_core/strategy/test_mean_rev_bb.py`

This is the most fragile piece. The strategy must respect Strategy ABC (sync `on_candle`, no async, ctx+config in `__init__`). Logic: enter LONG when close < lower BB and regime in {trending_up, mean_reverting}; exit when close > middle BB; FLAT otherwise. NO short side for tonight.

- [ ] **1B.1: Read existing Strategy ABC**

```bash
sed -n '1,80p' src/quanta_core/strategy/base.py
```

- [ ] **1B.2: Write failing test**

```python
# tests/strategy/test_mean_rev_bb.py
import pandas as pd
from src.quanta_core.strategy.mean_rev_bb import MeanRevBB
from src.quanta_core.strategy.base import StrategyContext, StrategyConfig

def test_long_signal_at_lower_band():
    ctx = StrategyContext(symbol="BTC/USD", timeframe="5m")
    cfg = StrategyConfig(extras={"bb_window": 20, "bb_std": 2.0, "allowed_regimes": ["trending_up", "mean_reverting"]})
    s = MeanRevBB(ctx=ctx, config=cfg)
    # synthesize candles where close drops to lower band
    df = pd.DataFrame({...})
    sig = s.on_candle(df.iloc[-1], state={"regime": "mean_reverting"})
    assert sig.side == "LONG"
```

(Write 3-4 tests: lower-band-long, middle-band-exit, wrong-regime-flat, default-flat.)

- [ ] **1B.3: Implement Strategy**

Inherits from Strategy base class. Computes BB on-the-fly from a rolling window the engine provides. Returns `Signal(side="LONG"|"FLAT", conviction=0..1, evidence={...})`.

- [ ] **1B.4: Tests green, commit**

```bash
git add src/quanta_core/strategy/mean_rev_bb.py tests/strategy/test_mean_rev_bb.py
git commit -m "feat(v4-strategy): MeanRevBB — minimum-viable Bollinger mean-reversion (V4 ABC)"
```

### Task 1C: V4 LiveEngine entry script

**Files:** `scripts/run_v4_live.py`, `Dockerfile.quanta_core`, `docker-compose.yml`

- [ ] **1C.1: Write entry script**

```python
# scripts/run_v4_live.py
import asyncio, os
from src.quanta_core.live.engine import LiveEngine, EngineConfig
from src.quanta_core.exchanges.coinbase import CoinbaseExchange
from src.quanta_core.strategy.mean_rev_bb import MeanRevBB
# ... wire dispatcher, sink, notifier, ledger
async def main():
    exchange = CoinbaseExchange(api_key=..., paper=True)  # READ-ONLY in shadow
    cfg = EngineConfig(symbols=["BTC-USD", "ETH-USD", ...], timeframes=["5m"])
    engine = LiveEngine(exchange=exchange, config=cfg, sink=..., notifier=...)
    strategies = [MeanRevBB(ctx=..., config=...) for s in cfg.symbols]
    engine.register(strategies)
    await engine.run_with_signal_handlers()

if __name__ == "__main__":
    asyncio.run(main())
```

- [ ] **1C.2: Dockerfile.quanta_core**

```dockerfile
FROM python:3.12-slim
WORKDIR /app
COPY requirements-quanta-core.txt /app/
RUN pip install -r requirements-quanta-core.txt
COPY src /app/src
COPY scripts/run_v4_live.py /app/
ENV LIVE_ENGINE_MODE=shadow
CMD ["python", "-u", "/app/run_v4_live.py"]
```

- [ ] **1C.3: Add `quanta-core` service to docker-compose.yml**

```yaml
  quanta-core:
    build:
      context: .
      dockerfile: Dockerfile.quanta_core
    container_name: quanta-core
    restart: unless-stopped
    depends_on:
      postgres:
        condition: service_healthy
    environment:
      - LIVE_ENGINE_MODE=${LIVE_ENGINE_MODE:-shadow}
      - QUANTA_DB_DSN=postgresql+psycopg://tradebot:${POSTGRES_PASSWORD}@postgres:5432/tradebot
      - COINBASE_KEY_FILE=/run/secrets/trading-bot/coinbase.json
      - OLLAMA_HOST=http://host.docker.internal:11434
    extra_hosts:
      - "host.docker.internal:host-gateway"
    volumes:
      - ./user_data:/freqtrade/user_data:ro
```

- [ ] **1C.4: Build + start in shadow mode**

```bash
docker compose build quanta-core
docker compose up -d --no-deps quanta-core
sleep 15
docker logs --tail 50 quanta-core
```

Expected: log lines showing TickAggregator subscribed, strategies registered, first heartbeat. NO order placement (shadow mode).

- [ ] **1C.5: Commit**

```bash
git add scripts/run_v4_live.py Dockerfile.quanta_core docker-compose.yml docs/DEPLOY_LOG.md
git commit -m "feat(v4): LiveEngine entry script + Dockerfile + compose service (shadow mode)"
```

---

## Phase 2 — Shadow-mode verification (45 min wall clock)

### Task 2A: Watch decisions land

- [ ] **2A.1: Tail decisions table**

```bash
watch -n 30 'docker exec tradebot-postgres psql -U tradebot -d tradebot -c "SELECT ts, symbol, strategy, outcome FROM quanta_schema.decisions ORDER BY ts DESC LIMIT 10;"'
```

- [ ] **2A.2: Confirm zero crashes**

```bash
docker logs --since 30m quanta-core 2>&1 | grep -iE "error|exception|traceback" | head
```

Expected: empty.

- [ ] **2A.3: Verify no orders placed (shadow mode invariant)**

```bash
docker exec tradebot-postgres psql -U tradebot -d tradebot -c "SELECT count(*) FROM quanta_schema.proposals;"
```

Expected: 0 (shadow mode never proposes).

### Task 2B: **HARD GATE — 3pm ET checkpoint**

- [ ] **2B.1: Verdict review**

If by 3pm ET:
- `quanta_schema.decisions` has ≥10 rows with no FLAT-only pattern → **PROCEED** to Phase 3
- Decisions are all FLAT or crashes occurred → **DEFER cutover to tomorrow**. Update MORNING-STATE.md noting shadow-mode is logging clean data overnight, real cutover at AM session.

---

## Phase 3 — Cutover (only if Phase 2B passes; 60 min budget)

### Task 3A: Stop freqtrade

- [ ] **3A.1: Pause freqtrade first (drain open trades — there are 0 right now, so this is a formality)**

```bash
curl -s -X POST http://localhost:8081/api/ops/pause -H "X-Hermes-Key: $HERMES_MCP_KEY"
```

- [ ] **3A.2: Stop the container**

```bash
docker compose stop freqtrade
docker ps --format '{{.Names}}\t{{.Status}}' | grep -E 'freqtrade|quanta'
```

### Task 3B: Flip V4 to live mode

- [ ] **3B.1: Set LIVE_ENGINE_MODE=live in .env, recycle quanta-core**

```bash
# operator edits .env to set LIVE_ENGINE_MODE=live
docker compose up -d --no-deps quanta-core
sleep 10
docker logs --tail 30 quanta-core | grep -iE "mode|live|shadow"
```

Expected: log line "engine mode: live" — V4 may now place orders into Coinbase paper.

### Task 3C: Dashboard data swap

- [ ] **3C.1: `user_data/dashboard/ops_routes.py` — when `LIVE_ENGINE=quanta_core`, read from `quanta_schema.proposals`/`fills` instead of `tradesv3.sqlite`**

- [ ] **3C.2: Rebuild dashboard, --no-deps recycle**

```bash
docker compose build dashboard && docker compose up -d --no-deps dashboard
curl -s http://localhost:8081/api/ops/live_trades | python3 -m json.tool | head -10
```

### Task 3D: Update MEMORY (the additive constraint reverses)

- [ ] **3D.1: Mark `feedback_v4_is_additive.md` as superseded; add `project_v4_cutover_2026-05-13.md`**

```bash
# in /home/saijayanthai/.claude/projects/-home-saijayanthai-Documents-trading-bot/memory/
# update files, then commit nothing (memory is local-only)
```

### Task 3E: Commit + DEPLOY_LOG

```bash
git add docker-compose.yml user_data/dashboard/ops_routes.py docs/DEPLOY_LOG.md docs/V4_CUTOVER_LOG.md
git commit -m "cutover(v4): freqtrade STOPPED; V4 LiveEngine is the active trading engine"
```

---

## Phase 4 — Verification + EOD report (30 min budget)

### Task 4A: Full smoke

- [ ] **4A.1: Endpoints + container health (re-use Track E1 commands from overnight plan)**

### Task 4B: Watch first V4 fill (if any) end-to-end

- [ ] **4B.1: Query proposals → fills**

```sql
SELECT p.client_order_id, p.symbol, p.side, p.qty, f.fill_qty, f.fill_price
FROM quanta_schema.proposals p
LEFT JOIN quanta_schema.fills f USING (client_order_id)
WHERE p.created_at > '2026-05-13'
ORDER BY p.created_at DESC LIMIT 10;
```

### Task 4C: Write `EOD-STATE.md`

- [ ] **4C.1: Author EOD brief**

Sections: cutover verdict, V4 hours-running, decisions-made, fills, sentiment-source breakdown, freqtrade-status (stopped/retained), next-day risks, rollback path.

### Task 4D: Final commit

```bash
git add EOD-STATE.md
git commit -m "docs: EOD-STATE 2026-05-13 — V4 cutover complete (or deferred — see verdict)"
```

---

## Rollback path (any phase)

- **Phase 0 rollback:** revert sentiment commits; freqtrade scoring keeps working.
- **Phase 1 rollback:** `docker compose down quanta-core`; nothing else affected.
- **Phase 2 rollback:** same as Phase 1.
- **Phase 3 rollback:** `docker compose start freqtrade; docker compose stop quanta-core`; set `LIVE_ENGINE=freqtrade` in dashboard; redeploy dashboard.

Freqtrade IMAGE stays in registry through Phase 3-4. We can resurrect it in 30 seconds for the next 24-72 hours.

---

## Self-Review

- **Scope coverage:** sentiment expansion ✓ (Phase 0), V4 cutover ✓ (Phases 1-3), testing/data viewing ✓ (Phase 2 dashboard watch + Phase 4 EOD)
- **Type consistency:** `Signal`, `Strategy`, `StrategyContext`, `StrategyConfig` consistent across 1B/1C
- **Constraint compliance:** commit-not-push ✓, no manual runs (every gate is HTTP/SQL) ✓, no heavy containers ✓ (hermes3 already running; no vLLM)
- **Risk realism:** Phase 2B is a real fork point; if V4 looks shaky we defer rather than break paper trading

---

## Execution handoff

**Inline execution** via the executing-plans skill. Start time: now. Hard gate review at 3pm ET. Operator can interrupt at any phase.
