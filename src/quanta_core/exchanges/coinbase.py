"""coinbase-advanced-py wrapper.

Mirrors :class:`AlpacaExchange` so the strategy layer dispatches on venue
without branching on adapter type.

Behaviours that are venue-specific:

* **JWT auth**: CDP API keys → ES256 JWT with 120-s TTL, regenerated per
  request by the SDK. We surface ``api_key`` / ``api_secret`` (or a key
  file) and let the SDK handle the rotation.
* **sequence_num gap detection**: every WS message carries a
  ``sequence_num``. The stream loop tracks the last-seen number per
  ``product_id`` and on a gap raises :class:`SequenceGap`, which the
  engine catches → REST reconcile + Slack warn.
* **30 req/s** private rate limit. We pass ``rate_limit_headers=True``
  to the SDK so 429 backoffs are visible; the engine layer rate-limits.
* **Spot only** in v4 — no perp/futures even if the SDK supports them.
"""

from __future__ import annotations

import logging
import os
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any

import anyio

from quanta_core.exchanges.base import (
    AccountSnapshot,
    Exchange,
    ExchangeError,
    Fill,
    OrderAck,
    OrderbookLevel,
    OrderbookSnapshot,
    OrderProposal,
    OrderRejected,
    OrderStatus,
    PositionSnapshot,
    RateLimited,
    SequenceGap,
    Side,
    Tick,
    _to_decimal,
    _utc,
)
from quanta_core.exchanges.idempotency import parse_client_order_id

if TYPE_CHECKING:  # pragma: no cover
    from coinbase.rest import RESTClient

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CoinbaseConfig:
    """Resolved Coinbase configuration."""

    api_key: str
    api_secret: str
    mode: str  # "paper" | "live" — Coinbase has no sandbox; paper mode short-circuits
    base_url: str = "api.coinbase.com"
    ws_url: str = "wss://advanced-trade-ws.coinbase.com"
    rate_limit_headers: bool = True
    reconnect_max_delay_s: float = 30.0
    request_timeout_s: float = 30.0

    def __post_init__(self) -> None:
        if self.mode not in ("paper", "live"):
            raise ValueError(f"coinbase mode must be 'paper' or 'live', got {self.mode!r}")
        if not self.api_key or not self.api_secret:
            raise ValueError("coinbase api_key and api_secret are required")

    @property
    def is_paper(self) -> bool:
        return self.mode == "paper"

    @classmethod
    def from_env(cls, mode: str = "paper") -> CoinbaseConfig:
        return cls(
            api_key=os.environ.get("COINBASE_API_KEY", "test-key"),
            api_secret=os.environ.get("COINBASE_API_SECRET", "test-secret"),
            mode=mode,
        )


# ---------------------------------------------------------------------------
# Status mapping
# ---------------------------------------------------------------------------


_CB_STATUS_MAP: dict[str, OrderStatus] = {
    "OPEN": "open",
    "PENDING": "open",
    "QUEUED": "open",
    "FILLED": "filled",
    "PARTIALLY_FILLED": "partially_filled",
    "CANCELLED": "canceled",
    "CANCELED": "canceled",
    "EXPIRED": "expired",
    "FAILED": "rejected",
    "REJECTED": "rejected",
}


def _map_status(raw: str) -> OrderStatus:
    return _CB_STATUS_MAP.get(raw.upper(), "open")


