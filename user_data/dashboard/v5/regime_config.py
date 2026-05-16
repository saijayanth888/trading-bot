"""V5 regime-config editor.

Thin wrapper over the canonical ``ops_routes.regime_config_get`` and
``regime_config_post`` handlers. v5 returns raw payloads (no envelope) per
spec §5.1.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Request

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v5/regime_config", tags=["v5", "regime"])


try:
    from ..ops_routes import (  # type: ignore[attr-defined]
        regime_config_get as _legacy_get,
        regime_config_post as _legacy_post,
        require_mcp_key,
    )
except Exception:  # pragma: no cover — direct-host smoke
    _legacy_get = None
    _legacy_post = None
    def require_mcp_key(*_a, **_k) -> None:  # type: ignore[no-redef]
        return None


@router.get("")
def regime_config_get_v5() -> dict:
    """Return the current regime_gating block (raw v5 shape — no envelope)."""
    if _legacy_get is None:
        return {"regime_gating": {}, "schema": {}, "config_path": None}
    legacy = _legacy_get()
    # Legacy returns the envelope shape; unwrap.
    if isinstance(legacy, dict) and "data" in legacy:
        return legacy.get("data") or {}
    return legacy if isinstance(legacy, dict) else {}


@router.post("", dependencies=[Depends(require_mcp_key)])
async def regime_config_post_v5(request: Request) -> dict:
    """Validated atomic write of the new regime_gating block."""
    if _legacy_post is None:
        return {"ok": False, "error": "legacy regime_config handler unavailable"}
    legacy = await _legacy_post(request)
    if isinstance(legacy, dict) and "data" in legacy:
        return legacy.get("data") or {"ok": True}
    return legacy if isinstance(legacy, dict) else {"ok": True}


__all__ = ["router"]
