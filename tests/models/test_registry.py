"""Tests for :mod:`quanta_core.models.registry`."""

from __future__ import annotations

import threading
from typing import Any

import pytest

from quanta_core.models.registry import (
    ModelHandle,
    ModelRegistry,
    RegistryError,
)


class _Clock:
    """Deterministic clock for LRU tests."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _counting_loader(value: object) -> tuple[Any, list[int]]:
    """Return ``(loader, calls_counter)`` for assertion."""
    calls = [0]

    def loader() -> object:
        calls[0] += 1
        return value

    return loader, calls


def test_register_then_get_loads_on_demand() -> None:
    clock = _Clock()
    registry = ModelRegistry(max_resident_bytes=1024, clock=clock)
    loader, calls = _counting_loader("model-a")
    registry.register(
        ModelHandle(name="a", loader=loader, estimated_bytes=10),
    )

    assert registry.resident_names() == []
    assert calls[0] == 0

    got = registry.get("a")

    assert got == "model-a"
    assert calls[0] == 1
    assert registry.resident_names() == ["a"]
    assert registry.is_resident("a")

    # Subsequent get() must NOT re-invoke the loader.
    again = registry.get("a")
    assert again == "model-a"
    assert calls[0] == 1


def test_duplicate_register_raises() -> None:
    registry = ModelRegistry()
    loader, _ = _counting_loader("x")
    registry.register(ModelHandle(name="dup", loader=loader))
    with pytest.raises(RegistryError, match="already registered"):
        registry.register(ModelHandle(name="dup", loader=loader))


def test_unknown_get_raises() -> None:
    registry = ModelRegistry()
    with pytest.raises(RegistryError, match="unknown model"):
        registry.get("missing")


def test_predict_calls_registered_predictor() -> None:
    registry = ModelRegistry()
    loader, _ = _counting_loader({"weights": 1})

    def predictor(model: dict[str, int], features: int) -> int:
        return model["weights"] + features

    registry.register(
        ModelHandle(name="p", loader=loader, predictor=predictor, estimated_bytes=5),
    )
    assert registry.predict("p", 41) == 42


def test_predict_without_predictor_raises() -> None:
    registry = ModelRegistry()
    loader, _ = _counting_loader("x")
    registry.register(ModelHandle(name="nopred", loader=loader))
    with pytest.raises(RegistryError, match="without predictor"):
        registry.predict("nopred", 1)


def test_lru_evicts_oldest_when_budget_exceeded() -> None:
    clock = _Clock()
    registry = ModelRegistry(max_resident_bytes=100, clock=clock)

    def make_loader(name: str) -> Any:
        loader, _ = _counting_loader(name)
        return loader

    # Three handles, each 40 bytes — only two fit under a 100-byte cap.
    registry.register(ModelHandle(name="a", loader=make_loader("A"), estimated_bytes=40))
    registry.register(ModelHandle(name="b", loader=make_loader("B"), estimated_bytes=40))
    registry.register(ModelHandle(name="c", loader=make_loader("C"), estimated_bytes=40))

    registry.get("a")
    clock.advance(1.0)
    registry.get("b")
    clock.advance(1.0)
    # a was touched first; loading c should evict a (LRU).
    registry.get("c")

    assert sorted(registry.resident_names()) == ["b", "c"]
    assert not registry.is_resident("a")


def test_re_get_after_eviction_reloads() -> None:
    clock = _Clock()
    registry = ModelRegistry(max_resident_bytes=50, clock=clock)
    load_a, calls_a = _counting_loader("A")
    load_b, _ = _counting_loader("B")
    registry.register(ModelHandle(name="a", loader=load_a, estimated_bytes=30))
    registry.register(ModelHandle(name="b", loader=load_b, estimated_bytes=30))

    registry.get("a")
    assert calls_a[0] == 1
    clock.advance(1.0)
    registry.get("b")  # evicts a
    assert "a" not in registry.resident_names()

    clock.advance(1.0)
    registry.get("a")  # reload — loader runs a second time
    assert calls_a[0] == 2


def test_get_after_evict_explicit() -> None:
    registry = ModelRegistry(max_resident_bytes=1000)
    loader, calls = _counting_loader("X")
    registry.register(ModelHandle(name="x", loader=loader, estimated_bytes=10))
    registry.get("x")
    assert registry.evict("x") is True
    assert registry.evict("x") is False  # already evicted
    registry.get("x")
    assert calls[0] == 2


def test_evict_calls_unloader() -> None:
    registry = ModelRegistry()
    seen: list[str] = []

    def unload(model: str) -> None:
        seen.append(model)

    loader, _ = _counting_loader("M")
    registry.register(
        ModelHandle(name="m", loader=loader, estimated_bytes=10, unloader=unload),
    )
    registry.get("m")
    registry.evict("m")
    assert seen == ["M"]


def test_unloader_exception_does_not_block_eviction() -> None:
    registry = ModelRegistry()

    def bad_unload(_model: str) -> None:
        raise RuntimeError("boom")

    loader, _ = _counting_loader("M")
    registry.register(
        ModelHandle(name="m", loader=loader, estimated_bytes=10, unloader=bad_unload),
    )
    registry.get("m")
    assert registry.evict("m") is True
    assert "m" not in registry.resident_names()


def test_resident_bytes_total() -> None:
    registry = ModelRegistry(max_resident_bytes=1000)
    for name, size in (("a", 100), ("b", 200), ("c", 300)):
        loader, _ = _counting_loader(name.upper())
        registry.register(ModelHandle(name=name, loader=loader, estimated_bytes=size))
    assert registry.resident_bytes() == 0
    registry.get("a")
    registry.get("b")
    assert registry.resident_bytes() == 300


def test_names_returns_sorted() -> None:
    registry = ModelRegistry()
    for name in ("zeta", "alpha", "mu"):
        loader, _ = _counting_loader(name)
        registry.register(ModelHandle(name=name, loader=loader))
    assert registry.names() == ["alpha", "mu", "zeta"]


def test_unregister_evicts_first() -> None:
    registry = ModelRegistry()
    seen: list[str] = []

    def unload(model: str) -> None:
        seen.append(model)

    loader, _ = _counting_loader("M")
    registry.register(
        ModelHandle(name="m", loader=loader, estimated_bytes=10, unloader=unload),
    )
    registry.get("m")
    registry.unregister("m")
    assert seen == ["M"]
    assert "m" not in registry.names()


def test_unregister_unknown_raises() -> None:
    registry = ModelRegistry()
    with pytest.raises(RegistryError, match="unknown model"):
        registry.unregister("missing")


def test_evict_all() -> None:
    registry = ModelRegistry(max_resident_bytes=1000)
    for name in ("a", "b", "c"):
        loader, _ = _counting_loader(name.upper())
        registry.register(ModelHandle(name=name, loader=loader, estimated_bytes=50))
        registry.get(name)
    evicted = registry.evict_all()
    assert evicted == 3
    assert registry.resident_names() == []


def test_oversized_model_admitted_after_evictions() -> None:
    """A model larger than the budget is admitted after evicting others."""
    registry = ModelRegistry(max_resident_bytes=100)
    small_load, _ = _counting_loader("small")
    big_load, _ = _counting_loader("big")
    registry.register(ModelHandle(name="small", loader=small_load, estimated_bytes=50))
    registry.register(ModelHandle(name="big", loader=big_load, estimated_bytes=500))

    registry.get("small")
    registry.get("big")  # forces eviction of small even though big > budget
    assert registry.resident_names() == ["big"]


def test_negative_max_bytes_rejected() -> None:
    with pytest.raises(ValueError):
        ModelRegistry(max_resident_bytes=-1)


def test_negative_estimated_bytes_rejected() -> None:
    registry = ModelRegistry()
    loader, _ = _counting_loader("x")
    with pytest.raises(ValueError):
        registry.register(ModelHandle(name="x", loader=loader, estimated_bytes=-1))


def test_thread_safety_concurrent_get() -> None:
    """Concurrent get() calls must not double-load the same handle."""
    registry = ModelRegistry(max_resident_bytes=10_000)
    barrier = threading.Barrier(8)
    call_count = [0]
    call_lock = threading.Lock()

    def slow_loader() -> str:
        with call_lock:
            call_count[0] += 1
        return "M"

    registry.register(
        ModelHandle(name="shared", loader=slow_loader, estimated_bytes=10),
    )

    def worker() -> None:
        barrier.wait()
        assert registry.get("shared") == "M"

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # All 8 workers see the loaded model; loader ran exactly once.
    assert call_count[0] == 1
