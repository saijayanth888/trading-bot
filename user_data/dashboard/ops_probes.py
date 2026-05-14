"""
Liveness probes for the Ops tab.

Each probe is a pure async function that returns a dict shaped so the
endpoint layer can drop it straight into the typed envelope. No I/O outside
of network + filesystem reads. 2-second hard timeout per probe.

The dashboard container reaches:
  - postgres / freqtrade / dashboard via docker-compose service names
    (compose default network). Grafana + InfluxDB were retired 2026-05-12.
  - ollama / hermes-mcp / hermes-gateway / hermes-dashboard on the docker host
    via ``host.docker.internal`` (requires extra_hosts in docker-compose.yml)
  - hermes-gateway has no port — its liveness is read from a heartbeat file
    that a systemd timer on the host writes every 30s.
"""

from __future__ import annotations

import asyncio
import os
import socket
import time
from pathlib import Path
from typing import Any

import httpx

PROBE_TIMEOUT_S = float(os.environ.get("OPS_PROBE_TIMEOUT_S", "2.0"))
HOST = os.environ.get("HOST_DOCKER_INTERNAL", "host.docker.internal")
USER_DATA_ROOT = Path(os.environ.get(
    "USER_DATA_ROOT",
    str(Path(__file__).resolve().parent.parent),
))
HEARTBEAT_FILE = USER_DATA_ROOT / "state" / "hermes-gateway.alive"
HEARTBEAT_MCP = USER_DATA_ROOT / "state" / "hermes-mcp.alive"
HEARTBEAT_DASHBOARD = USER_DATA_ROOT / "state" / "hermes-dashboard.alive"
HEARTBEAT_MAX_AGE_S = float(os.environ.get("OPS_HEARTBEAT_MAX_AGE_S", "120"))

MCP_LOG_PATH = USER_DATA_ROOT / "logs" / "hermes_mcp.log"


# --------------------------------------------------------------------------
# Primitive probes
# --------------------------------------------------------------------------


async def tcp_probe(host: str, port: int) -> dict[str, Any]:
    """Open a TCP connection — succeeds if the listener accepts."""
    try:
        fut = asyncio.open_connection(host, port)
        reader, writer = await asyncio.wait_for(fut, timeout=PROBE_TIMEOUT_S)
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass
        return {"up": True, "via": "tcp", "endpoint": f"{host}:{port}"}
    except (OSError, asyncio.TimeoutError, socket.gaierror) as exc:
        return {"up": False, "via": "tcp", "endpoint": f"{host}:{port}", "error": str(exc)}


async def http_probe(url: str, expect_codes: tuple[int, ...] = (200, 204, 401, 403, 406)) -> dict[str, Any]:
    """HTTP GET — up if we got a response with an expected status code.

    We accept 401/403/406 because some services correctly refuse anonymous
    GETs (Grafana, streamable-http MCP) but their reachability is what we
    care about, not their auth state.
    """
    try:
        async with httpx.AsyncClient(timeout=PROBE_TIMEOUT_S, follow_redirects=False) as c:
            r = await c.get(url)
        ok = r.status_code in expect_codes
        return {"up": ok, "via": "http", "endpoint": url, "code": r.status_code}
    except (httpx.HTTPError, asyncio.TimeoutError, OSError) as exc:
        return {"up": False, "via": "http", "endpoint": url, "error": str(exc)}


def heartbeat_probe(path: Path = HEARTBEAT_FILE, max_age_s: float = HEARTBEAT_MAX_AGE_S) -> dict[str, Any]:
    """Up if file exists, mtime within max_age_s, content is a healthy systemd state.

    The heartbeat writer pipes `systemctl is-active <svc>` output into these
    .alive files. systemd's state vocabulary includes:
      * `active`     — running
      * `activating` — starting (transitional, can last ~5s during restarts)
      * `reloading`  — handling SIGHUP (transitional)
      * `deactivating`/`inactive`/`failed`/`unknown` — not up

    Originally this probe only accepted `active`, which flagged a healthy
    gateway as DOWN during every restart window. Accept the transitional
    states too — they're not failures.
    """
    HEALTHY_STATES = {"active", "activating", "reloading"}
    try:
        if not path.exists():
            return {"up": False, "via": "heartbeat", "endpoint": str(path), "error": "missing"}
        mtime = path.stat().st_mtime
        age_s = time.time() - mtime
        content = path.read_text(errors="replace").strip()
        ok = age_s <= max_age_s and content in HEALTHY_STATES
        return {
            "up": ok,
            "via": "heartbeat",
            "endpoint": str(path),
            "age_s": round(age_s, 1),
            "content": content,
        }
    except OSError as exc:
        return {"up": False, "via": "heartbeat", "endpoint": str(path), "error": str(exc)}


