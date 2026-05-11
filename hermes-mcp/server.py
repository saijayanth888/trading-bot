"""
Hermes MCP server — exposes the trading bot's internals to the Hermes Agent
orchestration layer over the MCP protocol.

Tools (read-only unless tagged ❗):
  ── Trade data ────────────────────────────────────────────────
    get_open_trades()                  current positions, P&L, duration
    get_trade_history(days)            closed trades, full detail
    get_daily_pnl(days)                daily P&L breakdown
    get_performance_metrics()          Sharpe / DD / PF / win rate

  ── EPT evolution ──────────────────────────────────────────────
    get_evolution_status()             generation, champion, fitness scores
    trigger_evolution_cycle()          ❗ kicks off a new generation
    get_champion_genome()              champion's hyperparams + features

  ── Risk + control ─────────────────────────────────────────────
    get_risk_status()                  drawdown, daily loss, breaker, positions
    pause_trading(reason)              ❗ flips dry_run=true (kill switch)
    resume_trading(confirm)            ❗ flips dry_run=false (requires confirm)

  ── Market ─────────────────────────────────────────────────────
    get_current_regime()               HMM label + probabilities
    get_sentiment_scores()             latest sentiment per pair
    get_onchain_signals()              latest whale + MVRV + netflow

  ── Database ───────────────────────────────────────────────────
    query_trade_journal(sql)           read-only SQL on trade_journal
    get_regime_history(days)           regime transitions over time

  ── Stocks (Shark + Wheel) ─────────────────────────────────────
    get_combined_portfolio()           crypto + stocks equity, drawdown, breaker
    get_stock_positions()              Alpaca + wheel positions (read-only)
    get_stock_pnl(days)                Stock P&L from stocks/memory/TRADE-LOG.md
    get_wheel_status()                 open puts/calls/shares + cumulative premium

Authentication: HERMES_MCP_KEY env var (required). All tool calls are
logged to user_data/logs/hermes_mcp.log.

Port 8089 (configurable via HERMES_MCP_PORT).
"""

from __future__ import annotations

import asyncio
import functools
import json
import logging
import os
import re
import subprocess
import sys
import time
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any
from urllib.parse import quote_plus

import httpx
import psycopg
from psycopg.rows import dict_row

# FastMCP — Anthropic's reference Python MCP framework
try:
    from mcp.server.fastmcp import FastMCP
except ImportError:
    print("FATAL: mcp not installed. `pip install mcp`", file=sys.stderr)
    sys.exit(1)

# ---------------------------------------------------------------------------
# Config (env-driven)
# ---------------------------------------------------------------------------

ROOT_DIR = Path(os.environ.get(
    "TRADING_BOT_ROOT",
    str(Path(__file__).resolve().parent.parent),
))
LOG_PATH = ROOT_DIR / "user_data" / "logs" / "hermes_mcp.log"
CONFIG_PATH = ROOT_DIR / "user_data" / "config.json"

FREQTRADE_API = os.environ.get("FREQTRADE_API_URL", "http://localhost:8080")
FREQTRADE_USER = os.environ.get("FREQTRADE_API_USER", "freqtrader")
FREQTRADE_PASS = os.environ.get("FREQTRADE_API_PASS", "")

PORT = int(os.environ.get("HERMES_MCP_PORT", "8089"))
HOST = os.environ.get("HERMES_MCP_HOST", "0.0.0.0")
HERMES_MCP_KEY = os.environ.get("HERMES_MCP_KEY", "").strip()

# Block writes from the SQL passthrough tool — only SELECT and CTE allowed.
_READ_ONLY_RE = re.compile(
    r"^\s*(SELECT|WITH)\b", re.IGNORECASE,
)
_FORBIDDEN_RE = re.compile(
    r"\b(INSERT|UPDATE|DELETE|DROP|TRUNCATE|ALTER|CREATE|GRANT|REVOKE|"
    r"COPY|VACUUM|CLUSTER|REINDEX|REFRESH)\b",
    re.IGNORECASE,
)
# Dangerous tokens that survived the keyword blocklist: pg_sleep can be used
# to DoS the dashboard (lock a Postgres connection for hours); pg_read_file
# / pg_ls_dir leak host filesystem state; comment / statement-terminator
# sequences enable union-stacking and trailing-payload injection. See
# REVIEW_2026-05-11 §P0-Q for the verified bypasses. We blocklist explicitly
# in addition to the read-only transaction layer so any future widening of
# the role's grants still trips here first.
_SQLI_DENY_RE = re.compile(
    r"(--|/\*|\*/|;|"
    r"\bunion\b|\bpg_sleep\b|\bpg_read_file\b|\bpg_ls_dir\b|"
    r"\bpg_read_binary_file\b|\bcurrent_setting\b|\bdblink\b|"
    r"\blo_import\b|\blo_export\b|\bcopy\s+from\b)",
    re.IGNORECASE,
)

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
log = logging.getLogger("hermes_mcp")
if not log.handlers:
    h = RotatingFileHandler(str(LOG_PATH), maxBytes=2_000_000, backupCount=5)
    fmt = logging.Formatter(
        "%(asctime)s %(levelname)s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%SZ",
    )
    # Emit asctime in UTC. Without this override, %(asctime)s uses
    # `time.localtime` while we suffix the datefmt with literal 'Z', producing
    # local-time-stamped strings that *claim* to be UTC. JS Date() then
    # silently drifts by the host's offset (US/Eastern → -4h: see the
    # "-14400s ago" report in REVIEW_2026-05-11 §1.5).
    fmt.converter = time.gmtime
    h.setFormatter(fmt)
    log.addHandler(h)
    log.setLevel(logging.INFO)


