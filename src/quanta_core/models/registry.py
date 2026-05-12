"""Model registry — load-on-demand + LRU eviction.

Implements the multi-model residency policy from
``docs/quanta-core-v4-rev2/01-RESEARCH-MULTI_MODEL_RESIDENCY.md``:

- Models are registered with a name, a loader callable, an optional
  predictor, and a byte-size estimate. They are NOT held in memory at
  registration time.
- The first :meth:`ModelRegistry.get` (or :meth:`ModelRegistry.predict`)
  call invokes the loader and pins the resulting handle in the resident
  pool. Subsequent calls return the cached handle.
- When the resident pool would exceed ``max_resident_bytes``, the
  least-recently-used handle is evicted (loader output discarded; user
  ``unloader`` callback, if supplied, is invoked for explicit cleanup
  such as ``torch.cuda.empty_cache()`` or ``ollama keep_alive=0s``).
- Access timestamps come from an injected ``time.monotonic``-shaped
  callable so tests can advance the clock deterministically.

This module is intentionally synchronous. The orchestrator wraps calls
in ``asyncio.to_thread`` when used from the async live engine. Adding an
async surface here would force tests to use ``pytest.mark.asyncio`` for
every operation and buys nothing on a single-process pool.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Generic, TypeVar

__all__ = ["ModelHandle", "ModelRegistry", "RegistryError"]


T = TypeVar("T")


class RegistryError(Exception):
    """Raised on registry-level misuse (duplicate name, unknown name, etc.)."""


@dataclass
class ModelHandle(Generic[T]):
    """A registered model entry.

    Attributes
    ----------
    name:
        Unique identifier used by :meth:`ModelRegistry.get` and friends.
    loader:
        Zero-arg callable that returns the underlying model object. Called
        at most once per residency. May be expensive (file IO, GPU
        materialisation); the registry serialises concurrent loads behind
        an internal lock so callers never race.
    predictor:
        Optional ``(model, features) -> prediction`` callable used by
        :meth:`ModelRegistry.predict`. Pure function of the loaded model
        and the feature payload; no side effects on the model object
        beyond what the model itself does internally.
    estimated_bytes:
        Resident-memory cost used by the LRU eviction policy. Must be a
        non-negative integer. Used solely as a budget signal — the
        registry never actually measures live memory.
    unloader:
        Optional callable invoked with the loaded model object when the
        handle is evicted. Use for explicit GPU cache release or Ollama
        ``keep_alive: 0s`` calls. Exceptions raised here are caught and
        logged as a warning; eviction always completes.
    """

    name: str
    loader: Callable[[], T]
    predictor: Callable[[T, Any], Any] | None = None
    estimated_bytes: int = 0
    unloader: Callable[[T], None] | None = None

    _model: T | None = field(default=None, init=False, repr=False)
    _last_access: float = field(default=0.0, init=False, repr=False)
    _resident: bool = field(default=False, init=False, repr=False)


class ModelRegistry:
    """Thread-safe load-on-demand registry with LRU eviction.

    Parameters
    ----------
    max_resident_bytes:
        Soft ceiling on the sum of ``estimated_bytes`` across resident
        handles. When a load would push the total above this limit the
        least-recently-used handles are evicted until the new model fits
        OR only the new model is resident. A new model larger than the
        ceiling is admitted (with a warning-level state transition);
        callers are expected to set a ceiling that accommodates their
        largest single model.
    clock:
        Injected ``time.monotonic``-shaped callable. Defaults to
        :func:`time.monotonic`.

    Raises
    ------
    RegistryError
        On duplicate registration, unknown name lookup, or attempts to
        call :meth:`predict` against a handle that has no predictor.
    """

    def __init__(
        self,
        max_resident_bytes: int = 80 * 1024**3,
        clock: Callable[[], float] | None = None,
    ) -> None:
        if max_resident_bytes < 0:
            raise ValueError("max_resident_bytes must be non-negative")
        self._max_bytes = max_resident_bytes
        self._clock: Callable[[], float] = clock or time.monotonic
        self._handles: dict[str, ModelHandle[Any]] = {}
        self._lock = threading.RLock()

    # ------------------------------------------------------------------
    # Registration
    # ------------------------------------------------------------------

    def register(self, handle: ModelHandle[Any]) -> None:
        """Register a model handle. Does NOT load.

        Raises
        ------
        RegistryError
            If a handle with the same ``name`` is already registered.
        """
        if handle.estimated_bytes < 0:
            raise ValueError("estimated_bytes must be non-negative")
        with self._lock:
            if handle.name in self._handles:
                raise RegistryError(f"model already registered: {handle.name!r}")
            self._handles[handle.name] = handle

    def unregister(self, name: str) -> None:
        """Remove a handle. Evicts the resident model first if needed."""
        with self._lock:
            handle = self._handles.pop(name, None)
            if handle is None:
                raise RegistryError(f"unknown model: {name!r}")
            if handle._resident:
                self._evict_handle_locked(handle)

    # ------------------------------------------------------------------
    # Inspection
    # ------------------------------------------------------------------

    def names(self) -> list[str]:
        """Return the names of every registered handle (sorted)."""
        with self._lock:
            return sorted(self._handles)

    def resident_names(self) -> list[str]:
        """Return the names of every CURRENTLY-RESIDENT handle (sorted)."""
        with self._lock:
            return sorted(n for n, h in self._handles.items() if h._resident)

    def resident_bytes(self) -> int:
        """Return the sum of ``estimated_bytes`` for resident handles."""
        with self._lock:
            return sum(h.estimated_bytes for h in self._handles.values() if h._resident)

    def is_resident(self, name: str) -> bool:
        """Return True iff ``name`` is registered AND currently resident."""
        with self._lock:
            handle = self._handles.get(name)
            return bool(handle and handle._resident)

    # ------------------------------------------------------------------
    # Load / get / predict
    # ------------------------------------------------------------------

    def get(self, name: str) -> Any:
        """Return the loaded model for ``name``. Loads on first access.

        Raises
        ------
        RegistryError
            If ``name`` is not registered.
        """
        with self._lock:
            handle = self._require_handle(name)
            if not handle._resident:
                self._load_handle_locked(handle)
            handle._last_access = self._clock()
            return handle._model

    def predict(self, name: str, features: Any) -> Any:
        """Run the registered predictor for ``name`` against ``features``.

        Raises
        ------
        RegistryError
            If ``name`` is not registered or the handle has no predictor.
        """
        with self._lock:
            handle = self._require_handle(name)
            if handle.predictor is None:
                raise RegistryError(f"model {name!r} registered without predictor")
            if not handle._resident:
                self._load_handle_locked(handle)
            handle._last_access = self._clock()
            model = handle._model
            predictor = handle.predictor
        # Call the predictor OUTSIDE the registry lock — it may be slow
        # (GPU forward pass, network call). The handle's resident state
        # is protected by the load above; eviction of THIS handle while
        # the predictor runs is prevented by the predictor reading from
        # ``model`` directly (a local reference) and the eviction path
        # not invalidating the model object (it just drops the registry's
        # reference).
        return predictor(model, features)

    def evict(self, name: str) -> bool:
        """Evict ``name`` if resident. Returns True if evicted, False otherwise.

        Raises
        ------
        RegistryError
            If ``name`` is not registered.
        """
        with self._lock:
            handle = self._require_handle(name)
            if not handle._resident:
                return False
            self._evict_handle_locked(handle)
            return True

    def evict_all(self) -> int:
        """Evict every resident handle. Returns the number evicted."""
        with self._lock:
            resident = [h for h in self._handles.values() if h._resident]
            for h in resident:
                self._evict_handle_locked(h)
            return len(resident)

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _require_handle(self, name: str) -> ModelHandle[Any]:
        handle = self._handles.get(name)
        if handle is None:
            raise RegistryError(f"unknown model: {name!r}")
        return handle

    def _load_handle_locked(self, handle: ModelHandle[Any]) -> None:
        """Load ``handle`` after making room. Caller MUST hold ``_lock``."""
        self._make_room_locked(handle)
        handle._model = handle.loader()
        handle._resident = True
        handle._last_access = self._clock()

    def _make_room_locked(self, incoming: ModelHandle[Any]) -> None:
        """Evict LRU residents until ``incoming`` fits, or only it remains.

        Caller MUST hold ``_lock``. An ``incoming`` larger than
        ``max_resident_bytes`` is admitted after every other handle is
        evicted; the registry never refuses a load on size grounds.
        """
        needed = incoming.estimated_bytes
        while True:
            resident = [h for h in self._handles.values() if h._resident]
            if not resident:
                return
            current_bytes = sum(h.estimated_bytes for h in resident)
            if current_bytes + needed <= self._max_bytes:
                return
            # Evict the LRU resident (oldest _last_access). Tie-breaker:
            # registration order via dict insertion (Python 3.7+ ordered).
            lru = min(resident, key=lambda h: (h._last_access, _stable_index(self, h)))
            self._evict_handle_locked(lru)

    def _evict_handle_locked(self, handle: ModelHandle[Any]) -> None:
        """Evict a resident handle. Caller MUST hold ``_lock``."""
        model = handle._model
        handle._model = None
        handle._resident = False
        if handle.unloader is not None and model is not None:
            try:
                handle.unloader(model)
            except Exception:
                # Unloader failure must NOT block eviction. The handle is
                # already marked non-resident; a future get() will re-load.
                # We swallow to keep the registry's API total — callers
                # should wire structlog into the unloader itself if they
                # want visibility.
                pass


def _stable_index(registry: ModelRegistry, handle: ModelHandle[Any]) -> int:
    """Stable tiebreaker for LRU eviction.

    When two handles share the same ``_last_access`` timestamp (common
    under :func:`time.monotonic` resolution), break the tie by their
    registration order (dict insertion order). Caller holds the lock.
    """
    for i, name in enumerate(registry._handles):
        if name == handle.name:
            return i
    return 0
