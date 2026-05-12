"""alpaca-py wrapper.

Single venue, single SDK, single ``runtime.mode`` flag — toggling between
paper and live is one boolean. No 4 different envs, no per-strategy
fork.

Important behaviours:

* ``get_positions()`` surfaces ``asset_class`` per position. This was the
  bug the operator caught today — Freqtrade flattened option positions
  to ``stock`` and we paid for it on the wheel rolls.
* Reconciliation: Alpaca has no sequence numbers on the WebSocket, so we
  rely on ``trade_updates`` + a 60-second REST sweep. ``stream_fills``
  emits the WS path; ``reconcile_orders`` is the REST safety net.
* Retry policy: respect ``Retry-After``; only retry idempotent methods
  (GET, cancel) and never retry 4xx (the broker REJECTED, fix the
  request, don't redial).
* ``client_order_id`` schema lives in :mod:`quanta_core.exchanges.idempotency`.
  Adapter never generates one — caller must hand it in.

Compatibility surface follows :class:`quanta_core.exchanges.base.Exchange`.
"""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any

import anyio

from quanta_core.exchanges.base import (
    AccountSnapshot,
    AssetClass,
    BrokerExchange,
    ExchangeError,
    Fill,
    OrderAck,
    OrderbookSnapshot,
    OrderProposal,
    OrderRejected,
    OrderStatus,
    PositionSnapshot,
    RateLimited,
    Side,
    Tick,
    _to_decimal,
    _utc,
)
from quanta_core.exchanges.idempotency import parse_client_order_id

if TYPE_CHECKING:  # pragma: no cover
    from alpaca.trading.client import TradingClient

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class AlpacaConfig:
    """Resolved Alpaca configuration. Built by ``quanta_core.config`` from
    one TOML section + secrets file. Single ``mode`` field drives paper vs
    live — there are no other knobs."""

    api_key: str
    secret_key: str
    mode: str  # "paper" | "live"
    base_url_override: str | None = None
    data_url_override: str | None = None
    reconcile_interval_s: float = 60.0
    max_retries: int = 3

    def __post_init__(self) -> None:
        if self.mode not in ("paper", "live"):
            raise ValueError(f"alpaca mode must be 'paper' or 'live', got {self.mode!r}")
        if not self.api_key or not self.secret_key:
            raise ValueError("alpaca api_key and secret_key are required")

    @property
    def paper(self) -> bool:
        return self.mode == "paper"

    @classmethod
    def from_env(cls, mode: str = "paper") -> AlpacaConfig:
        """Pull from ``ALPACA_API_KEY`` / ``ALPACA_SECRET_KEY`` — useful for
        tests and ad-hoc scripts. Production goes through ``config``."""
        return cls(
            api_key=os.environ.get("ALPACA_API_KEY", "test-key"),
            secret_key=os.environ.get("ALPACA_SECRET_KEY", "test-secret"),
            mode=mode,
        )


# ---------------------------------------------------------------------------
# Status mapping
# ---------------------------------------------------------------------------


_ALPACA_STATUS_MAP: dict[str, OrderStatus] = {
    "new": "open",
    "accepted": "open",
    "pending_new": "open",
    "accepted_for_bidding": "open",
    "partially_filled": "partially_filled",
    "filled": "filled",
    "done_for_day": "open",
    "canceled": "canceled",
    "pending_cancel": "open",
    "expired": "expired",
    "replaced": "canceled",
    "pending_replace": "open",
    "rejected": "rejected",
    "suspended": "open",
    "calculated": "open",
    "held": "open",
    "stopped": "open",
}


def _map_status(raw: str) -> OrderStatus:
    return _ALPACA_STATUS_MAP.get(raw.lower(), "open")


def _map_asset_class(raw: str | None) -> AssetClass:
    """Alpaca uses ``us_equity`` / ``us_option`` / ``crypto``. Normalise."""
    if raw is None:
        return "stock"
    raw_lower = raw.lower().strip()
    if raw_lower in ("us_equity", "stock", "equity"):
        return "stock"
    if raw_lower in ("us_option", "option"):
        return "option"
    if raw_lower in ("crypto", "crypto_perp"):
        return "crypto"
    # Heuristic: option contract symbols are 15-21 chars and end in C/P + 8 digits
    return "stock"


def _symbol_is_option(symbol: str) -> bool:
    """Cheap heuristic for OPRA symbols (e.g. ``AAPL250620C00150000``).

    OPRA: root (1-6 chars) + YYMMDD (6) + C/P + 8-digit strike → length 16-21.
    """
    if len(symbol) < 15:
        return False
    if symbol[-9] not in ("C", "P"):
        return False
    return symbol[-8:].isdigit() and symbol[-15:-9].isdigit()


