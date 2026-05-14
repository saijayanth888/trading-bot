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

# RiskGovernor — gates new BUY entries in live mode. Added 2026-05-14 after
# audit found V4 production path had ZERO risk approval (no drawdown pause,
# no daily-loss limit, no concurrent-position cap, no correlation gate, no
# Kelly sizing). Lazy import so the runner still boots if the risk module
# fails — better than failing-closed in a way that takes the bot dark.
try:
    from quanta_core.risk.governor import RiskGovernor, RiskConfig, RiskDecision
    _RISK_GOVERNOR_AVAILABLE = True
except Exception as _rg_exc:  # pragma: no cover
    RiskGovernor = None  # type: ignore[assignment]
    RiskConfig = None  # type: ignore[assignment]
    RiskDecision = None  # type: ignore[assignment]
    _RISK_GOVERNOR_AVAILABLE = False
    _RG_IMPORT_ERROR = repr(_rg_exc)

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


async def _read_run_state(conn: psycopg.AsyncConnection) -> tuple[bool, str | None]:
    """Read quanta_schema.run_state (singleton row id=1).

    Returns (paused, reason). Defaults to (False, None) on miss or error
    so a transient DB hiccup doesn't accidentally halt trading. The pause
    flag is operator-controlled via /api/ops/pause + /api/ops/resume.
    """
    try:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT paused, paused_reason FROM quanta_schema.run_state WHERE id = 1"
            )
            row = await cur.fetchone()
        if not row:
            return False, None
        return bool(row[0]), row[1]
    except Exception as exc:
        log.warning("run_state read failed (defaulting to not-paused): %s", exc)
        return False, None


# ---------------------------------------------------------------------------
# RiskGovernor — singleton + gate helper. See top-of-file import block for
# rationale. Phase 1 wiring: gate BUY entries in live mode against the
# drawdown / daily-loss / concurrent-positions / circuit-breaker rails.
# Phase 2 follow-ups (not done in this commit): pair_returns for correlation
# gate, governor.record_trade_close() on fill, real-time equity tracking.
# ---------------------------------------------------------------------------

_GOVERNOR: "RiskGovernor | None" = None


def _get_governor() -> "RiskGovernor | None":
    """Lazy-init the process-wide RiskGovernor. Returns None if the risk
    module wasn't importable — callers must treat None as 'fail-open'."""
    global _GOVERNOR
    if _GOVERNOR is not None:
        return _GOVERNOR
    if not _RISK_GOVERNOR_AVAILABLE:
        return None
    try:
        # Defaults in RiskConfig() mirror user_data/config.json[risk_management]
        # verbatim; explicit env override (RISK_CONFIG_PATH) lets operators
        # tune limits without rebuilding the image.
        cfg_path = os.environ.get("RISK_CONFIG_PATH", "/app/risk_config.json")
        if Path(cfg_path).exists():
            _GOVERNOR = RiskGovernor.from_config_file(cfg_path)
            log.info("RiskGovernor loaded from %s", cfg_path)
        else:
            _GOVERNOR = RiskGovernor(RiskConfig())
            log.info("RiskGovernor initialised with defaults (no %s on disk)", cfg_path)
    except Exception as exc:
        log.exception("RiskGovernor init failed (fail-OPEN — proposals will NOT be gated): %s", exc)
        _GOVERNOR = None
    return _GOVERNOR


def _rg_gate_buy(
    symbol: str,
    qty: Decimal | float,
    signal_price: float,
    conviction: float | None,
    open_positions_all: dict[str, dict[str, dict[str, Any]]],
) -> tuple[bool, str | None, dict[str, Any]]:
    """Run the RiskGovernor entry gate for a single BUY proposal.

    Returns ``(approved, block_reason, extra)``. ``extra`` is the governor's
    structured decision dict (for the debate JSONB) or empty on fail-open.
    Fail-OPEN on any exception — better than taking the bot dark when the
    governor itself is misbehaving.
    """
    gov = _get_governor()
    if gov is None:
        return True, None, {}
    try:
        base_stake = float(abs(Decimal(str(qty)))) * float(signal_price)
        # Flatten per-strategy positions to (sym, stake_quote_ccy) tuples.
        open_positions: list[tuple[str, float]] = []
        for _strat, sym_map in (open_positions_all or {}).items():
            for sym, p in (sym_map or {}).items():
                pos_qty = float(p.get("qty") or 0.0)
                pos_avg = float(p.get("avg_px") or 0.0)
                if pos_qty > 0 and pos_avg > 0:
                    open_positions.append((sym, pos_qty * pos_avg))
        # Equity: prefer config-pinned starting equity (paper mode); fall back
        # to the governor's running peak (live mode would update it itself).
        equity = float(
            os.environ.get("V4_EQUITY_USD")
            or gov.config.starting_equity_for_pct_limits
            or 20000.0
        )
        rd = gov.approve_entry(
            pair=symbol,
            signal_price=float(signal_price),
            base_stake=base_stake,
            equity=equity,
            model_confidence=float(conviction) if conviction is not None else None,
            open_positions=open_positions,
            pair_returns=None,  # Phase 2: wire correlation gate
            open_unrealised_pnl=0.0,  # Phase 2: compute from live MTM
        )
        if not rd.approved:
            return False, f"{rd.blocking_constraint}: {rd.reason}", rd.to_dict()
        return True, None, rd.to_dict()
    except Exception as exc:
        log.exception("RG gate raised (failing OPEN for %s): %s", symbol, exc)
        return True, None, {"rg_error": repr(exc)}


