"""Property + unit tests for ``quanta_core.exchanges.idempotency``.

The whole point of this layer is deterministic, replay-safe order ids.
We use hypothesis to assert:

* Same intent → same id (deterministic).
* Different intent (any field) → different id (collision-free in practice).
* UUID7 hex prefix is monotonic for intents created in time order.
* The schema parses round-trip.
"""

from __future__ import annotations

import re
from decimal import Decimal

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from quanta_core.exchanges.idempotency import (
    InMemoryReservation,
    IntentKey,
    intent_from_proposal,
    make_client_order_id,
    parse_client_order_id,
    serialise_intent,
)

# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

VENUE = st.sampled_from(["alpaca", "coinbase"])
SIDE = st.sampled_from(["BUY", "SELL"])
STRATEGY_ID = st.from_regex(r"^[a-z0-9_]{1,8}$", fullmatch=True)
SYMBOL = st.text(
    alphabet="ABCDEFGHIJKLMNOPQRSTUVWXYZ/0123456789",
    min_size=1,
    max_size=10,
).filter(lambda s: s.strip() != "")
QTY = st.decimals(
    min_value=Decimal("0.00001"),
    max_value=Decimal("100000"),
    allow_nan=False,
    allow_infinity=False,
    places=5,
)
TS_MS = st.integers(min_value=0, max_value=(1 << 48) - 1)


@st.composite
def intent_keys(draw: st.DrawFn) -> IntentKey:
    return IntentKey(
        venue=draw(VENUE),
        strategy_id=draw(STRATEGY_ID),
        symbol=draw(SYMBOL),
        side=draw(SIDE),
        qty=draw(QTY),
        intent_timestamp_ms=draw(TS_MS),
    )


# ---------------------------------------------------------------------------
# Schema & format
# ---------------------------------------------------------------------------


_COID_FORMAT = re.compile(r"^qc4-(alpaca|coinbase)-[a-z0-9_]{1,8}-[0-9a-f]{32}$")


@given(intent_keys())
@settings(suppress_health_check=[HealthCheck.too_slow], max_examples=200)
def test_format_matches_schema(key: IntentKey) -> None:
    coid = make_client_order_id(key.strategy_id, key)
    assert _COID_FORMAT.match(coid), f"bad format: {coid}"
    parsed = parse_client_order_id(coid)
    assert parsed.venue == key.venue
    assert parsed.strategy_id == key.strategy_id
    assert len(parsed.uuid7_hex) == 32


# ---------------------------------------------------------------------------
# Determinism
# ---------------------------------------------------------------------------


@given(intent_keys())
@settings(suppress_health_check=[HealthCheck.too_slow], max_examples=200)
def test_same_intent_same_id(key: IntentKey) -> None:
    a = make_client_order_id(key.strategy_id, key)
    b = make_client_order_id(key.strategy_id, key)
    assert a == b, "same intent must yield same coid"


def test_same_intent_same_id_explicit() -> None:
    """Belt-and-braces concrete case in case hypothesis shrinks oddly."""
    key = IntentKey(
        venue="alpaca",
        strategy_id="wheel",
        symbol="AAPL",
        side="BUY",
        qty=Decimal("100"),
        intent_timestamp_ms=1_700_000_000_000,
    )
    assert make_client_order_id("wheel", key) == make_client_order_id("wheel", key)


# ---------------------------------------------------------------------------
# Collision-resistance
# ---------------------------------------------------------------------------


@given(intent_keys(), intent_keys())
@settings(suppress_health_check=[HealthCheck.too_slow], max_examples=200)
def test_different_intent_different_id(a: IntentKey, b: IntentKey) -> None:
    # Two intents are "different" only when at least one field differs.
    if (a.venue, a.strategy_id, a.symbol, a.side, a.qty.normalize(), a.intent_timestamp_ms) == (
        b.venue,
        b.strategy_id,
        b.symbol,
        b.side,
        b.qty.normalize(),
        b.intent_timestamp_ms,
    ):
        return  # equivalent intents — determinism test covers them
    coid_a = make_client_order_id(a.strategy_id, a)
    coid_b = make_client_order_id(b.strategy_id, b)
    assert coid_a != coid_b


