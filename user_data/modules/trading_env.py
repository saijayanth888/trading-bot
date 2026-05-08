"""
Custom Gymnasium environment for the DRL ensemble.

Observation (17-dim Box):
   0..2   TFT probabilities                  (down, flat, up)
   3..7   On-chain features                  (netflow_z, mvrv, whale_count_1h,
                                              whale_volume_1h, onchain_pressure)
   8..9   Sentiment                          (score, confidence)
  10..13  Regime one-hot                     (trending_up, trending_down,
                                              mean_reverting, high_volatility)
     14   Portfolio: cash ratio              [0, 1]
     15   Portfolio: position direction      {-1, 0, +1}
     16   Portfolio: unrealized PnL pct      ≈ [-1, +1]

Action (Discrete(5)): 0=strong_buy, 1=buy, 2=hold, 3=sell, 4=strong_sell

Reward = differential Sharpe ratio (Moody/Saffell formulation)
         − transaction cost penalty (10 bps per direction change)
         − drawdown penalty (drawdown_pct * drawdown_lambda)

Episode terminates after `episode_length` steps OR if the equity curve
drops below `bankruptcy_pct` of starting capital.

The env consumes a single pandas DataFrame indexed in chronological order;
required columns are configurable via `column_map`.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Sequence

import gymnasium as gym
import numpy as np
import pandas as pd
from gymnasium import spaces

logger = logging.getLogger(__name__)

# Action discretisation: (direction in {-1, 0, +1}, magnitude in [0, 1])
ACTION_MEANINGS: tuple[tuple[int, float], ...] = (
    ( 1, 1.0),   # 0 strong_buy
    ( 1, 0.5),   # 1 buy
    ( 0, 0.0),   # 2 hold
    (-1, 0.5),   # 3 sell
    (-1, 1.0),   # 4 strong_sell
)

REGIME_LABELS: tuple[str, ...] = (
    "trending_up",
    "trending_down",
    "mean_reverting",
    "high_volatility",
)


@dataclass
class ColumnMap:
    """Names of columns the env reads from the input DataFrame."""

    close: str = "close"
    tft_down: str = "down"
    tft_flat: str = "flat"
    tft_up: str = "up"

    onchain: tuple[str, ...] = (
        "%-onchain_netflow_z",
        "%-onchain_mvrv",
        "%-onchain_whale_count_1h",
        "%-onchain_whale_volume_1h",
    )

    sentiment_score: str = "%-sentiment_score"
    sentiment_confidence: str = "%-sentiment_confidence"

    regime_label: str = "regime_label"
    regime_one_hot_prefix: str = "%-regime_is_"

    @property
    def required(self) -> tuple[str, ...]:
        return (self.close,)


@dataclass
class EnvConfig:
    """Trading-env hyperparameters."""

    episode_length: int = 1000
    initial_capital: float = 10_000.0
    transaction_cost_bps: float = 10.0       # 0.10% per direction change
    drawdown_lambda: float = 1.0             # weight on drawdown penalty
    sharpe_eta: float = 0.01                 # diff-Sharpe smoothing
    sharpe_warmup_steps: int = 20            # skip diff-Sharpe until EMAs settle
    reward_clip: float = 5.0                 # clip reward to [-clip, +clip]
    bankruptcy_pct: float = 0.5              # terminate if equity < 50% capital
    obs_clip: float = 5.0                    # clip standardised features
    seed: int | None = None
    columns: ColumnMap = field(default_factory=ColumnMap)


class TradingEnv(gym.Env):
    """
    Single-asset, single-position trading environment.

    The agent's position is internally a continuous fraction in [-1, 1],
    set to (direction * magnitude) on each step. The environment is *not*
    leveraged — magnitude=1 means 100% of remaining capital.
    """

    metadata = {"render_modes": []}

    OBS_DIM = 17

    def __init__(self, df: pd.DataFrame, config: EnvConfig | None = None):
        super().__init__()
        self.cfg = config or EnvConfig()
        self.df = df.reset_index(drop=True)
        self._validate_dataframe()

        self.action_space = spaces.Discrete(len(ACTION_MEANINGS))
        self.observation_space = spaces.Box(
            low=-self.cfg.obs_clip,
            high=self.cfg.obs_clip,
            shape=(self.OBS_DIM,),
            dtype=np.float32,
        )

        # Pre-computed return series (next-bar pct change)
        close = self.df[self.cfg.columns.close].astype(np.float32).to_numpy()
        self._returns = np.zeros_like(close)
        self._returns[:-1] = (close[1:] - close[:-1]) / np.maximum(close[:-1], 1e-9)
        self._n_rows = len(self.df)

        # Pre-extract feature blocks once (fast obs build)
        self._tft = self._extract_tft_block()
        self._onchain = self._extract_onchain_block()
        self._sentiment = self._extract_sentiment_block()
        self._regime = self._extract_regime_block()

        self._rng = np.random.default_rng(self.cfg.seed)

        # Episode state — initialised in reset()
        self._t: int = 0
        self._t_start: int = 0
        self._t_end: int = 0
        self._steps_taken: int = 0
        self._capital: float = self.cfg.initial_capital
        self._equity: float = self.cfg.initial_capital
        self._peak_equity: float = self.cfg.initial_capital
        self._position: float = 0.0
        self._entry_price: float = 0.0
        self._sharpe_a: float = 0.0          # 1st moment, EMA (in return units)
        self._sharpe_b: float = 0.0          # 2nd moment, EMA (in return^2 units)

    # ------------------------------------------------------------------
    # Gym API
    # ------------------------------------------------------------------

    def reset(
        self, *, seed: int | None = None, options: dict | None = None,
    ) -> tuple[np.ndarray, dict]:
        super().reset(seed=seed)
        if seed is not None:
            self._rng = np.random.default_rng(seed)

        # Pick a random episode start that leaves room for `episode_length` steps.
        max_start = max(1, self._n_rows - self.cfg.episode_length - 1)
        self._t_start = int(self._rng.integers(0, max_start))
        self._t_end = self._t_start + self.cfg.episode_length
        self._t = self._t_start

        self._capital = self.cfg.initial_capital
        self._equity = self.cfg.initial_capital
        self._peak_equity = self.cfg.initial_capital
        self._position = 0.0
        self._entry_price = 0.0
        self._sharpe_a = 0.0
        self._sharpe_b = 0.0
        self._steps_taken = 0

        return self._observation(), {"t_start": self._t_start}

    def step(
        self, action: int,
    ) -> tuple[np.ndarray, float, bool, bool, dict]:
        if not self.action_space.contains(int(action)):
            raise ValueError(f"invalid action {action}")

        direction, magnitude = ACTION_MEANINGS[int(action)]
        target_position = float(direction) * float(magnitude)

        # Direction-change cost (no cost for resizing in same direction)
        prev_dir = np.sign(self._position)
        new_dir = np.sign(target_position)
        cost = 0.0
        if prev_dir != new_dir:
            # Cost is on the absolute change in exposure, in bps of equity
            change = abs(target_position - self._position)
            cost = self._equity * change * (self.cfg.transaction_cost_bps / 10_000.0)

        # Apply cost to equity
        self._equity -= cost

        # Realize PnL on the previous position based on next-bar return
        bar_return = float(self._returns[self._t])
        pnl_dollars = self._equity * self._position * bar_return
        self._equity += pnl_dollars

        # Track peak + drawdown
        if self._equity > self._peak_equity:
            self._peak_equity = self._equity
        drawdown_pct = max(0.0, 1.0 - self._equity / max(self._peak_equity, 1e-9))

        # Differential Sharpe (Moody/Saffell), in unit-return space. Using
        # pnl/initial_capital keeps EMAs in O(1e-3) range so the Sharpe
        # ratio doesn't explode early in an episode.
        pnl_unit = pnl_dollars / max(self.cfg.initial_capital, 1.0)
        eta = self.cfg.sharpe_eta

        # Skip diff-Sharpe until the EMAs have absorbed a few samples; pay
        # a tiny return-shaped reward in the warmup so gradients aren't zero.
        self._steps_taken += 1
        if self._steps_taken <= self.cfg.sharpe_warmup_steps:
            d_sharpe = pnl_unit
        else:
            var = max(self._sharpe_b - self._sharpe_a ** 2, 1e-8)
            denom = var ** 1.5
            delta_a = pnl_unit - self._sharpe_a
            delta_b = pnl_unit * pnl_unit - self._sharpe_b
            d_sharpe = (
                (self._sharpe_b * delta_a - 0.5 * self._sharpe_a * delta_b) / denom
            )

        # Update EMAs after computing reward (paper-faithful order).
        self._sharpe_a = (1 - eta) * self._sharpe_a + eta * pnl_unit
        self._sharpe_b = (1 - eta) * self._sharpe_b + eta * pnl_unit * pnl_unit

        # Reward = diff-Sharpe − cost (in unit-return space) − drawdown penalty
        cost_unit = cost / max(self.cfg.initial_capital, 1.0)
        reward = float(
            d_sharpe
            - cost_unit
            - self.cfg.drawdown_lambda * drawdown_pct * drawdown_pct
        )
        # Bound the reward so a single freak bar can't dominate gradients.
        reward = float(np.clip(reward, -self.cfg.reward_clip, self.cfg.reward_clip))

        # Commit new position AFTER computing PnL on the previous one.
        self._position = target_position
        if target_position != 0.0:
            self._entry_price = float(self.df[self.cfg.columns.close].iloc[self._t])

        self._t += 1
        terminated = (
            self._equity < self.cfg.bankruptcy_pct * self.cfg.initial_capital
        )
        truncated = self._t >= self._t_end or self._t >= self._n_rows - 1

        info = {
            "equity": self._equity,
            "drawdown_pct": drawdown_pct,
            "position": self._position,
            "cost": cost,
            "bar_return": bar_return,
            "pnl_unit": pnl_unit,
            "diff_sharpe": float(d_sharpe),
        }

        return self._observation(), reward, terminated, truncated, info

    # ------------------------------------------------------------------
    # Observation
    # ------------------------------------------------------------------

    def _observation(self) -> np.ndarray:
        t = self._t
        clip = self.cfg.obs_clip
        obs = np.empty(self.OBS_DIM, dtype=np.float32)

        # 0..2 TFT (already in [0, 1] from softmax)
        obs[0:3] = self._tft[t]

        # 3..7 onchain — already z-scored where applicable; clip the rest
        obs[3:8] = np.clip(self._onchain[t], -clip, clip)

        # 8..9 sentiment
        obs[8:10] = np.clip(self._sentiment[t], -clip, clip)

        # 10..13 regime one-hot
        obs[10:14] = self._regime[t]

        # 14 cash ratio (1 - |position|), 15 dir, 16 unrealized PnL pct
        cash_ratio = float(1.0 - abs(self._position))
        if self._position != 0.0 and self._entry_price > 0.0:
            curr_price = float(self.df[self.cfg.columns.close].iloc[t])
            unrealized = (curr_price - self._entry_price) / self._entry_price
            unrealized = unrealized * np.sign(self._position)
        else:
            unrealized = 0.0

        obs[14] = float(np.clip(cash_ratio, 0.0, 1.0))
        obs[15] = float(np.sign(self._position))
        obs[16] = float(np.clip(unrealized, -1.0, 1.0))
        return obs

    # ------------------------------------------------------------------
    # Feature extraction helpers
    # ------------------------------------------------------------------

    def _validate_dataframe(self) -> None:
        for col in self.cfg.columns.required:
            if col not in self.df.columns:
                raise ValueError(f"DataFrame missing required column '{col}'")
        if len(self.df) < self.cfg.episode_length + 2:
            raise ValueError(
                f"DataFrame too short: have {len(self.df)} rows, "
                f"need ≥ {self.cfg.episode_length + 2}"
            )

    def _column_or_zeros(self, col: str) -> np.ndarray:
        if col in self.df.columns:
            return self.df[col].astype(np.float32).fillna(0.0).to_numpy()
        return np.zeros(self._n_rows, dtype=np.float32)

    def _extract_tft_block(self) -> np.ndarray:
        cols = self.cfg.columns
        out = np.zeros((self._n_rows, 3), dtype=np.float32)
        out[:, 0] = self._column_or_zeros(cols.tft_down)
        out[:, 1] = self._column_or_zeros(cols.tft_flat)
        out[:, 2] = self._column_or_zeros(cols.tft_up)
        # If flat is missing, redistribute mass so the sum-to-1 invariant
        # roughly holds — keeps observations interpretable.
        rowsum = out.sum(axis=1, keepdims=True)
        rowsum[rowsum < 1e-6] = 1.0
        return out / rowsum

    def _extract_onchain_block(self) -> np.ndarray:
        """
        5 onchain features. The first 4 come from the strategy's standard
        onchain pipeline; the 5th ("onchain_pressure") is a derived
        netflow_z * (mvrv - 1) — directional pressure scaled by valuation.
        """
        names = self.cfg.columns.onchain
        if len(names) < 4:
            raise ValueError("ColumnMap.onchain must list at least 4 columns")

        out = np.zeros((self._n_rows, 5), dtype=np.float32)
        for i, name in enumerate(names[:4]):
            out[:, i] = self._column_or_zeros(name)
        # mvrv neutral baseline = 1.0 in the strategy
        netflow = out[:, 0]
        mvrv = out[:, 1]
        out[:, 4] = netflow * (mvrv - 1.0)
        return out

    def _extract_sentiment_block(self) -> np.ndarray:
        cols = self.cfg.columns
        out = np.zeros((self._n_rows, 2), dtype=np.float32)
        out[:, 0] = self._column_or_zeros(cols.sentiment_score)
        out[:, 1] = self._column_or_zeros(cols.sentiment_confidence)
        return out

    def _extract_regime_block(self) -> np.ndarray:
        out = np.zeros((self._n_rows, 4), dtype=np.float32)
        prefix = self.cfg.columns.regime_one_hot_prefix
        any_one_hot = False
        for i, label in enumerate(REGIME_LABELS):
            col = f"{prefix}{label}"
            if col in self.df.columns:
                out[:, i] = self._column_or_zeros(col)
                any_one_hot = True

        if not any_one_hot and self.cfg.columns.regime_label in self.df.columns:
            labels = self.df[self.cfg.columns.regime_label].astype(str).to_numpy()
            for i, label in enumerate(REGIME_LABELS):
                out[:, i] = (labels == label).astype(np.float32)
        return out


def make_env(df: pd.DataFrame, **cfg_overrides) -> TradingEnv:
    """Convenience factory: TradingEnv with a fresh EnvConfig."""
    cfg = EnvConfig(**cfg_overrides)
    return TradingEnv(df=df, config=cfg)
