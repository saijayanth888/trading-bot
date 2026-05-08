"""
EPT (Evolutionary Population Training) for trading agents.

Adapted from ModelForge's LoRA-EPT (apps/api/src/agents/ept/{crossover,mutation,
population}.py). The core algorithm — UNIFORM tensor-wise weight blend, rank
selection with elitism, snapshot-per-generation lineage tracking — is the same.
What changed: each "agent" is a TFT + DRL ensemble combo with a genome
(hyperparameters + feature subset + risk parameters), not a LoRA adapter.

Genome (one trading agent)
--------------------------
    learning_rate        log-uniform in [1e-5, 1e-2]
    lookback_window      int in [60, 240]
    n_estimators         int in [1, 5]   (DRL ensemble size for the vote)
    attention_heads      categorical {2, 4, 8}
    feature_subset       tuple of column names; size = 70..90% of master
    stop_loss            float in [-0.10, -0.01]
    take_profit          float in [0.005, 0.06]
    max_position_pct     float in [0.1, 1.0]

Operators
---------
    crossover_genome(A, B, alpha)
        — log-blend lr; linear-blend ints (rounded); union-then-sample features;
          linear-blend risk; categorical → random pick.
    crossover_weights(dir_A, dir_B, dir_C, alpha)
        — tensor-wise UNIFORM blend across every matching .pt and SB3 .zip
          file; identical math to ModelForge's CrossoverStrategy.UNIFORM.
    mutate_genome(g, sigma)
        — Gaussian noise on numerics; sigma decays per generation.
    mutate_features(genome)
        — drop k, add k different features (subset preserved size).

Lifecycle (per generation)
--------------------------
    1. Rank by fitness.
    2. Top 3 = elites (kept unchanged; rank 1 = champion).
    3. Bottom 3 = eliminated.
    4. Breed 3 children from elite pairs (0,1), (0,2), (1,2); each gets
       genome crossover + weight crossover + genome mutation.
    5. Inject 2 random members for diversity.
    6. Train children + randoms via injected `train_fn`.
    7. Evaluate everyone via injected `eval_fn`; compute fitness.
    8. Snapshot population to evolution.json (full lineage).

Auto-demotion
-------------
    `check_demotion()` returns True iff the champion's mean Sharpe over the
    last `auto_demote_window` reports falls below `auto_demote_threshold`.
    On True the runner-up is promoted to champion in place; the demoted
    member stays in the population as "alive" (eligible to be re-elected
    when its fitness improves).

Plugging into the bot
---------------------
The `train_fn` and `eval_fn` are caller-supplied. The contract is:

    train_fn(member) -> None
        — fully train the TFT + DRL stack described by member.genome and
          write all artefacts under member.weights_dir.

    eval_fn(member) -> FitnessMetrics
        — run a 48h paper-trading window using the trained agent, return
          metrics. The default `compute_fitness` formula is

            fitness = sharpe * (1 - max_dd / 0.15) * profit_factor *
                      sqrt(num_trades / 50)

A mock pair lives in this module (`mock_train_fn`, `mock_eval_fn`) for
tests and deterministic dry-runs.
"""

from __future__ import annotations

import json
import logging
import math
import random
import statistics
import shutil
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence

import numpy as np

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Master feature list — superset the genome can sample from. Mirrors the
# columns the strategy is known to produce (see FreqAIMeanRevV1.py). Keep
# this list explicit rather than auto-discovered so genomes are reproducible
# across processes that don't share the strategy import.
# ---------------------------------------------------------------------------

MASTER_FEATURES: tuple[str, ...] = (
    # FreqAI period-expanded features (one per indicator_periods_candles entry)
    "%-rsi-period_10", "%-rsi-period_20",
    "%-atr-period_10", "%-atr-period_20",
    "%-bb_width-period_10", "%-bb_width-period_20",
    "%-bb_pct-period_10", "%-bb_pct-period_20",
    "%-volume_sma_ratio-period_10", "%-volume_sma_ratio-period_20",
    # FreqAI basic features
    "%-macd", "%-macdsignal", "%-macdhist",
    "%-pct_change", "%-raw_volume", "%-raw_price",
    # Time features
    "%-day_of_week", "%-hour_of_day",
    # On-chain
    "%-onchain_netflow_z", "%-onchain_mvrv",
    "%-onchain_whale_count_1h", "%-onchain_whale_volume_1h",
    # Sentiment
    "%-sentiment_score", "%-sentiment_confidence",
    "%-sentiment_bullish", "%-sentiment_bearish", "%-sentiment_agreement",
    # Regime one-hot
    "%-regime_is_trending_up", "%-regime_is_trending_down",
    "%-regime_is_mean_reverting", "%-regime_is_high_volatility",
)