# --------------------------------------------------------------------------
# Aggregated service summary
# --------------------------------------------------------------------------


async def services_summary() -> dict[str, Any]:
    """Run all service probes in parallel.

    Post-2026-05-14 (freqtrade decommissioned): quanta_core is the only
    engine probed. The quanta_core probe is a postgres query (no http
    listener on the container) — checks decision freshness in
    quanta_schema.decisions.

    The host-only services (hermes-mcp, hermes-gateway, hermes-dashboard) are
    checked via heartbeat files because the host's firewall blocks docker
    bridge traffic to those ports. Heartbeat files are written by the
    hermes-gateway-heartbeat.service (user systemd, every 30 s).
    """
    tasks: dict[str, Any] = {
        "ollama":      tcp_probe(HOST, 11434),
        "postgres":    tcp_probe("postgres", 5432),
        "quanta_core": _quanta_core_probe(),
    }

    # Heartbeat-based (sync, fast)
    results: dict[str, Any] = {
        "hermes_gateway":   heartbeat_probe(HEARTBEAT_FILE),
        "hermes_mcp":       heartbeat_probe(HEARTBEAT_MCP),
        "hermes_dashboard": heartbeat_probe(HEARTBEAT_DASHBOARD),
    }
    keys = list(tasks.keys())
    coros = [tasks[k] for k in keys]
    outs = await asyncio.gather(*coros, return_exceptions=True)
    for k, out in zip(keys, outs):
        if isinstance(out, Exception):
            results[k] = {"up": False, "via": "?", "endpoint": "?", "error": str(out)}
        else:
            results[k] = out
    return results


async def _quanta_core_probe() -> dict[str, Any]:
    """Probe V4 by asking postgres: how stale is the latest decision?

    The quanta-core container has no HTTP surface; its liveness is
    indistinguishable from postgres reachability + decision freshness.
    Returns up=True when there's a decision row in the last 10 minutes,
    "stale" when older, "no_data" when the table is empty.
    """
    endpoint = "quanta_schema.decisions (postgres)"
    try:
        # Use the same sync ops_db._connect — cheap, ~1 ms.
        from . import ops_db
        if not ops_db._HAVE_PG:
            return {"up": False, "via": "pg", "endpoint": endpoint,
                    "error": "psycopg not installed"}
        with ops_db._connect() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT EXTRACT(EPOCH FROM (NOW() - MAX(ts)))::int FROM quanta_schema.decisions"
            )
            row = cur.fetchone()
        # dict_row factory: row is a dict with one key (extracted age value)
        age_s = None
        if row:
            # The value is the only column; grab it whatever key it landed under.
            age_s = next(iter(row.values()), None)
        if age_s is None:
            return {"up": False, "via": "pg", "endpoint": endpoint,
                    "error": "no decisions in ledger yet", "age_s": None}
        if age_s < 600:
            return {"up": True, "via": "pg", "endpoint": endpoint, "age_s": int(age_s)}
        return {"up": False, "via": "pg", "endpoint": endpoint,
                "error": f"last decision {age_s}s ago", "age_s": int(age_s)}
    except Exception as exc:
        return {"up": False, "via": "pg", "endpoint": endpoint, "error": str(exc)[:200]}


# --------------------------------------------------------------------------
# Training state
# --------------------------------------------------------------------------


def training_state(freqtrade_log: Path | None = None) -> dict[str, Any]:
    """Best-effort training snapshot.

    Post-2026-05-14 (freqtrade decommissioned): the TFT branch that walked
    freqtrade.log to reconstruct per-pair training queue state is gone with
    the FreqAI pipeline. ``out["tft"]`` will be ``None`` until a
    quanta-core training-state reader replaces it (the new TFT at
    src/quanta_core/models/tft.py does its own checkpointing — operator
    needs a JSON status file we can poll, similar to drl_status.json).

    DRL: read user_data/logs/drl_status.json if present.
    EPT: last entry of user_data/logs/evolution.json.

    The ``freqtrade_log`` parameter is retained for ABI compatibility with
    older callers but is now ignored.
    """
    out: dict[str, Any] = {"tft": None, "drl": None, "ept": None, "warmup": None}

    # DRL — optional status file
    drl_path = USER_DATA_ROOT / "logs" / "drl_status.json"
    if drl_path.exists():
        try:
            import json
            out["drl"] = json.loads(drl_path.read_text())
        except (OSError, ValueError):
            out["drl"] = {"status": "unreadable"}
    else:
        out["drl"] = {"status": "n/a", "note": "no drl_status.json yet"}

    # EPT — last entry of evolution.json (newline-delimited JSON or single array).
    evo_path = USER_DATA_ROOT / "logs" / "evolution.json"
    if evo_path.exists():
        try:
            import json
            text = evo_path.read_text(errors="replace").strip()
            if text.startswith("["):
                arr = json.loads(text)
                last = arr[-1] if arr else None
            else:
                # newline-delimited
                lines = [ln for ln in text.splitlines() if ln.strip()]
                last = json.loads(lines[-1]) if lines else None
            if last:
                out["ept"] = {
                    "generation": last.get("generation"),
                    "champion_id": last.get("champion_id") or last.get("champion"),
                    "champion_sharpe": last.get("champion_sharpe") or last.get("sharpe"),
                    "ts_age_s": int(time.time() - evo_path.stat().st_mtime),
                }
        except (OSError, ValueError, IndexError):
            out["ept"] = {"status": "unreadable"}
    else:
        out["ept"] = {"status": "n/a", "note": "no evolution.json yet"}

    return out


