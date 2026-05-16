"""Tests for ``user_data.dashboard.legacy_proxy``.

Spec §5.3 CRITICAL constraints under test:

* Mutating routes (``POST /api/ops/pause`` etc.) must NEVER return 410 —
  the circuit-breaker call in ``unified_risk.py:802`` depends on the
  200/202 path working for ≥7 days post-cutover.
* Every legacy response must carry ``Deprecation: true``.
* Known mutating routes must rewrite to their v5 successor (so the
  v5 handler runs) and carry ``Link: <v5>; rel="successor-version"``.
* GET routes pass through unchanged but pick up the deprecation header.
"""
from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from user_data.dashboard.legacy_proxy import LegacyProxyMiddleware


def _build_app() -> FastAPI:
    """A tiny app that mirrors the v4/v5 + legacy endpoints for the middleware."""
    app = FastAPI()

    # Legacy endpoints — what's already in ops_routes today.
    @app.post("/api/ops/pause")
    async def legacy_ops_pause():
        # If the rewrite worked, this handler should NEVER fire.
        return {"who": "legacy_pause"}

    @app.get("/api/ops/portfolio")
    async def legacy_get_portfolio():
        return {"envelope": True, "who": "legacy_portfolio"}

    @app.get("/api/v4/strategies/{kind}")
    async def legacy_v4_strategies(kind: str):
        return {"who": "legacy_v4_strategies", "kind": kind}

    # V5 endpoints — what the rewrite SHOULD land on.
    @app.post("/api/v5/actions/pause/crypto")
    async def v5_pause_crypto():
        return {"who": "v5_pause_crypto"}

    @app.post("/api/v5/actions/kill")
    async def v5_kill():
        return {"who": "v5_kill"}

    app.add_middleware(LegacyProxyMiddleware)
    return app


# ---------------------------------------------------------------------------
# Mutating-route invariants (spec §5.3 CRITICAL)
# ---------------------------------------------------------------------------


def test_post_ops_pause_rewrites_to_v5_never_410():
    """``POST /api/ops/pause`` must reach the v5 handler, NOT 410."""
    app = _build_app()
    client = TestClient(app)

    r = client.post("/api/ops/pause", json={"reason": "unified_risk: test"})

    # CRITICAL: never 410 a mutating route.
    assert r.status_code != 410
    assert r.status_code == 200
    # The rewrite landed on v5.
    assert r.json() == {"who": "v5_pause_crypto"}
    # Deprecation tag attached
    assert r.headers.get("Deprecation") == "true"
    assert "/api/v5/actions/pause/crypto" in r.headers.get("Link", "")


def test_post_ops_kill_rewrites_to_v5_kill():
    app = _build_app()
    client = TestClient(app)
    r = client.post("/api/ops/kill", json={"confirm": True})
    assert r.status_code != 410
    assert r.json() == {"who": "v5_kill"}
    assert r.headers.get("Deprecation") == "true"


def test_unmapped_mutating_legacy_route_still_passes_through_not_410():
    """A POST under /api/ops/* with no successor mapping must still reach the
    in-tree legacy handler — never a blanket 410.

    We register a fresh app with an /api/ops/{something_unmapped} route
    to verify the middleware does not gate it.
    """
    app = FastAPI()

    @app.post("/api/ops/some_uncovered_action")
    async def legacy():
        return {"who": "legacy_uncovered"}

    app.add_middleware(LegacyProxyMiddleware)
    client = TestClient(app)
    r = client.post("/api/ops/some_uncovered_action", json={})
    assert r.status_code != 410
    assert r.json() == {"who": "legacy_uncovered"}
    assert r.headers.get("Deprecation") == "true"


# ---------------------------------------------------------------------------
# GET passthrough
# ---------------------------------------------------------------------------


def test_get_ops_portfolio_passthrough_with_deprecation_header():
    app = _build_app()
    client = TestClient(app)
    r = client.get("/api/ops/portfolio")
    assert r.status_code == 200
    # Envelope preserved
    assert r.json() == {"envelope": True, "who": "legacy_portfolio"}
    # Deprecation tag
    assert r.headers.get("Deprecation") == "true"
    assert "/api/v5/portfolio" in r.headers.get("Link", "")


def test_get_v4_strategies_passthrough_with_successor_link():
    app = _build_app()
    client = TestClient(app)
    r = client.get("/api/v4/strategies/crypto-v4")
    assert r.status_code == 200
    assert r.json() == {"who": "legacy_v4_strategies", "kind": "crypto-v4"}
    assert "/api/v5/strategies" in r.headers.get("Link", "")


# ---------------------------------------------------------------------------
# Non-legacy path passthrough
# ---------------------------------------------------------------------------


def test_non_legacy_get_untouched():
    app = FastAPI()

    @app.get("/api/something_new")
    async def hello():
        return {"hello": "world"}

    app.add_middleware(LegacyProxyMiddleware)
    client = TestClient(app)
    r = client.get("/api/something_new")
    assert r.status_code == 200
    assert "Deprecation" not in r.headers
