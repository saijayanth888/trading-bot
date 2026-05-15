"""
Circuit breaker for LLM provider failover (Ollama → Anthropic).

Three states:
  CLOSED    → all calls go to primary (Ollama). Healthy state.
  OPEN      → all calls go to fallback (Anthropic). Primary not contacted.
  HALF_OPEN → next call probes primary. Success → CLOSED, failure → OPEN.

Trip conditions
  - FAILURE_THRESHOLD consecutive failures (HTTP errors, timeouts, refused conns).
  - Rolling 1-min p95 latency exceeds the per-tier ceiling. Slow responses
    are functionally outages for live trading; we'd rather pay Anthropic
    than miss a trade window.

Reset
  - RECOVERY_TIMEOUT_S in OPEN, then transition to HALF_OPEN.
  - HALF_OPEN success → CLOSED.
  - HALF_OPEN failure → re-OPEN for another RECOVERY_TIMEOUT_S.

Persistence
  - State stored as JSON under SHARK_CB_DIR (default /tmp). Atomic write
    via tmp+rename. Cross-process readers see consistent state — important
    because shark phases can spawn subprocesses.

File naming
  - shark-cb-{name}.json. Colons in `name` are normalised to underscores
    so paths stay shell-friendly.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

logger = logging.getLogger(__name__)

_STATE_DIR = Path(os.environ.get("SHARK_CB_DIR", "/tmp"))
try:
    _STATE_DIR.mkdir(parents=True, exist_ok=True)
except OSError:
    pass

# Tier-specific p95 ceilings. Above this for ≥10 samples in the window
# the breaker trips even without errors.
LATENCY_P95_THRESHOLD_S = {
    "fast": float(os.environ.get("CB_FAST_LATENCY_THRESHOLD", "15.0")),
    "deep": float(os.environ.get("CB_DEEP_LATENCY_THRESHOLD", "60.0")),
}

FAILURE_THRESHOLD = int(os.environ.get("CB_FAILURE_THRESHOLD", "5"))
RECOVERY_TIMEOUT_S = float(os.environ.get("CB_RECOVERY_TIMEOUT", "60.0"))
LATENCY_WINDOW_SECONDS = 60
LATENCY_MIN_SAMPLES = 10  # don't trip on latency until we have N samples


class State(str, Enum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


@dataclass
class CircuitState:
    state: State = State.CLOSED
    failure_count: int = 0
    last_failure_ts: float = 0.0
    last_state_change_ts: float = field(default_factory=time.time)
    # (timestamp, latency_seconds) tuples — pruned to LATENCY_WINDOW_SECONDS
    latency_samples: list[tuple[float, float]] = field(default_factory=list)

    def to_json(self) -> dict:
        return {
            "state": self.state.value,
            "failure_count": self.failure_count,
            "last_failure_ts": self.last_failure_ts,
            "last_state_change_ts": self.last_state_change_ts,
            "latency_samples": self.latency_samples[-100:],
        }

    @classmethod
    def from_json(cls, data: dict) -> CircuitState:
        try:
            samples = [(float(t), float(l)) for t, l in (data.get("latency_samples") or [])]
        except (TypeError, ValueError):
            samples = []
        return cls(
            state=State(data.get("state", "closed")),
            failure_count=int(data.get("failure_count", 0)),
            last_failure_ts=float(data.get("last_failure_ts", 0.0)),
            last_state_change_ts=float(data.get("last_state_change_ts", time.time())),
            latency_samples=samples,
        )


class CircuitBreaker:
    """File-backed circuit breaker. Thread-safe within a process."""

    def __init__(self, name: str, tier: str = "deep"):
        self.name = name
        self.tier = tier
        # Normalise colons so paths stay shell-friendly (e.g. ollama:fast → ollama_fast)
        safe_name = name.replace(":", "_")
        self.state_file = _STATE_DIR / f"shark-cb-{safe_name}.json"
        self._lock = threading.Lock()

    # ── Persistence ────────────────────────────────────────────────────
    def _load(self) -> CircuitState:
        try:
            if self.state_file.is_file():
                return CircuitState.from_json(json.loads(self.state_file.read_text()))
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("CB[%s]: load failed (%s) — fresh state", self.name, exc)
        return CircuitState()

    def _save(self, state: CircuitState) -> None:
        try:
            tmp = self.state_file.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(state.to_json(), default=str))
            tmp.replace(self.state_file)
        except OSError as exc:
            logger.warning("CB[%s]: save failed: %s", self.name, exc)

    # ── Public API ─────────────────────────────────────────────────────
    def can_execute(self) -> tuple[bool, str]:
        """(allowed, reason) — caller logs the reason."""
        with self._lock:
            s = self._load()
            now = time.time()

            if s.state == State.CLOSED:
                return True, "closed"

            if s.state == State.OPEN:
                if now - s.last_state_change_ts >= RECOVERY_TIMEOUT_S:
                    # Auto-transition to HALF_OPEN
                    s.state = State.HALF_OPEN
                    s.last_state_change_ts = now
                    self._save(s)
                    logger.info("CB[%s]: OPEN → HALF_OPEN (probe time)", self.name)
                    return True, "half_open_probe"
                return False, "open"

            # HALF_OPEN: allow probe
            return True, "half_open_probe"

    def record_success(self, latency_seconds: float) -> None:
        with self._lock:
            s = self._load()
            now = time.time()

            # Add latency sample, prune
            s.latency_samples.append((now, float(latency_seconds)))
            cutoff = now - LATENCY_WINDOW_SECONDS
            s.latency_samples = [(t, l) for t, l in s.latency_samples if t >= cutoff]

            # Latency-based trip — slow responses are functional outages
            recent_latencies = [l for _, l in s.latency_samples]
            p95 = self._percentile(recent_latencies, 0.95)
            threshold = LATENCY_P95_THRESHOLD_S.get(self.tier, 60.0)
            if (
                p95 is not None
                and p95 > threshold
                and len(recent_latencies) >= LATENCY_MIN_SAMPLES
            ):
                logger.warning(
                    "CB[%s]: latency trip — p95=%.1fs > threshold=%.1fs (n=%d)",
                    self.name, p95, threshold, len(recent_latencies),
                )
                s.state = State.OPEN
                s.last_state_change_ts = now
                s.failure_count = FAILURE_THRESHOLD  # mark as fully failed
                self._save(s)
                return

            # Successful call — close if half-open, reset failure count
            if s.state == State.HALF_OPEN:
                logger.info("CB[%s]: HALF_OPEN probe ok → CLOSED", self.name)
                s.state = State.CLOSED
                s.last_state_change_ts = now
            s.failure_count = 0
            self._save(s)

    def record_failure(self, error: str) -> None:
        with self._lock:
            s = self._load()
            now = time.time()
            s.failure_count += 1
            s.last_failure_ts = now

            if s.state == State.HALF_OPEN:
                logger.warning("CB[%s]: HALF_OPEN probe failed → OPEN", self.name)
                s.state = State.OPEN
                s.last_state_change_ts = now
            elif s.state == State.CLOSED and s.failure_count >= FAILURE_THRESHOLD:
                logger.warning(
                    "CB[%s]: %d consecutive failures → OPEN (last: %s)",
                    self.name, s.failure_count, str(error)[:200],
                )
                s.state = State.OPEN
                s.last_state_change_ts = now

            self._save(s)

    def get_status(self) -> dict:
        with self._lock:
            s = self._load()
            now = time.time()
            recent = [l for t, l in s.latency_samples if t >= now - LATENCY_WINDOW_SECONDS]
            return {
                "name": self.name,
                "tier": self.tier,
                "state": s.state.value,
                "failure_count": s.failure_count,
                "last_failure_ago_seconds": (
                    int(now - s.last_failure_ts) if s.last_failure_ts else None
                ),
                "in_state_seconds": int(now - s.last_state_change_ts),
                "p50_latency_s": (
                    round(self._percentile(recent, 0.5), 2) if recent else None
                ),
                "p95_latency_s": (
                    round(self._percentile(recent, 0.95), 2) if recent else None
                ),
                "threshold_s": LATENCY_P95_THRESHOLD_S.get(self.tier, 60.0),
                "samples_in_window": len(recent),
            }

    # ── Helpers ────────────────────────────────────────────────────────
    @staticmethod
    def _percentile(samples: list[float], p: float) -> float | None:
        if not samples:
            return None
        s = sorted(samples)
        idx = min(int(len(s) * p), len(s) - 1)
        return s[idx]


# ──────────────────────────────────────────────────────────────────────
# Singleton registry — one breaker per (provider:tier) name
# ──────────────────────────────────────────────────────────────────────

_breakers: dict[str, CircuitBreaker] = {}
_registry_lock = threading.Lock()


def get_breaker(name: str, tier: str = "deep") -> CircuitBreaker:
    with _registry_lock:
        if name not in _breakers:
            _breakers[name] = CircuitBreaker(name, tier=tier)
        return _breakers[name]


def get_all_status() -> list[dict]:
    """Snapshot of every breaker known to this process. The dashboard's
    /api/ops/circuit_breakers endpoint can also discover breakers from disk
    by listing shark-cb-*.json files — useful when the dashboard process
    didn't create them itself."""
    with _registry_lock:
        return [b.get_status() for b in _breakers.values()]


def discover_from_disk() -> list[dict]:
    """Discover breakers from on-disk state files. Used by the dashboard
    side, which doesn't share an in-process registry with shark."""
    out = []
    try:
        for f in _STATE_DIR.glob("shark-cb-*.json"):
            try:
                data = json.loads(f.read_text())
            except (OSError, json.JSONDecodeError):
                continue
            name = f.stem.replace("shark-cb-", "").replace("_", ":", 1)
            tier = "fast" if "fast" in name else "deep"
            cb = CircuitBreaker(name, tier=tier)
            out.append(cb.get_status())
    except OSError:
        pass
    return out
