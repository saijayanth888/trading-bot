"""
Deep RL ensemble: PPO + A2C + DQN trained on the trading environment.

Each agent trains independently (different hyperparameters / exploration
profiles) and the resulting models are combined by `ensemble_voter.vote`.

Public surface:

    ensemble = DRLEnsemble(save_dir=Path("user_data/models/drl"))
    ensemble.train(df_train, total_timesteps=200_000)
    ensemble.load()
    actions = ensemble.predict(observation)        # {"ppo": int, "a2c": int, "dqn": int}
    if ensemble.should_retrain():
        ensemble.train(df_train, total_timesteps=200_000)

Models are saved per-agent under `<save_dir>/{ppo,a2c,dqn}.zip` plus a
`meta.json` with the last-train timestamp (UTC ISO-8601) used by the
weekly retrain check.

Schedule contract: `should_retrain()` returns True when more than
`retrain_days` (default 7) have elapsed since `meta.json["last_train"]`,
or when the file is missing. Cron the trainer for Sunday 00:00 UTC, e.g.

    0 0 * * 0 docker compose exec freqtrade python -m user_data.modules.drl_ensemble retrain
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

import numpy as np
import pandas as pd

from stable_baselines3 import A2C, DQN, PPO
from stable_baselines3.common.base_class import BaseAlgorithm
from stable_baselines3.common.monitor import Monitor
from stable_baselines3.common.vec_env import DummyVecEnv

from .trading_env import EnvConfig, TradingEnv

logger = logging.getLogger(__name__)

DEFAULT_SAVE_DIR = Path("user_data/models/drl")
META_FILENAME = "meta.json"

AGENT_NAMES: tuple[str, ...] = ("ppo", "a2c", "dqn")


@dataclass
class AgentHyperparams:
    """Per-algorithm SB3 kwargs. Defaults are sensible starting points."""

    ppo: dict[str, Any] = field(default_factory=lambda: {
        "learning_rate": 3e-4,
        "n_steps": 2048,
        "batch_size": 64,
        "n_epochs": 10,
        "gamma": 0.99,
        "gae_lambda": 0.95,
        "clip_range": 0.2,
        "ent_coef": 0.01,
        "vf_coef": 0.5,
        "max_grad_norm": 0.5,
        "policy": "MlpPolicy",
    })
    a2c: dict[str, Any] = field(default_factory=lambda: {
        "learning_rate": 7e-4,
        "n_steps": 5,
        "gamma": 0.99,
        "gae_lambda": 1.0,
        "ent_coef": 0.01,
        "vf_coef": 0.5,
        "max_grad_norm": 0.5,
        "policy": "MlpPolicy",
    })
    dqn: dict[str, Any] = field(default_factory=lambda: {
        "learning_rate": 1e-4,
        "buffer_size": 100_000,
        "learning_starts": 1_000,
        "batch_size": 64,
        "tau": 1.0,
        "gamma": 0.99,
        "train_freq": 4,
        "gradient_steps": 1,
        "target_update_interval": 1_000,
        "exploration_fraction": 0.2,
        "exploration_final_eps": 0.05,
        "policy": "MlpPolicy",
    })


def _algo_class(name: str) -> type[BaseAlgorithm]:
    return {"ppo": PPO, "a2c": A2C, "dqn": DQN}[name]


def _make_vec_env(df: pd.DataFrame, env_cfg: EnvConfig | None, seed: int) -> DummyVecEnv:
    def _factory() -> Monitor:
        env = TradingEnv(df=df, config=env_cfg or EnvConfig())
        env.reset(seed=seed)
        return Monitor(env)
    return DummyVecEnv([_factory])


class DRLEnsemble:
    """PPO + A2C + DQN ensemble persisted on disk."""

    def __init__(
        self,
        save_dir: Path | str = DEFAULT_SAVE_DIR,
        hyperparams: AgentHyperparams | None = None,
        device: str = "auto",
        retrain_days: int = 7,
        verbose: int = 0,
    ):
        self.save_dir = Path(save_dir)
        self.hyperparams = hyperparams or AgentHyperparams()
        self.device = device
        self.retrain_days = retrain_days
        self.verbose = verbose
        self.agents: dict[str, BaseAlgorithm] = {}

    # ------------------------------------------------------------------
    # Train / save / load
    # ------------------------------------------------------------------

    def train(
        self,
        df: pd.DataFrame,
        total_timesteps: int = 200_000,
        env_config: EnvConfig | None = None,
        seed: int = 0,
        agents: Iterable[str] = AGENT_NAMES,
    ) -> dict[str, BaseAlgorithm]:
        """
        Train each named agent for `total_timesteps` on the given data.

        Each agent gets a fresh DummyVecEnv (seeded reproducibly per agent)
        so they don't share replay state.
        """
        self.save_dir.mkdir(parents=True, exist_ok=True)
        for i, name in enumerate(agents):
            if name not in AGENT_NAMES:
                raise ValueError(f"unknown agent '{name}'")
            cls = _algo_class(name)
            kwargs = dict(getattr(self.hyperparams, name))
            policy = kwargs.pop("policy", "MlpPolicy")

            env = _make_vec_env(df, env_config, seed=seed + i * 1000)
            agent = cls(
                policy=policy,
                env=env,
                device=self.device,
                seed=seed + i * 1000,
                verbose=self.verbose,
                **kwargs,
            )
            logger.info(
                "[drl] training %s (steps=%d, device=%s)",
                name, total_timesteps, self.device,
            )
            agent.learn(total_timesteps=total_timesteps, progress_bar=False)
            agent.save(str(self.save_dir / f"{name}.zip"))
            self.agents[name] = agent

        self._write_meta(total_timesteps=total_timesteps, n_rows=len(df))
        return self.agents

    def load(self, agents: Iterable[str] = AGENT_NAMES) -> dict[str, BaseAlgorithm]:
        """Load saved agents from `save_dir`."""
        loaded: dict[str, BaseAlgorithm] = {}
        for name in agents:
            cls = _algo_class(name)
            path = self.save_dir / f"{name}.zip"
            if not path.exists():
                raise FileNotFoundError(f"missing {name} weights at {path}")
            loaded[name] = cls.load(str(path), device=self.device)
        self.agents = loaded
        return loaded

    # ------------------------------------------------------------------
    # Inference
    # ------------------------------------------------------------------

    def predict(
        self, observation: np.ndarray, deterministic: bool = True,
    ) -> dict[str, int]:
        """
        Predict an action per agent for a single observation OR a batch.

        Returns a dict {name: int_action} for a single obs, or
        {name: ndarray[int]} for a batch (shape inferred from input).
        """
        if not self.agents:
            self.load()
        is_batch = observation.ndim == 2
        out: dict[str, Any] = {}
        for name, agent in self.agents.items():
            action, _ = agent.predict(observation, deterministic=deterministic)
            if is_batch:
                out[name] = np.asarray(action, dtype=np.int64).reshape(-1)
            else:
                out[name] = int(np.asarray(action).reshape(-1)[0])
        return out

    # ------------------------------------------------------------------
    # Retrain scheduling
    # ------------------------------------------------------------------

    def should_retrain(self, now: datetime | None = None) -> bool:
        meta = self._read_meta()
        if meta is None:
            return True
        try:
            last = datetime.fromisoformat(meta["last_train"])
        except Exception:
            return True
        if last.tzinfo is None:
            last = last.replace(tzinfo=timezone.utc)
        now = now or datetime.now(timezone.utc)
        return (now - last) >= timedelta(days=self.retrain_days)

    def last_train_time(self) -> datetime | None:
        meta = self._read_meta()
        if meta is None or "last_train" not in meta:
            return None
        try:
            return datetime.fromisoformat(meta["last_train"])
        except Exception:
            return None

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _meta_path(self) -> Path:
        return self.save_dir / META_FILENAME

    def _read_meta(self) -> dict | None:
        path = self._meta_path()
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text())
        except Exception:
            return None

    def _write_meta(self, **extra) -> None:
        meta = {
            "last_train": datetime.now(timezone.utc).isoformat(),
            "agents": list(self.agents.keys()),
            **extra,
        }
        self._meta_path().write_text(json.dumps(meta, indent=2))


# Module-level CLI hook for cron: `python -m user_data.modules.drl_ensemble retrain`
def _cli_retrain() -> int:
    """Cron entry point. Loads training data via a hook the user must implement."""
    raise SystemExit(
        "drl_ensemble: this module is a library; provide your own training "
        "script that builds a DataFrame with TFT/onchain/sentiment/regime "
        "columns and calls `DRLEnsemble.train(df)`. See tests/test_drl.py."
    )


if __name__ == "__main__":
    import sys
    if len(sys.argv) == 2 and sys.argv[1] == "retrain":
        sys.exit(_cli_retrain())
    sys.exit(0)
