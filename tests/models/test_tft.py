"""Tests for :mod:`quanta_core.models.tft`.

Heavy on :func:`validate_artifact` coverage — the legacy
``validate_model_zip`` stub returned True for an empty directory, and
the operator's brief mandates 95%+ coverage on the replacement so the
same class of bug never recurs.
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest
import torch
from safetensors.torch import save_file

from quanta_core.models.tft import (
    QUANTILE_LEVELS,
    TFTConfig,
    TFTModel,
    TFTValidationError,
    validate_artifact,
)


def _small_config(**overrides: object) -> TFTConfig:
    """Build a fast TFTConfig for round-trip tests."""
    defaults: dict[str, object] = {
        "n_features": 4,
        "n_classes": 3,
        "window_size": 8,
        "hidden_size": 16,
        "n_heads": 4,
        "n_epochs": 1,
        "batch_size": 4,
        "use_amp": False,
        "use_compile": False,
        "early_stopping_patience": 0,
    }
    defaults.update(overrides)
    return TFTConfig(**defaults)  # type: ignore[arg-type]


def _build_loaded_model(tmp_path: Path) -> TFTModel:
    """Construct a TFTModel with materialised weights (no training)."""
    config = _small_config()
    model = TFTModel(config, device="cpu")
    # Materialise the module without running fit() — bypasses the slow
    # training loop for the round-trip tests by reaching into the private
    # builder. Production code path goes through fit() or load().
    model._model = model._build_module()
    return model


# ---------------------------------------------------------------------------
# TFTConfig
# ---------------------------------------------------------------------------


def test_config_rejects_zero_features() -> None:
    with pytest.raises(ValueError):
        _small_config(n_features=0)


def test_config_rejects_single_class() -> None:
    with pytest.raises(ValueError):
        TFTConfig(n_features=4, n_classes=1, window_size=8, hidden_size=16)


def test_config_rejects_tiny_window() -> None:
    with pytest.raises(ValueError):
        _small_config(window_size=3)


def test_config_rejects_bad_dropout() -> None:
    with pytest.raises(ValueError):
        _small_config(dropout=1.0)


def test_config_rejects_heads_not_dividing_hidden() -> None:
    with pytest.raises(ValueError):
        _small_config(hidden_size=10, n_heads=3)


def test_config_rejects_class_name_mismatch() -> None:
    with pytest.raises(ValueError):
        _small_config(class_names=["only-one"])


def test_config_round_trip_through_dict() -> None:
    config = _small_config()
    restored = TFTConfig.from_dict(config.to_dict())
    assert restored.to_dict() == config.to_dict()


# ---------------------------------------------------------------------------
# Save / load round-trip
# ---------------------------------------------------------------------------


def test_save_writes_safetensors_and_metadata(tmp_path: Path) -> None:
    model = _build_loaded_model(tmp_path)
    out = tmp_path / "tft_v1"
    model.save(out)

    assert (out / "model.safetensors").is_file()
    assert (out / "metadata.json").is_file()

    metadata = json.loads((out / "metadata.json").read_text())
    assert metadata["version"] == 1
    assert metadata["tensor_count"] > 0
    assert metadata["config"]["n_features"] == 4
    assert "saved_at" in metadata


def test_save_then_load_round_trip(tmp_path: Path) -> None:
    model = _build_loaded_model(tmp_path)
    out = tmp_path / "tft_v1"
    model.save(out)

    # Capture a tensor BEFORE reload to compare bit-equal after.
    captured = {k: v.detach().cpu().clone() for k, v in model.module.state_dict().items()}

    reloaded = TFTModel.load(out, device="cpu")
    reloaded_state = reloaded.module.state_dict()
    assert set(captured.keys()) == set(reloaded_state.keys())
    for k, v in captured.items():
        assert torch.equal(v, reloaded_state[k]), f"tensor mismatch on {k!r}"


def test_save_without_fit_raises(tmp_path: Path) -> None:
    config = _small_config()
    model = TFTModel(config, device="cpu")
    with pytest.raises(RuntimeError, match="nothing to save"):
        model.save(tmp_path / "empty")


def test_module_property_raises_when_not_loaded() -> None:
    config = _small_config()
    model = TFTModel(config, device="cpu")
    with pytest.raises(RuntimeError, match="no module"):
        _ = model.module


# ---------------------------------------------------------------------------
# Inference
# ---------------------------------------------------------------------------


def test_predict_proba_pads_initial_window(tmp_path: Path) -> None:
    model = _build_loaded_model(tmp_path)
    config = model.config
    rng = np.random.default_rng(seed=0)
    n_rows = 20
    features = rng.standard_normal((n_rows, config.n_features)).astype(np.float32)

    probs, conf = model.predict_proba(features)

    assert probs.shape == (n_rows, config.n_classes)
    assert conf.shape == (n_rows,)
    # First window_size-1 rows are zero by contract.
    pad_rows = config.window_size - 1
    assert np.all(probs[:pad_rows] == 0.0)
    assert np.all(conf[:pad_rows] == 0.0)
    # Subsequent probabilities sum to ~1 across classes.
    sums = probs[pad_rows:].sum(axis=1)
    assert np.allclose(sums, 1.0, atol=1e-5)


def test_predict_proba_short_input_returns_zeros(tmp_path: Path) -> None:
    model = _build_loaded_model(tmp_path)
    config = model.config
    features = np.zeros((config.window_size - 1, config.n_features), dtype=np.float32)
    probs, conf = model.predict_proba(features)
    assert probs.shape == (config.window_size - 1, config.n_classes)
    assert np.all(probs == 0.0)
    assert np.all(conf == 0.0)


# ---------------------------------------------------------------------------
# Training (minimal — exercise the loop, not the math)
# ---------------------------------------------------------------------------


def test_fit_runs_one_epoch(tmp_path: Path) -> None:
    config = _small_config(n_epochs=1, batch_size=4)
    model = TFTModel(config, device="cpu")
    rng = np.random.default_rng(seed=42)
    n_rows = 50
    features = rng.standard_normal((n_rows, config.n_features)).astype(np.float32)
    labels = rng.integers(0, config.n_classes, size=n_rows).astype(np.int64)
    summary = model.fit(features, labels)
    assert summary["epochs_run"] == 1
    assert not np.isnan(summary["final_loss"])


def test_fit_with_val_split_records_sharpe(tmp_path: Path) -> None:
    config = _small_config(n_epochs=1, batch_size=4)
    model = TFTModel(config, device="cpu")
    rng = np.random.default_rng(seed=1)
    feat = rng.standard_normal((40, config.n_features)).astype(np.float32)
    lbl = rng.integers(0, config.n_classes, size=40).astype(np.int64)
    val_feat = rng.standard_normal((30, config.n_features)).astype(np.float32)
    val_lbl = rng.integers(0, config.n_classes, size=30).astype(np.int64)
    summary = model.fit(feat, lbl, val_features=val_feat, val_labels=val_lbl)
    assert summary["best_val_sharpe"] is not None


def test_fit_with_too_few_rows_raises() -> None:
    config = _small_config(window_size=8, batch_size=4)
    model = TFTModel(config, device="cpu")
    features = np.zeros((10, config.n_features), dtype=np.float32)
    labels = np.zeros(10, dtype=np.int64)
    # 10 rows → only 3 windows; batch_size=4 fails.
    with pytest.raises(ValueError, match="Not enough"):
        model.fit(features, labels)


def test_fit_with_mismatched_lengths_raises() -> None:
    config = _small_config()
    model = TFTModel(config, device="cpu")
    features = np.zeros((20, config.n_features), dtype=np.float32)
    labels = np.zeros(19, dtype=np.int64)
    with pytest.raises(ValueError, match="length mismatch"):
        model.fit(features, labels)


# ---------------------------------------------------------------------------
# validate_artifact — must be 95%+ covered
# ---------------------------------------------------------------------------


def test_validate_missing_directory(tmp_path: Path) -> None:
    with pytest.raises(TFTValidationError, match="does not exist"):
        validate_artifact(tmp_path / "no_such_dir")


def test_validate_path_is_file_not_dir(tmp_path: Path) -> None:
    f = tmp_path / "a_file"
    f.write_text("hi")
    with pytest.raises(TFTValidationError, match="not a directory"):
        validate_artifact(f)


def test_validate_missing_weights(tmp_path: Path) -> None:
    artefact = tmp_path / "a"
    artefact.mkdir()
    (artefact / "metadata.json").write_text(json.dumps({"version": 1}))
    with pytest.raises(TFTValidationError, match="weights file missing"):
        validate_artifact(artefact)


def test_validate_missing_metadata(tmp_path: Path) -> None:
    artefact = tmp_path / "a"
    artefact.mkdir()
    save_file({"x": torch.tensor([1.0])}, str(artefact / "model.safetensors"))
    with pytest.raises(TFTValidationError, match="metadata file missing"):
        validate_artifact(artefact)


def test_validate_empty_directory_rejected(tmp_path: Path) -> None:
    """The original bug: empty dir used to pass validation."""
    artefact = tmp_path / "empty_stub"
    artefact.mkdir()
    with pytest.raises(TFTValidationError):
        validate_artifact(artefact)


def test_validate_bad_json(tmp_path: Path) -> None:
    artefact = tmp_path / "a"
    artefact.mkdir()
    save_file({"x": torch.tensor([1.0])}, str(artefact / "model.safetensors"))
    (artefact / "metadata.json").write_text("{not json")
    with pytest.raises(TFTValidationError, match="not valid JSON"):
        validate_artifact(artefact)


def test_validate_metadata_not_object(tmp_path: Path) -> None:
    artefact = tmp_path / "a"
    artefact.mkdir()
    save_file({"x": torch.tensor([1.0])}, str(artefact / "model.safetensors"))
    (artefact / "metadata.json").write_text(json.dumps(["not", "an", "object"]))
    with pytest.raises(TFTValidationError, match="must be a JSON object"):
        validate_artifact(artefact)


def test_validate_unknown_version(tmp_path: Path) -> None:
    artefact = tmp_path / "a"
    artefact.mkdir()
    save_file({"x": torch.tensor([1.0])}, str(artefact / "model.safetensors"))
    (artefact / "metadata.json").write_text(json.dumps({"version": 999}))
    with pytest.raises(TFTValidationError, match="unrecognised artefact version"):
        validate_artifact(artefact)


def test_validate_missing_config(tmp_path: Path) -> None:
    artefact = tmp_path / "a"
    artefact.mkdir()
    save_file({"x": torch.tensor([1.0])}, str(artefact / "model.safetensors"))
    (artefact / "metadata.json").write_text(json.dumps({"version": 1}))
    with pytest.raises(TFTValidationError, match="missing 'config'"):
        validate_artifact(artefact)


def test_validate_invalid_config_payload(tmp_path: Path) -> None:
    artefact = tmp_path / "a"
    artefact.mkdir()
    save_file({"x": torch.tensor([1.0])}, str(artefact / "model.safetensors"))
    (artefact / "metadata.json").write_text(
        json.dumps({"version": 1, "config": {"n_features": -1, "n_classes": 3}}),
    )
    with pytest.raises(TFTValidationError, match="config is invalid"):
        validate_artifact(artefact)


def test_validate_empty_safetensors_file(tmp_path: Path) -> None:
    artefact = tmp_path / "a"
    artefact.mkdir()
    # Write a zero-tensor file (safetensors allows empty header).
    save_file({}, str(artefact / "model.safetensors"))
    config = TFTConfig(n_features=4, n_classes=3, window_size=8, hidden_size=16)
    (artefact / "metadata.json").write_text(
        json.dumps(
            {
                "version": 1,
                "config": config.to_dict(),
                "tensor_count": 0,
                "tensor_names": [],
            },
        ),
    )
    with pytest.raises(TFTValidationError, match="zero tensors"):
        validate_artifact(artefact)


def test_validate_safetensors_load_failure(tmp_path: Path) -> None:
    artefact = tmp_path / "a"
    artefact.mkdir()
    (artefact / "model.safetensors").write_bytes(b"not a safetensors file")
    config = TFTConfig(n_features=4, n_classes=3, window_size=8, hidden_size=16)
    (artefact / "metadata.json").write_text(
        json.dumps(
            {
                "version": 1,
                "config": config.to_dict(),
                "tensor_count": 1,
                "tensor_names": ["weights"],
            },
        ),
    )
    with pytest.raises(TFTValidationError, match="safetensors load failed"):
        validate_artifact(artefact)


def test_validate_tensor_count_mismatch(tmp_path: Path) -> None:
    artefact = tmp_path / "a"
    artefact.mkdir()
    save_file(
        {"x": torch.tensor([1.0]), "y": torch.tensor([2.0])},
        str(artefact / "model.safetensors"),
    )
    config = TFTConfig(n_features=4, n_classes=3, window_size=8, hidden_size=16)
    (artefact / "metadata.json").write_text(
        json.dumps(
            {
                "version": 1,
                "config": config.to_dict(),
                "tensor_count": 5,  # lie: file actually has 2
                "tensor_names": ["x", "y"],
            },
        ),
    )
    with pytest.raises(TFTValidationError, match="tensor_count mismatch"):
        validate_artifact(artefact)


def test_validate_tensor_count_wrong_type(tmp_path: Path) -> None:
    artefact = tmp_path / "a"
    artefact.mkdir()
    save_file({"x": torch.tensor([1.0])}, str(artefact / "model.safetensors"))
    config = TFTConfig(n_features=4, n_classes=3, window_size=8, hidden_size=16)
    (artefact / "metadata.json").write_text(
        json.dumps(
            {
                "version": 1,
                "config": config.to_dict(),
                "tensor_count": "many",
                "tensor_names": ["x"],
            },
        ),
    )
    with pytest.raises(TFTValidationError, match="must be int"):
        validate_artifact(artefact)


def test_validate_tensor_names_wrong_type(tmp_path: Path) -> None:
    artefact = tmp_path / "a"
    artefact.mkdir()
    save_file({"x": torch.tensor([1.0])}, str(artefact / "model.safetensors"))
    config = TFTConfig(n_features=4, n_classes=3, window_size=8, hidden_size=16)
    (artefact / "metadata.json").write_text(
        json.dumps(
            {
                "version": 1,
                "config": config.to_dict(),
                "tensor_count": 1,
                "tensor_names": "should be a list",
            },
        ),
    )
    with pytest.raises(TFTValidationError, match="must be a list"):
        validate_artifact(artefact)


def test_validate_tensor_names_disagree(tmp_path: Path) -> None:
    artefact = tmp_path / "a"
    artefact.mkdir()
    save_file({"x": torch.tensor([1.0])}, str(artefact / "model.safetensors"))
    config = TFTConfig(n_features=4, n_classes=3, window_size=8, hidden_size=16)
    (artefact / "metadata.json").write_text(
        json.dumps(
            {
                "version": 1,
                "config": config.to_dict(),
                "tensor_count": 1,
                "tensor_names": ["wrong_name"],
            },
        ),
    )
    with pytest.raises(TFTValidationError, match="tensor names in metadata"):
        validate_artifact(artefact)


def test_validate_tensor_name_not_string(tmp_path: Path) -> None:
    artefact = tmp_path / "a"
    artefact.mkdir()
    save_file({"x": torch.tensor([1.0])}, str(artefact / "model.safetensors"))
    config = TFTConfig(n_features=4, n_classes=3, window_size=8, hidden_size=16)
    (artefact / "metadata.json").write_text(
        json.dumps(
            {
                "version": 1,
                "config": config.to_dict(),
                "tensor_count": 1,
                "tensor_names": [42],
            },
        ),
    )
    with pytest.raises(TFTValidationError, match="expected string tensor name"):
        validate_artifact(artefact)


def test_validate_metadata_read_oserror(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Force the metadata read to raise OSError to cover the error branch."""
    artefact = tmp_path / "a"
    artefact.mkdir()
    save_file({"x": torch.tensor([1.0])}, str(artefact / "model.safetensors"))
    metadata_path = artefact / "metadata.json"
    metadata_path.write_text("{}")

    original_read_text = Path.read_text

    def boom(self: Path, *args: object, **kwargs: object) -> str:
        if self.name == "metadata.json":
            raise PermissionError("no read")
        return original_read_text(self, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(Path, "read_text", boom)
    with pytest.raises(TFTValidationError, match="metadata read failed"):
        validate_artifact(artefact)


def test_validate_happy_path_after_real_save(tmp_path: Path) -> None:
    model = _build_loaded_model(tmp_path)
    out = tmp_path / "tft_real"
    model.save(out)
    metadata = validate_artifact(out)
    assert metadata["version"] == 1
    assert metadata["tensor_count"] > 0


def test_quantile_levels_constant() -> None:
    assert QUANTILE_LEVELS == (0.1, 0.5, 0.9)
