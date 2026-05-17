"""Contract test for shark's adapter-aware role routing.

Pins down the behaviour that makes ModelForge promotions auto-pickup
without code changes:

    1. If the adapter tag exists in Ollama → route picks it
    2. If not → route falls back to the configured base, logs WARN once
    3. The Ollama probe is cached (60s) so repeated calls don't hammer

Without this test the silent-failure mode where a routing entry points
at a tag that doesn't exist would slip through CI (which is exactly
what happened pre-2026-05-17 when trading-bull pointed at vLLM with
no live server).
"""
from __future__ import annotations

import logging
from unittest.mock import patch

import pytest

from shark.llm import client as cli


@pytest.fixture(autouse=True)
def _reset_caches():
    """Each test starts with empty route + probe caches."""
    cli._reset_routing_cache()
    yield
    cli._reset_routing_cache()


def _fake_tags(*names: str):
    """Build a /api/tags-style payload."""
    return {"models": [{"name": n} for n in names]}


# ── adapter-present path ──────────────────────────────────────────────


def test_trading_bull_routes_to_adapter_when_present():
    """When the adapter tag is in Ollama, that's what the router picks."""
    fake_resp = type("R", (), {
        "status_code": 200,
        "json": lambda self: _fake_tags("hermes3:8b", "hermes3:8b-bull-current"),
    })()
    with patch("requests.get", return_value=fake_resp):
        route = cli.resolve_role_route("trading-bull")
    assert route["backend"] == "ollama"
    assert route["model"] == "hermes3:8b-bull-current"


def test_all_six_trading_roles_pick_their_adapter_when_published():
    """All 6 ModelForge-trained roles route to their -current alias when
    Ollama has them. Sanity-check that the routing block is wired uniformly."""
    fake_resp = type("R", (), {
        "status_code": 200,
        "json": lambda self: _fake_tags(
            "hermes3:8b-reflector-current",
            "hermes3:8b-bull-current",
            "hermes3:8b-bear-current",
            "hermes3:8b-arbiter-current",
            "hermes3:8b-regime-tagger-current",
            "hermes3:8b-indicator-selector-current",
        ),
    })()
    with patch("requests.get", return_value=fake_resp):
        for role, expected in [
            ("trading-reflector", "hermes3:8b-reflector-current"),
            ("trading-bull", "hermes3:8b-bull-current"),
            ("trading-bear", "hermes3:8b-bear-current"),
            ("trading-arbiter", "hermes3:8b-arbiter-current"),
            ("trading-regime-tagger", "hermes3:8b-regime-tagger-current"),
            ("trading-indicator-selector", "hermes3:8b-indicator-selector-current"),
        ]:
            cli._reset_routing_cache()
            route = cli.resolve_role_route(role)
            assert route["backend"] == "ollama"
            assert route["model"] == expected, f"{role} routed to {route['model']!r}"


# ── fallback path ─────────────────────────────────────────────────────


def test_trading_bull_falls_back_when_adapter_missing(caplog):
    """When the adapter tag is absent, the router swaps to the configured
    fallback base model. The fallback is logged WARNING exactly once."""
    fake_resp = type("R", (), {
        "status_code": 200,
        "json": lambda self: _fake_tags("hermes3:8b", "hermes3:70b"),
    })()
    with patch("requests.get", return_value=fake_resp):
        with caplog.at_level(logging.WARNING, logger=cli.logger.name):
            route1 = cli.resolve_role_route("trading-bull")
            # Second call within the cache window must NOT re-log.
            route2 = cli.resolve_role_route("trading-bull")
    assert route1["model"] == "hermes3:8b"
    assert route2["model"] == "hermes3:8b"
    fallback_warnings = [r for r in caplog.records if "route fallback" in r.message]
    assert len(fallback_warnings) == 1


