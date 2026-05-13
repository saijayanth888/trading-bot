"""
V4 dashboard surfaces.

Stub endpoints feeding the new `frontend-v4/` SPA. These return deterministic
dummy payloads when the real V4 modules (debate, monte_carlo, adapter
registry, weekly publisher) haven't landed yet, so the SPA renders end-to-end
during the wave-2 development cycle. Each handler is structured so swapping
the body for the real implementation is a one-line edit.

Mount from `app.py`:

    from . import v4_routes
    v4_routes.mount(app)

That call also mounts `frontend-v4/dist/` at /v4/* (if the build artifact
exists). In dev, hit http://localhost:5173 directly — vite proxies /api/*
back here.
"""
from __future__ import annotations

import asyncio
import json
import math
import os
import random
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, AsyncIterator

from fastapi import APIRouter, FastAPI, HTTPException
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles

# Local vendored copy; canonical lives at src/quanta_core/observability/v4_buffer.py.
# The dashboard image's build context excludes src/, so we keep a sibling copy here.
try:
    from .v4_buffer import V4Buffer
except ImportError:  # pragma: no cover — fallback for direct-host runs
    from v4_buffer import V4Buffer  # type: ignore[no-redef]

router = APIRouter(prefix="/api/v4", tags=["v4"])

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parents[1]                  # …/trading-bot
V4_DIST = REPO_ROOT / "frontend-v4" / "dist"

# ----------------------------------------------------------------------------
# V4 runtime buffers (live-data substrate)
#
# Each /api/v4/* handler below tries the buffer first (O(1) ring read);
# when empty (e.g., pre-shadow-mode, fresh container, no writers yet) the
# handler falls back to the deterministic mock body via `_live_or_mock`.
#
# Writers (future): debate orchestrator, parity oracle, monte carlo runner.
# See docs/V4_SHADOW_MODE_DESIGN.md for the cutover blueprint.
#
# Storage path resolution: prefer the existing USER_DATA_ROOT mount
# (`/freqtrade/user_data` inside the container; `./user_data/` on host),
# so the buffer files land on the bind-mounted volume — survives container
# restarts and is visible to off-container tools.
# ----------------------------------------------------------------------------
_USER_DATA_ROOT = Path(os.environ.get("USER_DATA_ROOT", str(REPO_ROOT / "user_data")))
_V4_DATA_DIR = _USER_DATA_ROOT / "v4_runtime"
_DEBATE_BUFFER = V4Buffer(_V4_DATA_DIR / "debates.jsonl", capacity=256)
_PARITY_BUFFER = V4Buffer(_V4_DATA_DIR / "parity.jsonl", capacity=128)
_MONTECARLO_BUFFER = V4Buffer(_V4_DATA_DIR / "montecarlo.jsonl", capacity=64)


def _live_or_mock(buffer: V4Buffer, mock_fn, limit: int = 8) -> list[dict[str, Any]]:
    """Read live buffer; fall back to deterministic mock if empty.

    The buffer is canonical when populated. Mocks keep the SPA rendering
    end-to-end during early shadow-mode bring-up — once writers land,
    `read_recent` will return real events and the mock branch goes cold.
    """
    live = buffer.read_recent(limit=limit)
    return live if live else mock_fn()

# ----------------------------------------------------------------------------
# Debate
# ----------------------------------------------------------------------------

_ROLES = ("regime", "micro", "bull", "bear", "arbiter")
_DEMO_PAIRS = ("BTC/USD", "ETH/USD", "SOL/USD", "SOFI", "NVDA", "PLTR")


def _seed_session_id(suffix: str = "") -> str:
    return uuid.uuid5(uuid.NAMESPACE_DNS, f"quanta-v4-debate-{suffix}").hex[:16]


def _mock_debate_history() -> list[dict[str, Any]]:
    now = datetime.now(timezone.utc)
    sessions = []
    for i in range(8):
        ts = now - timedelta(minutes=15 + i * 47)
        pair = _DEMO_PAIRS[i % len(_DEMO_PAIRS)]
        sessions.append(
            {
                "session_id": _seed_session_id(f"{pair}-{i}"),
                "pair": pair,
                "setup_ts": ts.isoformat(),
                "decision": ["FLAT", "LONG", "FLAT", "SHORT", "FLAT"][i % 5],
                "total_latency_ms": 28000 + (i * 1100) % 6000,
            }
        )
    return sessions


