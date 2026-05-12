"""Pydantic v2 data contracts for every cross-module event in the stack.

The five concrete models (``Bar``, ``Tick``, ``Fill``, ``Position``,
``OrderProposal``) plus the runtime ``Context`` protocol form the load-bearing
type surface that the rest of quanta-core composes on top of. Every wire-level
event entering or leaving the engine round-trips through one of these models;
adding a field here ripples to every consumer, so changes must land via a
typed migration step in the ledger.

References
----------
* ``docs/quanta-core-v4/06-ARCHITECTURE.md`` §3 — module API spec
* ``docs/quanta-core-v4-rev2/DESIGN-LOCK.md`` §2 — three-way ownership
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal  # noqa: TC003 — runtime needed for Pydantic v2 model resolution
from typing import TYPE_CHECKING, Any, Literal, NewType, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

if TYPE_CHECKING:  # pragma: no cover — typing only
    from collections.abc import Sequence

# ---------------------------------------------------------------------------
# Type aliases (kept tiny so they're cheap to import everywhere)
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
# Shared base — strict, immutable, validate-on-assignment.
# ---------------------------------------------------------------------------


class _QuantaModel(BaseModel):
    """Strict, immutable Pydantic base used by every quanta-core event model."""

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
    """One closed OHLCV candle for a single symbol/timeframe."""

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
        if v.tzinfo is None:
            msg = "timestamp_utc must be timezone-aware (UTC)"
            raise ValueError(msg)
        return v.astimezone(UTC)

    @model_validator(mode="after")
    def _ohlc_consistency(self) -> Bar:
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
        if v.tzinfo is None:
            msg = "timestamp_utc must be timezone-aware (UTC)"
            raise ValueError(msg)
        return v.astimezone(UTC)


# ---------------------------------------------------------------------------
# Fill
# ---------------------------------------------------------------------------


class Fill(_QuantaModel):
    """Confirmed venue-side execution of a (partial) order."""

    order_id: str
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
        if v.tzinfo is None:
            msg = "opened_at must be timezone-aware (UTC)"
            raise ValueError(msg)
        return v.astimezone(UTC)

    @model_validator(mode="after")
    def _qty_sign_matches_side(self) -> Position:
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

    Implementations live in ``quanta_core.live.engine`` (wall clock + venue)
    and ``quanta_core.backtest.engine`` (bar clock + paper venue). Strategies
    only ever see this protocol — they never reach into adapters or the
    ledger directly. The narrow surface is the type-checked enforcement of
    the §2 ownership rule "Strategy never imports exchanges or ledger".
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
        """Return the last ``n`` closed bars for ``(symbol, timeframe)``.

        Returned in chronological order (oldest first). Caller MUST NOT
        mutate the returned sequence; engines may return a shared view.
        """
        ...

    def submit_proposal(self, proposal: OrderProposal) -> None:
        """Hand an ``OrderProposal`` to the framework for risk + execution gating.

        The proposal is queued; gates may reject it asynchronously. Strategies
        learn the outcome via ``on_fill`` (success) or via the structured log
        stream (rejection).
        """
        ...

    def log_decision(self, decision: dict[str, Any]) -> None:
        """Persist a freeform decision payload to the ledger for the reflector.

        Used by the nightly reflector to build the ``decisions.md`` corpus that
        feeds ModelForge. ``decision`` must be JSON-serialisable.
        """
        ...