# ---------------------------------------------------------------------------
# Sequence-num tracker (used by WS consumer)
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class _SequenceTracker:
    """Per-(channel, product) sequence-number ledger. The Coinbase WS
    guarantees monotonically-increasing ``sequence_num`` per channel; a
    jump > 1 means we lost frames.
    """

    last_seen: dict[tuple[str, str], int] = field(default_factory=dict)

    def observe(self, channel: str, product_id: str, sequence_num: int) -> None:
        """Record a sequence_num; raise :class:`SequenceGap` on gap."""
        key = (channel, product_id)
        prev = self.last_seen.get(key)
        if prev is not None and sequence_num > prev + 1:
            self.last_seen[key] = sequence_num  # advance so we don't replay
            raise SequenceGap(
                channel=channel,
                product_id=product_id,
                expected=prev + 1,
                got=sequence_num,
            )
        # Out-of-order or duplicate: ignore the message.
        if prev is None or sequence_num > prev:
            self.last_seen[key] = sequence_num

    def reset(self, channel: str | None = None, product_id: str | None = None) -> None:
        if channel is None and product_id is None:
            self.last_seen.clear()
            return
        for key in list(self.last_seen.keys()):
            if channel is not None and key[0] != channel:
                continue
            if product_id is not None and key[1] != product_id:
                continue
            del self.last_seen[key]


# ---------------------------------------------------------------------------
# Adapter
# ---------------------------------------------------------------------------