# Hyperparameter ranges
LR_MIN, LR_MAX = 1e-5, 1e-2
LOOKBACK_MIN, LOOKBACK_MAX = 60, 240
NEST_MIN, NEST_MAX = 1, 5
ATTN_CHOICES: tuple[int, ...] = (2, 4, 8)

# Risk ranges
STOP_LOSS_MIN, STOP_LOSS_MAX = -0.10, -0.01
TAKE_PROFIT_MIN, TAKE_PROFIT_MAX = 0.005, 0.06
MAX_POS_MIN, MAX_POS_MAX = 0.1, 1.0

# Feature subset size — fraction of MASTER_FEATURES to keep.
FEAT_FRACTION_MIN, FEAT_FRACTION_MAX = 0.70, 0.90


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------


@dataclass
class TradingGenome:
    learning_rate: float
    lookback_window: int
    n_estimators: int
    attention_heads: int
    feature_subset: tuple[str, ...]
    stop_loss: float
    take_profit: float
    max_position_pct: float

    def to_dict(self) -> dict:
        return {
            "learning_rate": float(self.learning_rate),
            "lookback_window": int(self.lookback_window),
            "n_estimators": int(self.n_estimators),
            "attention_heads": int(self.attention_heads),
            "feature_subset": list(self.feature_subset),
            "stop_loss": float(self.stop_loss),
            "take_profit": float(self.take_profit),
            "max_position_pct": float(self.max_position_pct),
        }

    @classmethod
    def from_dict(cls, d: dict) -> "TradingGenome":
        return cls(
            learning_rate=float(d["learning_rate"]),
            lookback_window=int(d["lookback_window"]),
            n_estimators=int(d["n_estimators"]),
            attention_heads=int(d["attention_heads"]),
            feature_subset=tuple(d.get("feature_subset", ())),
            stop_loss=float(d["stop_loss"]),
            take_profit=float(d["take_profit"]),
            max_position_pct=float(d["max_position_pct"]),
        )


@dataclass
class FitnessMetrics:
    """Components used by `compute_fitness` (and useful for logging)."""
    sharpe_ratio: float
    max_drawdown: float          # absolute value, e.g. 0.08 = -8% peak-to-trough
    profit_factor: float         # gross_profit / max(gross_loss, 1e-9)
    num_trades: int
    extra: dict[str, float] = field(default_factory=dict)


@dataclass
class PopulationMember:
    member_id: str
    genome: TradingGenome
    generation: int
    weights_dir: Path
    parent_a: str | None = None
    parent_b: str | None = None
    crossover_alpha: float | None = None
    crossover_strategy: str = "uniform"
    fitness: float = 0.0
    metrics: FitnessMetrics | None = None
    sharpe_history: list[float] = field(default_factory=list)
    status: str = "alive"        # alive | eliminated | champion | standby
    created_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )

    def to_dict(self) -> dict:
        return {
            "member_id": self.member_id,
            "genome": self.genome.to_dict(),
            "generation": self.generation,
            "weights_dir": str(self.weights_dir),
            "parent_a": self.parent_a,
            "parent_b": self.parent_b,
            "crossover_alpha": self.crossover_alpha,
            "crossover_strategy": self.crossover_strategy,
            "fitness": float(self.fitness),
            "metrics": (asdict(self.metrics) if self.metrics else None),
            "sharpe_history": list(self.sharpe_history),
            "status": self.status,
            "created_at": self.created_at,
        }


@dataclass
class EvolutionConfig:
    population_size: int = 8
    elite_count: int = 3
    eliminate_count: int = 3
    random_inject: int = 2

    alpha_min: float = 0.3
    alpha_max: float = 0.7
    mutation_sigma_initial: float = 0.30
    mutation_sigma_decay: float = 0.95     # per generation
    feature_mutation_swap_count: int = 1   # drop k, add k

    auto_demote_threshold: float = 0.5
    auto_demote_window: int = 3            # number of recent reports

    base_dir: Path = field(
        default_factory=lambda: Path("user_data/models/evolution")
    )
    log_path: Path = field(
        default_factory=lambda: Path("user_data/logs/evolution.json")
    )
    seed: int = 42

    def __post_init__(self) -> None:
        # Sanity: 3 elites + 3 children + 2 random = 8 by default. Allow
        # overrides but warn if they don't add up.
        expected = self.elite_count + self.random_inject + 3
        if self.population_size != expected:
            logger.warning(
                "[ept] population_size=%d but elite+children+random=%d; "
                "the manager will only enforce population_size as the survival cap.",
                self.population_size, expected,
            )


# ---------------------------------------------------------------------------
# Fitness
# ---------------------------------------------------------------------------


