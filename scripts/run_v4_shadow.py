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
import time
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
    """Minimum Context that satisfies MeanRevBB / TrendFollow's protocol.

    The strategy calls `get_history` and `get_position`. We seed
    `get_history` from a rolling deque the runner fills each cycle.

    Position behavior:
      - SHADOW mode: get_position always returns None (no inventory).
      - LIVE mode: get_position returns the paper-position from the fills
        ledger (seeded by `set_positions` once per cycle). The strategy
        sees its inventory and will emit SELL exits when appropriate.
    """

    def __init__(self) -> None:
        self._history: dict[str, list[Any]] = {}
        self._positions: dict[str, Any] = {}

    def now(self) -> datetime:
        return datetime.now(timezone.utc)

    def get_position(self, symbol: str) -> Any | None:  # type: ignore[override]
        pos = self._positions.get(str(symbol))
        if pos is None:
            return None
        # Lightweight duck-typed Position — strategies only read .side / .qty.
        class _P:
            pass
        p = _P()
        p.side = pos["side"]
        p.qty = pos["qty"]
        p.avg_price = pos.get("avg_px")
        return p

    def get_history(self, symbol: str, timeframe: str, window: int) -> list[Any]:  # type: ignore[override]
        return self._history.get(str(symbol), [])[-window:]

    def set_history(self, symbol: str, bars: list[Any]) -> None:
        self._history[symbol] = bars

    def set_positions(self, positions: dict[str, dict[str, Any]]) -> None:
        self._positions = positions or {}


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


# ---------------------------------------------------------------------------
# Regime compute — replaces the freqtrade-side hourly HMM cron
# ---------------------------------------------------------------------------
#
# The HMM model state (means, covars, transmat, state_to_label) is loaded
# from /app/regime_hmm.json (baked into the image). We pull BTC 1h candles
# from Coinbase, compute the 4 features the model expects, score each
# Gaussian state and pick argmax. Result is INSERTed into regime_log so
# the dashboard's /api/ops/regime envelope stops going stale.

_REGIME_MODEL: dict[str, Any] | None = None
_REGIME_MODEL_PATH = Path("/app/regime_hmm.json")
_REGIME_LAST_RUN_AT: float = 0.0
_REGIME_INTERVAL_SEC: int = 3600  # hourly


def _load_regime_model() -> dict[str, Any] | None:
    global _REGIME_MODEL
    if _REGIME_MODEL is not None:
        return _REGIME_MODEL
    if not _REGIME_MODEL_PATH.is_file():
        return None
    try:
        _REGIME_MODEL = json.loads(_REGIME_MODEL_PATH.read_text())
        log.info("regime model loaded: %s components, labels=%s",
                 _REGIME_MODEL.get("n_components"),
                 _REGIME_MODEL.get("state_to_label"))
        return _REGIME_MODEL
    except Exception as exc:
        log.warning("regime model load failed: %s", exc)
        return None


def _rsi(closes: list[float], period: int = 14) -> float | None:
    """Wilder RSI on close array; returns None if too few points."""
    if len(closes) < period + 1:
        return None
    deltas = [closes[i] - closes[i-1] for i in range(1, len(closes))]
    gains = [max(d, 0) for d in deltas]
    losses = [-min(d, 0) for d in deltas]
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    for g, l in zip(gains[period:], losses[period:]):
        avg_gain = (avg_gain * (period - 1) + g) / period
        avg_loss = (avg_loss * (period - 1) + l) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - 100.0 / (1.0 + rs)


