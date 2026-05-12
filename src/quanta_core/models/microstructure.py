"""Microstructure model — STUB.

Per ``docs/quanta-core-v4-rev2/01-RESEARCH-MULTI_MODEL_RESIDENCY.md``
§6.1, the v4 hot path does NOT include a dedicated microstructure
transformer. This module is a placeholder so the registry has a
named entry to map against if a future strategy depends on it.

TODO(v4-build / post-cutover): replace this stub with the real
implementation. Likely a small (~100 MB) PyTorch model trained on
order-book imbalance features. See ``docs/quanta-core-v4/06-ARCHITECTURE.md``
for the hot-path module map and confirm with the operator before
materialising.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

__all__ = ["MicrostructureModel", "MicrostructurePrediction"]


@dataclass(frozen=True)
class MicrostructurePrediction:
    """Stub return type for :meth:`MicrostructureModel.predict`.

    Attributes
    ----------
    imbalance:
        Order-book imbalance in ``[-1, 1]``. ``-1`` = sell pressure;
        ``+1`` = buy pressure.
    confidence:
        Calibration in ``[0, 1]``. ``0`` for the stub.
    """

    imbalance: float
    confidence: float


class MicrostructureModel:
    """Stub microstructure model — implementation deferred."""

    def predict(self, orderbook: Mapping[str, Any]) -> MicrostructurePrediction:
        """Return a neutral microstructure prediction.

        Parameters
        ----------
        orderbook:
            Order-book snapshot. Expected to contain ``bids`` and
            ``asks`` keys mapping to sequences of ``(price, size)``
            tuples once the real implementation lands. Unused here.

        Returns
        -------
        MicrostructurePrediction
            ``imbalance=0.0``, ``confidence=0.0``.
        """
        _ = orderbook  # explicit unused-arg marker; ruff happy
        return MicrostructurePrediction(imbalance=0.0, confidence=0.0)
