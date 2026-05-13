# Overnight V4 Wiring & Bug-Free Paper Trading

> **For agentic workers:** REQUIRED SUB-SKILL: Use `superpowers:subagent-driven-development` (recommended) or `superpowers:executing-plans` to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** By 2026-05-13 08:00 ET — freqtrade paper-trading still healthy + dashboard renders V4 surfaces as live data (additively) + dead grafana/influx code paths purged + a written shadow-mode design doc + parity-oracle scaffolding committed. All overnight, no heavy GPU ops, no freqtrade restarts unless mandatory.

**Architecture:** V4 stays additive ([[feedback-v4-is-additive]]). Tonight's work upgrades `user_data/dashboard/v4_routes.py` stub bodies to read from real V4 ledger writes / agent buffers / parity oracle output where available, and ships a `data/v4_runtime/` JSONL log surface that a *future* cron job will populate. The dashboard SPA already consumes `/api/v4/*`; this plan makes those payloads real instead of mocked. Freqtrade remains the live trading engine; paper trading runs untouched.

**Tech Stack:** FastAPI (existing `user_data/dashboard/`), Pydantic v2 (`src/quanta_core/types.py`), psycopg 3 async (`src/quanta_core/ledger/`), Hermes 3 via Ollama (already running), Docker (do NOT recycle freqtrade unless approved), pytest.

**Hard constraints (memory-derived):**
- [[feedback-v4-is-additive]] — never swap UIs/routes; `/api/ops/*` stays primary
- [[feedback-commit-not-push]] — commit to local main; do NOT push
- [[feedback-no-manual-runs]] — no `python -m X` recovery steps; all verification via cron/HTTP
- [[feedback-no-heavy-containers-without-explicit-ok]] — no vLLM, no model pulls >5 GB, no LoRA training
- [[feedback-dashboard-design]] — no shadows/gradients/serif-italic; production dYdX/Geist aesthetic
- [[reference-dashboard-deploy]] — source baked into image; rebuild + cache-bust required to ship CSS/JS changes — and `docker compose up -d dashboard` will recycle freqtrade unless `--no-deps`

---

## File Structure

**New files:**
- `data/v4_runtime/.gitkeep` — runtime data dir for V4 JSONL logs
- `data/v4_runtime/README.md` — what writes here, what reads here
- `docs/V4_SHADOW_MODE_DESIGN.md` — the freqtrade→V4 cutover design doc
- `src/quanta_core/observability/v4_buffer.py` — in-memory ring buffer + JSONL appender for debate sessions / parity diffs (so v4_routes can read live state without DB)
- `tests/unit/quanta_core/observability/test_v4_buffer.py` — TDD for the buffer

**Modified files:**
- `user_data/dashboard/v4_routes.py` — swap 5 stub handlers to read from `v4_buffer` with mock fallback
- `user_data/dashboard/ops_routes.py` — add `/api/ops/v4_status` health card row (read-only, lightweight)
- `docker-compose.yml` — remove dead grafana/influxdb stragglers (already mostly gone — finish the job)
- `tests/test_ops_dashboard.py` — drop influx assertions
- `user_data/modules/metrics_writer.py` + `monitoring_mixin.py` — neutralize InfluxDB writes (kill or no-op behind feature flag)
- `MORNING-STATE.md` — write the 2026-05-13 morning brief at end-of-execution

**Deleted (under explicit approval rule):**
- `grafana/` directory tree (provisioning + dashboards) — DO NOT delete tonight without operator sign-off — leave a `grafana/.deprecated_2026-05-12` marker file instead and stage delete for tomorrow

---

## Track A — Backend cleanup (lowest risk, start here)

### Task A1: Audit live grafana/influx references

**Files:**
- Read: `docker-compose.yml`
- Read: `user_data/modules/metrics_writer.py`
- Read: `user_data/modules/monitoring_mixin.py`
- Read: `tests/test_ops_dashboard.py`

- [ ] **Step 1: Grep + classify**