def _compute_btc_features(bars_1h: list[Any]) -> list[float] | None:
    """[log_return, realized_vol_30d, volume_ratio, rsi_14] from 1h bars.

    Needs ≥30 days × 24h = 720 bars for the realized_vol window; we use the
    last 30 days. Returns None if insufficient history.
    """
    import math
    if len(bars_1h) < 60:
        return None
    closes = [float(b.close) for b in bars_1h]
    volumes = [float(b.volume) for b in bars_1h]

    # 1-bar log return (current bar close vs previous)
    log_return = math.log(closes[-1] / closes[-2]) if closes[-2] > 0 else 0.0

    # Realized vol — std of last min(720, len-1) log returns, annualised
    n_vol = min(720, len(closes) - 1)
    log_rets = [
        math.log(closes[i] / closes[i-1])
        for i in range(len(closes) - n_vol, len(closes))
        if closes[i-1] > 0
    ]
    if log_rets:
        mean = sum(log_rets) / len(log_rets)
        var = sum((r - mean) ** 2 for r in log_rets) / len(log_rets)
        realized_vol = math.sqrt(var) * math.sqrt(24 * 365)  # annualised
    else:
        realized_vol = 0.0

    # Volume ratio — current bar vol / avg of last 20 bars
    avg_vol = sum(volumes[-20:]) / 20 if len(volumes) >= 20 else 1.0
    volume_ratio = volumes[-1] / avg_vol if avg_vol > 0 else 1.0

    rsi = _rsi(closes, period=14)
    if rsi is None:
        return None

    return [log_return, realized_vol, volume_ratio, rsi]


def _gaussian_logpdf_diag(x: list[float], mean: list[float], covar_diag: list[list[float]]) -> float:
    """log-pdf of a multivariate Gaussian with diagonal covariance.

    `covar_diag` is the n×n covariance matrix in the JSON (zeros off-diag).
    """
    import math
    n = len(x)
    log_p = 0.0
    for i in range(n):
        sigma2 = covar_diag[i][i]
        if sigma2 <= 0:
            return -1e9
        diff = x[i] - mean[i]
        log_p += -0.5 * (math.log(2 * math.pi * sigma2) + diff * diff / sigma2)
    return log_p


def _classify_regime(features: list[float]) -> tuple[str, float] | None:
    """Score features against each HMM state; return (label, posterior_prob)."""
    import math
    model = _load_regime_model()
    if not model:
        return None
    # z-score using the training-set feature stats
    fmean = model["feature_mean"]
    fstd = model["feature_std"]
    z = [
        (features[i] - fmean[i]) / (fstd[i] if fstd[i] > 0 else 1.0)
        for i in range(len(features))
    ]
    means = model["means"]
    covars = model["covars"]
    state_to_label = model["state_to_label"]

    # Use UNIFORM prior — the model's `startprob` is the t=0 initial
    # distribution (often [0,1,0,0] from training), not a meaningful
    # steady-state prior. Argmax-of-likelihood is what we want for
    # "which regime best explains this bar's features".
    log_probs = []
    for i in range(model["n_components"]):
        log_lik = _gaussian_logpdf_diag(z, means[i], covars[i])
        log_probs.append(log_lik)
    # softmax for posterior probability
    m = max(log_probs)
    exps = [math.exp(p - m) for p in log_probs]
    total = sum(exps)
    posteriors = [e / total for e in exps]
    best_state = max(range(len(posteriors)), key=lambda i: posteriors[i])
    label = state_to_label.get(str(best_state)) or state_to_label.get(best_state) or "unknown"
    return label, posteriors[best_state]


async def compute_and_write_regime(
    session: aiohttp.ClientSession,
    conn: psycopg.AsyncConnection,
    coinbase_base: str,
) -> tuple[str, float] | None:
    """Pull 30 days of BTC 1h candles, classify regime, write to regime_log.

    Returns the (label, probability) tuple or None on failure.
    """
    bars = await fetch_coinbase_candles(session, coinbase_base, "BTC/USD", granularity_sec=3600)
    if len(bars) < 60:
        log.warning("regime: insufficient BTC 1h history (%d bars)", len(bars))
        return None

    feats = _compute_btc_features(bars)
    if feats is None:
        log.warning("regime: feature compute returned None")
        return None

    result = _classify_regime(feats)
    if result is None:
        log.warning("regime: model not available")
        return None

    label, prob = result
    try:
        async with conn.cursor() as cur:
            # regime_log schema (existing): ts, regime, probability, regime_duration_hours
            # Duration: how long the current regime has held. Look up most recent row.
            await cur.execute(
                "SELECT regime, regime_duration_hours FROM regime_log "
                "WHERE ts > NOW() - INTERVAL '24 hours' ORDER BY ts DESC LIMIT 1"
            )
            row = await cur.fetchone()
            if row and row[0] == label:
                duration = (row[1] or 0) + 1
            else:
                duration = 1  # regime flipped
            await cur.execute(
                """
                INSERT INTO regime_log (ts, regime, probability, regime_duration_hours)
                VALUES (NOW(), %s, %s, %s)
                """,
                (label, float(prob), int(duration)),
            )
        await conn.commit()
        log.info("regime written: %s (p=%.3f, duration=%dh, feats=%s)",
                 label, prob, duration, [round(f, 4) for f in feats])
        return label, prob
    except Exception as exc:
        log.exception("regime_log write failed: %s", exc)
        return None


