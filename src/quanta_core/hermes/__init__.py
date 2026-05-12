"""Quanta Core Hermes — Layer 8 scheduler.

This package owns the **scheduler glue** between the V4 trading system and
disk/network state.  Per ``docs/quanta-core-v4-rev2/11-HERMES_CRON_LEARNING.md``
the seven modules in this package never import ``quanta_core.strategy`` or
``quanta_core.execution``; they read state files + the ledger, produce other
state files + Slack notifications, and call out to external services
(Ollama, Postgres, mf-api, exchange APIs).

Each module exposes ``def run(argv: list[str] | None = None) -> int`` so the
invocation contract is ``python -m quanta_core.hermes.<name>``.
"""

from quanta_core.hermes._common import (
    HermesError,
    SlackNotifier,
    StateWriter,
    load_config,
    state_dir,
)

__all__ = [
    "HermesError",
    "SlackNotifier",
    "StateWriter",
    "load_config",
    "state_dir",
]
