"""Cross-module data contracts for Quanta Core V4.

Five concrete Pydantic v2 models (``Bar``, ``Tick``, ``Fill``, ``Position``,
``OrderProposal``) plus the ``Context`` protocol form the load-bearing type
surface every layer composes on. They are strict, frozen, UTC-only, and
extras-forbidden so a typo at a module boundary fails immediately.

These types intentionally mirror the foundation branch
(``feat/v4-build-foundation`` at ``quanta_core/src/quanta_core/types.py``)
so the morning merge is a straight rename of the package path — no schema
drift between the wave-1 foundation and this wave-2 backtest module.

References
----------
* ``docs/quanta-core-v4/06-ARCHITECTURE.md`` §3, §5 — module API spec
* ``docs/quanta-core-v4-rev2/DESIGN-LOCK.md`` §2 — ownership rules
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Literal, NewType, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

if TYPE_CHECKING:  # pragma: no cover — typing only
    from collections.abc import Sequence


# ---------------------------------------------------------------------------
# Aliases
# ---------------------------------------------------------------------------

Symbol = NewType("Symbol", str)
Venue = Literal["alpaca", "coinbase", "paper"]
Side = Literal["BUY", "SELL"]
Timeframe = Literal["1m", "5m", "15m", "1h", "4h", "1d"]
OrderType = Literal["market", "limit", "stop", "stop_limit"]
TimeInForce = Literal["day", "gtc", "ioc", "fok"]
AssetClass = Literal["equity", "crypto", "option", "etf"]
ClientOrderId = NewType("ClientOrderId", str)


# ---------------------------------------------------------------------------
# Base model: strict, frozen, validates on assignment.
# ---------------------------------------------------------------------------


class _QuantaModel(BaseModel):
    """Strict, frozen Pydantic base used by every quanta-core event model."""

    model_config = ConfigDict(
        frozen=True,
        extra="forbid",
        str_strip_whitespace=True,
        validate_assignment=True,
        arbitrary_types_allowed=False,
    )


# ---------------------------------------------------------------------------
# Bar
# ---------------------------------------------------------------------------


class Bar(_QuantaModel):
    """One closed OHLCV candle for a single (symbol, timeframe)."""

    symbol: Symbol
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal = Field(ge=0)
    timestamp_utc: datetime
    timeframe: Timeframe

    @field_validator("timestamp_utc")
    @classmethod
    def _require_utc(cls, v: datetime) -> datetime:
        """Reject naive timestamps; coerce to UTC."""
        if v.tzinfo is None:
            msg = "timestamp_utc must be timezone-aware (UTC)"
            raise ValueError(msg)
        return v.astimezone(UTC)

    @model_validator(mode="after")
    def _ohlc_consistency(self) -> Bar:
        """Reject bars where high < low or open/close fall outside [low, high]."""
        if self.high < self.low:
            msg = f"high ({self.high}) < low ({self.low})"
            raise ValueError(msg)
        if not (self.low <= self.open <= self.high):
            msg = f"open ({self.open}) outside [low={self.low}, high={self.high}]"
            raise ValueError(msg)
        if not (self.low <= self.close <= self.high):
            msg = f"close ({self.close}) outside [low={self.low}, high={self.high}]"
            raise ValueError(msg)
        return self


# ---------------------------------------------------------------------------
# Tick
# ---------------------------------------------------------------------------


class Tick(_QuantaModel):
    """One trade print (or normalised trade event) for a single symbol."""

    symbol: Symbol
    price: Decimal = Field(gt=0)
    size: Decimal = Field(gt=0)
    timestamp_utc: datetime
    side: Side | None = None

    @field_validator("timestamp_utc")
    @classmethod
    def _require_utc(cls, v: datetime) -> datetime:
        """Reject naive timestamps; coerce to UTC."""
        if v.tzinfo is None:
            msg = "timestamp_utc must be timezone-aware (UTC)"
            raise ValueError(msg)
        return v.astimezone(UTC)


# ---------------------------------------------------------------------------
# Fill
# ---------------------------------------------------------------------------


class Fill(_QuantaModel):
    """Confirmed venue-side execution of a (partial) order."""

    order_id: str = Field(min_length=1)
    client_order_id: ClientOrderId
    symbol: Symbol
    side: Side
    qty: Decimal = Field(gt=0)
    price: Decimal = Field(gt=0)
    fee: Decimal = Field(ge=0)
    timestamp_utc: datetime
    venue: Venue

    @field_validator("timestamp_utc")
    @classmethod
    def _require_utc(cls, v: datetime) -> datetime:
        """Reject naive timestamps; coerce to UTC."""
        if v.tzinfo is None:
            msg = "timestamp_utc must be timezone-aware (UTC)"
            raise ValueError(msg)
        return v.astimezone(UTC)


# ---------------------------------------------------------------------------
# Position
# ---------------------------------------------------------------------------


class Position(_QuantaModel):
    """Net open exposure for one symbol within one subsystem."""

    symbol: Symbol
    qty: Decimal
    avg_entry: Decimal = Field(gt=0)
    mark: Decimal = Field(gt=0)
    unrealized_pnl: Decimal
    side: Side
    asset_class: AssetClass
    opened_at: datetime
    subsystem_tag: str = Field(min_length=1, max_length=64)

    @field_validator("opened_at")
    @classmethod
    def _require_utc(cls, v: datetime) -> datetime:
        """Reject naive timestamps; coerce to UTC."""
        if v.tzinfo is None:
            msg = "opened_at must be timezone-aware (UTC)"
            raise ValueError(msg)
        return v.astimezone(UTC)

    @model_validator(mode="after")
    def _qty_sign_matches_side(self) -> Position:
        """Long positions hold qty > 0; shorts hold qty < 0."""
        if self.side == "BUY" and self.qty <= 0:
            msg = f"BUY position must have qty > 0, got {self.qty}"
            raise ValueError(msg)
        if self.side == "SELL" and self.qty >= 0:
            msg = f"SELL position must have qty < 0, got {self.qty}"
            raise ValueError(msg)
        return self


# ---------------------------------------------------------------------------
# OrderProposal
# ---------------------------------------------------------------------------


class OrderProposal(_QuantaModel):
    """Strategy-emitted order intent, prior to risk + execution gating."""

    symbol: Symbol
    side: Side
    qty: Decimal = Field(gt=0)
    order_type: OrderType
    limit_px: Decimal | None = None
    stop_px: Decimal | None = None
    tif: TimeInForce = "day"
    client_order_id: ClientOrderId
    rationale: str = Field(min_length=1, max_length=2048)
    asset_class: AssetClass

    @model_validator(mode="after")
    def _price_required_for_type(self) -> OrderProposal:
        """Enforce limit/stop price presence per order_type."""
        if self.order_type in {"limit", "stop_limit"} and self.limit_px is None:
            msg = f"limit_px required for order_type={self.order_type}"
            raise ValueError(msg)
        if self.order_type in {"stop", "stop_limit"} and self.stop_px is None:
            msg = f"stop_px required for order_type={self.order_type}"
            raise ValueError(msg)
        if self.order_type == "market" and (self.limit_px is not None or self.stop_px is not None):
            msg = "market orders must not carry limit_px or stop_px"
            raise ValueError(msg)
        return self


# ---------------------------------------------------------------------------
# Context — runtime protocol provided by live/backtest engines.
# ---------------------------------------------------------------------------


@runtime_checkable
class Context(Protocol):
    """Runtime services exposed to a strategy during a hook call.

    Implementations live in :mod:`quanta_core.live.engine` (wall clock + venue)
    and :mod:`quanta_core.backtest.engine` (bar clock + paper venue). Strategies
    only ever see this protocol — they never reach into adapters or the
    ledger directly.
    """

    def now(self) -> datetime:
        """Return the current UTC time (wall clock live, bar clock backtest)."""
        ...

    def get_position(self, symbol: Symbol) -> Position | None:
        """Return current net position for ``symbol`` or ``None`` if flat."""
        ...

    def get_history(
        self,
        symbol: Symbol,
        timeframe: Timeframe,
        n: int,
    ) -> Sequence[Bar]:
        """Return the last ``n`` closed bars for ``(symbol, timeframe)``."""
        ...

    def submit_proposal(self, proposal: OrderProposal) -> None:
        """Queue an :class:`OrderProposal` for risk + execution gating."""
        ...

    def log_decision(self, decision: dict[str, Any]) -> None:
        """Persist a freeform decision payload for the nightly reflector."""
        ...
