"""
Smoke test for the DRL ensemble + voter + meta-agent.

Builds a small synthetic dataset that mirrors the columns the strategy
emits (TFT probs, on-chain, sentiment, regime one-hot), trains each
agent for a small number of steps to confirm the pipeline learns at all,
runs voting + meta-agent, and round-trips save/load.

    python tests/test_drl.py
"""

from __future__ import annotations

import shutil
import sys
import tempfile
import time
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "user_data"))

from modules.drl_ensemble import DRLEnsemble    # noqa: E402
from modules.ensemble_voter import (             # noqa: E402
    HOLD_ACTION,
    vote,
    vote_batch,
)
from modules.meta_agent import compute_signal    # noqa: E402
from modules.trading_env import (                # noqa: E402
    EnvConfig,
    TradingEnv,
    REGIME_LABELS,
)


def _ok(msg: str) -> None: print(f"  [✓] {msg}")
def _info(msg: str) -> None: print(f"  [i] {msg}")
def _warn(msg: str) -> None: print(f"  [!] {msg}")
def _hr() -> None: print("=" * 64)


def _make_synthetic_df(n: int = 1500, seed: int = 7) -> pd.DataFrame:
    """
    Create a DataFrame with all columns the env reads. The price series
    has a weak persistent trend tied to the TFT 'up' probability so the
    agents have something to learn.
    """
    rng = np.random.default_rng(seed)

    # Latent signal: smoothed Brownian motion, used to drive both price
    # direction and the synthetic TFT 'up' probability.
    latent = np.cumsum(rng.normal(0, 1, size=n) * 0.05)
    latent = (latent - latent.min()) / (np.ptp(latent) + 1e-9)

    p_up = 0.3 + 0.4 * latent + rng.normal(0, 0.05, size=n)
    p_up = np.clip(p_up, 0.05, 0.95)
    p_down_raw = 0.3 + 0.4 * (1 - latent) + rng.normal(0, 0.05, size=n)
    p_down_raw = np.clip(p_down_raw, 0.05, 0.95)
    p_flat = np.clip(1 - p_up - p_down_raw, 0.01, 0.5)
    s = p_up + p_down_raw + p_flat
    p_up, p_flat, p_down = p_up / s, p_flat / s, p_down_raw / s

    # Price: cumulative returns nudged by (p_up - p_down)
    drift = (p_up - p_down) * 0.001
    noise = rng.normal(0, 0.003, size=n)
    log_returns = drift + noise
    close = 100.0 * np.exp(np.cumsum(log_returns))

    # Onchain proxies
    netflow_z = rng.normal(0, 1, size=n).astype(np.float32)
    mvrv = 1.0 + rng.normal(0, 0.3, size=n).astype(np.float32)
    whale_count = np.maximum(0, rng.poisson(2, size=n)).astype(np.float32)
    whale_volume = np.maximum(0, rng.gamma(2, 1e6, size=n)).astype(np.float32) / 1e7

    # Sentiment
    sent_score = np.tanh(latent * 4 - 2 + rng.normal(0, 0.2, size=n)).astype(np.float32)
    sent_conf = np.clip(rng.beta(2, 2, size=n), 0.0, 1.0).astype(np.float32)

    # Regime one-hot (random — not the focus of this test)
    regime_idx = rng.integers(0, 4, size=n)
    regime_one_hot = np.zeros((n, 4), dtype=np.float32)
    regime_one_hot[np.arange(n), regime_idx] = 1.0

    df = pd.DataFrame({
        "close": close.astype(np.float32),
        "down": p_down.astype(np.float32),
        "flat": p_flat.astype(np.float32),
        "up": p_up.astype(np.float32),
        "%-onchain_netflow_z": netflow_z,
        "%-onchain_mvrv": mvrv,
        "%-onchain_whale_count_1h": whale_count,
        "%-onchain_whale_volume_1h": whale_volume,
        "%-sentiment_score": sent_score,
        "%-sentiment_confidence": sent_conf,
        "regime_label": np.array(REGIME_LABELS)[regime_idx],
    })
    for i, label in enumerate(REGIME_LABELS):
        df[f"%-regime_is_{label}"] = regime_one_hot[:, i]
    return df