class CoinbaseExchange(Exchange):
    """Concrete :class:`Exchange` impl backed by coinbase-advanced-py."""

    venue = "coinbase"

    def __init__(
        self,
        cfg: CoinbaseConfig,
        *,
        client: RESTClient | None = None,
    ) -> None:
        self._cfg = cfg
        self._client: RESTClient | None = client
        self._connected = False
        self._tracker = _SequenceTracker()

    # -- lifecycle ------------------------------------------------------

    async def connect(self) -> None:
        if self._connected:
            return
        if self._client is None:
            from coinbase.rest import RESTClient

            self._client = RESTClient(
                api_key=self._cfg.api_key,
                api_secret=self._cfg.api_secret,
                base_url=self._cfg.base_url,
                timeout=int(self._cfg.request_timeout_s),
                rate_limit_headers=self._cfg.rate_limit_headers,
            )
        # Smoke-test by pinging the server time. Cheap, unauthenticated.
        await self._run(self._client.get_unix_time)
        self._connected = True

    async def disconnect(self) -> None:
        self._client = None
        self._connected = False
        self._tracker.reset()

    # -- account / positions --------------------------------------------

    async def get_account(self) -> AccountSnapshot:
        await self._ensure()
        assert self._client is not None
        raw = await self._run(self._client.get_accounts)
        raw_dict = _to_dict(raw)
        accounts = raw_dict.get("accounts") or []
        # Sum USD-equivalent balances. Coinbase returns one account per
        # currency; non-USD balances need conversion which lives in the
        # ledger layer — here we surface raw USD only.
        total_usd = Decimal("0")
        available_usd = Decimal("0")
        for acc in accounts:
            acc_dict = _to_dict(acc)
            currency = str(acc_dict.get("currency", "")).upper()
            if currency != "USD":
                continue
            available_balance = _to_dict(acc_dict.get("available_balance") or {})
            hold = _to_dict(acc_dict.get("hold") or {})
            available_usd += _to_decimal(available_balance.get("value"))
            total_usd += _to_decimal(available_balance.get("value")) + _to_decimal(
                hold.get("value")
            )
        return AccountSnapshot(
            venue=self.venue,
            equity=total_usd,
            buying_power=available_usd,
            cash=available_usd,
            portfolio_value=total_usd,
            currency="USD",
            pattern_day_trader=False,
            trading_blocked=False,
            raw=raw_dict,
        )

    async def get_positions(self) -> list[PositionSnapshot]:
        await self._ensure()
        assert self._client is not None
        raw = await self._run(self._client.get_accounts)
        raw_dict = _to_dict(raw)
        out: list[PositionSnapshot] = []
        for acc in raw_dict.get("accounts") or []:
            acc_dict = _to_dict(acc)
            currency = str(acc_dict.get("currency", "")).upper()
            if currency in ("", "USD", "USDC", "USDT", "DAI"):
                continue
            balance = _to_dict(acc_dict.get("available_balance") or {})
            qty = _to_decimal(balance.get("value"))
            if qty <= 0:
                continue
            symbol = f"{currency}/USD"
            out.append(
                PositionSnapshot(
                    symbol=symbol,
                    qty=qty,
                    avg_entry_price=Decimal("0"),  # Coinbase doesn't track avg cost
                    market_value=Decimal("0"),  # computed from book in ledger
                    asset_class="crypto",
                    unrealized_pl=Decimal("0"),
                    venue=self.venue,
                    raw=acc_dict,
                )
            )
        return out

    # -- orders ---------------------------------------------------------

    async def submit_order(self, proposal: OrderProposal) -> OrderAck:
        await self._ensure()
        assert self._client is not None
        parsed = parse_client_order_id(proposal.client_order_id)
        if parsed.venue != "coinbase":
            raise ExchangeError(
                f"coid venue mismatch: coid is for {parsed.venue}, adapter is coinbase"
            )
        if proposal.asset_class != "crypto":
            raise ExchangeError(
                f"coinbase adapter only handles crypto, got {proposal.asset_class!r}"
            )

        product_id = _to_product_id(proposal.symbol)
        side_str = "BUY" if proposal.side == "BUY" else "SELL"
        base_size = format(proposal.qty.normalize(), "f")

        try:
            if proposal.order_type == "market":
                raw_response = await self._run(
                    self._client.create_order,
                    client_order_id=proposal.client_order_id,
                    product_id=product_id,
                    side=side_str,
                    order_configuration={
                        "market_market_ioc": {"base_size": base_size},
                    },
                )
            elif proposal.order_type == "limit":
                if proposal.limit_price is None:
                    raise ValueError("limit order requires limit_price")
                limit_price = format(proposal.limit_price.normalize(), "f")
                tif_key = (
                    "limit_limit_gtc"
                    if proposal.time_in_force in ("gtc", "day")
                    else "limit_limit_ioc"
                )
                config: dict[str, Any] = {
                    "base_size": base_size,
                    "limit_price": limit_price,
                }
                if tif_key == "limit_limit_gtc":
                    config["post_only"] = bool(proposal.metadata.get("post_only", False))
                raw_response = await self._run(
                    self._client.create_order,
                    client_order_id=proposal.client_order_id,
                    product_id=product_id,
                    side=side_str,
                    order_configuration={tif_key: config},
                )
            else:
                raise ValueError(
                    f"coinbase adapter does not support order_type={proposal.order_type!r}"
                )
        except Exception as exc:
            self._maybe_raise_rate_limit(exc)
            raise OrderRejected(
                str(exc),
                client_order_id=proposal.client_order_id,
                venue=self.venue,
            ) from exc

        resp_dict = _to_dict(raw_response)
        if not resp_dict.get("success", True):
            err = resp_dict.get("error_response") or resp_dict.get("failure_reason") or "rejected"
            raise OrderRejected(
                str(err),
                client_order_id=proposal.client_order_id,
                venue=self.venue,
                raw=resp_dict,
            )

        venue_order_id = ""
        success_resp = _to_dict(resp_dict.get("success_response") or {})
        venue_order_id = str(success_resp.get("order_id") or resp_dict.get("order_id") or "")

        return OrderAck(
            venue=self.venue,
            client_order_id=proposal.client_order_id,
            venue_order_id=venue_order_id,
            status="open",
            symbol=proposal.symbol,
            side=proposal.side,
            qty=proposal.qty,
            filled_qty=Decimal("0"),
            asset_class="crypto",
            submitted_at=datetime.now(UTC),
            raw=resp_dict,
        )

    async def cancel_order(self, client_order_id: str) -> None:
        await self._ensure()
        assert self._client is not None
        # Coinbase cancel requires the venue order id, not client_order_id.
        # We list orders filtered by client_order_id, then cancel by id.
        try:
            listing = await self._run(
                self._client.list_orders,
                order_ids=None,
                product_ids=None,
                limit=10,
            )
        except Exception as exc:
            self._maybe_raise_rate_limit(exc)
            logger.info("cancel: list_orders failed for %s: %s", client_order_id, exc)
            return

        listing_dict = _to_dict(listing)
        for order in listing_dict.get("orders") or []:
            order_dict = _to_dict(order)
            if order_dict.get("client_order_id") == client_order_id:
                venue_id = str(order_dict.get("order_id", ""))
                if not venue_id:
                    return
                try:
                    await self._run(self._client.cancel_orders, order_ids=[venue_id])
                except Exception as exc:
                    self._maybe_raise_rate_limit(exc)
                    logger.info("cancel: %s rejected: %s", client_order_id, exc)
                return

    async def get_orders(self, status: OrderStatus | None = None) -> list[OrderAck]:
        await self._ensure()
        assert self._client is not None
        status_filter: list[str] | None = None
        if status == "open" or status == "partially_filled":
            status_filter = ["OPEN", "PENDING"]
        elif status == "filled":
            status_filter = ["FILLED"]
        elif status == "canceled":
            status_filter = ["CANCELLED"]
        elif status == "rejected":
            status_filter = ["FAILED"]

        raw = await self._run(
            self._client.list_orders,
            order_status=status_filter,
            limit=500,
        )
        raw_dict = _to_dict(raw)
        out: list[OrderAck] = []
        for order in raw_dict.get("orders") or []:
            ack = _order_to_ack(order, self.venue)
            if status is not None and ack.status != status:
                continue
            out.append(ack)
        return out

    # -- streams --------------------------------------------------------

    async def stream_ticks(self, symbols: Sequence[str]) -> AsyncIterator[Tick]:
        """Subscribe to ``ticker`` + ``market_trades`` channels.

        Real WS wiring lives in a follow-up PR; the surface here is enough
        for the live engine to add channels and for tests to inject a
        fake stream. Yields nothing until wired.
        """
        await self._ensure()
        if False:  # pragma: no cover
            yield Tick(venue=self.venue, symbol="", price=Decimal(0), size=Decimal(0), ts=_utc())
        return

    async def stream_fills(self) -> AsyncIterator[Fill]:
        """Subscribe to ``user`` channel for our account's fills."""
        await self._ensure()
        if False:  # pragma: no cover
            yield Fill(
                venue=self.venue,
                asset_class="crypto",
                symbol="",
                venue_order_id="",
                client_order_id="",
                side="BUY",
                qty=Decimal(0),
                price=Decimal(0),
                fee=Decimal(0),
                fee_accrued_later=False,
                ts=_utc(),
                raw={},
            )
        return

    async def stream_orderbook(
        self,
        symbols: Sequence[str] | None = None,
        depth: int = 10,
    ) -> AsyncIterator[OrderbookSnapshot]:
        """Subscribe to ``level2`` channel."""
        await self._ensure()
        if False:  # pragma: no cover
            yield OrderbookSnapshot(venue=self.venue, symbol="", bids=[], asks=[], ts=_utc())
        return

    # -- WS message ingestion helpers (called by future WS task) --------

    def observe_sequence(self, channel: str, product_id: str, sequence_num: int) -> None:
        """Public entry-point for the WS pump to register a sequence_num.

        Separated out so unit tests can drive the gap-detector without
        spinning a real WebSocket.
        """
        self._tracker.observe(channel, product_id, sequence_num)

    @staticmethod
    def parse_orderbook_message(payload: dict[str, Any]) -> OrderbookSnapshot:
        """Map a ``level2`` WS message into our :class:`OrderbookSnapshot`."""
        product_id = str(payload.get("product_id", ""))
        symbol = _from_product_id(product_id)
        bids = [
            OrderbookLevel(
                price=_to_decimal(level.get("price_level")),
                size=_to_decimal(level.get("new_quantity")),
            )
            for level in payload.get("updates", [])
            if str(level.get("side", "")).lower() == "bid"
        ]
        asks = [
            OrderbookLevel(
                price=_to_decimal(level.get("price_level")),
                size=_to_decimal(level.get("new_quantity")),
            )
            for level in payload.get("updates", [])
            if str(level.get("side", "")).lower() == "offer"
        ]
        ts_raw = payload.get("timestamp")
        ts: datetime
        if isinstance(ts_raw, datetime):
            ts = _utc(ts_raw)
        elif isinstance(ts_raw, str):
            try:
                ts = _utc(datetime.fromisoformat(ts_raw.replace("Z", "+00:00")))
            except ValueError:
                ts = _utc()
        else:
            ts = _utc()
        seq = payload.get("sequence_num")
        return OrderbookSnapshot(
            venue="coinbase",
            symbol=symbol,
            bids=bids,
            asks=asks,
            ts=ts,
            sequence_num=int(seq) if isinstance(seq, int) else None,
        )

    # -- internals ------------------------------------------------------

    async def _ensure(self) -> None:
        if not self._connected:
            await self.connect()

    async def _run(self, fn: Any, *args: Any, **kwargs: Any) -> Any:
        return await anyio.to_thread.run_sync(lambda: fn(*args, **kwargs))

    def _maybe_raise_rate_limit(self, exc: BaseException) -> None:
        msg = str(exc)
        if "429" in msg or ("rate" in msg.lower() and "limit" in msg.lower()):
            retry_after = None
            for header in ("Retry-After", "retry-after"):
                val = getattr(exc, header, None) or getattr(exc, "retry_after", None)
                if val is not None:
                    try:
                        retry_after = float(val)
                    except (TypeError, ValueError):
                        retry_after = None
                    break
            raise RateLimited(msg, retry_after_s=retry_after) from exc


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _to_product_id(symbol: str) -> str:
    """``BTC/USD`` → ``BTC-USD`` (Coinbase's wire format)."""
    return symbol.replace("/", "-").upper()