def test_field_change_changes_id() -> None:
    """Each individual field must contribute to the id (smoke test)."""
    base = IntentKey(
        venue="alpaca",
        strategy_id="mr01",
        symbol="AAPL",
        side="BUY",
        qty=Decimal("10"),
        intent_timestamp_ms=1_700_000_000_000,
    )
    base_coid = make_client_order_id(base.strategy_id, base)

    mutations: list[IntentKey] = [
        IntentKey(
            venue="coinbase",
            strategy_id="mr01",
            symbol="AAPL",
            side="BUY",
            qty=Decimal("10"),
            intent_timestamp_ms=1_700_000_000_000,
        ),
        IntentKey(
            venue="alpaca",
            strategy_id="mr02",
            symbol="AAPL",
            side="BUY",
            qty=Decimal("10"),
            intent_timestamp_ms=1_700_000_000_000,
        ),
        IntentKey(
            venue="alpaca",
            strategy_id="mr01",
            symbol="TSLA",
            side="BUY",
            qty=Decimal("10"),
            intent_timestamp_ms=1_700_000_000_000,
        ),
        IntentKey(
            venue="alpaca",
            strategy_id="mr01",
            symbol="AAPL",
            side="SELL",
            qty=Decimal("10"),
            intent_timestamp_ms=1_700_000_000_000,
        ),
        IntentKey(
            venue="alpaca",
            strategy_id="mr01",
            symbol="AAPL",
            side="BUY",
            qty=Decimal("11"),
            intent_timestamp_ms=1_700_000_000_000,
        ),
        IntentKey(
            venue="alpaca",
            strategy_id="mr01",
            symbol="AAPL",
            side="BUY",
            qty=Decimal("10"),
            intent_timestamp_ms=1_700_000_000_001,
        ),
    ]
    for mut in mutations:
        assert make_client_order_id(mut.strategy_id, mut) != base_coid


# ---------------------------------------------------------------------------
# UUID7 monotonicity
# ---------------------------------------------------------------------------


@given(
    a_ts=st.integers(min_value=0, max_value=(1 << 47)),
    b_ts=st.integers(min_value=0, max_value=(1 << 47)),
)
@settings(suppress_health_check=[HealthCheck.too_slow], max_examples=200)
def test_uuid7_monotonic_prefix(a_ts: int, b_ts: int) -> None:
    """The 12-hex-char prefix of the uuid7 must order the same as the
    intent timestamps. This is the property that makes the coid sortable
    in the database."""
    key_a = IntentKey(
        venue="alpaca",
        strategy_id="mono",
        symbol="AAA",
        side="BUY",
        qty=Decimal("1"),
        intent_timestamp_ms=a_ts,
    )
    key_b = IntentKey(
        venue="alpaca",
        strategy_id="mono",
        symbol="AAA",
        side="BUY",
        qty=Decimal("1"),
        intent_timestamp_ms=b_ts,
    )
    coid_a = parse_client_order_id(make_client_order_id("mono", key_a)).uuid7_hex
    coid_b = parse_client_order_id(make_client_order_id("mono", key_b)).uuid7_hex
    # First 12 hex chars = 48 bits = millisecond timestamp.
    prefix_a = coid_a[:12]
    prefix_b = coid_b[:12]
    if a_ts < b_ts:
        assert prefix_a < prefix_b
    elif a_ts > b_ts:
        assert prefix_a > prefix_b
    else:
        assert prefix_a == prefix_b


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def test_rejects_bad_strategy_id() -> None:
    with pytest.raises(ValueError):
        IntentKey(
            venue="alpaca",
            strategy_id="HasCaps",
            symbol="AAPL",
            side="BUY",
            qty=Decimal("1"),
            intent_timestamp_ms=0,
        )
    with pytest.raises(ValueError):
        IntentKey(
            venue="alpaca",
            strategy_id="too_long_id",
            symbol="AAPL",
            side="BUY",
            qty=Decimal("1"),
            intent_timestamp_ms=0,
        )


def test_rejects_bad_venue() -> None:
    with pytest.raises(ValueError):
        IntentKey(
            venue="binance",
            strategy_id="x",
            symbol="X",  # type: ignore[arg-type]
            side="BUY",
            qty=Decimal("1"),
            intent_timestamp_ms=0,
        )


def test_rejects_negative_qty() -> None:
    with pytest.raises(ValueError):
        IntentKey(
            venue="alpaca",
            strategy_id="x",
            symbol="X",
            side="BUY",
            qty=Decimal("-1"),
            intent_timestamp_ms=0,
        )