def _read_decisions_from_db(limit: int = 12) -> list[dict[str, Any]]:
    """Pull recent V4 decisions from quanta_schema.decisions.

    Source-of-truth for V4 shadow-mode output. Empty list on connection
    failure (the handler then falls back to the in-memory buffer / mock).
    """
    try:
        import psycopg
    except Exception:
        return []
    user = os.environ.get("POSTGRES_USER", "tradebot")
    pw = os.environ.get("POSTGRES_PASSWORD", "")
    host = os.environ.get("POSTGRES_HOST", "postgres")
    port = os.environ.get("POSTGRES_PORT", "5432")
    db = os.environ.get("POSTGRES_DB", "tradebot")
    if not pw:
        return []
    dsn = f"host={host} port={port} user={user} password={pw} dbname={db}"
    try:
        with psycopg.connect(dsn, connect_timeout=2) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT ts, symbol, strategy, outcome, rationale, debate
                    FROM quanta_schema.decisions
                    ORDER BY ts DESC
                    LIMIT %s
                    """,
                    (limit,),
                )
                rows = cur.fetchall()
    except Exception:
        return []
    sessions: list[dict[str, Any]] = []
    for ts, symbol, strategy, outcome, rationale, debate in rows:
        sessions.append({
            "session_id": uuid.uuid5(
                uuid.NAMESPACE_DNS, f"{symbol}-{ts.isoformat()}"
            ).hex[:16],
            "pair": symbol,
            "setup_ts": ts.isoformat(),
            "decision": outcome,
            "strategy": strategy,
            "rationale": rationale,
            "regime": (debate or {}).get("regime"),
            "close": (debate or {}).get("close"),
            # latency is N/A for the simple shadow runner; surface 0
            "total_latency_ms": 0,
        })
    return sessions


def _connect_pg():
    """Open a sync psycopg connection from POSTGRES_* env. Returns None on miss."""
    try:
        import psycopg
    except Exception:
        return None
    pw = os.environ.get("POSTGRES_PASSWORD")
    if not pw:
        return None
    dsn = (
        f"host={os.environ.get('POSTGRES_HOST', 'postgres')} "
        f"port={os.environ.get('POSTGRES_PORT', '5432')} "
        f"user={os.environ.get('POSTGRES_USER', 'tradebot')} "
        f"password={pw} "
        f"dbname={os.environ.get('POSTGRES_DB', 'tradebot')}"
    )
    try:
        return psycopg.connect(dsn, connect_timeout=2)
    except Exception:
        return None


@router.get("/positions")
async def v4_positions() -> dict[str, Any]:
    """Net positions per symbol from the V4 paper ledger.

    Aggregates fills: net_qty = SUM(BUY qty) - SUM(SELL qty), avg buy price
    weighted by qty. Empty list when no fills exist (which is the
    expected state until regime flips and V4 places its first BUYs).
    """
    conn = _connect_pg()
    if conn is None:
        return {"positions": [], "source": "no_db"}
    try:
        with conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT p.symbol,
                       SUM(CASE WHEN f.side = 'BUY'  THEN f.qty ELSE 0 END) -
                       SUM(CASE WHEN f.side = 'SELL' THEN f.qty ELSE 0 END)            AS net_qty,
                       SUM(CASE WHEN f.side = 'BUY' THEN f.qty * f.price ELSE 0 END) /
                       NULLIF(SUM(CASE WHEN f.side = 'BUY' THEN f.qty ELSE 0 END), 0)  AS avg_buy_px,
                       MAX(f.ts) AS last_fill_ts
                FROM quanta_schema.fills f
                JOIN quanta_schema.proposals p USING (client_order_id)
                GROUP BY p.symbol
                HAVING SUM(CASE WHEN f.side='BUY' THEN f.qty ELSE 0 END) -
                       SUM(CASE WHEN f.side='SELL' THEN f.qty ELSE 0 END) > 0
                ORDER BY 1
                """
            )
            rows = cur.fetchall()
    except Exception as exc:
        return {"positions": [], "error": str(exc)[:200]}
    positions = [
        {
            "symbol": sym,
            "qty": float(qty),
            "avg_buy_px": float(avg_px) if avg_px is not None else None,
            "last_fill_ts": ts.isoformat() if ts else None,
        }
        for sym, qty, avg_px, ts in rows
    ]
    return {"positions": positions, "source": "quanta_schema.fills"}


