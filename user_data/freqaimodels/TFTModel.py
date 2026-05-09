"""
FreqAI integration for the Temporal Fusion Transformer.

GPU memory budget on DGX Spark (128 GB unified):
  ModelForge:    ~25 GB (20% during campaigns)
  Hermes 3 70B:  ~40 GB (evicts between 15-min sentiment polls)
  Hermes 3 8B:   ~5 GB  (stays warm)
  TFT training:  ~38 GB cap (this model, set_per_process_memory_fraction=0.3)
  Headroom:      ~20 GB for OS + Docker + spikes

Inherits from BasePyTorchClassifier so the existing strategy keeps using
`dataframe["up"]` / `dataframe["down"]` columns. Adds a `tft_confidence`
column derived from the quantile spread of an auxiliary regression head.

Configurable from `config.json`:

    "freqaimodel": "TFTModel",
    "freqaimodel_path": "user_data/freqaimodels",
    "freqai": {
        "conv_width": 120,                 // sequence length fed to the TFT
        "model_training_parameters": {
            "hidden_size": 64,
            "n_heads": 4,
            "dropout": 0.1,
            "var_dim": 8,
            "lr": 1e-3,
            "weight_decay": 1e-4,
            "n_epochs": 25,
            "batch_size": 256,
            "quantile_loss_weight": 0.3,
            "early_stopping_patience": 5,
            "use_amp": true,
            "use_compile": true,
            "warmup_pct": 0.05
        }
    }

Two-year lookback and 24h retrain are controlled by `freqai.train_period_days`
and `freqai.live_retrain_hours` respectively (config.json, top-level freqai
block — no model-side change needed).
"""

from __future__ import annotations

import logging
import sys
import time
from pathlib import Path
from typing import Any

import numpy as np
import numpy.typing as npt
import pandas as pd
import torch
from pandas import DataFrame
from torch import nn
from torch.utils.data import DataLoader, TensorDataset

from freqtrade.freqai.base_models.BasePyTorchClassifier import BasePyTorchClassifier
from freqtrade.freqai.data_kitchen import FreqaiDataKitchen
from freqtrade.freqai.torch.PyTorchDataConvertor import (
    DefaultPyTorchDataConvertor,
    PyTorchDataConvertor,
)

# Make sibling modules under user_data/ importable from this file
_USER_DATA = Path(__file__).resolve().parent.parent
if str(_USER_DATA) not in sys.path:
    sys.path.insert(0, str(_USER_DATA))

from freqaimodels.tft_architecture import (   # noqa: E402
    TemporalFusionTransformer,
    pinball_loss,
)

logger = logging.getLogger(__name__)

QUANTILE_LEVELS: tuple[float, ...] = (0.1, 0.5, 0.9)


def _set_inference_mode(module: nn.Module) -> None:
    """Equivalent of `module.eval()` — uses .train(False) to avoid hook false positives."""
    module.train(False)


def _set_training_mode(module: nn.Module) -> None:
    module.train(True)


# ---------------------------------------------------------------------------
# Trainer wrapper — minimal surface area FreqAI's BasePyTorchClassifier needs:
# a `model` (nn.Module) and a `model_meta_data` dict, plus save +
# load_from_checkpoint hooks.
# ---------------------------------------------------------------------------


class TFTTrainerWrapper:
    def __init__(self, model: nn.Module, model_meta_data: dict[str, Any]):
        self.model = model
        self.model_meta_data = model_meta_data
        self.optimizer = None  # populated by fit() — kept for save() round-trip

    def save(self, path: Path) -> None:
        torch.save(
            {
                "model_state_dict": self.model.state_dict(),
                "model_meta_data": self.model_meta_data,
                "pytrainer": self,
                "optimizer_state_dict": (
                    self.optimizer.state_dict() if self.optimizer is not None else None
                ),
            },
            path,
        )

    def load_from_checkpoint(self, checkpoint: dict) -> "TFTTrainerWrapper":
        self.model.load_state_dict(checkpoint["model_state_dict"])
        self.model_meta_data = checkpoint["model_meta_data"]
        return self