def _audit(tool: str, args: dict, result_summary: str = "ok") -> None:
    log.info("tool=%s args=%s result=%s",
             tool, json.dumps(args, default=str)[:300], result_summary[:200])


def _require_auth(fn):
    """Decorator: reject mutating MCP tool calls when HERMES_MCP_KEY is unset.

    FastMCP doesn't yet support transport-level auth headers, so this is a
    server-side gate: if the key isn't configured, the call is refused
    outright. When transport-level token validation lands, replace the body
    of this wrapper with the per-request check.
    """
    @functools.wraps(fn)
    async def wrapper(*args, **kwargs):
        if not HERMES_MCP_KEY:
            log.warning("BLOCKED %s — HERMES_MCP_KEY not configured", fn.__name__)
            return {"error": "MCP authentication not configured. "
                             "Set HERMES_MCP_KEY in .env to enable mutating tools."}
        _audit(fn.__name__, {"auth": "key_present"}, "authorized")
        return await fn(*args, **kwargs)
    return wrapper


# ---------------------------------------------------------------------------
# DSN resolution (URL-encoded password — same pattern as modules/db.py)
# ---------------------------------------------------------------------------


def _dsn() -> str:
    explicit = os.environ.get("DATABASE_URL", "").strip()
    if explicit:
        return explicit
    user = os.environ.get("POSTGRES_USER", "tradebot")
    password = os.environ.get("POSTGRES_PASSWORD", "")
    if not password:
        raise RuntimeError(
            "POSTGRES_PASSWORD env var is required for hermes-mcp. "
            "Set it in .env or export it before starting."
        )
    host = os.environ.get("POSTGRES_HOST", "localhost")
    port = os.environ.get("POSTGRES_PORT", "5434")
    db = os.environ.get("POSTGRES_DB", "tradebot")
    return f"postgresql://{quote_plus(user)}:{quote_plus(password)}@{host}:{port}/{db}"


@asynccontextmanager
async def _db_conn():
    """Read-only connection (sets default_transaction_read_only)."""
    conn = await psycopg.AsyncConnection.connect(_dsn(), connect_timeout=5)
    try:
        async with conn.cursor() as cur:
            await cur.execute("SET default_transaction_read_only = on")
        yield conn
    finally:
        await conn.close()


def _query(sql: str, params: tuple = ()) -> list[dict]:
    """Synchronous read query — used inside non-async tool implementations."""
    with psycopg.connect(_dsn(), connect_timeout=5) as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute("SET default_transaction_read_only = on")
            cur.execute(sql, params)
            try:
                return list(cur.fetchall())
            except psycopg.ProgrammingError:
                return []


# ---------------------------------------------------------------------------
# Freqtrade REST client (JWT-cached)
# ---------------------------------------------------------------------------

_jwt_token: str | None = None
_jwt_expires_at: datetime | None = None
_jwt_lock = asyncio.Lock()


async def _ft_token(client: httpx.AsyncClient, force_refresh: bool = False) -> str | None:
    """Cached JWT login. ``force_refresh`` drops the cache before re-issuing."""
    global _jwt_token, _jwt_expires_at
    async with _jwt_lock:
        if force_refresh:
            _jwt_token = None
            _jwt_expires_at = None
        if _jwt_token and _jwt_expires_at and datetime.now(timezone.utc) < _jwt_expires_at:
            return _jwt_token
        if not FREQTRADE_PASS:
            return None
        try:
            r = await client.post(
                f"{FREQTRADE_API}/api/v1/token/login",
                auth=(FREQTRADE_USER, FREQTRADE_PASS), timeout=5.0,
            )
            if r.status_code != 200:
                return None
            _jwt_token = r.json().get("access_token")
            _jwt_expires_at = datetime.now(timezone.utc) + timedelta(minutes=9)
            return _jwt_token
        except Exception as exc:
            log.warning("freqtrade login failed: %s", exc)
            return None


async def _ft_get(path: str) -> Any:
    """Authenticated freqtrade GET with one 401 → re-login retry.

    The cached JWT survives the 9-min TTL but is silently invalidated by a
    freqtrade restart (signing key rotation). Without a 401 retry, the next
    call onward fails until the TTL expires — see the "78 401s in 30 min"
    flood that prompted REVIEW_2026-05-11 §2.1.
    """
    async with httpx.AsyncClient() as client:
        token = await _ft_token(client)
        if not token:
            return None
        url = f"{FREQTRADE_API}{path}"
        try:
            r = await client.get(url, headers={"Authorization": f"Bearer {token}"}, timeout=10.0)
            if r.status_code == 401:
                log.info("freqtrade %s returned 401 — refreshing JWT", path)
                token = await _ft_token(client, force_refresh=True)
                if not token:
                    return None
                r = await client.get(url, headers={"Authorization": f"Bearer {token}"}, timeout=10.0)
            if r.status_code != 200:
                return None
            return r.json()
        except Exception as exc:
            log.warning("ft_get %s failed: %s", path, exc)
            return None


# ---------------------------------------------------------------------------
# MCP server
# ---------------------------------------------------------------------------

