"""
Local implementations of the Hermes MCP tools, used by the dashboard's
``/api/mcp/{tool_name}`` proxy.

Why a local shim and not a true HTTP proxy:
The dashboard runs in docker compose (172.19.x.x bridge). The hermes-mcp
server runs on the host (systemd, bound to 0.0.0.0:8089). The host's
firewall blocks docker-bridge → host:8089 traffic, so the dashboard can't
HTTP-proxy to the MCP server. Instead, we re-implement each tool's logic
locally — same data sources (freqtrade REST API + Postgres + log files +
config.json) — and write to the same audit log so behaviour matches MCP.

If/when the firewall is opened, replace each function below with an HTTPX
call to ``f"{HERMES_MCP_URL}/tool/{name}"`` and pass through the body.

Every tool returns plain JSON-serialisable types. Errors are returned as
``{"error": "..."}`` rather than raised, mirroring the MCP server's pattern.

Tool registry (TOOLS dict) carries enough metadata for the Console UI's
typeahead + parameter form: { name → {func, params, mutating, doc} }.
"""

from __future__ import annotations

import json
import logging
import os
import re
import statistics
import subprocess
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

import httpx

from .data_sources import _ensure_jwt
from . import ops_db

logger = logging.getLogger(__name__)

# Audit log — written in the same place as the real MCP server's log so the
# Ops "MCP wire" panel surfaces dashboard-driven calls too.
USER_DATA_ROOT = Path(os.environ.get(
    "USER_DATA_ROOT",
    str(Path(__file__).resolve().parent.parent),
))
AUDIT_LOG = USER_DATA_ROOT / "logs" / "hermes_mcp.log"
CONFIG_PATH = Path(os.environ.get(
    "FREQTRADE_CONFIG_PATH",
    "/freqtrade/user_data/config.json",
))
FREQTRADE_API = os.environ.get("FREQTRADE_API_URL", "http://freqtrade:8080")
HERMES_MCP_KEY = os.environ.get("HERMES_MCP_KEY", "").strip()


def _audit(tool: str, args: dict, result_summary: str = "ok") -> None:
    """Append an audit line in the same format as hermes-mcp/server.py."""
    try:
        AUDIT_LOG.parent.mkdir(parents=True, exist_ok=True)
        with AUDIT_LOG.open("a") as f:
            ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            args_str = json.dumps(args, default=str)[:300]
            f.write(f"{ts} INFO via=dashboard tool={tool} args={args_str} result={result_summary[:200]}\n")
    except OSError:
        pass


def _require_auth() -> dict | None:
    """Mirror of hermes-mcp/server.py's _require_auth.

    Returns an error dict if the gate should refuse the call, None otherwise.
    Used for mutating tools (pause / resume / trigger_evolution_cycle).
    """
    if not HERMES_MCP_KEY:
        return {"error": "MCP authentication not configured. "
                         "Set HERMES_MCP_KEY in .env to enable mutating tools."}
    return None


# --------------------------------------------------------------------------
# freqtrade API helper (mirrors hermes-mcp/server.py:_ft_get)
# --------------------------------------------------------------------------


async def _ft_get(path: str) -> Any:
    async with httpx.AsyncClient(timeout=4.0) as client:
        token = await _ensure_jwt(client)
        if token is None:
            return None
        try:
            r = await client.get(
                f"{FREQTRADE_API}{path}",
                headers={"Authorization": f"Bearer {token}"},
            )
            return r.json() if r.status_code == 200 else None
        except Exception as exc:
            logger.debug("ft_get %s failed: %s", path, exc)
            return None


# --------------------------------------------------------------------------
# Trade data tools
# --------------------------------------------------------------------------


async def get_open_trades() -> list[dict]:
    data = await _ft_get("/api/v1/status") or []
    out = [
        {
            "trade_id": t.get("trade_id"),
            "pair": t.get("pair"),
            "open_rate": t.get("open_rate"),
            "current_rate": t.get("current_rate"),
            "stake_amount": t.get("stake_amount"),
            "profit_pct": t.get("profit_pct"),
            "profit_abs": t.get("profit_abs"),
            "open_date": t.get("open_date_hum") or t.get("open_date"),
            "duration": t.get("trade_duration_s"),
        }
        for t in data
    ]
    _audit("get_open_trades", {}, f"{len(out)} open")
    return out


