"""Shared primitives for ``quanta_core.hermes`` modules.

Single source of truth for:

* ``StateWriter`` — atomic JSON state writes per doc §5.1.
* ``SlackNotifier`` — best-effort webhook poster (fail-open on network).
* ``load_config`` — TOML / env-var driven config with sane defaults.
* ``state_dir`` — resolves ``~/.quanta/state/`` (override via ``QUANTA_STATE_DIR``).
* ``HermesError`` — sentinel exception so modules can raise on data
  problems while infra failures fall open per doc §7.
"""

from __future__ import annotations

import json
import logging
import os
import sys
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

# httpx is a hard dep (declared in pyproject) — but the import is kept in a
# try/except so unit-tests on slim CI runners can still drive the modules
# with a fake-poster injected.
try:  # pragma: no cover — exercised in integration only
    import httpx
except Exception:  # pragma: no cover
    httpx = None  # type: ignore[assignment]


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class HermesError(RuntimeError):
    """Raised for *data* problems that must be loud.

    Per doc §7 "fail-open on infra, fail-loud on data":

    * Network timeouts, missing creds, container down → log + return 1.
    * Bad data shape, schema mismatch, missing required keys → raise this.
    """


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------


def state_dir() -> Path:
    """Return the directory where Hermes modules write state.

    Defaults to ``~/.quanta/state``.  Override via ``QUANTA_STATE_DIR`` so
    unit tests can point at ``tmp_path``.
    """

    env = os.environ.get("QUANTA_STATE_DIR")
    base = Path(env) if env else Path.home() / ".quanta" / "state"
    base.mkdir(parents=True, exist_ok=True)
    return base


def repo_root() -> Path:
    """Resolve the trading-bot repository root.

    ``QUANTA_REPO_ROOT`` env override is honoured for tests.  Otherwise we
    walk up from this file until we find a ``pyproject.toml`` or fall back
    to the user's standard location.
    """

    env = os.environ.get("QUANTA_REPO_ROOT")
    if env:
        return Path(env)

    here = Path(__file__).resolve()
    for ancestor in (here, *here.parents):
        if (ancestor / "pyproject.toml").exists():
            return ancestor
    return Path.home() / "Documents" / "trading-bot"


# ---------------------------------------------------------------------------
# State writer
# ---------------------------------------------------------------------------


class StateWriter:
    """Atomic JSON state writer.

    Implementation pattern from doc §5.1::

        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(payload, indent=2, default=str))
        os.replace(tmp, path)   # POSIX atomic rename
    """

    def __init__(self, path: Path) -> None:
        self.path = path

    def write(self, payload: Mapping[str, Any]) -> None:
        """Atomic write of ``payload`` to ``self.path``."""

        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(json.dumps(dict(payload), indent=2, default=str))
        os.replace(tmp, self.path)

    def append_text_atomic(self, text: str) -> None:
        """Atomically append ``text`` to ``self.path``.

        We read-modify-write into a tmp file then ``os.replace`` so a crash
        mid-write leaves the prior content intact.  This is the pattern used
        by ``decisions.md`` appends (per doc §7.1 — a crashed reflector must
        not leave a half-line in the file).
        """

        self.path.parent.mkdir(parents=True, exist_ok=True)
        existing = self.path.read_text() if self.path.exists() else ""
        new_content = existing + text
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(new_content)
        os.replace(tmp, self.path)


# ---------------------------------------------------------------------------
# Slack
# ---------------------------------------------------------------------------


@dataclass
class SlackNotifier:
    """Best-effort Slack webhook poster.

    Fail-open: a network error returns ``False`` but does not raise — the
    cron continues.  Errors are logged at ``warning`` level.  The webhook
    URL is read from ``SLACK_WEBHOOK_URL`` unless explicitly supplied.

    A ``channel`` may be configured but is **not** wired to a channel-routing
    webhook today — it is captured in the JSON body so a downstream router
    (or human reading logs) can correlate.  Per doc §9 open question 1.
    """

    webhook_url: str | None = None
    channel: str | None = None
    timeout_seconds: float = 5.0
    logger: logging.Logger = field(
        default_factory=lambda: logging.getLogger("quanta_core.hermes.slack")
    )

    def __post_init__(self) -> None:
        if self.webhook_url is None:
            self.webhook_url = os.environ.get("SLACK_WEBHOOK_URL")

    def post(self, text: str) -> bool:
        """POST ``text`` to the webhook.  Returns ``True`` on success."""

        if not self.webhook_url:
            self.logger.warning("slack post skipped — no SLACK_WEBHOOK_URL")
            return False
        if httpx is None:  # pragma: no cover
            self.logger.warning("httpx unavailable — slack post skipped")
            return False
        body: dict[str, Any] = {"text": text}
        if self.channel:
            body["channel"] = self.channel
        try:
            resp = httpx.post(
                self.webhook_url, json=body, timeout=self.timeout_seconds
            )
            if resp.status_code >= 400:
                self.logger.warning(
                    "slack post non-2xx: %d %s", resp.status_code, resp.text[:120]
                )
                return False
            return True
        except Exception as exc:  # pragma: no cover — network
            self.logger.warning("slack post failed: %s", exc)
            return False


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