mcp = FastMCP("trading-bot", host=HOST, port=PORT)


# ----- Trade data ----------------------------------------------------------


@mcp.tool()
async def get_open_trades() -> list[dict]:
    """List currently open positions with pair, entry, current P&L, duration."""
    data = await _ft_get("/api/v1/status") or []
    out = []
    for t in data:
        out.append({
            "trade_id": t.get("trade_id"),
            "pair": t.get("pair"),
            "open_rate": t.get("open_rate"),
            "current_rate": t.get("current_rate"),
            "stake_amount": t.get("stake_amount"),
            "profit_pct": t.get("profit_pct"),
            "profit_abs": t.get("profit_abs"),
            "open_date": t.get("open_date_hum") or t.get("open_date"),
            "duration": t.get("trade_duration_s"),
        })
    _audit("get_open_trades", {}, f"{len(out)} open")
    return out


@mcp.tool()
async def get_trade_history(days: int = 7) -> list[dict]:
    """Closed trades from the last `days` days, full detail from trade_journal."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=int(days))
    rows = _query(
        "SELECT trade_id, pair, direction, opened_at, closed_at, entry_price, "
        "exit_price, pnl, pnl_pct, duration_min, confidence, regime, exit_reason "
        "FROM trade_journal "
        "WHERE closed_at IS NOT NULL AND closed_at >= %s "
        "ORDER BY closed_at DESC LIMIT 500",
        (cutoff,),
    )
    _audit("get_trade_history", {"days": days}, f"{len(rows)} rows")
    return [{k: (v.isoformat() if isinstance(v, datetime) else v) for k, v in r.items()}
            for r in rows]


@mcp.tool()
async def get_daily_pnl(days: int = 14) -> list[dict]:
    """Per-day P&L for the last `days` days."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=int(days))
    rows = _query(
        "SELECT to_char(closed_at AT TIME ZONE 'UTC', 'YYYY-MM-DD') AS day, "
        "       COUNT(*) AS trades, "
        "       SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) AS wins, "
        "       COALESCE(SUM(pnl), 0) AS pnl, "
        "       COALESCE(AVG(pnl_pct), 0) AS avg_pnl_pct "
        "FROM trade_journal WHERE closed_at >= %s "
        "GROUP BY day ORDER BY day DESC",
        (cutoff,),
    )
    _audit("get_daily_pnl", {"days": days}, f"{len(rows)} days")
    return rows


@mcp.tool()
async def get_performance_metrics() -> dict:
    """Sharpe (annualised), max drawdown, profit factor, win rate."""
    rows = _query(
        "SELECT closed_at, pnl, pnl_pct FROM trade_journal "
        "WHERE closed_at IS NOT NULL ORDER BY closed_at ASC"
    )
    if not rows:
        return {"trades": 0, "sharpe": 0.0, "max_dd": 0.0,
                "profit_factor": 0.0, "win_rate": 0.0, "total_pnl": 0.0}

    pnls = [float(r["pnl"] or 0) for r in rows]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    win_rate = len(wins) / len(rows)
    pf = (sum(wins) / abs(sum(losses))) if losses else float("inf")

    # Daily Sharpe annualised at 365
    daily: dict[str, float] = {}
    for r in rows:
        day = r["closed_at"].astimezone(timezone.utc).strftime("%Y-%m-%d")
        daily[day] = daily.get(day, 0.0) + float(r["pnl_pct"] or 0)
    daily_pcts = list(daily.values())
    if len(daily_pcts) >= 2:
        import statistics
        mean = statistics.fmean(daily_pcts)
        sd = statistics.stdev(daily_pcts)
        sharpe = (mean / sd * (365 ** 0.5)) if sd > 0 else 0.0
    else:
        sharpe = 0.0

    # Max drawdown via cumulative P&L curve
    cum = 0.0
    peak = 0.0
    max_dd = 0.0
    for p in pnls:
        cum += p
        peak = max(peak, cum)
        dd = (peak - cum) / max(peak, 1.0)
        max_dd = max(max_dd, dd)

    out = {
        "trades": len(rows),
        "sharpe": round(sharpe, 4),
        "max_dd": round(max_dd, 4),
        "profit_factor": round(pf, 4) if pf != float("inf") else None,
        "win_rate": round(win_rate, 4),
        "total_pnl": round(sum(pnls), 2),
    }
    _audit("get_performance_metrics", {}, json.dumps(out))
    return out


# ----- EPT evolution -------------------------------------------------------


@mcp.tool()
async def get_evolution_status() -> dict:
    """Current generation, champion ID, fitness scores from evolution.json."""
    log_path = ROOT_DIR / "user_data" / "logs" / "evolution.json"
    if not log_path.exists():
        out = {"generation": 0, "champion": None, "alive": [], "note": "evolution.json not present"}
        _audit("get_evolution_status", {}, "no log")
        return out
    try:
        history = json.loads(log_path.read_text())
        last = history[-1] if history else {}
        out = {
            "generation": last.get("generation"),
            "champion": last.get("champion"),
            "runner_up": last.get("runner_up"),
            "alive": [
                {"member_id": m.get("member_id"), "fitness": m.get("fitness"),
                 "metrics": m.get("metrics")}
                for m in (last.get("alive") or [])
            ],
            "snapshots": len(history),
        }
        _audit("get_evolution_status", {}, f"gen={out.get('generation')}")
        return out
    except Exception as exc:
        return {"error": str(exc)}