async def fetch_regime(session: aiohttp.ClientSession, url: str) -> dict[str, Any]:
    """Pull current regime from the dashboard. Returns {} on failure.

    REGIME_OVERRIDE env var, when set to one of {trending_up, trending_down,
    mean_reverting, high_volatility}, replaces the live regime with the
    override value. Useful for end-to-end testing of the BUY/SELL pipeline
    when the live regime would otherwise gate every entry to FLAT.
    """
    override = (os.environ.get("REGIME_OVERRIDE") or "").lower()
    if override in {"trending_up", "trending_down", "mean_reverting", "high_volatility"}:
        # Safety: REGIME_OVERRIDE without a paired REGIME_OVERRIDE_UNTIL is a
        # foot-gun in live mode — operator can set it once and forget. Require
        # an ISO8601 expiry; treat expired overrides as cleared.
        until_raw = (os.environ.get("REGIME_OVERRIDE_UNTIL") or "").strip()
        live_mode = (os.environ.get("LIVE_ENGINE_MODE", "shadow").lower() == "live")
        if not until_raw and live_mode:
            log.error(
                "REGIME_OVERRIDE=%r ignored in live mode: REGIME_OVERRIDE_UNTIL "
                "(ISO8601 expiry) is required. Use shadow mode to override "
                "without expiry, OR set REGIME_OVERRIDE_UNTIL=<iso8601>.",
                override,
            )
        else:
            expired = False
            if until_raw:
                try:
                    # Accept naive + tz-aware; treat naive as UTC.
                    expiry = datetime.fromisoformat(until_raw.replace("Z", "+00:00"))
                    if expiry.tzinfo is None:
                        expiry = expiry.replace(tzinfo=timezone.utc)
                    expired = datetime.now(timezone.utc) >= expiry
                except Exception as exc:
                    log.error(
                        "REGIME_OVERRIDE_UNTIL=%r invalid (%s) — refusing override; "
                        "set ISO8601 like 2026-05-14T18:00:00Z",
                        until_raw, exc,
                    )
                    expired = True  # fail-closed: ignore override if expiry invalid
            if not expired:
                log.critical(
                    "REGIME_OVERRIDE active: returning %r (live regime ignored, expires %s)",
                    override, until_raw or "NEVER (shadow-mode only)",
                )
                return {"current": override, "probability": 1.0, "override": True}
            log.warning("REGIME_OVERRIDE %r expired at %s — using live regime", override, until_raw)

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
# Momentum classifier — Wave D (2026-05-14)
# ---------------------------------------------------------------------------
#
# Replaces the FreqAI TFT classifier that died with the freqtrade cutover.
# Honest naming: this is NOT a learned deep model. It's a transparent
# heuristic that produces well-calibrated p_up / p_flat / p_down probs from
# observable features the strategy already touches:
#   momentum_5   (5-bar = 25min return)
#   momentum_20  (20-bar = 100min return)
#   rsi_14       (Wilder RSI, already used elsewhere in this file)
#   regime_bias  (mapped from current regime label)
#   sentiment    (latest sentiment_log score, 0 if missing)
#
# Composition:
#   score_up   = 8.0 × momentum_5 + 2.0 × momentum_20 + 0.012 × (rsi - 50)
#                + regime_bias + 0.5 × sentiment
#   score_down = -score_up
#   score_flat = max(0.0, 1.2 - abs(score_up))   ← high when signal weak
#   (p_up, p_flat, p_down) = softmax([score_up, score_flat, score_down])
#   confidence = (max(p) - 1/3) / (2/3)          ← 0=random, 1=max-conviction
#
# The classifier name "momentum_v1" is recorded in every row so future
# changes can be diffed against this baseline.


