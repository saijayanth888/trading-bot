"""Layer-8 boundary test (per doc 11 §3.1).

The contract: **no ``quanta_core.hermes.*`` module imports
``quanta_core.strategy`` or ``quanta_core.execution``.**  Hermes is a
*consumer* of ledger state files only.

This test scans the module sources statically — it doesn't import the
forbidden packages itself, so the assertion fires even if those packages
later become installable.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

HERMES_DIR = Path(__file__).resolve().parents[2] / "src" / "quanta_core" / "hermes"

FORBIDDEN_PREFIXES = (
    "quanta_core.strategy",
    "quanta_core.execution",
)


@pytest.mark.parametrize("module_path", sorted(HERMES_DIR.rglob("*.py")))
def test_no_strategy_or_execution_import(module_path: Path) -> None:
    text = module_path.read_text()
    for prefix in FORBIDDEN_PREFIXES:
        # match `import quanta_core.strategy`, `from quanta_core.strategy ...`
        pat = re.compile(
            rf"^(?:from|import)\s+{re.escape(prefix)}\b",
            re.MULTILINE,
        )
        assert not pat.search(text), (
            f"{module_path.name} violates Layer-8 boundary: imports {prefix}"
        )


def test_modules_expose_run_entrypoint() -> None:
    """Each of the 7 modules must expose ``def run(argv) -> int``."""

    from quanta_core.hermes import (
        briefer,
        gpu_yield_adapter,
        healthcheck,
        lora_promoter,
        post_mortem,
        reflector,
        weekly_publisher,
    )

    for mod in (
        reflector,
        lora_promoter,
        weekly_publisher,
        briefer,
        post_mortem,
        healthcheck,
        gpu_yield_adapter,
    ):
        assert callable(getattr(mod, "run", None)), (
            f"{mod.__name__} is missing a run() entrypoint"
        )
