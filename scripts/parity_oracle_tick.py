"""Parity oracle tick — V4 self-consistency check.

For each recent decision in ``quanta_schema.decisions`` (crypto pairs only —
stocks are not on the V4 shadow runner yet), this script:

    1. Pulls a fresh history window for the symbol from Coinbase REST.
    2. Locates the bar at ``debate.ts`` (the bar the original decision saw).
    3. Re-feeds the bar through the SAME strategy class with the SAME regime
       label that was active at decision time (read from ``debate.regime``).
    4. Computes a verdict via ``quanta_core.observability.parity_oracle.
       compare_decisions`` and appends a parity row to the V4Buffer at
       ``user_data/v4_runtime/parity.jsonl`` — the dashboard reads this file
       (via the in-memory ring; see CAVEAT below).

Verdict semantics (per parity_oracle.compare_decisions):
    * agree    — replay side == live side
    * abstain  — one is FLAT, the other directional
    * conflict — both directional, opposite sides

The output rows match the ``ParityRow`` shape consumed by
``frontend-v4/src/types/v4.ts:118`` so the existing dashboard SPA renders the
real verdicts without a frontend change:

    {
        "ts": "2026-05-14 12:35",
        "pair": "BTC/USD",
        "live_action": "FLAT",         # what V4 decided
        "backtest_action": "FLAT",      # what the deterministic replay decided
        "live_pnl": null,
        "backtest_pnl": null,
        "divergent": false,             # true when verdict != "agree"
        "_verdict": "agree",            # raw verdict (debug)
        "_decision_id": 6360,           # source row in quanta_schema.decisions
        "_strategy": "mean_rev_bb",
    }

CAVEAT (architecture):
    V4Buffer in v4_routes.py is an in-process singleton (one per uvicorn
    worker). ``read_recent`` returns from the in-memory deque, NOT from
    the JSONL file. Writing JSONL from this host-side script populates the
    durable record but is NOT visible to the live dashboard process until
    its v4_buffer.read_recent() is upgraded to fall back to JSONL-tail
    when the ring is empty, AND the dashboard container is recycled.

    The companion structural fix to ``user_data/dashboard/v4_buffer.py``
    (read_recent JSONL fallback) lands alongside this script. Until the
    dashboard is gracefully recycled, /api/v4/parity continues to serve
    `_mock_parity_rows`. The mock fallback is intentionally preserved.

Run from host:
    POSTGRES_HOST=localhost POSTGRES_PORT=5434 \\
    /home/saijayanthai/Documents/spark/envs/ml-env/bin/python3 \\
        scripts/parity_oracle_tick.py

Cron-friendly: idempotent on the ``id`` column of the source decision row;
already-checked rows are skipped via a small JSONL sidecar at
``user_data/v4_runtime/.parity_seen_ids.json``.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import time
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

# Make `quanta_core` importable when running from host without packaging.
_REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO / "src"))

import aiohttp  # noqa: E402
import psycopg  # noqa: E402

from quanta_core.observability.parity_oracle import compare_decisions  # noqa: E402
from quanta_core.observability.v4_buffer import V4Buffer  # noqa: E402
from quanta_core.strategy.mean_rev_bb import MeanRevBB  # noqa: E402
from quanta_core.types import Bar, Symbol  # noqa: E402

# TrendFollow is optional — tolerate if the import path breaks.
try:
    from quanta_core.strategy.trend_follow import TrendFollow  # noqa: E402
    _TRENDFOLLOW_AVAILABLE = True
except Exception:  # pragma: no cover
    TrendFollow = None  # type: ignore[assignment]
    _TRENDFOLLOW_AVAILABLE = False


logging.basicConfig(
    level=os.environ.get("PARITY_LOG_LEVEL", "INFO"),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("quanta.parity_tick")


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

_COINBASE_BASE = os.environ.get("COINBASE_REST_BASE", "https://api.exchange.coinbase.com")
_BATCH_LIMIT = int(os.environ.get("PARITY_BATCH_LIMIT", "50"))
_HISTORY_WINDOW = int(os.environ.get("PARITY_HISTORY_WINDOW", "60"))
_USER_DATA_ROOT = Path(os.environ.get(
    "USER_DATA_ROOT_HOST",  # explicit host override
    str(_REPO / "user_data"),
))
_V4_DATA_DIR = _USER_DATA_ROOT / "v4_runtime"
_PARITY_JSONL = _V4_DATA_DIR / "parity.jsonl"
_SEEN_IDS_PATH = _V4_DATA_DIR / ".parity_seen_ids.json"
_SEEN_IDS_MAX = 5000  # ring buffer of last N decision ids


# ---------------------------------------------------------------------------
# In-process context (mirrors the shadow runner's minimal context)
# ---------------------------------------------------------------------------

class _ReplayContext:
    """Tiny Context impl satisfying MeanRevBB / TrendFollow's protocol.

    Mirrors ``scripts.run_v4_shadow._InProcessContext`` but never has a
    position seeded — the parity oracle replays in the entry-only regime
    the shadow runner uses today (SHADOW mode = no inventory). This means
    the replay verdict for ``outcome == "SELL"`` rows will trivially diverge
    from an entry-only replay; we exclude SELL/ERROR rows from the parity
    set to avoid spurious "conflict" verdicts that aren't actually drift.
    """

    def __init__(self) -> None:
        self._history: dict[str, list[Any]] = {}

    def now(self) -> datetime:
        return datetime.now(UTC)

    def get_position(self, symbol: str) -> Any | None:  # type: ignore[override]
        return None

    def get_history(self, symbol: str, timeframe: str, window: int) -> list[Any]:  # type: ignore[override]
        return self._history.get(str(symbol), [])[-window:]

    def set_history(self, symbol: str, bars: list[Any]) -> None:
        self._history[symbol] = bars


# ---------------------------------------------------------------------------
# Seen-id ring (idempotency)
# ---------------------------------------------------------------------------

def _load_seen_ids() -> set[int]:
    try:
        if not _SEEN_IDS_PATH.is_file():
            return set()
        data = json.loads(_SEEN_IDS_PATH.read_text())
        return set(int(x) for x in (data.get("ids") or []))
    except Exception as exc:
        log.warning("seen-ids load failed (%s); starting fresh", exc)
        return set()


def _save_seen_ids(ids: set[int]) -> None:
    try:
        _SEEN_IDS_PATH.parent.mkdir(parents=True, exist_ok=True)
        # Keep the most-recent N ids (sorted desc, truncate, sort asc).
        keep = sorted(ids, reverse=True)[:_SEEN_IDS_MAX]
        _SEEN_IDS_PATH.write_text(json.dumps({"ids": sorted(keep)}))
    except Exception as exc:
        log.warning("seen-ids save failed: %s", exc)


# ---------------------------------------------------------------------------
# DB read
# ---------------------------------------------------------------------------

def _build_dsn() -> str:
    """Build a key=val DSN from POSTGRES_* env vars.

    Defaults to host=localhost port=5434 so the script runs cleanly from the
    host shell (TimescaleDB is bound to 5434 on the host; the docker-network
    name 'postgres' is unreachable from outside the compose network).
    """
    return " ".join([
        f"host={os.environ.get('POSTGRES_HOST', 'localhost')}",
        f"port={os.environ.get('POSTGRES_PORT', '5434')}",
        f"user={os.environ.get('POSTGRES_USER', 'tradebot')}",
        f"password={os.environ['POSTGRES_PASSWORD']}",
        f"dbname={os.environ.get('POSTGRES_DB', 'tradebot')}",
    ])


async def fetch_recent_decisions(
    conn: psycopg.AsyncConnection,
    limit: int,
) -> list[dict[str, Any]]:
    """Pull the last N decisions where outcome is FLAT or BUY (entry-side).

    SELL/ERROR rows are excluded because the entry-only replay can't
    reproduce SELL (needs position) and ERROR is a strategy-raised exception
    that's not meaningful to compare against a fresh replay.
    """
    rows: list[dict[str, Any]] = []
    async with conn.cursor() as cur:
        # Note: we pass the LIKE pattern as a bind param to avoid psycopg
        # mis-parsing the '%/' literal as a format placeholder.
        await cur.execute(
            """
            SELECT id, ts, symbol, strategy, outcome, rationale, debate
              FROM quanta_schema.decisions
             WHERE outcome IN ('FLAT', 'BUY')
               AND symbol LIKE %s            -- crypto pairs only (V4 shadow)
             ORDER BY ts DESC
             LIMIT %s
            """,
            ("%/USD", limit),
        )
        for rid, ts, symbol, strategy, outcome, rationale, debate in await cur.fetchall():
            rows.append({
                "id": int(rid),
                "ts": ts,
                "symbol": symbol,
                "strategy": strategy,
                "outcome": outcome,
                "rationale": rationale,
                "debate": debate or {},
            })
    return rows


# ---------------------------------------------------------------------------
# Coinbase fetch (lifted from run_v4_shadow with a granularity = 5m default)
# ---------------------------------------------------------------------------

async def fetch_coinbase_candles(
    session: aiohttp.ClientSession,
    base: str,
    symbol: str,
    granularity_sec: int = 300,
) -> list[Bar]:
    product_id = symbol.replace("/", "-")
    url = f"{base}/products/{product_id}/candles?granularity={granularity_sec}"
    async with session.get(url, timeout=aiohttp.ClientTimeout(total=15)) as r:
        r.raise_for_status()
        data = await r.json()

    bars: list[Bar] = []
    for row in data:
        try:
            t, low, high, op, close, vol = row[:6]
            bars.append(Bar(
                symbol=Symbol(symbol),
                open=Decimal(str(op)),
                high=Decimal(str(high)),
                low=Decimal(str(low)),
                close=Decimal(str(close)),
                volume=Decimal(str(vol)),
                timestamp_utc=datetime.fromtimestamp(int(t), tz=UTC),
                timeframe="5m",  # type: ignore[arg-type]
            ))
        except Exception as exc:
            log.debug("bad candle row for %s: %s (row=%s)", symbol, exc, row[:6])
            continue
    bars.sort(key=lambda b: b.timestamp_utc)
    return bars


# ---------------------------------------------------------------------------
# Strategy mapping
# ---------------------------------------------------------------------------

def _build_strategy(name: str, *, symbol: str, regime: str) -> Any | None:
    """Construct a strategy instance for the replay.

    The roster is exactly what run_v4_shadow.py wires; we keep the names
    in sync with the ``strategy`` column written by write_decision.
    """
    ctx = _ReplayContext()
    config = {"symbol": symbol, "timeframe": "5m", "state": {"regime": regime}}
    if name == "mean_rev_bb":
        return MeanRevBB(ctx=ctx, config=config), ctx
    if name == "trend_follow" and _TRENDFOLLOW_AVAILABLE:
        return TrendFollow(ctx=ctx, config=config), ctx
    return None


def _outcome_to_side(outcome: str) -> str:
    """Map decisions.outcome -> compare_decisions side.

    BUY  → LONG       (the V4 strategy is long-only today)
    SELL → SHORT      (semantic; only emitted on position-close in live mode)
    FLAT → FLAT
    ERROR → FLAT      (treat as no signal for parity)
    """
    o = (outcome or "FLAT").upper()
    if o == "BUY":
        return "LONG"
    if o == "SELL":
        return "SHORT"
    return "FLAT"


def _proposal_to_side(proposals: Any) -> str:
    """Map an OrderProposal sequence -> compare_decisions side."""
    if not proposals:
        return "FLAT"
    # MeanRevBB / TrendFollow emit ONE proposal per bar at most.
    first = proposals[0]
    side = str(getattr(first, "side", "")).upper()
    if side == "BUY":
        return "LONG"
    if side == "SELL":
        return "SHORT"
    return "FLAT"


# ---------------------------------------------------------------------------
# Replay one decision
# ---------------------------------------------------------------------------

async def replay_decision(
    session: aiohttp.ClientSession,
    decision: dict[str, Any],
    *,
    candle_cache: dict[str, list[Bar]],
) -> dict[str, Any] | None:
    """Replay one decision row through its strategy; return a ParityRow.

    Returns None on hard failure (network, missing strategy, etc.) so the
    caller can skip + retry next tick.
    """
    symbol = decision["symbol"]
    strategy_name = decision["strategy"]
    debate = decision["debate"] or {}
    regime = debate.get("regime") or "unknown"
    bar_ts_iso = debate.get("ts")
    if not bar_ts_iso:
        log.debug("decision %s: no debate.ts; skipping", decision["id"])
        return None

    try:
        bar_ts = datetime.fromisoformat(bar_ts_iso.replace("Z", "+00:00"))
    except Exception:
        log.debug("decision %s: malformed debate.ts=%s", decision["id"], bar_ts_iso)
        return None

    # Pull (cached) candles for this symbol.
    bars = candle_cache.get(symbol)
    if bars is None:
        try:
            bars = await fetch_coinbase_candles(session, _COINBASE_BASE, symbol)
        except Exception as exc:
            log.warning("candles fetch %s failed: %s", symbol, exc)
            return None
        candle_cache[symbol] = bars

    if len(bars) < 25:  # same warm-up gate as run_v4_shadow
        log.info("%s: insufficient bars (%d); skipping", symbol, len(bars))
        return None

    # Locate the bar at debate.ts. Allow ±5-min tolerance for clock skew.
    target_idx = None
    for i, b in enumerate(bars):
        if abs((b.timestamp_utc - bar_ts).total_seconds()) <= 300:
            target_idx = i
            break
    if target_idx is None:
        # Bar is older than the freshest 300 candles (~25 hours @ 5m) —
        # Coinbase doesn't serve that far back from /products/.../candles.
        # Skip silently; this row will fall off the seen-ids ring eventually.
        log.debug("decision %s @ %s: target bar not in window; skipping",
                  decision["id"], bar_ts_iso)
        return None

    history = bars[:target_idx]
    if len(history) < 20:  # strategy needs window=20
        log.debug("decision %s: too little pre-bar history (%d); skipping",
                  decision["id"], len(history))
        return None

    target_bar = bars[target_idx]

    # Build a fresh strategy instance and seed its context with pre-bar history.
    built = _build_strategy(strategy_name, symbol=symbol, regime=regime)
    if built is None:
        log.debug("decision %s: unknown strategy %r; skipping",
                  decision["id"], strategy_name)
        return None
    strat, ctx = built
    ctx.set_history(symbol, history)

    try:
        proposals = strat.on_candle(target_bar)
    except Exception as exc:
        log.warning("replay raised for decision %s: %s", decision["id"], exc)
        return None

    live_side = _outcome_to_side(decision["outcome"])
    replay_side = _proposal_to_side(proposals)

    verdict_envelope = compare_decisions(
        freqtrade={"side": live_side, "pair": symbol},   # "live" V4 decision
        v4={"side": replay_side, "pair": symbol},         # deterministic replay
    )

    # Compose the ParityRow shape the frontend already consumes.
    # ts: prefer the bar timestamp (what the row actually represents).
    row = {
        "ts": target_bar.timestamp_utc.strftime("%Y-%m-%d %H:%M"),
        "pair": symbol,
        "live_action": live_side,
        "backtest_action": replay_side,
        "live_pnl": None,           # not computed at parity-check time
        "backtest_pnl": None,
        "divergent": verdict_envelope["verdict"] != "agree",
        # Debug/telemetry fields (prefixed _) — extra fields are ignored by
        # the strongly-typed frontend ParityRow consumer, present for ops.
        "_verdict": verdict_envelope["verdict"],
        "_decision_id": decision["id"],
        "_strategy": strategy_name,
        "_regime": regime,
        "_decision_ts": decision["ts"].isoformat() if hasattr(decision["ts"], "isoformat") else str(decision["ts"]),
    }
    return row


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def main_async() -> int:
    t0 = time.monotonic()
    dsn = _build_dsn()

    buffer = V4Buffer(_PARITY_JSONL, capacity=128)
    seen = _load_seen_ids()

    rows_written = 0
    rows_skipped_seen = 0
    rows_skipped_replay = 0
    verdicts: dict[str, int] = {"agree": 0, "abstain": 0, "conflict": 0}

    candle_cache: dict[str, list[Bar]] = {}

    async with await psycopg.AsyncConnection.connect(dsn) as conn:
        decisions = await fetch_recent_decisions(conn, _BATCH_LIMIT)
        log.info("pulled %d candidate decisions (limit=%d)", len(decisions), _BATCH_LIMIT)

        async with aiohttp.ClientSession() as session:
            for d in decisions:
                if d["id"] in seen:
                    rows_skipped_seen += 1
                    continue
                row = await replay_decision(session, d, candle_cache=candle_cache)
                if row is None:
                    rows_skipped_replay += 1
                    continue
                buffer.append(row)
                seen.add(d["id"])
                rows_written += 1
                verdicts[row["_verdict"]] = verdicts.get(row["_verdict"], 0) + 1

    _save_seen_ids(seen)

    elapsed = time.monotonic() - t0
    log.info(
        "parity tick done · written=%d skipped_seen=%d skipped_replay=%d "
        "verdicts=%s elapsed=%.1fs jsonl=%s",
        rows_written, rows_skipped_seen, rows_skipped_replay,
        verdicts, elapsed, _PARITY_JSONL,
    )
    return 0


def main() -> int:
    try:
        return asyncio.run(main_async())
    except KeyboardInterrupt:
        log.warning("interrupted")
        return 130
    except Exception:
        log.exception("parity tick fatal")
        return 1


if __name__ == "__main__":
    sys.exit(main())