def get_trade_history(days: int = 7) -> list[dict]:
    cutoff = datetime.now(timezone.utc) - timedelta(days=int(days))
    if not ops_db._HAVE_PG:
        return []
    with ops_db._connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT trade_id, pair, direction, opened_at, closed_at, entry_price,
                   exit_price, pnl, pnl_pct, duration_min, confidence, regime, exit_reason
            FROM trade_journal
            WHERE closed_at IS NOT NULL AND closed_at >= %s
            ORDER BY closed_at DESC LIMIT 500
            """,
            (cutoff,),
        )
        rows = cur.fetchall()
    out = [
        {k: (v.isoformat() if isinstance(v, datetime) else v) for k, v in r.items()}
        for r in rows
    ]
    _audit("get_trade_history", {"days": days}, f"{len(out)} rows")
    return out


def get_daily_pnl(days: int = 14) -> list[dict]:
    cutoff = datetime.now(timezone.utc) - timedelta(days=int(days))
    if not ops_db._HAVE_PG:
        return []
    with ops_db._connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT to_char(closed_at AT TIME ZONE 'UTC', 'YYYY-MM-DD') AS day,
                   COUNT(*)                                            AS trades,
                   SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END)            AS wins,
                   COALESCE(SUM(pnl), 0)                                AS pnl,
                   COALESCE(AVG(pnl_pct), 0)                            AS avg_pnl_pct
            FROM trade_journal
            WHERE closed_at >= %s
            GROUP BY day ORDER BY day DESC
            """,
            (cutoff,),
        )
        rows = [dict(r) for r in cur.fetchall()]
    _audit("get_daily_pnl", {"days": days}, f"{len(rows)} days")
    return rows


def get_performance_metrics() -> dict:
    if not ops_db._HAVE_PG:
        return {"trades": 0, "sharpe": 0.0, "max_dd": 0.0,
                "profit_factor": 0.0, "win_rate": 0.0, "total_pnl": 0.0}
    with ops_db._connect() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT closed_at, pnl, pnl_pct FROM trade_journal "
            "WHERE closed_at IS NOT NULL ORDER BY closed_at ASC"
        )
        rows = cur.fetchall()
    if not rows:
        out = {"trades": 0, "sharpe": 0.0, "max_dd": 0.0,
               "profit_factor": 0.0, "win_rate": 0.0, "total_pnl": 0.0}
        _audit("get_performance_metrics", {}, json.dumps(out))
        return out

    pnls = [float(r["pnl"] or 0) for r in rows]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    win_rate = len(wins) / len(rows)
    pf = (sum(wins) / abs(sum(losses))) if losses else None

    daily: dict[str, float] = {}
    for r in rows:
        day = r["closed_at"].astimezone(timezone.utc).strftime("%Y-%m-%d")
        daily[day] = daily.get(day, 0.0) + float(r["pnl_pct"] or 0)
    daily_pcts = list(daily.values())
    if len(daily_pcts) >= 2:
        mean = statistics.fmean(daily_pcts)
        sd = statistics.stdev(daily_pcts)
        sharpe = (mean / sd * (365 ** 0.5)) if sd > 0 else 0.0
    else:
        sharpe = 0.0

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
        "profit_factor": round(pf, 4) if pf is not None else None,
        "win_rate": round(win_rate, 4),
        "total_pnl": round(sum(pnls), 2),
    }
    _audit("get_performance_metrics", {}, json.dumps(out))
    return out


# --------------------------------------------------------------------------
# EPT evolution tools
# --------------------------------------------------------------------------


def get_evolution_status() -> dict:
    log_path = USER_DATA_ROOT / "logs" / "evolution.json"
    if not log_path.exists():
        out = {"generation": 0, "champion": None, "alive": [], "note": "evolution.json not present"}
        _audit("get_evolution_status", {}, "no log")
        return out
    try:
        history = json.loads(log_path.read_text())
    except Exception as exc:
        return {"error": f"could not parse evolution.json: {exc}"}
    if not history:
        return {"generation": 0, "champion": None, "alive": []}
    last = history[-1]
    out = {
        "generation": last.get("generation"),
        "champion": last.get("champion"),
        "alive": [
            {"member_id": m.get("member_id"), "fitness": m.get("fitness"),
             "metrics": m.get("metrics")}
            for m in (last.get("alive") or [])
        ],
        "snapshots": len(history),
    }
    _audit("get_evolution_status", {}, f"gen={out.get('generation')}")
    return out


