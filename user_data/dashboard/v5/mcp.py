"""V5 MCP tool console — scaffold.

Lists the locally-implemented Hermes MCP tools (registered in
``dashboard.mcp_local.TOOLS``) and proxies POST invocations through to
them. Identical behaviour to the legacy ``/api/mcp/{tool_name}``; the v5
shape returns raw JSON (no envelope).

Read-only tools require no auth; mutating tools (``mutating=True`` in
the TOOLS registry) require the shared MCP key.
"""
from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v5/mcp", tags=["v5", "mcp"])

try:
    from .. import mcp_local  # type: ignore[attr-defined]
    from ..ops_routes import require_mcp_key  # type: ignore[attr-defined]
except Exception:  # pragma: no cover — direct-host smoke
    mcp_local = None
    def require_mcp_key(*_a, **_k) -> None:  # type: ignore[no-redef]
        return None


@router.get("/tools")
async def list_tools() -> dict[str, Any]:
    """Enumerate the available MCP tools (name + params + mutating flag)."""
    if mcp_local is None:
        return {"tools": []}
    tools = []
    for name, meta in (mcp_local.TOOLS or {}).items():
        tools.append({
            "name": name,
            "doc": meta.get("doc", ""),
            "params": meta.get("params") or [],
            "mutating": bool(meta.get("mutating", False)),
        })
    tools.sort(key=lambda t: t["name"])
    return {"tools": tools}


@router.post("/{tool_name}", dependencies=[Depends(require_mcp_key)])
async def call_tool(tool_name: str, request: Request) -> Any:
    """Invoke a registered MCP tool.

    Body is the keyword-arg dict for the tool's ``func``. The handler is
    intentionally lenient — unknown kwargs are dropped because the legacy
    surface behaves the same.
    """
    if mcp_local is None:
        raise HTTPException(status_code=503, detail="mcp_local unavailable")
    tool = (mcp_local.TOOLS or {}).get(tool_name)
    if not tool:
        raise HTTPException(status_code=404, detail=f"unknown tool: {tool_name}")
    try:
        body = await request.json() if request.headers.get("content-length") else {}
    except Exception:
        body = {}
    if not isinstance(body, dict):
        body = {}

    func = tool["func"]
    accepted = {p["name"] for p in (tool.get("params") or [])}
    kwargs = {k: v for k, v in body.items() if k in accepted}

    if tool.get("async"):
        result = await func(**kwargs)
    else:
        result = func(**kwargs)
    return result if isinstance(result, (dict, list)) else {"result": result}


__all__ = ["router"]
