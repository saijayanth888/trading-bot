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
from logging.handlers import RotatingFileHandler
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

# Rotate at the same size/backup count as hermes-mcp/server.py:_audit so the
# unified log file can't grow unbounded — REVIEW_2026-05-11 §P0-R found the
# old path used raw ``AUDIT_LOG.open("a")`` calls that bypassed any rotation,
# producing a 600+MB hermes_mcp.log on the host.
_AUDIT_LOG_MAX_BYTES = 2_000_000
_AUDIT_LOG_BACKUPS = 5

_audit_logger: logging.Logger | None = None


def _get_audit_logger() -> logging.Logger:
    """Lazy single-instance RotatingFileHandler logger for MCP audit lines.

    Idempotent: the second caller gets the same logger (and the same single
    handler). We attach our own handler instead of reusing logger.handlers
    so a parent app.py changing root-logger handlers doesn't accidentally
    duplicate this log into stderr / Slack / etc.
    """
    global _audit_logger
    if _audit_logger is not None:
        return _audit_logger
    lg = logging.getLogger("hermes_mcp.audit.dashboard")
    lg.setLevel(logging.INFO)
    # Don't propagate to the root — the audit channel is a wire/wire-tap; we
    # don't want a stray Sentry / Slack handler picking up every tool call.
    lg.propagate = False
    if not lg.handlers:
        try:
            AUDIT_LOG.parent.mkdir(parents=True, exist_ok=True)
            h = RotatingFileHandler(
                str(AUDIT_LOG),
                maxBytes=_AUDIT_LOG_MAX_BYTES,
                backupCount=_AUDIT_LOG_BACKUPS,
            )
            # %(message)s only — we format the audit string ourselves
            # (timestamp + via= + tool= + args= + result=) to stay
            # byte-compatible with hermes-mcp/server.py's audit lines so
            # the ops "MCP wire" parser sees one homogenous log.
            h.setFormatter(logging.Formatter("%(message)s"))
            lg.addHandler(h)
        except OSError:
            # If we can't open the file (read-only volume, missing dir),
            # fall back silently — audit is best-effort.
            pass
    _audit_logger = lg
    return lg


def _audit(tool: str, args: dict, result_summary: str = "ok") -> None:
    """Emit an audit line in the same format as hermes-mcp/server.py.

    Routes through the RotatingFileHandler in _get_audit_logger() instead of
    raw open(..., "a") so the log can't grow unbounded; rotates at ~2 MB,
    five backups.
    """
    try:
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        args_str = json.dumps(args, default=str)[:300]
        _get_audit_logger().info(
            "%s INFO via=dashboard tool=%s args=%s result=%s",
            ts, tool, args_str, result_summary[:200],
        )
    except Exception as exc:  # noqa: BLE001 — audit must never crash a caller
        logger.debug("audit log failed: %s", exc)


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
    """Authenticated freqtrade GET with transparent 401 → re-login retry.

    Wraps ``data_sources.ft_authed_get`` to keep this module's public surface
    unchanged (callers still receive parsed JSON-or-None).
    """
    from .data_sources import ft_authed_get
    async with httpx.AsyncClient(timeout=4.0) as client:
        try:
            r = await ft_authed_get(client, path, timeout=4.0)
            if r is None:
                return None
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


