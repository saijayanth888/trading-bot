"""Hermes integration — schedule, recent runs, composite health.

Wired up under ``/api/v5/hermes/*``. Reads from three on-disk sources:

* ``~/.hermes/cron/jobs.json`` — READ-ONLY (spec §5.4 hard constraint).
* ``~/.hermes/cron/output/<job_id>/*.md`` — parsed for run history.
* ``~/.hermes/heartbeats/*`` (best-effort) for composite health.

The retrigger action lives in ``v5/actions.py``; this router only exposes
GET endpoints.
"""
from __future__ import annotations

import json
import logging
import os
import re
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v5/hermes", tags=["v5", "hermes"])

# ---------------------------------------------------------------------------
# Path resolution
# ---------------------------------------------------------------------------


def _hermes_root() -> Path:
    return Path(os.environ.get("HERMES_ROOT", str(Path.home() / ".hermes")))


def _jobs_path() -> Path:
    return _hermes_root() / "cron" / "jobs.json"


def _output_root() -> Path:
    return _hermes_root() / "cron" / "output"


def _heartbeat_root() -> Path:
    return _hermes_root() / "heartbeats"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _read_jobs() -> list[dict[str, Any]]:
    """READ-ONLY parse of ``~/.hermes/cron/jobs.json``. Returns ``[]`` on any
    error (the dashboard must never 500 because Hermes is offline)."""
    p = _jobs_path()
    if not p.exists():
        return []
    try:
        raw = json.loads(p.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("hermes: failed to parse %s: %s", p, exc)
        return []
    jobs = raw.get("jobs") if isinstance(raw, dict) else raw
    return list(jobs or [])


def _meta(snapshot_ts: datetime, source: Path | None = None) -> dict[str, Any]:
    age_s = int((datetime.now(tz=UTC) - snapshot_ts).total_seconds())
    return {
        "snapshot_ts": snapshot_ts.isoformat(),
        "age_s": age_s,
        "stale": age_s > 300,  # 5 min default freshness window for Hermes
        "market_open_now": None,  # not relevant for Hermes — operator state
        "source": str(source) if source else None,
    }


_ISO_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})[ T](\d{2})[:-](\d{2})[:-](\d{2})")


def _parse_ts_loose(s: str | None) -> datetime | None:
    """Parse ISO-ish timestamps tolerantly. Hermes uses TZ-aware timestamps in
    jobs.json (`2026-05-12T10:12:00.591199-04:00`) but the output filenames
    are naive local-time (`2026-05-14_18-00-49.md`). Return UTC datetimes."""
    if not s:
        return None
    s = s.strip()
    try:
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        return dt.astimezone(UTC)
    except ValueError:
        pass
    m = _ISO_RE.match(s)
    if not m:
        return None
    y, mo, d, hh, mm, ss = (int(x) for x in m.groups())
    return datetime(y, mo, d, hh, mm, ss, tzinfo=UTC)


def _parse_md_run(path: Path) -> dict[str, Any] | None:
    """Parse a Hermes run-output markdown file. Tolerant to missing fields."""
    try:
        text = path.read_text(errors="replace")
    except OSError:
        return None
    # Header form:
    #   # Cron Job: <name>
    #   **Job ID:** <hex>
    #   **Run Time:** 2026-05-14 18:00:49
    #   **Schedule:** 0 18 * * *
    name_m = re.search(r"^#\s*Cron Job:\s*(.+)$", text, re.MULTILINE)
    job_id_m = re.search(r"\*\*Job ID:\*\*\s*([0-9a-f]+)", text)
    run_ts_m = re.search(r"\*\*Run Time:\*\*\s*([\d\-: ]+)", text)
    schedule_m = re.search(r"\*\*Schedule:\*\*\s*(.+?)$", text, re.MULTILINE)

    # Tail snippet — last 400 chars of body
    response_idx = text.find("## Response")
    snippet = (text[response_idx:][:1200] if response_idx >= 0 else text[-1200:]).strip()

    run_ts = _parse_ts_loose(run_ts_m.group(1) if run_ts_m else None)
    return {
        "job": (name_m.group(1).strip() if name_m else path.parent.name),
        "job_id": (job_id_m.group(1).strip() if job_id_m else path.parent.name),
        "run_ts": run_ts.isoformat() if run_ts else None,
        "schedule": (schedule_m.group(1).strip() if schedule_m else None),
        "file": path.name,
        "snippet": snippet,
        "silent": "[SILENT]" in text,
    }