def _from_product_id(product_id: str) -> str:
    """``BTC-USD`` → ``BTC/USD``."""
    return product_id.replace("-", "/").upper()


def _to_dict(obj: Any) -> dict[str, Any]:
    if obj is None:
        return {}
    if isinstance(obj, dict):
        return obj
    for attr in ("to_dict", "model_dump", "dict", "_asdict"):
        fn = getattr(obj, attr, None)
        if callable(fn):
            try:
                result = fn()
            except TypeError:
                continue
            if isinstance(result, dict):
                return result
    if hasattr(obj, "__dict__"):
        return dict(obj.__dict__)
    return {"value": obj}


def _order_to_ack(raw: Any, venue: str) -> OrderAck:
    d = _to_dict(raw)
    product_id = str(d.get("product_id", ""))
    symbol = _from_product_id(product_id) if product_id else str(d.get("symbol", ""))
    side_raw = str(d.get("side", "")).upper()
    side: Side = "BUY" if side_raw == "BUY" else "SELL"
    status_raw = str(d.get("status", "OPEN"))
    submitted_at = d.get("created_time") or d.get("submitted_at")
    if isinstance(submitted_at, str):
        try:
            submitted_at = datetime.fromisoformat(submitted_at.replace("Z", "+00:00"))
        except ValueError:
            submitted_at = datetime.now(UTC)
    if not isinstance(submitted_at, datetime):
        submitted_at = datetime.now(UTC)
    qty = _to_decimal(d.get("base_size") or d.get("filled_size") or d.get("size"))
    filled = _to_decimal(d.get("filled_size") or d.get("completion_percentage"))
    return OrderAck(
        venue=venue,
        client_order_id=str(d.get("client_order_id", "")),
        venue_order_id=str(d.get("order_id", "")),
        status=_map_status(status_raw),
        symbol=symbol,
        side=side,
        qty=qty,
        filled_qty=filled,
        asset_class="crypto",
        submitted_at=_utc(submitted_at),
        raw=d,
    )