```bash
grep -rnE "grafana|influx" --include="*.py" --include="*.yml" --include="*.yaml" 2>/dev/null | grep -v '.claude/worktrees' > /tmp/grafana_refs.txt
wc -l /tmp/grafana_refs.txt
head -50 /tmp/grafana_refs.txt
```

Expected: ~20-40 lines. Each line goes into one of three buckets: **dead** (config-only, no runtime caller), **gated** (already behind an env flag), **live** (actually executes).

- [ ] **Step 2: Note classifications inline as code comments — DO NOT modify behavior yet**

Add a TODO marker like `# DEPRECATED 2026-05-13 — see V4_SHADOW_MODE_DESIGN.md` at the top of dead-code blocks. Do NOT delete code in this task — only annotate.

- [ ] **Step 3: Commit annotations**

```bash
git add docker-compose.yml user_data/modules/metrics_writer.py user_data/modules/monitoring_mixin.py tests/test_ops_dashboard.py
git commit -m "docs(cleanup): annotate dead grafana/influx call sites for staged removal"
```

### Task A2: Neutralize InfluxDB writer (no-op behind already-off flag)

**Files:**
- Modify: `user_data/modules/metrics_writer.py`
- Test: `tests/test_metrics_writer_noop.py` (new)

- [ ] **Step 1: Write failing test that asserts no network call when `INFLUX_ENABLED=0`**

```python
# tests/test_metrics_writer_noop.py
import os
import pytest
from unittest.mock import patch
from user_data.modules.metrics_writer import write_point

def test_write_point_is_noop_when_influx_disabled(monkeypatch):
    monkeypatch.setenv("INFLUX_ENABLED", "0")
    with patch("user_data.modules.metrics_writer._http_post") as http:
        write_point("trade", {"pair": "BTC/USD"}, value=1.0)
    http.assert_not_called()
```

- [ ] **Step 2: Run — expect failure (no `_http_post` symbol yet, or assertion fails)**

```bash
docker exec freqtrade pytest tests/test_metrics_writer_noop.py -x 2>&1 | tail -20
```

If freqtrade-exec doesn't work, run from host with `PYTHONPATH=. pytest ...`. Expected: FAIL with `_http_post not found` or `http.assert_not_called()` fails (currently writes always-on).

- [ ] **Step 3: Refactor `write_point` to short-circuit when env flag off**

```python
# user_data/modules/metrics_writer.py — add at top of write_point:
def write_point(measurement: str, tags: dict, value: float) -> None:
    if os.environ.get("INFLUX_ENABLED", "0") != "1":
        return
    _http_post(...)  # existing body
```

Wrap the existing HTTP call as `_http_post(...)` if not already factored.

- [ ] **Step 4: Re-run test → PASS**

- [ ] **Step 5: Commit**

```bash
git add user_data/modules/metrics_writer.py tests/test_metrics_writer_noop.py
git commit -m "chore(metrics): no-op InfluxDB writer when INFLUX_ENABLED!=1 (no network calls)"
```

### Task A3: Drop influx assertions from ops dashboard test

**Files:**
- Modify: `tests/test_ops_dashboard.py`

- [ ] **Step 1: Find influx-asserting tests**

```bash
grep -n "influx\|grafana" tests/test_ops_dashboard.py
```

- [ ] **Step 2: Remove influx-only assertions; preserve general dashboard health assertions**

Mark removed lines with one-line note in commit, not in code.

- [ ] **Step 3: Run test → PASS**

```bash
PYTHONPATH=. pytest tests/test_ops_dashboard.py -x 2>&1 | tail -10
```

- [ ] **Step 4: Commit**

```bash
git add tests/test_ops_dashboard.py
git commit -m "test(ops-dashboard): drop dead influx assertions (writer is no-op now)"
```

---

## Track B — V4 observability buffer (the live-data substrate)

### Task B1: Create `v4_buffer` module with ring buffer + JSONL appender