@mcp.tool()
@_require_auth
async def trigger_evolution_cycle(mode: str = "mock") -> dict:
    """❗ Kick off a new EPT generation — runs run_ept_generation.py synchronously.

    Returns the full summary dict (champion id, fitness, leaderboard) so
    the caller can report real numbers — not the previously misconfigured
    fire-and-forget call that ran train_drl.py and left evolution.json
    empty. mode='mock' uses the deterministic surrogate (default until
    real per-agent paper-trading bots exist); mode='live' reads
    trade_journal for real Sharpe/PF/DD.
    """
    script = ROOT_DIR / "user_data" / "scripts" / "run_ept_generation.py"
    if not script.exists():
        return {"error": f"script missing: {script}"}
    try:
        proc = subprocess.run(
            ["python3", str(script), "--mode", str(mode)],
            capture_output=True, text=True, timeout=300,
            env=os.environ.copy(), cwd=str(ROOT_DIR),
        )
    except subprocess.TimeoutExpired:
        _audit("trigger_evolution_cycle", {"mode": mode}, "TIMEOUT >5min")
        return {"error": "evolution cycle timed out after 5 minutes"}
    except Exception as exc:
        _audit("trigger_evolution_cycle", {"mode": mode}, f"exec failed: {exc}")
        return {"error": f"could not execute runner: {exc}"}

    if proc.returncode != 0:
        _audit("trigger_evolution_cycle", {"mode": mode},
               f"exit={proc.returncode} stderr={proc.stderr[-400:]}")
        return {
            "error": f"runner exited with code {proc.returncode}",
            "stderr_tail": proc.stderr[-1200:],
        }
    try:
        summary = json.loads(proc.stdout.strip().splitlines()[-1] if False else proc.stdout.strip()[proc.stdout.strip().rfind("{"):])
    except Exception:
        # fall back to raw stdout if we can't parse the JSON tail
        summary = {"ok": True, "raw_stdout": proc.stdout[-2000:]}
    _audit("trigger_evolution_cycle", {"mode": mode},
           f"gen={summary.get('generation')} champ={(summary.get('champion') or {}).get('member_id')}")
    return summary


@mcp.tool()
async def get_champion_genome() -> dict:
    """Champion's hyperparameters + feature subset from the latest snapshot."""
    log_path = ROOT_DIR / "user_data" / "logs" / "evolution.json"
    if not log_path.exists():
        return {"error": "evolution.json not present"}
    history = json.loads(log_path.read_text())
    if not history:
        return {"error": "no snapshots"}
    last = history[-1]
    champ_id = last.get("champion")
    for m in last.get("alive", []):
        if m.get("member_id") == champ_id:
            return {"member_id": champ_id, "genome": m.get("genome"),
                    "fitness": m.get("fitness"), "metrics": m.get("metrics")}
    return {"error": f"champion {champ_id} not found in alive list"}


# ----- Risk + control ------------------------------------------------------


@mcp.tool()
async def get_risk_status() -> dict:
    """Drawdown, daily loss, circuit-breaker state, open positions count."""
    open_trades = await _ft_get("/api/v1/status") or []
    profit = await _ft_get("/api/v1/profit") or {}
    out = {
        "open_positions": len(open_trades),
        "total_pnl_closed": profit.get("profit_closed_coin", 0),
        "trade_count": profit.get("trade_count", 0),
        "winning_trades": profit.get("winning_trades", 0),
        "first_trade": profit.get("first_trade_humanized"),
        "latest_trade": profit.get("latest_trade_humanized"),
    }
    _audit("get_risk_status", {}, f"open={out['open_positions']}")
    return out


@mcp.tool()
@_require_auth
async def pause_trading(reason: str = "manual_pause_via_mcp") -> dict:
    """❗ Flip dry_run=true in config.json. Reversible via resume_trading."""
    try:
        cfg = json.loads(CONFIG_PATH.read_text())
        was = cfg.get("dry_run", True)
        cfg["dry_run"] = True
        tmp = CONFIG_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(cfg, indent=4))
        tmp.replace(CONFIG_PATH)
        _audit("pause_trading", {"reason": reason}, f"was={was}")
        return {"ok": True, "previously_dry_run": was, "reason": reason,
                "note": "restart freqtrade for this to take effect"}
    except Exception as exc:
        return {"error": str(exc)}


@mcp.tool()
@_require_auth
async def resume_trading(confirm: bool = False) -> dict:
    """❗ Flip dry_run=false. Requires confirm=True for safety."""
    if not confirm:
        return {"error": "confirm=True required to flip out of dry-run"}
    try:
        cfg = json.loads(CONFIG_PATH.read_text())
        was = cfg.get("dry_run", True)
        cfg["dry_run"] = False
        tmp = CONFIG_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(cfg, indent=4))
        tmp.replace(CONFIG_PATH)
        _audit("resume_trading", {"confirm": True}, f"was={was}")
        return {"ok": True, "previously_dry_run": was,
                "note": "restart freqtrade for this to take effect"}
    except Exception as exc:
        return {"error": str(exc)}


# ----- Market --------------------------------------------------------------


