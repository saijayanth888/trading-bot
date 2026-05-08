"""
Liveness probes for the Ops tab.

Each probe is a pure async function that returns a dict shaped so the
endpoint layer can drop it straight into the typed envelope. No I/O outside
of network + filesystem reads. 2-second hard timeout per probe.

The dashboard container reaches:
  - postgres / freqtrade / dashboard / influxdb / grafana via docker-compose
    service names (compose default network)
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
    """Up if file exists, mtime within max_age_s, content == 'active'."""
    try:
        if not path.exists():
            return {"up": False, "via": "heartbeat", "endpoint": str(path), "error": "missing"}
        mtime = path.stat().st_mtime
        age_s = time.time() - mtime
        content = path.read_text(errors="replace").strip()
        ok = age_s <= max_age_s and content == "active"
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

    The host-only services (hermes-mcp, hermes-gateway, hermes-dashboard) are
    checked via heartbeat files because the host's firewall blocks docker
    bridge traffic to those ports. Heartbeat files are written by the
    hermes-gateway-heartbeat.service (user systemd, every 30 s).
    """
    tasks = {
        "ollama":     tcp_probe(HOST, 11434),
        "freqtrade":  http_probe("http://freqtrade:8080/api/v1/ping"),
        "postgres":   tcp_probe("postgres", 5432),
        "influxdb":   http_probe("http://influxdb:8086/health"),
        "grafana":    http_probe("http://grafana:3000/api/health"),
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


# --------------------------------------------------------------------------
# Training state
# --------------------------------------------------------------------------


def training_state(freqtrade_log: Path | None = None) -> dict[str, Any]:
    """Best-effort training snapshot.

    TFT: parse most-recent ``TFTModel - INFO - epoch N/M loss=... val_sharpe=...`` line.
    DRL: read ``user_data/logs/drl_status.json`` if present.
    EPT: last entry of ``user_data/logs/evolution.json``.
    """
    out: dict[str, Any] = {"tft": None, "drl": None, "ept": None}

    # TFT — read tail of the freqtrade log we have access to.
    log_candidates = [freqtrade_log] if freqtrade_log else [
        USER_DATA_ROOT / "logs" / "freqtrade.log",
    ]
    for log_path in log_candidates:
        if log_path and log_path.exists():
            try:
                # tail efficiently
                with log_path.open("rb") as f:
                    f.seek(0, 2)
                    size = f.tell()
                    chunk = f.read(min(size, 200_000))
                tail = chunk.decode("utf-8", errors="replace").splitlines()
                for line in reversed(tail):
                    if "TFTModel" in line and "epoch " in line:
                        out["tft"] = _parse_tft_line(line, log_path)
                        break
                if out["tft"]:
                    break
            except OSError:
                continue

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
    """Probe streamable-http and parse last tool call from the audit log."""
    probe = await http_probe(f"http://{HOST}:8089/mcp")
    out: dict[str, Any] = {
        "endpoint": probe["endpoint"],
        "transport": "streamable-http",
        "probe": {
            "code": probe.get("code"),
            "ok_for_streamable_http": probe.get("up", False),
        },
        "tools_count": None,
        "last_call": None,
    }

    if MCP_LOG_PATH.exists():
        try:
            with MCP_LOG_PATH.open("rb") as f:
                f.seek(0, 2)
                size = f.tell()
                chunk = f.read(min(size, 100_000))
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
       2026-05-08 14:12:00 INFO hermes_mcp tool=get_risk_status args=... result=...
    """
    import re
    ts_m = re.search(r"(\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2})", line)
    tool_m = re.search(r"tool=([\w_]+)", line)
    return {
        "ts": ts_m.group(1) if ts_m else None,
        "tool": tool_m.group(1) if tool_m else None,
        "raw": line[-200:],
    }
