"""``quanta_core.hermes.healthcheck`` — every-15-min probe.

Cadence
-------
Cron ``*/15 * * * *``.

Run
---
Five sub-probes, each producing a ``ProbeResult``:

* Ollama   — ``GET /api/ps`` + tiny generate.
* Postgres — ``SELECT 1``.
* Alpaca   — ``GET /v2/account``.
* Coinbase — ``GET /api/v3/brokerage/accounts``.
* mf-api   — ``GET /api/system/health``.

Aggregate result is written to ``~/.quanta/state/healthcheck_last.json``.
On ``consecutive_failures`` reaching the configured threshold the module
posts an alert to Slack.  Per doc §7.6 the *healthcheck itself* fails
silently — Hermes Agent's external watchdog is the canary of last resort.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

try:
    import httpx
except Exception:  # pragma: no cover
    httpx = None  # type: ignore[assignment]

from quanta_core.hermes._common import (
    HermesConfig,
    SlackNotifier,
    StateWriter,
    configure_logging,
    load_config,
    utc_iso,
)
from quanta_core.hermes._ledger import LedgerClient
from quanta_core.hermes._ollama import OllamaClient


@dataclass
class ProbeResult:
    name: str
    ok: bool
    latency_ms: float
    detail: Mapping[str, object] = field(default_factory=dict)
    error: str | None = None

    def as_payload(self) -> dict[str, object]:
        out: dict[str, object] = {
            "ok": self.ok,
            "latency_ms": round(self.latency_ms, 2),
        }
        out.update(self.detail)
        if self.error:
            out["error"] = self.error
        return out


# ---------------------------------------------------------------------------
# Individual probes
# ---------------------------------------------------------------------------


def probe_ollama(cfg: HermesConfig) -> ProbeResult:
    client = OllamaClient(cfg.ollama_base_url, timeout_seconds=5.0)
    ok, latency, resident = client.ping()
    return ProbeResult(
        name="ollama",
        ok=ok,
        latency_ms=latency,
        detail={"resident_models": resident},
    )


def probe_postgres(cfg: HermesConfig) -> ProbeResult:
    start = time.monotonic()
    client = LedgerClient(cfg.postgres_dsn, cfg.postgres_timeout_seconds)
    ok = client.ping()
    latency = (time.monotonic() - start) * 1000.0
    return ProbeResult(name="postgres", ok=ok, latency_ms=latency)


def probe_alpaca(cfg: HermesConfig) -> ProbeResult:
    if httpx is None:  # pragma: no cover
        return ProbeResult("alpaca", False, 0.0, error="httpx unavailable")
    if not cfg.alpaca_key_id or not cfg.alpaca_secret_key:
        return ProbeResult(
            "alpaca",
            False,
            0.0,
            error="missing_credentials",
        )
    start = time.monotonic()
    headers = {
        "APCA-API-KEY-ID": cfg.alpaca_key_id,
        "APCA-API-SECRET-KEY": cfg.alpaca_secret_key,
    }
    try:
        resp = httpx.get(
            f"{cfg.alpaca_base_url}/v2/account",
            headers=headers,
            timeout=5.0,
        )
        latency = (time.monotonic() - start) * 1000.0
        ok = resp.status_code == 200
        detail: dict[str, object] = {}
        if ok:
            try:
                data = resp.json()
                if isinstance(data, dict):
                    detail["account_status"] = data.get("status")
            except Exception:
                pass
        return ProbeResult(
            name="alpaca",
            ok=ok,
            latency_ms=latency,
            detail=detail,
            error=None if ok else f"http_{resp.status_code}",
        )
    except Exception as exc:
        return ProbeResult(
            "alpaca",
            False,
            (time.monotonic() - start) * 1000.0,
            error=str(exc)[:80],
        )


def probe_coinbase(cfg: HermesConfig) -> ProbeResult:
    if httpx is None:  # pragma: no cover
        return ProbeResult("coinbase", False, 0.0, error="httpx unavailable")
    if not cfg.coinbase_api_key:
        return ProbeResult(
            "coinbase", False, 0.0, error="missing_credentials"
        )
    # We don't sign requests here; we just probe the public health route.
    # Full account probe requires JWT/HMAC and lives in the exchange client.
    start = time.monotonic()
    try:
        resp = httpx.get(
            f"{cfg.coinbase_base_url}/api/v3/brokerage/time",
            timeout=5.0,
        )
        latency = (time.monotonic() - start) * 1000.0
        ok = resp.status_code == 200
        return ProbeResult(
            name="coinbase",
            ok=ok,
            latency_ms=latency,
            error=None if ok else f"http_{resp.status_code}",
        )
    except Exception as exc:
        return ProbeResult(
            "coinbase",
            False,
            (time.monotonic() - start) * 1000.0,
            error=str(exc)[:80],
        )


def probe_mf_api(cfg: HermesConfig) -> ProbeResult:
    if httpx is None:  # pragma: no cover
        return ProbeResult("mf_api", False, 0.0, error="httpx unavailable")
    start = time.monotonic()
    headers = {}
    if cfg.mf_api_key:
        headers["X-API-Key"] = cfg.mf_api_key
    try:
        resp = httpx.get(
            f"{cfg.mf_api_url}/api/system/health",
            headers=headers,
            timeout=5.0,
        )
        latency = (time.monotonic() - start) * 1000.0
        ok = resp.status_code == 200
        return ProbeResult(
            name="mf_api",
            ok=ok,
            latency_ms=latency,
            error=None if ok else f"http_{resp.status_code}",
        )
    except Exception as exc:
        return ProbeResult(
            "mf_api",
            False,
            (time.monotonic() - start) * 1000.0,
            error=str(exc)[:80],
        )


# ---------------------------------------------------------------------------
# Aggregation
# ---------------------------------------------------------------------------


def aggregate(
    probes: Sequence[ProbeResult], prev_state: Mapping[str, object] | None
) -> dict[str, object]:
    any_fail = any(not p.ok for p in probes)
    prev_consec = 0
    if prev_state is not None:
        raw_consec = prev_state.get("consecutive_failures")
        if isinstance(raw_consec, int):
            prev_consec = raw_consec
    consecutive = prev_consec + 1 if any_fail else 0
    payload: dict[str, object] = {
        "ts": utc_iso(),
        "any_failure": any_fail,
        "consecutive_failures": consecutive,
    }
    for p in probes:
        payload[p.name] = p.as_payload()
    return payload


def maybe_post_alert(
    state: Mapping[str, object],
    threshold: int,
    notifier: SlackNotifier,
) -> bool:
    """Post a Slack alert when the consecutive-failure threshold is crossed."""

    if not state.get("any_failure"):
        return False
    raw_consec = state.get("consecutive_failures") or 0
    consec = int(raw_consec) if isinstance(raw_consec, (int, str)) else 0
    if consec < threshold:
        return False
    failed: list[str] = []
    for name in ("ollama", "postgres", "alpaca", "coinbase", "mf_api"):
        probe = state.get(name)
        if isinstance(probe, dict) and not probe.get("ok"):
            failed.append(name)
    return notifier.post(
        f":rotating_light: healthcheck · {consec} consecutive failure(s) · "
        f"failed={','.join(failed) or '?'}"
    )


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------


def _read_prev_state(state_path: Path) -> Mapping[str, object] | None:
    if not state_path.exists():
        return None
    try:
        data = json.loads(state_path.read_text())
    except Exception:
        return None
    return data if isinstance(data, dict) else None


def _parse_args(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="quanta_core.hermes.healthcheck",
        description="Every-15-min infra health probe",
    )
    parser.add_argument(
        "--no-alert",
        action="store_true",
        help="never post to Slack regardless of consecutive failures",
    )
    return parser.parse_args(list(argv))


def run(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv if argv is not None else sys.argv[1:])
    cfg = load_config()
    log = configure_logging("healthcheck")
    notifier = SlackNotifier(cfg.slack_webhook_url, cfg.slack_channel)
    state_path = cfg.state_root / "healthcheck_last.json"

    prev_state = _read_prev_state(state_path)
    probes: list[ProbeResult] = [
        probe_ollama(cfg),
        probe_postgres(cfg),
        probe_alpaca(cfg),
        probe_coinbase(cfg),
        probe_mf_api(cfg),
    ]
    state = aggregate(probes, prev_state)
    StateWriter(state_path).write(state)

    log.info(
        "healthcheck any_fail=%s consec=%s",
        state.get("any_failure"),
        state.get("consecutive_failures"),
    )
    if not args.no_alert:
        maybe_post_alert(state, cfg.consecutive_failure_threshold, notifier)

    # Per doc §7 fail-open: even when probes fail, the cron returns 0.  The
    # state file + Slack alert are the load-bearing signals.
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(run())