def main() -> int:
    _hr()
    print(" DRL ensemble smoke test")
    _hr()

    df = _make_synthetic_df(n=1500)
    _ok(f"synthetic dataset: {df.shape}, columns={len(df.columns)}")

    # ----------------------------------------------------------------------
    # 1. Environment reset/step sanity
    # ----------------------------------------------------------------------
    print("\n[1/5] TradingEnv reset/step shape check")
    env = TradingEnv(df=df, config=EnvConfig(episode_length=200))
    obs, info = env.reset(seed=0)
    assert obs.shape == (17,) and obs.dtype == np.float32, f"obs shape={obs.shape}"
    _ok(f"obs shape {obs.shape}, t_start={info['t_start']}")

    total_reward = 0.0
    for a in [0, 0, 1, 2, 3, 4, 2]:
        obs, r, term, trunc, info = env.step(a)
        total_reward += r
        if term or trunc:
            break
    _ok(f"7-step rollout: total_reward={total_reward:+.4f}, equity={info['equity']:.2f}")
    assert obs.shape == (17,)

    # ----------------------------------------------------------------------
    # 2. Train all three agents briefly
    # ----------------------------------------------------------------------
    print("\n[2/5] Training PPO + A2C + DQN (small budget)")
    with tempfile.TemporaryDirectory() as td:
        tmpdir = Path(td) / "drl"
        ensemble = DRLEnsemble(save_dir=tmpdir, device="cpu", verbose=0)
        # Small budgets — we're just checking the wiring works.
        budgets = {"ppo": 1024, "a2c": 1024, "dqn": 1500}

        # Train each agent independently with its own budget (sb3 differs by algo)
        for name in ("ppo", "a2c", "dqn"):
            t0 = time.perf_counter()
            ensemble.train(
                df=df,
                total_timesteps=budgets[name],
                env_config=EnvConfig(episode_length=200, seed=0),
                seed=11,
                agents=[name],
            )
            t1 = time.perf_counter()
            _ok(f"{name}: {budgets[name]} steps in {(t1 - t0):.1f}s")

        assert (tmpdir / "ppo.zip").exists()
        assert (tmpdir / "a2c.zip").exists()
        assert (tmpdir / "dqn.zip").exists()
        assert (tmpdir / "meta.json").exists()
        _ok(f"all 3 weights + meta.json saved to {tmpdir}")

        # ------------------------------------------------------------------
        # 3. Load + predict + vote
        # ------------------------------------------------------------------
        print("\n[3/5] Load ensemble + per-agent predict + vote")
        fresh = DRLEnsemble(save_dir=tmpdir, device="cpu")
        fresh.load()
        sample_obs = env.reset(seed=1)[0]
        actions = fresh.predict(sample_obs)
        assert set(actions.keys()) == {"ppo", "a2c", "dqn"}, actions
        _ok(f"per-agent actions: {actions}")
        for name, a in actions.items():
            assert 0 <= int(a) <= 4, (name, a)

        v = vote(actions)
        _ok(
            f"vote → dir={v.direction:+d} mag={v.magnitude:.2f} "
            f"conf={v.confidence:.2f} action={v.final_action} "
            f"all_disagree={v.all_disagree}"
        )
        assert 0.0 <= v.confidence <= 1.0

        # Batch predict
        obs_batch = np.stack([env.reset(seed=k)[0] for k in range(8)])
        batch_actions = fresh.predict(obs_batch)
        assert all(arr.shape == (8,) for arr in batch_actions.values())
        votes = vote_batch(batch_actions)
        assert len(votes) == 8
        _ok(f"batch vote: {len(votes)} results, "
            f"mean_conf={np.mean([v.confidence for v in votes]):.2f}")

        # Adversarial: force all-disagree
        forced = vote({"ppo": 0, "a2c": 2, "dqn": 4})  # +1 / 0 / -1
        assert forced.all_disagree and forced.direction == 0
        assert forced.final_action == HOLD_ACTION
        _ok("all-disagree → hold (verified)")

        # ------------------------------------------------------------------
        # 4. Meta-agent in each regime
        # ------------------------------------------------------------------
        print("\n[4/5] Meta-agent regime weighting")
        tft_probs = {"down": 0.15, "flat": 0.25, "up": 0.60}
        for regime in ("trending_up", "mean_reverting", "high_volatility", "unknown"):
            ms = compute_signal(
                tft_probs=tft_probs,
                tft_confidence=0.8,
                drl_vote=v,
                regime=regime,
                regime_confidence=0.9,
            )
            _ok(
                f"regime={regime:<16} sig={ms.final_signal:+d} "
                f"conf={ms.final_confidence:.2f} size={ms.position_size_pct:.2f} "
                f"weights={ms.weights} blocked={ms.blocked_reason}"
            )
            assert -1 <= ms.final_signal <= 1
            assert 0.0 <= ms.position_size_pct <= 1.0

        # High-vol must block when disagreeing
        ms_hv = compute_signal(
            tft_probs={"down": 0.7, "flat": 0.1, "up": 0.2},
            tft_confidence=0.8,
            drl_vote=vote({"ppo": 0, "a2c": 0, "dqn": 0}),    # DRL says up
            regime="high_volatility", regime_confidence=1.0,
        )
        assert ms_hv.final_signal == 0 and ms_hv.blocked_reason == "high_vol_disagreement"
        _ok("high_vol_disagreement → blocked (verified)")

        # ------------------------------------------------------------------
        # 5. Retrain scheduling
        # ------------------------------------------------------------------
        print("\n[5/5] should_retrain timing")
        assert not fresh.should_retrain(), "fresh save should not need retrain"
        # Simulate 8 days passing
        from datetime import datetime, timedelta, timezone
        future = datetime.now(timezone.utc) + timedelta(days=8)
        assert fresh.should_retrain(now=future), "+8d should require retrain"
        _ok("retrain gate works (now → no, +8d → yes)")

    print("\nALL CHECKS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
