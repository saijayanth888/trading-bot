"""structlog configuration for the entire quanta-core process.

Every component logs through structlog so that downstream consumers (the
Hermes nightly reflector, the dashboard event tail, ModelForge ingestion)
see one canonical JSONL shape regardless of which subsystem emitted the line.
The default sink is stdout; the systemd unit pipes it to
``~/.quanta/logs/quanta-core.jsonl`` (rotated externally by logrotate).

Standard fields on every line:

* ``timestamp`` — ISO-8601 UTC, RFC-3339.
* ``level`` — ``debug|info|warning|error|critical``.
* ``logger`` — module path of the emitter.
* ``event`` — snake_case verb_noun describing the action.
* arbitrary kwargs supplied by the caller.

PII / secret redaction runs as a structlog processor; the
:data:`_REDACTED_KEYS` set is the canonical list and may be extended by
build agents that introduce new secret-shaped config keys.

See ``docs/quanta-core-v4/10-CODE_PATTERNS.md`` §1.5.
"""

from __future__ import annotations

import logging
import sys
from typing import TYPE_CHECKING, Any, Literal

import structlog

if TYPE_CHECKING:  # pragma: no cover — typing only
    from structlog.typing import EventDict, WrappedLogger

LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]


# ---------------------------------------------------------------------------
# Redaction
# ---------------------------------------------------------------------------

_REDACTED_PLACEHOLDER = "***REDACTED***"

_REDACTED_KEYS: frozenset[str] = frozenset(
    {
        "api_key",
        "api_secret",
        "secret",
        "secret_key",
        "password",
        "token",
        "bearer",
        "authorization",
        "private_key",
        "passphrase",
        "alpaca_api_key",
        "alpaca_secret_key",
        "coinbase_api_key",
        "coinbase_api_secret",
        "anthropic_api_key",
        "openai_api_key",
        "modelforge_api_key",
    }
)


def _redact_secrets(
    _logger: WrappedLogger,
    _method_name: str,
    event_dict: EventDict,
) -> EventDict:
    """Replace any value under a sensitive key name with a redaction marker.

    Returns
    -------
    EventDict
        The same event dict, mutated in place.
    """
    for key in list(event_dict.keys()):
        if key.lower() in _REDACTED_KEYS:
            event_dict[key] = _REDACTED_PLACEHOLDER
    return event_dict


# ---------------------------------------------------------------------------
# Public configuration entrypoint.
# ---------------------------------------------------------------------------


def configure(
    *,
    level: LogLevel = "INFO",
    json_output: bool = True,
) -> None:
    """Configure structlog + stdlib logging for the current process.

    Idempotent — calling twice resets the config to the latest call's
    arguments, which keeps the test suite cheap.

    Parameters
    ----------
    level
        Minimum log level. Calls below this level are dropped at the
        structlog filter stage (no formatting cost).
    json_output
        When ``True``, emit one JSON object per line. When ``False``, emit
        the human-readable :class:`structlog.dev.ConsoleRenderer` format
        (intended for interactive debugging only).
    """
    numeric_level = getattr(logging, level)

    # Reset any prior handlers so the configuration is deterministic in tests.
    root_logger = logging.getLogger()
    root_logger.handlers.clear()
    handler = logging.StreamHandler(stream=sys.stdout)
    handler.setFormatter(logging.Formatter("%(message)s"))
    root_logger.addHandler(handler)
    root_logger.setLevel(numeric_level)

    processors: list[Any] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_logger_name,
        structlog.stdlib.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
        _redact_secrets,
    ]
    if json_output:
        processors.append(structlog.processors.JSONRenderer(sort_keys=True))
    else:
        processors.append(structlog.dev.ConsoleRenderer(colors=False))

    structlog.configure(
        processors=processors,
        wrapper_class=structlog.make_filtering_bound_logger(numeric_level),
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=False,
    )


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    """Return a bound logger, optionally namespaced by ``name``.

    Parameters
    ----------
    name
        Optional logger name (typically the calling module's ``__name__``).

    Returns
    -------
    structlog.stdlib.BoundLogger
        Logger bound through the configured processor chain.
    """
    return structlog.stdlib.get_logger(name) if name else structlog.stdlib.get_logger()