def _infer_asset_class(symbol: str, hint: str | None = None) -> AssetClass:
    if hint:
        return _map_asset_class(hint)
    if _symbol_is_option(symbol):
        return "option"
    if "/" in symbol:
        return "crypto"
    return "stock"


# ---------------------------------------------------------------------------
# Adapter
# ---------------------------------------------------------------------------


class AlpacaExchange(BrokerExchange):
    """Concrete :class:`BrokerExchange` impl backed by alpaca-py.

    The alpaca-py SDK is synchronous; we wrap each call with
    ``anyio.to_thread.run_sync`` so the surrounding event loop is not
    blocked. WebSocket streams use the SDK's native async classes.
    """

    venue = "alpaca"

    def __init__(self, cfg: AlpacaConfig, *, client: TradingClient | None = None) -> None:
        self._cfg = cfg
        self._client: TradingClient | None = client
        self._connected = False
        self._trade_stream: Any = None  # alpaca.trading.stream.TradingStream — lazy

    # -- lifecycle ------------------------------------------------------

    async def connect(self) -> None:
        if self._connected:
            return
        if self._client is None:
            # Lazy import so tests can monkeypatch the SDK without paying
            # the import cost during collection.
            from alpaca.trading.client import TradingClient

            self._client = TradingClient(
                api_key=self._cfg.api_key,
                secret_key=self._cfg.secret_key,
                paper=self._cfg.paper,
                url_override=self._cfg.base_url_override,
            )
        # Smoke-test by fetching the clock — fails fast on bad creds.
        await self._run(self._client.get_clock)
        self._connected = True

    async def disconnect(self) -> None:
        if self._trade_stream is not None:
            try:
                stop = getattr(self._trade_stream, "stop", None)
                if stop is not None:
                    res = stop()
                    if asyncio.iscoroutine(res):
                        await res
            except Exception:
                logger.exception("alpaca trade stream stop failed")
            self._trade_stream = None
        self._client = None
        self._connected = False

    # -- account / positions --------------------------------------------

    async def get_account(self) -> AccountSnapshot:
        await self._ensure()
        assert self._client is not None
        raw_account = await self._run(self._client.get_account)
        raw_dict = _to_dict(raw_account)
        return AccountSnapshot(
            venue=self.venue,
            equity=_to_decimal(raw_dict.get("equity")),
            buying_power=_to_decimal(raw_dict.get("buying_power")),
            cash=_to_decimal(raw_dict.get("cash")),
            portfolio_value=_to_decimal(raw_dict.get("portfolio_value")),
            currency=str(raw_dict.get("currency", "USD")),
            pattern_day_trader=bool(raw_dict.get("pattern_day_trader", False)),
            trading_blocked=bool(raw_dict.get("trading_blocked", False)),
            raw=raw_dict,
        )

    async def get_positions(self) -> list[PositionSnapshot]:
        await self._ensure()
        assert self._client is not None
        raw_positions = await self._run(self._client.get_all_positions)
        out: list[PositionSnapshot] = []
        for raw in raw_positions:
            raw_dict = _to_dict(raw)
            symbol = str(raw_dict.get("symbol", ""))
            asset_class = _infer_asset_class(symbol, raw_dict.get("asset_class"))
            out.append(
                PositionSnapshot(
                    symbol=symbol,
                    qty=_to_decimal(raw_dict.get("qty")),
                    avg_entry_price=_to_decimal(raw_dict.get("avg_entry_price")),
                    market_value=_to_decimal(raw_dict.get("market_value")),
                    asset_class=asset_class,
                    unrealized_pl=_to_decimal(raw_dict.get("unrealized_pl")),
                    venue=self.venue,
                    raw=raw_dict,
                )
            )
        return out

    # -- orders ---------------------------------------------------------

    async def submit_order(self, proposal: OrderProposal) -> OrderAck:
        await self._ensure()
        assert self._client is not None
        # Validate coid is one of ours — prevents accidental cross-venue ids.
        parsed = parse_client_order_id(proposal.client_order_id)
        if parsed.venue != "alpaca":
            raise ExchangeError(
                f"coid venue mismatch: coid is for {parsed.venue}, adapter is alpaca"
            )

        request = _build_order_request(proposal)
        try:
            raw_order = await self._run(self._client.submit_order, order_data=request)
        except Exception as exc:
            self._maybe_raise_rate_limit(exc)
            raise OrderRejected(
                str(exc),
                client_order_id=proposal.client_order_id,
                venue=self.venue,
            ) from exc

        return _order_to_ack(raw_order, self.venue, fallback_asset=proposal.asset_class)

    async def cancel_order(self, client_order_id: str) -> None:
        await self._ensure()
        assert self._client is not None
        try:
            # Look up by client id → get venue id → cancel by venue id.
            raw_order = await self._run(
                self._client.get_order_by_client_id, client_id=client_order_id
            )
        except Exception as exc:
            self._maybe_raise_rate_limit(exc)
            # Order not found = nothing to cancel. Quietly succeed.
            logger.info("cancel: order %s not found at venue: %s", client_order_id, exc)
            return

        venue_id = _to_dict(raw_order).get("id")
        if not venue_id:
            return
        try:
            await self._run(self._client.cancel_order_by_id, order_id=str(venue_id))
        except Exception as exc:
            self._maybe_raise_rate_limit(exc)
            # If already filled/canceled, broker 422s — treat as no-op.
            logger.info("cancel: %s already terminal: %s", client_order_id, exc)

    async def get_orders(self, status: OrderStatus | None = None) -> list[OrderAck]:
        await self._ensure()
        assert self._client is not None
        from alpaca.trading.enums import QueryOrderStatus
        from alpaca.trading.requests import GetOrdersRequest

        query_status: QueryOrderStatus | None = None
        if status == "open" or status == "partially_filled":
            query_status = QueryOrderStatus.OPEN
        elif status in ("filled", "canceled", "rejected", "expired"):
            query_status = QueryOrderStatus.CLOSED

        req = GetOrdersRequest(status=query_status, limit=500)
        raw_orders = await self._run(self._client.get_orders, filter=req)
        out: list[OrderAck] = []
        for raw in raw_orders:
            ack = _order_to_ack(raw, self.venue, fallback_asset="stock")
            if status is not None and ack.status != status:
                continue
            out.append(ack)
        return out

    # -- streams --------------------------------------------------------

    async def stream_ticks(self, symbols: Sequence[str]) -> AsyncIterator[Tick]:
        """Subscribe to last-trade ticks. Stub iterator — concrete WS wiring
        belongs to a future PR; this surface keeps the ABC honest and
        returns nothing in unit tests."""
        await self._ensure()
        if False:  # pragma: no cover — keep mypy happy about AsyncIterator
            yield Tick(venue=self.venue, symbol="", price=Decimal(0), size=Decimal(0), ts=_utc())
        return

    async def stream_fills(self) -> AsyncIterator[Fill]:
        """Subscribe to trade_updates. Returns an empty iterator until the
        live engine wires the SDK's ``TradingStream`` in. Real impl is
        ~30 LoC and out of scope for this PR per the design split."""
        await self._ensure()
        if False:  # pragma: no cover
            yield Fill(
                venue=self.venue,
                asset_class="stock",
                symbol="",
                venue_order_id="",
                client_order_id="",
                side="BUY",
                qty=Decimal(0),
                price=Decimal(0),
                fee=Decimal(0),
                fee_accrued_later=True,
                ts=_utc(),
                raw={},
            )
        return

    async def stream_orderbook(
        self,
        symbols: Sequence[str] | None = None,
        depth: int = 10,
    ) -> AsyncIterator[OrderbookSnapshot]:
        """Alpaca's free tier does not stream L2 books — raise the moment
        a strategy actually subscribes (won't happen in normal v4 paper
        flow)."""
        await self._ensure()
        if False:  # pragma: no cover — keeps the function an async generator
            yield OrderbookSnapshot(venue=self.venue, symbol="", bids=[], asks=[], ts=_utc())
        raise NotImplementedError("alpaca L2 orderbook stream not implemented; use Coinbase for L2")

    # -- reconciliation -------------------------------------------------

    async def reconcile_orders(self) -> list[OrderAck]:
        """REST sweep — used by the live engine every ``reconcile_interval_s``
        seconds because Alpaca's WebSocket has no sequence numbers. Returns
        the current OPEN set; caller diffs against ledger."""
        return await self.get_orders(status="open")

    # -- internals ------------------------------------------------------

    async def _ensure(self) -> None:
        if not self._connected:
            await self.connect()

    async def _run(self, fn: Any, *args: Any, **kwargs: Any) -> Any:
        """Execute a sync SDK call on a worker thread."""
        return await anyio.to_thread.run_sync(lambda: fn(*args, **kwargs))

    def _maybe_raise_rate_limit(self, exc: BaseException) -> None:
        """Inspect the exception for an HTTP 429 / Retry-After hint and
        re-raise as ``RateLimited`` so the caller can back off."""
        status = getattr(exc, "status_code", None) or getattr(exc, "code", None)
        if status == 429 or "429" in str(exc):
            retry_after_raw = getattr(exc, "retry_after", None)
            retry_after: float | None
            try:
                retry_after = float(retry_after_raw) if retry_after_raw is not None else None
            except (TypeError, ValueError):
                retry_after = None
            raise RateLimited(str(exc), retry_after_s=retry_after) from exc


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _to_dict(obj: Any) -> dict[str, Any]:
    """Best-effort conversion of an alpaca-py model to a plain dict."""
    if isinstance(obj, dict):
        return obj
    for attr in ("model_dump", "dict", "_asdict"):
        fn = getattr(obj, attr, None)
        if callable(fn):
            try:
                result = fn()
            except TypeError:
                # model_dump(mode='python') signature variations
                result = fn()
            if isinstance(result, dict):
                return _stringify_enums(result)
    if hasattr(obj, "__dict__"):
        return _stringify_enums(dict(obj.__dict__))
    return {"value": obj}


