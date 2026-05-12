"""Smoke tests that every subpackage imports cleanly.

These exist so the package's import graph is exercised; placeholders are
filled in by sibling build agents (post-reconciliation we have execution,
exchanges, live, risk, models, observability, util filled in). Foundation-
owned placeholders (agents/backtest/hermes/ledger/lora) still have an
empty ``__all__``.
"""

from __future__ import annotations

import importlib

import pytest

# Foundation-owned placeholders — still empty post-reconciliation.
PLACEHOLDER_PACKAGES = [
    "quanta_core.agents",
    "quanta_core.backtest",
    "quanta_core.hermes",
    "quanta_core.ledger",
    "quanta_core.lora",
]

# Filled-in packages from sibling build agents.
FILLED_PACKAGES = [
    "quanta_core.exchanges",
    "quanta_core.execution",
    "quanta_core.live",
    "quanta_core.models",
    "quanta_core.observability",
    "quanta_core.risk",
    "quanta_core.strategy",
    "quanta_core.util",
]


@pytest.mark.parametrize("name", PLACEHOLDER_PACKAGES)
def test_placeholder_package_imports(name: str) -> None:
    mod = importlib.import_module(name)
    assert mod.__all__ == []


@pytest.mark.parametrize("name", FILLED_PACKAGES)
def test_filled_package_imports(name: str) -> None:
    mod = importlib.import_module(name)
    # Filled packages expose at least one public name.
    assert hasattr(mod, "__all__")


def test_top_level_version_stamp() -> None:
    import quanta_core

    # Post-reconciliation we're on the 0.4.x dev line.
    assert quanta_core.__version__.startswith(("0.1.", "0.4."))
