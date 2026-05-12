"""Shared fixtures for backtest tests.

Provides:

* ``btc_symbol`` / ``eth_symbol`` — sample :class:`Symbol` aliases.
* ``simple_strategy_cls`` — deterministic strategy that buys once and sells
  N bars later, used by every test that doesn't need bespoke logic.
* ``synthetic_source`` — small deterministic candle stream.
"""

from __future__ import annotations

import sys
from collections.abc import Sequence
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

# Make `src/quanta_core/` importable without an editable install.
_REPO_ROOT = Path(__file__).resolve().parents[2]
_SRC_DIR = _REPO_ROOT / "src"
if str(_SRC_DIR) not in sys.path:
    sys.path.insert(0, str(_SRC_DIR))

from quanta_core.backtest.candle_source import SyntheticCandleSource  # noqa: E402
from quanta_core.strategy.base import Strategy  # noqa: E402
from quanta_core.types import (  # noqa: E402
    Bar,
    ClientOrderId,
    OrderProposal,
    Symbol,
    Timeframe,
)


@pytest.fixture
def btc_symbol() -> Symbol:
    """Canonical BTC/USD symbol alias."""
    return Symbol("BTC/USD")


@pytest.fixture
def eth_symbol() -> Symbol:
    """Canonical ETH/USD symbol alias."""
    return Symbol("ETH/USD")


@pytest.fixture
def fixed_start() -> datetime:
    """UTC start timestamp used by every synthetic fixture."""
    return datetime(2026, 1, 1, tzinfo=UTC)


@pytest.fixture
def synthetic_source(btc_symbol: Symbol, fixed_start: datetime) -> SyntheticCandleSource:
    """Deterministic 60-bar 1m candle stream."""
    return SyntheticCandleSource(
        symbol=btc_symbol,
        timeframe="1m",
        start=fixed_start,
        n_bars=60,
        seed=7,
    )


# ---------------------------------------------------------------------------
# Test strategies
# ---------------------------------------------------------------------------


class _DeterministicBuySell(Strategy):
    """Trade once: buy on bar ``buy_at``, sell on bar ``sell_at``.

    Used by tests that need a stable, reproducible trade pattern.
    """

    name = "det_buy_sell"

    def __init__(self, ctx: Any, config: dict[str, Any]) -> None:
        """Wire up bar-index counters."""
        super().__init__(ctx, config)
        self._idx = 0
        self._buy_at = int(config.get("buy_at", 1))
        self._sell_at = int(config.get("sell_at", 5))
        self._qty = Decimal(str(config.get("qty", "1")))

    def on_candle(self, bar: Bar) -> Sequence[OrderProposal]:
        """Emit a buy or sell at fixed indices, no-op otherwise."""
        out: list[OrderProposal] = []
        if self._idx == self._buy_at:
            out.append(
                OrderProposal(
                    symbol=bar.symbol,
                    side="BUY",
                    qty=self._qty,
                    order_type="market",
                    client_order_id=ClientOrderId(f"co-buy-{self._idx}"),
                    rationale="deterministic buy",
                    asset_class="crypto",
                )
            )
        elif self._idx == self._sell_at:
            out.append(
                OrderProposal(
                    symbol=bar.symbol,
                    side="SELL",
                    qty=self._qty,
                    order_type="market",
                    client_order_id=ClientOrderId(f"co-sell-{self._idx}"),
                    rationale="deterministic sell",
                    asset_class="crypto",
                )
            )
        self._idx += 1
        return out


@pytest.fixture
def simple_strategy_cls() -> type[Strategy]:
    """Return the deterministic buy-sell strategy class."""
    return _DeterministicBuySell


class _AlwaysFlat(Strategy):
    """No-op strategy that never proposes."""

    name = "always_flat"

    def on_candle(self, bar: Bar) -> Sequence[OrderProposal]:
        """Return empty sequence."""
        return ()


@pytest.fixture
def flat_strategy_cls() -> type[Strategy]:
    """Return a strategy that never trades — used for edge-case tests."""
    return _AlwaysFlat


# ---------------------------------------------------------------------------
# Bar builder — handy for tests that need a specific OHLCV shape.
# ---------------------------------------------------------------------------


@pytest.fixture
def make_bar() -> Any:
    """Return a function that builds a :class:`Bar` from short kwargs."""

    def _make(
        *,
        symbol: Symbol,
        timeframe: Timeframe = "1m",
        ts: datetime,
        open_: float = 100.0,
        high: float = 101.0,
        low: float = 99.5,
        close: float = 100.5,
        volume: float = 1000.0,
    ) -> Bar:
        return Bar(
            symbol=symbol,
            open=Decimal(str(open_)),
            high=Decimal(str(high)),
            low=Decimal(str(low)),
            close=Decimal(str(close)),
            volume=Decimal(str(volume)),
            timestamp_utc=ts,
            timeframe=timeframe,
        )

    return _make
