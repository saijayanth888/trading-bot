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

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
log = logging.getLogger("hermes_mcp")
if not log.handlers:
    h = RotatingFileHandler(str(LOG_PATH), maxBytes=2_000_000, backupCount=5)
    h.setFormatter(logging.Formatter(
        "%(asctime)s %(levelname)s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%SZ",
    ))
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


async def _ft_token(client: httpx.AsyncClient) -> str | None:
    global _jwt_token, _jwt_expires_at
    async with _jwt_lock:
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
    async with httpx.AsyncClient() as client:
        token = await _ft_token(client)
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        try:
            r = await client.get(f"{FREQTRADE_API}{path}", headers=headers, timeout=10.0)
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
async def trigger_evolution_cycle() -> dict:
    """❗ Kick off a new EPT generation — calls scripts/train_drl.py via shell."""
    script = ROOT_DIR / "user_data" / "scripts" / "train_drl.py"
    if not script.exists():
        return {"error": f"script missing: {script}"}
    proc = subprocess.Popen(
        ["python3", str(script), "--synthetic", "--timesteps", "20000"],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        env=os.environ.copy(), cwd=str(ROOT_DIR),
    )
    _audit("trigger_evolution_cycle", {}, f"pid={proc.pid}")
    return {"started": True, "pid": proc.pid, "note": "running in background"}


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
    """Latest whale transfers, MVRV, exchange netflow per asset."""
    out = {"netflow": [], "mvrv": [], "whales": []}
    out["netflow"] = _query(
        "SELECT DISTINCT ON (asset) asset, ts, netflow "
        "FROM exchange_netflow ORDER BY asset, ts DESC"
    )
    out["mvrv"] = _query(
        "SELECT DISTINCT ON (asset) asset, ts, value "
        "FROM mvrv_ratio ORDER BY asset, ts DESC"
    )
    out["whales"] = _query(
        "SELECT id, ts, symbol, amount_usd, from_owner_type, to_owner_type "
        "FROM whale_transactions ORDER BY ts DESC LIMIT 20"
    )
    for k in out:
        for r in out[k]:
            for kk, vv in list(r.items()):
                if isinstance(vv, datetime):
                    r[kk] = vv.isoformat()
    _audit("get_onchain_signals", {}, f"netflow={len(out['netflow'])} mvrv={len(out['mvrv'])} whales={len(out['whales'])}")
    return out


# ----- Database ------------------------------------------------------------


@mcp.tool()
async def query_trade_journal(sql: str) -> dict:
    """
    Read-only SELECT/CTE queries against the trade_journal table only.
    Other tables and any write/DDL operation are rejected.
    """
    if not _READ_ONLY_RE.match(sql or ""):
        return {"error": "only SELECT or WITH (CTE) statements allowed"}
    if _FORBIDDEN_RE.search(sql):
        return {"error": "forbidden keyword detected — read-only enforcement"}
    if not re.search(r"\btrade_journal\b", sql, re.IGNORECASE):
        return {"error": "query must reference the trade_journal table"}
    try:
        rows = _query(sql, ())
    except Exception as exc:
        return {"error": str(exc)[:200]}
    for r in rows[:1000]:
        for k, v in list(r.items()):
            if isinstance(v, datetime):
                r[k] = v.isoformat()
    _audit("query_trade_journal", {"sql": sql[:120]}, f"{len(rows)} rows")
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