def _parse_tft_line(line: str, log_path: Path) -> dict[str, Any]:
    """Parse: ``... TFTModel - INFO - epoch 4/25  loss=1.10... val_sharpe=0.910 ...``"""
    import re
    epoch_m = re.search(r"epoch\s+(\d+)\s*/\s*(\d+)", line)
    loss_m = re.search(r"loss=([0-9.]+)", line)
    sharpe_m = re.search(r"val_sharpe=([\-0-9.]+)", line)
    out: dict[str, Any] = {}
    if epoch_m:
        out["epoch"] = int(epoch_m.group(1))
        out["max_epoch"] = int(epoch_m.group(2))
    if loss_m:
        out["loss"] = float(loss_m.group(1))
    if sharpe_m:
        out["val_sharpe"] = float(sharpe_m.group(1))
    out["log_age_s"] = int(time.time() - log_path.stat().st_mtime)
    return out


# --------------------------------------------------------------------------
# MCP probe + last call
# --------------------------------------------------------------------------


async def mcp_state() -> dict[str, Any]:
    """Liveness via heartbeat file (firewall blocks docker-bridge → host:8089),
    plus parse last tool call from the audit log so the panel shows real activity.
    """
    hb = heartbeat_probe(HEARTBEAT_MCP)
    out: dict[str, Any] = {
        "endpoint": "http://localhost:8089/mcp",
        "transport": "streamable-http",
        "probe": {
            "via": "heartbeat",
            "ok_for_streamable_http": hb.get("up", False),
            "age_s": hb.get("age_s"),
            "content": hb.get("content"),
        },
        "tools_count": None,
        "last_call": None,
    }

    if MCP_LOG_PATH.exists():
        try:
            with MCP_LOG_PATH.open("rb") as f:
                f.seek(0, 2)
                size = f.tell()
                # Read the last 100k bytes (or whole file if smaller).
                f.seek(max(0, size - 100_000))
                chunk = f.read()
            tail = chunk.decode("utf-8", errors="replace").splitlines()
            # Newest-first scan for "tool=" markers.
            for line in reversed(tail):
                if "tool=" in line:
                    out["last_call"] = _parse_mcp_log_line(line)
                    break
        except OSError:
            pass

    return out


def _parse_mcp_log_line(line: str) -> dict[str, Any]:
    """Best-effort parse of a server.py audit-log line.

    Format produced by `_audit(tool, args, result)` is something like:
       2026-05-08T14:12:00Z INFO hermes_mcp tool=get_risk_status args=... result=...

    The hermes-mcp logger emits timestamps in UTC (its `Formatter.converter`
    is `time.gmtime`). We normalise to RFC-3339 with explicit `Z` so the
    browser-side `new Date(ts)` treats the value as UTC. Without the Z,
    Date() parses it as local time, producing the "-14400s ago" drift
    operators were seeing.
    """
    import re
    # Accept ISO 8601 with optional 'Z' or '+HH:MM' suffix from the log line.
    ts_m = re.search(
        r"(\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?)",
        line,
    )
    tool_m = re.search(r"tool=([\w_]+)", line)
    ts: str | None = None
    if ts_m:
        raw_ts = ts_m.group(1)
        # Normalise space → 'T' (RFC 3339 / ISO 8601 form).
        if " " in raw_ts:
            raw_ts = raw_ts.replace(" ", "T", 1)
        # If no explicit timezone designator, the timestamp is UTC (hermes-mcp
        # writes UTC times) — append 'Z' so JS Date() parses correctly.
        if not (raw_ts.endswith("Z") or "+" in raw_ts[10:] or raw_ts.count("-") > 2):
            raw_ts = raw_ts + "Z"
        ts = raw_ts
    return {
        "ts": ts,
        "tool": tool_m.group(1) if tool_m else None,
        "raw": line[-200:],
    }
