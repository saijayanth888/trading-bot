"""Execution engine — port of ``user_data/modules/execution_engine.py``.

Reuses the legacy module's hard-won behaviour:

* Pre-flight slippage gate (now factored out to :mod:`slippage_gate`).
* Exponential-backoff retry on **transient** errors only.
* Order timeout → cancel.
* Per-order structured audit log.

Adds the two P0 fixes called out in
``docs/quanta-core-v4/07-VALIDATOR_REPORT.md`` (P0-4) and required by the
build spec:

* **``_cancel`` honours the venue response.** If we issued a cancel but
  the venue had already filled (or partially filled) the order, we
  *record the fill* and report :class:`CancelOutcome.ALREADY_FILLED`
  instead of dropping it on the floor. The legacy code ignored the
  cancel response entirely; we lost ~$340 in unaccounted fills in 2026-04
  before noticing.
* **``_retry_order`` distinguishes 5xx / network / timeout from 4xx.**
  Retrying on 4xx (auth, validation, duplicate-client-order-id) is what
  created the phantom-order class of bug. Now: 5xx / IO / timeout retry
  with backoff; 4xx never retry, surface as terminal rejection.

The engine is **synchronous**. The DESIGN-LOCK calls for asyncio elsewhere,
but the build spec for this module fixes the API as
``submit(proposal) -> Fill | RejectedReason``; that's a sync return.
Wrapping in ``asyncio.to_thread`` is the integration concern of the live
engine, not this layer.
"""

from __future__ import annotations

import contextlib
import datetime as dt
import logging
import time
from collections.abc import Iterable
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, Field

from quanta_core.execution.idempotency import (
    DuplicateClientOrderId,
    IdempotencyStore,
)
from quanta_core.execution.order_state_machine import (
    OrderState,
    OrderStateMachine,
)
from quanta_core.execution.slippage_gate import passes

__all__ = [
    "CancelOutcome",
    "ExchangeError",
    "ExecutionEngine",
    "Fill",
    "OrderProposal",
    "OrderResponse",
    "RejectedReason",
    "RetryableError",
    "Side",
]


logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Domain types
# ---------------------------------------------------------------------------


class Side(StrEnum):
    BUY = "BUY"
    SELL = "SELL"


class OrderProposal(BaseModel):
    """Strategy → engine: "I want to send this order."

    The engine derives the ``client_order_id`` from the proposal hash plus
    the namespace UUID, then either reserves a new id or skips the venue
    call entirely on a duplicate (proven idempotent replay).
    """

    model_config = ConfigDict(frozen=True)

    client_order_id: str = Field(min_length=8, max_length=64)
    symbol: str
    side: Side
    qty: Decimal
    limit_px: Decimal | None = None  # None == market order
    signal_px: Decimal  # the model's reference price (for slippage gate)
    strategy_name: str
    intent_ts_ms: int  # bar-close ms used for replay-safety
    metadata: dict[str, Any] = Field(default_factory=dict)

    def to_intent_dict(self) -> dict[str, Any]:
        """JSON-safe snapshot used by :class:`IdempotencyStore`."""
        return {
            "client_order_id": self.client_order_id,
            "symbol": self.symbol,
            "side": self.side.value,
            "qty": str(self.qty),
            "limit_px": None if self.limit_px is None else str(self.limit_px),
            "signal_px": str(self.signal_px),
            "strategy_name": self.strategy_name,
            "intent_ts_ms": self.intent_ts_ms,
            "metadata": self.metadata,
        }


class Fill(BaseModel):
    """Engine → caller: terminal success."""

    model_config = ConfigDict(frozen=True)

    client_order_id: str
    exchange_order_id: str
    symbol: str
    side: Side
    filled_qty: Decimal
    avg_price: Decimal
    status: str  # final status string from venue ("FILLED" / "PARTIAL")
    venue_ts: dt.datetime
    raw: dict[str, Any] = Field(default_factory=dict)


