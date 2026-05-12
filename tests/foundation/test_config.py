"""Tests for ``quanta_core.config`` — TOML loader + env-var overrides."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from quanta_core.config import (
    DEFAULT_UNIVERSE,
    ConfigError,
    RuntimeSection,
    Settings,
    load,
)

if TYPE_CHECKING:
    from pathlib import Path

# ---------------------------------------------------------------------------
# Default Settings
# ---------------------------------------------------------------------------


def test_default_settings_paper_mode() -> None:
    settings = Settings()
    assert settings.runtime.mode == "paper"
    assert settings.runtime.max_trades_per_week == 3
    assert settings.runtime.hold_horizon_days == (3, 10)
    assert settings.runtime.hold_max_days == 14
    assert settings.runtime.log_level == "INFO"


def test_default_universe_size() -> None:
    settings = Settings()
    assert len(settings.runtime.universe) == 27
    assert "BTC/USD" in settings.runtime.universe
    assert "AAPL" in settings.runtime.universe


def test_default_universe_constant_matches_settings() -> None:
    settings = Settings()
    assert tuple(settings.runtime.universe) == DEFAULT_UNIVERSE


def test_settings_is_frozen() -> None:
    settings = Settings()
    with pytest.raises(Exception):
        settings.runtime = RuntimeSection(mode="live")


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def test_invalid_mode_rejected() -> None:
    with pytest.raises(Exception):
        Settings(runtime=RuntimeSection.model_validate({"mode": "bogus"}))


def test_invalid_hold_horizon_lo_too_low() -> None:
    with pytest.raises(Exception):
        RuntimeSection(hold_horizon_days=(0, 5))


def test_invalid_hold_horizon_inverted() -> None:
    with pytest.raises(Exception):
        RuntimeSection(hold_horizon_days=(10, 3))


def test_empty_universe_rejected() -> None:
    with pytest.raises(Exception):
        RuntimeSection(universe=[])


def test_universe_with_duplicates_rejected() -> None:
    with pytest.raises(Exception, match="duplicate"):
        RuntimeSection(universe=["BTC/USD", "BTC/USD"])


def test_universe_strips_whitespace_and_filters_blanks() -> None:
    section = RuntimeSection(universe=["  BTC/USD  ", "", "ETH/USD"])
    assert section.universe == ["BTC/USD", "ETH/USD"]


def test_extra_keys_rejected_on_runtime() -> None:
    with pytest.raises(Exception):
        RuntimeSection.model_validate({"mode": "paper", "boom": True})


# ---------------------------------------------------------------------------
# TOML loader
# ---------------------------------------------------------------------------


_SAMPLE_TOML = """
[runtime]
mode = "live"
max_trades_per_week = 5
hold_horizon_days = [4, 9]
hold_max_days = 12
log_level = "DEBUG"
universe = ["BTC/USD", "ETH/USD", "AAPL"]

[strategy_overrides]

[strategy_overrides.tft_blind_fallback]
enabled = false
position_size_multiplier = 0.25
"""


def test_load_from_explicit_path(tmp_path: Path) -> None:
    cfg_file = tmp_path / "quanta_core.toml"
    cfg_file.write_text(_SAMPLE_TOML)
    settings = load(cfg_file)
    assert settings.runtime.mode == "live"
    assert settings.runtime.max_trades_per_week == 5
    assert settings.runtime.hold_horizon_days == (4, 9)
    assert settings.runtime.hold_max_days == 12
    assert settings.runtime.log_level == "DEBUG"
    assert settings.runtime.universe == ["BTC/USD", "ETH/USD", "AAPL"]
    assert settings.strategy_overrides.tft_blind_fallback == {
        "enabled": False,
        "position_size_multiplier": 0.25,
    }


def test_load_missing_explicit_path_raises(tmp_path: Path) -> None:
    missing = tmp_path / "does_not_exist.toml"
    with pytest.raises(ConfigError, match="not found"):
        load(missing)


def test_load_malformed_toml_raises(tmp_path: Path) -> None:
    cfg_file = tmp_path / "bad.toml"
    cfg_file.write_text("this is = not [valid] toml = = =\n")
    with pytest.raises(ConfigError, match="valid TOML"):
        load(cfg_file)


def test_load_invalid_schema_raises(tmp_path: Path) -> None:
    cfg_file = tmp_path / "invalid.toml"
    cfg_file.write_text('[runtime]\nmode = "halfway"\n')
    with pytest.raises(ConfigError, match="validation"):
        load(cfg_file)


def test_load_no_file_returns_defaults(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.chdir(tmp_path)  # empty cwd — no quanta_core.toml here
    settings = load()
    assert settings.runtime.mode == "paper"
    assert settings.runtime.max_trades_per_week == 3


def test_load_env_var_path(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    cfg_file = tmp_path / "via_env.toml"
    cfg_file.write_text('[runtime]\nmode = "live"\n')
    monkeypatch.setenv("QUANTA_CONFIG", str(cfg_file))
    settings = load()
    assert settings.runtime.mode == "live"


def test_load_cwd_default(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    cfg_file = tmp_path / "quanta_core.toml"
    cfg_file.write_text("[runtime]\nmax_trades_per_week = 7\n")
    monkeypatch.chdir(tmp_path)
    settings = load()
    assert settings.runtime.max_trades_per_week == 7


# ---------------------------------------------------------------------------
# Env-var override
# ---------------------------------------------------------------------------


def test_env_override_simple_field(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("QUANTA__RUNTIME__MODE", "live")
    monkeypatch.setenv("QUANTA__RUNTIME__MAX_TRADES_PER_WEEK", "9")
    settings = load()
    assert settings.runtime.mode == "live"
    assert settings.runtime.max_trades_per_week == 9


def test_env_override_log_level(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("QUANTA__RUNTIME__LOG_LEVEL", "WARNING")
    settings = load()
    assert settings.runtime.log_level == "WARNING"


def test_env_override_invalid_value_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("QUANTA__RUNTIME__MODE", "bogus")
    with pytest.raises(ConfigError, match="validation"):
        load()


def test_strategy_overrides_default_empty() -> None:
    settings = Settings()
    assert settings.strategy_overrides.tft_blind_fallback == {}


def test_strategy_overrides_extra_passthrough() -> None:
    # Passthrough section accepts arbitrary keys (extra='allow').
    section = Settings().strategy_overrides
    # Constructing a fresh one with extras must not raise:
    from quanta_core.config import StrategyOverridesSection

    s2 = StrategyOverridesSection.model_validate(
        {"tft_blind_fallback": {"enabled": True}, "custom_knob": 42},
    )
    assert s2.tft_blind_fallback == {"enabled": True}
    # ``extra='allow'`` keeps the unknown field accessible via model_extra.
    assert (s2.model_extra or {}).get("custom_knob") == 42
    del section  # silence unused-var lint
