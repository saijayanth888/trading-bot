"""
Ollama health monitor — production probe service.

Tracks
  - Endpoint reachability (HTTP 200 from /api/tags)
  - Required-model availability (configured fast + deep models loaded)
  - Inference latency (probes /api/generate; cheaper than full chat)

Alerts (deduped per-status to avoid Slack spam)
  - 3 consecutive failures → CRITICAL
  - Required model missing → WARNING
  - Probe latency over a threshold → WARNING

Persists status to /tmp/ollama-health.json so the dashboard's
`/api/ops/ollama_health` endpoint can read it without re-probing.

Run from cron:
    python -m user_data.modules.ollama_health
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass, asdict, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

OLLAMA_URL = (
    os.environ.get("OLLAMA_BASE_URL", "")
    or os.environ.get("OLLAMA_HOST", "")
    or "http://localhost:11434"
).rstrip("/")
ALERT_AFTER_FAILURES = int(os.environ.get("OLLAMA_ALERT_FAILURES", "3"))
LATENCY_WARN_S = float(os.environ.get("OLLAMA_LATENCY_WARN_S", "30.0"))
STATUS_FILE = Path(os.environ.get("OLLAMA_HEALTH_STATUS_FILE", "/tmp/ollama-health.json"))

# Required models — picked from the same env vars shark/sentiment use, with
# safe fallbacks. Empty entries are skipped so a half-configured env doesn't
# trigger false "missing" alerts.
_DEEP = (
    os.environ.get("OLLAMA_MODEL", "")
    or os.environ.get("OLLAMA_MODEL_DEEP", "")
    or "hermes3:70b"
)
_FAST = (
    os.environ.get("OLLAMA_FAST_MODEL", "")
    or os.environ.get("OLLAMA_MODEL_FAST", "")
    or "hermes3:8b"
)
REQUIRED_MODELS: list[str] = [m for m in (_DEEP, _FAST) if m]


@dataclass
class HealthStatus:
    healthy: bool = True
    timestamp: str = ""
    consecutive_failures: int = 0
    models_available: list[str] = field(default_factory=list)
    models_missing: list[str] = field(default_factory=list)
    last_probe_latency_s: Optional[float] = None
    error: Optional[str] = None


def check_endpoint() -> tuple[bool, list[str], Optional[str]]:
    """Hit /api/tags. Returns (ok, model_names, error_msg)."""
    try:
        import httpx
        with httpx.Client(timeout=5.0) as c:
            r = c.get(f"{OLLAMA_URL}/api/tags")
        if r.status_code != 200:
            return False, [], f"HTTP {r.status_code}"
        data = r.json()
        names = [m.get("name") for m in (data.get("models") or []) if m.get("name")]
        return True, names, None
    except Exception as exc:  # pragma: no cover — network conditions
        return False, [], str(exc)[:200]


def probe_latency(model: str) -> Optional[float]:
    """One-token probe call to measure end-to-end latency."""
    try:
        import httpx
        start = time.monotonic()
        with httpx.Client(timeout=30.0) as c:
            r = c.post(
                f"{OLLAMA_URL}/api/generate",
                json={
                    "model": model,
                    "prompt": "OK",
                    "stream": False,
                    "keep_alive": "5m",  # keep warm so subsequent calls are fast
                    "options": {"num_predict": 5, "temperature": 0.0},
                },
            )
        elapsed = time.monotonic() - start
        if r.status_code == 200:
            return round(elapsed, 2)
    except Exception:
        pass
    return None


def run_check() -> HealthStatus:
    """Run a single health check, persist the result, fire alerts as needed."""
    status = HealthStatus(timestamp=datetime.now(timezone.utc).isoformat())

    # Load previous failure count so consecutive-failure alerting works
    # across cron invocations.
    prev: dict = {}
    if STATUS_FILE.is_file():
        try:
            prev = json.loads(STATUS_FILE.read_text())
        except (OSError, json.JSONDecodeError):
            pass

    ok, models, err = check_endpoint()
    if not ok:
        status.healthy = False
        status.error = err
        status.consecutive_failures = int(prev.get("consecutive_failures", 0)) + 1
        # Alert exactly once when we cross the threshold
        if status.consecutive_failures == ALERT_AFTER_FAILURES:
            _alert_slack(
                level="CRITICAL",
                title="Ollama unreachable",
                msg=(
                    f"Endpoint {OLLAMA_URL} returning errors. "
                    f"{status.consecutive_failures} consecutive failures. "
                    f"Trading is now using Anthropic fallback (paying real "
                    f"money). Investigate: `systemctl status ollama` on the Spark."
                ),
            )
    else:
        status.models_available = models
        status.models_missing = [m for m in REQUIRED_MODELS if m not in models]
        status.consecutive_failures = 0

        if status.models_missing:
            status.healthy = False
            # De-dup: only alert when the missing-set CHANGES from prior run
            prev_missing = set(prev.get("models_missing") or [])
            if set(status.models_missing) != prev_missing:
                _alert_slack(
                    level="WARNING",
                    title="Ollama models missing",
                    msg=(
                        f"Required: {REQUIRED_MODELS}\n"
                        f"Available: {models}\n"
                        f"Run: " + " && ".join(
                            f"`ollama pull {m}`" for m in status.models_missing
                        )
                    ),
                )
        elif REQUIRED_MODELS:
            # Probe the FAST model — cheaper than the 70B
            fast = _FAST if _FAST in models else REQUIRED_MODELS[0]
            lat = probe_latency(fast)
            if lat is not None:
                status.last_probe_latency_s = lat
                if lat > LATENCY_WARN_S:
                    status.healthy = False
                    _alert_slack(
                        level="WARNING",
                        title="Ollama latency high",
                        msg=(
                            f"Probe call to {fast} took {lat:.1f}s "
                            f"(threshold {LATENCY_WARN_S}s). Trading agents "
                            f"may time out and fail over to Anthropic."
                        ),
                    )

    # Atomic persist
    try:
        STATUS_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = STATUS_FILE.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(asdict(status), indent=2))
        tmp.replace(STATUS_FILE)
    except OSError as exc:
        logger.warning("ollama_health: status persist failed: %s", exc)

    return status


def _alert_slack(level: str, title: str, msg: str) -> None:
    """Best-effort dual-channel alert (Slack + Telegram). Never raises."""
    try:
        from .notifier import notify
        if level == "CRITICAL":
            notify.critical("ollama_down", title=title, message=msg,
                            consecutive_failures=int(re_extract_int(msg)))
        else:
            notify.warning("ollama_down", title=title, message=msg)
    except Exception:
        # Last-resort: raw webhook so we still get a Slack ping if notifier
        # import fails (e.g. circular import during early bootstrap).
        try:
            webhook = os.environ.get("SLACK_WEBHOOK_URL", "").strip()
            if not webhook:
                return
            import httpx
            emoji = {"CRITICAL": ":rotating_light:", "WARNING": ":warning:"}.get(level, ":bell:")
            text = f"{emoji} *[{level}] {title}*\n{msg}"
            with httpx.Client(timeout=5.0) as c:
                c.post(webhook, json={"text": text})
        except Exception:
            pass


def re_extract_int(s: str) -> int:
    """Pull the first integer out of a string; 0 if none."""
    import re
    m = re.search(r"\d+", s or "")
    return int(m.group(0)) if m else 0


def main() -> int:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    status = run_check()
    print(json.dumps(asdict(status), indent=2))
    return 0 if status.healthy else 1


if __name__ == "__main__":
    raise SystemExit(main())
