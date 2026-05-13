"""Tests for src.quanta_core.observability.parity_oracle.compare_decisions."""
from __future__ import annotations

import pytest

from src.quanta_core.observability.parity_oracle import compare_decisions


def test_agreement_same_side_long() -> None:
    d = compare_decisions(
        freqtrade={"pair": "BTC/USD", "side": "LONG", "ts": "2026-05-13T01:00:00Z"},
        v4={"pair": "BTC/USD", "side": "LONG", "ts": "2026-05-13T01:00:05Z"},
    )
    assert d["verdict"] == "agree"
    assert d["pair"] == "BTC/USD"
    assert d["freqtrade_side"] == "LONG"
    assert d["v4_side"] == "LONG"


def test_agreement_same_side_flat() -> None:
    d = compare_decisions(
        freqtrade={"pair": "BTC/USD", "side": "FLAT", "ts": "..."},
        v4={"pair": "BTC/USD", "side": "FLAT", "ts": "..."},
    )
    assert d["verdict"] == "agree"


def test_conflict_opposite_side() -> None:
    d = compare_decisions(
        freqtrade={"pair": "BTC/USD", "side": "LONG", "ts": "..."},
        v4={"pair": "BTC/USD", "side": "SHORT", "ts": "..."},
    )
    assert d["verdict"] == "conflict"


def test_abstain_freqtrade_flat_v4_directional() -> None:
    d = compare_decisions(
        freqtrade={"pair": "BTC/USD", "side": "FLAT", "ts": "..."},
        v4={"pair": "BTC/USD", "side": "LONG", "ts": "..."},
    )
    assert d["verdict"] == "abstain"


def test_abstain_v4_flat_freqtrade_directional() -> None:
    d = compare_decisions(
        freqtrade={"pair": "BTC/USD", "side": "SHORT", "ts": "..."},
        v4={"pair": "BTC/USD", "side": "FLAT", "ts": "..."},
    )
    assert d["verdict"] == "abstain"


def test_pair_from_v4_when_freqtrade_missing() -> None:
    d = compare_decisions(
        freqtrade={"side": "LONG", "ts": "..."},
        v4={"pair": "ETH/USD", "side": "LONG", "ts": "..."},
    )
    assert d["pair"] == "ETH/USD"


def test_unknown_side_raises() -> None:
    with pytest.raises(ValueError, match="unknown side"):
        compare_decisions(
            freqtrade={"pair": "BTC/USD", "side": "MOON", "ts": "..."},
            v4={"pair": "BTC/USD", "side": "LONG", "ts": "..."},
        )


def test_missing_side_defaults_to_flat() -> None:
    """A missing side key is treated as FLAT (the sane no-decision default)."""
    d = compare_decisions(
        freqtrade={"pair": "BTC/USD", "ts": "..."},
        v4={"pair": "BTC/USD", "side": "FLAT", "ts": "..."},
    )
    assert d["verdict"] == "agree"
    assert d["freqtrade_side"] == "FLAT"


def test_returns_all_keys() -> None:
    d = compare_decisions(
        freqtrade={"pair": "BTC/USD", "side": "LONG", "ts": "..."},
        v4={"pair": "BTC/USD", "side": "SHORT", "ts": "..."},
    )
    assert set(d.keys()) == {"pair", "freqtrade_side", "v4_side", "verdict"}
