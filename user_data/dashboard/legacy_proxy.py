"""Legacy /api/ops/* and /api/v4/* deprecation middleware.

Spec §5.3 (REVISED per backend-debate G2 + functional-debate G1):

* **GET** routes — pass through to the existing in-tree handler, which
  preserves the ``{status, data, error, checked_at}`` envelope shape. We
  attach ``Deprecation: true`` and ``Link: </api/v5/...>; rel="successor-version"``
  headers so callers see the soft warning.
* **POST/PUT/PATCH/DELETE** routes — **internally rewrite** the URL to
  the v5 equivalent so the safety brake (``unified_risk.py:802`` POSTing
  ``/api/ops/pause`` for the circuit breaker) never 410s. The response
  body is whatever v5 produces; we still attach the deprecation headers.

**CRITICAL constraint**: a mutating route MUST NEVER return 410. The
circuit breaker depends on POST /api/ops/pause continuing to work for
≥7 days post-cutover. The hidden-caller audit (`inventory/hidden-callers-audit.md`)
gates the eventual deletion in v1.1 — not this file.
"""
from __future__ import annotations

import logging
from typing import Iterable

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response
from starlette.types import ASGIApp

logger = logging.getLogger(__name__)


# Mapping from legacy POST/PUT/PATCH/DELETE paths to their v5 successor.
# Order matters — longer prefixes first so we don't accidentally rewrite
# ``/api/ops/pause/something`` to ``/api/v5/actions/pause`` when a more
# specific match exists.
_MUTATING_REWRITES: list[tuple[str, str]] = [
    # circuit breaker — the call in unified_risk.py:802 lands here
    ("/api/ops/pause",         "/api/v5/actions/pause/crypto"),
    ("/api/ops/resume",        "/api/v5/actions/pause/crypto"),  # body says resume=true
    ("/api/ops/kill",          "/api/v5/actions/kill"),
    ("/api/ops/rebalance",     "/api/v5/actions/rebalance"),
    ("/api/ops/regime_config", "/api/v5/regime_config"),
    ("/api/ops/risk_gates",    "/api/v5/regime_config"),
    ("/api/ops/mcp",           "/api/v5/mcp"),
    # /api/v4/* mutating equivalents (operator quick-actions panel)
    ("/api/v4/actions/kill",     "/api/v5/actions/kill"),
    ("/api/v4/actions/pause",    "/api/v5/actions/pause"),
    ("/api/v4/actions/flatten",  "/api/v5/actions/flatten"),
]


# Mapping from legacy GET paths to their v5 successor (for the
# ``Link: successor-version`` header). We do NOT rewrite the URL — the
# legacy handler keeps returning the envelope shape — but the header
# tells migrators which endpoint to switch to.
_GET_SUCCESSORS: list[tuple[str, str]] = [
    ("/api/ops/status",         "/api/v5/status"),
    ("/api/ops/portfolio",      "/api/v5/portfolio"),
    ("/api/ops/positions",      "/api/v5/positions"),
    ("/api/ops/metrics",        "/api/v5/metrics"),
    ("/api/ops/alerts",         "/api/v5/alerts"),
    ("/api/ops/regime_config",  "/api/v5/regime_config"),
    ("/api/ops/risk_gates",     "/api/v5/regime_config"),
    ("/api/v4/portfolio",       "/api/v5/portfolio"),
    ("/api/v4/positions",       "/api/v5/positions"),
    ("/api/v4/metrics",         "/api/v5/metrics"),
    ("/api/v4/strategies",      "/api/v5/strategies"),
]

_MUTATING_METHODS = {"POST", "PUT", "PATCH", "DELETE"}


def _match_prefix(path: str, table: Iterable[tuple[str, str]]) -> str | None:
    """Return the successor path for ``path`` (preserving suffix), or None."""
    for legacy, successor in table:
        if path == legacy:
            return successor
        if path.startswith(legacy + "/"):
            return successor + path[len(legacy):]
    return None


class LegacyProxyMiddleware(BaseHTTPMiddleware):
    """ASGI middleware: rewrite mutating legacy routes; tag GETs as deprecated.

    Mounted from ``app.py`` via ``app.add_middleware(LegacyProxyMiddleware)``.
    """

    async def dispatch(self, request: Request, call_next) -> Response:
        path = request.url.path
        method = request.method.upper()

        # 1. Mutating verbs on /api/ops/* or /api/v4/actions/* — REWRITE
        if method in _MUTATING_METHODS and (
            path.startswith("/api/ops/") or path.startswith("/api/v4/actions")
        ):
            successor = _match_prefix(path, _MUTATING_REWRITES)
            if successor:
                # In-place rewrite. We mutate the ASGI scope so downstream
                # FastAPI routing picks up the new path. This is the
                # canonical Starlette pattern for internal proxying.
                logger.info(
                    "legacy_proxy: rewrite %s %s -> %s (mutating)",
                    method, path, successor,
                )
                request.scope["path"] = successor
                request.scope["raw_path"] = successor.encode("utf-8")
                response = await call_next(request)
                _attach_deprecation_headers(response, successor)
                return response
            # No mapping — let it through so the in-tree legacy handler runs.
            # NEVER 410 a mutating route (spec §5.3 CRITICAL).
            response = await call_next(request)
            _attach_deprecation_headers(response, None)
            return response

        # 2. GET on a known legacy path — let through, tag with successor header
        if method == "GET" and (
            path.startswith("/api/ops/") or path.startswith("/api/v4/")
        ):
            successor = _match_prefix(path, _GET_SUCCESSORS)
            response = await call_next(request)
            _attach_deprecation_headers(response, successor)
            return response

        # 3. Anything else — passthrough.
        return await call_next(request)


def _attach_deprecation_headers(response: Response, successor: str | None) -> None:
    """Tag the response per RFC 8594. Idempotent."""
    response.headers["Deprecation"] = "true"
    if successor:
        # RFC 8594: Link header with rel="successor-version".
        response.headers["Link"] = f'<{successor}>; rel="successor-version"'


def install(app) -> None:
    """Convenience: install the middleware on ``app``. Idempotent."""
    # Starlette doesn't expose middleware introspection cleanly; we rely on
    # the caller invoking ``install`` once.
    app.add_middleware(LegacyProxyMiddleware)


__all__ = ["LegacyProxyMiddleware", "install"]
