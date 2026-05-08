"""
End-to-end smoke test for ept_evolution.

Verifies:
  1. Random genome generation respects all hyperparameter ranges.
  2. crossover_genome blends within bounds and respects parents.
  3. mutate_genome stays within bounds and reduces magnitude with sigma.
  4. crossover_weights tensor-blends matching .pt files (UNIFORM math).
  5. TradingPopulation.initialize_population() seeds 8 members + snapshot.
  6. evolve_generation() applies the 3-elite / 3-children / 2-random rule.
  7. evolution.json contains lineage (parent_a, parent_b, alpha) for children.
  8. compute_fitness matches the spec.
  9. record_live_sharpe + check_demotion swap champion when triggered.
"""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
from pathlib import Path

import numpy as np
import torch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "user_data"))

from modules.ept_evolution import (   # noqa: E402
    ATTN_CHOICES,
    EvolutionConfig,
    FEAT_FRACTION_MAX,
    FEAT_FRACTION_MIN,
    FitnessMetrics,
    LR_MAX,
    LR_MIN,
    LOOKBACK_MAX,
    LOOKBACK_MIN,
    MASTER_FEATURES,
    MAX_POS_MAX,
    MAX_POS_MIN,
    NEST_MAX,
    NEST_MIN,
    STOP_LOSS_MAX,
    STOP_LOSS_MIN,
    TAKE_PROFIT_MAX,
    TAKE_PROFIT_MIN,
    TradingGenome,
    TradingPopulation,
    _random_genome,
    compute_fitness,
    crossover_genome,
    crossover_weights,
    mock_eval_fn,
    mock_train_fn,
    mutate_genome,
)
import random


def _ok(msg: str) -> None: print(f"  [✓] {msg}")
def _info(msg: str) -> None: print(f"  [i] {msg}")
def _hr() -> None: print("=" * 64)


def _validate_genome(g: TradingGenome) -> None:
    assert LR_MIN <= g.learning_rate <= LR_MAX, g.learning_rate
    assert LOOKBACK_MIN <= g.lookback_window <= LOOKBACK_MAX, g.lookback_window
    assert NEST_MIN <= g.n_estimators <= NEST_MAX, g.n_estimators
    assert g.attention_heads in ATTN_CHOICES, g.attention_heads
    feat_frac = len(g.feature_subset) / len(MASTER_FEATURES)
    assert FEAT_FRACTION_MIN - 0.05 <= feat_frac <= FEAT_FRACTION_MAX + 0.05, feat_frac
    assert STOP_LOSS_MIN <= g.stop_loss <= STOP_LOSS_MAX, g.stop_loss
    assert TAKE_PROFIT_MIN <= g.take_profit <= TAKE_PROFIT_MAX, g.take_profit
    assert MAX_POS_MIN <= g.max_position_pct <= MAX_POS_MAX, g.max_position_pct
    assert all(f in MASTER_FEATURES for f in g.feature_subset)


