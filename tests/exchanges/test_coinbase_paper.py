"""Cassette-based tests for ``CoinbaseExchange``.

Coinbase Advanced Trade signs every authenticated request with an ES256
JWT, which makes vcrpy-style HTTP-level cassettes fragile — the signature
header changes on every request. We use **JSON cassettes** that hold
just the response body, fed into a mocked ``RESTClient``. This is
equivalent to vcrpy with a body-only matcher and skips the auth dance.

Coverage focus:

* ``get_account`` aggregates USD balances correctly.
* ``get_positions`` filters out fiat/stablecoin entries.
* ``submit_order`` builds the correct order_configuration for both
  market and limit orders.
* ``stream_orderbook`` parsing handles snapshots + sequence_num.
* ``_SequenceTracker`` detects gaps and ignores out-of-order frames.
* Asset class is always ``crypto`` (no option/stock confusion possible).
"""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from quanta_core.exchanges.base import (
    ExchangeError,
    OrderProposal,
    OrderRejected,
    RateLimited,
    SequenceGap,
)
from quanta_core.exchanges.coinbase import (
    CoinbaseConfig,
    CoinbaseExchange,
    _from_product_id,
    _map_status,
    _SequenceTracker,
    _to_product_id,
)
from quanta_core.exchanges.idempotency import IntentKey, make_client_order_id

CASSETTE_DIR = Path(__file__).parent / "cassettes"


def _load(name: str) -> dict[str, Any]:
    with open(CASSETTE_DIR / name) as fh:
        return json.load(fh)


def _coid(strategy: str = "mr01") -> str:
    key = IntentKey(
        venue="coinbase",
        strategy_id=strategy,
        symbol="BTC/USD",
        side="BUY",
        qty=Decimal("0.001"),
        intent_timestamp_ms=1_715_544_000_000,
    )
    return make_client_order_id(strategy, key)


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


def test_config_rejects_bad_mode() -> None:
    with pytest.raises(ValueError):
        CoinbaseConfig(api_key="a", api_secret="b", mode="sandbox")


def test_config_requires_keys() -> None:
    with pytest.raises(ValueError):
        CoinbaseConfig(api_key="", api_secret="b", mode="paper")


def test_config_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("COINBASE_API_KEY", "env-key")
    monkeypatch.setenv("COINBASE_API_SECRET", "env-sec")
    cfg = CoinbaseConfig.from_env()
    assert cfg.api_key == "env-key"
    assert cfg.is_paper is True


def test_config_live_mode() -> None:
    cfg = CoinbaseConfig(api_key="a", api_secret="b", mode="live")
    assert cfg.is_paper is False


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def test_product_id_round_trip() -> None:
    assert _to_product_id("BTC/USD") == "BTC-USD"
    assert _from_product_id("BTC-USD") == "BTC/USD"
    assert _from_product_id(_to_product_id("ETH/USDC")) == "ETH/USDC"


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("OPEN", "open"),
        ("PENDING", "open"),
        ("FILLED", "filled"),
        ("PARTIALLY_FILLED", "partially_filled"),
        ("CANCELLED", "canceled"),
        ("CANCELED", "canceled"),
        ("FAILED", "rejected"),
        ("EXPIRED", "expired"),
        ("WHATEVER", "open"),
    ],
)
def test_map_status(raw: str, expected: str) -> None:
    assert _map_status(raw) == expected


# ---------------------------------------------------------------------------
# Sequence tracker
# ---------------------------------------------------------------------------


def test_sequence_tracker_normal_progression() -> None:
    t = _SequenceTracker()
    for n in range(1, 10):
        t.observe("level2", "BTC-USD", n)
    assert t.last_seen[("level2", "BTC-USD")] == 9


def test_sequence_tracker_raises_on_gap() -> None:
    """The whole point — a missing sequence_num MUST raise so the
    caller can REST-reconcile + Slack-warn."""
    t = _SequenceTracker()
    t.observe("level2", "BTC-USD", 1)
    t.observe("level2", "BTC-USD", 2)
    with pytest.raises(SequenceGap) as exc:
        t.observe("level2", "BTC-USD", 5)
    assert exc.value.channel == "level2"
    assert exc.value.product_id == "BTC-USD"
    assert exc.value.expected == 3
    assert exc.value.got == 5
    assert exc.value.gap == 2
    # After raise, tracker MUST advance so we don't re-raise on next frame.
    t.observe("level2", "BTC-USD", 6)


