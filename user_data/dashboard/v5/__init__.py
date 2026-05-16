"""V5 dashboard API aggregator.

Exports a single ``v5_router`` (``APIRouter``) that combines every v5
sub-router into one mount point. ``user_data/dashboard/app.py`` does:

    from .v5 import v5_router
    app.include_router(v5_router)

Builder split (2026-05-16 redesign):

* Builder A owns ``portfolio.py``, ``positions.py``, ``metrics.py``,
  ``strategies.py`` (producer-side rollups).
* Builder B (this file) owns ``status.py``, ``alerts.py``, ``actions.py``,
  ``hermes.py``, ``regime_config.py``, ``decisions.py``, ``mcp.py``.

Imports are wrapped in try/except so a partial deploy where A's routers
have not landed yet still gives operators the B surface — and vice
versa. The fail-soft posture is important: a missing module must NEVER
take the whole dashboard down (spec §5.1).
"""
from __future__ import annotations

import logging

from fastapi import APIRouter

logger = logging.getLogger(__name__)


def _build_router() -> APIRouter:
    """Aggregate every v5 sub-router. Best-effort: log + skip on import errors."""
    agg = APIRouter()

    # ---- Builder B routers (this file's owner) ----------------------------
    builder_b_modules = [
        ("status", "status"),
        ("alerts", "alerts"),
        ("actions", "actions"),
        ("hermes", "hermes"),
        ("regime_config", "regime_config"),
        ("decisions", "decisions"),
        ("mcp", "mcp"),
    ]
    for mod_name, attr in builder_b_modules:
        try:
            mod = __import__(f"{__name__}.{mod_name}", fromlist=[attr])
            agg.include_router(getattr(mod, "router"))
        except Exception as exc:
            logger.warning("v5: failed to mount %s: %s", mod_name, exc)

    # ---- Builder A routers (parallel work — may not have landed yet) ------
    builder_a_modules = ["portfolio", "positions", "metrics", "strategies"]
    for mod_name in builder_a_modules:
        try:
            mod = __import__(f"{__name__}.{mod_name}", fromlist=["router"])
            agg.include_router(getattr(mod, "router"))
        except Exception as exc:
            # Demoted to debug — Builder A's routers may legitimately be
            # absent during parallel development.
            logger.debug("v5: builder-A router %s not mounted: %s", mod_name, exc)

    return agg


v5_router: APIRouter = _build_router()

__all__ = ["v5_router"]
