"""
Stock-side TFT predictor (ALPHA — paper-pilot only).

Architecture is a small TFT-flavoured transformer:
  Linear projection → LSTM → multi-head attention → classifier head

Smaller than the crypto TFT because:
  - Daily bars, fewer features (12 vs 50+)
  - Single horizon target (5-day direction) — no quantile regression
  - Optimised for ~250K samples × 60-day windows; ~30 min on the
    Spark's Blackwell GPU at fp16.

Training contract
  - Walk-forward split (no random)
  - Per-ticker normalization computed on train portion only
  - Class-weighted loss (down/flat/up are imbalanced)
  - Early stop on val accuracy with patience=4
  - Mixed precision via torch.amp on CUDA

Inference contract
  - Returns dict {0: down_prob, 1: flat_prob, 2: up_prob, "confidence":
    max - second}. Caller decides what to do with confidence.
  - Logs `[STOCKS_ML_ALPHA]` at every call so we have a trace if
    untested predictions caused a bad decision.

Persisted model includes:
  state_dict, config, per-ticker norms, train/val accuracy, code git hash.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Optional

import numpy as np
import torch
import torch.nn as nn
from torch.utils.data import DataLoader

from .dataset_stock import StockDataset
from .features_stock import FEATURE_COLS

logger = logging.getLogger(__name__)


@dataclass
class TFTStockConfig:
    sequence_length: int = 60
    num_features: int = len(FEATURE_COLS)
    hidden_dim: int = 64
    num_heads: int = 4
    num_classes: int = 3       # down / flat / up
    dropout: float = 0.15
    batch_size: int = 256
    learning_rate: float = 1e-3
    weight_decay: float = 1e-5
    epochs: int = 25
    early_stop_patience: int = 4
    # When False, the trainer ignores `early_stop_patience` and always
    # runs every epoch in [0, epochs). Useful for diagnostic full-curve
    # runs (does the model actually plateau, overfit catastrophically,
    # or oscillate?). Default keeps early stopping ON for production
    # GPU efficiency.
    enable_early_stopping: bool = True
    gpu_memory_fraction: float = 0.20
    target_horizon_days: int = 5

    def to_dict(self) -> dict:
        return asdict(self)


class StockTFT(nn.Module):
    def __init__(self, cfg: TFTStockConfig):
        super().__init__()
        self.cfg = cfg
        self.input_proj = nn.Linear(cfg.num_features, cfg.hidden_dim)
        self.lstm = nn.LSTM(
            cfg.hidden_dim, cfg.hidden_dim, num_layers=2,
            dropout=cfg.dropout, batch_first=True,
        )
        self.attention = nn.MultiheadAttention(
            cfg.hidden_dim, cfg.num_heads,
            dropout=cfg.dropout, batch_first=True,
        )
        self.norm = nn.LayerNorm(cfg.hidden_dim)
        self.head = nn.Sequential(
            nn.Linear(cfg.hidden_dim, cfg.hidden_dim),
            nn.GELU(),
            nn.Dropout(cfg.dropout),
            nn.Linear(cfg.hidden_dim, cfg.num_classes),
        )

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        # x: (batch, seq_len, num_features)
        h = self.input_proj(x)               # (b, s, hidden)
        h, _ = self.lstm(h)                  # (b, s, hidden)
        a, _ = self.attention(h, h, h)       # (b, s, hidden)
        h = self.norm(h + a)
        return self.head(h[:, -1, :])        # final-step classifier


def _device() -> str:
    return "cuda" if torch.cuda.is_available() else "cpu"


def _class_weights(labels: list[int]) -> torch.Tensor:
    """Inverse-frequency class weights for the imbalanced 3-way task."""
    counts = np.bincount(labels, minlength=3).astype(np.float64)
    counts = np.where(counts == 0, 1.0, counts)
    inv = counts.sum() / (3.0 * counts)
    return torch.tensor(inv, dtype=torch.float32)


def _stack_batch(batch):
    return (
        torch.stack([s[0] for s in batch]),
        torch.stack([s[1] for s in batch]),
    )


def train(
    kb_dir: Path,
    *,
    cfg: Optional[TFTStockConfig] = None,
    output_dir: Optional[Path] = None,
    tickers: Optional[list[str]] = None,
    max_train_samples: Optional[int] = None,
) -> dict:
    """Train one cross-ticker TFT on all S&P 500 historical bars."""
    cfg = cfg or TFTStockConfig()
    output_dir = output_dir or Path(__file__).resolve().parents[2] / "kb" / "models" / "tft"
    output_dir.mkdir(parents=True, exist_ok=True)
    device = _device()

    if device == "cuda":
        try:
            torch.cuda.set_per_process_memory_fraction(cfg.gpu_memory_fraction)
            logger.info("CUDA memory cap: %.0f%% (Stocks TFT)", cfg.gpu_memory_fraction * 100)
        except Exception as exc:
            logger.warning("set_per_process_memory_fraction failed: %s", exc)

    logger.info("[STOCKS_ML_ALPHA] training TFT on device=%s", device)
    train_ds = StockDataset(
        kb_dir, tickers=tickers,
        sequence_length=cfg.sequence_length,
        target_horizon_days=cfg.target_horizon_days,
        split="train",
        max_samples=max_train_samples,
    )
    val_ds = StockDataset(
        kb_dir, tickers=tickers,
        sequence_length=cfg.sequence_length,
        target_horizon_days=cfg.target_horizon_days,
        split="val",
        splits=train_ds.splits,
    )

    if len(train_ds) == 0 or len(val_ds) == 0:
        raise RuntimeError(
            f"Insufficient data: train={len(train_ds)}, val={len(val_ds)}. "
            f"Refresh stocks/kb/historical_bars/ first."
        )

    train_loader = DataLoader(
        train_ds, batch_size=cfg.batch_size, shuffle=True, drop_last=True,
        num_workers=0, collate_fn=_stack_batch,
    )
    val_loader = DataLoader(
        val_ds, batch_size=cfg.batch_size, shuffle=False, collate_fn=_stack_batch,
    )

    train_labels = [int(train_ds._ticker_targets[t].loc[a]) for t, a in train_ds._index]
    cw = _class_weights(train_labels).to(device)

    model = StockTFT(cfg).to(device)
    opt = torch.optim.AdamW(model.parameters(), lr=cfg.learning_rate,
                            weight_decay=cfg.weight_decay)
    sched = torch.optim.lr_scheduler.CosineAnnealingLR(opt, T_max=cfg.epochs)
    loss_fn = nn.CrossEntropyLoss(weight=cw, label_smoothing=0.05)
    use_amp = device == "cuda"
    scaler = torch.amp.GradScaler("cuda", enabled=use_amp)

    best_val_acc = 0.0
    best_epoch = -1
    patience = 0
    history: list[dict] = []
    weights_path = output_dir / "stock_tft_v1.pt"

    for epoch in range(cfg.epochs):
        t_start = time.monotonic()
        model.train()
        train_loss = 0.0
        train_n = 0
        for X, y in train_loader:
            X = X.to(device, non_blocking=True)
            y = y.to(device, non_blocking=True)
            opt.zero_grad(set_to_none=True)
            with torch.amp.autocast("cuda", enabled=use_amp):
                logits = model(X)
                loss = loss_fn(logits, y)
            scaler.scale(loss).backward()
            scaler.unscale_(opt)
            torch.nn.utils.clip_grad_norm_(model.parameters(), 1.0)
            scaler.step(opt)
            scaler.update()
            train_loss += float(loss.item()) * X.size(0)
            train_n += X.size(0)
        sched.step()

        # Validate (model.eval() puts dropout/BN into deterministic mode)
        _validate_into_history(model, val_loader, history, epoch, train_loss, train_n,
                               t_start, cfg, device, use_amp)

        if history[-1]["val_acc"] > best_val_acc + 1e-4:
            best_val_acc = float(history[-1]["val_acc"])
            best_epoch = epoch + 1
            patience = 0
            torch.save({
                "state_dict": model.state_dict(),
                "config": cfg.to_dict(),
                "ticker_norms": {
                    t: (mu.tolist(), sd.tolist())
                    for t, (mu, sd) in train_ds._ticker_norms.items()
                },
                "feature_cols": list(FEATURE_COLS),
                "best_val_acc": best_val_acc,
                "best_epoch": best_epoch,
                "trained_at_utc": time.time(),
            }, weights_path)
        else:
            patience += 1
            if cfg.enable_early_stopping and patience >= cfg.early_stop_patience:
                logger.info("early stop at epoch %d (no val improvement for %d epochs)",
                            epoch + 1, patience)
                break
            elif not cfg.enable_early_stopping:
                # Diagnostic mode — log that we'd have stopped but are continuing.
                logger.info(
                    "no val improvement for %d epoch(s) [would early-stop at %d, "
                    "but enable_early_stopping=False — continuing]",
                    patience, cfg.early_stop_patience,
                )

    summary = {
        "weights_path": str(weights_path),
        "best_val_acc": round(best_val_acc, 4),
        "best_epoch": best_epoch,
        "n_train": len(train_ds),
        "n_val": len(val_ds),
        "n_tickers": len(train_ds._ticker_features),
        "history": history,
        "device": device,
    }
    summary_path = output_dir / "stock_tft_v1_summary.json"
    summary_path.write_text(json.dumps(summary, indent=2))
    logger.info("[STOCKS_ML_ALPHA] training done: %s", summary)
    return summary


def _validate_into_history(model, val_loader, history, epoch, train_loss, train_n,
                           t_start, cfg, device, use_amp):
    """Run validation pass and append epoch summary to history."""
    model.eval()
    correct = 0
    total = 0
    with torch.no_grad():
        for X, y in val_loader:
            X = X.to(device, non_blocking=True)
            y = y.to(device, non_blocking=True)
            with torch.amp.autocast("cuda", enabled=use_amp):
                logits = model(X)
            pred = logits.argmax(dim=-1)
            correct += int((pred == y).sum().item())
            total += y.size(0)
    val_acc = correct / max(1, total)
    elapsed = time.monotonic() - t_start
    history.append({
        "epoch": epoch + 1,
        "train_loss": round(train_loss / max(1, train_n), 5),
        "val_acc": round(val_acc, 4),
        "elapsed_s": round(elapsed, 1),
    })
    logger.info(
        "epoch %d/%d  loss=%.4f  val_acc=%.3f  (%.1fs)",
        epoch + 1, cfg.epochs, train_loss / max(1, train_n), val_acc, elapsed,
    )


# ── Inference ────────────────────────────────────────────────────────

_inference_cache: dict[str, tuple] = {}


def _load_for_inference(weights_path: Path):
    """Cache loaded model + norms + config in-process."""
    key = str(weights_path)
    if key in _inference_cache:
        return _inference_cache[key]
    payload = torch.load(weights_path, map_location=_device(), weights_only=False)
    cfg_dict = payload["config"]
    cfg = TFTStockConfig(**{k: v for k, v in cfg_dict.items()
                            if k in TFTStockConfig.__dataclass_fields__})
    model = StockTFT(cfg).to(_device())
    model.load_state_dict(payload["state_dict"])
    model.eval()
    norms = payload.get("ticker_norms", {})
    cols = payload.get("feature_cols", list(FEATURE_COLS))
    _inference_cache[key] = (model, norms, payload, cols)
    return _inference_cache[key]


def predict_direction(
    ticker: str,
    feature_window: np.ndarray,
    *,
    weights_path: Optional[Path] = None,
) -> dict:
    """Run the trained TFT on a single (sequence_length × num_features) window."""
    weights_path = weights_path or (
        Path(__file__).resolve().parents[2] / "kb" / "models" / "tft" / "stock_tft_v1.pt"
    )
    if not weights_path.is_file():
        return {"error": "no trained model — run train_tft first",
                "down": None, "flat": None, "up": None, "confidence": 0.0}

    model, norms, payload, _cols = _load_for_inference(weights_path)
    mu, sd = norms.get(ticker, (None, None))
    if mu is None:
        mu = np.zeros(feature_window.shape[1])
        sd = np.ones(feature_window.shape[1])
    else:
        mu = np.asarray(mu, dtype=np.float32)
        sd = np.asarray(sd, dtype=np.float32)

    x = (feature_window.astype(np.float32) - mu) / np.maximum(sd, 1e-6)
    x = np.nan_to_num(x, nan=0.0, posinf=0.0, neginf=0.0)
    t = torch.from_numpy(x).unsqueeze(0).to(_device())
    with torch.no_grad():
        logits = model(t)
        probs = torch.softmax(logits, dim=-1).cpu().numpy().flatten()
    sorted_p = np.sort(probs)[::-1]
    confidence = float(sorted_p[0] - sorted_p[1])
    age = int(time.time() - float(payload.get("trained_at_utc", time.time())))

    logger.info(
        "[STOCKS_ML_ALPHA] %s prediction: down=%.2f flat=%.2f up=%.2f conf=%.2f",
        ticker, probs[0], probs[1], probs[2], confidence,
    )

    return {
        "down": float(probs[0]),
        "flat": float(probs[1]),
        "up": float(probs[2]),
        "confidence": confidence,
        "model_age_s": age,
        "model_val_acc": float(payload.get("best_val_acc") or 0.0),
    }