def test_sequence_tracker_ignores_out_of_order() -> None:
    t = _SequenceTracker()
    t.observe("level2", "BTC-USD", 10)
    t.observe("level2", "BTC-USD", 11)
    # Late frame — should be silently dropped.
    t.observe("level2", "BTC-USD", 7)
    assert t.last_seen[("level2", "BTC-USD")] == 11


def test_sequence_tracker_per_channel_and_product() -> None:
    t = _SequenceTracker()
    t.observe("level2", "BTC-USD", 1)
    t.observe("level2", "ETH-USD", 1)
    t.observe("ticker", "BTC-USD", 1)
    # Each (channel, product) tracks independently.
    assert len(t.last_seen) == 3


def test_sequence_tracker_reset() -> None:
    t = _SequenceTracker()
    t.observe("level2", "BTC-USD", 5)
    t.observe("ticker", "BTC-USD", 5)
    t.reset(channel="level2")
    assert ("level2", "BTC-USD") not in t.last_seen
    assert ("ticker", "BTC-USD") in t.last_seen
    t.reset()
    assert len(t.last_seen) == 0


def test_observe_sequence_via_adapter() -> None:
    cfg = CoinbaseConfig(api_key="k", api_secret="s", mode="paper")
    ex = CoinbaseExchange(cfg)
    ex.observe_sequence("level2", "BTC-USD", 1)
    with pytest.raises(SequenceGap):
        ex.observe_sequence("level2", "BTC-USD", 5)


# ---------------------------------------------------------------------------
# Orderbook parser
# ---------------------------------------------------------------------------


def test_orderbook_message_parses() -> None:
    payload = _load("coinbase_ws_orderbook.json")
    # Adapt to the level2 event shape (one event payload per snapshot)
    event = payload["events"][0]
    event["timestamp"] = payload["timestamp"]
    event["sequence_num"] = payload["sequence_num"]
    snap = CoinbaseExchange.parse_orderbook_message(event)
    assert snap.symbol == "BTC/USD"
    assert snap.sequence_num == 42
    assert len(snap.bids) == 2
    assert len(snap.asks) == 2
    assert snap.bids[0].price == Decimal("65000.10")
    assert snap.asks[0].price == Decimal("65001.00")


def test_orderbook_message_handles_bad_timestamp() -> None:
    """A garbage timestamp should not blow up the parser."""
    bad = {"product_id": "BTC-USD", "updates": [], "timestamp": "not-a-date"}
    snap = CoinbaseExchange.parse_orderbook_message(bad)
    assert snap.symbol == "BTC/USD"
    assert snap.sequence_num is None


# ---------------------------------------------------------------------------
# Cassette-driven REST tests (mocked client)
# ---------------------------------------------------------------------------


def _build_ex_with_mock(method_map: dict[str, Any]) -> CoinbaseExchange:
    """Wire a CoinbaseExchange with a MagicMock for the RESTClient."""
    cfg = CoinbaseConfig(api_key="k", api_secret="s", mode="paper")
    fake = MagicMock()
    fake.get_unix_time.return_value = {"epochSeconds": "1715544000"}
    for name, value in method_map.items():
        getattr(fake, name).return_value = value
    return CoinbaseExchange(cfg, client=fake)


def _as_response_obj(data: dict[str, Any]) -> Any:
    """Wrap a dict in something with a ``to_dict`` method (mimics the SDK)."""
    obj = MagicMock()
    obj.to_dict.return_value = data
    obj.__dict__.update(data)
    return obj


async def test_get_account_aggregates_usd() -> None:
    cassette = _load("coinbase_paper_accounts.json")
    ex = _build_ex_with_mock({"get_accounts": _as_response_obj(cassette)})
    await ex.connect()
    acct = await ex.get_account()
    assert acct.venue == "coinbase"
    assert acct.currency == "USD"
    assert acct.cash == Decimal("10000.00")
    assert acct.equity == Decimal("10000.00")
    assert acct.pattern_day_trader is False