def compute_fitness(m: FitnessMetrics) -> float:
    """
    Trading fitness:
        sharpe * (1 - max_dd / 0.15) * profit_factor * sqrt(num_trades / 50)

    Notes:
      - max_dd is taken as an absolute (positive) drawdown fraction.
      - The `(1 - dd/0.15)` term is clipped at 0 so a >15% drawdown nukes
        fitness (rather than going negative and contradicting the sign of
        sharpe * pf for a profitable-but-volatile agent).
      - `sqrt(num_trades / 50)` rewards engagement up to ~50 trades and
        sub-linearly thereafter — discourages sample-starved Sharpe spikes.
    """
    dd_term = max(0.0, 1.0 - abs(m.max_drawdown) / 0.15)
    pf_term = max(0.0, float(m.profit_factor))
    n_term = math.sqrt(max(int(m.num_trades), 0) / 50.0)
    return float(m.sharpe_ratio) * dd_term * pf_term * n_term


# ---------------------------------------------------------------------------
# Random / blend / mutate primitives
# ---------------------------------------------------------------------------


def _clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, float(x)))


def _log_blend(a: float, b: float, alpha: float) -> float:
    return float(math.exp(alpha * math.log(max(a, 1e-12)) + (1 - alpha) * math.log(max(b, 1e-12))))


def _random_genome(rng: random.Random) -> TradingGenome:
    log_lr = rng.uniform(math.log(LR_MIN), math.log(LR_MAX))
    target_size = rng.randint(
        int(FEAT_FRACTION_MIN * len(MASTER_FEATURES)),
        int(FEAT_FRACTION_MAX * len(MASTER_FEATURES)),
    )
    feats = tuple(rng.sample(MASTER_FEATURES, k=target_size))
    return TradingGenome(
        learning_rate=float(math.exp(log_lr)),
        lookback_window=int(rng.randint(LOOKBACK_MIN, LOOKBACK_MAX)),
        n_estimators=int(rng.randint(NEST_MIN, NEST_MAX)),
        attention_heads=int(rng.choice(ATTN_CHOICES)),
        feature_subset=feats,
        stop_loss=float(rng.uniform(STOP_LOSS_MIN, STOP_LOSS_MAX)),
        take_profit=float(rng.uniform(TAKE_PROFIT_MIN, TAKE_PROFIT_MAX)),
        max_position_pct=float(rng.uniform(MAX_POS_MIN, MAX_POS_MAX)),
    )


def _blend_features(
    a: Sequence[str], b: Sequence[str], rng: random.Random,
) -> tuple[str, ...]:
    """Union of parents, sampled to a 70-90% subset of MASTER_FEATURES."""
    union = list(set(a) | set(b))
    target_size = rng.randint(
        int(FEAT_FRACTION_MIN * len(MASTER_FEATURES)),
        int(FEAT_FRACTION_MAX * len(MASTER_FEATURES)),
    )
    if len(union) >= target_size:
        return tuple(rng.sample(union, k=target_size))
    pool = [f for f in MASTER_FEATURES if f not in union]
    needed = target_size - len(union)
    if needed > len(pool):
        return tuple(union + pool)
    return tuple(union + rng.sample(pool, k=needed))


def crossover_genome(
    a: TradingGenome, b: TradingGenome, alpha: float, rng: random.Random,
) -> TradingGenome:
    """Blend two genomes. alpha=1.0 → all A, alpha=0.0 → all B."""
    return TradingGenome(
        learning_rate=_clamp(_log_blend(a.learning_rate, b.learning_rate, alpha), LR_MIN, LR_MAX),
        lookback_window=int(round(_clamp(
            alpha * a.lookback_window + (1 - alpha) * b.lookback_window,
            LOOKBACK_MIN, LOOKBACK_MAX,
        ))),
        n_estimators=int(round(_clamp(
            alpha * a.n_estimators + (1 - alpha) * b.n_estimators,
            NEST_MIN, NEST_MAX,
        ))),
        # Attention heads are categorical; pick one parent's choice.
        attention_heads=rng.choice([a.attention_heads, b.attention_heads]),
        feature_subset=_blend_features(a.feature_subset, b.feature_subset, rng),
        stop_loss=_clamp(
            alpha * a.stop_loss + (1 - alpha) * b.stop_loss,
            STOP_LOSS_MIN, STOP_LOSS_MAX,
        ),
        take_profit=_clamp(
            alpha * a.take_profit + (1 - alpha) * b.take_profit,
            TAKE_PROFIT_MIN, TAKE_PROFIT_MAX,
        ),
        max_position_pct=_clamp(
            alpha * a.max_position_pct + (1 - alpha) * b.max_position_pct,
            MAX_POS_MIN, MAX_POS_MAX,
        ),
    )