def trigger_evolution_cycle() -> dict:
    refused = _require_auth()
    if refused:
        return refused
    script = USER_DATA_ROOT.parent / "scripts" / "train_drl.py"
    if not script.exists():
        # Try the alternate location
        script = USER_DATA_ROOT / "scripts" / "train_drl.py"
        if not script.exists():
            return {"error": f"script missing: {script}"}
    proc = subprocess.Popen(
        ["python3", str(script), "--synthetic", "--timesteps", "20000"],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        env=os.environ.copy(), cwd=str(USER_DATA_ROOT.parent),
    )
    _audit("trigger_evolution_cycle", {}, f"pid={proc.pid}")
    return {"started": True, "pid": proc.pid, "note": "running in background"}


def get_champion_genome() -> dict:
    log_path = USER_DATA_ROOT / "logs" / "evolution.json"
    if not log_path.exists():
        return {"error": "evolution.json not present"}
    try:
        history = json.loads(log_path.read_text())
    except Exception as exc:
        return {"error": f"could not parse evolution.json: {exc}"}
    if not history:
        return {"error": "no snapshots"}
    last = history[-1]
    champ_id = last.get("champion")
    for m in last.get("alive", []):
        if m.get("member_id") == champ_id:
            return {"member_id": champ_id, "genome": m.get("genome"),
                    "fitness": m.get("fitness"), "metrics": m.get("metrics")}
    return {"error": f"champion {champ_id} not found in alive list"}


# --------------------------------------------------------------------------
# Risk + control tools
# --------------------------------------------------------------------------


async def get_risk_status() -> dict:
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


def pause_trading(reason: str = "manual_pause_via_dashboard") -> dict:
    refused = _require_auth()
    if refused:
        return refused
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


def resume_trading(confirm: bool = False) -> dict:
    refused = _require_auth()
    if refused:
        return refused
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


# --------------------------------------------------------------------------
# Market data tools
# --------------------------------------------------------------------------


def get_current_regime() -> dict:
    if not ops_db._HAVE_PG:
        return {"regime": "unknown", "probability": 0.0}
    with ops_db._connect() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT ts, regime, probability, regime_duration_hours, state_probabilities "
            "FROM regime_log ORDER BY ts DESC LIMIT 1"
        )
        row = cur.fetchone()
    if not row:
        return {"regime": "unknown", "probability": 0.0}
    out = {
        "regime": row["regime"],
        "probability": float(row["probability"] or 0),
        "duration_hours": float(row["regime_duration_hours"] or 0),
        "state_probabilities": row["state_probabilities"],
        "ts": row["ts"].isoformat() if isinstance(row["ts"], datetime) else row["ts"],
    }
    _audit("get_current_regime", {}, out["regime"])
    return out


def get_sentiment_scores() -> list[dict]:
    if not ops_db._HAVE_PG:
        return []
    with ops_db._connect() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT ts, sentiment_score, confidence, agreement, market_impact, "
            "       n_headlines, key_events "
            "FROM sentiment_log ORDER BY ts DESC LIMIT 1"
        )
        row = cur.fetchone()
    if not row:
        return []
    out = [{
        "ts": row["ts"].isoformat() if isinstance(row["ts"], datetime) else row["ts"],
        "sentiment_score": float(row["sentiment_score"] or 0),
        "confidence": float(row["confidence"] or 0),
        "agreement": bool(row["agreement"]),
        "market_impact": row.get("market_impact"),
        "n_headlines": int(row["n_headlines"] or 0),
        "key_events": row.get("key_events"),
    }]
    _audit("get_sentiment_scores", {}, f"score={out[0]['sentiment_score']}")
    return out