# Safety fallback ONLY — runtime weights live in public.classifier_config.
# The operator can UPDATE that table to retune; the classifier reads them
# each cycle with a small in-process cache to keep overhead negligible.
# Hardcoded defaults exist solely so a DB outage doesn't blank the model.
_CLASSIFIER_DEFAULT_WEIGHTS: dict[str, Any] = {
    "w_momentum_5":   0.85,
    "w_momentum_20":  0.45,
    "w_rsi":          0.60,
    "w_regime":       1.00,
    "w_sentiment":    0.50,
    "vol_window":     30,
    "vol_z_clip":     2.5,
    "rsi_z_scale":    20.0,
    "sentiment_amp":  2.0,
    "regime_bias_trending_up":     +0.35,
    "regime_bias_trending_down":   -0.35,
    "regime_bias_mean_reverting":   0.00,
    "regime_bias_high_volatility":  0.00,
    "regime_bias_unknown":          0.00,
    "mean_reverting_regimes":       ["mean_reverting", "high_volatility", "unknown"],
}

# Per-process weight cache. 60-second TTL so an operator UPDATE on the
# config table propagates within a minute without us round-tripping
# the DB every cycle.
_CLASSIFIER_WEIGHT_CACHE: dict[str, Any] = {"weights": None, "fetched_at": 0.0}
_CLASSIFIER_WEIGHT_TTL_S = 60.0


async def _get_classifier_weights(
    conn: psycopg.AsyncConnection,
    classifier: str = "momentum_v1",
) -> dict[str, Any]:
    """Fetch the live tunable weights for ``classifier`` from DB.

    The classifier_config table is the source of truth. The operator can
    UPDATE classifier_config SET weights=... WHERE classifier='momentum_v1'
    to retune live — changes propagate within a minute (TTL cache).

    Falls back to ``_CLASSIFIER_DEFAULT_WEIGHTS`` on any DB error so the
    classifier keeps producing output even if the config table is dropped.
    """
    now = time.time()
    cached = _CLASSIFIER_WEIGHT_CACHE.get("weights")
    if cached and (now - _CLASSIFIER_WEIGHT_CACHE["fetched_at"]) < _CLASSIFIER_WEIGHT_TTL_S:
        return cached
    try:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT weights FROM public.classifier_config WHERE classifier=%s",
                (classifier,),
            )
            row = await cur.fetchone()
            if row and row[0]:
                # row[0] is jsonb → already a dict
                merged = dict(_CLASSIFIER_DEFAULT_WEIGHTS)
                merged.update(row[0])
                _CLASSIFIER_WEIGHT_CACHE["weights"] = merged
                _CLASSIFIER_WEIGHT_CACHE["fetched_at"] = now
                return merged
    except Exception as exc:
        log.debug("classifier weights fetch failed (using defaults): %s", exc)
    _CLASSIFIER_WEIGHT_CACHE["weights"] = _CLASSIFIER_DEFAULT_WEIGHTS
    _CLASSIFIER_WEIGHT_CACHE["fetched_at"] = now
    return _CLASSIFIER_DEFAULT_WEIGHTS


def _realized_vol(closes: list[float], window: int = 30) -> float:
    """Standard deviation of log-returns over the last ``window`` bars.

    Used to convert raw returns into z-scores so saturation adapts to the
    symbol's own volatility instead of a hardcoded ±2% scale. Returns
    1e-6 (effectively infinite vol scaling, hence no signal) when there's
    insufficient data or zero variance.
    """
    import math
    if len(closes) < window + 1:
        return 1e-6
    rets = []
    for i in range(1, window + 1):
        prev = closes[-i - 1]
        cur = closes[-i]
        if prev > 0 and cur > 0:
            rets.append(math.log(cur / prev))
    if len(rets) < 2:
        return 1e-6
    mu = sum(rets) / len(rets)
    var = sum((r - mu) ** 2 for r in rets) / (len(rets) - 1)
    sd = math.sqrt(max(var, 0.0))
    return sd if sd > 1e-9 else 1e-6


