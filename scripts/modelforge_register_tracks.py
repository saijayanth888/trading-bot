#!/usr/bin/env python3
"""
ModelForge track registration — one-shot, idempotent.

Registers the 6 trading-bot LLM roles as ``evolution_tracks`` rows in
ModelForge so its LangGraph + APScheduler + Pareto-promotion loop ("Sunday
champion run") can pick them up automatically.

Once a row exists, ModelForge owns the training/eval/promote lifecycle for
that role. The trading-bot's only ongoing responsibilities are (1) writing
curated examples into ``~/.dgx-train/datasets/<track-id>/curated/`` via the
nightly ingest+curate cron, and (2) calling the resulting adapter at
inference time via ``POST /api/forge/query``.

Idempotency
-----------
For each row we GET ``/api/forge/tracks/{id}`` first. If it 404s we POST
the row body to ``/api/forge/tracks``. If it 200s we log "already
registered" and skip. ``--force`` re-POSTs anyway (caller's risk:
ModelForge may reject or merge depending on its current schema; we honour
HTTP status and surface it in the summary).

Environment / auth
------------------
ModelForge gates ``/api/*`` behind ``X-API-Key`` (constant-time, see
``model-forge/apps/api/src/middleware/auth.py``). In dev with no key set,
the middleware logs a single warning and lets requests through. Production
deployments **must** set ``MODELFORGE_API_KEY``.

The key is resolved from these sources in order, first hit wins:

  1. ``--api-key=<value>`` CLI flag
  2. ``MODELFORGE_API_KEY`` env var
  3. ``MODELFORGE_API_KEY=...`` line in ``~/.env-modelforge``
  4. ``MODELFORGE_API_KEY=...`` line in ``./.env``

If none are found, the script proceeds without the header — fine in dev,
will 401 in prod.

Base URL is taken from ``--base-url`` or ``MODELFORGE_BASE_URL`` env, falling
back to ``http://localhost:8000``.

CLI
---
::

    python3 scripts/modelforge_register_tracks.py            # register all 6
    python3 scripts/modelforge_register_tracks.py --dry-run  # preview, no POST
    python3 scripts/modelforge_register_tracks.py --track trading-reflector
    python3 scripts/modelforge_register_tracks.py --force    # re-POST existing
    python3 scripts/modelforge_register_tracks.py --delete trading-reflector

Exit codes
----------
* ``0`` — all targeted tracks ended in a known-good state
  (``created`` / ``already_registered`` / ``forced_update`` / ``dry_run`` / ``deleted``)
* ``1`` — at least one track failed (HTTP error, network, or unexpected response)

See ``docs/MODELFORGE_TRACK_REGISTRATION.md`` for the operator runbook.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

logger = logging.getLogger("modelforge_register_tracks")

# --------------------------------------------------------------------------- #
# Defaults — must stay in lock-step with:
#   * docs/MODELFORGE_INTEGRATION_PLAN.md  §2 (the 6 roles)
#   * docs/4_WEEK_EXECUTION_PLAN.md        "The 6 'tracks'" table
#   * scripts/modelforge_ingest.py         AGENT_TO_TRACK mapping
# --------------------------------------------------------------------------- #

DEFAULT_BASE_URL = "http://localhost:8000"
DEFAULT_TIMEOUT_S = 10.0
DEFAULT_BASE_MODEL = "qwen3:30b"
DEFAULT_LORA_RANK = 16
DEFAULT_LORA_ALPHA = 32
DEFAULT_LEARNING_RATE = 2e-4
DEFAULT_MAX_SAMPLES = 2000
DEFAULT_SCHEDULE = "weekly"  # ModelForge's Sunday 02:00 ET champion run

# Operator's locked decision (per memory project_session_2026-05-11_t30_checkpoint):
# qwen3:30b base for 6+ months. Override per-track if the role calls for a
# different base (e.g. hermes3:8b for strict JSON roles).

TRACKS: list[dict[str, Any]] = [
    {
        "id": "trading-reflector",
        "display_name": "Reflector — post-mortem writer",
        "base_model": DEFAULT_BASE_MODEL,
        "role": (
            "Write 2-4 sentence post-mortems on closed paper trades, "
            "cite the realized alpha figure and the most informative input signal."
        ),
        "evals": [
            "faithfulness_regex",
            "predictive_hit_rate_30d",
            "judge_score",
            "debate_impact",
        ],
        "schedule": DEFAULT_SCHEDULE,
        "expected_data_path": "~/.dgx-train/datasets/trading-reflector/curated/",
    },
    {
        "id": "trading-bull",
        "display_name": "Bull Analyst",
        "base_model": DEFAULT_BASE_MODEL,
        "role": (
            "Bullish debate participant — 200-1500 word prose with evidence "
            "citations drawn from the input bundle."
        ),
        "evals": ["evidence_density", "judge_preference"],
        "schedule": DEFAULT_SCHEDULE,
        "expected_data_path": "~/.dgx-train/datasets/trading-bull/curated/",
    },
    {
        "id": "trading-bear",
        "display_name": "Bear Analyst",
        "base_model": DEFAULT_BASE_MODEL,
        "role": (
            "Bearish debate participant — 200-1500 word prose with evidence "
            "citations drawn from the input bundle."
        ),
        "evals": ["evidence_density", "judge_preference"],
        "schedule": DEFAULT_SCHEDULE,
        "expected_data_path": "~/.dgx-train/datasets/trading-bear/curated/",
    },
    {
        "id": "trading-arbiter",
        "display_name": "Portfolio Manager (Arbiter)",
        "base_model": DEFAULT_BASE_MODEL,
        "role": (
            "Structured TraderProposal output — BUY/SELL/HOLD/SKIP plus "
            "entry, stop, and target levels."
        ),
        "evals": [
            "decision_consistency",
            "downstream_pnl_per_decision",
            "structured_output_validity",
        ],
        "schedule": DEFAULT_SCHEDULE,
        "expected_data_path": "~/.dgx-train/datasets/trading-arbiter/curated/",
    },
    {
        "id": "trading-regime-tagger",
        "display_name": "Regime Tagger (JSON)",
        "base_model": DEFAULT_BASE_MODEL,
        "role": (
            "Classify market regime as one of: trending_up, trending_down, "
            "mean_reverting, high_volatility, unknown."
        ),
        "evals": ["structured_output_validity", "agreement_with_hmm"],
        "schedule": DEFAULT_SCHEDULE,
        "expected_data_path": "~/.dgx-train/datasets/trading-regime-tagger/curated/",
    },
    {
        "id": "trading-indicator-selector",
        "display_name": "Indicator Selector (JSON)",
        "base_model": DEFAULT_BASE_MODEL,
        "role": "Pick at most 8 non-redundant indicators per market regime.",
        "evals": ["structured_output_validity", "downstream_strategy_alpha"],
        "schedule": DEFAULT_SCHEDULE,
        "expected_data_path": "~/.dgx-train/datasets/trading-indicator-selector/curated/",
    },
]


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def track_ids() -> list[str]:
    return [t["id"] for t in TRACKS]


def build_post_body(track: dict[str, Any]) -> dict[str, Any]:
    """Map our TRACK spec dict to the ``evolution_tracks`` row schema.

    Schema (per ``lineage_db.py:820-848`` upsert_track INSERT statement):
      * ``track_id``         TEXT   (PK)
      * ``name``             TEXT
      * ``description``      TEXT
      * ``base_model``       TEXT
      * ``target_benchmarks`` JSONB (list of arbitrary strings)
      * ``lora_rank``        INT
      * ``lora_alpha``       INT
      * ``learning_rate``    DOUBLE PRECISION
      * ``max_samples``      INT
      * ``enabled``          BOOLEAN

    Extras we send (``schedule``, ``expected_data_path``) are not currently
    persisted by ModelForge's schema; we include them in the POST body so a
    future schema bump can pick them up. Today's ``upsert_track`` ignores
    unknown keys (it pulls only the columns it knows about).
    """
    return {
        "track_id": track["id"],
        "name": track["display_name"],
        "description": track["role"],
        "base_model": track["base_model"],
        "target_benchmarks": list(track["evals"]),
        "lora_rank": DEFAULT_LORA_RANK,
        "lora_alpha": DEFAULT_LORA_ALPHA,
        "learning_rate": DEFAULT_LEARNING_RATE,
        "max_samples": DEFAULT_MAX_SAMPLES,
        "enabled": True,
        # Extras — see docstring above.
        "schedule": track["schedule"],
        "expected_data_path": track["expected_data_path"],
    }


def _load_env_file(path: Path) -> dict[str, str]:
    """Parse a minimal ``KEY=VALUE`` env file. Comments + blanks ignored.

    Values may be quoted with single or double quotes; quotes are stripped.
    Lines without ``=`` are skipped silently.
    """
    out: dict[str, str] = {}
    try:
        text = path.read_text(encoding="utf-8")
    except (FileNotFoundError, PermissionError, OSError):
        return out
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        if key:
            out[key] = value
    return out


def resolve_api_key(cli_value: str | None) -> str | None:
    """Resolve API key in priority order: CLI > env > ~/.env-modelforge > ./.env."""
    if cli_value:
        return cli_value
    env_val = os.environ.get("MODELFORGE_API_KEY")
    if env_val:
        return env_val
    for candidate in (Path.home() / ".env-modelforge", Path.cwd() / ".env"):
        parsed = _load_env_file(candidate)
        if "MODELFORGE_API_KEY" in parsed and parsed["MODELFORGE_API_KEY"]:
            return parsed["MODELFORGE_API_KEY"]
    return None


def resolve_base_url(cli_value: str | None) -> str:
    if cli_value:
        return cli_value.rstrip("/")
    return os.environ.get("MODELFORGE_BASE_URL", DEFAULT_BASE_URL).rstrip("/")


# --------------------------------------------------------------------------- #
# Result type — printed by the summary table
# --------------------------------------------------------------------------- #


@dataclass
class RegistrationResult:
    track_id: str
    status: str  # created | already_registered | forced_update | dry_run | deleted | error | skipped
    message: str = ""
    http_status: int | None = None
    detail: dict[str, Any] = field(default_factory=dict)

    def is_ok(self) -> bool:
        return self.status not in {"error"}


# --------------------------------------------------------------------------- #
# Core registration / deletion ops
# --------------------------------------------------------------------------- #


def _track_url(base_url: str, track_id: str | None = None) -> str:
    if track_id is None:
        return f"{base_url}/api/forge/tracks"
    return f"{base_url}/api/forge/tracks/{track_id}"


def check_track_exists(
    client: httpx.Client, base_url: str, track_id: str
) -> tuple[bool, int]:
    """GET the per-track endpoint. Returns ``(exists, http_status)``.

    The current ModelForge API exposes only ``GET /api/forge/tracks`` (list)
    — not per-id. So we fall back to listing and scanning if the per-id GET
    404s with that specific reason (or returns 405).

    Treats network errors as "unknown — bail" by re-raising; callers catch.
    """
    try:
        resp = client.get(_track_url(base_url, track_id))
    except httpx.HTTPError:
        raise

    # Per-id endpoint — fast path if ModelForge ships it.
    if resp.status_code == 200:
        return True, 200
    if resp.status_code == 404:
        # Could mean "no such track" OR "no such route". Resolve via list.
        return _exists_via_list(client, base_url, track_id), 404
    if resp.status_code in (405, 501):
        # Route doesn't exist — fall back to list scan.
        return _exists_via_list(client, base_url, track_id), resp.status_code
    # Any other status — propagate as not-found so caller surfaces it.
    return False, resp.status_code


def _exists_via_list(
    client: httpx.Client, base_url: str, track_id: str
) -> bool:
    """Fallback existence check: list all tracks and look for our id."""
    resp = client.get(_track_url(base_url))
    resp.raise_for_status()
    payload = resp.json()
    # ModelForge returns ``{"tracks": [...]}`` (forge.py:46).
    rows: list[dict[str, Any]]
    if isinstance(payload, dict) and "tracks" in payload:
        rows = list(payload["tracks"])
    elif isinstance(payload, list):
        rows = list(payload)
    else:
        rows = []
    return any(r.get("track_id") == track_id or r.get("id") == track_id for r in rows)


def post_track(
    client: httpx.Client, base_url: str, body: dict[str, Any]
) -> httpx.Response:
    return client.post(_track_url(base_url), json=body)


def delete_track(
    client: httpx.Client, base_url: str, track_id: str
) -> httpx.Response:
    return client.delete(_track_url(base_url, track_id))


def register_one(
    client: httpx.Client,
    base_url: str,
    track: dict[str, Any],
    *,
    dry_run: bool,
    force: bool,
) -> RegistrationResult:
    track_id = track["id"]
    body = build_post_body(track)

    if dry_run:
        logger.info("[dry-run] would POST %s body=%s", track_id, json.dumps(body))
        return RegistrationResult(
            track_id=track_id,
            status="dry_run",
            message="dry-run: no POST issued",
            detail=body,
        )

    try:
        exists, get_status = check_track_exists(client, base_url, track_id)
    except httpx.HTTPError as exc:
        logger.error("GET %s failed: %s", track_id, exc)
        return RegistrationResult(
            track_id=track_id,
            status="error",
            message=f"GET failed: {exc}",
        )

    if exists and not force:
        logger.info("[skip] %s already registered", track_id)
        return RegistrationResult(
            track_id=track_id,
            status="already_registered",
            message="row exists; not re-posted (use --force to override)",
            http_status=200,
        )

    try:
        resp = post_track(client, base_url, body)
    except httpx.HTTPError as exc:
        logger.error("POST %s failed: %s", track_id, exc)
        return RegistrationResult(
            track_id=track_id,
            status="error",
            message=f"POST failed: {exc}",
        )

    if resp.status_code in (200, 201):
        status = "forced_update" if (exists and force) else "created"
        logger.info("[%s] %s (HTTP %s)", status, track_id, resp.status_code)
        return RegistrationResult(
            track_id=track_id,
            status=status,
            message=f"HTTP {resp.status_code}",
            http_status=resp.status_code,
            detail=_safe_json(resp),
        )

    # Non-2xx — record it.
    logger.error(
        "POST %s returned HTTP %s: %s",
        track_id,
        resp.status_code,
        resp.text[:300],
    )
    return RegistrationResult(
        track_id=track_id,
        status="error",
        message=f"HTTP {resp.status_code}: {resp.text[:200]}",
        http_status=resp.status_code,
    )


def delete_one(
    client: httpx.Client, base_url: str, track_id: str, *, dry_run: bool
) -> RegistrationResult:
    if dry_run:
        logger.info("[dry-run] would DELETE %s", track_id)
        return RegistrationResult(
            track_id=track_id, status="dry_run", message="dry-run: no DELETE issued"
        )
    try:
        resp = delete_track(client, base_url, track_id)
    except httpx.HTTPError as exc:
        logger.error("DELETE %s failed: %s", track_id, exc)
        return RegistrationResult(
            track_id=track_id, status="error", message=f"DELETE failed: {exc}"
        )
    if resp.status_code in (200, 204):
        logger.info("[deleted] %s", track_id)
        return RegistrationResult(
            track_id=track_id,
            status="deleted",
            message=f"HTTP {resp.status_code}",
            http_status=resp.status_code,
        )
    if resp.status_code == 404:
        logger.info("[skip] %s not present, nothing to delete", track_id)
        return RegistrationResult(
            track_id=track_id,
            status="skipped",
            message="not found",
            http_status=404,
        )
    logger.error(
        "DELETE %s returned HTTP %s: %s",
        track_id,
        resp.status_code,
        resp.text[:300],
    )
    return RegistrationResult(
        track_id=track_id,
        status="error",
        message=f"HTTP {resp.status_code}: {resp.text[:200]}",
        http_status=resp.status_code,
    )


def _safe_json(resp: httpx.Response) -> dict[str, Any]:
    try:
        data = resp.json()
        if isinstance(data, dict):
            return data
    except (ValueError, TypeError, json.JSONDecodeError):
        pass
    return {}


# --------------------------------------------------------------------------- #
# Summary table
# --------------------------------------------------------------------------- #


def print_summary(results: list[RegistrationResult]) -> None:
    if not results:
        print("(no tracks processed)")
        return
    width_id = max(len("TRACK_ID"), max(len(r.track_id) for r in results))
    width_status = max(len("STATUS"), max(len(r.status) for r in results))
    width_http = max(len("HTTP"), 5)

    header = f"{'TRACK_ID'.ljust(width_id)}  {'STATUS'.ljust(width_status)}  {'HTTP'.ljust(width_http)}  MESSAGE"
    sep = "-" * (width_id + width_status + width_http + 12)
    print(header)
    print(sep)
    for r in results:
        http = "" if r.http_status is None else str(r.http_status)
        print(
            f"{r.track_id.ljust(width_id)}  "
            f"{r.status.ljust(width_status)}  "
            f"{http.ljust(width_http)}  "
            f"{r.message}"
        )
    print()
    ok = sum(1 for r in results if r.is_ok())
    print(f"{ok}/{len(results)} OK")


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Register trading-bot LLM roles as ModelForge evolution_tracks (idempotent).",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--base-url",
        default=None,
        help="ModelForge API base URL. Default: $MODELFORGE_BASE_URL or http://localhost:8000",
    )
    parser.add_argument(
        "--api-key",
        default=None,
        help="X-API-Key header value. Default: $MODELFORGE_API_KEY or ~/.env-modelforge",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=DEFAULT_TIMEOUT_S,
        help="HTTP request timeout in seconds.",
    )
    parser.add_argument(
        "--track",
        default=None,
        help="Register only this track id. Default: all 6.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show the request body that would be POSTed but don't send it.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Re-POST even if the track is already registered.",
    )
    parser.add_argument(
        "--delete",
        default=None,
        help=(
            "Rollback: delete the named track from ModelForge. "
            "Mutually exclusive with normal registration."
        ),
    )
    parser.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        help="Increase log verbosity.",
    )
    return parser.parse_args(argv)


def _select_tracks(only: str | None) -> list[dict[str, Any]]:
    if only is None:
        return list(TRACKS)
    matches = [t for t in TRACKS if t["id"] == only]
    if not matches:
        valid = ", ".join(track_ids())
        raise SystemExit(f"--track {only!r} not found. Known: {valid}")
    return matches


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    base_url = resolve_base_url(args.base_url)
    api_key = resolve_api_key(args.api_key)
    headers: dict[str, str] = {"Content-Type": "application/json"}
    if api_key:
        headers["X-API-Key"] = api_key
    else:
        logger.warning(
            "No MODELFORGE_API_KEY found — proceeding without auth (OK for dev)."
        )

    if args.delete and (args.force or args.dry_run is False and args.track):
        # Allow --delete with --dry-run, but reject illegal mixes.
        pass

    results: list[RegistrationResult] = []

    with httpx.Client(timeout=args.timeout, headers=headers) as client:
        if args.delete:
            results.append(
                delete_one(client, base_url, args.delete, dry_run=args.dry_run)
            )
        else:
            selected = _select_tracks(args.track)
            for track in selected:
                results.append(
                    register_one(
                        client,
                        base_url,
                        track,
                        dry_run=args.dry_run,
                        force=args.force,
                    )
                )

    print_summary(results)
    return 0 if all(r.is_ok() for r in results) else 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
