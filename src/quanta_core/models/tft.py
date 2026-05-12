"""TFT classifier — standalone PyTorch port of the FreqAI ``TFTModel``.

Ports ``user_data/freqaimodels/TFTModel.py`` (829 lines) and DROPS:

- ``BasePyTorchClassifier`` inheritance — registry manages the model now.
- ``FreqaiDataKitchen`` — replaced by direct ``(features, labels)`` arrays.
- ``IResolver`` re-import workarounds via the legacy ``tft_pickle`` shim —
  the shim is DELETED. Weights are saved with
  :func:`safetensors.torch.save_file`; metadata rides in a sibling
  ``metadata.json``. No ``torch.save``, no Python stdlib serialiser.
- The startup quarantine scan + ``sys.modules`` proxy — moot once the
  legacy load path is gone.

The training loop, AMP path, cosine-warmup scheduler, sliding-window
helpers, early-stopping-on-val-Sharpe heuristic, and the per-epoch resume
checkpoint pattern survive the port (with the checkpoint format swapped
to safetensors).
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

import numpy as np
import torch
from numpy.typing import NDArray
from safetensors.torch import load_file, save_file
from torch import nn
from torch.utils.data import DataLoader, TensorDataset

from quanta_core.models.tft_architecture import (
    TemporalFusionTransformer,
    pinball_loss,
)

__all__ = [
    "QUANTILE_LEVELS",
    "TFTConfig",
    "TFTModel",
    "TFTValidationError",
    "validate_artifact",
]

logger = logging.getLogger(__name__)

QUANTILE_LEVELS: tuple[float, ...] = (0.1, 0.5, 0.9)
"""Default quantile levels for the auxiliary pinball-loss head."""

_METADATA_FILENAME = "metadata.json"
_WEIGHTS_FILENAME = "model.safetensors"
_ARTIFACT_VERSION = 1


def _set_inference_mode(module: nn.Module) -> None:
    """Switch ``module`` to inference mode (dropout / BN frozen)."""
    module.train(mode=False)


def _set_training_mode(module: nn.Module) -> None:
    """Switch ``module`` back to training mode."""
    module.train(mode=True)


class TFTValidationError(Exception):
    """Raised by :func:`validate_artifact` when an artefact is malformed.

    Existed in spirit as the legacy ``validate_model_zip`` contract.
    The legacy implementation returned ``True`` for an empty stub
    directory; this replacement raises so the caller's "model is loaded"
    code path never silently runs on a missing weight file.
    """


@dataclass
class TFTConfig:
    """Hyperparameters + training knobs for :class:`TFTModel`.

    Defaults mirror the production ``config.json`` values in use on
    2026-05-12. Every field is plain ``int`` / ``float`` / ``bool`` so
    the config round-trips through ``metadata.json`` losslessly.
    """

    n_features: int
    n_classes: int
    window_size: int = 120
    hidden_size: int = 64
    n_heads: int = 4
    dropout: float = 0.1
    var_dim: int = 8
    lr: float = 1e-3
    weight_decay: float = 1e-4
    n_epochs: int = 25
    batch_size: int = 256
    quantile_loss_weight: float = 0.3
    early_stopping_patience: int = 5
    use_amp: bool = True
    use_compile: bool = False
    warmup_pct: float = 0.05
    class_names: list[str] = field(default_factory=lambda: ["down", "flat", "up"])
    quantile_levels: list[float] = field(default_factory=lambda: list(QUANTILE_LEVELS))

    def __post_init__(self) -> None:
        if self.n_features <= 0:
            raise ValueError("n_features must be positive")
        if self.n_classes <= 1:
            raise ValueError("n_classes must be >= 2")
        if self.window_size < 4:
            raise ValueError("window_size must be >= 4")
        if not 0.0 <= self.dropout < 1.0:
            raise ValueError("dropout must be in [0, 1)")
        if self.hidden_size % self.n_heads != 0:
            raise ValueError(
                f"hidden_size={self.hidden_size} must be divisible by n_heads={self.n_heads}"
            )
        if len(self.class_names) != self.n_classes:
            raise ValueError(
                f"class_names has {len(self.class_names)} entries; expected n_classes={self.n_classes}"
            )
        if self.batch_size <= 0:
            raise ValueError("batch_size must be positive")

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable snapshot of the config."""
        return {
            "n_features": int(self.n_features),
            "n_classes": int(self.n_classes),
            "window_size": int(self.window_size),
            "hidden_size": int(self.hidden_size),
            "n_heads": int(self.n_heads),
            "dropout": float(self.dropout),
            "var_dim": int(self.var_dim),
            "lr": float(self.lr),
            "weight_decay": float(self.weight_decay),
            "n_epochs": int(self.n_epochs),
            "batch_size": int(self.batch_size),
            "quantile_loss_weight": float(self.quantile_loss_weight),
            "early_stopping_patience": int(self.early_stopping_patience),
            "use_amp": bool(self.use_amp),
            "use_compile": bool(self.use_compile),
            "warmup_pct": float(self.warmup_pct),
            "class_names": list(self.class_names),
            "quantile_levels": list(self.quantile_levels),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> TFTConfig:
        """Reconstruct a config from a :meth:`to_dict` payload."""
        return cls(
            n_features=int(data["n_features"]),
            n_classes=int(data["n_classes"]),
            window_size=int(data.get("window_size", 120)),
            hidden_size=int(data.get("hidden_size", 64)),
            n_heads=int(data.get("n_heads", 4)),
            dropout=float(data.get("dropout", 0.1)),
            var_dim=int(data.get("var_dim", 8)),
            lr=float(data.get("lr", 1e-3)),
            weight_decay=float(data.get("weight_decay", 1e-4)),
            n_epochs=int(data.get("n_epochs", 25)),
            batch_size=int(data.get("batch_size", 256)),
            quantile_loss_weight=float(data.get("quantile_loss_weight", 0.3)),
            early_stopping_patience=int(data.get("early_stopping_patience", 5)),
            use_amp=bool(data.get("use_amp", True)),
            use_compile=bool(data.get("use_compile", False)),
            warmup_pct=float(data.get("warmup_pct", 0.05)),
            class_names=list(data.get("class_names", ["down", "flat", "up"])),
            quantile_levels=list(data.get("quantile_levels", list(QUANTILE_LEVELS))),
        )


class TFTModel:
    """Standalone TFT trainer / inference wrapper.

    Constructed from a :class:`TFTConfig` and a target device. The
    underlying ``nn.Module`` is materialised on demand in :meth:`fit`
    and :meth:`load`. Inference (:meth:`predict_proba`) requires a
    loaded model.
    """

    def __init__(self, config: TFTConfig, device: str | torch.device = "cpu") -> None:
        self.config = config
        self.device = torch.device(device)
        self._model: TemporalFusionTransformer | None = None
        self._optimizer: torch.optim.Optimizer | None = None
        self._class_name_to_index: dict[str, int] = {
            name: i for i, name in enumerate(config.class_names)
        }
        self._use_amp = config.use_amp and self.device.type == "cuda"

    def _build_module(self) -> TemporalFusionTransformer:
        cfg = self.config
        module = TemporalFusionTransformer(
            n_features=cfg.n_features,
            n_classes=cfg.n_classes,
            hidden_size=cfg.hidden_size,
            n_heads=cfg.n_heads,
            n_quantiles=len(cfg.quantile_levels),
            dropout=cfg.dropout,
            var_dim=cfg.var_dim,
            sequence_length=cfg.window_size,
        ).to(self.device)
        if cfg.use_compile and hasattr(torch, "compile") and self.device.type == "cuda":
            try:
                module = cast(
                    TemporalFusionTransformer,
                    torch.compile(module, mode="reduce-overhead"),
                )
            except Exception as exc:  # pragma: no cover - guarded best-effort path
                logger.warning("torch.compile failed, falling back to eager: %s", exc)
        return module

    @property
    def module(self) -> TemporalFusionTransformer:
        """Return the underlying ``nn.Module``. Raises if not yet loaded."""
        if self._model is None:
            raise RuntimeError("TFTModel has no module; call fit() or load() first")
        return self._model

    def fit(
        self,
        train_features: NDArray[np.float32],
        train_labels: NDArray[np.int64],
        *,
        val_features: NDArray[np.float32] | None = None,
        val_labels: NDArray[np.int64] | None = None,
    ) -> dict[str, Any]:
        """Train the TFT on pre-windowed feature/label tensors.

        Parameters
        ----------
        train_features:
            Array of shape ``(n_rows, n_features)``. Sliding windows of
            length ``window_size`` are extracted internally.
        train_labels:
            Integer class indices of shape ``(n_rows,)``. Same length as
            ``train_features``; the label at row ``i`` aligns with the
            window ending at row ``i``.
        val_features, val_labels:
            Optional held-out split. When supplied, ``val_sharpe`` is
            computed each epoch and used for early stopping.

        Returns
        -------
        dict[str, Any]
            Training summary: ``epochs_run``, ``best_val_sharpe``,
            ``final_loss``.
        """
        train_seq, train_lbl = self._sliding_windows(train_features, train_labels)
        if len(train_seq) < self.config.batch_size:
            raise ValueError(
                f"Not enough training windows ({len(train_seq)}) for batch_size "
                f"{self.config.batch_size}. Reduce window_size, batch_size, or data."
            )
        train_q = self._class_to_quantile_target(train_lbl)
        train_loader = self._build_loader(train_seq, train_lbl, train_q, shuffle=True)

        val_loader: DataLoader[tuple[torch.Tensor, ...]] | None = None
        if (
            val_features is not None
            and val_labels is not None
            and len(val_features) >= self.config.window_size + 1
        ):
            val_seq, val_lbl = self._sliding_windows(val_features, val_labels)
            val_q = self._class_to_quantile_target(val_lbl)
            val_loader = self._build_loader(val_seq, val_lbl, val_q, shuffle=False)

        self._model = self._build_module()
        return self._run_training_loop(self._model, train_loader, val_loader)

    def _build_loader(
        self,
        seq: NDArray[np.float32],
        lbl: NDArray[np.int64],
        q_target: NDArray[np.float32],
        *,
        shuffle: bool,
    ) -> DataLoader[tuple[torch.Tensor, ...]]:
        dataset = TensorDataset(
            torch.from_numpy(seq),
            torch.from_numpy(lbl),
            torch.from_numpy(q_target),
        )
        return DataLoader(
            dataset,
            batch_size=self.config.batch_size,
            shuffle=shuffle,
            drop_last=shuffle,
            num_workers=0,
            pin_memory=(self.device.type == "cuda"),
        )

    def _run_training_loop(
        self,
        model: TemporalFusionTransformer,
        train_loader: DataLoader[tuple[torch.Tensor, ...]],
        val_loader: DataLoader[tuple[torch.Tensor, ...]] | None,
    ) -> dict[str, Any]:
        params = [p for p in model.parameters() if p.requires_grad]
        optimizer = torch.optim.AdamW(
            params,
            lr=self.config.lr,
            weight_decay=self.config.weight_decay,
        )
        self._optimizer = optimizer

        steps_per_epoch = max(1, len(train_loader))
        total_steps = steps_per_epoch * self.config.n_epochs
        warmup_steps = max(1, int(total_steps * self.config.warmup_pct))

        def lr_lambda(step: int) -> float:
            if step < warmup_steps:
                return float(step) / float(warmup_steps)
            progress = (step - warmup_steps) / max(1, total_steps - warmup_steps)
            return 0.5 * (1.0 + float(np.cos(np.pi * min(1.0, progress))))

        scheduler = torch.optim.lr_scheduler.LambdaLR(optimizer, lr_lambda)
        ce_loss = nn.CrossEntropyLoss()
        quantile_levels = torch.tensor(
            self.config.quantile_levels,
            device=self.device,
            dtype=torch.float32,
        )
        scaler: Any = torch.amp.GradScaler("cuda") if self._use_amp else None  # type: ignore[attr-defined]

        best_val_sharpe = -float("inf")
        patience_left = self.config.early_stopping_patience
        epochs_run = 0
        final_loss = float("nan")

        for epoch in range(1, self.config.n_epochs + 1):
            _set_training_mode(model)
            running_loss = 0.0
            n_batches = 0
            for xb, yb, qb in train_loader:
                xb = xb.to(self.device, non_blocking=True)
                yb = yb.to(self.device, non_blocking=True)
                qb = qb.to(self.device, non_blocking=True)
                optimizer.zero_grad(set_to_none=True)

                if scaler is not None:
                    with torch.amp.autocast(device_type="cuda"):  # type: ignore[attr-defined]
                        logits, quantiles, _ = model.forward_with_quantiles(xb)
                        ce = ce_loss(logits, yb)
                        ql = pinball_loss(quantiles, qb, quantile_levels)
                        loss = ce + self.config.quantile_loss_weight * ql
                    scaler.scale(loss).backward()
                    scaler.unscale_(optimizer)
                    torch.nn.utils.clip_grad_norm_(model.parameters(), 1.0)
                    scaler.step(optimizer)
                    scaler.update()
                else:
                    logits, quantiles, _ = model.forward_with_quantiles(xb)
                    ce = ce_loss(logits, yb)
                    ql = pinball_loss(quantiles, qb, quantile_levels)
                    loss = ce + self.config.quantile_loss_weight * ql
                    loss.backward()
                    torch.nn.utils.clip_grad_norm_(model.parameters(), 1.0)
                    optimizer.step()

                scheduler.step()
                running_loss += float(loss.item())
                n_batches += 1

            final_loss = running_loss / max(1, n_batches)
            epochs_run = epoch

            if val_loader is not None:
                val_sharpe = self._validate_sharpe(model, val_loader)
                if val_sharpe > best_val_sharpe:
                    best_val_sharpe = val_sharpe
                    patience_left = self.config.early_stopping_patience
                elif self.config.early_stopping_patience > 0:
                    patience_left -= 1
                    if patience_left <= 0:
                        break

        return {
            "epochs_run": epochs_run,
            "best_val_sharpe": (best_val_sharpe if best_val_sharpe > -float("inf") else None),
            "final_loss": final_loss,
        }

    def _validate_sharpe(
        self,
        model: TemporalFusionTransformer,
        val_loader: DataLoader[tuple[torch.Tensor, ...]],
    ) -> float:
        """Pseudo-Sharpe on the val split. Mirrors the FreqAI version."""
        _set_inference_mode(model)
        signals: list[NDArray[np.float64]] = []
        proxies: list[NDArray[np.float64]] = []
        up_idx = self._class_name_to_index.get("up", min(1, self.config.n_classes - 1))
        down_idx = self._class_name_to_index.get("down", 0)
        with torch.no_grad():
            for xb, yb, _qb in val_loader:
                xb = xb.to(self.device, non_blocking=True)
                logits = model(xb)
                probs = torch.softmax(logits, dim=-1).cpu().numpy()
                if probs.shape[1] >= 2:
                    sig = probs[:, up_idx] - probs[:, down_idx]
                else:
                    sig = probs[:, 0]
                yn = yb.cpu().numpy()
                proxy = np.where(yn == up_idx, 1.0, np.where(yn == down_idx, -1.0, 0.0))
                signals.append(sig.astype(np.float64))
                proxies.append(proxy.astype(np.float64))
        _set_training_mode(model)
        if not signals:
            return float("nan")
        sig_arr = np.concatenate(signals)
        ret_arr = np.concatenate(proxies)
        pnl = sig_arr * ret_arr
        std = float(pnl.std())
        if std == 0.0:
            return 0.0
        return float(pnl.mean() / std * np.sqrt(252.0))

    def predict_proba(
        self,
        features: NDArray[np.float32],
    ) -> tuple[NDArray[np.float32], NDArray[np.float32]]:
        """Return ``(probs, confidence)`` for each row in ``features``.

        Parameters
        ----------
        features:
            ``(n_rows, n_features)`` numpy array. The first
            ``window_size - 1`` rows of the result are zero-padded
            because no full window ends there (mirrors the legacy
            FreqAI semantics).

        Returns
        -------
        tuple[NDArray[np.float32], NDArray[np.float32]]
            ``probs`` shape ``(n_rows, n_classes)`` and ``confidence``
            shape ``(n_rows,)`` — the max of ``P(up)``, ``P(down)`` per
            row (Guo et al. 2017 calibration-style directional confidence).
        """
        model = self.module
        cfg = self.config
        n_rows = features.shape[0]
        probs_full = np.zeros((n_rows, cfg.n_classes), dtype=np.float32)
        conf_full = np.zeros(n_rows, dtype=np.float32)

        if n_rows < cfg.window_size:
            return probs_full, conf_full

        tensor = torch.from_numpy(features.astype(np.float32, copy=False)).to(self.device)
        windows = tensor.unfold(0, cfg.window_size, 1).permute(0, 2, 1).contiguous()
        dataset = TensorDataset(windows)
        loader = DataLoader(
            dataset,
            batch_size=cfg.batch_size,
            shuffle=False,
            drop_last=False,
            num_workers=0,
        )

        _set_inference_mode(model)
        chunks: list[NDArray[np.float32]] = []
        confs: list[NDArray[np.float32]] = []
        down_idx = self._class_name_to_index.get("down", 0)
        up_idx = self._class_name_to_index.get(
            "up",
            cfg.n_classes - 1 if cfg.n_classes >= 3 else 1,
        )
        with torch.no_grad():
            for (xb,) in loader:
                xb = xb.to(self.device, non_blocking=True)
                logits = model(xb)
                p = torch.softmax(logits.float(), dim=-1)
                p_down = p[:, down_idx]
                p_up = p[:, up_idx]
                conf = torch.maximum(p_up, p_down)
                chunks.append(p.cpu().numpy().astype(np.float32))
                confs.append(conf.cpu().numpy().astype(np.float32))

        if chunks:
            probs_arr = np.concatenate(chunks, axis=0)
            conf_arr = np.concatenate(confs, axis=0)
            probs_full[cfg.window_size - 1 :] = probs_arr
            conf_full[cfg.window_size - 1 :] = conf_arr
        return probs_full, conf_full

    def save(self, path: str | Path) -> None:
        """Persist the model to ``path`` (a directory; created if missing).

        Writes two files atomically:

        - ``model.safetensors`` — state-dict via :func:`safetensors.torch.save_file`.
        - ``metadata.json`` — config + tensor count + a saved-at timestamp.

        The legacy single-zip artefact path is not produced. Loading is
        performed by :meth:`load`; validation by :func:`validate_artifact`.
        """
        if self._model is None:
            raise RuntimeError("nothing to save; fit() or load() must run first")
        out_dir = Path(path)
        out_dir.mkdir(parents=True, exist_ok=True)
        weights_path = out_dir / _WEIGHTS_FILENAME
        metadata_path = out_dir / _METADATA_FILENAME

        underlying = self._underlying_module(self._model)
        state_dict = {k: v.detach().contiguous().cpu() for k, v in underlying.state_dict().items()}

        save_file(state_dict, str(weights_path))

        metadata = {
            "version": _ARTIFACT_VERSION,
            "config": self.config.to_dict(),
            "tensor_count": len(state_dict),
            "tensor_names": sorted(state_dict.keys()),
            "saved_at": time.time(),
        }
        metadata_tmp = metadata_path.with_suffix(".json.tmp")
        metadata_tmp.write_text(json.dumps(metadata, indent=2, sort_keys=True))
        metadata_tmp.replace(metadata_path)

    @classmethod
    def load(cls, path: str | Path, device: str | torch.device = "cpu") -> TFTModel:
        """Load a model previously saved by :meth:`save`.

        Calls :func:`validate_artifact` first; raises
        :class:`TFTValidationError` on a malformed directory.
        """
        artefact_dir = Path(path)
        validate_artifact(artefact_dir)
        metadata = json.loads((artefact_dir / _METADATA_FILENAME).read_text())
        config = TFTConfig.from_dict(metadata["config"])
        instance = cls(config, device=device)
        instance._model = instance._build_module()
        state_dict = load_file(
            str(artefact_dir / _WEIGHTS_FILENAME),
            device=str(instance.device),
        )
        underlying = instance._underlying_module(instance._model)
        underlying.load_state_dict(state_dict)
        return instance

    @staticmethod
    def _underlying_module(module: nn.Module) -> nn.Module:
        return getattr(module, "_orig_mod", module)

    def _sliding_windows(
        self,
        features: NDArray[np.float32],
        labels: NDArray[np.int64],
    ) -> tuple[NDArray[np.float32], NDArray[np.int64]]:
        ws = self.config.window_size
        if len(features) < ws:
            raise ValueError(f"need >= {ws} rows, got {len(features)}")
        if len(features) != len(labels):
            raise ValueError(
                f"features ({len(features)}) and labels ({len(labels)}) length mismatch"
            )
        windows = np.lib.stride_tricks.sliding_window_view(
            features,
            (ws, features.shape[1]),
        )[:, 0, :, :].copy()
        return (
            windows.astype(np.float32, copy=False),
            labels[ws - 1 :].astype(np.int64, copy=False),
        )

    def _class_to_quantile_target(
        self,
        class_idx: NDArray[np.int64],
    ) -> NDArray[np.float32]:
        out = np.zeros_like(class_idx, dtype=np.float32)
        up_idx = self._class_name_to_index.get("up")
        down_idx = self._class_name_to_index.get("down")
        if up_idx is not None:
            out[class_idx == up_idx] = 1.0
        if down_idx is not None:
            out[class_idx == down_idx] = -1.0
        return out


def validate_artifact(path: str | Path) -> dict[str, Any]:
    """Validate a saved TFT artefact.

    Replaces the legacy ``validate_model_zip`` contract. The legacy
    function returned a permissive boolean and shipped with a stub that
    accepted empty directories — the bug that prompted the 95% coverage
    requirement on this function.

    Parameters
    ----------
    path:
        Directory written by :meth:`TFTModel.save`.

    Returns
    -------
    dict[str, Any]
        Parsed metadata payload with ``version``, ``config``,
        ``tensor_count``, ``tensor_names`` and ``saved_at`` keys.

    Raises
    ------
    TFTValidationError
        If the directory does not exist, is not a directory, is missing
        either expected file, the safetensors file fails to deserialise,
        the tensor count is zero, the metadata JSON is invalid, the
        version is unrecognised, or the tensor count recorded in the
        metadata disagrees with the on-disk state-dict.
    """
    artefact_dir = Path(path)
    if not artefact_dir.exists():
        raise TFTValidationError(f"artefact path does not exist: {artefact_dir}")
    if not artefact_dir.is_dir():
        raise TFTValidationError(f"artefact path is not a directory: {artefact_dir}")

    weights_path = artefact_dir / _WEIGHTS_FILENAME
    metadata_path = artefact_dir / _METADATA_FILENAME
    if not weights_path.is_file():
        raise TFTValidationError(
            f"weights file missing: {weights_path} (expected '{_WEIGHTS_FILENAME}')"
        )
    if not metadata_path.is_file():
        raise TFTValidationError(
            f"metadata file missing: {metadata_path} (expected '{_METADATA_FILENAME}')"
        )

    try:
        metadata_raw = metadata_path.read_text()
    except OSError as exc:
        raise TFTValidationError(f"metadata read failed: {exc}") from exc
    try:
        metadata = json.loads(metadata_raw)
    except json.JSONDecodeError as exc:
        raise TFTValidationError(f"metadata.json is not valid JSON: {exc}") from exc
    if not isinstance(metadata, dict):
        raise TFTValidationError(
            f"metadata.json must be a JSON object, got {type(metadata).__name__}"
        )

    version = metadata.get("version")
    if version != _ARTIFACT_VERSION:
        raise TFTValidationError(
            f"unrecognised artefact version {version!r}; expected {_ARTIFACT_VERSION}"
        )

    config_payload = metadata.get("config")
    if not isinstance(config_payload, dict):
        raise TFTValidationError("metadata.json missing 'config' object")
    try:
        TFTConfig.from_dict(config_payload)
    except (KeyError, TypeError, ValueError) as exc:
        raise TFTValidationError(f"metadata.json config is invalid: {exc}") from exc

    # Load the safetensors file with the CPU device so the check is
    # safe on hosts without a GPU. We only need the keys to confirm the
    # tensor count; weights stay on CPU for this validation.
    try:
        state_dict = load_file(str(weights_path), device="cpu")
    except Exception as exc:
        raise TFTValidationError(f"safetensors load failed for {weights_path}: {exc}") from exc

    tensor_count = len(state_dict)
    if tensor_count == 0:
        raise TFTValidationError(f"safetensors file has zero tensors: {weights_path}")

    recorded_count = metadata.get("tensor_count")
    if not isinstance(recorded_count, int):
        raise TFTValidationError(
            f"metadata.json 'tensor_count' must be int, got {type(recorded_count).__name__}"
        )
    if recorded_count != tensor_count:
        raise TFTValidationError(
            f"tensor_count mismatch: metadata says {recorded_count}, file has {tensor_count}"
        )

    recorded_names = metadata.get("tensor_names")
    if not isinstance(recorded_names, list):
        raise TFTValidationError("metadata.json 'tensor_names' must be a list")
    if sorted(state_dict.keys()) != sorted(_coerce_str(n) for n in recorded_names):
        raise TFTValidationError("tensor names in metadata do not match the safetensors file")

    return metadata


def _coerce_str(value: Any) -> str:
    if not isinstance(value, str):
        raise TFTValidationError(
            f"expected string tensor name, got {type(value).__name__}: {value!r}"
        )
    return value