@router.get("/trades")
async def v4_trades(limit: int = 30) -> dict[str, Any]:
    """Recent paper fills joined with their proposals (V4 trade tape)."""
    conn = _connect_pg()
    if conn is None:
        return {"trades": [], "source": "no_db"}
    try:
        with conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT f.ts, p.symbol, p.strategy, f.side, f.qty, f.price,
                       p.client_order_id, p.intent
                FROM quanta_schema.fills f
                JOIN quanta_schema.proposals p USING (client_order_id)
                ORDER BY f.ts DESC
                LIMIT %s
                """,
                (limit,),
            )
            rows = cur.fetchall()
    except Exception as exc:
        return {"trades": [], "error": str(exc)[:200]}
    trades = [
        {
            "ts": ts.isoformat() if ts else None,
            "symbol": sym,
            "strategy": strat,
            "side": side,
            "qty": float(qty),
            "price": float(price),
            "client_order_id": coid,
            "rationale": (intent or {}).get("rationale"),
        }
        for ts, sym, strat, side, qty, price, coid, intent in rows
    ]
    return {"trades": trades, "source": "quanta_schema.fills"}


@router.get("/debate/history")
async def debate_history() -> dict[str, Any]:
    """Recent V4 decisions.

    Reads, in order of preference:
      1. `quanta_schema.decisions` postgres table (V4 shadow runner output)
      2. `_DEBATE_BUFFER` in-memory ring (future debate orchestrator)
      3. Deterministic mock (early bring-up only)

    Real-data branch wins as soon as the shadow runner writes a row.
    """
    db_sessions = _read_decisions_from_db(limit=12)
    if db_sessions:
        return {"sessions": db_sessions, "source": "quanta_schema.decisions"}

    sessions = _live_or_mock(_DEBATE_BUFFER, _mock_debate_history, limit=8)
    return {"sessions": sessions, "source": "buffer_or_mock"}


def _vote_payload(role: str, pair: str, idx: int) -> dict[str, Any]:
    rng = random.Random(f"{role}-{pair}-{idx}")
    vote = ["LONG", "SHORT", "FLAT"][rng.randrange(3)]
    conviction = round(0.4 + rng.random() * 0.55, 2)
    rationale_map = {
        "regime": f"Macro regime is {('trending_up' if vote=='LONG' else 'trending_down' if vote=='SHORT' else 'unknown')} with conviction {conviction:.2f}. Last 20 bars confirm the read.",
        "micro": f"Spread {rng.randrange(2, 14)}bps · depth healthy · IV-rank {rng.randrange(20, 70)}. Book is sane.",
        "bull": "Earnings beat carried multiple expansion through the open; the regime engine still favors risk-on. The strongest LONG case rests on options-flow asymmetry and dollar weakness into the close.",
        "bear": "Adverse on-chain netflow + put/call ratio dilation argue for a short. The bull case ignores the breakdown of the 20-day VWAP and the late-day liquidity drop.",
        "arbiter": "Bull cites flow asymmetry; bear cites breakdown of VWAP. The flow read is conditional on the macro tape continuing — bear's invalidation is structural. Reflector should re-audit if VWAP reclaims.",
    }
    return {
        "role": role,
        "model": "hermes3:8b" if role in ("regime", "micro") else "hermes3:70b",
        "vote": vote,
        "conviction": conviction,
        "rationale": rationale_map.get(role, ""),
        "evidence_keys": [f"feat:{role}:{rng.randrange(100, 999)}" for _ in range(3)],
        "latency_ms": (1800 if role in ("regime", "micro") else 10500) + rng.randrange(-400, 900),
        "emitted_at": datetime.now(timezone.utc).isoformat(),
    }


def _arbiter_payload(votes: list[dict[str, Any]]) -> dict[str, Any]:
    directions = [v["vote"] for v in votes if v["role"] in ("bull", "bear", "regime", "micro")]
    agree = len(set(directions)) == 1
    pattern = "unanimous" if agree else "split"
    dissent = [] if agree else [
        f"{v['role']} voted {v['vote']} (conv {v['conviction']:.2f})" for v in votes
    ]
    return {
        "synthesized_action": "LONG" if directions.count("LONG") > len(directions) / 2 else
                              "SHORT" if directions.count("SHORT") > len(directions) / 2 else "FLAT",
        "synthesis_rationale": "Panel converged on a single direction." if agree else
                               "Panel diverged — no consensus. Per the unanimous-or-FLAT rule, decision will be FLAT regardless of arbiter preference.",
        "agreement_pattern": pattern,
        "dissent_notes": dissent,
    }


def _aggregate(votes: list[dict[str, Any]]) -> tuple[dict[str, Any], str]:
    directions = [v["vote"] for v in votes if v["role"] in ("bull", "bear", "regime", "micro")]
    consensus = len(set(directions)) == 1 and "ABSTAIN" not in directions and "FLAT" not in directions
    score = sum(
        (1 if v["vote"] == "LONG" else -1 if v["vote"] == "SHORT" else 0) * v["conviction"]
        for v in votes if v["role"] in ("bull", "bear", "regime", "micro")
    )
    method = "weighted_vote" if consensus else "veto_quorum"
    decision = "LONG" if consensus and score > 0 else "SHORT" if consensus and score < 0 else "FLAT"
    return ({"score": round(score, 3), "n_valid": len(directions), "consensus": consensus, "method": method}, decision)


async def _debate_stream(session_id: str) -> AsyncIterator[str]:
    """Yield Server-Sent Events frames mimicking a live 30s debate."""
    rng = random.Random(session_id)
    pair = _DEMO_PAIRS[rng.randrange(len(_DEMO_PAIRS))]
    setup_ts = datetime.now(timezone.utc).isoformat()

    yield _sse({"kind": "session_start", "session_id": session_id, "pair": pair, "setup_ts": setup_ts})
    await asyncio.sleep(0.2)

    votes: list[dict[str, Any]] = []
    for idx, role in enumerate(_ROLES[:-1]):  # regime, micro, bull, bear
        vote = _vote_payload(role, pair, idx)
        # Emit a few partial tokens for visual life
        snippet = vote["rationale"].split()
        accum = ""
        for word in snippet[:8]:
            accum += word + " "
            yield _sse({"kind": "vote_partial", "role": role, "token": word + " "})
            await asyncio.sleep(0.06)
        yield _sse({"kind": "vote_complete", "vote": vote})
        votes.append(vote)
        await asyncio.sleep(0.2)
        # heartbeat between heavy roles
        yield _sse({"kind": "heartbeat", "ts": datetime.now(timezone.utc).isoformat()})

    arbiter = _arbiter_payload(votes)
    yield _sse({"kind": "arbiter", "arbiter": arbiter})
    await asyncio.sleep(0.15)

    agg, decision = _aggregate(votes)
    yield _sse(
        {
            "kind": "decision",
            "aggregate": agg,
            "decision": decision,
            "total_latency_ms": sum(v["latency_ms"] for v in votes) + 4000,
        }
    )


def _sse(obj: dict[str, Any]) -> str:
    return f"data: {json.dumps(obj)}\n\n"


@router.get("/debate/stream/{session_id}")
async def debate_stream(session_id: str) -> StreamingResponse:
    """Server-Sent Events stream of debate events.

    Real implementation will subscribe to `quanta_core.agents.debate.events`;
    this stub deterministically generates a complete 30s deliberation given
    the session id (so reloads replay the same debate, useful for testing).
    """
    headers = {
        "Cache-Control": "no-cache, no-transform",
        "X-Accel-Buffering": "no",
        "Connection": "keep-alive",
    }
    return StreamingResponse(_debate_stream(session_id), media_type="text/event-stream", headers=headers)


# ----------------------------------------------------------------------------
# Monte Carlo
# ----------------------------------------------------------------------------


def _mock_montecarlo(trade_id: str) -> dict[str, Any]:
    rng = random.Random(f"mc-{trade_id}")
    horizon = 48
    n_paths = 10_000
    sample_n = 120
    mu = 0.0008
    sigma = 0.012

    quantiles = {f"p{p:02d}": [] for p in (5, 25, 50, 75, 95)}
    for bar in range(horizon + 1):
        t = bar
        drift = 1 + mu * t
        scale = sigma * math.sqrt(t)
        for p, z in ((5, -1.645), (25, -0.674), (50, 0), (75, 0.674), (95, 1.645)):
            quantiles[f"p{p:02d}"].append(round(drift + scale * z, 5))

    sample_paths = []
    for _ in range(sample_n):
        v = 1.0
        path = [v]
        for _bar in range(horizon):
            v *= 1 + rng.gauss(mu, sigma)
            path.append(round(v, 5))
        sample_paths.append({"values": path})

    var_95 = round(quantiles["p05"][-1] - 1.0, 4)
    es_95 = round(var_95 * 1.32, 4)

    blocked = trade_id.startswith("blocked")
    return {
        "trade_id": trade_id,
        "pair": rng.choice(list(_DEMO_PAIRS)),
        "side": "LONG",
        "n_paths": n_paths,
        "horizon_bars": horizon,
        "sample_paths": sample_paths,
        "quantiles": quantiles,
        "var_95": var_95,
        "expected_shortfall_95": es_95,
        "blocked": blocked,
        "block_reason": "VaR breach" if blocked else None,
    }


@router.get("/montecarlo/{trade_id}")
async def montecarlo(trade_id: str) -> dict[str, Any]:
    """Monte Carlo path envelope for a given trade id.

    Looks up `trade_id` in `_MONTECARLO_BUFFER` (live runs published by
    `quanta_core.risk.monte_carlo`); falls back to a deterministic
    closed-form normal envelope when no real run is recorded.
    """
    for event in reversed(_MONTECARLO_BUFFER.read_recent(limit=64)):
        if event.get("trade_id") == trade_id:
            return event
    return _mock_montecarlo(trade_id)


# ----------------------------------------------------------------------------
# Adapter registry
# ----------------------------------------------------------------------------


@router.get("/adapters")
async def adapters() -> dict[str, Any]:
    """Recent LoRA promotions across the 6 debate roles."""
    now = datetime.now(timezone.utc)
    rows = []
    for i in range(24):
        role = ("regime", "micro", "bull", "bear", "arbiter", "reflector")[i % 6]
        promoted = now - timedelta(days=(i // 6) * 7, hours=i)
        rng = random.Random(f"ad-{i}-{role}")
        status = (
            "champion" if i // 6 == 0 else
            "pareto" if rng.random() > 0.4 else
            "rolled_back" if rng.random() > 0.7 else "candidate"
        )
        rows.append(
            {
                "id": f"v{(i//6)+1}-{role}-{rng.randrange(100, 999)}",
                "role": role,
                "base_model": "hermes3:8b" if role in ("regime", "micro") else "hermes3:70b",
                "promoted_at": promoted.isoformat(),
                "faithfulness": round(0.55 + rng.random() * 0.40, 3),
                "hit_rate": round(0.40 + rng.random() * 0.50, 3),
                "pareto_dominated": status == "candidate" and rng.random() > 0.5,
                "status": status,
                "notes": None,
            }
        )
    return {"adapters": rows}


@router.post("/adapters/{adapter_id}/rollback")
async def adapter_rollback(adapter_id: str) -> dict[str, Any]:
    """Rolls back an adapter to the previous Pareto-frontier champion.

    Real impl calls mf-api at `:8000/api/adapters/{id}/rollback`. Here we
    only validate the id shape so the UI can wire the round-trip end-to-end.
    """
    if not adapter_id or "-" not in adapter_id:
        raise HTTPException(status_code=400, detail="Invalid adapter id")
    return {
        "ok": True,
        "adapter_id": adapter_id,
        "rolled_back_at": datetime.now(timezone.utc).isoformat(),
        "note": "stub — wire to mf-api once the registry endpoint is live",
    }


# ----------------------------------------------------------------------------
# Weekly preview
# ----------------------------------------------------------------------------


@router.get("/weekly/preview")
async def weekly_preview() -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    iso_year, iso_week, _ = now.isocalendar()
    monday = now - timedelta(days=now.weekday())
    sunday = monday + timedelta(days=6)

    md = _render_weekly_md(iso_year, iso_week, monday, sunday)
    return {
        "iso_week": f"{iso_year}-{iso_week:02d}",
        "monday": monday.date().isoformat(),
        "sunday": sunday.date().isoformat(),
        "generated_ts": now.isoformat(),
        "net_pnl": -84.92,
        "net_pnl_pct": -0.71,
        "drawdown_pct": 1.42,
        "open_count": 3,
        "trade_count": 4,
        "run_mode": "paper",
        "markdown": md,
    }


def _render_weekly_md(year: int, week: int, monday: datetime, sunday: datetime) -> str:
    return f"""# Quanta · Week {year}-{week:02d} ({monday.date()} → {sunday.date()})

