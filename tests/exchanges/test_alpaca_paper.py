"""Cassette-based tests for ``AlpacaExchange``.

These tests NEVER hit the live broker — cassettes are pre-recorded and
``record_mode='none'`` forces vcrpy to fail loudly on any cassette miss.

Coverage focus:

* ``get_account`` parses the canonical paper-account payload.
* ``get_positions`` surfaces ``asset_class`` per position (the bug today).
* ``submit_order`` round-trips client_order_id + returns a normalised ack.
* ``get_orders(status='open')`` filters correctly.
* Status / asset-class normalisation helpers handle each known variant.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
import vcr

from quanta_core.exchanges.alpaca import (
    AlpacaConfig,
    AlpacaExchange,
    _infer_asset_class,
    _map_asset_class,
    _map_status,
    _symbol_is_option,
)
from quanta_core.exchanges.base import (
    ExchangeError,
    OrderProposal,
    OrderRejected,
)
from quanta_core.exchanges.idempotency import IntentKey, make_client_order_id

CASSETTE_DIR = Path(__file__).parent / "cassettes"

_VCR = vcr.VCR(
    cassette_library_dir=str(CASSETTE_DIR),
    record_mode="none",
    match_on=["method", "scheme", "host", "path"],
    serializer="yaml",
    filter_headers=["authorization", "APCA-API-KEY-ID", "APCA-API-SECRET-KEY"],
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _coid(strategy: str = "wheel", venue: str = "alpaca") -> str:
    key = IntentKey(
        venue=venue,  # type: ignore[arg-type]
        strategy_id=strategy,
        symbol="AAPL",
        side="BUY",
        qty=Decimal("1"),
        intent_timestamp_ms=1_715_544_000_000,
    )
    return make_client_order_id(strategy, key)


# ---------------------------------------------------------------------------
# Config validation
# ---------------------------------------------------------------------------


def test_config_rejects_bad_mode() -> None:
    with pytest.raises(ValueError):
        AlpacaConfig(api_key="a", secret_key="b", mode="sandbox")


def test_config_requires_keys() -> None:
    with pytest.raises(ValueError):
        AlpacaConfig(api_key="", secret_key="b", mode="paper")


def test_config_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ALPACA_API_KEY", "env-key")
    monkeypatch.setenv("ALPACA_SECRET_KEY", "env-secret")
    cfg = AlpacaConfig.from_env(mode="paper")
    assert cfg.api_key == "env-key"
    assert cfg.paper is True


def test_config_live_mode() -> None:
    cfg = AlpacaConfig(api_key="a", secret_key="b", mode="live")
    assert cfg.paper is False


# ---------------------------------------------------------------------------
# Pure-function helpers
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("new", "open"),
        ("accepted", "open"),
        ("partially_filled", "partially_filled"),
        ("filled", "filled"),
        ("canceled", "canceled"),
        ("CANCELED", "canceled"),
        ("rejected", "rejected"),
        ("expired", "expired"),
        ("replaced", "canceled"),
        ("unknown_status_string", "open"),  # safe default
    ],
)
def test_map_status(raw: str, expected: str) -> None:
    assert _map_status(raw) == expected


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("us_equity", "stock"),
        ("US_EQUITY", "stock"),
        ("us_option", "option"),
        ("crypto", "crypto"),
        ("crypto_perp", "crypto"),
        (None, "stock"),
        ("unknown", "stock"),
    ],
)
def test_map_asset_class(raw: Any, expected: str) -> None:
    assert _map_asset_class(raw) == expected


def test_symbol_is_option() -> None:
    assert _symbol_is_option("AAPL250620C00150000")
    assert _symbol_is_option("SPY250101P04500000")
    assert not _symbol_is_option("AAPL")
    assert not _symbol_is_option("BTC/USD")
    assert not _symbol_is_option("SHORT")


def test_infer_asset_class() -> None:
    assert _infer_asset_class("AAPL") == "stock"
    assert _infer_asset_class("BTC/USD") == "crypto"
    assert _infer_asset_class("AAPL250620C00150000") == "option"
    # Hint overrides heuristic
    assert _infer_asset_class("AAPL", hint="us_option") == "option"


# ---------------------------------------------------------------------------
# Cassette-based REST tests
# ---------------------------------------------------------------------------


@pytest.mark.vcr
async def test_get_account_parses_paper_response() -> None:
    cfg = AlpacaConfig(api_key="test-key", secret_key="test-secret", mode="paper")
    ex = AlpacaExchange(cfg)
    with _VCR.use_cassette("alpaca_paper_account.yaml"):
        await ex.connect()
        account = await ex.get_account()
    assert account.venue == "alpaca"
    assert account.equity == Decimal("4000.32")
    assert account.buying_power == Decimal("16001.28")
    assert account.cash == Decimal("4000.32")
    assert account.currency == "USD"
    assert account.pattern_day_trader is False
    assert account.trading_blocked is False


@pytest.mark.vcr
async def test_get_positions_surfaces_asset_class() -> None:
    """REGRESSION: today's bug — option positions came back flagged as
    ``stock``. The adapter MUST surface ``asset_class`` correctly per
    position, both for the SDK-tagged ``us_option`` AND for the OPRA
    symbol heuristic."""
    cfg = AlpacaConfig(api_key="test-key", secret_key="test-secret", mode="paper")
    ex = AlpacaExchange(cfg)
    with _VCR.use_cassette("alpaca_paper_account.yaml"):
        await ex.connect()
        positions = await ex.get_positions()

    assert len(positions) == 2
    by_symbol = {p.symbol: p for p in positions}

    stock_pos = by_symbol["AAPL"]
    assert stock_pos.asset_class == "stock"
    assert stock_pos.qty == Decimal("10")
    assert stock_pos.avg_entry_price == Decimal("180.50")

    option_pos = by_symbol["AAPL250620C00150000"]
    assert option_pos.asset_class == "option", (
        f"BUG: option position {option_pos.symbol} surfaced as "
        f"{option_pos.asset_class!r}; should be 'option'"
    )
    assert option_pos.qty == Decimal("-1")


@pytest.mark.vcr
async def test_submit_order_round_trips_client_order_id() -> None:
    cfg = AlpacaConfig(api_key="test-key", secret_key="test-secret", mode="paper")
    ex = AlpacaExchange(cfg)
    coid = _coid()
    proposal = OrderProposal(
        symbol="AAPL",
        side="BUY",
        qty=Decimal("1"),
        order_type="limit",
        limit_price=Decimal("150"),
        time_in_force="day",
        client_order_id=coid,
        asset_class="stock",
        strategy_id="wheel",
    )
    with _VCR.use_cassette("alpaca_paper_submit_order.yaml"):
        await ex.connect()
        ack = await ex.submit_order(proposal)
    assert ack.venue == "alpaca"
    assert ack.client_order_id == coid
    assert ack.symbol == "AAPL"
    assert ack.side == "BUY"
    assert ack.qty == Decimal("1")
    assert ack.status == "open"
    assert ack.asset_class == "stock"


@pytest.mark.vcr
async def test_get_orders_open() -> None:
    cfg = AlpacaConfig(api_key="test-key", secret_key="test-secret", mode="paper")
    ex = AlpacaExchange(cfg)
    with _VCR.use_cassette("alpaca_paper_get_orders.yaml"):
        await ex.connect()
        orders = await ex.get_orders(status="open")
    assert len(orders) == 1
    assert orders[0].status == "open"
    assert orders[0].symbol == "AAPL"


# ---------------------------------------------------------------------------
# Validation paths (no network)
# ---------------------------------------------------------------------------


def _fake_client_with(method_map: dict[str, Any]) -> MagicMock:
    """Build a mock alpaca-py TradingClient with the given method outputs."""
    client = MagicMock()
    for name, value in method_map.items():
        getattr(client, name).return_value = value
    return client


async def test_submit_order_rejects_wrong_venue_coid() -> None:
    cfg = AlpacaConfig(api_key="test-key", secret_key="test-secret", mode="paper")
    fake_client = _fake_client_with({"get_clock": object()})
    ex = AlpacaExchange(cfg, client=fake_client)
    coid = _coid(venue="coinbase")
    proposal = OrderProposal(
        symbol="BTC/USD",
        side="BUY",
        qty=Decimal("0.01"),
        order_type="market",
        client_order_id=coid,
        asset_class="crypto",
        strategy_id="wheel",
    )
    await ex.connect()
    with pytest.raises(ExchangeError, match="coid venue mismatch"):
        await ex.submit_order(proposal)


async def test_submit_order_translates_sdk_error_to_rejected() -> None:
    cfg = AlpacaConfig(api_key="test-key", secret_key="test-secret", mode="paper")
    fake_client = MagicMock()
    fake_client.get_clock.return_value = object()
    fake_client.submit_order.side_effect = RuntimeError("insufficient buying power")
    ex = AlpacaExchange(cfg, client=fake_client)
    coid = _coid()
    proposal = OrderProposal(
        symbol="AAPL",
        side="BUY",
        qty=Decimal("1"),
        order_type="market",
        client_order_id=coid,
        strategy_id="wheel",
    )
    await ex.connect()
    with pytest.raises(OrderRejected) as exc:
        await ex.submit_order(proposal)
    assert "insufficient buying power" in str(exc.value)
    assert exc.value.venue == "alpaca"


async def test_submit_order_429_surfaces_rate_limited() -> None:
    """HTTP 429 (rate limit) MUST become RateLimited, not OrderRejected."""
    cfg = AlpacaConfig(api_key="test-key", secret_key="test-secret", mode="paper")
    fake_client = MagicMock()
    fake_client.get_clock.return_value = object()
    err = RuntimeError("429 too many requests")
    err.status_code = 429  # type: ignore[attr-defined]
    err.retry_after = "2.5"  # type: ignore[attr-defined]
    fake_client.submit_order.side_effect = err
    ex = AlpacaExchange(cfg, client=fake_client)
    coid = _coid()
    proposal = OrderProposal(
        symbol="AAPL",
        side="BUY",
        qty=Decimal("1"),
        order_type="market",
        client_order_id=coid,
        strategy_id="wheel",
    )
    await ex.connect()
    from quanta_core.exchanges.base import RateLimited

    with pytest.raises(RateLimited) as exc:
        await ex.submit_order(proposal)
    assert exc.value.retry_after_s == pytest.approx(2.5)


async def test_submit_limit_without_price_raises() -> None:
    """Programmer error (missing limit_price) raises ValueError, not
    OrderRejected — broker never sees the request."""
    cfg = AlpacaConfig(api_key="test-key", secret_key="test-secret", mode="paper")
    fake_client = MagicMock()
    fake_client.get_clock.return_value = object()
    ex = AlpacaExchange(cfg, client=fake_client)
    coid = _coid()
    proposal = OrderProposal(
        symbol="AAPL",
        side="BUY",
        qty=Decimal("1"),
        order_type="limit",
        client_order_id=coid,
        strategy_id="wheel",
    )
    await ex.connect()
    with pytest.raises(ValueError, match="limit_price"):
        await ex.submit_order(proposal)


async def test_cancel_unknown_order_is_noop() -> None:
    cfg = AlpacaConfig(api_key="test-key", secret_key="test-secret", mode="paper")
    fake_client = MagicMock()
    fake_client.get_clock.return_value = object()
    fake_client.get_order_by_client_id.side_effect = RuntimeError("not found")
    ex = AlpacaExchange(cfg, client=fake_client)
    await ex.connect()
    # Must NOT raise.
    await ex.cancel_order(_coid())


async def test_cancel_known_order_calls_sdk() -> None:
    cfg = AlpacaConfig(api_key="test-key", secret_key="test-secret", mode="paper")
    fake_client = MagicMock()
    fake_client.get_clock.return_value = object()
    fake_order = MagicMock()
    fake_order._asdict = lambda: {"id": "venue-id-123"}
    fake_client.get_order_by_client_id.return_value = fake_order
    ex = AlpacaExchange(cfg, client=fake_client)
    await ex.connect()
    await ex.cancel_order(_coid())
    fake_client.cancel_order_by_id.assert_called_once_with(order_id="venue-id-123")


async def test_orderbook_stream_raises_not_implemented() -> None:
    cfg = AlpacaConfig(api_key="test-key", secret_key="test-secret", mode="paper")
    fake_client = MagicMock()
    fake_client.get_clock.return_value = object()
    ex = AlpacaExchange(cfg, client=fake_client)
    await ex.connect()
    with pytest.raises(NotImplementedError):
        async for _ in ex.stream_orderbook(["AAPL"]):
            pass


async def test_tick_and_fill_streams_are_empty_until_wired() -> None:
    """The stream surfaces exist for the ABC; concrete WS wiring is a
    follow-up PR. They MUST be no-op async iterators today."""
    cfg = AlpacaConfig(api_key="test-key", secret_key="test-secret", mode="paper")
    fake_client = MagicMock()
    fake_client.get_clock.return_value = object()
    ex = AlpacaExchange(cfg, client=fake_client)
    await ex.connect()
    ticks: list[Any] = []
    async for tick in ex.stream_ticks(["AAPL"]):
        ticks.append(tick)
    fills: list[Any] = []
    async for fill in ex.stream_fills():
        fills.append(fill)
    assert ticks == []
    assert fills == []


async def test_disconnect_is_idempotent() -> None:
    cfg = AlpacaConfig(api_key="test-key", secret_key="test-secret", mode="paper")
    fake_client = MagicMock()
    fake_client.get_clock.return_value = object()
    ex = AlpacaExchange(cfg, client=fake_client)
    await ex.connect()
    await ex.disconnect()
    await ex.disconnect()  # second call is fine


async def test_connect_is_idempotent() -> None:
    cfg = AlpacaConfig(api_key="test-key", secret_key="test-secret", mode="paper")
    fake_client = MagicMock()
    fake_client.get_clock.return_value = object()
    ex = AlpacaExchange(cfg, client=fake_client)
    await ex.connect()
    await ex.connect()  # second call must not double-init
    fake_client.get_clock.assert_called_once()


async def test_submit_stop_order_builds_correct_request() -> None:
    cfg = AlpacaConfig(api_key="test-key", secret_key="test-secret", mode="paper")
    fake_client = MagicMock()
    fake_client.get_clock.return_value = object()
    fake_order = MagicMock()
    fake_order._asdict = lambda: {
        "id": "v1",
        "client_order_id": _coid(),
        "status": "new",
        "symbol": "AAPL",
        "side": "buy",
        "qty": "1",
        "filled_qty": "0",
        "asset_class": "us_equity",
    }
    fake_client.submit_order.return_value = fake_order
    ex = AlpacaExchange(cfg, client=fake_client)
    await ex.connect()
    proposal = OrderProposal(
        symbol="AAPL",
        side="BUY",
        qty=Decimal("1"),
        order_type="stop",
        stop_price=Decimal("180"),
        client_order_id=_coid(),
        strategy_id="wheel",
    )
    ack = await ex.submit_order(proposal)
    assert ack.symbol == "AAPL"
    fake_client.submit_order.assert_called_once()


async def test_submit_stop_limit_builds_correct_request() -> None:
    cfg = AlpacaConfig(api_key="test-key", secret_key="test-secret", mode="paper")
    fake_client = MagicMock()
    fake_client.get_clock.return_value = object()
    fake_order = MagicMock()
    fake_order._asdict = lambda: {
        "id": "v1",
        "client_order_id": _coid(),
        "status": "new",
        "symbol": "AAPL",
        "side": "buy",
        "qty": "1",
        "filled_qty": "0",
        "asset_class": "us_equity",
    }
    fake_client.submit_order.return_value = fake_order
    ex = AlpacaExchange(cfg, client=fake_client)
    await ex.connect()
    proposal = OrderProposal(
        symbol="AAPL",
        side="BUY",
        qty=Decimal("1"),
        order_type="stop_limit",
        stop_price=Decimal("180"),
        limit_price=Decimal("181"),
        client_order_id=_coid(),
        strategy_id="wheel",
    )
    await ex.submit_order(proposal)
    fake_client.submit_order.assert_called_once()


async def test_submit_stop_without_price_raises() -> None:
    cfg = AlpacaConfig(api_key="test-key", secret_key="test-secret", mode="paper")
    fake_client = MagicMock()
    fake_client.get_clock.return_value = object()
    ex = AlpacaExchange(cfg, client=fake_client)
    await ex.connect()
    proposal = OrderProposal(
        symbol="AAPL",
        side="BUY",
        qty=Decimal("1"),
        order_type="stop",
        client_order_id=_coid(),
        strategy_id="wheel",
    )
    with pytest.raises(ValueError, match="stop_price"):
        await ex.submit_order(proposal)


async def test_submit_stop_limit_missing_either_raises() -> None:
    cfg = AlpacaConfig(api_key="test-key", secret_key="test-secret", mode="paper")
    fake_client = MagicMock()
    fake_client.get_clock.return_value = object()
    ex = AlpacaExchange(cfg, client=fake_client)
    await ex.connect()
    proposal = OrderProposal(
        symbol="AAPL",
        side="BUY",
        qty=Decimal("1"),
        order_type="stop_limit",
        stop_price=Decimal("180"),  # missing limit_price
        client_order_id=_coid(),
        strategy_id="wheel",
    )
    with pytest.raises(ValueError):
        await ex.submit_order(proposal)


async def test_cancel_known_order_swallows_terminal_error() -> None:
    """If broker says 422 'already done', cancel must succeed silently."""
    cfg = AlpacaConfig(api_key="test-key", secret_key="test-secret", mode="paper")
    fake_client = MagicMock()
    fake_client.get_clock.return_value = object()
    fake_order = MagicMock()
    fake_order._asdict = lambda: {"id": "v1"}
    fake_client.get_order_by_client_id.return_value = fake_order
    fake_client.cancel_order_by_id.side_effect = RuntimeError("422 already canceled")
    ex = AlpacaExchange(cfg, client=fake_client)
    await ex.connect()
    await ex.cancel_order(_coid())  # MUST NOT raise


async def test_cancel_returns_when_no_venue_id() -> None:
    cfg = AlpacaConfig(api_key="test-key", secret_key="test-secret", mode="paper")
    fake_client = MagicMock()
    fake_client.get_clock.return_value = object()
    fake_order = MagicMock()
    fake_order._asdict = lambda: {"id": None}
    fake_client.get_order_by_client_id.return_value = fake_order
    ex = AlpacaExchange(cfg, client=fake_client)
    await ex.connect()
    await ex.cancel_order(_coid())
    fake_client.cancel_order_by_id.assert_not_called()


async def test_get_orders_filtered_by_terminal_status() -> None:
    cfg = AlpacaConfig(api_key="test-key", secret_key="test-secret", mode="paper")
    fake_client = MagicMock()
    fake_client.get_clock.return_value = object()
    open_order = MagicMock()
    open_order._asdict = lambda: {
        "id": "x1",
        "client_order_id": "qc4-alpaca-w-" + "0" * 32,
        "status": "new",
        "symbol": "AAPL",
        "side": "buy",
        "qty": "1",
        "filled_qty": "0",
        "asset_class": "us_equity",
    }
    filled_order = MagicMock()
    filled_order._asdict = lambda: {
        "id": "x2",
        "client_order_id": "qc4-alpaca-w-" + "0" * 31 + "1",
        "status": "filled",
        "symbol": "AAPL",
        "side": "buy",
        "qty": "1",
        "filled_qty": "1",
        "asset_class": "us_equity",
    }
    fake_client.get_orders.return_value = [open_order, filled_order]
    ex = AlpacaExchange(cfg, client=fake_client)
    await ex.connect()
    filled = await ex.get_orders(status="filled")
    assert len(filled) == 1
    assert filled[0].status == "filled"


def test_to_dict_handles_plain_dict() -> None:
    from quanta_core.exchanges.alpaca import _to_dict

    assert _to_dict({"a": 1}) == {"a": 1}


def test_to_dict_falls_back_to_value_for_scalars() -> None:
    from quanta_core.exchanges.alpaca import _to_dict

    # Use __slots__ object with no __dict__ to force the value path
    class Slotted:
        __slots__ = ()

    out = _to_dict(Slotted())
    assert "value" in out or out == {}


def test_stringify_enums_preserves_primitives() -> None:
    from quanta_core.exchanges.alpaca import _stringify_enums

    out = _stringify_enums({"a": 1, "b": "x", "c": True})
    assert out == {"a": 1, "b": "x", "c": True}


async def test_reconcile_orders_returns_open_set() -> None:
    cfg = AlpacaConfig(api_key="test-key", secret_key="test-secret", mode="paper")
    fake_client = MagicMock()
    fake_client.get_clock.return_value = object()
    # Build a fake order object that looks like alpaca-py's model
    fake_order = MagicMock()
    fake_order._asdict = lambda: {
        "id": "x",
        "client_order_id": "qc4-alpaca-w-" + "0" * 32,
        "status": "new",
        "symbol": "AAPL",
        "side": "buy",
        "qty": "1",
        "filled_qty": "0",
        "asset_class": "us_equity",
    }
    fake_client.get_orders.return_value = [fake_order]
    ex = AlpacaExchange(cfg, client=fake_client)
    await ex.connect()
    orders = await ex.reconcile_orders()
    assert len(orders) == 1
    assert orders[0].status == "open"