async def fetch_regime(session: aiohttp.ClientSession, url: str) -> dict[str, Any]:
    """Pull current regime from the dashboard. Returns {} on failure.

    REGIME_OVERRIDE env var, when set to one of {trending_up, trending_down,
    mean_reverting, high_volatility}, replaces the live regime with the
    override value. Useful for end-to-end testing of the BUY/SELL pipeline
    when the live regime would otherwise gate every entry to FLAT.
    """
    override = (os.environ.get("REGIME_OVERRIDE") or "").lower()
    if override in {"trending_up", "trending_down", "mean_reverting", "high_volatility"}:
        log.warning("REGIME_OVERRIDE active: returning %r (live regime ignored)", override)
        return {"current": override, "probability": 1.0, "override": True}

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
# Order placement — LIVE mode only (paper-fill simulator)
# ---------------------------------------------------------------------------
#
# When LIVE_ENGINE_MODE=live, the runner translates strategy proposals into:
#   1. A row in quanta_schema.proposals (the canonical ledger record).
#   2. A row in quanta_schema.orders with status='PROPOSED'.
#   3. On the NEXT cycle (~5 min later), if the proposal is still PROPOSED,
#      we write a Fill row at the current bar's close price (paper fill),
#      flip the order to FILLED, and the strategy sees the new position
#      via get_position() on subsequent cycles.
#
# Coinbase Spot has no shorting; SELL proposals only fire when the strategy
# has an open LONG position. The simulator handles the inventory math.


async def write_proposal_and_order(
    conn: psycopg.AsyncConnection,
    *,
    client_order_id: str,
    venue: str,
    symbol: str,
    side: str,
    qty: Any,
    limit_price: Any | None,
    strategy: str,
    intent: dict[str, Any],
) -> None:
    """Write a proposal + order row (PROPOSED). Idempotent on client_order_id."""
    async with conn.cursor() as cur:
        await cur.execute(
            """
            INSERT INTO quanta_schema.proposals
                (client_order_id, venue, symbol, side, qty, limit_price, strategy, intent)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (client_order_id) DO NOTHING
            """,
            (client_order_id, venue, symbol, side, str(qty),
             str(limit_price) if limit_price is not None else None,
             strategy, json.dumps(intent)),
        )
        await cur.execute(
            """
            INSERT INTO quanta_schema.orders
                (client_order_id, status, last_update)
            VALUES (%s, 'PROPOSED', NOW())
            ON CONFLICT (client_order_id) DO NOTHING
            """,
            (client_order_id,),
        )
    await conn.commit()


async def fill_pending_proposals(
    conn: psycopg.AsyncConnection,
    *,
    close_by_symbol: dict[str, float],
) -> int:
    """Paper-fill any PROPOSED orders at the latest close price for their symbol.

    Returns the count of newly-filled orders. Symbols missing from
    close_by_symbol are skipped (will be retried next cycle).
    """
    filled = 0
    async with conn.cursor() as cur:
        await cur.execute(
            """
            SELECT p.client_order_id, p.symbol, p.side, p.qty
            FROM quanta_schema.proposals p
            JOIN quanta_schema.orders o ON o.client_order_id = p.client_order_id
            WHERE o.status = 'PROPOSED'
            """
        )
        rows = await cur.fetchall()

        for coid, symbol, side, qty in rows:
            price = close_by_symbol.get(symbol)
            if price is None:
                continue
            # write the fill at the simulated price
            await cur.execute(
                """
                INSERT INTO quanta_schema.fills
                    (client_order_id, venue_fill_id, qty, price, fee, fee_currency, side, ts)
                VALUES (%s, %s, %s, %s, 0, 'USD', %s, NOW())
                """,
                (coid, f"paper-{coid[:8]}", str(qty), str(price), side),
            )
            await cur.execute(
                """
                UPDATE quanta_schema.orders
                   SET status = 'FILLED', last_update = NOW()
                 WHERE client_order_id = %s
                """,
                (coid,),
            )
            # Mirror the fill into public.trade_journal so the dashboard's
            # legacy endpoints (/api/ops/readiness, /rebalance, /slack_preview,
            # /explainability, /trades_risk live-tape, /api/state.recent_trades)
            # see V4 paper activity without code changes.
            try:
                await _write_trade_journal_row(
                    cur, coid=coid, symbol=symbol, side=side,
                    qty=qty, price=price,
                )
            except Exception as exc:
                log.warning("trade_journal write failed for %s/%s: %s",
                            symbol, side, exc)
            filled += 1
    if filled:
        await conn.commit()
    return filled