def get_onchain_signals() -> dict:
    if not ops_db._HAVE_PG:
        return {}
    out: dict = {}
    with ops_db._connect() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT ts, asset, netflow, netflow_z FROM exchange_netflow "
            "ORDER BY ts DESC LIMIT 5"
        )
        out["exchange_netflow"] = [
            {"ts": r["ts"].isoformat() if isinstance(r["ts"], datetime) else r["ts"],
             "asset": r["asset"],
             "netflow": float(r["netflow"] or 0),
             "netflow_z": float(r["netflow_z"] or 0)}
            for r in cur.fetchall()
        ]
        cur.execute(
            "SELECT ts, asset, mvrv FROM mvrv_ratio ORDER BY ts DESC LIMIT 5"
        )
        out["mvrv_ratio"] = [
            {"ts": r["ts"].isoformat() if isinstance(r["ts"], datetime) else r["ts"],
             "asset": r["asset"], "mvrv": float(r["mvrv"] or 0)}
            for r in cur.fetchall()
        ]
        cur.execute(
            "SELECT ts, hash, blockchain, amount_usd, transaction_type "
            "FROM whale_transactions ORDER BY ts DESC LIMIT 5"
        )
        out["whale_transactions"] = [
            {"ts": r["ts"].isoformat() if isinstance(r["ts"], datetime) else r["ts"],
             "hash": r.get("hash"),
             "blockchain": r.get("blockchain"),
             "amount_usd": float(r["amount_usd"] or 0),
             "type": r.get("transaction_type")}
            for r in cur.fetchall()
        ]
    _audit("get_onchain_signals", {},
           f"netflow={len(out['exchange_netflow'])} mvrv={len(out['mvrv_ratio'])} whale={len(out['whale_transactions'])}")
    return out


# --------------------------------------------------------------------------
# Database tools
# --------------------------------------------------------------------------


_READ_ONLY_RE = re.compile(r"^\s*(SELECT|WITH)\b", re.IGNORECASE)
_FORBIDDEN_RE = re.compile(
    r"\b(INSERT|UPDATE|DELETE|DROP|TRUNCATE|ALTER|CREATE|GRANT|REVOKE|"
    r"COPY|VACUUM|CLUSTER|REINDEX|REFRESH)\b",
    re.IGNORECASE,
)


def query_trade_journal(sql: str) -> dict:
    if not _READ_ONLY_RE.match(sql or ""):
        return {"error": "only SELECT or WITH (CTE) statements allowed"}
    if _FORBIDDEN_RE.search(sql):
        return {"error": "forbidden keyword detected — read-only enforcement"}
    if not re.search(r"\btrade_journal\b", sql, re.IGNORECASE):
        return {"error": "query must reference the trade_journal table"}
    if not ops_db._HAVE_PG:
        return {"error": "psycopg not installed"}
    try:
        with ops_db._connect() as conn, conn.cursor() as cur:
            cur.execute(sql)
            rows = cur.fetchall()
    except Exception as exc:
        return {"error": str(exc)[:200]}
    serialised = []
    for r in rows[:1000]:
        serialised.append({
            k: (v.isoformat() if isinstance(v, datetime) else v)
            for k, v in r.items()
        })
    _audit("query_trade_journal", {"sql": sql[:120]}, f"{len(rows)} rows")
    return {"rows": serialised, "truncated": len(rows) > 1000, "n": len(rows)}