def mutate_genome(
    g: TradingGenome, sigma: float, rng: random.Random,
    feature_mutation_swap_count: int = 1,
) -> TradingGenome:
    """
    Gaussian perturbation of numeric genes; categorical heads get a low-
    probability resample; feature subset gets `swap_count` drop+add.

    sigma is the relative perturbation scale (e.g. 0.30 at gen 0). Each
    field is scaled by an appropriate "natural" step — 30% lr factor,
    20% of the lookback range, etc.
    """
    # Numeric genes
    new_lr = g.learning_rate * math.exp(sigma * rng.gauss(0, 1))
    new_lookback = int(round(g.lookback_window + sigma * 30 * rng.gauss(0, 1)))
    new_nest = int(round(g.n_estimators + sigma * 1.5 * rng.gauss(0, 1)))
    new_sl = g.stop_loss + sigma * 0.02 * rng.gauss(0, 1)
    new_tp = g.take_profit + sigma * 0.01 * rng.gauss(0, 1)
    new_pos = g.max_position_pct + sigma * 0.15 * rng.gauss(0, 1)

    # Categorical: low-probability resample
    new_heads = (
        rng.choice(ATTN_CHOICES) if rng.random() < sigma else g.attention_heads
    )

    # Feature mutation: drop k, add k different ones
    feats = list(g.feature_subset)
    pool = [f for f in MASTER_FEATURES if f not in feats]
    k = max(1, int(round(feature_mutation_swap_count)))
    if feats:
        for _ in range(min(k, len(feats))):
            feats.pop(rng.randrange(len(feats)))
    if pool:
        for _ in range(min(k, len(pool))):
            feats.append(pool.pop(rng.randrange(len(pool))))

    return TradingGenome(
        learning_rate=_clamp(new_lr, LR_MIN, LR_MAX),
        lookback_window=int(_clamp(new_lookback, LOOKBACK_MIN, LOOKBACK_MAX)),
        n_estimators=int(_clamp(new_nest, NEST_MIN, NEST_MAX)),
        attention_heads=new_heads,
        feature_subset=tuple(feats),
        stop_loss=_clamp(new_sl, STOP_LOSS_MIN, STOP_LOSS_MAX),
        take_profit=_clamp(new_tp, TAKE_PROFIT_MIN, TAKE_PROFIT_MAX),
        max_position_pct=_clamp(new_pos, MAX_POS_MIN, MAX_POS_MAX),
    )


# ---------------------------------------------------------------------------
# Weight crossover — UNIFORM tensor blend, lifted from ModelForge EPT.
# ---------------------------------------------------------------------------


def _blend_state_dicts(
    sd_a: dict, sd_b: dict, alpha: float,
    *, noise: float = 0.0, rng_seed: int | None = None,
) -> dict:
    """
    EPT UNIFORM blend: child[k] = alpha * A[k] + (1 - alpha) * B[k].

    Optional `noise` (paper "alpha=0.5 + noise" hint) adds a tiny amount of
    Gaussian noise scaled by the per-tensor std, keeping the blend in the
    same regime as the parents.
    """
    import torch

    if rng_seed is not None:
        torch.manual_seed(rng_seed)

    common = set(sd_a.keys()) & set(sd_b.keys())
    if not common:
        return {}
    if len(common) < int(0.8 * max(len(sd_a), len(sd_b))):
        raise ValueError(
            f"weight crossover: parents incompatible — "
            f"{len(sd_a)} / {len(sd_b)} keys with only {len(common)} in common"
        )

    out: dict = {}
    for k in common:
        ta = sd_a[k]
        tb = sd_b[k]
        if not hasattr(ta, "shape") or ta.shape != tb.shape:
            # Skip non-tensor entries (e.g. SB3 buffers like timesteps)
            out[k] = ta
            continue
        blended = alpha * ta.float() + (1.0 - alpha) * tb.float()
        if noise > 0.0:
            std = float(blended.std()) if blended.numel() > 1 else 0.0
            if std > 0.0:
                blended = blended + noise * std * torch.randn_like(blended)
        out[k] = blended.to(ta.dtype)
    return out


def _crossover_torch_pt(path_a: Path, path_b: Path, path_c: Path, alpha: float, noise: float) -> None:
    import torch
    a = torch.load(path_a, map_location="cpu", weights_only=False)
    b = torch.load(path_b, map_location="cpu", weights_only=False)
    if isinstance(a, dict) and "model_state_dict" in a and "model_state_dict" in b:
        merged_sd = _blend_state_dicts(a["model_state_dict"], b["model_state_dict"], alpha, noise=noise)
        a["model_state_dict"] = merged_sd
        torch.save(a, path_c)
    elif isinstance(a, dict) and isinstance(b, dict):
        torch.save(_blend_state_dicts(a, b, alpha, noise=noise), path_c)
    else:
        # Fallback — copy parent A
        shutil.copy(path_a, path_c)