## Headline
- **Net P&L** · -$84.92 (-0.71%)
- **Drawdown** · 1.42%
- **Open positions** · 3
- **Mode** · paper

## Trades this week (4)

### 1. BTC/USD · LONG
- **Entry** $80,383 @ 2026-05-12 13:25 UTC
- **Exit**  $81,267 @ 2026-05-12 19:00 UTC
- **P&L**   +$22.18 (+1.10%)
- **Hold**  5h 35m
- **Strategy** mean_rev_tft · **Regime at entry** trending_up

<details><summary>Debate transcript (4 turns · unanimous)</summary>

regime LONG · conv 0.67 — Macro regime is trending_up; last 20 bars confirm.
micro LONG · conv 0.71 — Spread 4bps · depth healthy.
bull LONG · conv 0.82 — Flow asymmetry confirms.
bear FLAT · conv 0.31 — No compelling short setup.
arbiter — unanimous LONG; entry approved.

</details>

**Lessons logged by Reflector**
- Entry timing aligned with VWAP reclaim — keep this pattern.

### 2. SOFI · SHORT_PUT
- **Entry** $15.50 @ 2026-05-08
- **Exit**  expired @ 2026-05-15
- **P&L**   +$35.50 (+229%)
- **Hold**  7d
- **Strategy** wheel · **Regime at entry** mean_reverting