def trigger_evolution_cycle(mode: str = "mock") -> dict:
    """❗ Kick off a new EPT generation — mirrors hermes-mcp/server.py.

    The previous wiring fired ``scripts/train_drl.py`` which trains the DRL
    component and leaves evolution.json empty — confirming "wrong script"
    in REVIEW_2026-05-11 §P0-S. The right runner is
    ``user_data/scripts/run_ept_generation.py`` (used by the MCP server's
    trigger_evolution_cycle tool and the nightly evolution cron).

    We invoke it synchronously with a 5-minute timeout and return the
    parsed champion summary so callers see real generation numbers, not
    a fire-and-forget pid.
    """
    refused = _require_auth()
    if refused:
        return refused
    script = USER_DATA_ROOT / "scripts" / "run_ept_generation.py"
    if not script.exists():
        return {"error": f"script missing: {script}"}
    try:
        proc = subprocess.run(
            ["python3", str(script), "--mode", str(mode)],
            capture_output=True, text=True, timeout=300,
            env=os.environ.copy(), cwd=str(USER_DATA_ROOT.parent),
        )
    except subprocess.TimeoutExpired:
        _audit("trigger_evolution_cycle", {"mode": mode}, "TIMEOUT >5min")
        return {"error": "evolution cycle timed out after 5 minutes"}
    except Exception as exc:  # noqa: BLE001
        _audit("trigger_evolution_cycle", {"mode": mode}, f"exec failed: {exc}")
        return {"error": f"could not execute runner: {exc}"}

    if proc.returncode != 0:
        _audit(
            "trigger_evolution_cycle",
            {"mode": mode},
            f"exit={proc.returncode} stderr={(proc.stderr or '')[-400:]}",
        )
        return {
            "error": f"runner exited with code {proc.returncode}",
            "stderr_tail": (proc.stderr or "")[-1200:],
        }
    summary: dict[str, Any]
    try:
        # The runner prints a JSON summary on its final line; we use the
        # last `{...}` we can find rather than a strict splitlines parse
        # to be robust to trailing newlines / warnings on stdout.
        stdout = (proc.stdout or "").strip()
        last_brace = stdout.rfind("{")
        summary = json.loads(stdout[last_brace:]) if last_brace >= 0 else {}
    except Exception:
        summary = {"ok": True, "raw_stdout": (proc.stdout or "")[-2000:]}
    _audit(
        "trigger_evolution_cycle",
        {"mode": mode},
        f"gen={summary.get('generation')} champ={(summary.get('champion') or {}).get('member_id')}",
    )
    return summary


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
    """Read on-chain features from the new free pipeline (rebuilt 2026-05-08).

    The old paid-API tables (exchange_netflow, mvrv_ratio, whale_transactions)
    are still present in the schema but no longer written — we now ingest into
    derivatives_features (OKX funding rate → netflow proxy via z-score) and
    macro_features (BTC MVRV, fear & greed, mempool fees). Querying the old
    tables silently returned empty arrays which made every MCP caller think
    we had no on-chain signal.
    """
    if not ops_db._HAVE_PG:
        return {}
    out: dict = {}
    with ops_db._connect() as conn, conn.cursor() as cur:
        # derivatives_features: per-pair OKX funding rate + open interest +
        # taker buy/sell volume. Used as the "netflow proxy" (no genuinely
        # free spot exchange-flow API exists for retail).
        cur.execute(
            "SELECT ts, pair, funding_rate, open_interest_usd, "
            "       long_short_ratio, taker_buy_vol_usd, taker_sell_vol_usd "
            "FROM derivatives_features ORDER BY ts DESC LIMIT 8"
        )
        out["derivatives"] = [
            {"ts": r["ts"].isoformat() if isinstance(r["ts"], datetime) else r["ts"],
             "pair": r["pair"],
             "funding_rate": float(r["funding_rate"]) if r.get("funding_rate") is not None else None,
             "open_interest_usd": float(r["open_interest_usd"]) if r.get("open_interest_usd") is not None else None,
             "long_short_ratio": float(r["long_short_ratio"]) if r.get("long_short_ratio") is not None else None,
             "taker_buy_usd": float(r["taker_buy_vol_usd"]) if r.get("taker_buy_vol_usd") is not None else None,
             "taker_sell_usd": float(r["taker_sell_vol_usd"]) if r.get("taker_sell_vol_usd") is not None else None}
            for r in cur.fetchall()
        ]
        # macro_features: stablecoin mcap (delta), F&G index, BTC dominance,
        # BTC MVRV, mempool fastest-fee.
        cur.execute(
            "SELECT ts, stablecoin_mcap_usd, stablecoin_mcap_chg_24h, "
            "       fear_greed_index, btc_dominance_pct, btc_mvrv, "
            "       btc_mempool_fastest_fee "
            "FROM macro_features ORDER BY ts DESC LIMIT 5"
        )
        out["macro"] = [
            {"ts": r["ts"].isoformat() if isinstance(r["ts"], datetime) else r["ts"],
             "stablecoin_mcap_usd": float(r["stablecoin_mcap_usd"]) if r.get("stablecoin_mcap_usd") is not None else None,
             "stablecoin_chg_24h_pct": float(r["stablecoin_mcap_chg_24h"]) if r.get("stablecoin_mcap_chg_24h") is not None else None,
             "fear_greed": float(r["fear_greed_index"]) if r.get("fear_greed_index") is not None else None,
             "btc_dominance_pct": float(r["btc_dominance_pct"]) if r.get("btc_dominance_pct") is not None else None,
             "btc_mvrv": float(r["btc_mvrv"]) if r.get("btc_mvrv") is not None else None,
             "btc_mempool_fastest_fee": float(r["btc_mempool_fastest_fee"]) if r.get("btc_mempool_fastest_fee") is not None else None}
            for r in cur.fetchall()
        ]
    _audit("get_onchain_signals", {},
           f"derivatives={len(out['derivatives'])} macro={len(out['macro'])}")
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
# Dangerous tokens that survived the keyword blocklist before
# (REVIEW_2026-05-11 §P0-Q): semicolons / comments / union-stacking and
# Postgres-specific privilege-leaks. ops_db._connect() already enforces a
# 2-second statement_timeout, but pg_read_file and friends can leak host
# state in well under 2 seconds; we must reject them at the input layer.
_SQLI_DENY_RE = re.compile(
    r"(--|/\*|\*/|;|"
    r"\bunion\b|\bpg_sleep\b|\bpg_read_file\b|\bpg_ls_dir\b|"
    r"\bpg_read_binary_file\b|\bcurrent_setting\b|\bdblink\b|"
    r"\blo_import\b|\blo_export\b|\bcopy\s+from\b)",
    re.IGNORECASE,
)