async def _write_trade_journal_row(
    cur: psycopg.AsyncCursor,
    *,
    coid: str,
    symbol: str,
    side: str,
    qty: Any,
    price: Any,
) -> None:
    """Translate a V4 paper fill into a public.trade_journal entry.

    BUY  → INSERT a new open row (closed_at=NULL).
    SELL → UPDATE the most-recent matching open row with closed_at,
           exit_price, derived pnl + pnl_pct + duration_min.

    Schema lives in user_data/modules/trade_journal.py — we hit only the
    columns the dashboard reads. The `external_id` mirrors the V4
    client_order_id so V4-ledger ↔ trade_journal can be cross-referenced.
    """
    qty_f = float(qty)
    price_f = float(price)
    stake = qty_f * price_f

    if side == "BUY":
        await cur.execute(
            """
            INSERT INTO public.trade_journal
                (external_id, pair, direction, opened_at, entry_price, stake,
                 confidence, regime, reasoning)
            VALUES (%s, %s, 'long', NOW(), %s, %s, NULL, NULL,
                    'V4 paper fill from quanta-core (' || %s || ')')
            """,
            (coid, symbol, price_f, stake, coid),
        )
    elif side == "SELL":
        # Close the most-recent open long row on this pair.
        await cur.execute(
            """
            UPDATE public.trade_journal
               SET closed_at    = NOW(),
                   exit_price   = %s,
                   pnl          = (%s - entry_price) * (stake / NULLIF(entry_price, 0)),
                   pnl_pct      = ((%s - entry_price) / NULLIF(entry_price, 0)) * 100.0,
                   duration_min = EXTRACT(EPOCH FROM (NOW() - opened_at)) / 60.0,
                   exit_reason  = 'V4 SELL signal (' || %s || ')'
             WHERE trade_id = (
                SELECT trade_id
                  FROM public.trade_journal
                 WHERE pair = %s AND direction = 'long' AND closed_at IS NULL
                 ORDER BY opened_at DESC
                 LIMIT 1
             )
            """,
            (price_f, price_f, price_f, coid, symbol),
        )


async def fetch_positions(conn: psycopg.AsyncConnection) -> dict[str, dict[str, Any]]:
    """Aggregate net positions per symbol from the fills ledger.

    Returns {symbol: {"side": "BUY"|"SELL", "qty": Decimal, "avg_px": float}}.
    Used to seed the in-process Context so strategies see their open inventory.
    Pure paper accounting; no exchange round-trip.
    """
    out: dict[str, dict[str, Any]] = {}
    async with conn.cursor() as cur:
        await cur.execute(
            """
            SELECT
                p.symbol,
                SUM(CASE WHEN f.side = 'BUY'  THEN f.qty ELSE 0 END) -
                SUM(CASE WHEN f.side = 'SELL' THEN f.qty ELSE 0 END)            AS net_qty,
                SUM(CASE WHEN f.side = 'BUY' THEN f.qty * f.price ELSE 0 END) /
                NULLIF(SUM(CASE WHEN f.side = 'BUY' THEN f.qty ELSE 0 END), 0)  AS avg_buy_px
            FROM quanta_schema.fills f
            JOIN quanta_schema.proposals p USING (client_order_id)
            GROUP BY p.symbol
            HAVING SUM(CASE WHEN f.side='BUY' THEN f.qty ELSE 0 END) -
                   SUM(CASE WHEN f.side='SELL' THEN f.qty ELSE 0 END) > 0
            """
        )
        rows = await cur.fetchall()
    for sym, net_qty, avg_px in rows:
        out[sym] = {
            "side": "BUY",
            "qty": net_qty,
            "avg_px": float(avg_px) if avg_px is not None else None,
        }
    return out


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