def test_rejects_negative_ts() -> None:
    with pytest.raises(ValueError):
        IntentKey(
            venue="alpaca",
            strategy_id="x",
            symbol="X",
            side="BUY",
            qty=Decimal("1"),
            intent_timestamp_ms=-5,
        )


def test_rejects_bad_side() -> None:
    with pytest.raises(ValueError):
        IntentKey(
            venue="alpaca",
            strategy_id="x",
            symbol="X",
            side="HOLD",
            qty=Decimal("1"),
            intent_timestamp_ms=0,
        )  # type: ignore[arg-type]


def test_make_coid_strategy_must_match() -> None:
    key = IntentKey(
        venue="alpaca",
        strategy_id="abc",
        symbol="X",
        side="BUY",
        qty=Decimal("1"),
        intent_timestamp_ms=0,
    )
    with pytest.raises(ValueError):
        make_client_order_id("def", key)


def test_make_coid_invalid_strategy() -> None:
    key = IntentKey(
        venue="alpaca",
        strategy_id="abc",
        symbol="X",
        side="BUY",
        qty=Decimal("1"),
        intent_timestamp_ms=0,
    )
    with pytest.raises(ValueError):
        make_client_order_id("WITH-DASH", key)


def test_parse_rejects_garbage() -> None:
    with pytest.raises(ValueError):
        parse_client_order_id("not-a-coid")
    with pytest.raises(ValueError):
        parse_client_order_id("qc4-binance-x-deadbeef")


def test_raw_hash_form() -> None:
    """The fallback form (raw bytes + venue) builds a coid from a stored hash."""
    raw = bytes.fromhex("00000170" + "00" * 12)  # 16 bytes
    coid = make_client_order_id("wheel", raw, venue="alpaca")
    parsed = parse_client_order_id(coid)
    assert parsed.venue == "alpaca"
    assert parsed.strategy_id == "wheel"


def test_raw_hash_requires_venue() -> None:
    with pytest.raises(ValueError):
        make_client_order_id("wheel", b"\x00" * 16)


def test_raw_hash_too_short() -> None:
    with pytest.raises(ValueError):
        make_client_order_id("wheel", b"\x00" * 8, venue="alpaca")


# ---------------------------------------------------------------------------
# Reservation stub
# ---------------------------------------------------------------------------


def test_reservation_fresh_replay_duplicate() -> None:
    res = InMemoryReservation()
    coid = "qc4-alpaca-wheel-" + "a" * 32

    r1 = res.reserve(coid, {"qty": "1"})
    assert r1.kind == "fresh"

    r2 = res.reserve(coid, {"qty": "1"})
    assert r2.kind == "replay"
    assert r2.prior_payload == {"qty": "1"}

    res.commit(coid)
    r3 = res.reserve(coid, {"qty": "1"})
    assert r3.kind == "duplicate"


def test_reservation_commit_unknown_raises() -> None:
    res = InMemoryReservation()
    with pytest.raises(KeyError):
        res.commit("qc4-alpaca-x-" + "0" * 32)


def test_reservation_abandon_is_idempotent() -> None:
    res = InMemoryReservation()
    coid = "qc4-alpaca-x-" + "1" * 32
    res.reserve(coid, {})
    res.abandon(coid)
    res.abandon(coid)  # idempotent
    # Second reserve is fresh again.
    assert res.reserve(coid, {}).kind == "fresh"


def test_intent_from_proposal_helper() -> None:
    k = intent_from_proposal(
        venue="alpaca",
        strategy_id="mr01",
        symbol="AAPL",
        side="BUY",
        qty=Decimal("10"),
        intent_timestamp_ms=42,
    )
    assert k.intent_timestamp_ms == 42
    assert k.venue == "alpaca"
    assert k.symbol == "AAPL"


def test_intent_from_proposal_default_ts() -> None:
    k = intent_from_proposal(
        venue="coinbase",
        strategy_id="x",
        symbol="BTC/USD",
        side="SELL",
        qty=Decimal("0.001"),
    )
    assert k.intent_timestamp_ms > 0


def test_serialise_intent() -> None:
    k = IntentKey(
        venue="alpaca",
        strategy_id="x",
        symbol="A",
        side="BUY",
        qty=Decimal("0.10"),
        intent_timestamp_ms=10,
    )
    data = serialise_intent(k)
    assert data["qty"] == "0.1"
    assert data["intent_timestamp_ms"] == 10