def test_regime_tagger_falls_back_to_hermes3_8b_trader_not_plain_base():
    """The regime/indicator-selector roles have a different fallback
    (hermes3:8b-trader) because that's the existing custom-prompt model
    they used pre-ModelForge. The router must honour per-role fallbacks."""
    fake_resp = type("R", (), {
        "status_code": 200,
        "json": lambda self: _fake_tags("hermes3:8b-trader", "hermes3:8b"),
    })()
    with patch("requests.get", return_value=fake_resp):
        route = cli.resolve_role_route("trading-regime-tagger")
    assert route["model"] == "hermes3:8b-trader"


# ── probe-failure path ────────────────────────────────────────────────


def test_ollama_probe_failure_treats_adapter_as_missing(caplog):
    """If /api/tags fails (transport error, non-200, etc.) the router
    must fail-CLOSED — assume the adapter is absent and fall back. The
    inverse would silently route to a tag that doesn't exist and surface
    as an opaque 404 inside the LLM call."""
    with patch("requests.get", side_effect=Exception("boom")):
        with caplog.at_level(logging.WARNING, logger=cli.logger.name):
            route = cli.resolve_role_route("trading-bull")
    assert route["model"] == "hermes3:8b"  # fallback


def test_ollama_probe_non_200_treats_adapter_as_missing():
    fake_resp = type("R", (), {"status_code": 503, "json": lambda self: {}})()
    with patch("requests.get", return_value=fake_resp):
        route = cli.resolve_role_route("trading-bull")
    assert route["model"] == "hermes3:8b"


# ── caching invariants ────────────────────────────────────────────────


def test_probe_is_cached_across_resolve_calls():
    """Resolving 5 trading roles back-to-back must hit /api/tags exactly
    once, not five times. Otherwise every shark cycle hammers Ollama."""
    fake_resp = type("R", (), {
        "status_code": 200,
        "json": lambda self: _fake_tags("hermes3:8b-bull-current"),
    })()
    with patch("requests.get", return_value=fake_resp) as mock_get:
        for role in [
            "trading-reflector", "trading-bull", "trading-bear",
            "trading-arbiter", "trading-regime-tagger",
        ]:
            cli.resolve_role_route(role)
    assert mock_get.call_count == 1


# ── env override still wins (must not be broken by new code path) ────


def test_env_override_short_circuits_probe(monkeypatch):
    """SHARK_ROLE_TRADING_BULL_BACKEND=... must bypass the routing block
    AND the Ollama probe entirely. Operators rely on this for emergency
    swaps without editing JSON."""
    monkeypatch.setenv("SHARK_ROLE_TRADING_BULL_BACKEND", "ollama")
    monkeypatch.setenv("SHARK_ROLE_TRADING_BULL_MODEL", "phi3.5:latest")
    with patch("requests.get", side_effect=AssertionError("probe must not fire")):
        route = cli.resolve_role_route("trading-bull")
    assert route["model"] == "phi3.5:latest"


# ── routing-block hygiene ─────────────────────────────────────────────


def test_routing_block_has_all_six_trading_roles():
    """If the routing JSON loses a role, this test screams. Catches the
    accidental delete or copy-paste reduction during future edits."""
    cli._reset_routing_cache()
    routing = cli._load_routing()
    required = {
        "trading-reflector",
        "trading-bull",
        "trading-bear",
        "trading-arbiter",
        "trading-regime-tagger",
        "trading-indicator-selector",
    }
    assert required.issubset(routing.keys()), \
        f"missing routing entries: {required - routing.keys()}"


def test_every_trading_role_has_a_fallback():
    """Every trading-* routing entry MUST carry a fallback so that an
    unpublished or deleted adapter never takes the agent dark. This is
    the structural invariant that makes the system self-healing."""
    cli._reset_routing_cache()
    routing = cli._load_routing()
    missing = [
        k for k, v in routing.items()
        if k.startswith("trading-") and not v.get("fallback")
    ]
    assert not missing, f"trading roles without fallback: {missing}"
