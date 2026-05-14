"""
Postgres-backed reads for the Ops tab.

Read-only queries against the trading-bot DB. Uses the same DSN-builder
pattern as data_sources.py so it inherits the URL-encoded password handling.
Each function returns a dict ready for the typed envelope.

All SELECTs are bounded — LIMIT clauses or time windows — so no query can
fan out unbounded rows.

Convention: every ``_pct`` field returned by functions in this module is a
**fraction** (e.g. ``-0.0123`` = -1.23%). Callers must multiply by 100 at
the display boundary — never inside this module. The /api/ops/* envelopes
and the dashboard JS each do their own × 100 at the render edge.
"""

from __future__ import annotations

import logging
import os
from typing import Any

logger = logging.getLogger(__name__)

try:
    import psycopg
    from psycopg.rows import dict_row
    _HAVE_PG = True
except Exception:
    psycopg = None
    dict_row = None
    _HAVE_PG = False


def _resolve_dsn() -> str:
    """Mirror of data_sources._resolve_dsn — keep them in sync if either changes."""
    from urllib.parse import quote_plus
    explicit = os.environ.get("DATABASE_URL", "").strip()
    if explicit:
        return explicit
    user = os.environ.get("POSTGRES_USER", "tradebot")
    password = os.environ.get("POSTGRES_PASSWORD", "tradebot-change-me")
    host = os.environ.get("POSTGRES_HOST", "postgres")
    port = os.environ.get("POSTGRES_PORT", "5432")
    db = os.environ.get("POSTGRES_DB", "tradebot")
    return f"postgresql://{quote_plus(user)}:{quote_plus(password)}@{host}:{port}/{db}"


def _connect():
    if not _HAVE_PG:
        raise RuntimeError("psycopg not installed")
    # 2-second statement timeout per connection — matches the endpoint timeout
    return psycopg.connect(_resolve_dsn(), row_factory=dict_row, options="-c statement_timeout=2000")


# --------------------------------------------------------------------------
# Regime
# --------------------------------------------------------------------------


def regime_latest() -> dict[str, Any] | None:
    if not _HAVE_PG:
        return None
    with _connect() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT regime, probability, regime_duration_hours, ts "
            "FROM regime_log ORDER BY ts DESC LIMIT 1"
        )
        row = cur.fetchone()
        return dict(row) if row else None


def regime_transitions_24h(limit: int = 10) -> list[dict[str, Any]]:
    """Return up to ``limit`` actual regime *changes* in the last 24h."""
    if not _HAVE_PG:
        return []
    with _connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            WITH ranked AS (
              SELECT ts, regime, regime_duration_hours,
                     LAG(regime) OVER (ORDER BY ts) AS prev_regime
              FROM regime_log
              WHERE ts > NOW() - INTERVAL '24 hours'
            )
            SELECT ts, regime, regime_duration_hours
            FROM ranked
            WHERE regime IS DISTINCT FROM prev_regime
            ORDER BY ts DESC
            LIMIT %s
            """,
            (limit,),
        )
        return [dict(r) for r in cur.fetchall()]


# --------------------------------------------------------------------------
# Sentiment
# --------------------------------------------------------------------------


def sentiment_latest() -> dict[str, Any] | None:
    if not _HAVE_PG:
        return None
    with _connect() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT ts, sentiment_score, confidence, agreement, n_headlines, "
            "       claude_score, claude_impact, llama_score, llama_impact, "
            "       fear_greed_value, fear_greed_classification, "
            "       community_score_avg, key_events, "
            "       n_reddit, sources_ok, sources_failed "
            "FROM sentiment_log ORDER BY ts DESC LIMIT 1"
        )
        row = cur.fetchone()
        return dict(row) if row else None


# --------------------------------------------------------------------------
# Open positions — quanta-core paper engine source of truth
# Post-2026-05-14 freqtrade decommissioning: trade_journal's open rows
# (closed_at IS NULL) are the canonical open-position list. quanta-core
# mirrors fills here on every paper-fill, so this is always current.
# --------------------------------------------------------------------------


def open_positions(limit: int = 50) -> list[dict[str, Any]]:
    """All currently-open paper positions from trade_journal.

    Shape matches what the dashboard `positions` payload expects:
    ``pair``, ``open_rate`` (= entry_price), ``stake_amount`` (= stake),
    ``current_profit`` (None — would need a live price feed to compute),
    ``open_date`` (ISO timestamp), ``trade_id``, ``direction``.
    """
    if not _HAVE_PG:
        return []
    with _connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT trade_id, pair, direction, entry_price, stake,
                   opened_at, external_id, regime
            FROM trade_journal
            WHERE closed_at IS NULL
            ORDER BY opened_at DESC
            LIMIT %s
            """,
            (limit,),
        )
        rows = cur.fetchall()
        return [
            {
                "trade_id": int(r["trade_id"]),
                "pair": r["pair"],
                "direction": r["direction"],
                "open_rate": float(r["entry_price"]) if r["entry_price"] is not None else None,
                "stake_amount": float(r["stake"]) if r["stake"] is not None else None,
                "current_profit": None,  # would need a live-quote join; UI tolerates None
                "open_date": r["opened_at"].isoformat() if r["opened_at"] else None,
                "external_id": r["external_id"],
                "regime_at_entry": r["regime"],
            }
            for r in rows
        ]