def _crossover_sb3_zip(path_a: Path, path_b: Path, path_c: Path, alpha: float, noise: float, algo: str) -> None:
    """Tensor-blend the policy weights of two SB3 models, save the result."""
    from stable_baselines3 import A2C, DQN, PPO
    cls = {"ppo": PPO, "a2c": A2C, "dqn": DQN}.get(algo)
    if cls is None:
        shutil.copy(path_a, path_c)
        return
    model_a = cls.load(str(path_a), device="cpu")
    model_b = cls.load(str(path_b), device="cpu")
    sd_a = model_a.policy.state_dict()
    sd_b = model_b.policy.state_dict()
    blended = _blend_state_dicts(sd_a, sd_b, alpha, noise=noise)
    # SB3's strict load requires exact key match — _blend_state_dicts only
    # returns keys present in both, so backfill any A-only keys.
    for k in sd_a:
        if k not in blended:
            blended[k] = sd_a[k]
    model_a.policy.load_state_dict(blended, strict=True)
    model_a.save(str(path_c))


def crossover_weights(
    dir_a: Path, dir_b: Path, dir_c: Path, alpha: float = 0.5,
    *, noise: float = 0.0, rng_seed: int | None = None,
) -> list[str]:
    """
    Tensor-wise UNIFORM blend across every matching weight file in two
    parent directories. Recognised filenames:

        ppo.zip / a2c.zip / dqn.zip   — SB3 policies
        tft.pt                        — torch checkpoint (state_dict or
                                        {"model_state_dict": ...})
        *.pt                          — generic torch state_dict

    Files present in only one parent are copied through verbatim.

    Returns the list of filenames written into `dir_c`.
    """
    dir_a = Path(dir_a)
    dir_b = Path(dir_b)
    dir_c = Path(dir_c)
    dir_c.mkdir(parents=True, exist_ok=True)

    SB3_ALGOS = {"ppo": "ppo", "a2c": "a2c", "dqn": "dqn"}
    written: list[str] = []
    files_a = {p.name: p for p in dir_a.iterdir() if p.is_file()}
    files_b = {p.name: p for p in dir_b.iterdir() if p.is_file()}

    for name, pa in files_a.items():
        pc = dir_c / name
        if name not in files_b:
            shutil.copy(pa, pc)
            written.append(name)
            continue
        pb = files_b[name]
        algo = SB3_ALGOS.get(name.replace(".zip", "").lower())
        try:
            if algo:
                _crossover_sb3_zip(pa, pb, pc, alpha, noise, algo)
            elif name.endswith(".pt") or name.endswith(".pth"):
                _crossover_torch_pt(pa, pb, pc, alpha, noise)
            elif name.endswith(".json"):
                # Genome / metadata — not weights; child writes its own.
                continue
            else:
                shutil.copy(pa, pc)
        except Exception as exc:
            logger.warning("[ept] weight blend failed for %s: %s; copying parent A", name, exc)
            shutil.copy(pa, pc)
        written.append(name)

    # Drop a metadata file describing the blend so the lineage survives
    # outside the population manager too.
    meta = {
        "kind": "ept_weight_crossover",
        "parent_a": str(dir_a),
        "parent_b": str(dir_b),
        "alpha": float(alpha),
        "noise": float(noise),
        "rng_seed": rng_seed,
        "files": written,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    (dir_c / "crossover_metadata.json").write_text(json.dumps(meta, indent=2))
    return written


# ---------------------------------------------------------------------------
# Population manager
# ---------------------------------------------------------------------------


TrainFn = Callable[[PopulationMember], None]
EvalFn = Callable[[PopulationMember], FitnessMetrics]


class TradingPopulation:
    """
    Manages an evolving population of trading agents.

    Typical lifecycle (one full cycle = 24h train + 48h paper-trade):

        pop = TradingPopulation(EvolutionConfig(), train_fn, eval_fn)
        pop.initialize_population()              # generation 0
        for _ in range(max_generations):
            pop.evolve_generation()              # next generation
            if pop.check_demotion():
                logger.warning("champion demoted by drift guard")

    Both `train_fn` and `eval_fn` must be supplied for a real run. For
    tests, `mock_train_fn` / `mock_eval_fn` provide deterministic stand-ins.
    """

    def __init__(
        self,
        config: EvolutionConfig | None = None,
        train_fn: TrainFn | None = None,
        eval_fn: EvalFn | None = None,
    ) -> None:
        self.config = config or EvolutionConfig()
        self.train_fn = train_fn
        self.eval_fn = eval_fn
        self.rng = random.Random(self.config.seed)
        self.members: list[PopulationMember] = []
        self.generation = 0
        self.history: list[dict] = []
        self.config.base_dir.mkdir(parents=True, exist_ok=True)
        self.config.log_path.parent.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def initialize_population(self) -> None:
        for i in range(self.config.population_size):
            mid = f"gen0-{i:03d}"
            member = self._make_member(mid, _random_genome(self.rng), generation=0)
            self._train(member)
            self._evaluate(member)
            self.members.append(member)
        self._sort_and_mark()
        self._snapshot(reason="initial")

    def evolve_generation(self) -> PopulationMember | None:
        self.generation += 1
        cfg = self.config

        # 1. Sort + identify elites
        alive = self._alive_members()
        if len(alive) < cfg.elite_count:
            logger.warning("[ept] not enough alive members to evolve")
            return self.get_champion()

        alive.sort(key=lambda m: m.fitness, reverse=True)
        elites = alive[: cfg.elite_count]
        elite_ids = {m.member_id for m in elites}

        # 2. Eliminate the bottom `eliminate_count` non-elites
        remaining = [m for m in alive if m.member_id not in elite_ids]
        remaining.sort(key=lambda m: m.fitness)              # ascending
        to_eliminate = remaining[: cfg.eliminate_count]
        for m in to_eliminate:
            m.status = "eliminated"

        # 3. Sigma decay
        sigma = cfg.mutation_sigma_initial * (
            cfg.mutation_sigma_decay ** self.generation
        )

        # 4. Breed children — pairs (0,1), (0,2), (1,2) capped to elite count
        children: list[PopulationMember] = []
        pairs = [(i, j) for i in range(len(elites)) for j in range(i + 1, len(elites))]
        for k, (i, j) in enumerate(pairs[: cfg.eliminate_count]):
            pa, pb = elites[i], elites[j]
            alpha = self.rng.uniform(cfg.alpha_min, cfg.alpha_max)
            child_genome = crossover_genome(pa.genome, pb.genome, alpha, self.rng)
            child_genome = mutate_genome(
                child_genome, sigma, self.rng,
                feature_mutation_swap_count=cfg.feature_mutation_swap_count,
            )
            mid = f"gen{self.generation}-c{k:02d}"
            child = self._make_member(
                mid, child_genome, generation=self.generation,
                parent_a=pa.member_id, parent_b=pb.member_id,
                crossover_alpha=alpha,
            )
            # Weight crossover (best-effort — falls back to copying parent A)
            try:
                crossover_weights(
                    pa.weights_dir, pb.weights_dir, child.weights_dir,
                    alpha=alpha, noise=0.01,
                    rng_seed=self.rng.randint(0, 2**31 - 1),
                )
            except Exception as exc:
                logger.warning(
                    "[ept] weight crossover %s × %s failed: %s",
                    pa.member_id, pb.member_id, exc,
                )
            self._train(child)            # short fine-tune on the blended weights
            children.append(child)

        # 5. Inject random newcomers
        randoms: list[PopulationMember] = []
        for k in range(cfg.random_inject):
            mid = f"gen{self.generation}-r{k:02d}"
            member = self._make_member(mid, _random_genome(self.rng), generation=self.generation)
            self._train(member)
            randoms.append(member)

        # 6. Evaluate all new members
        for m in children + randoms:
            self._evaluate(m)
            self.members.append(m)

        # 7. Survival cap — keep top population_size from currently-alive set.
        # Champion is in the elite group so it's preserved by construction.
        alive_after = self._alive_members() + children + randoms
        # de-dup
        seen: set[str] = set()
        deduped: list[PopulationMember] = []
        for m in alive_after:
            if m.member_id in seen:
                continue
            seen.add(m.member_id)
            deduped.append(m)
        deduped.sort(key=lambda m: m.fitness, reverse=True)
        survivors = deduped[: cfg.population_size]
        survivor_ids = {m.member_id for m in survivors}

        for m in self.members:
            if m.status == "eliminated":
                continue
            if m.member_id not in survivor_ids:
                m.status = "eliminated"
            else:
                m.status = "alive"

        self._sort_and_mark()
        self._snapshot(reason=f"gen{self.generation}", sigma=sigma)
        return self.get_champion()

    # ------------------------------------------------------------------
    # Auto-demotion guard
    # ------------------------------------------------------------------

    def record_live_sharpe(self, sharpe: float) -> None:
        """Append a live (e.g. daily) Sharpe sample to the champion's history."""
        champ = self.get_champion()
        if champ is None:
            return
        champ.sharpe_history.append(float(sharpe))
        # Cap history so it doesn't grow unbounded
        max_keep = max(8, self.config.auto_demote_window * 4)
        if len(champ.sharpe_history) > max_keep:
            champ.sharpe_history[:] = champ.sharpe_history[-max_keep:]

    def check_demotion(self) -> bool:
        """
        Demote the champion if its mean Sharpe over the last `window` samples
        is below `threshold`. Promotes the runner-up in place; the demoted
        member returns to "alive" status (eligible to be re-elected).

        Returns True iff a swap happened.
        """
        cfg = self.config
        champ = self.get_champion()
        if champ is None:
            return False
        if len(champ.sharpe_history) < cfg.auto_demote_window:
            return False
        recent = champ.sharpe_history[-cfg.auto_demote_window:]
        mean_recent = statistics.fmean(recent)
        if mean_recent >= cfg.auto_demote_threshold:
            return False

        runner_up = self._runner_up()
        if runner_up is None or runner_up.member_id == champ.member_id:
            return False

        logger.warning(
            "[ept] auto-demote: champion=%s mean_sharpe=%.3f < %.3f → promoting %s",
            champ.member_id, mean_recent, cfg.auto_demote_threshold, runner_up.member_id,
        )
        champ.status = "alive"
        runner_up.status = "champion"
        # Reset history on the new champion so demotion doesn't cascade
        # immediately based on the runner-up's stale numbers.
        runner_up.sharpe_history = []
        self._snapshot(
            reason="auto_demotion",
            demoted=champ.member_id,
            promoted=runner_up.member_id,
            mean_sharpe=mean_recent,
        )
        return True

    # ------------------------------------------------------------------
    # Accessors
    # ------------------------------------------------------------------

    def get_champion(self) -> PopulationMember | None:
        for m in self.members:
            if m.status == "champion":
                return m
        alive = self._alive_members()
        return max(alive, key=lambda m: m.fitness) if alive else None

    def get_runner_up(self) -> PopulationMember | None:
        return self._runner_up()

    def get_lineage(self, member_id: str) -> list[str]:
        by_id = {m.member_id: m for m in self.members}
        seen: set[str] = set()
        out: list[str] = []
        cur = by_id.get(member_id)
        while cur and cur.member_id not in seen:
            seen.add(cur.member_id)
            out.append(cur.member_id)
            cur = by_id.get(cur.parent_a or "") if cur.parent_a else None
        return list(reversed(out))

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _alive_members(self) -> list[PopulationMember]:
        return [m for m in self.members if m.status in ("alive", "champion", "standby")]

    def _runner_up(self) -> PopulationMember | None:
        alive = self._alive_members()
        if len(alive) < 2:
            return None
        ranked = sorted(alive, key=lambda m: m.fitness, reverse=True)
        # Skip the champion, take next
        for m in ranked:
            if m.status != "champion":
                return m
        return None

    def _make_member(
        self, member_id: str, genome: TradingGenome, generation: int,
        *, parent_a: str | None = None, parent_b: str | None = None,
        crossover_alpha: float | None = None,
    ) -> PopulationMember:
        weights_dir = self.config.base_dir / member_id
        weights_dir.mkdir(parents=True, exist_ok=True)
        member = PopulationMember(
            member_id=member_id,
            genome=genome,
            generation=generation,
            weights_dir=weights_dir,
            parent_a=parent_a,
            parent_b=parent_b,
            crossover_alpha=crossover_alpha,
        )
        # Persist genome.json so weight artefacts can be tied back to
        # their hyperparameters even outside the manager process.
        (weights_dir / "genome.json").write_text(json.dumps(member.genome.to_dict(), indent=2))
        return member

    def _train(self, member: PopulationMember) -> None:
        if self.train_fn is None:
            logger.debug("[ept] no train_fn supplied — skipping training of %s", member.member_id)
            return
        try:
            self.train_fn(member)
        except Exception as exc:
            logger.warning("[ept] train_fn failed for %s: %s", member.member_id, exc)

    def _evaluate(self, member: PopulationMember) -> None:
        if self.eval_fn is None:
            return
        try:
            metrics = self.eval_fn(member)
        except Exception as exc:
            logger.warning("[ept] eval_fn failed for %s: %s", member.member_id, exc)
            metrics = FitnessMetrics(
                sharpe_ratio=0.0, max_drawdown=1.0, profit_factor=0.0, num_trades=0,
            )
        member.metrics = metrics
        member.fitness = compute_fitness(metrics)
        member.sharpe_history.append(float(metrics.sharpe_ratio))

    def _sort_and_mark(self) -> None:
        """
        Mark champion and standby (runner-up) statuses on alive members.
        Champion is the highest-fitness alive member; runner-up gets
        `standby` (no behaviour change here, just labelling for ops).
        """
        alive = self._alive_members()
        if not alive:
            return
        alive_sorted = sorted(alive, key=lambda m: m.fitness, reverse=True)
        for m in alive_sorted:
            m.status = "alive"
        alive_sorted[0].status = "champion"
        if len(alive_sorted) > 1:
            alive_sorted[1].status = "standby"

    # ------------------------------------------------------------------
    # Snapshots
    # ------------------------------------------------------------------

    def _snapshot(self, *, reason: str, **extra: Any) -> None:
        """
        Append a generation snapshot to evolution.json. Each entry has full
        lineage info: every alive member's genome + parents + alpha.
        """
        champ = self.get_champion()
        runner = self._runner_up()
        snapshot = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "generation": self.generation,
            "reason": reason,
            "config": {
                "population_size": self.config.population_size,
                "elite_count": self.config.elite_count,
                "eliminate_count": self.config.eliminate_count,
                "random_inject": self.config.random_inject,
                "alpha_min": self.config.alpha_min,
                "alpha_max": self.config.alpha_max,
                "auto_demote_threshold": self.config.auto_demote_threshold,
                "auto_demote_window": self.config.auto_demote_window,
            },
            "champion": (champ.member_id if champ else None),
            "runner_up": (runner.member_id if runner else None),
            "alive": [m.to_dict() for m in self._alive_members()],
            "newly_eliminated": [
                m.to_dict() for m in self.members
                if m.status == "eliminated" and m.generation == self.generation - 1
            ],
            **extra,
        }
        self.history.append(snapshot)
        try:
            self.config.log_path.write_text(json.dumps(self.history, indent=2, default=str))
        except Exception as exc:
            logger.warning("[ept] could not write evolution.json: %s", exc)

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def save_state(self, path: Path | None = None) -> Path:
        path = path or (self.config.base_dir / "population_state.json")
        state = {
            "generation": self.generation,
            "members": [m.to_dict() for m in self.members],
            "config": asdict(self.config),
        }
        # Serialise Path-typed config fields as strings.
        state["config"]["base_dir"] = str(self.config.base_dir)
        state["config"]["log_path"] = str(self.config.log_path)
        path.write_text(json.dumps(state, indent=2, default=str))
        return path


