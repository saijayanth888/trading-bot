"""
Walk-forward Dataset for stock TFT training.

Critical correctness property: **no look-ahead leakage**.

  - Sequences end at date T; the target is the 5-day-forward return
    measured from T. Anything inside the sequence uses ONLY values
    available at or before T.
  - Train / val / test splits are TEMPORAL: train ≤ T_train_end,
    val (T_train_end, T_val_end], test (T_val_end, ∞). Random splits
    would cause silent data leakage because consecutive rows share a
    sliding window.
  - Cross-sectional features (SPY excess return) are computed using
    only that day's SPY data — no peeking at tomorrow.

Per-ticker normalization happens at sample time, NOT precomputed across
the whole dataset (which would leak future scaling factors back into
the train window).

Output sample shape (for one (ticker, anchor_date) pair):
  X:  (sequence_length, num_features)
  y:  scalar in {0, 1, 2}
  meta: dict with ticker + anchor_date for debugging
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd
import torch
from torch.utils.data import Dataset

from .features_stock import (
    FEATURE_COLS,
    attach_spy_excess,
    build_features,
    build_target,
)

logger = logging.getLogger(__name__)


@dataclass
class SplitDates:
    """Walk-forward split boundaries. All dates inclusive on the lower
    side except for the train_end which is exclusive on the val side."""
    train_end: pd.Timestamp
    val_end: pd.Timestamp


def default_splits(latest_date: pd.Timestamp) -> SplitDates:
    """Default 80/10/10 by time. With ~504 daily bars: train 400, val 50, test 54."""
    train_end = latest_date - pd.Timedelta(days=104)
    val_end = latest_date - pd.Timedelta(days=54)
    return SplitDates(train_end=train_end, val_end=val_end)


def _load_bars_json(path: Path) -> pd.DataFrame:
    """Load one historical_bars/{TICKER}.json into a tidy DataFrame."""
    raw = json.loads(path.read_text())
    bars = raw.get("bars") or []
    if not bars:
        return pd.DataFrame()
    df = pd.DataFrame(bars)
    df.rename(columns={"o": "o", "h": "h", "l": "l", "c": "c", "v": "v"}, inplace=True)
    df["date"] = pd.to_datetime(df["date"], utc=True)
    return df


class StockDataset(Dataset):
    """Walk-forward sliding-window Dataset.

    One sample = (sequence_length-day window of features for one ticker,
    direction label for the 5-day-forward return after the window).

    Train mode includes ALL valid samples within the train date range.
    Val/test only include samples whose anchor date is in their window.
    """

    def __init__(
        self,
        kb_dir: Path,
        *,
        tickers: list[str] | None = None,
        sequence_length: int = 60,
        target_horizon_days: int = 5,
        up_threshold: float = 0.015,
        down_threshold: float = -0.015,
        split: str = "train",  # "train" | "val" | "test" | "all"
        splits: SplitDates | None = None,
        max_samples: int | None = None,
        normalize_per_ticker: bool = True,
    ):
        self.kb_dir = Path(kb_dir)
        self.sequence_length = sequence_length
        self.target_horizon = target_horizon_days
        self.up_thr = up_threshold
        self.down_thr = down_threshold
        self.split = split
        self.normalize_per_ticker = normalize_per_ticker

        # 1) Resolve ticker list
        if tickers:
            paths = [self.kb_dir / f"{t}.json" for t in tickers]
        else:
            paths = sorted(self.kb_dir.glob("*.json"))
        paths = [p for p in paths if p.is_file() and p.stem != "SPY"]

        # 2) Build SPY 5-day-return series for cross-sectional feature
        spy_path = self.kb_dir / "SPY.json"
        spy_5d_return: pd.Series = pd.Series(dtype=float)
        if spy_path.is_file():
            spy_bars = _load_bars_json(spy_path)
            if not spy_bars.empty:
                spy_close = spy_bars.sort_values("date").set_index(
                    pd.to_datetime(spy_bars["date"], utc=True),
                )["c"]
                spy_5d_return = spy_close.pct_change(5)

        # 3) Resolve splits
        if splits is None:
            # Use the latest date across all tickers for the split
            all_max = pd.Timestamp("2000-01-01", tz="UTC")
            for p in paths[:10]:  # sample a few for the latest date
                bars = _load_bars_json(p)
                if not bars.empty:
                    all_max = max(all_max, bars["date"].max())
            splits = default_splits(all_max)
        self.splits = splits

        # 4) Build (ticker, anchor_date) index — pure metadata, no tensors yet
        self._index: list[tuple[str, pd.Timestamp]] = []
        self._ticker_features: dict[str, pd.DataFrame] = {}
        self._ticker_targets: dict[str, pd.Series] = {}
        self._ticker_norms: dict[str, tuple[np.ndarray, np.ndarray]] = {}

        skipped = 0
        for path in paths:
            ticker = path.stem
            bars = _load_bars_json(path)
            if bars.empty or len(bars) < sequence_length + target_horizon_days + 30:
                skipped += 1
                continue

            try:
                feats = build_features(bars)
                if not spy_5d_return.empty:
                    feats = attach_spy_excess(feats, spy_5d_return)
                target = build_target(
                    bars,
                    horizon_days=target_horizon_days,
                    up_threshold=up_threshold,
                    down_threshold=down_threshold,
                )
            except Exception as exc:  # pragma: no cover
                logger.warning("feature build failed for %s: %s", ticker, exc)
                skipped += 1
                continue

            # Align feats and target on the date axis
            common = feats.index.intersection(target.index)
            if len(common) < sequence_length + target_horizon_days:
                skipped += 1
                continue

            feats = feats.loc[common, list(FEATURE_COLS)]
            target = target.loc[common]

            # Drop rows where target is NaN (last `target_horizon` rows)
            mask = target.notna()
            feats = feats[mask]
            target = target[mask]

            # Per-ticker normalization stats — computed ON TRAIN PORTION ONLY
            train_mask = feats.index <= splits.train_end
            if normalize_per_ticker and train_mask.any():
                mu = feats[train_mask].mean(axis=0).values
                sd = feats[train_mask].std(axis=0).replace(0, 1.0).values
            else:
                mu = np.zeros(feats.shape[1])
                sd = np.ones(feats.shape[1])
            self._ticker_norms[ticker] = (mu, sd)
            self._ticker_features[ticker] = feats
            self._ticker_targets[ticker] = target

            # 5) Enumerate valid anchor dates per split
            valid_anchors = feats.index[sequence_length - 1:]  # need full window
            for anchor in valid_anchors:
                if split == "train" and anchor > splits.train_end:
                    continue
                if split == "val" and not (splits.train_end < anchor <= splits.val_end):
                    continue
                if split == "test" and anchor <= splits.val_end:
                    continue
                self._index.append((ticker, anchor))

        if max_samples is not None and len(self._index) > max_samples:
            # Stratified subsample — random but fixed seed for reproducibility
            rng = np.random.default_rng(42)
            sel = rng.choice(len(self._index), size=max_samples, replace=False)
            self._index = [self._index[i] for i in sorted(sel)]

        logger.info(
            "StockDataset[%s]: %d samples across %d tickers (skipped %d). "
            "Train end=%s, val end=%s.",
            split, len(self._index), len(self._ticker_features), skipped,
            self.splits.train_end.date(), self.splits.val_end.date(),
        )

    def __len__(self) -> int:
        return len(self._index)

    def __getitem__(self, idx: int) -> tuple[torch.Tensor, torch.Tensor, dict]:
        ticker, anchor = self._index[idx]
        feats = self._ticker_features[ticker]
        target = self._ticker_targets[ticker]

        # Slice the sequence ending at anchor (inclusive)
        end_pos = feats.index.get_loc(anchor)
        if isinstance(end_pos, slice):  # duplicate-date guard
            end_pos = end_pos.start
        start_pos = end_pos - self.sequence_length + 1
        seq = feats.iloc[start_pos: end_pos + 1].values.astype(np.float32)

        # Normalize using train-only stats
        mu, sd = self._ticker_norms[ticker]
        mu = mu.astype(np.float32)
        sd = sd.astype(np.float32)
        seq = (seq - mu) / np.maximum(sd, 1e-6)
        seq = np.nan_to_num(seq, nan=0.0, posinf=0.0, neginf=0.0).astype(np.float32)

        label = int(target.loc[anchor])

        return (
            torch.from_numpy(seq),
            torch.tensor(label, dtype=torch.long),
            {"ticker": ticker, "anchor": str(anchor.date())},
        )