# --------------------------------------------------------------------------
# On-chain enrich — read from derivatives_features + macro_features
# Post-2026-05-14 freqtrade decommissioning: the previous on-chain enrich
# path lived inside latest_state_from_df() and only fired when the df had
# rows (i.e. when freqtrade's pair_candles returned). Now that the df is
# always None on the no-freqtrade path, this helper extracts the same DB
# lookup so _v4_state_fallback can call it directly.
#
# Mapping (mirrors data_sources.latest_state_from_df lines 587-604):
#   onchain_netflow_z   ← OKX funding_rate × 10000 (basis points)
#   onchain_mvrv        ← BTC MVRV (only for BTC/USD; 1.0 neutral elsewhere)
#   onchain_whale_count ← log1p(taker_buy_vol_usd) over last hour
# --------------------------------------------------------------------------


def onchain_latest(pair: str | None) -> dict[str, Any]:
    """Latest on-chain values for the dashboard's Market context card.

    Returns a dict with keys ``netflow_z``, ``mvrv``, ``whale_count_1h``.
    Missing values are None; UI renders them as ``—``.
    """
    out: dict[str, Any] = {"netflow_z": None, "mvrv": None, "whale_count_1h": None}
    if not _HAVE_PG or not pair:
        return out
    try:
        with _connect() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT funding_rate, taker_buy_vol_usd, taker_sell_vol_usd "
                "FROM derivatives_features WHERE pair=%s ORDER BY ts DESC LIMIT 1",
                (pair,),
            )
            deriv = cur.fetchone() or {}
            cur.execute("SELECT btc_mvrv FROM macro_features ORDER BY ts DESC LIMIT 1")
            macro = cur.fetchone() or {}
    except Exception as exc:
        logger.debug("onchain_latest(%s) failed: %s", pair, exc)
        return out

    fr = deriv.get("funding_rate")
    if fr is not None:
        # Mirror data_sources.py:595 — express as basis points × 100.
        out["netflow_z"] = float(fr) * 10000.0
    buy = deriv.get("taker_buy_vol_usd") or 0.0
    if buy > 0:
        import math
        out["whale_count_1h"] = math.log1p(float(buy))

    if pair.split("/")[0].upper() == "BTC":
        mvrv = macro.get("btc_mvrv")
        if mvrv is not None:
            out["mvrv"] = float(mvrv)
    else:
        out["mvrv"] = 1.0  # neutral for non-BTC pairs
    return out