# ---------------------------------------------------------------------------
# Mock train / eval — deterministic synthetic stand-ins for tests.
# ---------------------------------------------------------------------------


def mock_train_fn(member: PopulationMember) -> None:
    """
    Write tiny synthetic 'tft.pt' + 'ppo.zip'/'a2c.zip'/'dqn.zip' surrogates
    so weight crossover has real files to blend. Fast — no GPU, no SB3.

    The 'weights' here are toy 1D tensors that scale with genome
    hyperparameters — enough to confirm the crossover math runs.
    """
    import torch
    g = member.genome
    member.weights_dir.mkdir(parents=True, exist_ok=True)
    fingerprint = (
        math.log(g.learning_rate)
        + g.lookback_window / 100.0
        + g.attention_heads
    )
    sd = {
        "linear.weight": torch.full((4, 4), fingerprint, dtype=torch.float32),
        "linear.bias": torch.full((4,), fingerprint * 0.1, dtype=torch.float32),
    }
    torch.save({"model_state_dict": sd}, member.weights_dir / "tft.pt")
    # SB3 zips — skip in mock; weight crossover handles missing parts gracefully.


def mock_eval_fn(member: PopulationMember) -> FitnessMetrics:
    """
    Deterministic surrogate fitness:
      - Sharpe rewards moderate lookback (~120) and feature size around 0.8 of master.
      - Drawdown grows with leverage (max_position_pct).
      - profit_factor + num_trades scaled to look realistic.

    Reproducibility: depends only on the genome, no RNG.
    """
    g = member.genome
    feat_frac = len(g.feature_subset) / max(1, len(MASTER_FEATURES))
    feat_score = 1.0 - abs(feat_frac - 0.80) * 4.0           # peaks at 0.80
    lookback_score = 1.0 - abs(g.lookback_window - 120) / 120
    head_score = {2: 0.7, 4: 1.0, 8: 0.85}.get(g.attention_heads, 0.5)

    sharpe = max(-1.0, 1.5 * feat_score * lookback_score * head_score - 0.2)
    max_dd = max(0.01, 0.04 + 0.10 * g.max_position_pct - 0.05 * abs(g.stop_loss))
    pf = max(0.1, 1.2 + 0.5 * sharpe)
    trades = int(40 + 30 * (g.take_profit / TAKE_PROFIT_MAX) - 20 * abs(g.stop_loss))
    return FitnessMetrics(
        sharpe_ratio=float(sharpe),
        max_drawdown=float(max_dd),
        profit_factor=float(pf),
        num_trades=int(max(1, trades)),
    )


# ---------------------------------------------------------------------------
# Convenience: convert genome → DRL/TFT config dicts
# ---------------------------------------------------------------------------


def genome_to_drl_hparams(g: TradingGenome) -> dict:
    """Map a trading genome onto the SB3 hyperparameter dict the DRL ensemble accepts."""
    return {
        "ppo": {"learning_rate": g.learning_rate, "policy": "MlpPolicy"},
        "a2c": {"learning_rate": g.learning_rate * 1.5, "policy": "MlpPolicy"},
        "dqn": {"learning_rate": g.learning_rate * 0.5, "policy": "MlpPolicy"},
    }


def genome_to_tft_kwargs(g: TradingGenome) -> dict:
    """Map a trading genome onto the TFT constructor kwargs."""
    return {
        "n_heads": g.attention_heads,
        "sequence_length": g.lookback_window,
    }