**Files:**
- Create: `src/quanta_core/observability/v4_buffer.py`
- Create: `tests/unit/quanta_core/observability/test_v4_buffer.py`
- Create: `data/v4_runtime/.gitkeep`
- Create: `data/v4_runtime/README.md`

- [ ] **Step 1: Write failing test for append + read_recent**

```python
# tests/unit/quanta_core/observability/test_v4_buffer.py
import json
from pathlib import Path
from src.quanta_core.observability.v4_buffer import V4Buffer

def test_append_then_read_recent(tmp_path: Path):
    buf = V4Buffer(jsonl_path=tmp_path / "debates.jsonl", capacity=4)
    buf.append({"kind": "debate", "session_id": "abc", "pair": "BTC/USD"})
    buf.append({"kind": "debate", "session_id": "def", "pair": "ETH/USD"})
    recent = buf.read_recent(limit=10)
    assert len(recent) == 2
    assert recent[0]["session_id"] == "abc"
    # JSONL persisted
    lines = (tmp_path / "debates.jsonl").read_text().strip().splitlines()
    assert json.loads(lines[0])["session_id"] == "abc"

def test_ring_buffer_bounded(tmp_path: Path):
    buf = V4Buffer(jsonl_path=tmp_path / "debates.jsonl", capacity=3)
    for i in range(5):
        buf.append({"i": i})
    recent = buf.read_recent(limit=10)
    assert [r["i"] for r in recent] == [2, 3, 4]  # oldest evicted from RAM
```

- [ ] **Step 2: Run → FAIL (module missing)**

```bash
PYTHONPATH=. pytest tests/unit/quanta_core/observability/test_v4_buffer.py -x 2>&1 | tail -15
```

- [ ] **Step 3: Implement minimal V4Buffer**

```python
# src/quanta_core/observability/v4_buffer.py
from __future__ import annotations
import json
from collections import deque
from pathlib import Path
from threading import Lock
from typing import Any

class V4Buffer:
    """In-memory ring + JSONL tail for V4 runtime observability.

    Writers (debate orchestrator, parity oracle, monte carlo) append events;
    /api/v4/* read_recent for live dashboard payloads. JSONL is the durable
    record; the ring is the fast path.
    """

    def __init__(self, jsonl_path: Path, capacity: int = 256) -> None:
        self._path = jsonl_path
        self._ring: deque[dict[str, Any]] = deque(maxlen=capacity)
        self._lock = Lock()
        self._path.parent.mkdir(parents=True, exist_ok=True)

    def append(self, event: dict[str, Any]) -> None:
        with self._lock:
            self._ring.append(event)
            with self._path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(event, default=str) + "\n")

    def read_recent(self, limit: int = 64) -> list[dict[str, Any]]:
        with self._lock:
            return list(self._ring)[-limit:]
```

- [ ] **Step 4: Run → PASS**

- [ ] **Step 5: Add `data/v4_runtime/README.md`**