def sentiment_hourly_24h() -> list[dict[str, Any]]:
    """Hourly aggregate over last 24h (avg score weighted by n_headlines)."""
    if not _HAVE_PG:
        return []
    with _connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT date_trunc('hour', ts) AS hour,
                   AVG(sentiment_score) AS score,
                   SUM(n_headlines)     AS n
            FROM sentiment_log
            WHERE ts > NOW() - INTERVAL '24 hours'
            GROUP BY 1
            ORDER BY 1
            """
        )
        return [{"hour": r["hour"], "score": float(r["score"] or 0), "n": int(r["n"] or 0)} for r in cur.fetchall()]


# --------------------------------------------------------------------------
# Trades + risk
# --------------------------------------------------------------------------


def trades_risk_summary() -> dict[str, Any]:
    """Pull what we can from the DB; the freqtrade API call layer fills in
    the live open-positions list at the endpoint level.
    """
    out: dict[str, Any] = {
        "open_count_db": None,
        "daily_pnl_usd": None,
        "daily_pnl_pct": None,
        "drawdown_pct_30d": None,
        "circuit_breaker": {"active": False, "cooldown_remaining_min": 0},
        "live_tape": [],
    }
    if not _HAVE_PG:
        return out

    with _connect() as conn, conn.cursor() as cur:
        # Daily P&L (closed trades today, UTC). Schema: pnl=USD, pnl_pct=
        # per-trade fractional return on the trade's own stake (NOT
        # portfolio-wide). `daily_pnl_pct` is intentionally NOT computed
        # here — see below — because SUM(pnl_pct) is meaningless: on a
        # 50-fill paper-engine day each row contributes a few percent of
        # its own stake, summed = 277% nonsense. The caller MUST compute
        # day_pnl_pct = daily_pnl_usd / day_start_equity itself, since
        # only the caller knows the right denominator (combined equity
        # vs crypto-only vs stocks-only).
        cur.execute(
            """
            SELECT
                COALESCE(SUM(pnl), 0)        AS pnl_usd,
                COUNT(*)                     AS n
            FROM trade_journal
            WHERE closed_at IS NOT NULL
              AND closed_at >= date_trunc('day', NOW() AT TIME ZONE 'UTC')
            """
        )
        row = cur.fetchone() or {}
        out["daily_pnl_usd"] = float(row.get("pnl_usd") or 0)
        out["daily_pnl_pct"] = None  # caller computes from USD + equity
        out["closed_today"] = int(row.get("n") or 0)

        # Live tape — last 5 closed trades, newest first
        cur.execute(
            """
            SELECT pair, direction AS side, opened_at, closed_at,
                   pnl_pct, pnl AS pnl_abs, regime AS regime_at_entry
            FROM trade_journal
            WHERE closed_at IS NOT NULL
            ORDER BY closed_at DESC
            LIMIT 5
            """
        )
        rows = cur.fetchall()
        # Re-key closed_at → exit_time so the endpoint payload stays stable.
        out["live_tape"] = []
        for r in rows:
            d = dict(r)
            d["exit_time"] = d.pop("closed_at", None)
            out["live_tape"].append(d)

        # 30-day max drawdown approximation: peak-to-trough on cumulative P&L%.
        cur.execute(
            """
            WITH cum AS (
                SELECT closed_at,
                       SUM(pnl_pct) OVER (ORDER BY closed_at) AS cum_pct
                FROM trade_journal
                WHERE closed_at IS NOT NULL
                  AND closed_at > NOW() - INTERVAL '30 days'
            )
            SELECT MIN(cum_pct - max_cum) AS max_drawdown
            FROM (
                SELECT cum_pct,
                       MAX(cum_pct) OVER (ORDER BY closed_at) AS max_cum
                FROM cum
            ) t
            """
        )
        row = cur.fetchone() or {}
        dd = row.get("max_drawdown")
        out["drawdown_pct_30d"] = float(dd) if dd is not None else 0.0

    return out