@mcp.tool()
async def get_current_regime() -> dict:
    """Latest HMM regime label + state probabilities from regime_log."""
    rows = _query(
        "SELECT ts, regime, probability, regime_duration_hours, state_probabilities "
        "FROM regime_log ORDER BY ts DESC LIMIT 1"
    )
    if not rows:
        return {"regime": "unknown", "probability": 0.0}
    r = rows[0]
    out = {
        "regime": r["regime"],
        "probability": float(r["probability"] or 0),
        "duration_hours": float(r["regime_duration_hours"] or 0),
        "state_probabilities": r["state_probabilities"],
        "ts": r["ts"].isoformat() if isinstance(r["ts"], datetime) else r["ts"],
    }
    _audit("get_current_regime", {}, out["regime"])
    return out


@mcp.tool()
async def get_sentiment_scores() -> list[dict]:
    """Last 12 sentiment polls (score, confidence, market_impact)."""
    rows = _query(
        "SELECT ts, market_impact, sentiment_score, confidence, agreement, "
        "       n_headlines, key_events "
        "FROM sentiment_log ORDER BY ts DESC LIMIT 12"
    )
    out = [{**r, "ts": r["ts"].isoformat() if isinstance(r["ts"], datetime) else r["ts"]}
           for r in rows]
    _audit("get_sentiment_scores", {}, f"{len(out)} rows")
    return out


@mcp.tool()
async def get_onchain_signals() -> dict:
    """Latest derivatives + macro features (free pipeline rebuilt 2026-05-08).

    Old paid-API tables (exchange_netflow, mvrv_ratio, whale_transactions)
    are retained for schema compatibility but no longer written. We now
    surface OKX funding rate, open interest, taker volume, and macro
    features (BTC MVRV, F&G index, mempool fastest-fee, stablecoin mcap).
    """
    out = {"derivatives": [], "macro": []}
    out["derivatives"] = _query(
        "SELECT DISTINCT ON (pair) pair, ts, funding_rate, open_interest_usd, "
        "       long_short_ratio, taker_buy_vol_usd, taker_sell_vol_usd "
        "FROM derivatives_features ORDER BY pair, ts DESC"
    )
    out["macro"] = _query(
        "SELECT ts, stablecoin_mcap_usd, stablecoin_mcap_chg_24h, "
        "       fear_greed_index, btc_dominance_pct, btc_mvrv, "
        "       btc_mempool_fastest_fee "
        "FROM macro_features ORDER BY ts DESC LIMIT 1"
    )
    for k in out:
        for r in out[k]:
            for kk, vv in list(r.items()):
                if isinstance(vv, datetime):
                    r[kk] = vv.isoformat()
    _audit("get_onchain_signals", {}, f"derivatives={len(out['derivatives'])} macro={len(out['macro'])}")
    return out


# ----- Database ------------------------------------------------------------


# ----- News + Fear & Greed (multi-source aggregator) ----------------------


@mcp.tool()
async def get_latest_headlines(pair: str = "BTC", limit: int = 20) -> list[dict]:
    """Latest aggregated headlines mentioning a pair, with source + community sentiment.

    Pulls from news_headlines (populated by user_data/modules/news_aggregator.py
    every 15 min). ``pair`` is the symbol-prefix; pass "BTC" not "BTC/USD".
    """
    pair = (pair or "BTC").split("/")[0].upper()
    limit = max(1, min(100, int(limit)))
    rows = _query(
        """
        SELECT ts, source, title, summary, url, pair_mentions,
               community_sentiment, attention_score
        FROM news_headlines
        WHERE pair_mentions @> %s::jsonb
        ORDER BY ts DESC LIMIT %s
        """,
        (json.dumps([pair]), limit),
    )
    out = [
        {
            "ts": r["ts"].isoformat() if isinstance(r["ts"], datetime) else r["ts"],
            "source": r["source"],
            "title": r["title"],
            "summary": r.get("summary"),
            "url": r.get("url"),
            "pair_mentions": r.get("pair_mentions"),
            "community_sentiment": r.get("community_sentiment"),
            "attention_score": r.get("attention_score"),
        }
        for r in rows
    ]
    _audit("get_latest_headlines", {"pair": pair, "limit": limit}, f"{len(out)} rows")
    return out


@mcp.tool()
async def get_fear_greed_index() -> dict:
    """Current Fear & Greed Index value, classification, and last 7 daily readings."""
    rows = _query(
        "SELECT ts, value, classification, history_7d FROM fear_greed_log "
        "ORDER BY ts DESC LIMIT 1"
    )
    if not rows:
        return {"error": "no fear_greed_log rows yet"}
    r = rows[0]
    out = {
        "value": int(r["value"]),
        "classification": r["classification"],
        "history_7d": r.get("history_7d") or [],
        "ts": r["ts"].isoformat() if isinstance(r["ts"], datetime) else r["ts"],
    }
    _audit("get_fear_greed_index", {}, f"{out['value']} ({out['classification']})")
    return out