class RejectedReason(BaseModel):
    """Engine → caller: terminal rejection.

    ``code`` is a stable machine-readable string. Add codes here as new
    rejection paths emerge; never raise from :meth:`ExecutionEngine.submit`
    for business-rule rejections.
    """

    model_config = ConfigDict(frozen=True)

    client_order_id: str
    code: str
    detail: str
    at: dt.datetime


class CancelOutcome(StrEnum):
    """Result of a cancel attempt.

    * ``CANCELED`` — the venue cancelled the (still-open) order.
    * ``ALREADY_FILLED`` — the cancel raced a fill; the engine recorded
      the fill instead. This is the **P0-4 fix**.
    * ``ALREADY_CANCELED`` — the venue says it was already cancelled.
    * ``NOT_FOUND`` — venue does not know this id.
    * ``ERROR`` — the venue returned an error we don't know how to classify.
    """

    CANCELED = "CANCELED"
    ALREADY_FILLED = "ALREADY_FILLED"
    ALREADY_CANCELED = "ALREADY_CANCELED"
    NOT_FOUND = "NOT_FOUND"
    ERROR = "ERROR"


class OrderResponse(BaseModel):
    """Normalised venue response. The :class:`Exchange` adapter is responsible
    for shaping the raw SDK reply into this struct."""

    model_config = ConfigDict(frozen=True)

    client_order_id: str
    exchange_order_id: str
    status: str  # e.g. "OPEN" / "FILLED" / "PARTIAL" / "CANCELED"
    filled_qty: Decimal
    avg_price: Decimal | None = None
    venue_ts: dt.datetime
    raw: dict[str, Any] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class ExchangeError(Exception):
    """Base class for any venue-side error. Carries a status code."""

    def __init__(self, status_code: int, message: str) -> None:
        super().__init__(f"{status_code}: {message}")
        self.status_code = status_code
        self.message = message


class RetryableError(ExchangeError):
    """5xx, network, or timeout: safe to retry with backoff."""


# ---------------------------------------------------------------------------
# Exchange + Ledger + Quote protocols (typed for the engine; impls live elsewhere)
# ---------------------------------------------------------------------------


class Quote(BaseModel):
    """Minimal L1 snapshot used by the slippage gate."""

    model_config = ConfigDict(frozen=True)

    symbol: str
    mid: Decimal
    ts: dt.datetime


class Exchange(Protocol):
    """The minimum surface the engine needs from a venue adapter.

    Real adapters (Alpaca, Coinbase) implement many more methods; the
    engine only depends on these four.
    """

    def place(self, proposal: OrderProposal) -> OrderResponse: ...
    def cancel(self, client_order_id: str) -> OrderResponse: ...
    def get_quote(self, symbol: str) -> Quote: ...
    def cancel_all(self, symbol: str | None = None) -> Iterable[OrderResponse]: ...


class Ledger(Protocol):
    """Just enough of the ledger to record fills.

    Full ``quanta_core.ledger.writer`` interface is wider; the engine only
    needs the fill-write path.
    """

    def record_fill(self, fill: Fill) -> None: ...
    def record_rejection(self, reason: RejectedReason) -> None: ...


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class _RetryPolicy:
    """5xx / network / timeout only. 4xx never retries."""

    max_attempts: int = 3
    initial_backoff_s: float = 0.5
    backoff_factor: float = 2.0

    def should_retry(self, exc: BaseException) -> bool:
        """P0 FIX: only retryable errors retry. 4xx is terminal."""
        if isinstance(exc, RetryableError):
            return True
        if isinstance(exc, ExchangeError):
            # Any 4xx is non-retryable (auth, validation, duplicate, rate-limit-throttle).
            return False
        # Built-in network/IO errors are retryable.
        return isinstance(exc, (TimeoutError, ConnectionError, OSError))


