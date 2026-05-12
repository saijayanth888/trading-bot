"""Backtest result data contracts.

A :class:`BacktestResult` is the immutable artefact a :class:`BacktestEngine`
emits at the end of :meth:`BacktestEngine.run`. It carries every closed
trade, every simulated fill, every ``OrderProposal`` the strategy returned,
the equity curve sampled at each bar close, and a small set of summary
metrics (Sharpe, max DD, win rate, total return, etc.).

The structure is JSONL-serialisable so backtest runs survive into the
nightly reflector pipeline and the dashboard's history view.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from quanta_core.types import OrderProposal, Side, Symbol

if TYPE_CHECKING:  # pragma: no cover — typing only
    from collections.abc import Sequence


class _ResultModel(BaseModel):
    """Frozen, strict Pydantic base used by every result model."""

    model_config = ConfigDict(
        frozen=True,
        extra="forbid",
        str_strip_whitespace=True,
        validate_assignment=True,
    )


# ---------------------------------------------------------------------------
# Equity curve point
# ---------------------------------------------------------------------------


class EquityPoint(_ResultModel):
    """A single ``(timestamp, equity)`` sample of the equity curve."""

    timestamp_utc: datetime
    equity: Decimal
    cash: Decimal
    holdings_value: Decimal

    @field_validator("timestamp_utc")
    @classmethod
    def _require_utc(cls, v: datetime) -> datetime:
        """Reject naive timestamps; coerce to UTC."""
        if v.tzinfo is None:
            msg = "timestamp_utc must be timezone-aware (UTC)"
            raise ValueError(msg)
        return v.astimezone(UTC)


# ---------------------------------------------------------------------------
# Simulated fill (lightweight; the canonical Fill carries venue order ids)
# ---------------------------------------------------------------------------


class SimFill(_ResultModel):
    """Backtest-side fill record.

    A trimmed sibling of :class:`quanta_core.types.Fill` for results storage
    — the venue_order_id field would always be ``"sim"`` in backtest, so we
    keep the record lean.
    """

    symbol: Symbol
    side: Side
    qty: Decimal = Field(gt=0)
    price: Decimal = Field(gt=0)
    fee: Decimal = Field(ge=0)
    timestamp_utc: datetime
    client_order_id: str

    @field_validator("timestamp_utc")
    @classmethod
    def _require_utc(cls, v: datetime) -> datetime:
        """Reject naive timestamps; coerce to UTC."""
        if v.tzinfo is None:
            msg = "timestamp_utc must be timezone-aware (UTC)"
            raise ValueError(msg)
        return v.astimezone(UTC)


# ---------------------------------------------------------------------------
# Round-trip trade (entry + exit)
# ---------------------------------------------------------------------------


class TradeRecord(_ResultModel):
    """One round-trip (entry leg + exit leg) on a single symbol."""

    symbol: Symbol
    side: Side
    entry_price: Decimal = Field(gt=0)
    exit_price: Decimal = Field(gt=0)
    qty: Decimal = Field(gt=0)
    entry_ts: datetime
    exit_ts: datetime
    pnl: Decimal
    fee_total: Decimal = Field(ge=0)
    bars_held: int = Field(ge=0)

    @field_validator("entry_ts", "exit_ts")
    @classmethod
    def _require_utc(cls, v: datetime) -> datetime:
        """Reject naive timestamps; coerce to UTC."""
        if v.tzinfo is None:
            msg = "timestamp must be timezone-aware (UTC)"
            raise ValueError(msg)
        return v.astimezone(UTC)


# ---------------------------------------------------------------------------
# Summary metrics
# ---------------------------------------------------------------------------


class SummaryMetrics(_ResultModel):
    """Aggregate performance metrics over one backtest run."""

    n_trades: int = Field(ge=0)
    n_proposals: int = Field(ge=0)
    n_fills: int = Field(ge=0)
    starting_equity: Decimal
    ending_equity: Decimal
    total_return_pct: Decimal
    win_rate: float = Field(ge=0.0, le=1.0)
    sharpe: float
    max_drawdown_pct: float = Field(ge=0.0, le=1.0)
    total_fees: Decimal = Field(ge=0)


# ---------------------------------------------------------------------------
# BacktestResult — the top-level artefact.
# ---------------------------------------------------------------------------


class BacktestResult(_ResultModel):
    """End-of-run artefact carrying every record from a single backtest."""

    strategy_name: str = Field(min_length=1)
    symbol: Symbol
    start: datetime
    end: datetime
    bars_processed: int = Field(ge=0)
    proposals: tuple[OrderProposal, ...]
    fills: tuple[SimFill, ...]
    trades: tuple[TradeRecord, ...]
    equity_curve: tuple[EquityPoint, ...]
    summary: SummaryMetrics

    @field_validator("start", "end")
    @classmethod
    def _require_utc(cls, v: datetime) -> datetime:
        """Reject naive timestamps; coerce to UTC."""
        if v.tzinfo is None:
            msg = "timestamp must be timezone-aware (UTC)"
            raise ValueError(msg)
        return v.astimezone(UTC)

    # ------------------------------------------------------------------
    # Serialisation
    # ------------------------------------------------------------------

    def to_jsonl(self, path: Path) -> None:
        """Write the result as a single-line JSONL record.

        The file format is one JSON object per backtest run. Multiple
        ``to_jsonl`` calls appending to the same path produce a valid JSONL
        history (e.g. one line per walk-forward fold).
        """
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = self.model_dump(mode="json")
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(payload, separators=(",", ":"), default=_json_default))
            fh.write("\n")

    @classmethod
    def from_jsonl(cls, path: Path) -> Sequence[BacktestResult]:
        """Read every JSONL record from ``path`` back into ``BacktestResult``."""
        path = Path(path)
        results: list[BacktestResult] = []
        with path.open(encoding="utf-8") as fh:
            for line in fh:
                line_stripped = line.strip()
                if not line_stripped:
                    continue
                results.append(cls.model_validate_json(line_stripped))
        return tuple(results)

    # ------------------------------------------------------------------
    # Human-readable summary
    # ------------------------------------------------------------------

    def summary_table(self) -> str:
        """Render a fixed-width single-section table for log/CLI output."""
        s = self.summary
        rows: list[tuple[str, str]] = [
            ("strategy", self.strategy_name),
            ("symbol", str(self.symbol)),
            ("window", f"{self.start.isoformat()} → {self.end.isoformat()}"),
            ("bars", f"{self.bars_processed:,}"),
            ("proposals", f"{s.n_proposals:,}"),
            ("fills", f"{s.n_fills:,}"),
            ("trades", f"{s.n_trades:,}"),
            ("starting_equity", f"{s.starting_equity}"),
            ("ending_equity", f"{s.ending_equity}"),
            ("total_return_pct", f"{s.total_return_pct}"),
            ("win_rate", f"{s.win_rate:.4f}"),
            ("sharpe", f"{s.sharpe:.4f}"),
            ("max_drawdown_pct", f"{s.max_drawdown_pct:.4f}"),
            ("total_fees", f"{s.total_fees}"),
        ]
        width = max(len(k) for k, _ in rows)
        lines = [f"{k.ljust(width)} : {v}" for k, v in rows]
        header = f"BacktestResult :: {self.strategy_name} @ {self.symbol}"
        bar = "-" * max(len(header), max(len(line) for line in lines))
        return "\n".join([bar, header, bar, *lines, bar])


# ---------------------------------------------------------------------------
# JSON default — Decimal → str (lossless), datetime → ISO 8601.
# ---------------------------------------------------------------------------


def _json_default(value: Any) -> Any:
    """Fallback ``default`` for :func:`json.dumps`."""
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    msg = f"object of type {type(value).__name__} is not JSON serializable"
    raise TypeError(msg)