```markdown
# V4 runtime data

JSONL event logs written by V4 modules:

- `debates.jsonl` — one event per debate phase fire (regime/micro/bull/bear/arbiter/reflect)
- `parity.jsonl` — backtest-vs-live decision diffs
- `montecarlo.jsonl` — one event per Monte Carlo run

Consumers:
- `user_data/dashboard/v4_routes.py` → `read_recent` for `/api/v4/*` payloads
- Future cron job at `scripts/v4_rotate_runtime.sh` will trim files >100 MB.

This directory is intentionally NOT mounted into the freqtrade container.
Only the dashboard container reads here.
```

- [ ] **Step 6: Commit**

```bash
git add src/quanta_core/observability/v4_buffer.py tests/unit/quanta_core/observability/test_v4_buffer.py data/v4_runtime/
git commit -m "feat(v4-observability): V4Buffer ring+JSONL substrate for live dashboard payloads"
```

---

## Track C — Wire `/api/v4/*` stubs to V4Buffer (with mock fallback)

### Task C1: Module-level singleton + helper

**Files:**
- Modify: `user_data/dashboard/v4_routes.py`

- [ ] **Step 1: Add singleton + helper near the top of v4_routes.py**

```python
# user_data/dashboard/v4_routes.py — add after imports
from src.quanta_core.observability.v4_buffer import V4Buffer

_V4_DATA_DIR = REPO_ROOT / "data" / "v4_runtime"
_DEBATE_BUFFER = V4Buffer(_V4_DATA_DIR / "debates.jsonl", capacity=256)
_PARITY_BUFFER = V4Buffer(_V4_DATA_DIR / "parity.jsonl", capacity=128)
_MONTECARLO_BUFFER = V4Buffer(_V4_DATA_DIR / "montecarlo.jsonl", capacity=64)

def _live_or_mock(buffer: V4Buffer, mock_fn, limit: int = 8) -> list[dict]:
    """Read live buffer; fall back to deterministic mock if empty."""
    live = buffer.read_recent(limit=limit)
    return live if live else mock_fn()
```

- [ ] **Step 2: Smoke-import → expect no crash**

```bash
docker exec dashboard python3 -c "from user_data.dashboard import v4_routes; print('ok')"
```

- [ ] **Step 3: Commit**

```bash
git add user_data/dashboard/v4_routes.py
git commit -m "feat(v4-routes): wire V4Buffer singletons (debates/parity/montecarlo) — empty buffers fall through to mocks"
```

### Task C2: `/api/v4/debate/history` reads V4Buffer when populated

**Files:**
- Modify: `user_data/dashboard/v4_routes.py:54-71` (the `debate_history` handler)

- [ ] **Step 1: Refactor handler to use `_live_or_mock`**

Existing mock loop becomes `_mock_debate_history()`. Handler calls `_live_or_mock(_DEBATE_BUFFER, _mock_debate_history, limit=8)`.

```python
def _mock_debate_history() -> list[dict]:
    now = datetime.now(timezone.utc)
    sessions = []
    for i in range(8):
        ts = now - timedelta(minutes=15 + i * 47)
        pair = _DEMO_PAIRS[i % len(_DEMO_PAIRS)]
        sessions.append({
            "session_id": _seed_session_id(f"{pair}-{i}"),
            "pair": pair,
            "setup_ts": ts.isoformat(),
            "decision": ["FLAT", "LONG", "FLAT", "SHORT", "FLAT"][i % 5],
            "total_latency_ms": 28000 + (i * 1100) % 6000,
        })
    return sessions

@router.get("/debate/history")
async def debate_history() -> dict[str, Any]:
    sessions = _live_or_mock(_DEBATE_BUFFER, _mock_debate_history, limit=8)
    return {"sessions": sessions}
```

- [ ] **Step 2: Smoke-test endpoint**

```bash
curl -s http://localhost:8081/api/v4/debate/history | python3 -c "import sys,json; d=json.load(sys.stdin); assert 'sessions' in d; print(len(d['sessions']), 'sessions')"
```

Expected output: `8 sessions` (mock fallback, buffer empty)

- [ ] **Step 3: Append a real-looking event and re-curl**

```bash
python3 -c "
from pathlib import Path
from src.quanta_core.observability.v4_buffer import V4Buffer
buf = V4Buffer(Path('data/v4_runtime/debates.jsonl'), capacity=256)
buf.append({'session_id':'live-001','pair':'BTC/USD','setup_ts':'2026-05-13T01:30:00Z','decision':'LONG','total_latency_ms':24000})
"
curl -s http://localhost:8081/api/v4/debate/history | python3 -m json.tool | head -10
```

Expected: returns the live event, not mocks.

- [ ] **Step 4: Commit**

```bash
git add user_data/dashboard/v4_routes.py
git commit -m "feat(v4-debate-history): read live V4Buffer with mock fallback"
```

### Task C3: Repeat C2 pattern for `/api/v4/parity` and `/api/v4/montecarlo/{trade_id}`

**Files:**
- Modify: `user_data/dashboard/v4_routes.py` (parity + montecarlo handlers)

- [ ] **Step 1: Extract existing mock bodies to `_mock_parity()` / `_mock_montecarlo(trade_id)` functions**

- [ ] **Step 2: Swap handler bodies to `_live_or_mock` calls**

- [ ] **Step 3: Curl each endpoint and verify shape unchanged**

```bash
curl -s http://localhost:8081/api/v4/parity | python3 -m json.tool | head -20
curl -s http://localhost:8081/api/v4/montecarlo/test-trade-id | python3 -m json.tool | head -20
```

- [ ] **Step 4: Commit**

```bash
git add user_data/dashboard/v4_routes.py
git commit -m "feat(v4-parity-montecarlo): live V4Buffer reads with mock fallback (3 endpoints converted)"
```

### Task C4: Rebuild dashboard image (no freqtrade recycle)

**Files:**
- None modified, just deploy

- [ ] **Step 1: Rebuild dashboard image only**

```bash
docker compose build dashboard 2>&1 | tail -5
```

- [ ] **Step 2: Recycle dashboard WITHOUT `--deps` (critical: don't bounce freqtrade)**

```bash
docker compose up -d --no-deps dashboard
sleep 8
docker ps --format '{{.Names}}\t{{.Status}}' | grep -E 'dashboard|freqtrade'
```

Expected: both `Up (healthy)`. Freqtrade uptime should NOT reset.

- [ ] **Step 3: Smoke all /api/v4/* endpoints**

```bash
for ep in debate/history parity adapters weekly/preview screening; do
  echo "=== /api/v4/$ep ==="
  curl -s -o /dev/null -w "%{http_code}\n" "http://localhost:8081/api/v4/$ep"
done
curl -s -o /dev/null -w "montecarlo: %{http_code}\n" "http://localhost:8081/api/v4/montecarlo/test"
```

Expected: all 200.

- [ ] **Step 4: Smoke /api/ops/* surface unchanged**

```bash
for ep in heartbeat metrics_summary regime_state trades_risk; do
  curl -s -o /dev/null -w "/api/ops/$ep: %{http_code}\n" "http://localhost:8081/api/ops/$ep"
done
```

Expected: all 200. No regression on legacy surface.

- [ ] **Step 5: Commit deployment marker**

```bash
echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) dashboard rebuilt with V4Buffer wiring" >> docs/DEPLOY_LOG.md
git add docs/DEPLOY_LOG.md
git commit -m "deploy: dashboard rebuild — V4Buffer wired into /api/v4/* (additive, no /api/ops/* changes)"
```

---

## Track D — Shadow-mode design doc (the freqtrade→V4 cutover blueprint)

### Task D1: Write `docs/V4_SHADOW_MODE_DESIGN.md`

**Files:**
- Create: `docs/V4_SHADOW_MODE_DESIGN.md`

- [ ] **Step 1: Author the doc**

Sections to include (each ~150-300 words):

1. **Context** — V4 is merged additively; freqtrade is the live trading engine; this doc plans the 1-2-week migration.
2. **Shadow-mode definition** — V4's `quanta_core.live.engine` reads the same market data as freqtrade, runs its own decision loop, writes to its own `decisions` table + `data/v4_runtime/decisions.jsonl`. No orders placed. Parity oracle compares V4 vs freqtrade decisions every 5 min.
3. **Decision schema** — fields, identity (pair, timestamp, regime, decision, conviction), correlation key with freqtrade trades.
4. **Parity rules** — agreement = same side; conflict = opposite side; abstain = one is FLAT. Target ≥85% agreement before cutover.
5. **Cutover gate** — 5 calendar days of shadow with ≥85% parity, zero V4 crashes, ledger writes match.
6. **Rollback plan** — `LIVE_ENGINE=freqtrade` env flips back instantly; V4 decisions become advisory only.
7. **Risks** — Ollama latency, postgres contention, dashboard rendering both feeds.
8. **Timeline** — Week 1: shadow runner cron; Week 2: parity oracle + dashboard parity card; Week 3: cutover gate review.

- [ ] **Step 2: Commit**

```bash
git add docs/V4_SHADOW_MODE_DESIGN.md
git commit -m "docs(v4): shadow-mode cutover design — 1-2 week migration blueprint, parity oracle gate, rollback plan"
```

### Task D2: Parity oracle scaffolding (code only, NOT wired to cron yet)

**Files:**
- Create: `src/quanta_core/observability/parity_oracle.py`
- Create: `tests/unit/quanta_core/observability/test_parity_oracle.py`

- [ ] **Step 1: Write failing test**

```python
# tests/unit/quanta_core/observability/test_parity_oracle.py
from src.quanta_core.observability.parity_oracle import compare_decisions

def test_agreement_same_side():
    d = compare_decisions(
        freqtrade={"pair": "BTC/USD", "side": "LONG", "ts": "2026-05-13T01:00:00Z"},
        v4={"pair": "BTC/USD", "side": "LONG", "ts": "2026-05-13T01:00:05Z"},
    )
    assert d["verdict"] == "agree"

def test_conflict_opposite_side():
    d = compare_decisions(
        freqtrade={"pair": "BTC/USD", "side": "LONG", "ts": "..."},
        v4={"pair": "BTC/USD", "side": "SHORT", "ts": "..."},
    )
    assert d["verdict"] == "conflict"

def test_abstain_one_flat():
    d = compare_decisions(
        freqtrade={"pair": "BTC/USD", "side": "FLAT", "ts": "..."},
        v4={"pair": "BTC/USD", "side": "LONG", "ts": "..."},
    )
    assert d["verdict"] == "abstain"
```

- [ ] **Step 2: Run → FAIL**

- [ ] **Step 3: Implement `compare_decisions`**

```python
# src/quanta_core/observability/parity_oracle.py
from __future__ import annotations
from typing import Any

_SIDES = {"LONG", "SHORT", "FLAT"}

def compare_decisions(freqtrade: dict[str, Any], v4: dict[str, Any]) -> dict[str, Any]:
    f = freqtrade.get("side", "FLAT")
    v = v4.get("side", "FLAT")
    if f not in _SIDES or v not in _SIDES:
        raise ValueError(f"unknown side: freqtrade={f} v4={v}")
    if f == "FLAT" or v == "FLAT":
        verdict = "abstain" if f != v else "agree"
    elif f == v:
        verdict = "agree"
    else:
        verdict = "conflict"
    return {
        "pair": freqtrade.get("pair") or v4.get("pair"),
        "freqtrade_side": f,
        "v4_side": v,
        "verdict": verdict,
    }
```

- [ ] **Step 4: Run → PASS**

- [ ] **Step 5: Commit**

```bash
git add src/quanta_core/observability/parity_oracle.py tests/unit/quanta_core/observability/test_parity_oracle.py
git commit -m "feat(parity): compare_decisions — freqtrade vs V4 verdict (agree/conflict/abstain)"
```

---

## Track E — Bug-free paper trading verification (morning gate)

### Task E1: Smoke-test the full dashboard surface

**Files:** None modified — read-only verification.

- [ ] **Step 1: Run end-to-end endpoint smoke**

```bash
ENDPOINTS=(
  /api/ops/heartbeat
  /api/ops/metrics_summary
  /api/ops/regime_state
  /api/ops/trades_risk
  /api/ops/sentiment
  /api/ops/agent_flow
  /api/v4/debate/history
  /api/v4/parity
  /api/v4/screening
)
FAIL=0
for ep in "${ENDPOINTS[@]}"; do
  code=$(curl -s -o /dev/null -w "%{http_code}" "http://localhost:8081$ep")
  if [ "$code" != "200" ]; then echo "FAIL $ep -> $code"; FAIL=1; else echo "ok   $ep"; fi
done
test $FAIL -eq 0 && echo "ALL GREEN" || echo "REGRESSION"
```

Expected: ALL GREEN.

- [ ] **Step 2: Verify dashboard renders (headless chrome screenshot)**

```bash
timeout 30 chromium --headless --disable-gpu --no-sandbox --hide-scrollbars \
  --window-size=1600,5400 --virtual-time-budget=15000 \
  --screenshot=/tmp/morning-state.png http://localhost:8081/ops 2>/dev/null
ls -la /tmp/morning-state.png
```

If chromium not available, skip silently — manual screenshot is operator's job in the morning.

- [ ] **Step 3: Container health**

```bash
docker ps --format '{{.Names}}\t{{.Status}}' | grep -E 'dashboard|freqtrade|postgres'
docker logs --tail 50 freqtrade 2>&1 | grep -iE "error|exception|traceback" | head -20
```

Expected: all `Up (healthy)`. Zero errors in last 50 lines of freqtrade log.

### Task E2: Write `MORNING-STATE.md` brief

**Files:**
- Modify: `MORNING-STATE.md`

- [ ] **Step 1: Replace MORNING-STATE.md body**

Structure:

```markdown
# Morning state — 2026-05-13 08:00 ET

## Status — GREEN/YELLOW/RED

[verdict + 1-line summary]

## What landed overnight

- [N commits on local main, NOT pushed (per standing rule)]
- [link to each track A/B/C/D outcome]

## Paper trading

- Freqtrade uptime: [from docker ps]
- Open positions: [from /api/ops/trades_risk]
- Last decision cycle: [from logs]
- Regime: [from /api/ops/regime_state]

## What's next

- [V4 cutover sprint scoped in docs/V4_SHADOW_MODE_DESIGN.md]
- [any deferred items]

## Risks / things to confirm at 8 AM

- [...]
```

- [ ] **Step 2: Commit**

```bash
git add MORNING-STATE.md
git commit -m "docs: MORNING-STATE 2026-05-13 — overnight V4 wiring summary"
```

---

## Self-Review

- **Spec coverage:**
  - "Wire V4 routes into live UI" → Track C ✓
  - "Backend cleanup matching frontend" → Track A ✓
  - "V4 agents+ledger live data" → Tracks B + C (buffer is the substrate; future cron writes here) ✓
  - "Freqtrade migration prep" → Track D (design doc + parity oracle scaffolding; NO cutover tonight) ✓
  - "Paper trading bug-free by 8am" → Track E ✓

- **Placeholder scan:** none — every step has commands or code.

- **Type consistency:** `V4Buffer.append/read_recent` consistent across Tracks B+C. `compare_decisions` signature stable.

- **Constraint compliance:**
  - V4 additive: ✓ (all changes are behind `/api/v4/*` or buffer-fallback)
  - No push: ✓ (every commit ends at local main)
  - No manual runs: ✓ (everything is HTTP smoke or cron-ready)
  - No heavy containers: ✓ (no LLM, no LoRA, no model pulls)

- **Execution order:** A → B → C → D → E (A is safest cleanup, B is foundation for C, D parallel-safe, E final gate)

---

## Execution Handoff

Approach: **Inline execution** with checkpoints. Single overnight session, no fresh subagents needed — the tasks are small, sequential, and benefit from shared dashboard/container context. Track A first (lowest risk), then B, then C (with the deploy gate in C4 being the only risky moment — guard with `--no-deps`), then D in parallel-friendly mode, then E as the morning gate.

**Stop-and-flag conditions:**
- Freqtrade uptime drops to <2 min unexpectedly → halt, dump logs, leave a "FREQTRADE BOUNCED — investigate" line in MORNING-STATE.md
- Any `/api/ops/*` endpoint returns non-200 after C4 deploy → roll back the C-track commits, redeploy dashboard
- Any heavy-resource decision required mid-execution → halt and write the question to MORNING-STATE.md for the 8 AM review
