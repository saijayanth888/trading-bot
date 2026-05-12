"""Quanta Core V4 — risk module.

Four submodules:

* :mod:`quanta_core.risk.governor` — pre-trade hard gates + Kelly sizing.
* :mod:`quanta_core.risk.monte_carlo` — real-time VaR/ES gate via CuPy
  + PyTorch CUDA Graphs (Heston-Bates jump-diffusion).
* :mod:`quanta_core.risk.ownership` — per-subsystem symbol ownership
  ledger (Shark vs Wheel).
* :mod:`quanta_core.risk.asset_class_gate` — pure-function ``is_quanta_managed``
  decision for cross-subsystem position isolation.

Everything in the top-level ``__all__`` is the stable public surface; any
name not listed here is an implementation detail.
"""

from __future__ import annotations

from quanta_core.risk.asset_class_gate import (
    Position,
    is_quanta_managed,
)
from quanta_core.risk.governor import (
    RiskConfig,
    RiskDecision,
    RiskGovernor,
    TradeRecord,
)
from quanta_core.risk.monte_carlo import (
    CALIBRATION_MAX_AGE_S,
    Calibration,
    MCDecision,
    MonteCarloEngine,
    MonteCarloError,
)
from quanta_core.risk.ownership import (
    SCHEMA_VERSION,
    Subsystem,
    claim,
    load_owned,
    owns,
    release,
    save_owned,
)

__all__ = [
    "CALIBRATION_MAX_AGE_S",
    "SCHEMA_VERSION",
    "Calibration",
    "MCDecision",
    "MonteCarloEngine",
    "MonteCarloError",
    "Position",
    "RiskConfig",
    "RiskDecision",
    "RiskGovernor",
    "Subsystem",
    "TradeRecord",
    "claim",
    "is_quanta_managed",
    "load_owned",
    "owns",
    "release",
    "save_owned",
]