# ---------------------------------------------------------------------------
# FreqAI prediction model
# ---------------------------------------------------------------------------


class TFTModel(BasePyTorchClassifier):
    @property
    def data_convertor(self) -> PyTorchDataConvertor:
        # features: float32, labels (class indices): long
        return DefaultPyTorchDataConvertor(target_tensor_type=torch.long)

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        cfg = self.freqai_info.get("model_training_parameters", {})
        self.hidden_size = int(cfg.get("hidden_size", 64))
        self.n_heads = int(cfg.get("n_heads", 4))
        self.dropout = float(cfg.get("dropout", 0.1))
        self.var_dim = int(cfg.get("var_dim", 8))
        self.lr = float(cfg.get("lr", 1e-3))
        self.weight_decay = float(cfg.get("weight_decay", 1e-4))
        self.n_epochs = int(cfg.get("n_epochs", 25))
        self.batch_size = int(cfg.get("batch_size", 256))
        self.quantile_loss_weight = float(cfg.get("quantile_loss_weight", 0.3))
        self.early_stopping_patience = int(cfg.get("early_stopping_patience", 5))
        self.use_amp = bool(cfg.get("use_amp", True)) and self.device == "cuda"
        self.use_compile = bool(cfg.get("use_compile", True))
        self.warmup_pct = float(cfg.get("warmup_pct", 0.05))

        if self.window_size < 4:
            logger.warning(
                "TFT works best with conv_width ≥ 60; you set %d", self.window_size,
            )

        logger.info(
            "TFTModel init: device=%s window=%d hidden=%d heads=%d epochs=%d "
            "batch=%d amp=%s compile=%s",
            self.device, self.window_size, self.hidden_size, self.n_heads,
            self.n_epochs, self.batch_size, self.use_amp, self.use_compile,
        )

    # ---------------------------------------------------------------
    # Fit
    # ---------------------------------------------------------------

    def fit(self, data_dictionary: dict, dk: FreqaiDataKitchen, **kwargs) -> Any:
        # Guard against ballooning past our GPU budget. The Spark's 128 GB
        # unified memory is shared with ModelForge + Hermes 3 70B/8B; without
        # a cap, TFT's autograd cache can drift past 50 GB on large windows
        # and starve the Ollama inference path. Cap to 30% (~38 GB).
        try:
            if str(self.device) == "cuda":
                torch.cuda.set_per_process_memory_fraction(0.3)
                logger.info("GPU memory fraction capped at 30%% (~38 GB of 128 GB unified)")
        except Exception as exc:
            logger.warning("Could not set GPU memory fraction: %s", exc)

        class_names = self.get_class_names()
        self.convert_label_column_to_int(data_dictionary, dk, class_names)

        n_features = data_dictionary["train_features"].shape[-1]
        n_classes = len(class_names)

        model = TemporalFusionTransformer(
            n_features=n_features,
            n_classes=n_classes,
            hidden_size=self.hidden_size,
            n_heads=self.n_heads,
            n_quantiles=len(QUANTILE_LEVELS),
            dropout=self.dropout,
            var_dim=self.var_dim,
            sequence_length=self.window_size,
        ).to(self.device)

        if self.use_compile and hasattr(torch, "compile"):
            try:
                model = torch.compile(model, mode="reduce-overhead")
                logger.info("torch.compile enabled (reduce-overhead)")
            except Exception as exc:
                logger.warning("torch.compile failed, falling back to eager: %s", exc)

        train_loader, val_loader = self._build_loaders(
            data_dictionary, n_features, n_classes,
        )
        optimizer = self._train(
            model, train_loader, val_loader, n_classes,
            pair=getattr(dk, "pair", None),
        )

        wrapper = TFTTrainerWrapper(
            model=model,
            model_meta_data={
                "class_names": class_names,
                "n_features": n_features,
                "window_size": self.window_size,
                "quantile_levels": list(QUANTILE_LEVELS),
            },
        )
        wrapper.optimizer = optimizer
        return wrapper

    # ---------------------------------------------------------------
    # Predict — windowed slide for classification probs + quantile spread
    # ---------------------------------------------------------------

    def predict(
        self, unfiltered_df: DataFrame, dk: FreqaiDataKitchen, **kwargs,
    ) -> tuple[DataFrame, npt.NDArray[np.int_]]:
        class_names = self.model.model_meta_data.get("class_names")
        if not class_names:
            raise ValueError("class_names missing from model_meta_data")
        if not self.class_name_to_index:
            self.init_class_names_to_index_mapping(class_names)

        dk.find_features(unfiltered_df)
        filtered_df, _ = dk.filter_features(
            unfiltered_df, dk.training_features_list, training_filter=False,
        )
        dk.data_dictionary["prediction_features"] = filtered_df
        dk.data_dictionary["prediction_features"], outliers, _ = (
            dk.feature_pipeline.transform(
                dk.data_dictionary["prediction_features"], outlier_check=True,
            )
        )

        feats_tensor = self.data_convertor.convert_x(
            dk.data_dictionary["prediction_features"], device=self.device,
        )                                                # (n_rows, n_features)

        n_rows = feats_tensor.shape[0]
        ws = self.window_size
        n_classes = len(class_names)

        probs_full = np.zeros((n_rows, n_classes), dtype=np.float32)
        confidence_full = np.zeros(n_rows, dtype=np.float32)
        pred_idx = np.zeros(n_rows, dtype=np.int64)

        underlying = self._underlying_module()
        _set_inference_mode(underlying)

        if n_rows >= ws:
            # Sliding windows; for very large eval sets this could be chunked.
            windows = feats_tensor.unfold(0, ws, 1).permute(0, 2, 1).contiguous()
            ds = TensorDataset(windows)
            dl = DataLoader(
                ds, batch_size=self.batch_size, shuffle=False, drop_last=False,
            )

            all_probs: list[np.ndarray] = []
            all_conf: list[np.ndarray] = []
            with torch.no_grad():
                for (xb,) in dl:
                    xb = xb.to(self.device, non_blocking=True)
                    if self.use_amp:
                        with torch.amp.autocast(device_type="cuda"):
                            logits, quantiles, _ = underlying.forward_with_quantiles(xb)
                    else:
                        logits, quantiles, _ = underlying.forward_with_quantiles(xb)

                    p = torch.softmax(logits.float(), dim=-1)
                    spread = (quantiles[:, -1] - quantiles[:, 0]).abs().float()
                    conf = 1.0 / (1.0 + spread)
                    all_probs.append(p.cpu().numpy())
                    all_conf.append(conf.cpu().numpy())

            probs = np.concatenate(all_probs, axis=0)
            conf = np.concatenate(all_conf, axis=0)

            probs_full[ws - 1:] = probs
            confidence_full[ws - 1:] = conf
            pred_idx[ws - 1:] = probs.argmax(axis=1)

        predicted_classes_str = [
            self.index_to_class_name.get(int(i), class_names[0]) for i in pred_idx
        ]
        for i in range(min(ws - 1, n_rows)):
            predicted_classes_str[i] = class_names[0]

        pred_df_prob = pd.DataFrame(probs_full, columns=class_names)
        pred_df = pd.DataFrame(predicted_classes_str, columns=[dk.label_list[0]])
        pred_df = pd.concat([pred_df, pred_df_prob], axis=1)
        pred_df["tft_confidence"] = confidence_full

        if dk.feature_pipeline["di"]:
            dk.DI_values = dk.feature_pipeline["di"].di_values
        else:
            dk.DI_values = np.zeros(outliers.shape[0])

        do_predict = (
            outliers.copy() if hasattr(outliers, "copy") else np.asarray(outliers)
        )
        if len(do_predict) >= ws:
            do_predict[: ws - 1] = 0
        dk.do_predict = do_predict

        return pred_df, dk.do_predict

    # ---------------------------------------------------------------
    # Helpers
    # ---------------------------------------------------------------

    def _underlying_module(self) -> nn.Module:
        """Return the bare nn.Module even if torch.compile wrapped it."""
        m = self.model.model
        return getattr(m, "_orig_mod", m)

    def _build_loaders(
        self, data_dictionary: dict, n_features: int, n_classes: int,
    ) -> tuple[DataLoader, DataLoader | None]:
        train_x = data_dictionary["train_features"].to_numpy(dtype=np.float32)
        train_y = data_dictionary["train_labels"].to_numpy().reshape(-1).astype(np.int64)

        train_seq, train_lbl = self._sliding_windows(train_x, train_y)
        if len(train_seq) < self.batch_size:
            raise ValueError(
                f"Not enough training windows ({len(train_seq)}) for batch_size "
                f"{self.batch_size}. Reduce conv_width, batch_size, or train_period_days."
            )

        train_q = self._class_to_target(train_lbl)

        train_ds = TensorDataset(
            torch.from_numpy(train_seq),
            torch.from_numpy(train_lbl),
            torch.from_numpy(train_q),
        )
        train_loader = DataLoader(
            train_ds,
            batch_size=self.batch_size,
            shuffle=True,
            drop_last=True,
            num_workers=0,
            pin_memory=(self.device == "cuda"),
        )

        val_loader = None
        if "test" in self.splits and "test_features" in data_dictionary:
            test_x = data_dictionary["test_features"].to_numpy(dtype=np.float32)
            test_y = data_dictionary["test_labels"].to_numpy().reshape(-1).astype(np.int64)
            if len(test_x) >= self.window_size + 1:
                test_seq, test_lbl = self._sliding_windows(test_x, test_y)
                test_q = self._class_to_target(test_lbl)
                val_ds = TensorDataset(
                    torch.from_numpy(test_seq),
                    torch.from_numpy(test_lbl),
                    torch.from_numpy(test_q),
                )
                val_loader = DataLoader(
                    val_ds,
                    batch_size=self.batch_size,
                    shuffle=False,
                    drop_last=False,
                    num_workers=0,
                    pin_memory=(self.device == "cuda"),
                )

        return train_loader, val_loader

    def _sliding_windows(
        self, x: np.ndarray, y: np.ndarray,
    ) -> tuple[np.ndarray, np.ndarray]:
        ws = self.window_size
        if len(x) < ws:
            raise ValueError(f"Need ≥{ws} rows, got {len(x)}")
        windows = np.lib.stride_tricks.sliding_window_view(
            x, (ws, x.shape[1]),
        )[:, 0, :, :].copy()
        labels = y[ws - 1:]
        return windows.astype(np.float32, copy=False), labels.astype(np.int64, copy=False)

    def _class_to_target(self, class_idx: np.ndarray) -> np.ndarray:
        """down → -1, up → +1, anything else → 0 (proxy for the quantile head)."""
        c2i = self.class_name_to_index
        up_idx = c2i.get("up")
        down_idx = c2i.get("down")
        out = np.zeros_like(class_idx, dtype=np.float32)
        if up_idx is not None:
            out[class_idx == up_idx] = 1.0
        if down_idx is not None:
            out[class_idx == down_idx] = -1.0
        return out

    # ---------------------------------------------------------------
    # Training loop with AdamW + cosine LR + AMP + early stop on val-Sharpe
    # ---------------------------------------------------------------

    # ---------------------------------------------------------------
    # Per-epoch resume checkpoints — survives mid-training restarts
    # without paying the full cold-start cost again. State-dict-only
    # so we never depend on the freqai pickle path being intact, and
    # torch.load(weights_only=True) safely round-trips with no class
    # imports needed (custom freqaimodels/ namespace would be unsafe).
    # ---------------------------------------------------------------

    _RESUME_VERSION = 1
    _RESUME_MAX_AGE_HOURS = 4.0

    def _resume_checkpoint_path(self, pair: str) -> Path:
        identifier = self.freqai_info.get("identifier", "tft_v1")
        base = Path("/freqtrade/user_data/models") / identifier / "checkpoints"
        base.mkdir(parents=True, exist_ok=True)
        safe = pair.replace("/", "_").replace("\\", "_")
        return base / f"{safe}_resume.pt"

    def _save_resume_checkpoint(
        self,
        model: nn.Module,
        optimizer: torch.optim.Optimizer,
        epoch: int,
        best_val_metric: float,
        pair: str | None,
    ) -> None:
        if not pair:
            return
        path = self._resume_checkpoint_path(pair)
        tmp = path.with_suffix(".tmp")
        underlying = getattr(model, "_orig_mod", model)
        # Only basic types + tensors so torch.load(weights_only=True) is safe.
        # Scheduler/scaler state intentionally omitted — they're deterministic
        # functions of (optimizer, global_step) and rebuild cleanly on resume.
        try:
            torch.save(
                {
                    "version": self._RESUME_VERSION,
                    "epoch": int(epoch),
                    "best_val_metric": float(best_val_metric),
                    "model_state_dict": underlying.state_dict(),
                    "optimizer_state_dict": optimizer.state_dict(),
                    "saved_at": float(time.time()),
                    "n_epochs_target": int(self.n_epochs),
                    "model_arch": {
                        "hidden_size": int(self.hidden_size),
                        "n_heads": int(self.n_heads),
                        "dropout": float(self.dropout),
                        "var_dim": int(self.var_dim),
                        "window_size": int(self.window_size),
                    },
                },
                tmp,
            )
            tmp.replace(path)
        except Exception as exc:
            logger.warning("[%s] resume checkpoint save failed: %s", pair, exc)

    def _load_resume_checkpoint(
        self,
        model: nn.Module,
        optimizer: torch.optim.Optimizer,
        pair: str | None,
    ) -> tuple[int, float] | None:
        if not pair:
            return None
        path = self._resume_checkpoint_path(pair)
        if not path.exists():
            return None
        # weights_only=True restricts deserialization to tensors + primitive
        # containers — no arbitrary-class unpickling, no code execution risk.
        try:
            ck = torch.load(path, map_location=self.device, weights_only=True)
        except Exception as exc:
            logger.warning("[%s] resume checkpoint load failed: %s", pair, exc)
            return None
        age_h = (time.time() - ck.get("saved_at", 0)) / 3600.0
        if age_h > self._RESUME_MAX_AGE_HOURS:
            logger.info(
                "[%s] resume checkpoint stale (%.1fh > %.1fh) — discarding",
                pair, age_h, self._RESUME_MAX_AGE_HOURS,
            )
            return None
        arch = ck.get("model_arch", {}) or {}
        if (arch.get("hidden_size") != self.hidden_size
                or arch.get("window_size") != self.window_size
                or arch.get("n_heads") != self.n_heads
                or arch.get("var_dim") != self.var_dim):
            logger.info("[%s] resume checkpoint architecture mismatch — discarding", pair)
            return None
        if ck.get("n_epochs_target") != self.n_epochs:
            logger.info("[%s] n_epochs changed — discarding checkpoint", pair)
            return None
        try:
            underlying = getattr(model, "_orig_mod", model)
            underlying.load_state_dict(ck["model_state_dict"])
            optimizer.load_state_dict(ck["optimizer_state_dict"])
            start_epoch = int(ck.get("epoch", 0))
            best_val = float(ck.get("best_val_metric", -float("inf")))
            logger.info(
                "[%s] resuming from epoch %d (saved %.1fh ago, best_val_sharpe=%.3f)",
                pair, start_epoch, age_h, best_val,
            )
            return start_epoch, best_val
        except Exception as exc:
            logger.warning("[%s] resume restore failed: %s", pair, exc)
            return None

    def _clear_resume_checkpoint(self, pair: str | None) -> None:
        if not pair:
            return
        try:
            self._resume_checkpoint_path(pair).unlink(missing_ok=True)
        except Exception:
            pass

    def _train(
        self,
        model: nn.Module,
        train_loader: DataLoader,
        val_loader: DataLoader | None,
        n_classes: int,
        pair: str | None = None,
    ) -> torch.optim.Optimizer:
        underlying = getattr(model, "_orig_mod", model)
        params = [p for p in model.parameters() if p.requires_grad]
        optimizer = torch.optim.AdamW(
            params, lr=self.lr, weight_decay=self.weight_decay,
        )

        steps_per_epoch = max(1, len(train_loader))
        total_steps = steps_per_epoch * self.n_epochs
        warmup_steps = max(1, int(total_steps * self.warmup_pct))

        def lr_lambda(step: int) -> float:
            if step < warmup_steps:
                return float(step) / float(warmup_steps)
            progress = (step - warmup_steps) / max(1, total_steps - warmup_steps)
            return 0.5 * (1.0 + np.cos(np.pi * min(1.0, progress)))

        scheduler = torch.optim.lr_scheduler.LambdaLR(optimizer, lr_lambda)

        ce_loss = nn.CrossEntropyLoss()
        quantile_levels = torch.tensor(
            QUANTILE_LEVELS, device=self.device, dtype=torch.float32,
        )
        scaler = torch.amp.GradScaler("cuda") if self.use_amp else None

        best_val_metric = -float("inf")
        patience_left = self.early_stopping_patience
        global_step = 0
        start_epoch = 1

        # Resume from a recent checkpoint if one exists. On any failure
        # we fall through to fresh training — never blocks the cold path.
        resume = self._load_resume_checkpoint(model, optimizer, pair)
        if resume is not None:
            saved_epoch, saved_best = resume
            start_epoch = saved_epoch + 1
            best_val_metric = saved_best
            global_step = (start_epoch - 1) * steps_per_epoch
            if start_epoch > self.n_epochs:
                logger.info(
                    "[%s] checkpoint already at full epochs (%d) — skipping training",
                    pair, saved_epoch,
                )
                self._clear_resume_checkpoint(pair)
                return optimizer
            # Fast-forward LR scheduler so lr matches the resumed step.
            for _ in range(global_step):
                scheduler.step()

        for epoch in range(start_epoch, self.n_epochs + 1):
            _set_training_mode(model)
            running_loss = 0.0
            running_ce = 0.0
            running_q = 0.0
            n_batches = 0

            for xb, yb, qb in train_loader:
                xb = xb.to(self.device, non_blocking=True)
                yb = yb.to(self.device, non_blocking=True)
                qb = qb.to(self.device, non_blocking=True)
                optimizer.zero_grad(set_to_none=True)

                if scaler is not None:
                    with torch.amp.autocast(device_type="cuda"):
                        logits, quantiles, _ = underlying.forward_with_quantiles(xb)
                        ce = ce_loss(logits, yb)
                        ql = pinball_loss(quantiles, qb, quantile_levels)
                        loss = ce + self.quantile_loss_weight * ql
                    scaler.scale(loss).backward()
                    scaler.unscale_(optimizer)
                    torch.nn.utils.clip_grad_norm_(model.parameters(), 1.0)
                    scaler.step(optimizer)
                    scaler.update()
                else:
                    logits, quantiles, _ = underlying.forward_with_quantiles(xb)
                    ce = ce_loss(logits, yb)
                    ql = pinball_loss(quantiles, qb, quantile_levels)
                    loss = ce + self.quantile_loss_weight * ql
                    loss.backward()
                    torch.nn.utils.clip_grad_norm_(model.parameters(), 1.0)
                    optimizer.step()

                scheduler.step()
                global_step += 1
                running_loss += loss.item()
                running_ce += ce.item()
                running_q += ql.item()
                n_batches += 1

            avg_loss = running_loss / max(1, n_batches)
            avg_ce = running_ce / max(1, n_batches)
            avg_q = running_q / max(1, n_batches)

            if val_loader is not None:
                val_metric = self._validate_sharpe(underlying, val_loader, n_classes)
            else:
                val_metric = float("nan")

            logger.info(
                "epoch %d/%d  loss=%.4f (ce=%.4f q=%.4f)  val_sharpe=%.3f  "
                "lr=%.2e  step=%d",
                epoch, self.n_epochs, avg_loss, avg_ce, avg_q, val_metric,
                scheduler.get_last_lr()[0], global_step,
            )

            if val_loader is not None and self.early_stopping_patience > 0:
                if val_metric > best_val_metric:
                    best_val_metric = val_metric
                    patience_left = self.early_stopping_patience
                else:
                    patience_left -= 1
                    if patience_left <= 0:
                        logger.info(
                            "early stopping at epoch %d (best val_sharpe=%.3f)",
                            epoch, best_val_metric,
                        )
                        break

            # Atomic per-epoch checkpoint — resumes pick up the last completed
            # epoch, never a partial one. Save errors are logged, not raised.
            self._save_resume_checkpoint(
                model, optimizer, epoch, best_val_metric, pair,
            )

        # Training complete (full n_epochs or early stop) — drop the checkpoint
        # so a fresh retrain in 24h won't accidentally resume from this run.
        self._clear_resume_checkpoint(pair)
        return optimizer

    def _validate_sharpe(
        self, underlying: nn.Module, val_loader: DataLoader, n_classes: int,
    ) -> float:
        """
        Pseudo-Sharpe on the validation split: signal = up_prob - down_prob,
        proxy return = +1 for up label, -1 for down label.
        """
        _set_inference_mode(underlying)
        signals: list[np.ndarray] = []
        proxies: list[np.ndarray] = []
        c2i = self.class_name_to_index
        up_idx = c2i.get("up", 1)
        down_idx = c2i.get("down", 0)

        with torch.no_grad():
            for xb, yb, _qb in val_loader:
                xb = xb.to(self.device, non_blocking=True)
                logits = underlying(xb)
                probs = torch.softmax(logits, dim=-1).cpu().numpy()
                if probs.shape[1] >= 2:
                    sig = probs[:, up_idx] - probs[:, down_idx]
                else:
                    sig = probs[:, 0]
                yn = yb.cpu().numpy()
                proxy = np.where(yn == up_idx, 1.0, np.where(yn == down_idx, -1.0, 0.0))
                signals.append(sig)
                proxies.append(proxy)

        _set_training_mode(underlying)
        if not signals:
            return float("nan")
        sig = np.concatenate(signals)
        ret = np.concatenate(proxies)
        pnl = sig * ret
        if pnl.std() == 0:
            return 0.0
        return float(pnl.mean() / pnl.std() * np.sqrt(252.0))