def get_regime_history(days: int = 7) -> list[dict]:
    cutoff = datetime.now(timezone.utc) - timedelta(days=int(days))
    if not ops_db._HAVE_PG:
        return []
    with ops_db._connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            WITH ranked AS (
              SELECT ts, regime, probability, regime_duration_hours,
                     LAG(regime) OVER (ORDER BY ts) AS prev_regime
              FROM regime_log
              WHERE ts >= %s
            )
            SELECT ts, regime, probability, regime_duration_hours
            FROM ranked
            WHERE regime IS DISTINCT FROM prev_regime
            ORDER BY ts DESC LIMIT 500
            """,
            (cutoff,),
        )
        rows = cur.fetchall()
    out = [
        {"ts": r["ts"].isoformat() if isinstance(r["ts"], datetime) else r["ts"],
         "regime": r["regime"],
         "probability": float(r["probability"] or 0),
         "duration_hours": float(r["regime_duration_hours"] or 0)}
        for r in rows
    ]
    _audit("get_regime_history", {"days": days}, f"{len(out)} transitions")
    return out


# --------------------------------------------------------------------------
# Tool registry — drives /api/mcp/tools and the Console UI
# --------------------------------------------------------------------------

# Each entry: name → {func, params, mutating, doc}
# params is a list of {name, type, default, required} dicts so the Console
# can render the right form fields.
TOOLS: dict[str, dict[str, Any]] = {
    "get_open_trades": {
        "func": get_open_trades, "async": True, "mutating": False,
        "params": [],
        "doc": "Currently open positions with pair, entry, current P&L, duration.",
    },
    "get_trade_history": {
        "func": get_trade_history, "async": False, "mutating": False,
        "params": [{"name": "days", "type": "int", "default": 7, "required": False}],
        "doc": "Closed trades from the last N days (full detail from trade_journal).",
    },
    "get_daily_pnl": {
        "func": get_daily_pnl, "async": False, "mutating": False,
        "params": [{"name": "days", "type": "int", "default": 14, "required": False}],
        "doc": "Per-day P&L for the last N days.",
    },
    "get_performance_metrics": {
        "func": get_performance_metrics, "async": False, "mutating": False,
        "params": [],
        "doc": "Sharpe (annualised), max drawdown, profit factor, win rate.",
    },
    "get_evolution_status": {
        "func": get_evolution_status, "async": False, "mutating": False,
        "params": [],
        "doc": "Current generation, champion ID, fitness scores from evolution.json.",
    },
    "trigger_evolution_cycle": {
        "func": trigger_evolution_cycle, "async": False, "mutating": True,
        "params": [],
        "doc": "❗ Kick off a new EPT generation in the background.",
    },
    "get_champion_genome": {
        "func": get_champion_genome, "async": False, "mutating": False,
        "params": [],
        "doc": "Champion's hyperparameters + feature subset.",
    },
    "get_risk_status": {
        "func": get_risk_status, "async": True, "mutating": False,
        "params": [],
        "doc": "Drawdown, daily loss, circuit-breaker state, open positions count.",
    },
    "pause_trading": {
        "func": pause_trading, "async": False, "mutating": True,
        "params": [{"name": "reason", "type": "str",
                    "default": "manual_pause_via_dashboard", "required": False}],
        "doc": "❗ Flip dry_run=true in config.json. Reversible via resume_trading.",
    },
    "resume_trading": {
        "func": resume_trading, "async": False, "mutating": True,
        "params": [{"name": "confirm", "type": "bool", "default": False, "required": True}],
        "doc": "❗ Flip dry_run=false. Requires confirm=True for safety.",
    },
    "get_current_regime": {
        "func": get_current_regime, "async": False, "mutating": False,
        "params": [],
        "doc": "Latest HMM regime label + state probabilities from regime_log.",
    },
    "get_sentiment_scores": {
        "func": get_sentiment_scores, "async": False, "mutating": False,
        "params": [],
        "doc": "Latest sentiment row (score, confidence, agreement, headlines).",
    },
    "get_onchain_signals": {
        "func": get_onchain_signals, "async": False, "mutating": False,
        "params": [],
        "doc": "Latest exchange netflow + MVRV + whale transactions.",
    },
    "query_trade_journal": {
        "func": query_trade_journal, "async": False, "mutating": False,
        "params": [{"name": "sql", "type": "str", "default": "", "required": True}],
        "doc": "Read-only SELECT/CTE against trade_journal only. SQL must "
               "match `\\btrade_journal\\b` and not contain DML/DDL.",
    },
    "get_regime_history": {
        "func": get_regime_history, "async": False, "mutating": False,
        "params": [{"name": "days", "type": "int", "default": 7, "required": False}],
        "doc": "Regime transitions over the last N days.",
    },
}


def schema() -> list[dict[str, Any]]:
    """Public schema for the Console UI's typeahead + parameter form."""
    return [
        {
            "name": name,
            "doc": meta["doc"],
            "mutating": meta["mutating"],
            "params": meta["params"],
        }
        for name, meta in TOOLS.items()
    ]


async def dispatch(name: str, kwargs: dict[str, Any]) -> Any:
    """Call ``TOOLS[name]['func']`` with kwargs.

    Coerces params to declared types so a JSON body of strings still works.
    Returns whatever the tool returns (dict / list / scalar).
    """
    if name not in TOOLS:
        return {"error": f"unknown tool: {name}"}
    meta = TOOLS[name]
    func = meta["func"]
    is_async = meta["async"]

    coerced: dict[str, Any] = {}
    for p in meta["params"]:
        pname = p["name"]
        if pname not in kwargs:
            continue
        val = kwargs[pname]
        ptype = p["type"]
        try:
            if ptype == "int":
                val = int(val)
            elif ptype == "float":
                val = float(val)
            elif ptype == "bool":
                val = (
                    val if isinstance(val, bool)
                    else str(val).lower() in ("1", "true", "yes", "y", "on")
                )
            else:
                val = str(val)
        except (TypeError, ValueError):
            return {"error": f"could not coerce {pname}={val!r} to {ptype}"}
        coerced[pname] = val

    if is_async:
        return await func(**coerced)
    return func(**coerced)