def _compute_classifier_probs(
    closes: list[float],
    regime: str,
    regime_probability: float,
    sentiment: float,
    weights: dict[str, Any],
) -> dict[str, Any] | None:
    """3-class probability classifier — transparent heuristic, not a deep model.

    All knobs come from the ``weights`` dict (sourced from
    public.classifier_config in production). No hardcoded thresholds or
    saturation scales — momentum is z-scored against the symbol's own
    realized volatility, RSI is z-scored against its 30-bar mean, and
    regime bias is multiplied by the regime's posterior probability so a
    "trending_up at 55% confidence" pushes less than "trending_up at 99%".

    Horizon: 5–30 minutes (matches the 5-bar / 20-bar momentum windows).
    Output: p_up + p_flat + p_down = 1.0 (softmax). confidence is the
    excess of max(p) over uniform-1/3, normalized to [0, 1].

    Returns None when ``closes`` is too short (< vol_window + 1 bars).
    """
    import math
    vol_window = int(weights.get("vol_window", 30))
    min_bars_needed = max(vol_window + 1, 21)
    if len(closes) < min_bars_needed:
        return None

    last = closes[-1]
    c_5 = closes[-6]
    c_20 = closes[-21]
    if last <= 0 or c_5 <= 0 or c_20 <= 0:
        return None

    # Realized vol of this symbol's recent log-returns. Saturation
    # adapts: 2% on BTC (low vol) ≠ 2% on DOGE (high vol).
    sd = _realized_vol(closes, window=vol_window)
    vol_z_clip = float(weights.get("vol_z_clip", 2.5))

    # Raw returns (kept for the features payload — operator visibility)
    momentum_5 = (last - c_5) / c_5
    momentum_20 = (last - c_20) / c_20

    def _clip(x: float, lim: float = 1.0) -> float:
        return max(-lim, min(lim, x))

    # z-scored momentum: number of stdev moves over the window's expected scale.
    # 5-bar window expects √5 stdev growth, 20-bar expects √20.
    m5_z = momentum_5 / (sd * math.sqrt(5)) if sd > 0 else 0.0
    m20_z = momentum_20 / (sd * math.sqrt(20)) if sd > 0 else 0.0
    momentum_5_signal = _clip(m5_z / vol_z_clip)        # ±vol_z_clip stdev → ±1
    momentum_20_signal = _clip(m20_z / vol_z_clip)

    # RSI — regime-conditional sign, z-scored against rsi_z_scale.
    rsi = _rsi(closes, period=14)
    if rsi is None:
        rsi = 50.0
    rsi_z_scale = float(weights.get("rsi_z_scale", 20.0))
    rsi_signal_raw = _clip((rsi - 50.0) / rsi_z_scale)
    mean_rev_regimes = set(weights.get(
        "mean_reverting_regimes",
        _CLASSIFIER_DEFAULT_WEIGHTS["mean_reverting_regimes"],
    ))
    rsi_flipped = regime in mean_rev_regimes
    rsi_signal = -rsi_signal_raw if rsi_flipped else rsi_signal_raw

    # Sentiment overlay — amplified then clipped.
    sentiment_amp = float(weights.get("sentiment_amp", 2.0))
    sentiment_signal = _clip(sentiment * sentiment_amp)

    # Regime directional bias — looked up by name, then weighted by the
    # regime detector's posterior probability so we push HARDER when the
    # regime is confidently classified and barely at all when it's a coin-flip.
    regime_bias_raw = float(weights.get(
        f"regime_bias_{regime}",
        weights.get("regime_bias_unknown", 0.0),
    ))
    regime_prob_clip = _clip(regime_probability, 1.0)
    regime_bias = regime_bias_raw * max(0.0, regime_prob_clip)

    # Weighted sum — weights are LIVE TUNABLE.
    score_up = (
        float(weights.get("w_momentum_5", 0.85)) * momentum_5_signal
        + float(weights.get("w_momentum_20", 0.45)) * momentum_20_signal
        + float(weights.get("w_rsi", 0.60)) * rsi_signal
        + float(weights.get("w_regime", 1.00)) * regime_bias
        + float(weights.get("w_sentiment", 0.50)) * sentiment_signal
    )
    score_down = -score_up
    # FLAT — high when signal is weak (near zero), zero when |score_up| > 1.
    score_flat = max(0.0, 1.0 - abs(score_up))

    # Softmax across [up, flat, down] — subtract max for numerical stability.
    scores = [score_up, score_flat, score_down]
    m = max(scores)
    exps = [math.exp(s - m) for s in scores]
    denom = sum(exps)
    p_up, p_flat, p_down = (e / denom for e in exps)

    confidence = max(0.0, (max(p_up, p_flat, p_down) - (1.0 / 3.0)) / (2.0 / 3.0))

    return {
        "p_up": p_up,
        "p_flat": p_flat,
        "p_down": p_down,
        "confidence": confidence,
        "features": {
            "momentum_5":         round(momentum_5, 6),
            "momentum_20":        round(momentum_20, 6),
            "momentum_5_z":       round(m5_z, 3),
            "momentum_20_z":      round(m20_z, 3),
            "rsi_14":             round(rsi, 2),
            "rsi_regime_flipped": rsi_flipped,
            "realized_vol_pct":   round(sd * 100.0, 4),
            "regime_bias_raw":    regime_bias_raw,
            "regime_prob":        round(regime_prob_clip, 4),
            "regime_bias_eff":    round(regime_bias, 4),
            "sentiment":          round(sentiment, 4),
        },
    }


