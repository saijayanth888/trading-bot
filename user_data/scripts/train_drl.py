"""
Train (or weekly-retrain) the DRL ensemble.

Designed to be cron-friendly. Reads a pre-prepared training DataFrame
that already contains TFT, on-chain, sentiment and regime columns
(parquet or CSV), then trains PPO + A2C + DQN and persists them to
`user_data/models/drl/`.

Cron the script for Sunday 00:00 UTC, e.g.

    0 0 * * 0 docker compose exec -T freqtrade python /freqtrade/user_data/scripts/train_drl.py \\
        --data /freqtrade/user_data/data/drl_train.parquet --timesteps 200000

The expected training DataFrame is whatever the strategy emits during
backtest: TFT classifier columns (`down`, `flat`, `up`, `tft_confidence`),
on-chain (`%-onchain_*`), sentiment (`%-sentiment_*`), regime
(`regime_label`, `%-regime_is_*`), and `close`.

For a smoke run with synthetic data, pass `--synthetic`.
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "user_data"))

from modules.drl_ensemble import DEFAULT_SAVE_DIR, DRLEnsemble  # noqa: E402
from modules.trading_env import EnvConfig, REGIME_LABELS         # noqa: E402

logger = logging.getLogger("train_drl")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")


def _load_dataframe(path: Path) -> pd.DataFrame:
    if path.suffix == ".parquet":
        return pd.read_parquet(path)
    if path.suffix in (".csv", ".tsv"):
        sep = "\t" if path.suffix == ".tsv" else ","
        return pd.read_csv(path, sep=sep)
    raise ValueError(f"unsupported extension: {path.suffix}")


def _make_synthetic(n: int = 30_000) -> pd.DataFrame:
    """Smoke data — same shape as the real strategy output."""
    rng = np.random.default_rng(0)
    latent = np.cumsum(rng.normal(0, 1, size=n) * 0.05)
    latent = (latent - latent.min()) / (np.ptp(latent) + 1e-9)
    p_up = np.clip(0.3 + 0.4 * latent + rng.normal(0, 0.05, n), 0.05, 0.95)
    p_down = np.clip(0.3 + 0.4 * (1 - latent) + rng.normal(0, 0.05, n), 0.05, 0.95)
    p_flat = np.clip(1 - p_up - p_down, 0.01, 0.5)
    s = p_up + p_flat + p_down
    p_up, p_flat, p_down = p_up / s, p_flat / s, p_down / s
    drift = (p_up - p_down) * 0.001
    log_returns = drift + rng.normal(0, 0.003, size=n)
    close = 100.0 * np.exp(np.cumsum(log_returns))

    regime_idx = rng.integers(0, 4, size=n)
    df = pd.DataFrame({
        "close": close.astype(np.float32),
        "down": p_down.astype(np.float32),
        "flat": p_flat.astype(np.float32),
        "up": p_up.astype(np.float32),
        "tft_confidence": np.clip(rng.beta(2, 2, n), 0, 1).astype(np.float32),
        "%-onchain_netflow_z": rng.normal(0, 1, n).astype(np.float32),
        "%-onchain_mvrv": (1.0 + rng.normal(0, 0.3, n)).astype(np.float32),
        "%-onchain_whale_count_1h": rng.poisson(2, n).astype(np.float32),
        "%-onchain_whale_volume_1h": (rng.gamma(2, 1e6, n) / 1e7).astype(np.float32),
        "%-sentiment_score": np.tanh(latent * 4 - 2 + rng.normal(0, 0.2, n)).astype(np.float32),
        "%-sentiment_confidence": np.clip(rng.beta(2, 2, n), 0, 1).astype(np.float32),
        "regime_label": np.array(REGIME_LABELS)[regime_idx],
    })
    for i, label in enumerate(REGIME_LABELS):
        df[f"%-regime_is_{label}"] = (regime_idx == i).astype(np.float32)
    return df


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--data", type=Path, default=None,
                   help="Path to training DataFrame (parquet or csv).")
    p.add_argument("--synthetic", action="store_true",
                   help="Use synthetic data — for smoke runs only.")
    p.add_argument("--timesteps", type=int, default=200_000,
                   help="Training timesteps per agent (default 200k).")
    p.add_argument("--save-dir", type=Path, default=Path(DEFAULT_SAVE_DIR),
                   help="Where to save trained agents (default user_data/models/drl).")
    p.add_argument("--episode-length", type=int, default=1000)
    p.add_argument("--device", type=str, default="auto",
                   help="cpu / cuda / auto (default).")
    p.add_argument("--seed", type=int, default=0)
    args = p.parse_args()

    if args.synthetic:
        logger.info("loading synthetic dataset")
        df = _make_synthetic(n=30_000)
    elif args.data is not None:
        logger.info("loading dataset from %s", args.data)
        df = _load_dataframe(args.data)
    else:
        p.error("--data PATH or --synthetic is required")

    logger.info("dataset rows=%d cols=%d", len(df), len(df.columns))

    ensemble = DRLEnsemble(save_dir=args.save_dir, device=args.device, verbose=0)
    env_cfg = EnvConfig(episode_length=args.episode_length, seed=args.seed)
    ensemble.train(
        df=df,
        total_timesteps=args.timesteps,
        env_config=env_cfg,
        seed=args.seed,
    )

    logger.info(
        "trained %d agents, last_train=%s, save_dir=%s",
        len(ensemble.agents),
        ensemble.last_train_time(),
        args.save_dir,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
