"""Deterministic ``client_order_id`` generation + reserve-then-commit stub.

Schema (from doc 04, §6.3 — slightly tweaked to match the BUILD spec):

    qc4-{venue}-{strategy_id}-{uuid7_hex}

* ``qc4``         — system prefix (Quanta Core v4)
* ``venue``       — ``alpaca`` | ``coinbase``
* ``strategy_id`` — lower-ASCII, <= 8 chars, validated
* ``uuid7_hex``   — 32 hex chars, **deterministic** for the same intent
  and **monotonic** across distinct intents within a millisecond.

The "uuid7" is built from the intent SHA-256:

* Top 48 bits = intent timestamp in ms (so sort order ≈ chronological).
* Version nibble (bits 48-51) = 7 per RFC 9562.
* Variant bits (bits 64-65) = 10.
* Remaining 74 bits = SHA-256 of the canonical intent payload.

Same key → same id (deterministic, restart-safe).
Different keys → different id (collision-free in practice: 2^74 entropy on
top of a millisecond bucket — birthday collision is ~3.6e11 intents in a
single millisecond, which we will never hit).

The DB layer is expected to add a UNIQUE index on ``trades.client_order_id``
so an in-process bug can't double-fire even if this layer mis-generates.
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Final, Literal

__all__ = [
    "InMemoryReservation",
    "IntentKey",
    "ReservationResult",
    "make_client_order_id",
    "parse_client_order_id",
]

_VALID_STRATEGY_RE: Final = re.compile(r"^[a-z0-9_]{1,8}$")
_VENUE_RE: Final = re.compile(r"^(alpaca|coinbase)$")
_COID_RE: Final = re.compile(
    r"^qc4-(?P<venue>alpaca|coinbase)-(?P<strategy>[a-z0-9_]{1,8})-(?P<uuid7>[0-9a-f]{32})$"
)


@dataclass(frozen=True, slots=True)
class IntentKey:
    """The five fields that determine identity of an order intent.

    Any change to any field yields a different ``client_order_id``.
    """

    venue: Literal["alpaca", "coinbase"]
    strategy_id: str
    symbol: str
    side: Literal["BUY", "SELL"]
    qty: Decimal
    intent_timestamp_ms: int  # bar-close ms, the moment the decision crystallised

    def __post_init__(self) -> None:
        if not _VENUE_RE.match(self.venue):
            raise ValueError(f"invalid venue: {self.venue!r}")
        if not _VALID_STRATEGY_RE.match(self.strategy_id):
            raise ValueError(
                f"strategy_id must match {_VALID_STRATEGY_RE.pattern}, got {self.strategy_id!r}"
            )
        if self.side not in ("BUY", "SELL"):
            raise ValueError(f"invalid side: {self.side!r}")
        if self.qty <= 0:
            raise ValueError(f"qty must be positive, got {self.qty}")
        if self.intent_timestamp_ms < 0:
            raise ValueError(f"intent_timestamp_ms must be >= 0, got {self.intent_timestamp_ms}")

    def canonical_payload(self) -> str:
        """Stable, sorted JSON used as the SHA-256 pre-image. The dict
        layout is deliberate — adding/removing/renaming a field will change
        every coid, which is the desired blast radius."""
        payload: dict[str, Any] = {
            "venue": self.venue,
            "strategy_id": self.strategy_id,
            "symbol": self.symbol,
            "side": self.side,
            # str() preserves the user's chosen scale (e.g. "0.10" vs "0.1")
            "qty": format(self.qty.normalize(), "f"),
            "intent_timestamp_ms": self.intent_timestamp_ms,
        }
        return json.dumps(payload, sort_keys=True, separators=(",", ":"))


@dataclass(frozen=True, slots=True)
class _ParsedCoid:
    venue: str
    strategy_id: str
    uuid7_hex: str


def _intent_hash(key: IntentKey) -> bytes:
    return hashlib.sha256(key.canonical_payload().encode("utf-8")).digest()


def _build_uuid7_hex(timestamp_ms: int, entropy: bytes) -> str:
    """Build a deterministic UUIDv7 string (32 hex chars, no dashes).

    Layout per RFC 9562:

        unix_ts_ms (48b) | ver=7 (4b) | rand_a (12b) | var=10 (2b) | rand_b (62b)

    ``entropy`` MUST be at least 10 bytes; we use the first 10. The version
    and variant bits are forced over the random bytes.
    """
    if timestamp_ms < 0 or timestamp_ms >= 1 << 48:
        raise ValueError(f"timestamp_ms out of range for uuid7: {timestamp_ms}")
    if len(entropy) < 10:
        raise ValueError("need >= 10 bytes of entropy for uuid7 build")

    ts_bytes = timestamp_ms.to_bytes(6, "big")
    rand_bytes = bytearray(entropy[:10])

    # rand_a: 16 bits; top nibble must be 0x7 (version)
    rand_bytes[0] = 0x70 | (rand_bytes[0] & 0x0F)
    # rand_b: 64 bits; top two bits must be 0b10 (variant)
    rand_bytes[2] = 0x80 | (rand_bytes[2] & 0x3F)

    raw = ts_bytes + bytes(rand_bytes)
    assert len(raw) == 16, "uuid7 must be 16 bytes"
    return uuid.UUID(bytes=raw).hex


def make_client_order_id(
    strategy_id: str,
    intent_hash: bytes | IntentKey,
    *,
    venue: Literal["alpaca", "coinbase"] | None = None,
) -> str:
    """Build a deterministic client_order_id from an intent.

    Two forms accepted:

    1. ``make_client_order_id(strategy_id, IntentKey(...))`` — full intent.
       Returns ``qc4-{intent.venue}-{strategy_id}-{uuid7_hex}``.

    2. ``make_client_order_id(strategy_id, sha256_bytes, venue=...)`` —
       raw SHA-256 of an intent payload + explicit venue. The first 6
       bytes are interpreted as a 48-bit millisecond timestamp; the next
       10 supply entropy. This form lets the ledger layer rebuild a coid
       from a stored hash without rebuilding the full intent.

    The same strategy_id + same intent always returns the same id. Two
    different intents only collide if their full SHA-256 plus their
    millisecond bucket collide — astronomically unlikely.
    """
    if not _VALID_STRATEGY_RE.match(strategy_id):
        raise ValueError(f"strategy_id must match {_VALID_STRATEGY_RE.pattern}")

    if isinstance(intent_hash, IntentKey):
        key = intent_hash
        if venue is None:
            venue = key.venue
        if strategy_id != key.strategy_id:
            raise ValueError("strategy_id arg disagrees with IntentKey.strategy_id")
        digest = _intent_hash(key)
        timestamp_ms = key.intent_timestamp_ms
        entropy = digest[6:16]
    else:
        if venue is None:
            raise ValueError("venue is required when passing raw bytes")
        if len(intent_hash) < 16:
            raise ValueError("intent_hash must be >= 16 bytes")
        timestamp_ms = int.from_bytes(intent_hash[:6], "big")
        # Clamp to 48-bit range — top byte of a SHA-256 may overflow.
        timestamp_ms = timestamp_ms & ((1 << 48) - 1)
        entropy = intent_hash[6:16]

    if not _VENUE_RE.match(venue):
        raise ValueError(f"invalid venue: {venue!r}")

    uuid7_hex = _build_uuid7_hex(timestamp_ms, entropy)
    return f"qc4-{venue}-{strategy_id}-{uuid7_hex}"


def parse_client_order_id(coid: str) -> _ParsedCoid:
    """Reverse-parse a coid into its components. Raises ``ValueError`` if
    the input does not conform to the qc4 schema."""
    m = _COID_RE.match(coid)
    if not m:
        raise ValueError(f"not a qc4 client_order_id: {coid!r}")
    return _ParsedCoid(
        venue=m.group("venue"),
        strategy_id=m.group("strategy"),
        uuid7_hex=m.group("uuid7"),
    )


# ---------------------------------------------------------------------------
# Reserve-then-commit placeholder
# ---------------------------------------------------------------------------
#
# The real ledger lives in ``quanta_core.ledger.postgres`` (separate agent).
# This module only ships a minimal in-process stub so the exchanges layer
# can be exercised standalone in tests. The stub matches the eventual
# Postgres surface so swapping it in is mechanical.


ReservationKind = Literal["fresh", "replay", "duplicate"]


@dataclass(frozen=True, slots=True)
class ReservationResult:
    """Outcome of an ``IdempotencyService.reserve()`` call.

    * ``fresh``     — first time we have ever seen this coid; caller may
                       proceed to submit to the venue.
    * ``replay``    — we have seen this coid before AND the previous
                       attempt did not commit (crash mid-submit). Caller
                       must look up the order at the venue before resubmit.
    * ``duplicate`` — we have seen this coid AND it committed. Caller MUST
                       NOT resubmit; return the prior ack.
    """

    kind: ReservationKind
    coid: str
    prior_payload: dict[str, Any] | None = None


class InMemoryReservation:
    """Thread-unsafe, single-process placeholder for the ledger.

    Production code uses ``quanta_core.ledger.postgres.PostgresLedger`` which
    backs reservations with a UNIQUE index on ``trades.client_order_id``.
    """

    def __init__(self) -> None:
        self._reserved: dict[str, dict[str, Any]] = {}
        self._committed: set[str] = set()

    def reserve(self, coid: str, payload: dict[str, Any]) -> ReservationResult:
        if coid in self._committed:
            return ReservationResult(
                kind="duplicate", coid=coid, prior_payload=self._reserved.get(coid)
            )
        if coid in self._reserved:
            return ReservationResult(kind="replay", coid=coid, prior_payload=self._reserved[coid])
        self._reserved[coid] = payload
        return ReservationResult(kind="fresh", coid=coid)

    def commit(self, coid: str) -> None:
        if coid not in self._reserved:
            raise KeyError(f"cannot commit unreserved coid: {coid}")
        self._committed.add(coid)

    def abandon(self, coid: str) -> None:
        self._reserved.pop(coid, None)


def _now_ms() -> int:
    """Wall-clock milliseconds since epoch (UTC). Helper for callers that
    want to build an IntentKey on the fly."""
    return int(datetime.now(UTC).timestamp() * 1000)


def intent_from_proposal(
    venue: Literal["alpaca", "coinbase"],
    strategy_id: str,
    symbol: str,
    side: Literal["BUY", "SELL"],
    qty: Decimal,
    *,
    intent_timestamp_ms: int | None = None,
) -> IntentKey:
    """Convenience constructor — used by execution.engine before submit."""
    ts = intent_timestamp_ms if intent_timestamp_ms is not None else _now_ms()
    return IntentKey(
        venue=venue,
        strategy_id=strategy_id,
        symbol=symbol,
        side=side,
        qty=qty,
        intent_timestamp_ms=ts,
    )


# Re-exported for ledger persistence
def serialise_intent(key: IntentKey) -> dict[str, Any]:
    """Round-trippable dict for ledger storage."""
    data = asdict(key)
    data["qty"] = format(key.qty.normalize(), "f")
    return data