async def run_cycle(cfg: Cfg, session: aiohttp.ClientSession, conn: psycopg.AsyncConnection) -> None:
    if not _STRATEGY_AVAILABLE:
        log.error("strategy import failed at startup: %s", _STRATEGY_IMPORT_ERROR)
        return

    # Hourly: recompute regime + INSERT into regime_log. Runs at startup
    # (so a freshly-recycled container immediately refreshes a stale row)
    # and then once per _REGIME_INTERVAL_SEC. Idempotent on accidental
    # double-fire — the new row just supersedes the previous.
    global _REGIME_LAST_RUN_AT
    now_ts = time.time()
    if now_ts - _REGIME_LAST_RUN_AT >= _REGIME_INTERVAL_SEC:
        try:
            await compute_and_write_regime(session, conn, cfg.coinbase_base)
        except Exception as exc:
            log.exception("regime compute failed: %s", exc)
        _REGIME_LAST_RUN_AT = now_ts

    regime_payload = await fetch_regime(session, cfg.regime_url)
    regime_label = regime_payload.get("current") or "unknown"
    log.info("cycle start · regime=%s · n_symbols=%d · mode=%s",
             regime_label, len(cfg.symbols), cfg.mode)

    ctx = _InProcessContext()
    if cfg.mode == "live":
        # 1) Fill any pending proposals from last cycle (paper simulator).
        # 2) Seed positions from the fills ledger so strategies see inventory.
        await fill_pending_then_collect_closes(cfg, session, conn)
        try:
            positions = await fetch_positions(conn)
            ctx.set_positions(positions)
            if positions:
                log.info("loaded %d open positions: %s", len(positions),
                         ", ".join(f"{s}={p['qty']}" for s, p in positions.items()))
        except Exception as exc:
            log.warning("position load failed: %s", exc)

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
                    # Paper order: writes to proposals + orders (PROPOSED).
                    # The NEXT cycle simulates the fill at that bar's close.
                    try:
                        await write_proposal_and_order(
                            conn,
                            client_order_id=str(prop.client_order_id),
                            venue="coinbase-paper",
                            symbol=symbol,
                            side=str(prop.side),
                            qty=prop.qty,
                            limit_price=getattr(prop, "limit_px", None),
                            strategy=strat_name,
                            intent={
                                "regime": regime_label,
                                "close": float(latest_bar.close),
                                "ts": latest_bar.timestamp_utc.isoformat(),
                                "rationale": prop.rationale,
                                "conviction": getattr(strat, "last_conviction", 0.0),
                            },
                        )
                        log.info(
                            "  → paper proposal queued (coid=%s)",
                            str(prop.client_order_id)[:16],
                        )
                    except Exception as exc:
                        log.exception("proposal write failed: %s", exc)


async def fill_pending_then_collect_closes(
    cfg: Cfg, session: aiohttp.ClientSession, conn: psycopg.AsyncConnection,
) -> dict[str, float]:
    """Fetch latest close for each symbol and paper-fill any pending orders.

    Runs at the TOP of each LIVE-mode cycle so that proposals placed last
    cycle get filled at this cycle's close before strategies see their
    inventory. Returns the close prices dict for downstream use.
    """
    close_by_symbol: dict[str, float] = {}
    for symbol in cfg.symbols:
        try:
            bars = await fetch_coinbase_candles(session, cfg.coinbase_base, symbol)
            if bars:
                close_by_symbol[symbol] = float(bars[-1].close)
        except Exception as exc:
            log.debug("close fetch %s failed: %s", symbol, exc)
            continue

    if cfg.mode == "live" and close_by_symbol:
        try:
            n_filled = await fill_pending_proposals(conn, close_by_symbol=close_by_symbol)
            if n_filled:
                log.info("paper-filled %d pending proposal(s)", n_filled)
        except Exception as exc:
            log.exception("fill simulator failed: %s", exc)
    return close_by_symbol


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
