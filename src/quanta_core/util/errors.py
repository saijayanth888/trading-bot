"""Exception hierarchy used across quanta_core.

All raised exceptions in the live module subclass ``QuantaError`` so callers
can ``except QuantaError`` without catching unrelated ``Exception``s.
"""

from __future__ import annotations


class QuantaError(Exception):
    """Base class for every quanta_core-raised exception."""


class StaleFeedError(QuantaError):
    """Raised when the heartbeat watchdog observes no events past its budget."""


class LateTickError(QuantaError):
    """Raised internally when a tick is older than the current open bar."""


class ReconciliationDriftError(QuantaError):
    """Raised when the reconciler detects an unrecoverable position gap."""


__all__ = [
    "LateTickError",
    "QuantaError",
    "ReconciliationDriftError",
    "StaleFeedError",
]