def query_trade_journal(sql: str) -> dict:
    raw = sql or ""
    if not _READ_ONLY_RE.match(raw):
        return {"error": "only SELECT or WITH (CTE) statements allowed"}
    if _FORBIDDEN_RE.search(raw):
        return {"error": "forbidden keyword detected — read-only enforcement"}
    sqli_hit = _SQLI_DENY_RE.search(raw)
    if sqli_hit:
        _audit("query_trade_journal", {"sql": raw[:120]}, f"reject: sqli_token={sqli_hit.group(0)!r}")
        return {"error": "query rejected — disallowed token (comments, semicolons, union, pg_sleep, etc.)"}
    if not re.search(r"\btrade_journal\b", raw, re.IGNORECASE):
        return {"error": "query must reference the trade_journal table"}
    if not ops_db._HAVE_PG:
        return {"error": "psycopg not installed"}
    try:
        with ops_db._connect() as conn, conn.cursor() as cur:
            # Defence-in-depth — force the transaction to RO at the DB level
            # so even if our input filter is bypassed, the planner refuses
            # mutations. Mirrors hermes-mcp/server.py:_query.
            cur.execute("SET default_transaction_read_only = on")
            cur.execute(raw)
            rows = cur.fetchall()
    except Exception as exc:
        return {"error": str(exc)[:200]}
    serialised = []
    for r in rows[:1000]:
        serialised.append({
            k: (v.isoformat() if isinstance(v, datetime) else v)
            for k, v in r.items()
        })
    _audit("query_trade_journal", {"sql": raw[:120]}, f"{len(rows)} rows")
    return {"rows": serialised, "truncated": len(rows) > 1000, "n": len(rows)}


# ─── Stocks subsystem tools — mirror hermes-mcp/server.py ─────────────────
# Dashboard-side parity wrappers so /api/ops/mcp/<tool> returns useful data
# instead of empty stubs. The hermes-mcp/server.py copies are authoritative
# when called via the streamable-http MCP on port 8089.

_STOCKS_ROOT = Path(os.environ.get("STOCKS_ROOT", "/freqtrade/stocks"))


