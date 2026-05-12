"""Exchange adapter package.

The concrete Alpaca / Coinbase / Paper adapters are built by a sibling
agent. This package exposes only the abstract base class so the live module
can depend on the interface without coupling to a specific SDK.
"""

from __future__ import annotations

from quanta_core.exchanges.base import (
    Exchange,
    ExchangeStream,
    StreamEvent,
)

__all__ = ["Exchange", "ExchangeStream", "StreamEvent"]
