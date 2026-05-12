"""Minimal HTTP server that publishes ``/health`` from Hermes state files.

Hermes writes per-subsystem state to ``~/.quanta/state/*.json``; this module
reads those files and exposes a single aggregated ``/health`` endpoint for
external probers (uptime-robot, load balancer, ops dashboard).

The server is intentionally tiny — it uses the stdlib
:class:`http.server.ThreadingHTTPServer` so it has zero dependency on
FastAPI / Starlette / Uvicorn (those are pulled in by other modules but we
do not want this healthcheck to depend on them).
"""

from __future__ import annotations

import json
import logging
import threading
import time
from collections.abc import Iterable
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Final, Literal

logger = logging.getLogger("quanta_core.observability.healthcheck")

HealthStatusLiteral = Literal["ok", "degraded", "down"]

_HTTP_OK: Final[int] = 200
_HTTP_SERVICE_UNAVAILABLE: Final[int] = 503
_HTTP_NOT_FOUND: Final[int] = 404
_STATE_FRESHNESS_S: Final[float] = 120.0


@dataclass(frozen=True, slots=True)
class HealthStatus:
    """Result of one ``/health`` evaluation.

    Attributes
    ----------
    status:
        ``"ok"`` if every required file is fresh and reports healthy;
        ``"degraded"`` if any file is stale or missing; ``"down"`` if a
        required file reports a hard failure.
    components:
        Per-file detail. Each value is ``{"status": ..., "age_s": ...,
        "detail": ...}``.
    ts:
        Wall-clock timestamp of the evaluation.
    """

    status: HealthStatusLiteral
    components: dict[str, dict[str, Any]]
    ts: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "components": self.components,
            "ts": self.ts,
        }

    @property
    def http_status(self) -> int:
        return _HTTP_OK if self.status == "ok" else _HTTP_SERVICE_UNAVAILABLE


class HealthcheckPublisher:
    """Read Hermes state files; serve ``/health`` over HTTP.

    Parameters
    ----------
    state_dir:
        Directory containing ``*.json`` state files. Defaults to
        ``~/.quanta/state``.
    required_components:
        File stems that MUST be present and fresh; absence ⇒ ``degraded``.
    freshness_s:
        Maximum age in seconds before a file is considered stale (default
        120s).
    """

    def __init__(
        self,
        state_dir: Path | None = None,
        required_components: Iterable[str] | None = None,
        freshness_s: float = _STATE_FRESHNESS_S,
    ) -> None:
        if freshness_s <= 0:
            raise ValueError(f"freshness_s must be > 0, got {freshness_s!r}")
        self._state_dir = state_dir or Path.home() / ".quanta" / "state"
        self._required = list(required_components or ["live_engine"])
        self._freshness_s = freshness_s
        self._server: ThreadingHTTPServer | None = None
        self._server_thread: threading.Thread | None = None

    @property
    def state_dir(self) -> Path:
        return self._state_dir

    def evaluate(self) -> HealthStatus:
        """Snapshot the current health from disk."""
        components: dict[str, dict[str, Any]] = {}
        now = time.time()
        overall: HealthStatusLiteral = "ok"
        seen: set[str] = set()
        if self._state_dir.is_dir():
            for path in sorted(self._state_dir.glob("*.json")):
                stem = path.stem
                seen.add(stem)
                comp = self._evaluate_file(path, now)
                components[stem] = comp
                status: str = comp["status"]
                if status == "down":
                    overall = "down"
                elif status == "degraded" and overall != "down":
                    overall = "degraded"
        for required in self._required:
            if required not in seen:
                components[required] = {
                    "status": "degraded",
                    "age_s": None,
                    "detail": "state file missing",
                }
                if overall == "ok":
                    overall = "degraded"
        return HealthStatus(status=overall, components=components, ts=now)

    def _evaluate_file(self, path: Path, now: float) -> dict[str, Any]:
        try:
            stat = path.stat()
        except OSError as exc:
            return {
                "status": "degraded",
                "age_s": None,
                "detail": f"stat failed: {exc}",
            }
        age_s = now - stat.st_mtime
        if age_s > self._freshness_s:
            return {
                "status": "degraded",
                "age_s": age_s,
                "detail": (f"stale (age {age_s:.0f}s > freshness_s {self._freshness_s:.0f}s)"),
            }
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            return {
                "status": "degraded",
                "age_s": age_s,
                "detail": f"unreadable: {exc}",
            }
        reported = self._extract_status(payload)
        return {
            "status": reported,
            "age_s": age_s,
            "detail": payload.get("detail") if isinstance(payload, dict) else None,
        }

    @staticmethod
    def _extract_status(payload: Any) -> HealthStatusLiteral:
        """Map a state-file payload to one of ok / degraded / down."""
        if not isinstance(payload, dict):
            return "degraded"
        raw = payload.get("status")
        if isinstance(raw, str):
            raw = raw.lower()
            if raw in {"ok", "healthy", "up", "running"}:
                return "ok"
            if raw in {"degraded", "warn", "warning"}:
                return "degraded"
            if raw in {"down", "error", "failed", "critical"}:
                return "down"
        # Fall back to ``paused`` semantics: paused-by-governor = degraded.
        if payload.get("paused") is True:
            return "degraded"
        return "ok"

    # ------------------------------------------------------------------ HTTP

    def start(self, host: str = "127.0.0.1", port: int = 8089) -> int:
        """Start the threaded HTTP server. Returns the bound port."""
        if self._server is not None:
            raise RuntimeError("healthcheck server already running")
        handler = self._build_handler()
        server = ThreadingHTTPServer((host, port), handler)
        thread = threading.Thread(
            target=server.serve_forever,
            name="quanta-healthcheck",
            daemon=True,
        )
        thread.start()
        self._server = server
        self._server_thread = thread
        return int(server.server_address[1])

    def stop(self) -> None:
        """Stop the HTTP server."""
        if self._server is None:
            return
        self._server.shutdown()
        self._server.server_close()
        if self._server_thread is not None:
            self._server_thread.join(timeout=5.0)
        self._server = None
        self._server_thread = None

    def _build_handler(self) -> type[BaseHTTPRequestHandler]:
        publisher = self

        class _Handler(BaseHTTPRequestHandler):
            def log_message(
                self,
                format: str,  # noqa: A002 — stdlib API uses ``format``
                *args: Any,
            ) -> None:
                logger.debug("healthcheck_http %s", format % args)

            def do_GET(self) -> None:
                if self.path not in {"/health", "/health/"}:
                    self.send_response(_HTTP_NOT_FOUND)
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(json.dumps({"error": "not found"}).encode("utf-8"))
                    return
                snapshot = publisher.evaluate()
                body = json.dumps(snapshot.to_dict()).encode("utf-8")
                self.send_response(snapshot.http_status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        return _Handler