async def test_get_positions_filters_fiat() -> None:
    cassette = _load("coinbase_paper_accounts.json")
    ex = _build_ex_with_mock({"get_accounts": _as_response_obj(cassette)})
    await ex.connect()
    positions = await ex.get_positions()
    # Only BTC should be returned — USD + USDC are filtered.
    assert len(positions) == 1
    assert positions[0].symbol == "BTC/USD"
    assert positions[0].qty == Decimal("0.5")
    assert positions[0].asset_class == "crypto"


async def test_submit_limit_order_builds_gtc_config() -> None:
    cassette = _load("coinbase_paper_create_order.json")
    ex = _build_ex_with_mock({"create_order": _as_response_obj(cassette)})
    await ex.connect()

    coid = _coid()
    proposal = OrderProposal(
        symbol="BTC/USD",
        side="BUY",
        qty=Decimal("0.001"),
        order_type="limit",
        limit_price=Decimal("60000"),
        time_in_force="gtc",
        client_order_id=coid,
        asset_class="crypto",
        strategy_id="mr01",
    )
    ack = await ex.submit_order(proposal)
    assert ack.venue == "coinbase"
    assert ack.symbol == "BTC/USD"
    assert ack.qty == Decimal("0.001")
    assert ack.asset_class == "crypto"
    assert ack.status == "open"

    # Verify create_order was called with the expected configuration.
    # Internal client is the fake; pull the call args.
    call = ex._client.create_order.call_args  # type: ignore[union-attr]
    assert call.kwargs["product_id"] == "BTC-USD"
    assert call.kwargs["side"] == "BUY"
    cfg = call.kwargs["order_configuration"]
    assert "limit_limit_gtc" in cfg
    assert cfg["limit_limit_gtc"]["base_size"] == "0.001"


async def test_submit_market_order_builds_market_ioc() -> None:
    cassette = _load("coinbase_paper_create_order.json")
    ex = _build_ex_with_mock({"create_order": _as_response_obj(cassette)})
    await ex.connect()

    coid = _coid()
    proposal = OrderProposal(
        symbol="BTC/USD",
        side="SELL",
        qty=Decimal("0.001"),
        order_type="market",
        client_order_id=coid,
        asset_class="crypto",
        strategy_id="mr01",
    )
    await ex.submit_order(proposal)
    cfg = ex._client.create_order.call_args.kwargs["order_configuration"]  # type: ignore[union-attr]
    assert "market_market_ioc" in cfg


async def test_submit_rejects_non_crypto() -> None:
    ex = _build_ex_with_mock({})
    await ex.connect()
    proposal = OrderProposal(
        symbol="AAPL",
        side="BUY",
        qty=Decimal("1"),
        order_type="market",
        client_order_id=_coid(),
        asset_class="stock",
        strategy_id="mr01",
    )
    with pytest.raises(ExchangeError, match="crypto"):
        await ex.submit_order(proposal)


async def test_submit_rejects_wrong_venue_coid() -> None:
    ex = _build_ex_with_mock({})
    await ex.connect()
    # Build an alpaca-flavored coid
    alpaca_coid = make_client_order_id(
        "mr01",
        IntentKey(
            venue="alpaca",
            strategy_id="mr01",
            symbol="X",
            side="BUY",
            qty=Decimal("1"),
            intent_timestamp_ms=0,
        ),
    )
    proposal = OrderProposal(
        symbol="BTC/USD",
        side="BUY",
        qty=Decimal("0.001"),
        order_type="market",
        client_order_id=alpaca_coid,
        asset_class="crypto",
        strategy_id="mr01",
    )
    with pytest.raises(ExchangeError, match="venue mismatch"):
        await ex.submit_order(proposal)


async def test_submit_limit_missing_price_raises() -> None:
    ex = _build_ex_with_mock({})
    await ex.connect()
    proposal = OrderProposal(
        symbol="BTC/USD",
        side="BUY",
        qty=Decimal("0.001"),
        order_type="limit",
        client_order_id=_coid(),
        asset_class="crypto",
        strategy_id="mr01",
    )
    with pytest.raises(OrderRejected, match="limit_price"):
        await ex.submit_order(proposal)


async def test_submit_unsupported_order_type() -> None:
    ex = _build_ex_with_mock({})
    await ex.connect()
    proposal = OrderProposal(
        symbol="BTC/USD",
        side="BUY",
        qty=Decimal("0.001"),
        order_type="stop",
        client_order_id=_coid(),
        asset_class="crypto",
        strategy_id="mr01",
    )
    with pytest.raises(OrderRejected, match="order_type"):
        await ex.submit_order(proposal)


