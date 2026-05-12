"""TOML + env-var configuration loader.

One TOML file. One ``runtime.mode`` flag flips live <-> paper. Every other
knob (universe, trade cadence, fallback toggles) flows through this module so
the rest of the stack can stay free of ``os.environ.get`` calls (banned by
``docs/quanta-core-v4/10-CODE_PATTERNS.md`` §1.6).

The loader resolves the config file path in this order:

1. Argument passed to :func:`load`.
2. ``QUANTA_CONFIG`` environment variable.
3. ``./quanta_core.toml`` in the current working directory.

Per-key env overrides use the ``QUANTA__SECTION__KEY=value`` pattern (double
underscore between section and key), matching pydantic-settings
``env_nested_delimiter='__'``.

See ``docs/quanta-core-v4/06-ARCHITECTURE.md`` §3.19 for the canonical schema.
"""

from __future__ import annotations

import os
import tomllib
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# ---------------------------------------------------------------------------
# Defaults — kept here so the sample TOML and the Settings model agree.
# ---------------------------------------------------------------------------

DEFAULT_UNIVERSE: tuple[str, ...] = (
    # Crypto (12)
    "BTC/USD",
    "ETH/USD",
    "SOL/USD",
    "BCH/USD",
    "AVAX/USD",
    "LINK/USD",
    "DOT/USD",
    "MATIC/USD",
    "ATOM/USD",
    "ADA/USD",
    "XRP/USD",
    "DOGE/USD",
    # Equities + ETFs (15)
    "AAPL",
    "MSFT",
    "NVDA",
    "GOOGL",
    "AMZN",
    "META",
    "TSLA",
    "AMD",
    "SOFI",
    "HOOD",
    "ORCL",
    "SPY",
    "QQQ",
    "IWM",
    "TLT",
)


# ---------------------------------------------------------------------------
# Nested section models.
# ---------------------------------------------------------------------------


class _SectionBase(BaseModel):
    """Strict, extra-forbid base for every config section."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class RuntimeSection(_SectionBase):
    """``[runtime]`` — the flag-bearing section."""

    mode: Literal["paper", "live"] = "paper"
    universe: list[str] = Field(default_factory=lambda: list(DEFAULT_UNIVERSE))
    max_trades_per_week: int = Field(default=3, ge=0, le=50)
    hold_horizon_days: tuple[int, int] = Field(default=(3, 10))
    hold_max_days: int = Field(default=14, ge=1, le=365)
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"

    @field_validator("hold_horizon_days")
    @classmethod
    def _validate_horizon(cls, v: tuple[int, int]) -> tuple[int, int]:
        lo, hi = v
        if lo < 1:
            msg = f"hold_horizon_days lower bound must be >= 1, got {lo}"
            raise ValueError(msg)
        if hi < lo:
            msg = f"hold_horizon_days upper bound ({hi}) < lower bound ({lo})"
            raise ValueError(msg)
        return v

    @field_validator("universe")
    @classmethod
    def _validate_universe(cls, v: list[str]) -> list[str]:
        cleaned = [s.strip() for s in v if s and s.strip()]
        if not cleaned:
            msg = "universe must contain at least one symbol"
            raise ValueError(msg)
        if len(cleaned) != len(set(cleaned)):
            msg = "universe contains duplicate symbols"
            raise ValueError(msg)
        return cleaned


class StrategyOverridesSection(_SectionBase):
    """``[strategy_overrides]`` — opaque passthrough for per-strategy knobs."""

    model_config = ConfigDict(extra="allow", str_strip_whitespace=True)

    tft_blind_fallback: dict[str, Any] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# Top-level Settings model.
# ---------------------------------------------------------------------------


class Settings(BaseSettings):
    """Top-level runtime configuration for quanta-core.

    Reads from a TOML file (passed in via :func:`load`) and overlays per-key
    environment overrides under the ``QUANTA__SECTION__KEY`` namespace. The
    final object is immutable; callers that need a hot-reload (see
    ``docs/quanta-core-v4/06-ARCHITECTURE.md`` §3.19) construct a new
    ``Settings`` rather than mutating in place.
    """

    model_config = SettingsConfigDict(
        env_prefix="QUANTA__",
        env_nested_delimiter="__",
        case_sensitive=False,
        extra="forbid",
        frozen=True,
    )

    runtime: RuntimeSection = Field(default_factory=RuntimeSection)
    strategy_overrides: StrategyOverridesSection = Field(
        default_factory=StrategyOverridesSection,
    )


# ---------------------------------------------------------------------------
# Loader helpers.
# ---------------------------------------------------------------------------


class ConfigError(Exception):
    """Raised when the TOML config cannot be located, parsed, or validated."""


def _resolve_path(explicit: str | os.PathLike[str] | None) -> Path | None:
    if explicit is not None:
        return Path(explicit)
    env = os.environ.get("QUANTA_CONFIG")
    if env:
        return Path(env)
    default = Path.cwd() / "quanta_core.toml"
    if default.exists():
        return default
    return None


def _read_toml(path: Path) -> dict[str, Any]:
    try:
        with path.open("rb") as fh:
            return tomllib.load(fh)
    except FileNotFoundError as exc:
        msg = f"config file not found: {path}"
        raise ConfigError(msg) from exc
    except tomllib.TOMLDecodeError as exc:
        msg = f"config file is not valid TOML: {path} ({exc})"
        raise ConfigError(msg) from exc


def load(path: str | os.PathLike[str] | None = None) -> Settings:
    """Load and validate a :class:`Settings` instance.

    Parameters
    ----------
    path
        Explicit TOML path. When ``None``, falls back to ``QUANTA_CONFIG`` or
        ``./quanta_core.toml``. When no file is found, returns the all-defaults
        :class:`Settings` instance (still subject to env overrides).

    Returns
    -------
    Settings
        Validated, immutable configuration.

    Raises
    ------
    ConfigError
        If the file is unreadable, malformed, or fails Pydantic validation.
    """
    resolved = _resolve_path(path)
    data: dict[str, Any] = {}
    if resolved is not None:
        data = _read_toml(resolved)
    try:
        return Settings.model_validate(data)
    except Exception as exc:
        msg = f"config failed validation: {exc}"
        raise ConfigError(msg) from exc
