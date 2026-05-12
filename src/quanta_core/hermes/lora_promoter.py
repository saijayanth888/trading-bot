"""``quanta_core.hermes.lora_promoter`` — Sunday LoRA promoter.

Cadence
-------
Cron ``0 14 * * 0`` (Sunday 14:00 ET; the real mf-api workflow runs at
04:00 ET, but the operator's spec says 14:00 ET locally — both fire the
same workflow trigger so the cadence is just *when this Python wrapper
runs*).

Run
---
1. ``POST /api/automation/workflows/{workflow_id}/trigger`` against mf-api.
2. Poll ``GET /api/automation/workflows/{workflow_id}/runs?limit=1`` (or
   ``GET /api/evolve/{run_id}``) until the run is ``completed`` or
   ``failed`` or until the global poll timeout fires.
3. Read ``champions.json`` (resolved from the mf-api response or the
   filesystem fallback) and record which roles promoted.
4. Write ``~/.quanta/state/last_lora_promotion.json`` per doc §5.2.

Per doc 13 §1 quanta_core **never** runs training, **never** runs Ollama
``create``.  This module only triggers mf-api and reads its outcome.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import Any

try:
    import httpx
except Exception:  # pragma: no cover
    httpx = None  # type: ignore[assignment]

from quanta_core.hermes._common import (
    HermesConfig,
    SlackNotifier,
    StateWriter,
    configure_logging,
    load_config,
    utc_iso,
    utc_now,
)


@dataclass
class PromotionRecord:
    role: str
    pareto_pass: bool
    from_version: str | None = None
    to_version: str | None = None
    metrics: Mapping[str, Any] = field(default_factory=dict)
    kept_champion: str | None = None


@dataclass
class MfApiClient:
    """Tiny client for the four mf-api endpoints we touch."""

    base_url: str
    api_key: str | None
    timeout_seconds: float = 30.0
    logger: logging.Logger = field(
        default_factory=lambda: logging.getLogger("quanta_core.hermes.mfapi")
    )

    @property
    def headers(self) -> dict[str, str]:
        h = {"Accept": "application/json"}
        if self.api_key:
            h["X-API-Key"] = self.api_key
        return h

    def trigger_workflow(self, workflow_id: str) -> dict[str, Any] | None:
        """POST trigger.  Returns the response JSON, or None on error."""

        if httpx is None:  # pragma: no cover
            self.logger.warning("httpx unavailable — cannot trigger workflow")
            return None
        url = f"{self.base_url}/api/automation/workflows/{workflow_id}/trigger"
        try:
            resp = httpx.post(url, headers=self.headers, timeout=self.timeout_seconds)
            if resp.status_code >= 400:
                self.logger.warning(
                    "mf-api trigger non-2xx %d: %s",
                    resp.status_code,
                    resp.text[:160],
                )
                return None
            body = resp.json()
            return body if isinstance(body, dict) else None
        except Exception as exc:
            self.logger.warning("mf-api trigger failed: %s", exc)
            return None

    def latest_run(self, workflow_id: str) -> dict[str, Any] | None:
        """GET latest run for a workflow."""

        if httpx is None:  # pragma: no cover
            return None
        url = (
            f"{self.base_url}/api/automation/workflows/"
            f"{workflow_id}/runs?limit=1"
        )
        try:
            resp = httpx.get(url, headers=self.headers, timeout=self.timeout_seconds)
            if resp.status_code != 200:
                return None
            data = resp.json()
            runs = data.get("runs") or data.get("items") or data
            if isinstance(runs, list) and runs:
                first = runs[0]
                if isinstance(first, dict):
                    return first
            return None
        except Exception as exc:
            self.logger.warning("mf-api latest_run failed: %s", exc)
            return None

    def champion(self) -> dict[str, Any] | None:
        """GET current champion across all tracks."""

        if httpx is None:  # pragma: no cover
            return None
        try:
            resp = httpx.get(
                f"{self.base_url}/api/models/champion",
                headers=self.headers,
                timeout=self.timeout_seconds,
            )
            if resp.status_code != 200:
                return None
            body = resp.json()
            return body if isinstance(body, dict) else None
        except Exception as exc:
            self.logger.warning("mf-api champion failed: %s", exc)
            return None


def _read_champions_json(
    cfg: HermesConfig,
) -> Mapping[str, Any] | Sequence[Mapping[str, Any]] | None:
    """Read the on-disk champions file, falling back across known paths.

    Per doc 13 §2 mf-api is the system of record but writes a sidecar
    summary the trading-bot host bind-mounts.  We probe the standard
    locations in priority order.
    """

    candidates = [
        Path.home() / ".dgx-train" / "champions.json",
        cfg.repo_root_path / "stocks" / "memory" / "champions.json",
        cfg.state_root / "champions.json",
    ]
    for path in candidates:
        if path.exists():
            try:
                data = json.loads(path.read_text())
                return data if isinstance(data, (dict, list)) else None
            except Exception:
                continue
    return None


def _records_from_champions(
    data: Mapping[str, Any] | Sequence[Mapping[str, Any]],
) -> list[PromotionRecord]:
    """Map champions.json shape → :class:`PromotionRecord` list.

    The shape is intentionally tolerant — mf-api's own schema evolves
    independently and we want partial reads to still produce useful state.
    """

    out: list[PromotionRecord] = []
    items: Sequence[Mapping[str, Any]]
    if isinstance(data, list):
        items = data
    elif isinstance(data, Mapping) and isinstance(data.get("promotions"), list):
        items = data["promotions"]
    elif isinstance(data, Mapping) and isinstance(data.get("tracks"), dict):
        items = [
            {"role": role, **payload}
            for role, payload in data["tracks"].items()
            if isinstance(payload, dict)
        ]
    else:
        items = []
    for item in items:
        role = str(item.get("role") or item.get("track_id") or "unknown")
        out.append(
            PromotionRecord(
                role=role,
                pareto_pass=bool(item.get("pareto_pass", item.get("promoted", False))),
                from_version=item.get("from") or item.get("prev_generation"),
                to_version=item.get("to") or item.get("generation"),
                metrics=item.get("metrics", {}) or {},
                kept_champion=item.get("kept_champion"),
            )
        )
    return out


def _poll_until_done(
    client: MfApiClient,
    workflow_id: str,
    interval: int,
    max_seconds: int,
    log: logging.Logger,
) -> dict[str, Any] | None:
    """Poll the workflow until it reaches a terminal state."""

    deadline = time.monotonic() + max_seconds
    last: dict[str, Any] | None = None
    while time.monotonic() < deadline:
        run = client.latest_run(workflow_id)
        if run:
            last = run
            status = str(run.get("status", "")).lower()
            log.info("mf-api workflow status=%s", status or "?")
            if status in {"completed", "success", "succeeded"}:
                return run
            if status in {"failed", "error", "errored"}:
                return run
        time.sleep(interval)
    log.warning("mf-api workflow poll timed out after %ds", max_seconds)
    return last


def _parse_args(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="quanta_core.hermes.lora_promoter",
        description="Trigger the weekly ModelForge workflow and record promotions",
    )
    parser.add_argument(
        "--workflow-id",
        default=None,
        help="mf-api workflow id (overrides MODELFORGE_WORKFLOW_ID env)",
    )
    parser.add_argument(
        "--skip-trigger",
        action="store_true",
        help="do not POST a trigger; just poll the existing latest run",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="skip mf-api calls; emit a state file marked dry_run=true",
    )
    return parser.parse_args(list(argv))


def run(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv if argv is not None else sys.argv[1:])
    cfg = load_config()
    log = configure_logging("lora_promoter")
    started = utc_now()
    notifier = SlackNotifier(cfg.slack_webhook_url, cfg.slack_channel)
    state_path = cfg.state_root / "last_lora_promotion.json"

    workflow_id = args.workflow_id or cfg.mf_weekly_workflow_id
    if not workflow_id and not args.dry_run:
        log.error("no workflow id (set MODELFORGE_WORKFLOW_ID or --workflow-id)")
        StateWriter(state_path).write(
            {
                "ts": utc_iso(started),
                "error": "no_workflow_id",
            }
        )
        return 2

    if args.dry_run:
        payload = {
            "ts": utc_iso(started),
            "dry_run": True,
            "workflow_id": workflow_id,
            "training_window": _training_window(started.date()),
        }
        StateWriter(state_path).write(payload)
        log.info("dry-run complete; no mf-api calls made")
        return 0

    assert workflow_id is not None  # mypy
    client = MfApiClient(
        base_url=cfg.mf_api_url,
        api_key=cfg.mf_api_key,
        timeout_seconds=cfg.postgres_timeout_seconds,
    )

    if not args.skip_trigger:
        triggered = client.trigger_workflow(workflow_id)
        if triggered is None:
            log.error("workflow trigger failed")
            notifier.post(
                ":rotating_light: *[lora_promoter]* mf-api trigger failed"
            )
            StateWriter(state_path).write(
                {
                    "ts": utc_iso(started),
                    "error": "trigger_failed",
                    "workflow_id": workflow_id,
                }
            )
            return 1
        log.info("workflow triggered; polling for completion")

    final_run = _poll_until_done(
        client,
        workflow_id,
        cfg.mf_poll_interval_seconds,
        cfg.mf_poll_max_seconds,
        log,
    )
    if final_run is None:
        log.warning("no run data available; recording state and exiting")
        StateWriter(state_path).write(
            {
                "ts": utc_iso(started),
                "error": "no_run_data",
                "workflow_id": workflow_id,
            }
        )
        return 1

    champions = _read_champions_json(cfg)
    records: list[PromotionRecord] = (
        _records_from_champions(champions) if champions else []
    )

    payload = {
        "ts": utc_iso(started),
        "workflow_id": workflow_id,
        "run_id": final_run.get("id") or final_run.get("run_id"),
        "run_status": final_run.get("status"),
        "training_window": _training_window(started.date()),
        "promotions": [
            {
                "role": r.role,
                "pareto_pass": r.pareto_pass,
                "from": r.from_version,
                "to": r.to_version,
                "metrics": dict(r.metrics),
                "kept_champion": r.kept_champion,
            }
            for r in records
        ],
        "champion_source": "champions.json" if champions else "missing",
        "gpu_window_seconds": int((utc_now() - started).total_seconds()),
    }
    StateWriter(state_path).write(payload)

    promoted = [r.role for r in records if r.pareto_pass]
    kept = [r.role for r in records if not r.pareto_pass]
    notifier.post(
        ":robot_face: lora_promoter · "
        f"promoted={','.join(promoted) or 'none'} · "
        f"kept={','.join(kept) or 'none'}"
    )
    return 0


def _training_window(today: date) -> list[str]:
    """Return the most-recent fully-completed Mon→Sun week as two ISO dates.

    The Sunday cron fires *after* the prior week's data is settled, so the
    correct bracket is the week ending on the most recent Sunday strictly
    before ``today`` (or *equal to* ``today`` when run on a Sunday — that
    Sunday is itself the close of the just-completed week).
    """

    # Most recent Sunday on or before ``today``.
    # Python weekday: Mon=0..Sun=6.
    days_since_sunday = (today.weekday() + 1) % 7
    sunday = today - timedelta(days=days_since_sunday)
    monday = sunday - timedelta(days=6)
    return [monday.isoformat(), sunday.isoformat()]


if __name__ == "__main__":  # pragma: no cover
    sys.exit(run())