@dataclass
class HermesConfig:
    """All knobs Hermes modules read at startup.

    Values are populated from env vars (the operator's standing preference
    is config-over-hardcoded; per ``user_profile.md``).  Defaults match the
    paper-trading reality on the operator's box today.
    """

    # Ollama (LLM)
    ollama_base_url: str = "http://localhost:11434"
    reflector_model: str = "hermes3:8b"
    post_mortem_model: str = "hermes3:70b"
    llm_timeout_seconds: float = 60.0

    # Postgres (ledger)
    postgres_dsn: str | None = None
    postgres_timeout_seconds: float = 8.0

    # mf-api (ModelForge)
    mf_api_url: str = "http://localhost:8000"
    mf_api_key: str | None = None
    mf_weekly_workflow_id: str | None = None
    mf_poll_interval_seconds: int = 30
    mf_poll_max_seconds: int = 5400

    # Exchanges (for healthcheck)
    alpaca_base_url: str = "https://paper-api.alpaca.markets"
    alpaca_key_id: str | None = None
    alpaca_secret_key: str | None = None
    coinbase_base_url: str = "https://api.coinbase.com"
    coinbase_api_key: str | None = None

    # Slack
    slack_webhook_url: str | None = None
    slack_channel: str | None = None

    # Healthcheck threshold
    consecutive_failure_threshold: int = 3

    # Paths
    state_root: Path = field(default_factory=state_dir)
    repo_root_path: Path = field(default_factory=repo_root)


def load_config() -> HermesConfig:
    """Populate :class:`HermesConfig` from env vars.

    Env-var contract (all optional — Hermes degrades gracefully):

    +-------------------------------+-------------------------------------+
    | ``OLLAMA_BASE_URL``           | LLM host                            |
    | ``HERMES_REFLECTOR_MODEL``    | model tag for the nightly reflector |
    | ``HERMES_POST_MORTEM_MODEL``  | model tag for Sat post-mortem       |
    | ``POSTGRES_DSN``              | ledger DSN                          |
    | ``MODELFORGE_API_URL``        | mf-api base                         |
    | ``MODELFORGE_API_KEY``        | mf-api key                          |
    | ``MODELFORGE_WORKFLOW_ID``    | Sunday workflow uuid                |
    | ``ALPACA_KEY_ID``             | broker (paper) key                  |
    | ``ALPACA_SECRET_KEY``         | broker (paper) secret               |
    | ``COINBASE_API_KEY``          | exchange creds                      |
    | ``SLACK_WEBHOOK_URL``         | webhook                             |
    | ``SLACK_CHANNEL``             | channel override                    |
    +-------------------------------+-------------------------------------+
    """

    return HermesConfig(
        ollama_base_url=os.environ.get(
            "OLLAMA_BASE_URL", "http://localhost:11434"
        ),
        reflector_model=os.environ.get(
            "HERMES_REFLECTOR_MODEL", "hermes3:8b"
        ),
        post_mortem_model=os.environ.get(
            "HERMES_POST_MORTEM_MODEL", "hermes3:70b"
        ),
        llm_timeout_seconds=float(os.environ.get("HERMES_LLM_TIMEOUT", "60")),
        postgres_dsn=os.environ.get("POSTGRES_DSN"),
        postgres_timeout_seconds=float(
            os.environ.get("HERMES_PG_TIMEOUT", "8")
        ),
        mf_api_url=os.environ.get(
            "MODELFORGE_API_URL", "http://localhost:8000"
        ),
        mf_api_key=os.environ.get("MODELFORGE_API_KEY"),
        mf_weekly_workflow_id=os.environ.get("MODELFORGE_WORKFLOW_ID"),
        mf_poll_interval_seconds=int(
            os.environ.get("MODELFORGE_POLL_INTERVAL", "30")
        ),
        mf_poll_max_seconds=int(
            os.environ.get("MODELFORGE_POLL_MAX", "5400")
        ),
        alpaca_base_url=os.environ.get(
            "ALPACA_BASE_URL", "https://paper-api.alpaca.markets"
        ),
        alpaca_key_id=os.environ.get("ALPACA_KEY_ID")
        or os.environ.get("ALPACA_API_KEY"),
        alpaca_secret_key=os.environ.get("ALPACA_SECRET_KEY")
        or os.environ.get("ALPACA_API_SECRET"),
        coinbase_base_url=os.environ.get(
            "COINBASE_API_URL", "https://api.coinbase.com"
        ),
        coinbase_api_key=os.environ.get("COINBASE_API_KEY"),
        slack_webhook_url=os.environ.get("SLACK_WEBHOOK_URL"),
        slack_channel=os.environ.get("SLACK_CHANNEL"),
        consecutive_failure_threshold=int(
            os.environ.get("HERMES_HEALTH_FAIL_THRESHOLD", "3")
        ),
    )


# ---------------------------------------------------------------------------
# Time helpers
# ---------------------------------------------------------------------------


def utc_now() -> datetime:
    """Return tz-aware UTC ``now``.  Single chokepoint so tests can monkey-patch."""

    return datetime.now(UTC)


def utc_iso(ts: datetime | None = None) -> str:
    """ISO-8601 string with explicit UTC offset."""

    ts = ts or utc_now()
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=UTC)
    return ts.isoformat()


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------


def configure_logging(module_name: str, level: str = "INFO") -> logging.Logger:
    """Configure a structured stderr logger for a module entrypoint.

    Each module is invoked as ``python -m quanta_core.hermes.<name>`` so a
    line-per-event format with the module name is the most useful default.
    """

    logger = logging.getLogger(f"quanta_core.hermes.{module_name}")
    if logger.handlers:
        return logger
    handler = logging.StreamHandler(stream=sys.stderr)
    handler.setFormatter(
        logging.Formatter(
            fmt="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
            datefmt="%Y-%m-%dT%H:%M:%S%z",
        )
    )
    logger.addHandler(handler)
    logger.setLevel(getattr(logging, level.upper(), logging.INFO))
    logger.propagate = False
    return logger


__all__ = [
    "HermesConfig",
    "HermesError",
    "SlackNotifier",
    "StateWriter",
    "configure_logging",
    "load_config",
    "repo_root",
    "state_dir",
    "utc_iso",
    "utc_now",
]