def _read_stocks_json(rel: str) -> Any:
    p = _STOCKS_ROOT / rel
    try:
        if p.is_file():
            return json.loads(p.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("read %s failed: %s", p, exc)
    return None


def get_combined_portfolio() -> dict:
    """Combined crypto + stocks portfolio + drawdown via unified_risk."""
    try:
        import sys
        if "/freqtrade" not in sys.path:
            sys.path.insert(0, "/freqtrade")
        from user_data.modules.unified_risk import get_combined_risk_status
    except ImportError as exc:
        return {"error": f"unified_risk import failed: {exc}"}
    status = get_combined_risk_status()
    _audit("get_combined_portfolio", {},
           f"total=${status['total_equity']:.0f} dd={status['combined_drawdown_pct']}%")
    return status


def get_stock_positions() -> list[dict]:
    """Current wheel positions from stocks/wheel/state/positions.json."""
    raw = _read_stocks_json("wheel/state/positions.json") or []
    if not isinstance(raw, list):
        raw = []
    out = [
        {
            "kind": p.get("kind"),
            "underlying": p.get("underlying"),
            "qty": p.get("qty"),
            "strike": p.get("strike"),
            "expiry": p.get("expiry"),
            "entry_credit_usd": float(p.get("entry_credit") or 0.0),
            "contract": p.get("contract_symbol"),
            "opened_at": p.get("opened_at"),
        }
        for p in raw
    ]
    _audit("get_stock_positions", {}, f"{len(out)} positions")
    return out


def get_stock_pnl(days: int = 7) -> dict:
    """Stock P&L from TRADE-LOG.md + wheel trades.jsonl over last N days."""
    import re as _re
    log_path = _STOCKS_ROOT / "memory" / "TRADE-LOG.md"
    if not log_path.is_file():
        return {"error": "TRADE-LOG.md missing", "days": days}

    text = log_path.read_text(errors="replace")
    cutoff = (datetime.now(timezone.utc) - timedelta(days=int(days))).date()
    action_re = _re.compile(r"\[(\d{4}-\d{2}-\d{2})\]\s+(BUY|SELL|STOPPED|TIGHTEN|SCAN)")
    pnl_re = _re.compile(r"\*\*Day P&L:\*\*\s+([+\-]?[\d.,]+)")
    eod_re = _re.compile(r"###\s+(\d{4}-\d{2}-\d{2})\s+—\s+EOD")

    actions = {"BUY": 0, "SELL": 0, "STOPPED": 0, "TIGHTEN": 0, "SCAN": 0}
    realized_pnl = 0.0
    eod_count = 0
    cur_eod = None
    for line in text.splitlines():
        m = eod_re.search(line)
        if m:
            try:
                cur_eod = datetime.strptime(m.group(1), "%Y-%m-%d").date()
            except ValueError:
                cur_eod = None
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
        if m and cur_eod and cur_eod >= cutoff:
            try:
                realized_pnl += float(m.group(1).replace(",", ""))
                eod_count += 1
            except ValueError:
                pass

    wheel_pnl = 0.0
    wheel_path = _STOCKS_ROOT / "wheel" / "state" / "trades.jsonl"
    if wheel_path.is_file():
        cutoff_iso = cutoff.isoformat()
        try:
            for line in wheel_path.read_text().splitlines():
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


def get_wheel_status() -> dict:
    """Wheel state: open puts/calls/shares + cumulative premium."""
    snap = _read_stocks_json("wheel/state/account_snapshot.json") or {}
    positions = _read_stocks_json("wheel/state/positions.json") or []
    if not isinstance(positions, list):
        positions = []

    snap_path = _STOCKS_ROOT / "wheel" / "state" / "account_snapshot.json"
    snap_age = None
    try:
        if snap_path.is_file():
            snap_age = int(datetime.now(timezone.utc).timestamp() - snap_path.stat().st_mtime)
    except OSError:
        pass

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
        "params": [{"name": "mode", "type": "str", "default": "mock", "required": False,
                    "doc": "'mock' (deterministic surrogate) or 'live' (reads trade_journal)"}],
        "doc": "❗ Kick off a new EPT generation synchronously (runs run_ept_generation.py).",
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
    # ── Stocks subsystem (Shark + Wheel) — mirrors hermes-mcp/server.py ────
    "get_combined_portfolio": {
        "func": get_combined_portfolio, "async": False, "mutating": False,
        "params": [],
        "doc": "Combined crypto + stocks equity, drawdown, breaker state.",
    },
    "get_stock_positions": {
        "func": get_stock_positions, "async": False, "mutating": False,
        "params": [],
        "doc": "Current Alpaca + wheel stock positions (read-only).",
    },
    "get_stock_pnl": {
        "func": get_stock_pnl, "async": False, "mutating": False,
        "params": [{"name": "days", "type": "int", "default": 7, "required": False}],
        "doc": "Stock P&L over N days from stocks/memory/TRADE-LOG.md + wheel trades.jsonl.",
    },
    "get_wheel_status": {
        "func": get_wheel_status, "async": False, "mutating": False,
        "params": [],
        "doc": "Options-wheel state — open puts/calls + cumulative premium.",
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