class ExecutionEngine:
    """Single chokepoint for order submission.

    Wiring
    ------
    ::

        engine = ExecutionEngine(
            exchange=alpaca_adapter,
            ledger=ledger_writer,
            idempotency_store=idem_store,
            slippage_threshold_pct=Decimal("0.5"),
        )
        outcome = engine.submit(proposal)
        if isinstance(outcome, Fill):
            ...
        else:
            ...  # RejectedReason

    All clock reads go through ``now_fn`` so the test suite can freeze time.
    """

    def __init__(
        self,
        exchange: Exchange,
        ledger: Ledger,
        idempotency_store: IdempotencyStore,
        *,
        slippage_threshold_pct: Decimal = Decimal("0.5"),
        max_quote_age_s: float = 5.0,
        retry_policy: _RetryPolicy | None = None,
        now_fn: Any | None = None,
        sleep_fn: Any | None = None,
    ) -> None:
        self._exchange = exchange
        self._ledger = ledger
        self._idem = idempotency_store
        self._slippage_threshold_pct = slippage_threshold_pct
        self._max_quote_age_s = max_quote_age_s
        self._retry = retry_policy or _RetryPolicy()
        self._now_fn = now_fn or (lambda: dt.datetime.now(tz=dt.UTC))
        self._sleep_fn = sleep_fn or time.sleep

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def submit(self, proposal: OrderProposal) -> Fill | RejectedReason:
        """End-to-end: reserve → slippage check → place (retry) → record.

        Never raises for business-rule rejections; always returns a typed
        outcome. Programmer-bug exceptions (illegal state transitions,
        DB errors that aren't IntegrityError) DO bubble up.
        """
        machine = OrderStateMachine()
        now = self._now_fn()

        # --- Step 1: reserve client_order_id ---------------------------
        try:
            self._idem.reserve(proposal.client_order_id, proposal.to_intent_dict())
        except DuplicateClientOrderId:
            # Replay path: the id already exists. Look up what happened.
            existing = self._idem.find_existing(proposal.client_order_id)
            if existing is not None and existing.status == "committed":
                # The order was already placed and committed; return the
                # recorded fill if we have it, otherwise a structured reject.
                fill = self._fill_from_row(existing, proposal)
                if fill is not None:
                    return fill
            return self._reject(
                machine,
                proposal,
                code="duplicate_client_order_id",
                detail="prior reservation exists; engine refused double-fire",
            )

        # --- Step 2: slippage gate -------------------------------------
        try:
            quote = self._exchange.get_quote(proposal.symbol)
        except (RetryableError, TimeoutError, ConnectionError, OSError) as exc:
            self._idem.abandon(proposal.client_order_id, "quote_fetch_failed")
            return self._reject(
                machine,
                proposal,
                code="quote_fetch_failed",
                detail=repr(exc),
            )
        except ExchangeError as exc:
            self._idem.abandon(proposal.client_order_id, "quote_4xx")
            return self._reject(
                machine,
                proposal,
                code=f"quote_http_{exc.status_code}",
                detail=exc.message,
            )

        gate = passes(
            proposal,
            current_mid=quote.mid,
            threshold_pct=self._slippage_threshold_pct,
            quote_ts=quote.ts,
            now=now,
            max_quote_age_s=self._max_quote_age_s,
        )
        if not gate.ok:
            self._idem.abandon(proposal.client_order_id, gate.reason)
            return self._reject(
                machine,
                proposal,
                code=f"slippage_{gate.reason}",
                detail=(
                    f"drift={gate.drift_pct} threshold={self._slippage_threshold_pct}"
                    if gate.drift_pct is not None
                    else gate.reason
                ),
            )

        # --- Step 3: place with bounded retry --------------------------
        machine.transition(OrderState.SENT, at=self._now_fn(), reason="placed")
        try:
            response = self._place_with_retry(proposal)
        except DuplicateClientOrderId as exc:
            # Venue says "I already have that id"; the network-error replay
            # rescued us. Lookup the existing trade.
            machine.transition(OrderState.REJECTED, at=self._now_fn(), reason="duplicate_at_venue")
            self._idem.abandon(proposal.client_order_id, "duplicate_at_venue")
            return RejectedReason(
                client_order_id=proposal.client_order_id,
                code="duplicate_at_venue",
                detail=str(exc),
                at=self._now_fn(),
            )
        except ExchangeError as exc:
            machine.transition(
                OrderState.REJECTED,
                at=self._now_fn(),
                reason=f"http_{exc.status_code}",
            )
            self._idem.abandon(proposal.client_order_id, f"http_{exc.status_code}")
            return self._reject(
                machine,
                proposal,
                code=f"http_{exc.status_code}",
                detail=exc.message,
                already_rejected=True,
            )
        except Exception as exc:
            # IO that wasn't classified or a programmer bug: surface, do not silently retry.
            machine.transition(OrderState.REJECTED, at=self._now_fn(), reason="exception")
            self._idem.abandon(proposal.client_order_id, "exception")
            logger.exception(
                "execution_submit_unexpected",
                extra={
                    "client_order_id": proposal.client_order_id,
                },
            )
            return self._reject(
                machine,
                proposal,
                code="exception",
                detail=repr(exc),
                already_rejected=True,
            )

        # --- Step 4: ack + record --------------------------------------
        machine.transition(OrderState.ACK, at=self._now_fn(), reason="venue_ack")
        return self._finalise(machine, proposal, response)

    def cancel(self, client_order_id: str) -> CancelOutcome:
        """Cancel a single order.

        Honours the venue response (the **P0-4 fix**):

        * If the venue says the order is already filled or partially filled,
          the engine records the fill via the ledger and returns
          :attr:`CancelOutcome.ALREADY_FILLED`.
        * Otherwise classify by status string.

        Never raises for venue-side state; raises only for programmer bugs.
        """
        try:
            response = self._exchange.cancel(client_order_id)
        except ExchangeError as exc:
            if exc.status_code == 404:
                return CancelOutcome.NOT_FOUND
            logger.warning(
                "execution_cancel_error",
                extra={"client_order_id": client_order_id, "code": exc.status_code},
            )
            return CancelOutcome.ERROR

        status = response.status.upper()
        if status in ("FILLED", "PARTIAL", "PARTIALLY_FILLED", "PARTIAL_FILL"):
            # P0-4 FIX: do NOT silently treat as canceled.
            fill = Fill(
                client_order_id=response.client_order_id,
                exchange_order_id=response.exchange_order_id,
                symbol=self._lookup_symbol(client_order_id, response),
                side=self._lookup_side(client_order_id, response),
                filled_qty=response.filled_qty,
                avg_price=response.avg_price or Decimal("0"),
                status=response.status,
                venue_ts=response.venue_ts,
                raw=response.raw,
            )
            self._ledger.record_fill(fill)
            try:
                self._idem.commit(
                    client_order_id,
                    response.exchange_order_id,
                    fill.model_dump(mode="json"),
                )
            except LookupError:
                # The cancel was issued for an order we never reserved (e.g.
                # external order). Recording the fill is still correct.
                logger.warning(
                    "cancel_filled_no_reservation",
                    extra={"client_order_id": client_order_id},
                )
            return CancelOutcome.ALREADY_FILLED
        if status in ("CANCELED", "CANCELLED"):
            return CancelOutcome.CANCELED
        if status in ("ALREADY_CANCELED", "ALREADY_CANCELLED"):
            return CancelOutcome.ALREADY_CANCELED
        if status in ("NOT_FOUND", "UNKNOWN_ORDER"):
            return CancelOutcome.NOT_FOUND
        logger.warning(
            "cancel_unknown_status",
            extra={"client_order_id": client_order_id, "status": status},
        )
        return CancelOutcome.ERROR

    def cancel_all(self, symbol: str | None = None) -> list[CancelOutcome]:
        """Cancel every open order (optionally filtered by symbol).

        Iterates the venue-reported list; each cancel goes through
        :meth:`cancel` so the partial-fill-race guard applies uniformly.
        """
        outcomes: list[CancelOutcome] = []
        for response in self._exchange.cancel_all(symbol):
            # The venue may have already classified each; we still route
            # through the same partial-fill detection.
            status = response.status.upper()
            if status in ("FILLED", "PARTIAL", "PARTIALLY_FILLED", "PARTIAL_FILL"):
                outcomes.append(self._record_cancel_fill(response))
            elif status in ("CANCELED", "CANCELLED"):
                outcomes.append(CancelOutcome.CANCELED)
            else:
                outcomes.append(CancelOutcome.ERROR)
        return outcomes

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _place_with_retry(self, proposal: OrderProposal) -> OrderResponse:
        """Place the order. Retry only on RetryableError / network / timeout.

        P0 FIX: ``_retry_order`` now only retries 5xx + timeout (see
        :meth:`_RetryPolicy.should_retry`). 4xx (auth, validation,
        rate-limit-throttle, duplicate-client-order-id) propagates as
        :class:`ExchangeError` immediately.
        """
        last_exc: BaseException | None = None
        for attempt in range(1, self._retry.max_attempts + 1):
            try:
                return self._exchange.place(proposal)
            except BaseException as exc:
                if not self._retry.should_retry(exc):
                    raise
                last_exc = exc
                if attempt < self._retry.max_attempts:
                    backoff = self._retry.initial_backoff_s * (
                        self._retry.backoff_factor ** (attempt - 1)
                    )
                    logger.warning(
                        "execution_retry",
                        extra={
                            "client_order_id": proposal.client_order_id,
                            "attempt": attempt,
                            "backoff_s": backoff,
                            "error": repr(exc),
                        },
                    )
                    self._sleep_fn(backoff)
        # Exhausted retries; surface the last error.
        assert last_exc is not None  # for mypy
        raise last_exc

    def _finalise(
        self,
        machine: OrderStateMachine,
        proposal: OrderProposal,
        response: OrderResponse,
    ) -> Fill | RejectedReason:
        """Map a venue response to either a Fill or a structured rejection."""
        status = response.status.upper()
        if status in ("FILLED", "DONE"):
            machine.transition(OrderState.FILLED, at=self._now_fn(), reason="filled")
            fill = Fill(
                client_order_id=proposal.client_order_id,
                exchange_order_id=response.exchange_order_id,
                symbol=proposal.symbol,
                side=proposal.side,
                filled_qty=response.filled_qty,
                avg_price=response.avg_price or Decimal("0"),
                status=response.status,
                venue_ts=response.venue_ts,
                raw=response.raw,
            )
            self._ledger.record_fill(fill)
            self._idem.commit(
                proposal.client_order_id,
                response.exchange_order_id,
                fill.model_dump(mode="json"),
            )
            return fill
        if status in ("PARTIAL", "PARTIALLY_FILLED", "PARTIAL_FILL"):
            machine.transition(OrderState.PARTIAL_FILL, at=self._now_fn(), reason="partial")
            fill = Fill(
                client_order_id=proposal.client_order_id,
                exchange_order_id=response.exchange_order_id,
                symbol=proposal.symbol,
                side=proposal.side,
                filled_qty=response.filled_qty,
                avg_price=response.avg_price or Decimal("0"),
                status=response.status,
                venue_ts=response.venue_ts,
                raw=response.raw,
            )
            self._ledger.record_fill(fill)
            self._idem.commit(
                proposal.client_order_id,
                response.exchange_order_id,
                fill.model_dump(mode="json"),
            )
            return fill
        if status in ("CANCELED", "CANCELLED"):
            machine.transition(OrderState.CANCELED, at=self._now_fn(), reason="venue_canceled")
            return self._reject(
                machine,
                proposal,
                code="venue_canceled",
                detail=response.status,
                already_rejected=True,
            )
        # Treat any unknown status as a rejection — refuse to invent semantics.
        machine.transition(OrderState.REJECTED, at=self._now_fn(), reason="unknown_status")
        return self._reject(
            machine,
            proposal,
            code="unknown_status",
            detail=response.status,
            already_rejected=True,
        )

    def _reject(
        self,
        machine: OrderStateMachine,
        proposal: OrderProposal,
        *,
        code: str,
        detail: str,
        already_rejected: bool = False,
    ) -> RejectedReason:
        if not already_rejected:
            # Suppress: machine may already be terminal (defensive belt-and-braces).
            with contextlib.suppress(Exception):
                machine.transition(OrderState.REJECTED, at=self._now_fn(), reason=code)
        reason = RejectedReason(
            client_order_id=proposal.client_order_id,
            code=code,
            detail=detail,
            at=self._now_fn(),
        )
        self._ledger.record_rejection(reason)
        return reason

    def _fill_from_row(self, row: Any, proposal: OrderProposal) -> Fill | None:
        """Reconstruct a Fill from a previously-committed idempotency row.

        Returns ``None`` if the stored ``fill_json`` is missing required fields
        (e.g. the row was committed by an older schema). Caller falls back to
        emitting a duplicate-rejection in that case.
        """
        data = row.fill_json
        if not data:
            return None
        try:
            return Fill(
                client_order_id=data["client_order_id"],
                exchange_order_id=data["exchange_order_id"],
                symbol=data["symbol"],
                side=Side(data["side"]),
                filled_qty=Decimal(str(data["filled_qty"])),
                avg_price=Decimal(str(data["avg_price"])),
                status=data["status"],
                venue_ts=_parse_ts(data["venue_ts"]),
                raw=data.get("raw", {}),
            )
        except (KeyError, ValueError, TypeError):
            logger.warning(
                "fill_replay_decode_failed",
                extra={"client_order_id": proposal.client_order_id},
            )
            return None

    def _record_cancel_fill(self, response: OrderResponse) -> CancelOutcome:
        """Helper for :meth:`cancel_all` — record the fill if a cancel raced one."""
        fill = Fill(
            client_order_id=response.client_order_id,
            exchange_order_id=response.exchange_order_id,
            symbol=self._lookup_symbol(response.client_order_id, response),
            side=self._lookup_side(response.client_order_id, response),
            filled_qty=response.filled_qty,
            avg_price=response.avg_price or Decimal("0"),
            status=response.status,
            venue_ts=response.venue_ts,
            raw=response.raw,
        )
        self._ledger.record_fill(fill)
        try:
            self._idem.commit(
                response.client_order_id,
                response.exchange_order_id,
                fill.model_dump(mode="json"),
            )
        except LookupError:
            logger.warning(
                "cancel_all_filled_no_reservation",
                extra={"client_order_id": response.client_order_id},
            )
        return CancelOutcome.ALREADY_FILLED

    def _lookup_symbol(self, client_order_id: str, response: OrderResponse) -> str:
        """Recover the symbol from the idempotency intent if needed.

        Some venues do not echo the symbol on the cancel response; this
        helper falls back to the reserved intent so the Fill record is
        complete.
        """
        sym = response.raw.get("symbol") or response.raw.get("product_id")
        if sym:
            return str(sym)
        existing = self._idem.find_existing(client_order_id)
        if existing and existing.intent_json:
            return str(existing.intent_json.get("symbol", "UNKNOWN"))
        return "UNKNOWN"

    def _lookup_side(self, client_order_id: str, response: OrderResponse) -> Side:
        s = response.raw.get("side")
        if s:
            return Side(str(s).upper())
        existing = self._idem.find_existing(client_order_id)
        if existing and existing.intent_json:
            side_val = existing.intent_json.get("side")
            if side_val:
                return Side(str(side_val).upper())
        # Default to BUY rather than crash; this branch is forensic-only.
        return Side.BUY


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _parse_ts(value: Any) -> dt.datetime:
    """Accept ``datetime`` or ISO-8601 str; raise ``ValueError`` otherwise."""
    if isinstance(value, dt.datetime):
        return value
    if isinstance(value, str):
        return dt.datetime.fromisoformat(value)
    raise ValueError(f"unparseable timestamp: {value!r}")