async def test_submit_failure_response_raises_rejected() -> None:
    cassette = {
        "success": False,
        "error_response": {"error": "INSUFFICIENT_FUND", "message": "Not enough USD"},
    }
    ex = _build_ex_with_mock({"create_order": _as_response_obj(cassette)})
    await ex.connect()
    proposal = OrderProposal(
        symbol="BTC/USD",
        side="BUY",
        qty=Decimal("0.001"),
        order_type="market",
        client_order_id=_coid(),
        asset_class="crypto",
        strategy_id="mr01",
    )
    with pytest.raises(OrderRejected):
        await ex.submit_order(proposal)


async def test_submit_429_surfaces_rate_limited() -> None:
    cfg = CoinbaseConfig(api_key="k", api_secret="s", mode="paper")
    fake = MagicMock()
    fake.get_unix_time.return_value = {}
    err = RuntimeError("429 rate limit exceeded")
    err.retry_after = "3.0"  # type: ignore[attr-defined]
    fake.create_order.side_effect = err
    ex = CoinbaseExchange(cfg, client=fake)
    await ex.connect()
    proposal = OrderProposal(
        symbol="BTC/USD",
        side="BUY",
        qty=Decimal("0.001"),
        order_type="market",
        client_order_id=_coid(),
        asset_class="crypto",
        strategy_id="mr01",
    )
    with pytest.raises(RateLimited):
        await ex.submit_order(proposal)


async def test_get_orders_filters() -> None:
    cassette = _load("coinbase_paper_list_orders.json")
    ex = _build_ex_with_mock({"list_orders": _as_response_obj(cassette)})
    await ex.connect()
    orders = await ex.get_orders(status="open")
    assert len(orders) == 1
    assert orders[0].status == "open"
    assert orders[0].symbol == "BTC/USD"
    assert orders[0].asset_class == "crypto"


async def test_cancel_known_order_calls_sdk() -> None:
    listing_cassette = _load("coinbase_paper_list_orders.json")
    ex = _build_ex_with_mock(
        {
            "list_orders": _as_response_obj(listing_cassette),
            "cancel_orders": _as_response_obj({"results": [{"success": True}]}),
        }
    )
    await ex.connect()
    coid_str = listing_cassette["orders"][0]["client_order_id"]
    await ex.cancel_order(coid_str)
    ex._client.cancel_orders.assert_called_once()  # type: ignore[union-attr]


async def test_cancel_unknown_order_is_noop() -> None:
    ex = _build_ex_with_mock(
        {
            "list_orders": _as_response_obj({"orders": []}),
        }
    )
    await ex.connect()
    await ex.cancel_order(_coid())  # MUST NOT raise


async def test_cancel_list_failure_is_swallowed() -> None:
    cfg = CoinbaseConfig(api_key="k", api_secret="s", mode="paper")
    fake = MagicMock()
    fake.get_unix_time.return_value = {}
    fake.list_orders.side_effect = RuntimeError("network down")
    ex = CoinbaseExchange(cfg, client=fake)
    await ex.connect()
    await ex.cancel_order(_coid())  # logs and returns


async def test_connect_idempotent() -> None:
    ex = _build_ex_with_mock({})
    await ex.connect()
    await ex.connect()
    ex._client.get_unix_time.assert_called_once()  # type: ignore[union-attr]


async def test_disconnect_resets_state() -> None:
    ex = _build_ex_with_mock({})
    await ex.connect()
    ex.observe_sequence("level2", "BTC-USD", 1)
    await ex.disconnect()
    assert ex._tracker.last_seen == {}


async def test_stream_iterators_are_empty_until_wired() -> None:
    ex = _build_ex_with_mock({})
    await ex.connect()
    ticks: list[Any] = []
    async for t in ex.stream_ticks(["BTC/USD"]):
        ticks.append(t)
    fills: list[Any] = []
    async for f in ex.stream_fills():
        fills.append(f)
    books: list[Any] = []
    async for b in ex.stream_orderbook(["BTC/USD"]):
        books.append(b)
    assert ticks == fills == books == []