# ---------------------------------------------------------------------------
# Pickle / sys.modules registration for freqai's spec-loader
# ---------------------------------------------------------------------------
#
# freqtrade's IResolver loads custom freqaimodels via:
#     spec  = importlib.util.spec_from_file_location("TFTModel", path)
#     mod   = importlib.util.module_from_spec(spec)
#     spec.loader.exec_module(mod)
# WITHOUT registering ``mod`` in ``sys.modules``. So at save() time
# torch.save's serializer raises:
#     Can't pickle <class 'TFTModel.TFTTrainerWrapper'>: No module named 'TFTModel'
# because the wrapper's __module__ is "TFTModel" (file stem) and that
# name isn't in sys.modules.
#
# Without the model save, freqai never writes pair_dictionary.json and
# load_data() returns null predictions for every pair forever.
#
# Fix: at end-of-file (after all class definitions), build a proxy module
# from the current globals() and register it as sys.modules["TFTModel"].
#
# IMPORTANT: do NOT change ``TFTModel.__module__`` — freqai's
# IResolver._search_object validates the loaded class via
#     obj.__module__ == module_name   # module_name = "TFTModel" (file stem)
# Pinning __module__ to "freqaimodels.TFTModel" makes the resolver reject
# the class entirely with "Impossible to load FreqaiModel 'TFTModel'."
import sys as _sys
import types as _types


def _register_module_aliases() -> None:
    if "TFTModel" not in _sys.modules:
        proxy = _types.ModuleType("TFTModel")
        for k, v in globals().items():
            if not k.startswith("_"):
                proxy.__dict__[k] = v
        _sys.modules["TFTModel"] = proxy


_register_module_aliases()
