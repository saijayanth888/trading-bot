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
    """Best-effort training snapshot, smart enough to be actionable.

    Parses the freqtrade log to surface:
      - which pair is currently training, current epoch / max
      - per-pair completion status this cycle (done / training / queued)
      - readiness: does pair_dictionary.json exist? (gate for predictions)
      - first-trade ETA based on remaining pairs × avg-epoch-time

    DRL: read user_data/logs/drl_status.json if present.
    EPT: last entry of user_data/logs/evolution.json.
    """
    out: dict[str, Any] = {"tft": None, "drl": None, "ept": None, "warmup": None}

    # ── TFT: walk the log forward to reconstruct queue state ─────────
    log_candidates = [freqtrade_log] if freqtrade_log else [
        USER_DATA_ROOT / "logs" / "freqtrade.log",
    ]
    log_path = next((p for p in log_candidates if p and p.exists()), None)
    if log_path:
        try:
            with log_path.open("rb") as f:
                f.seek(0, 2)
                size = f.tell()
                f.seek(max(0, size - 500_000))  # ~last 500KB has the recent training session
                chunk = f.read()
            tail = chunk.decode("utf-8", errors="replace").splitlines()

            # State machine: walk forward, track which pair is currently
            # training and whether it's still active. Reset epoch counter on
            # each "Starting training X/Y" or "early stopping" line.
            import re as _re
            cur_pair = None
            cur_epoch = None
            max_epoch = None
            cur_val = None
            cur_loss = None
            last_ts = None
            pairs_started: dict[str, dict] = {}     # pair → {start_ts, end_ts, epochs, val_sharpe, status}
            epoch_durations: list[float] = []       # for ETA estimation
            prev_epoch_ts = None

            for line in tail:
                ts_m = _re.search(r"(\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2})", line)
                ts = ts_m.group(1) if ts_m else None

                start_m = _re.search(r"Starting training (\w+)/\w+", line)
                if start_m:
                    pair = start_m.group(1).upper()
                    # Close previous pair (in case freqtrade switched without early-stop log)
                    if cur_pair and cur_pair in pairs_started and pairs_started[cur_pair].get("status") == "training":
                        pairs_started[cur_pair].update(
                            status="done", end_ts=ts, last_epoch=cur_epoch,
                            val_sharpe=cur_val, max_epoch=max_epoch,
                        )
                    cur_pair = pair
                    cur_epoch = None
                    max_epoch = None
                    cur_val = None
                    prev_epoch_ts = None
                    pairs_started[pair] = {"start_ts": ts, "status": "training"}
                    continue

                ep_m = _re.search(r"TFTModel.*epoch\s+(\d+)\s*/\s*(\d+)", line)
                if ep_m:
                    cur_epoch = int(ep_m.group(1))
                    max_epoch = int(ep_m.group(2))
                    val_m = _re.search(r"val_sharpe=([\-0-9.]+)", line)
                    loss_m = _re.search(r"loss=([0-9.]+)", line)
                    if val_m: cur_val = float(val_m.group(1))
                    if loss_m: cur_loss = float(loss_m.group(1))
                    if ts:
                        last_ts = ts
                        if prev_epoch_ts:
                            try:
                                from datetime import datetime as _dt
                                a = _dt.fromisoformat(prev_epoch_ts.replace(" ", "T"))
                                b = _dt.fromisoformat(ts.replace(" ", "T"))
                                dt = (b - a).total_seconds()
                                if 60 <= dt <= 1200:  # sane range: 1–20 min/epoch
                                    epoch_durations.append(dt)
                            except Exception:
                                pass
                        prev_epoch_ts = ts
                    continue

                if "early stopping at epoch" in line:
                    es_m = _re.search(r"early stopping at epoch (\d+).*best val_sharpe=([\-0-9.]+)", line)
                    if cur_pair and cur_pair in pairs_started:
                        pairs_started[cur_pair].update(
                            status="done", end_ts=ts,
                            last_epoch=int(es_m.group(1)) if es_m else cur_epoch,
                            val_sharpe=float(es_m.group(2)) if es_m else cur_val,
                            max_epoch=max_epoch, early_stopped=True,
                        )
                    continue

            # Whichever pair is "currently training" at the end of the walk.
            if cur_pair and pairs_started.get(cur_pair, {}).get("status") == "training":
                pairs_started[cur_pair].update(
                    last_epoch=cur_epoch, max_epoch=max_epoch,
                    val_sharpe=cur_val, loss=cur_loss,
                )

            # ETA computation: avg epoch duration × remaining epochs of current
            # pair + (estimated full-pair epochs × queue length).
            avg_epoch_s = (sum(epoch_durations) / len(epoch_durations)) if epoch_durations else None
            avg_pair_epochs = 8  # observed: pairs early-stop around epoch 7-10 typically
            current_pair_remaining_s = None
            if avg_epoch_s and cur_epoch is not None and cur_pair:
                # naive: remaining = max(avg_pair_epochs - cur_epoch, 0) × avg_epoch_s
                rem_eps = max(avg_pair_epochs - cur_epoch, 1)
                current_pair_remaining_s = int(rem_eps * avg_epoch_s)

            # ── pair_dictionary.json existence (the gate for "model ready") ──
            pair_dict_path = USER_DATA_ROOT / "models" / "tft_v1" / "pair_dictionary.json"
            pair_dict_exists = pair_dict_path.exists()

            # Build the human-friendly tft block.
            out["tft"] = {
                "current_pair": cur_pair if pairs_started.get(cur_pair, {}).get("status") == "training" else None,
                "epoch": cur_epoch,
                "max_epoch": max_epoch,
                "val_sharpe": cur_val,
                "loss": cur_loss,
                "log_age_s": int(time.time() - log_path.stat().st_mtime) if log_path.exists() else None,
                "pairs": [
                    {"pair": p, **info}
                    for p, info in pairs_started.items()
                ],
                "avg_epoch_seconds": int(avg_epoch_s) if avg_epoch_s else None,
                "current_pair_eta_s": current_pair_remaining_s,
                "pair_dict_ready": pair_dict_exists,
            }

            # ── Warmup banner: tell the operator clearly why no trades are firing ──
            done_count = sum(1 for info in pairs_started.values() if info.get("status") == "done")
            total = len(pairs_started)
            if not pair_dict_exists:
                msg = (
                    f"freqai warm-up: pair_dictionary.json not yet written "
                    f"(it lands after the first full training cycle). "
                    f"{done_count}/{total} pairs done, {cur_pair or '?'} training "
                    f"epoch {cur_epoch or '?'}/{max_epoch or '?'}. "
                    f"First trade ETA: {current_pair_remaining_s//60 if current_pair_remaining_s else '?'} min."
                )
                out["warmup"] = {"reason": "no_pair_dict", "message": msg, "eta_seconds": current_pair_remaining_s}
        except OSError:
            pass

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