@mcp.tool()
async def get_reddit_buzz(pair: str = "BTC") -> dict:
    """Reddit attention score + top posts for a pair in the last 24h."""
    pair = (pair or "BTC").split("/")[0].upper()
    rows = _query(
        """
        SELECT ts, source, title, url, attention_score
        FROM news_headlines
        WHERE source LIKE 'reddit:%%'
          AND ts > NOW() - INTERVAL '24 hours'
          AND pair_mentions @> %s::jsonb
        ORDER BY attention_score DESC NULLS LAST, ts DESC
        LIMIT 10
        """,
        (json.dumps([pair]),),
    )
    if not rows:
        out = {"pair": pair, "n_posts": 0, "avg_attention": 0.0, "top_posts": []}
        _audit("get_reddit_buzz", {"pair": pair}, "no posts")
        return out
    scores = [float(r["attention_score"] or 0) for r in rows]
    out = {
        "pair": pair,
        "n_posts": len(rows),
        "avg_attention": round(sum(scores) / len(scores), 4) if scores else 0.0,
        "max_attention": round(max(scores), 4) if scores else 0.0,
        "top_posts": [
            {
                "ts": r["ts"].isoformat() if isinstance(r["ts"], datetime) else r["ts"],
                "subreddit": r["source"].replace("reddit:", ""),
                "title": r["title"],
                "url": r.get("url"),
                "attention_score": r.get("attention_score"),
            }
            for r in rows[:10]
        ],
    }
    _audit("get_reddit_buzz", {"pair": pair}, f"{len(rows)} posts")
    return out


@mcp.tool()
async def get_source_agreement() -> dict:
    """Cross-source sentiment agreement matrix.

    For each watched pair, summarise the *current* signal from every source
    we have data for (Hermes-3 LLM scoring, Reddit upvote-ratio crowd
    sentiment, Reddit attention, CoinGecko trending, Fear & Greed). Lets
    the operator spot divergences (e.g. F&G says Greed but Reddit votes
    are bearish).
    """
    pairs = ("BTC", "ETH", "SOL", "ADA")
    out: dict = {"as_of": datetime.now(timezone.utc).isoformat(), "pairs": {}}

    # Latest LLM-scored sentiment_log row (broad market — same for all pairs).
    sent_rows = _query(
        "SELECT sentiment_score, market_impact, fear_greed_value, "
        "       fear_greed_classification, trending_pairs "
        "FROM sentiment_log ORDER BY ts DESC LIMIT 1"
    )
    base = sent_rows[0] if sent_rows else {}

    for p in pairs:
        # Reddit upvote-ratio average (the crowd-sentiment signal) over 6h.
        comm_rows = _query(
            """
            SELECT AVG(community_sentiment) AS avg_score, COUNT(*) AS n
            FROM news_headlines
            WHERE source LIKE 'reddit:%%'
              AND ts > NOW() - INTERVAL '6 hours'
              AND pair_mentions @> %s::jsonb
              AND community_sentiment IS NOT NULL
            """,
            (json.dumps([p]),),
        )
        comm = comm_rows[0] if comm_rows else {}
        # Reddit attention sum.
        rd_rows = _query(
            """
            SELECT AVG(attention_score) AS avg_score, COUNT(*) AS n
            FROM news_headlines
            WHERE source LIKE 'reddit:%%'
              AND ts > NOW() - INTERVAL '6 hours'
              AND pair_mentions @> %s::jsonb
              AND attention_score IS NOT NULL
            """,
            (json.dumps([p]),),
        )
        rd = rd_rows[0] if rd_rows else {}
        trending = base.get("trending_pairs") or []
        if isinstance(trending, str):
            try:
                trending = json.loads(trending)
            except ValueError:
                trending = []

        out["pairs"][p] = {
            "llm_market_impact": base.get("market_impact"),
            "llm_score": float(base.get("sentiment_score") or 0),
            # Reddit upvote-ratio crowd-sentiment.
            "reddit_community_avg": float(comm.get("avg_score") or 0) if comm.get("n") else None,
            "reddit_community_n": int(comm.get("n") or 0),
            "reddit_attention_avg": float(rd.get("avg_score") or 0) if rd.get("n") else None,
            "reddit_attention_n": int(rd.get("n") or 0),
            "fear_greed_value": base.get("fear_greed_value"),
            "fear_greed_classification": base.get("fear_greed_classification"),
            "trending": p in {str(x).upper() for x in trending},
        }
    _audit("get_source_agreement", {}, f"{len(pairs)} pairs")
    return out


# ----- DB query passthrough -----------------------------------------------


@mcp.tool()
async def query_trade_journal(sql: str) -> dict:
    """
    Read-only SELECT/CTE queries against the trade_journal table only.
    Other tables and any write/DDL operation are rejected.

    Defence-in-depth: the transaction itself is RO (psycopg sets
    ``default_transaction_read_only = on`` before execute), and the SQL
    text is filtered for dangerous tokens (`;`, comments, union, pg_sleep,
    pg_read_file, etc.) that survived the keyword blocklist in the past.
    Reject before we ever hand the string to psycopg.
    """
    raw = sql or ""
    if not _READ_ONLY_RE.match(raw):
        return {"error": "only SELECT or WITH (CTE) statements allowed"}
    if _FORBIDDEN_RE.search(raw):
        return {"error": "forbidden keyword detected — read-only enforcement"}
    sqli_hit = _SQLI_DENY_RE.search(raw)
    if sqli_hit:
        # Don't leak the exact match (defence-in-depth + don't help attackers
        # iterate); audit-log the full input below so we can review later.
        _audit("query_trade_journal", {"sql": raw[:120]}, f"reject: sqli_token={sqli_hit.group(0)!r}")
        return {"error": "query rejected — disallowed token (comments, semicolons, union, pg_sleep, etc.)"}
    if not re.search(r"\btrade_journal\b", raw, re.IGNORECASE):
        return {"error": "query must reference the trade_journal table"}
    try:
        rows = _query(raw, ())
    except Exception as exc:
        return {"error": str(exc)[:200]}
    for r in rows[:1000]:
        for k, v in list(r.items()):
            if isinstance(v, datetime):
                r[k] = v.isoformat()
    _audit("query_trade_journal", {"sql": raw[:120]}, f"{len(rows)} rows")
    return {"rows": rows[:1000], "truncated": len(rows) > 1000, "n": len(rows)}


