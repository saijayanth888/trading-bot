"""V4 shadow-mode runner — Coinbase REST → MeanRevBB → quanta_schema.decisions.

This is the minimum-viable "is V4 ready to trade?" runner. It exists so the
operator can see real V4 decisions land alongside freqtrade's live trades,
without standing up the full LiveEngine + WebSocket + execution stack on
day-zero. Once shadow output is clean, the same script is extended (Phase 3
of the cutover plan) to actually place orders via the V4 ExecutionEngine.

What this DOES tonight:
* For each crypto pair in `universe.json`, every cycle (default 5 min):
    1. Pull last 60 5m candles from Coinbase Exchange public REST.
    2. Pull current regime from the dashboard (`/api/ops/regime`).
    3. Feed candles to a MeanRevBB strategy via a thin in-process Context.
    4. If the strategy emits a BUY/SELL proposal, write a Decision row
       into `quanta_schema.decisions`. The full proposal envelope is
       persisted as JSONB.
    5. If the strategy emits nothing, write a FLAT decision so the operator
       can see "V4 looked at this bar and chose to do nothing" (helps
       answer "is the engine even running?").

What this DOES NOT do:
* Place orders. No exchange-side state changes. Pure shadow.
* WebSocket streams (REST poll is fine at 5m cadence; we can upgrade).
* Manage positions / reconcile. The simple Context returns None for
  `get_position` so MeanRevBB stays in entry-evaluation mode forever
  (the exit branch never triggers in shadow). Position management
  comes online with order placement in Phase 3.

Env:
    QUANTA_DB_DSN        postgres DSN (e.g. postgresql://tradebot:pw@postgres:5432/tradebot)
    REGIME_FEED_URL      default http://host.docker.internal:8081/api/ops/regime
    COINBASE_REST_BASE   default https://api.exchange.coinbase.com
    SHADOW_CYCLE_SEC     default 300 (5 min)
    SHADOW_SYMBOLS       comma-separated; default reads from /app/universe.json crypto.pairs
    LIVE_ENGINE_MODE     "shadow" (default) | "live" (PLACEHOLDER — Phase 3 wires order placement)
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

import aiohttp
import psycopg

# We import the strategy lazily so the script still runs as a standalone
# diagnostic if the strategy import is broken (writes ERROR rows instead
# of crashing the cycle).
try:
    from quanta_core.strategy.mean_rev_bb import MeanRevBB
    from quanta_core.types import Bar, Symbol
    _STRATEGY_AVAILABLE = True
except Exception as _strat_exc:  # pragma: no cover — bootstrapping path
    MeanRevBB = None  # type: ignore[assignment]
    Bar = None  # type: ignore[assignment]
    Symbol = None  # type: ignore[assignment]
    _STRATEGY_AVAILABLE = False
    _STRATEGY_IMPORT_ERROR = repr(_strat_exc)

# Second strategy — added in parallel; tolerate absence (import-on-demand).
try:
    from quanta_core.strategy.trend_follow import TrendFollow
    _TRENDFOLLOW_AVAILABLE = True
except Exception:  # pragma: no cover — bootstrapping path
    TrendFollow = None  # type: ignore[assignment]
    _TRENDFOLLOW_AVAILABLE = False

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("quanta.shadow")


# ---------------------------------------------------------------------------
# Config + Context
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Cfg:
    dsn: str
    regime_url: str
    coinbase_base: str
    cycle_sec: int
    symbols: list[str]
    mode: str  # "shadow" | "live"

    @classmethod
    def from_env(cls) -> "Cfg":
        # Prefer keyword-style env vars over QUANTA_DB_DSN — passwords with
        # `@` (which we have) break the URL parser. Compose with explicit
        # POSTGRES_* vars so psycopg gets clean key=val DSN strings.
        if "POSTGRES_PASSWORD" in os.environ:
            dsn = " ".join([
                f"host={os.environ.get('POSTGRES_HOST', 'postgres')}",
                f"port={os.environ.get('POSTGRES_PORT', '5432')}",
                f"user={os.environ.get('POSTGRES_USER', 'tradebot')}",
                f"password={os.environ['POSTGRES_PASSWORD']}",
                f"dbname={os.environ.get('POSTGRES_DB', 'tradebot')}",
            ])
        else:
            dsn = os.environ["QUANTA_DB_DSN"]
        regime = os.environ.get(
            "REGIME_FEED_URL",
            "http://host.docker.internal:8081/api/ops/regime",
        )
        cb_base = os.environ.get(
            "COINBASE_REST_BASE", "https://api.exchange.coinbase.com",
        )
        cycle = int(os.environ.get("SHADOW_CYCLE_SEC", "300"))
        mode = os.environ.get("LIVE_ENGINE_MODE", "shadow").lower()
        sym_env = os.environ.get("SHADOW_SYMBOLS")
        if sym_env:
            symbols = [s.strip() for s in sym_env.split(",") if s.strip()]
        else:
            uni = json.loads(Path("/app/universe.json").read_text())
            symbols = (uni.get("crypto") or {}).get("pairs") or []
        return cls(
            dsn=dsn, regime_url=regime, coinbase_base=cb_base,
            cycle_sec=cycle, symbols=symbols, mode=mode,
        )


class _InProcessContext:
    """Minimum Context that satisfies MeanRevBB's protocol.

    The strategy only calls `get_history` and `get_position`. We seed
    `get_history` from a rolling deque kept by the runner; `get_position`
    always returns None in shadow mode (no positions = strategy stays in
    entry-evaluation mode).
    """

    def __init__(self) -> None:
        self._history: dict[str, list[Any]] = {}

    def now(self) -> datetime:
        return datetime.now(timezone.utc)

    def get_position(self, symbol: str) -> None:  # type: ignore[override]
        return None  # always flat in shadow mode

    def get_history(self, symbol: str, timeframe: str, window: int) -> list[Any]:  # type: ignore[override]
        return self._history.get(str(symbol), [])[-window:]

    def set_history(self, symbol: str, bars: list[Any]) -> None:
        self._history[symbol] = bars


# ---------------------------------------------------------------------------
# Data feeds
# ---------------------------------------------------------------------------

async def fetch_coinbase_candles(
    session: aiohttp.ClientSession, base: str, symbol: str, granularity_sec: int = 300,
) -> list[Any]:
    """Pull recent candles from Coinbase Exchange public REST.

    Symbol mapping: "BTC/USD" → "BTC-USD" (product id format).
    Returns a list of Bars sorted oldest-first.
    Free, no auth, ~300 candles max per call.
    """
    if not _STRATEGY_AVAILABLE:
        return []
    product_id = symbol.replace("/", "-")
    url = f"{base}/products/{product_id}/candles?granularity={granularity_sec}"
    async with session.get(url, timeout=aiohttp.ClientTimeout(total=15)) as r:
        r.raise_for_status()
        data = await r.json()

    bars: list[Any] = []
    # Coinbase returns [time, low, high, open, close, volume] newest-first.
    for row in data:
        try:
            t, low, high, op, close, vol = row[0], row[1], row[2], row[3], row[4], row[5]
            bars.append(Bar(
                symbol=Symbol(symbol),
                open=Decimal(str(op)),
                high=Decimal(str(high)),
                low=Decimal(str(low)),
                close=Decimal(str(close)),
                volume=Decimal(str(vol)),
                timestamp_utc=datetime.fromtimestamp(int(t), tz=timezone.utc),
                timeframe="5m",  # type: ignore[arg-type]
            ))
        except Exception as exc:
            log.warning("bad candle row for %s: %s (row=%s)", symbol, exc, row[:6])
            continue
    bars.sort(key=lambda b: b.timestamp_utc)
    return bars


async def fetch_regime(session: aiohttp.ClientSession, url: str) -> dict[str, Any]:
    """Pull current regime from the dashboard. Returns {} on failure."""
    try:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=5)) as r:
            r.raise_for_status()
            envelope = await r.json()
            return (envelope.get("data") or {}) if isinstance(envelope, dict) else {}
    except Exception as exc:
        log.warning("regime fetch failed: %s", exc)
        return {}


# ---------------------------------------------------------------------------
# Ledger writer
# ---------------------------------------------------------------------------

async def write_decision(
    conn: psycopg.AsyncConnection,
    *,
    symbol: str,
    strategy: str,
    debate: dict[str, Any],
    outcome: str,
    rationale: str,
) -> None:
    async with conn.cursor() as cur:
        await cur.execute(
            """
            INSERT INTO quanta_schema.decisions
                (symbol, strategy, debate, outcome, rationale)
            VALUES (%s, %s, %s, %s, %s)
            """,
            (symbol, strategy, json.dumps(debate), outcome, rationale),
        )
    await conn.commit()


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

async def run_cycle(cfg: Cfg, session: aiohttp.ClientSession, conn: psycopg.AsyncConnection) -> None:
    if not _STRATEGY_AVAILABLE:
        log.error("strategy import failed at startup: %s", _STRATEGY_IMPORT_ERROR)
        return

    regime_payload = await fetch_regime(session, cfg.regime_url)
    regime_label = regime_payload.get("current") or "unknown"
    log.info("cycle start · regime=%s · n_symbols=%d · mode=%s",
             regime_label, len(cfg.symbols), cfg.mode)

    ctx = _InProcessContext()

    for symbol in cfg.symbols:
        try:
            bars = await fetch_coinbase_candles(session, cfg.coinbase_base, symbol)
        except Exception as exc:
            log.warning("candles fetch %s failed: %s", symbol, exc)
            continue

        if len(bars) < 25:  # warm-up: need at least window=20 + a few buffers
            log.info("%s: %d bars (warm-up)", symbol, len(bars))
            continue

        ctx.set_history(symbol, bars[:-1])  # history excludes the bar we'll feed
        latest_bar = bars[-1]

        # Build the strategy roster — each strategy gets its own clean
        # instance per cycle (cheap; they're tiny).
        roster: list[tuple[str, Any]] = []
        roster.append(("mean_rev_bb", MeanRevBB(
            ctx=ctx,
            config={"symbol": symbol, "timeframe": "5m", "state": {"regime": regime_label}},
        )))
        if _TRENDFOLLOW_AVAILABLE:
            roster.append(("trend_follow", TrendFollow(
                ctx=ctx,
                config={"symbol": symbol, "timeframe": "5m", "state": {"regime": regime_label}},
            )))

        for strat_name, strat in roster:
            try:
                proposals = strat.on_candle(latest_bar)
            except Exception as exc:
                log.exception("on_candle(%s/%s) raised: %s", symbol, strat_name, exc)
                await write_decision(
                    conn, symbol=symbol, strategy=strat_name,
                    debate={"error": repr(exc), "regime": regime_label},
                    outcome="ERROR", rationale=f"strategy raised: {exc!r}",
                )
                continue

            if not proposals:
                await write_decision(
                    conn, symbol=symbol, strategy=strat_name,
                    debate={
                        "regime": regime_label,
                        "close": float(latest_bar.close),
                        "ts": latest_bar.timestamp_utc.isoformat(),
                        "verdict": "no_signal",
                    },
                    outcome="FLAT",
                    rationale=f"no signal; regime={regime_label}",
                )
                log.info("%s @ %s [%s]: FLAT", symbol, latest_bar.close, strat_name)
                continue

            for prop in proposals:
                await write_decision(
                    conn, symbol=symbol, strategy=strat_name,
                    debate={
                        "regime": regime_label,
                        "close": float(latest_bar.close),
                        "ts": latest_bar.timestamp_utc.isoformat(),
                        "side": str(prop.side),
                        "qty": str(prop.qty),
                        "rationale": prop.rationale,
                        "conviction": getattr(strat, "last_conviction", 0.0),
                    },
                    outcome=str(prop.side),
                    rationale=prop.rationale,
                )
                log.info(
                    "%s @ %s [%s]: %s qty=%s (conviction=%.2f)",
                    symbol, latest_bar.close, strat_name, prop.side, prop.qty,
                    getattr(strat, "last_conviction", 0.0),
                )

    if cfg.mode == "live":
        log.warning("LIVE MODE requested but order placement is not wired in this "
                    "runner yet (Phase 3 of the cutover plan). Treating as SHADOW.")


async def main() -> None:
    cfg = Cfg.from_env()
    log.info("V4 shadow runner starting · symbols=%s · cycle=%ds · mode=%s",
             cfg.symbols, cfg.cycle_sec, cfg.mode)

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:  # pragma: no cover — Windows / sandboxed
            pass

    while not stop.is_set():
        try:
            async with await psycopg.AsyncConnection.connect(cfg.dsn) as conn:
                async with aiohttp.ClientSession() as session:
                    await run_cycle(cfg, session, conn)
        except Exception:
            log.exception("cycle failed; sleeping then retrying")

        try:
            await asyncio.wait_for(stop.wait(), timeout=cfg.cycle_sec)
        except asyncio.TimeoutError:
            continue

    log.info("shadow runner shutting down")


if __name__ == "__main__":
    sys.exit(asyncio.run(main()) or 0)