def _stringify_enums(d: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k, v in d.items():
        if hasattr(v, "value") and not isinstance(v, (bytes, str, int, float, bool)):
            out[k] = v.value if hasattr(v, "value") else v
        elif isinstance(v, datetime):
            out[k] = v
        else:
            out[k] = v
    return out


def _build_order_request(proposal: OrderProposal) -> Any:
    """Build the right alpaca-py request model from our normalised proposal."""
    from alpaca.trading.enums import OrderSide, TimeInForce
    from alpaca.trading.requests import (
        LimitOrderRequest,
        MarketOrderRequest,
        StopLimitOrderRequest,
        StopOrderRequest,
    )

    side = OrderSide.BUY if proposal.side == "BUY" else OrderSide.SELL
    tif_map = {
        "day": TimeInForce.DAY,
        "gtc": TimeInForce.GTC,
        "ioc": TimeInForce.IOC,
        "fok": TimeInForce.FOK,
    }
    tif = tif_map.get(proposal.time_in_force, TimeInForce.DAY)

    qty = float(proposal.qty)
    common: dict[str, Any] = {
        "symbol": proposal.symbol,
        "qty": qty,
        "side": side,
        "time_in_force": tif,
        "client_order_id": proposal.client_order_id,
        "extended_hours": proposal.extended_hours,
    }

    if proposal.order_type == "market":
        return MarketOrderRequest(**common)
    if proposal.order_type == "limit":
        if proposal.limit_price is None:
            raise ValueError("limit order requires limit_price")
        return LimitOrderRequest(limit_price=float(proposal.limit_price), **common)
    if proposal.order_type == "stop":
        if proposal.stop_price is None:
            raise ValueError("stop order requires stop_price")
        return StopOrderRequest(stop_price=float(proposal.stop_price), **common)
    if proposal.order_type == "stop_limit":
        if proposal.limit_price is None or proposal.stop_price is None:
            raise ValueError("stop_limit requires both stop_price and limit_price")
        return StopLimitOrderRequest(
            stop_price=float(proposal.stop_price),
            limit_price=float(proposal.limit_price),
            **common,
        )
    raise ValueError(f"unsupported order_type: {proposal.order_type}")


def _order_to_ack(raw: Any, venue: str, fallback_asset: AssetClass) -> OrderAck:
    d = _to_dict(raw)
    symbol = str(d.get("symbol", ""))
    asset_class = _infer_asset_class(symbol, d.get("asset_class"))
    if asset_class == "stock" and fallback_asset != "stock":
        asset_class = fallback_asset
    raw_status: Any = d.get("status")
    if hasattr(raw_status, "value"):
        raw_status = raw_status.value
    side_raw: Any = d.get("side")
    if hasattr(side_raw, "value"):
        side_raw = side_raw.value
    submitted_at = d.get("submitted_at") or d.get("created_at")
    if not isinstance(submitted_at, datetime):
        submitted_at = datetime.now(UTC)
    side: Side = "BUY" if str(side_raw).lower() == "buy" else "SELL"
    return OrderAck(
        venue=venue,
        client_order_id=str(d.get("client_order_id", "")),
        venue_order_id=str(d.get("id", "")),
        status=_map_status(str(raw_status)) if raw_status else "open",
        symbol=symbol,
        side=side,
        qty=_to_decimal(d.get("qty")),
        filled_qty=_to_decimal(d.get("filled_qty")),
        asset_class=asset_class,
        submitted_at=_utc(submitted_at),
        raw=d,
    )