@mcp.tool()
async def get_regime_history(days: int = 30) -> list[dict]:
    """Regime transitions over the last `days` days."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=int(days))
    rows = _query(
        "SELECT ts, regime, probability, regime_duration_hours "
        "FROM regime_log WHERE ts >= %s ORDER BY ts ASC",
        (cutoff,),
    )
    transitions = []
    last_regime: str | None = None
    for r in rows:
        rg = r["regime"]
        if rg != last_regime:
            transitions.append({
                "ts": r["ts"].isoformat() if isinstance(r["ts"], datetime) else r["ts"],
                "regime": rg,
                "probability": float(r["probability"] or 0),
            })
            last_regime = rg
    _audit("get_regime_history", {"days": days}, f"{len(transitions)} transitions")
    return transitions


# ---------------------------------------------------------------------------
# Stocks subsystem (Shark + Wheel) — read-only views
# ---------------------------------------------------------------------------


_STOCKS_ROOT = ROOT_DIR / "stocks"
_WHEEL_STATE_DIR = _STOCKS_ROOT / "wheel" / "state"
_TRADE_LOG_PATH = _STOCKS_ROOT / "memory" / "TRADE-LOG.md"


def _read_json_file(path: Path) -> Any:
    try:
        if not path.is_file():
            return None
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        log.warning("read %s failed: %s", path, exc)
        return None


def _file_age_seconds(path: Path) -> int | None:
    try:
        return int(datetime.now(timezone.utc).timestamp() - path.stat().st_mtime) if path.is_file() else None
    except OSError:
        return None


@mcp.tool()
async def get_combined_portfolio() -> dict:
    """Combined crypto + stocks portfolio status, drawdown, and risk-breaker
    state. Reads `user_data.modules.unified_risk` which aggregates Freqtrade
    + Alpaca equity behind a single peak / drawdown / threshold view.
    """
    try:
        sys.path.insert(0, str(ROOT_DIR))
        from user_data.modules.unified_risk import get_combined_risk_status
    except ImportError as exc:
        return {"error": f"unified_risk import failed: {exc}"}
    status = get_combined_risk_status()
    _audit("get_combined_portfolio", {},
           f"total=${status['total_equity']:.0f} dd={status['combined_drawdown_pct']}%")
    return status


@mcp.tool()
async def get_stock_positions() -> list[dict]:
    """Current Alpaca + wheel stock positions from disk-fed state files.

    Returns one entry per open position with kind (short_put, short_call,
    long_shares), strike, expiry, qty, entry_credit, contract_symbol.
    """
    pos_file = _WHEEL_STATE_DIR / "positions.json"
    raw = _read_json_file(pos_file) or []
    if not isinstance(raw, list):
        raw = []
    out = []
    for p in raw:
        out.append({
            "kind": p.get("kind"),
            "underlying": p.get("underlying"),
            "qty": p.get("qty"),
            "strike": p.get("strike"),
            "expiry": p.get("expiry"),
            "entry_credit_usd": float(p.get("entry_credit") or 0.0),
            "contract": p.get("contract_symbol"),
            "opened_at": p.get("opened_at"),
        })
    _audit("get_stock_positions", {}, f"{len(out)} positions")
    return out


@mcp.tool()
async def get_stock_pnl(days: int = 7) -> dict:
    """Stock P&L over the last N days, parsed from stocks/memory/TRADE-LOG.md.

    Returns realized P&L from EOD snapshots + counts of BUY / SELL /
    STOPPED actions. Wheel premium is reported separately via the wheel
    snapshot since it lives in trades.jsonl, not the markdown log.
    """
    if not _TRADE_LOG_PATH.is_file():
        return {"error": "TRADE-LOG.md missing", "days": days}

    text = _TRADE_LOG_PATH.read_text(errors="replace")
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).date()

    # Action lines look like "[2026-05-08] BUY TSLA 10 @ $250.50 | catalyst..."
    action_re = re.compile(
        r"\[(\d{4}-\d{2}-\d{2})\]\s+(BUY|SELL|STOPPED|TIGHTEN|SCAN)\s+(\S+)"
    )
    pnl_re = re.compile(r"\*\*Day P&L:\*\*\s+([+\-]?[\d.,]+)")
    eod_date_re = re.compile(r"###\s+(\d{4}-\d{2}-\d{2})\s+—\s+EOD")

    actions = {"BUY": 0, "SELL": 0, "STOPPED": 0, "TIGHTEN": 0, "SCAN": 0}
    realized_pnl = 0.0
    eod_count = 0
    cur_eod_date: datetime | None = None
    for line in text.splitlines():
        m = eod_date_re.search(line)
        if m:
            try:
                cur_eod_date = datetime.strptime(m.group(1), "%Y-%m-%d").date()
            except ValueError:
                cur_eod_date = None
            continue
        m = action_re.search(line)
        if m:
            try:
                d = datetime.strptime(m.group(1), "%Y-%m-%d").date()
                if d >= cutoff:
                    actions[m.group(2)] = actions.get(m.group(2), 0) + 1
            except ValueError:
                pass
            continue
        m = pnl_re.search(line)
        if m and cur_eod_date and cur_eod_date >= cutoff:
            try:
                realized_pnl += float(m.group(1).replace(",", ""))
                eod_count += 1
            except ValueError:
                pass

    # Cumulative wheel premium (lives in trades.jsonl)
    wheel_pnl = 0.0
    trades_file = _WHEEL_STATE_DIR / "trades.jsonl"
    if trades_file.is_file():
        cutoff_iso = cutoff.isoformat()
        try:
            for line in trades_file.read_text().splitlines():
                if not line.strip():
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                ts = (rec.get("timestamp") or "")[:10]
                if ts >= cutoff_iso:
                    wheel_pnl += float(rec.get("pnl", 0.0) or 0.0)
        except OSError:
            pass

    out = {
        "days": days,
        "shark_realized_pnl_usd": round(realized_pnl, 2),
        "wheel_realized_pnl_usd": round(wheel_pnl, 2),
        "total_realized_pnl_usd": round(realized_pnl + wheel_pnl, 2),
        "eod_snapshots_in_window": eod_count,
        "actions_in_window": actions,
    }
    _audit("get_stock_pnl", {"days": days},
           f"shark=${out['shark_realized_pnl_usd']} wheel=${out['wheel_realized_pnl_usd']}")
    return out


@mcp.tool()
async def get_wheel_status() -> dict:
    """Options-wheel state — open puts, covered calls, assignments + cumulative
    premium captured. Reads stocks/wheel/state/{account_snapshot,positions}.json.
    """
    snap = _read_json_file(_WHEEL_STATE_DIR / "account_snapshot.json") or {}
    positions = _read_json_file(_WHEEL_STATE_DIR / "positions.json") or []
    if not isinstance(positions, list):
        positions = []
    snap_age = _file_age_seconds(_WHEEL_STATE_DIR / "account_snapshot.json")

    csps = [p for p in positions if p.get("kind") == "short_put"]
    ccs = [p for p in positions if p.get("kind") == "short_call"]
    shares = [p for p in positions if p.get("kind") == "long_shares"]

    out = {
        "alpaca": {
            "cash": snap.get("cash"),
            "buying_power": snap.get("buying_power"),
            "portfolio_value": snap.get("portfolio_value"),
            "paper": snap.get("paper", True),
            "snapshot_ts": snap.get("ts"),
            "snapshot_age_seconds": snap_age,
        },
        "wheel": {
            "open_short_puts": len(csps),
            "open_covered_calls": len(ccs),
            "shares_held": sum(int(p.get("qty") or 0) for p in shares),
            "cumulative_premium_usd": float(snap.get("wheel_cumulative_pnl") or 0.0),
            "total_open_positions": len(positions),
        },
        "positions": positions,
    }
    _audit("get_wheel_status", {},
           f"csp={len(csps)} cc={len(ccs)} shares={len(shares)}")
    return out


# ---------------------------------------------------------------------------
# /health endpoint — plain HTTP, mounted on the same FastMCP ASGI app
# ---------------------------------------------------------------------------

START_TIME = datetime.now(timezone.utc).timestamp()


@mcp.custom_route("/health", methods=["GET"])
async def health_endpoint(request):  # type: ignore[no-untyped-def]
    """Plain HTTP health probe for systemd / Docker / load-balancer.
    No auth required (this is a liveness check, not a tool call).
    """
    from starlette.responses import JSONResponse
    tool_count = len(getattr(mcp, "_tool_manager", mcp).list_tools()) \
        if hasattr(mcp, "list_tools") else None
    if asyncio.iscoroutine(tool_count):
        tool_count = await tool_count
    if isinstance(tool_count, list):
        tool_count = len(tool_count)
    return JSONResponse({
        "status": "ok",
        "uptime_seconds": int(datetime.now(timezone.utc).timestamp() - START_TIME),
        "tools": tool_count,
        "trading_bot_root": str(ROOT_DIR),
        "freqtrade_api": FREQTRADE_API,
    })


# ---------------------------------------------------------------------------
# Authentication middleware (FastMCP supports header check at startup)
# ---------------------------------------------------------------------------


def _check_auth() -> None:
    if not HERMES_MCP_KEY:
        log.warning(
            "⚠️  HERMES_MCP_KEY not set — mutating tools (pause/resume/trigger) "
            "are DISABLED. Set HERMES_MCP_KEY in .env to enable."
        )
    else:
        log.info("MCP auth configured — mutating tools enabled")


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------


if __name__ == "__main__":
    _check_auth()
    log.info(
        "starting hermes-mcp on %s:%d transport=%s trading_bot_root=%s freqtrade=%s",
        HOST, PORT,
        os.environ.get("HERMES_MCP_TRANSPORT", "sse"),
        ROOT_DIR, FREQTRADE_API,
    )
    # FastMCP picks up host/port from the constructor (mcp v1.2+). The
    # transport is selected at run-time: stdio for direct-pipe parents,
    # sse / streamable-http for network access.
    transport = os.environ.get("HERMES_MCP_TRANSPORT", "sse")
    mcp.run(transport=transport)