async def _get_latest_sentiment(conn: psycopg.AsyncConnection) -> float:
    """Read the freshest sentiment_score from sentiment_log; 0.0 on failure
    (matches the producer's own neutral default).
    """
    try:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT sentiment_score FROM public.sentiment_log "
                "ORDER BY ts DESC LIMIT 1"
            )
            row = await cur.fetchone()
            if row and row[0] is not None:
                return float(row[0])
    except Exception:
        pass
    return 0.0


async def _get_latest_regime_probability(conn: psycopg.AsyncConnection) -> float:
    """Read the freshest regime posterior probability from regime_log.

    Returns 0.5 on failure (mid-confidence) so a DB miss doesn't blow up
    the regime-bias arm of the classifier composition. The regime LABEL
    is already in scope (regime_label) — this is only the posterior
    probability that the label is correct.
    """
    try:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT probability FROM public.regime_log "
                "ORDER BY ts DESC LIMIT 1"
            )
            row = await cur.fetchone()
            if row and row[0] is not None:
                return float(row[0])
    except Exception:
        pass
    return 0.5


async def write_classifier_log(
    conn: psycopg.AsyncConnection,
    *,
    symbol: str,
    probs: dict[str, Any],
    horizon_min: int = 30,    # 5–30 min effective horizon (5-bar to 20-bar momentum windows)
    classifier: str = "momentum_v1",
) -> None:
    """Persist one classifier output row to public.classifier_log."""
    async with conn.cursor() as cur:
        await cur.execute(
            """
            INSERT INTO public.classifier_log
                (ts, symbol, horizon_min, p_up, p_flat, p_down,
                 confidence, features, classifier)
            VALUES (NOW(), %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                symbol, horizon_min,
                float(probs["p_up"]), float(probs["p_flat"]), float(probs["p_down"]),
                float(probs["confidence"]),
                json.dumps(probs.get("features") or {}),
                classifier,
            ),
        )
    await conn.commit()


async def write_meta_signal(
    conn: psycopg.AsyncConnection,
    *,
    symbol: str,
    strategy_outcomes: dict[str, tuple[str, float]],
    regime: str,
) -> None:
    """Synthesize a single meta-signal for ``symbol`` from this cycle's
    strategy outcomes and persist to ``public.meta_signal_log``. The
    dashboard's card 02 reads the latest row per symbol for the
    META-AGENT block.

    Resolution rule:
      * any strategy emitted BUY  → meta_signal = +1
      * any strategy emitted SELL → meta_signal = -1 (BUY beats SELL if
        both happen — long-only paper engine on Coinbase Spot doesn't
        short, so a SELL is always closing an open LONG)
      * otherwise (all FLAT/ERROR/RG_BLOCKED) → meta_signal = 0

    Confidence is the max conviction among voting strategies (those
    that emitted BUY or SELL), or 0 if none voted.

    Wave B of the post-freqtrade rebuild (2026-05-14). Replaces what
    FreqAIMeanRevV1._compute_meta_signals used to emit into the
    in-memory dataframe.
    """
    has_buy = any(o == "BUY" for o, _ in strategy_outcomes.values())
    has_sell = any(o == "SELL" for o, _ in strategy_outcomes.values())
    signal = 1 if has_buy else (-1 if has_sell else 0)
    voting_convictions = [
        c for o, c in strategy_outcomes.values()
        if o in ("BUY", "SELL") and c is not None
    ]
    confidence = max(voting_convictions) if voting_convictions else 0.0

    # Plain-dict for jsonb storage: {strat: outcome}
    strategies_dict = {k: v[0] for k, v in strategy_outcomes.items()}
    summary = ", ".join(f"{k}={v[0]}" for k, v in strategy_outcomes.items())
    reasoning = (
        f"signal={'LONG' if signal > 0 else 'SHORT' if signal < 0 else 'FLAT'} "
        f"conf={confidence:.2f} regime={regime} | {summary}"
    )

    async with conn.cursor() as cur:
        await cur.execute(
            """
            INSERT INTO public.meta_signal_log
                (ts, symbol, signal, confidence, regime, strategies, reasoning)
            VALUES (NOW(), %s, %s, %s, %s, %s, %s)
            """,
            (symbol, signal, float(confidence), regime,
             json.dumps(strategies_dict), reasoning),
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
        # Pull intent JSON too: it carries the regime label + strategy
        # conviction that was current at proposal write-time. Threading these
        # into trade_journal keeps the regime/confidence columns truthful
        # for downstream readers (Tape, readiness, nightly_reflector) — see
        # Wave H-D2 audit, 2026-05-14.
        await cur.execute(
            """
            SELECT p.client_order_id, p.symbol, p.side, p.qty, p.intent
            FROM quanta_schema.proposals p
            JOIN quanta_schema.orders o ON o.client_order_id = p.client_order_id
            WHERE o.status = 'PROPOSED'
            """
        )
        rows = await cur.fetchall()

        for coid, symbol, side, qty, intent in rows:
            price = close_by_symbol.get(symbol)
            if price is None:
                continue
            # The intent column is jsonb; psycopg gives us a dict already,
            # but tolerate str/None defensively.
            intent_d: dict[str, Any] = {}
            if isinstance(intent, dict):
                intent_d = intent
            elif isinstance(intent, str):
                try:
                    intent_d = json.loads(intent) or {}
                except Exception:
                    intent_d = {}
            regime_val = intent_d.get("regime")
            conviction_val = intent_d.get("conviction")
            try:
                confidence_val: float | None = (
                    float(conviction_val) if conviction_val is not None else None
                )
            except (TypeError, ValueError):
                confidence_val = None

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
                    regime=regime_val if isinstance(regime_val, str) else None,
                    confidence=confidence_val,
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
    regime: str | None = None,
    confidence: float | None = None,
) -> None:
    """Translate a V4 paper fill into a public.trade_journal entry.

    BUY  → INSERT a new open row (closed_at=NULL).
    SELL → UPDATE the most-recent matching open row with closed_at,
           exit_price, derived pnl + pnl_pct + duration_min.

    Schema lives in user_data/modules/trade_journal.py — we hit only the
    columns the dashboard reads. The `external_id` mirrors the V4
    client_order_id so V4-ledger ↔ trade_journal can be cross-referenced.

    Unit convention (operator-canonical, see user_data/dashboard/ops_db.py:11-14):
        pnl_pct is a FRACTION (-0.0123 = -1.23%). Every dashboard reader
        multiplies × 100 at display time. Writing percent here causes 100×
        inflation across ~7 surfaces (slack_preview / readiness / rebalance /
        recent_trades JS / live_tape / drawdown / Sharpe). DO NOT × 100 here.

    ``regime`` and ``confidence`` are stamped on BUY-inserted open rows so
    downstream readers (Tape, readiness, nightly_reflector) see the regime
    and strategy conviction the order was emitted under. Both default to
    None so non-cycle callers (tests, migrations) still work.
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
            VALUES (%s, %s, 'long', NOW(), %s, %s, %s, %s,
                    'V4 paper fill from quanta-core (' || %s || ')')
            """,
            (coid, symbol, price_f, stake, confidence, regime, coid),
        )
    elif side == "SELL":
        # Close the most-recent open long row on this pair.
        # pnl_pct is stored as a FRACTION (no × 100); the BUY row's regime
        # and confidence are preserved by NOT touching those columns here.
        await cur.execute(
            """
            UPDATE public.trade_journal
               SET closed_at    = NOW(),
                   exit_price   = %s,
                   pnl          = (%s - entry_price) * (stake / NULLIF(entry_price, 0)),
                   pnl_pct      = (%s - entry_price) / NULLIF(entry_price, 0),
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


async def fetch_positions(
    conn: psycopg.AsyncConnection,
    *,
    strategy: str | None = None,
) -> dict[str, dict[str, Any]]:
    """Aggregate net positions per symbol from the fills ledger.

    Returns ``{symbol: {"side": "BUY", "qty": Decimal, "avg_px": float}}``.
    Pure paper accounting; no exchange round-trip.

    When ``strategy`` is provided, only fills whose proposal was emitted by
    that strategy are counted. This is the strategy-ownership rule that
    prevents MeanRevBB ↔ TrendFollow infighting: TrendFollow no longer
    "sees" a position MeanRevBB opened, so it can't exit it. Each strategy
    manages only its own positions end-to-end.
    """
    out: dict[str, dict[str, Any]] = {}
    if strategy is None:
        # legacy/global aggregate path (used by dashboard endpoints)
        sql = """
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
        params: tuple = ()
    else:
        sql = """
            SELECT
                p.symbol,
                SUM(CASE WHEN f.side = 'BUY'  THEN f.qty ELSE 0 END) -
                SUM(CASE WHEN f.side = 'SELL' THEN f.qty ELSE 0 END)            AS net_qty,
                SUM(CASE WHEN f.side = 'BUY' THEN f.qty * f.price ELSE 0 END) /
                NULLIF(SUM(CASE WHEN f.side = 'BUY' THEN f.qty ELSE 0 END), 0)  AS avg_buy_px
            FROM quanta_schema.fills f
            JOIN quanta_schema.proposals p USING (client_order_id)
            WHERE p.strategy = %s
            GROUP BY p.symbol
            HAVING SUM(CASE WHEN f.side='BUY' THEN f.qty ELSE 0 END) -
                   SUM(CASE WHEN f.side='SELL' THEN f.qty ELSE 0 END) > 0
        """
        params = (strategy,)

    async with conn.cursor() as cur:
        await cur.execute(sql, params)
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

    # Kill-switch gate — quanta_schema.run_state.paused short-circuits the
    # whole proposal+order path. We still refresh regime + write FLAT
    # decisions so the dashboard shows the engine alive but waiting.
    paused, paused_reason = await _read_run_state(conn)
    if paused:
        log.warning("RUN_STATE.paused=True (%s) — skipping proposal/order generation this cycle",
                    paused_reason or "no reason given")

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

    # Per-strategy positions: each strategy only sees positions IT opened.
    # This is the ownership rule that fixes the MeanRevBB <-> TrendFollow
    # infighting (where one strategy entered and the other exited within
    # 5 min). When a strategy can't see another's position, it can't emit
    # a SELL on it. Fetched once per cycle, used to seed strategy-scoped
    # ctx instances below.
    positions_by_strategy: dict[str, dict[str, dict[str, Any]]] = {
        "mean_rev_bb": {},
        "trend_follow": {},
    }
    if cfg.mode == "live":
        # 1) Fill any pending proposals from last cycle (paper simulator).
        await fill_pending_then_collect_closes(cfg, session, conn)
        # 2) Per-strategy position load. Each row's owning strategy comes
        #    from quanta_schema.proposals.strategy (already written on every
        #    BUY/SELL proposal). Fetch in parallel for both strategies.
        for strat_name in list(positions_by_strategy.keys()):
            try:
                positions_by_strategy[strat_name] = await fetch_positions(
                    conn, strategy=strat_name,
                )
            except Exception as exc:
                log.warning("position load failed for %s: %s", strat_name, exc)
        # one-line summary
        for strat_name, ps in positions_by_strategy.items():
            if ps:
                log.info(
                    "%s owns %d position(s): %s",
                    strat_name, len(ps),
                    ", ".join(f"{s}={p['qty']}" for s, p in ps.items()),
                )

    for symbol in cfg.symbols:
        try:
            bars = await fetch_coinbase_candles(session, cfg.coinbase_base, symbol)
        except Exception as exc:
            log.warning("candles fetch %s failed: %s", symbol, exc)
            continue

        if len(bars) < 25:  # warm-up: need at least window=20 + a few buffers
            log.info("%s: %d bars (warm-up)", symbol, len(bars))
            continue

        # Build the strategy roster — each strategy gets its OWN Context
        # instance with ONLY its own positions visible. This enforces the
        # strategy-ownership rule that fixes the open->close 5-min stomp
        # between MeanRevBB and TrendFollow.
        roster: list[tuple[str, Any]] = []

        ctx_mr = _InProcessContext()
        ctx_mr.set_history(symbol, bars[:-1])
        ctx_mr.set_positions(positions_by_strategy.get("mean_rev_bb") or {})
        roster.append(("mean_rev_bb", MeanRevBB(
            ctx=ctx_mr,
            config={"symbol": symbol, "timeframe": "5m", "state": {"regime": regime_label}},
        )))

        if _TRENDFOLLOW_AVAILABLE:
            ctx_tf = _InProcessContext()
            ctx_tf.set_history(symbol, bars[:-1])
            ctx_tf.set_positions(positions_by_strategy.get("trend_follow") or {})
            roster.append(("trend_follow", TrendFollow(
                ctx=ctx_tf,
                config={"symbol": symbol, "timeframe": "5m", "state": {"regime": regime_label}},
            )))

        latest_bar = bars[-1]

        # Wave B: aggregate this cycle's per-strategy outcomes (BUY/SELL/
        # FLAT/ERROR) so we can synthesize ONE meta-signal row per symbol
        # at the end of the inner loop. Strategy outcomes default to FLAT
        # and are overridden by each path below. Conviction is the
        # strategy's last_conviction attribute when it emitted a proposal.
        strategy_outcomes: dict[str, tuple[str, float]] = {}

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
                strategy_outcomes[strat_name] = ("ERROR", 0.0)
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
                strategy_outcomes[strat_name] = ("FLAT", 0.0)
                continue

            # Strategy emitted ≥1 proposal — use the first one's side as
            # the strategy's outcome for meta-signal purposes. Conviction
            # = strategy.last_conviction (0.0 if missing).
            strategy_outcomes[strat_name] = (
                str(proposals[0].side).upper(),
                float(getattr(strat, "last_conviction", 0.0) or 0.0),
            )

            for prop in proposals:
                # RiskGovernor entry gate (Phase 1) — applies to BUY entries
                # in live mode only. SELL = exit, not approve_entry territory.
                # Shadow mode is observability-only; no gate needed there.
                rg_block_reason: str | None = None
                rg_extra: dict[str, Any] = {}
                if cfg.mode == "live" and str(prop.side).upper() == "BUY":
                    _approved, rg_block_reason, rg_extra = _rg_gate_buy(
                        symbol=symbol,
                        qty=prop.qty,
                        signal_price=float(latest_bar.close),
                        conviction=getattr(strat, "last_conviction", None),
                        open_positions_all=positions_by_strategy,
                    )

                decision_outcome = "RG_BLOCKED" if rg_block_reason else str(prop.side)
                decision_rationale = rg_block_reason or prop.rationale
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
                        "rg": rg_extra,
                    },
                    outcome=decision_outcome,
                    rationale=decision_rationale,
                )

                if rg_block_reason:
                    log.warning(
                        "%s @ %s [%s]: %s qty=%s → RG_BLOCKED: %s",
                        symbol, latest_bar.close, strat_name, prop.side, prop.qty,
                        rg_block_reason,
                    )
                    continue  # skip the proposal/order write for this prop

                log.info(
                    "%s @ %s [%s]: %s qty=%s (conviction=%.2f)",
                    symbol, latest_bar.close, strat_name, prop.side, prop.qty,
                    getattr(strat, "last_conviction", 0.0),
                )

                if cfg.mode == "live" and not paused:
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
                elif cfg.mode == "live" and paused:
                    log.info(
                        "  → paper proposal SKIPPED (run_state.paused; coid=%s)",
                        str(prop.client_order_id)[:16],
                    )

        # Wave B: synthesize ONE meta-signal row per symbol per cycle from
        # the per-strategy outcomes gathered above. Writes to
        # public.meta_signal_log → dashboard card 02 META-AGENT block.
        # Never raises — if the write fails we just log and move on; a
        # failed meta-signal write must not interrupt trading.
        if strategy_outcomes:
            try:
                await write_meta_signal(
                    conn,
                    symbol=symbol,
                    strategy_outcomes=strategy_outcomes,
                    regime=regime_label,
                )
            except Exception as exc:
                log.warning("meta_signal write failed for %s: %s", symbol, exc)

        # Wave D: compute the momentum classifier (p_up/p_flat/p_down/
        # confidence) and persist to public.classifier_log. Weights are
        # LIVE TUNABLE via public.classifier_config (no hardcoded knobs).
        # Sentiment + regime_probability are pulled fresh from their
        # respective log tables so the classifier composes from current
        # data, not stale config. Never raises — classifier output is
        # observability-only and must not gate trading.
        try:
            sent_now = await _get_latest_sentiment(conn)
            regime_prob_now = await _get_latest_regime_probability(conn)
            weights = await _get_classifier_weights(conn)
            closes_list = [float(b.close) for b in bars]
            probs = _compute_classifier_probs(
                closes_list,
                regime=regime_label,
                regime_probability=regime_prob_now,
                sentiment=sent_now,
                weights=weights,
            )
            if probs is not None:
                await write_classifier_log(
                    conn, symbol=symbol, probs=probs,
                )
        except Exception as exc:
            log.warning("classifier write failed for %s: %s", symbol, exc)


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