def main() -> int:
    _hr()
    print(" EPT trading-evolution smoke test")
    _hr()

    # ----------------------------------------------------------------------
    # 1. Random genome bounds
    # ----------------------------------------------------------------------
    print("\n[1/8] Random genome generation")
    rng = random.Random(0)
    for _ in range(50):
        _validate_genome(_random_genome(rng))
    _ok("50 random genomes — all within bounds")

    # ----------------------------------------------------------------------
    # 2. Crossover stays within bounds and is between parents
    # ----------------------------------------------------------------------
    print("\n[2/8] crossover_genome")
    for _ in range(30):
        a = _random_genome(rng)
        b = _random_genome(rng)
        alpha = rng.uniform(0.3, 0.7)
        c = crossover_genome(a, b, alpha, rng)
        _validate_genome(c)
        # Risk parameters are linear blends → must lie between parents
        lo, hi = sorted([a.stop_loss, b.stop_loss])
        assert lo - 1e-9 <= c.stop_loss <= hi + 1e-9
        lo, hi = sorted([a.take_profit, b.take_profit])
        assert lo - 1e-9 <= c.take_profit <= hi + 1e-9
    _ok("30 crossovers — bounds + parent-bracketing OK")

    # ----------------------------------------------------------------------
    # 3. Mutation stays within bounds; sigma decay reduces step magnitude
    # ----------------------------------------------------------------------
    print("\n[3/8] mutate_genome")
    g0 = _random_genome(rng)
    big_steps = []
    small_steps = []
    for _ in range(40):
        big_steps.append(abs(mutate_genome(g0, 0.5, rng).learning_rate - g0.learning_rate))
        small_steps.append(abs(mutate_genome(g0, 0.05, rng).learning_rate - g0.learning_rate))
    bs_mean = float(np.mean(big_steps))
    ss_mean = float(np.mean(small_steps))
    assert bs_mean > ss_mean, f"sigma decay broken: big={bs_mean} small={ss_mean}"
    for _ in range(40):
        _validate_genome(mutate_genome(g0, 0.3, rng))
    _ok(f"mutated bounds OK; sigma=0.5 mean Δ={bs_mean:.2e} > sigma=0.05 mean Δ={ss_mean:.2e}")

    # ----------------------------------------------------------------------
    # 4. Weight crossover (UNIFORM tensor blend) on a real .pt file
    # ----------------------------------------------------------------------
    print("\n[4/8] crossover_weights — UNIFORM tensor blend")
    with tempfile.TemporaryDirectory() as td:
        td_path = Path(td)
        dir_a, dir_b, dir_c = td_path / "A", td_path / "B", td_path / "C"
        for d in (dir_a, dir_b):
            d.mkdir()
        sd_a = {"linear.weight": torch.full((4, 4), 1.0), "linear.bias": torch.full((4,), 1.0)}
        sd_b = {"linear.weight": torch.full((4, 4), 3.0), "linear.bias": torch.full((4,), 3.0)}
        torch.save({"model_state_dict": sd_a}, dir_a / "tft.pt")
        torch.save({"model_state_dict": sd_b}, dir_b / "tft.pt")
        # File only in A — should be copied through verbatim
        (dir_a / "extra.txt").write_text("a-only")

        crossover_weights(dir_a, dir_b, dir_c, alpha=0.5, noise=0.0, rng_seed=0)
        merged = torch.load(dir_c / "tft.pt", map_location="cpu", weights_only=False)
        w = merged["model_state_dict"]["linear.weight"]
        assert torch.allclose(w, torch.full_like(w, 2.0))
        assert (dir_c / "extra.txt").exists()
        # Metadata file written
        meta = json.loads((dir_c / "crossover_metadata.json").read_text())
        assert meta["alpha"] == 0.5 and meta["kind"] == "ept_weight_crossover"
        _ok("alpha=0.5 of {1.0, 3.0} → 2.0 across all tensors; A-only file copied; meta written")

    # ----------------------------------------------------------------------
    # 5 + 6. Run two evolutionary generations with mock train/eval
    # ----------------------------------------------------------------------
    print("\n[5/8] TradingPopulation initialise + evolve x2")
    with tempfile.TemporaryDirectory() as td:
        td_path = Path(td)
        cfg = EvolutionConfig(
            population_size=8,
            elite_count=3,
            eliminate_count=3,
            random_inject=2,
            base_dir=td_path / "members",
            log_path=td_path / "evolution.json",
            seed=11,
        )
        pop = TradingPopulation(cfg, train_fn=mock_train_fn, eval_fn=mock_eval_fn)
        pop.initialize_population()

        alive = [m for m in pop.members if m.status in ("alive", "champion", "standby")]
        assert len(alive) == 8, f"expected 8 alive after init, got {len(alive)}"
        champ0 = pop.get_champion()
        assert champ0 is not None
        _ok(f"gen0: 8 members, champion={champ0.member_id} fitness={champ0.fitness:.3f}")

        # Generation 1
        champ1 = pop.evolve_generation()
        gen1_alive = [m for m in pop.members if m.status in ("alive", "champion", "standby")]
        assert len(gen1_alive) == 8, f"expected 8 alive after gen1, got {len(gen1_alive)}"

        # Verify counts: 3 elite (gen 0 originals) + 3 children + 2 randoms among gen1
        gen1_new = [m for m in pop.members if m.generation == 1]
        gen1_children = [m for m in gen1_new if m.parent_a is not None]
        gen1_randoms = [m for m in gen1_new if m.parent_a is None]
        assert len(gen1_children) == 3, f"expected 3 children, got {len(gen1_children)}"
        assert len(gen1_randoms) == 2, f"expected 2 randoms, got {len(gen1_randoms)}"
        _ok(f"gen1: 3 children + 2 randoms; champion={champ1.member_id} fitness={champ1.fitness:.3f}")

        # Generation 2
        champ2 = pop.evolve_generation()
        assert champ2 is not None
        _ok(f"gen2: champion={champ2.member_id} fitness={champ2.fitness:.3f}")

    # ----------------------------------------------------------------------
    # 7. evolution.json structure + lineage
    # ----------------------------------------------------------------------
        history = json.loads(cfg.log_path.read_text())
        assert len(history) == 3, f"expected 3 snapshots (init + 2 gens), got {len(history)}"
        gen1_snap = history[1]
        assert gen1_snap["generation"] == 1
        assert gen1_snap["champion"] is not None
        # Each child member has parent_a + parent_b + crossover_alpha
        children_in_snap = [m for m in gen1_snap["alive"] if m["parent_a"]]
        assert children_in_snap, "no children with lineage in gen1 snapshot"
        for c in children_in_snap:
            assert c["parent_a"] and c["parent_b"]
            assert 0.3 <= c["crossover_alpha"] <= 0.7
        _ok(f"evolution.json: 3 snapshots, gen1 has {len(children_in_snap)} children with full lineage")

        # Lineage trace
        lineage = pop.get_lineage(champ2.member_id)
        assert lineage and lineage[-1] == champ2.member_id
        _ok(f"lineage of champion: {' → '.join(lineage[-3:])}")

    # ----------------------------------------------------------------------
    # 8. Fitness formula + auto-demotion
    # ----------------------------------------------------------------------
    print("\n[8/8] compute_fitness + auto-demotion")
    f = compute_fitness(FitnessMetrics(
        sharpe_ratio=2.0, max_drawdown=0.075, profit_factor=1.5, num_trades=200,
    ))
    # Manually: 2.0 * (1 - 0.075/0.15) * 1.5 * sqrt(200/50) = 2.0 * 0.5 * 1.5 * 2.0 = 3.0
    assert abs(f - 3.0) < 1e-9, f
    _ok(f"fitness(sharpe=2.0, dd=0.075, pf=1.5, trades=200) = {f:.3f} (expected 3.0)")

    # >15% drawdown → fitness clipped to 0
    f_blowup = compute_fitness(FitnessMetrics(
        sharpe_ratio=5.0, max_drawdown=0.20, profit_factor=2.0, num_trades=200,
    ))
    assert f_blowup == 0.0, f_blowup
    _ok(f"fitness with dd=20% clipped to 0 (catastrophe guard)")

    # Auto-demotion
    with tempfile.TemporaryDirectory() as td:
        td_path = Path(td)
        cfg = EvolutionConfig(
            base_dir=td_path / "members",
            log_path=td_path / "evolution.json",
            auto_demote_window=3,
            auto_demote_threshold=0.5,
            seed=99,
        )
        pop = TradingPopulation(cfg, train_fn=mock_train_fn, eval_fn=mock_eval_fn)
        pop.initialize_population()
        original_champ = pop.get_champion()
        original_runner = pop.get_runner_up()
        assert original_runner is not None and original_runner.member_id != original_champ.member_id

        # Two good days first → no demotion
        pop.record_live_sharpe(1.2)
        pop.record_live_sharpe(0.9)
        assert not pop.check_demotion(), "should not demote with high sharpe"
        # Three bad days in a row → demote
        for _ in range(3):
            pop.record_live_sharpe(0.1)
        demoted = pop.check_demotion()
        assert demoted, "should demote after 3 sub-threshold sharpes"
        new_champ = pop.get_champion()
        assert new_champ.member_id == original_runner.member_id
        assert new_champ.member_id != original_champ.member_id
        _ok(
            f"auto-demote: {original_champ.member_id} → {new_champ.member_id} "
            f"(threshold {cfg.auto_demote_threshold}, window {cfg.auto_demote_window})"
        )

        # Verify the demotion was logged
        events = json.loads(cfg.log_path.read_text())
        assert any(e.get("reason") == "auto_demotion" for e in events)
        _ok("auto_demotion event present in evolution.json")

    print("\nALL CHECKS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