def _walk_runs(limit: int) -> list[dict[str, Any]]:
    root = _output_root()
    if not root.exists():
        return []
    files: list[Path] = []
    for job_dir in root.iterdir():
        if not job_dir.is_dir():
            continue
        for md in job_dir.glob("*.md"):
            files.append(md)
    # Newest-first by mtime — robust against filenames using TZ-naive local
    # time (mtime is filesystem-truth).
    files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    runs: list[dict[str, Any]] = []
    for p in files[: max(limit, 0)]:
        row = _parse_md_run(p)
        if row is not None:
            runs.append(row)
    return runs


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.get("/schedule")
async def schedule() -> dict[str, Any]:
    """Parsed Hermes job schedule with next/last fire timestamps.

    READ-ONLY: this endpoint never modifies ``jobs.json``.
    """
    jobs_path = _jobs_path()
    snapshot_ts = (
        datetime.fromtimestamp(jobs_path.stat().st_mtime, tz=UTC)
        if jobs_path.exists()
        else datetime.now(tz=UTC)
    )
    items: list[dict[str, Any]] = []
    for j in _read_jobs():
        sched = j.get("schedule") or {}
        items.append({
            "id": j.get("id"),
            "name": j.get("name"),
            "schedule": sched.get("display") or sched.get("expr"),
            "kind": sched.get("kind"),
            "enabled": bool(j.get("enabled", False)),
            "state": j.get("state"),
            "next_run_at": j.get("next_run_at"),
            "last_run_at": j.get("last_run_at"),
            "last_status": j.get("last_status"),
            "last_error": j.get("last_error"),
            "deliver": j.get("deliver"),
            "workdir": j.get("workdir"),
        })
    return {"jobs": items, "_meta": _meta(snapshot_ts, source=jobs_path)}


@router.get("/runs")
async def runs(limit: int = 20) -> dict[str, Any]:
    """Recent Hermes run outputs, newest-first.

    Parses ``~/.hermes/cron/output/<job_id>/*.md``. ``limit`` defaults to 20
    per the spec; clamps at 200 to avoid pathological scans.
    """
    limit = max(1, min(int(limit), 200))
    runs_list = _walk_runs(limit)
    return {
        "runs": runs_list,
        "_meta": _meta(datetime.now(tz=UTC), source=_output_root()),
    }


@router.get("/health")
async def health() -> dict[str, Any]:
    """Composite Hermes health: heartbeat + last-fire-age + activating>30min.

    The result has three states:

    * ``green`` — heartbeats fresh, at least one job has fired in the last
      configured window, no jobs stuck activating.
    * ``amber`` — degraded: stale heartbeats OR no job fired in the last
      window OR a job has been ``state="activating"`` for >30 min.
    * ``red`` — every signal is bad (no jobs file, no recent runs, no
      heartbeats).
    """
    snapshot_ts = datetime.now(tz=UTC)
    jobs = _read_jobs()
    runs_list = _walk_runs(limit=20)

    # ---- last fire age ----
    last_fire_age_s: int | None = None
    for r in runs_list:
        dt = _parse_ts_loose(r.get("run_ts"))
        if dt is not None:
            last_fire_age_s = int((snapshot_ts - dt).total_seconds())
            break

    # ---- activating > 30 min ----
    stuck_activating: list[str] = []
    now_utc = snapshot_ts
    for j in jobs:
        if (j.get("state") or "").lower() != "activating":
            continue
        # Hermes writes `activating_since` when it transitions; fall back
        # to `updated_at` / `last_run_at` if missing.
        marker = j.get("activating_since") or j.get("updated_at") or j.get("last_run_at")
        dt = _parse_ts_loose(marker)
        if dt is None:
            continue
        if (now_utc - dt) > timedelta(minutes=30):
            stuck_activating.append(j.get("name") or j.get("id") or "?")

    # ---- heartbeats (best-effort) ----
    heartbeats: dict[str, dict[str, Any]] = {}
    hb_root = _heartbeat_root()
    if hb_root.exists():
        for f in hb_root.glob("*"):
            try:
                age = int((datetime.now(tz=UTC).timestamp() - f.stat().st_mtime))
            except OSError:
                continue
            heartbeats[f.name] = {"age_s": age, "stale": age > 300}

    # ---- rollup ----
    signals = {
        "jobs_loaded": len(jobs),
        "recent_runs_loaded": len(runs_list),
        "last_fire_age_s": last_fire_age_s,
        "stuck_activating": stuck_activating,
        "heartbeats": heartbeats,
    }
    status = "green"
    reasons: list[str] = []
    if not jobs:
        status = "red"
        reasons.append("hermes/cron/jobs.json missing or empty")
    if not runs_list:
        status = "red" if status == "red" else "amber"
        reasons.append("no recent runs in cron/output/")
    if last_fire_age_s is not None and last_fire_age_s > 24 * 3600:
        status = "amber" if status == "green" else status
        reasons.append(f"last fire was {last_fire_age_s // 3600}h ago")
    if stuck_activating:
        status = "amber" if status == "green" else status
        reasons.append(f"activating>30min: {', '.join(stuck_activating)}")
    if any(h["stale"] for h in heartbeats.values()):
        status = "amber" if status == "green" else status
        reasons.append("stale heartbeats")

    return {
        "status": status,
        "reasons": reasons,
        "signals": signals,
        "_meta": _meta(snapshot_ts, source=_jobs_path()),
    }


__all__ = ["router"]