## Closed-loop telemetry
- **Reflector lessons added this week** · 12
- **LoRA adapters promoted last Sunday** · v2-bull-487, v2-regime-202
- **Convergence funnel** · 14 detected → 6 converged → 4 traded
- **Debate participation** · 14 debates · avg 4.2 turns · consensus rate 43%

## Open positions
- **NVDA** · SHORT_PUT · entered 2026-05-08 · 4d held
  - Thesis: Earnings cushion + IV crush

## Next week's universe state
- **Regime** · trending_up
- **Sentiment composite** · +0.18 (bullish)
- **Scheduled events** · CPI Tue 08:30 ET; FOMC minutes Wed 14:00 ET

---
_Generated {datetime.now(timezone.utc).isoformat()} by `quanta_core.hermes.weekly_publisher`._
_Bot run-mode this week: **paper**. Paper mode — all values shown as-is._
"""


# ----------------------------------------------------------------------------
# Backtest parity
# ----------------------------------------------------------------------------


def _mock_parity_rows() -> list[dict[str, Any]]:
    rng = random.Random("parity")
    now = datetime.now(timezone.utc)
    rows = []
    for i in range(36):
        ts = now - timedelta(hours=i * 4)
        live_action = rng.choice(["LONG", "FLAT", "FLAT", "FLAT", "SHORT"])
        backtest_action = live_action if rng.random() < 0.86 else "FLAT"
        live_pnl = round(rng.gauss(0, 0.012), 4) if live_action != "FLAT" else None
        backtest_pnl = (
            round((live_pnl or 0) + rng.gauss(0, 0.0015), 4)
            if backtest_action != "FLAT"
            else None
        )
        rows.append(
            {
                "ts": ts.strftime("%Y-%m-%d %H:%M"),
                "pair": rng.choice(list(_DEMO_PAIRS)),
                "live_action": live_action,
                "backtest_action": backtest_action,
                "live_pnl": live_pnl,
                "backtest_pnl": backtest_pnl,
                "divergent": live_action != backtest_action,
            }
        )
    return rows


def _mock_parity_weeks() -> list[dict[str, Any]]:
    rng = random.Random("parity")
    weeks = [
        {
            "iso": f"2026-{18 - w:02d}",
            "divergence_pct": round(max(0.0, rng.gauss(6, 3)), 2),
        }
        for w in range(8)
    ]
    weeks.reverse()
    return weeks


@router.get("/parity")
async def parity() -> dict[str, Any]:
    """Backtest-vs-live parity rows + weekly divergence summary.

    Reads from `_PARITY_BUFFER` (live shadow-mode parity oracle output)
    when populated, falls back to a deterministic mock otherwise. Weekly
    summary stays mocked until the shadow-mode runner has 4+ weeks of
    data — that's an explicit Track-D follow-up.
    """
    rows = _live_or_mock(_PARITY_BUFFER, _mock_parity_rows, limit=36)
    return {
        "rows": rows,
        "weeks": _mock_parity_weeks(),
        "consecutive_days_ok": 9,
        "cutover_threshold_days": 14,
    }


# ----------------------------------------------------------------------------
# Screening
# ----------------------------------------------------------------------------


def _read_universe() -> dict[str, list[str]]:
    """Best-effort read of user_data/universe.json. Falls back to defaults.

    The on-disk shape (as of 2026-05-11) is:
        {"crypto": {"pairs": [...]}, "stocks": {"dashboard_basket": [...]}}
    so we walk a few candidate keys before giving up.
    """
    candidates = [
        REPO_ROOT / "user_data" / "universe.json",
        Path("/freqtrade/user_data/universe.json"),
    ]
    for path in candidates:
        try:
            if not path.is_file():
                continue
            data = json.loads(path.read_text())
            crypto_block = data.get("crypto") or {}
            stocks_block = data.get("stocks") or {}
            if isinstance(crypto_block, list):
                crypto = crypto_block
            else:
                crypto = (
                    crypto_block.get("pairs")
                    or crypto_block.get("symbols")
                    or []
                )
            if isinstance(stocks_block, list):
                stocks = stocks_block
            else:
                stocks = (
                    stocks_block.get("dashboard_basket")
                    or stocks_block.get("wheel_universe")
                    or stocks_block.get("symbols")
                    or []
                )
            # de-dup while preserving order
            seen: set[str] = set()
            crypto = [s for s in crypto if not (s in seen or seen.add(s))]
            seen = set()
            stocks = [s for s in stocks if not (s in seen or seen.add(s))]
            if crypto or stocks:
                return {"crypto": crypto, "stocks": stocks}
        except Exception:  # pragma: no cover — best effort
            continue
    return {
        "crypto": [
            "BTC/USD", "ETH/USD", "SOL/USD", "ADA/USD", "XRP/USD", "DOGE/USD",
            "AVAX/USD", "LINK/USD", "DOT/USD", "ATOM/USD", "LTC/USD", "BCH/USD",
        ],
        "stocks": [
            "SPY", "QQQ", "NVDA", "AMD", "AAPL", "MSFT", "META", "GOOGL",
            "TSLA", "PLTR", "SOFI", "HOOD", "MARA", "RIOT", "COIN",
        ],
    }


@router.get("/screening")
async def screening() -> dict[str, Any]:
    universe = _read_universe()
    rng = random.Random(int(time.time() // 600))  # rotate every 10 min so screen feels live
    names = []
    detected = 0
    converged = 0
    traded = 0
    for sym in universe["crypto"]:
        d = rng.random() < 0.30
        c = d and rng.random() < 0.45
        t = c and rng.random() < 0.30
        detected += int(d)
        converged += int(c)
        traded += int(t)
        names.append(_screen_row(sym, "crypto", rng, d, c, t))
    for sym in universe["stocks"]:
        d = rng.random() < 0.25
        c = d and rng.random() < 0.35
        t = c and rng.random() < 0.40
        detected += int(d)
        converged += int(c)
        traded += int(t)
        names.append(_screen_row(sym, "stock", rng, d, c, t))
    return {
        "generated_ts": datetime.now(timezone.utc).isoformat(),
        "names": names,
        "funnel": {"detected": detected, "converged": converged, "traded": traded},
    }


def _screen_row(symbol: str, asset_class: str, rng: random.Random, detected: bool, converged: bool, traded: bool) -> dict[str, Any]:
    regime = rng.choice(["trending_up", "trending_down", "mean_reverting", "high_volatility", "unknown"])
    return {
        "symbol": symbol,
        "asset_class": asset_class,
        "regime": regime,
        "detected": detected,
        "converged": converged,
        "traded": traded,
        "last_setup_ts": (datetime.now(timezone.utc) - timedelta(hours=rng.randrange(1, 96))).isoformat() if detected else None,
        "thesis": "Convergence in regime + microstructure; bull/bear panel pending." if detected else None,
    }


# ----------------------------------------------------------------------------
# Static mount
# ----------------------------------------------------------------------------


def mount(app: FastAPI) -> None:
    """Wire v4 routes + serve `frontend-v4/dist/` at /v4 if the build exists."""
    app.include_router(router)
    if V4_DIST.is_dir():
        app.mount("/v4", StaticFiles(directory=str(V4_DIST), html=True), name="v4_spa")
    else:  # pragma: no cover — startup logs the absence
        import logging
        logging.getLogger(__name__).info(
            "frontend-v4/dist not present — run `cd frontend-v4 && npm run build` "
            "to enable the /v4 SPA route. The /api/v4/* endpoints are still live.",
        )
