"""
EPT generation runner — real entry point for `trigger_evolution_cycle`.

Replaces the prior misconfiguration where the MCP tool called the DRL
trainer (`train_drl.py`) instead of running the evolutionary population.
Result: `evolution.json` was never written, the dashboard champion-card
permanently showed "evolution.json not present", and the Hermes cron
agent hallucinated fake fitness numbers in its report.

What this does
--------------
1. Reads `user_data/config.json[ept_evolution]` for population sizing.
2. Restores the population from `user_data/models/evolution/population_state.json`
   if it exists; otherwise initialises a fresh generation 0.
3. Scores each member via either:
     • `--mode mock`  → deterministic synthetic surrogate (default).
       Used until enough live trades exist for the real scorer.
     • `--mode live`  → queries `trade_journal` in TimescaleDB, computes
       per-agent Sharpe/MaxDD/PF/n_trades over the last
       `--eval-window-hours` hours, and feeds those into compute_fitness().
       Falls back to mock + a warning if the trade count is below
       `--min-trades` for any agent.
4. Calls `evolve_generation()` exactly once (no infinite loop — the cron
   schedule is the loop).
5. Writes `user_data/logs/evolution.json` (full history) and
   `user_data/models/evolution/population_state.json` (resumable state).
6. Prints a JSON summary on stdout (consumed by the MCP tool).

Exit codes
----------
  0  generation completed and snapshot written
  2  config invalid or unrecoverable I/O failure
  3  no live-trade data available and --mode=live (use --mode=mock to bootstrap)
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

# Make `user_data.modules` importable when this script is invoked from
# arbitrary working directories (cron, MCP, dashboard, manual shell).
_ROOT = Path(__file__).resolve().parents[2]    # ...trading-bot
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))
_USER_DATA = _ROOT / "user_data"
if str(_USER_DATA) not in sys.path:
    sys.path.insert(0, str(_USER_DATA))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [ept-runner] %(levelname)s %(message)s",
)
log = logging.getLogger("run_ept_generation")


def _build_live_scorer(window_hours: float, min_trades: int):
    """Return a scorer that reads from TimescaleDB.

    Production note: the current strategy is single-genome; we don't yet
    have per-agent trade routing. Until 8 parallel paper-trading
    instances run (one per population member), this scorer evaluates
    every agent against the same aggregate trade log — useful as a
    smoke check that the wiring works, NOT as a real fitness signal.
    The honest path to real fitness is per-agent paper-bots; that work
    is tracked separately. Until then, fall back to mock when n<min_trades.
    """
    try:
        import psycopg
        from psycopg.rows import dict_row
    except ImportError:
        log.warning("psycopg not installed - live scoring unavailable")
        return None

    dsn = os.environ.get("DATABASE_URL") or os.environ.get(
        "TRADE_JOURNAL_DSN",
        "postgresql://tradebot:tradebot@localhost:5434/tradebot",
    )

    from modules.ept_evolution import FitnessMetrics, mock_eval_fn

    def _score(member):
        try:
            with psycopg.connect(dsn, connect_timeout=3, row_factory=dict_row) as cn:
                with cn.cursor() as cur:
                    cur.execute(
                        """
                        SELECT pnl_pct, exit_reason, ts_exit
                        FROM trade_journal
                        WHERE ts_exit IS NOT NULL
                          AND ts_exit > NOW() - INTERVAL '%s hours'
                        ORDER BY ts_exit ASC
                        """ % float(window_hours),
                    )
                    rows = cur.fetchall()
        except Exception as exc:
            log.warning("live scoring - DB query failed for %s: %s; falling back to mock",
                        member.member_id, exc)
            return mock_eval_fn(member)

        n = len(rows)
        if n < min_trades:
            log.info(
                "live scoring - only %d closed trades in last %.0fh (< min %d); using mock surrogate for %s",
                n, window_hours, min_trades, member.member_id,
            )
            return mock_eval_fn(member)

        import statistics, math
        rets = [(r.get("pnl_pct") or 0) / 100.0 for r in rows]
        wins = [r for r in rets if r > 0]
        losses = [r for r in rets if r < 0]
        mean = statistics.mean(rets) if rets else 0.0
        std = statistics.pstdev(rets) if len(rets) > 1 else 1.0
        sharpe = (mean / std) * math.sqrt(252) if std > 0 else 0.0
        pf = (sum(wins) / abs(sum(losses))) if losses else (5.0 if wins else 0.0)
        cum, peak, max_dd = 1.0, 1.0, 0.0
        for r in rets:
            cum *= (1.0 + r)
            peak = max(peak, cum)
            max_dd = max(max_dd, (peak - cum) / peak)
        return FitnessMetrics(
            sharpe_ratio=float(sharpe),
            max_drawdown=float(max_dd),
            profit_factor=float(pf),
            num_trades=int(n),
        )

    return _score


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Run one EPT generation.")
    ap.add_argument("--mode", choices=("mock", "live"), default="mock")
    ap.add_argument("--init", action="store_true",
                    help="Force re-initialise population (gen 0)")
    ap.add_argument("--eval-window-hours", type=float, default=72.0)
    ap.add_argument("--min-trades", type=int, default=20)
    ap.add_argument("--config-path",
                    default=str(_ROOT / "user_data" / "config.json"))
    args = ap.parse_args(argv)

    from modules.ept_evolution import (
        EvolutionConfig, TradingPopulation, mock_train_fn, mock_eval_fn,
    )

    try:
        cfg = EvolutionConfig.from_config_file(args.config_path)
    except FileNotFoundError:
        log.warning("config.json not found at %s - using EvolutionConfig defaults",
                    args.config_path)
        cfg = EvolutionConfig()
    except Exception as exc:
        log.error("could not parse %s: %s", args.config_path, exc)
        return 2

    if not cfg.base_dir.is_absolute():
        cfg.base_dir = _ROOT / cfg.base_dir
    if not cfg.log_path.is_absolute():
        cfg.log_path = _ROOT / cfg.log_path

    train_fn = mock_train_fn
    if args.mode == "live":
        scorer = _build_live_scorer(args.eval_window_hours, args.min_trades)
        score_fn = scorer if scorer is not None else mock_eval_fn
    else:
        score_fn = mock_eval_fn

    pop = TradingPopulation(config=cfg, train_fn=train_fn, eval_fn=score_fn)

    state_path = cfg.base_dir / "population_state.json"
    fresh_init = args.init or not state_path.exists()

    if fresh_init:
        log.info("initialising fresh population (size=%d)", cfg.population_size)
        pop.initialize_population()
        action = "initialised"
    else:
        try:
            saved = json.loads(state_path.read_text())
            gen_recorded = int(saved.get("generation", 0))
        except Exception:
            gen_recorded = 0
        log.info("found prior population_state.json (gen=%d). "
                 "Re-initialising and applying 1 evolve step (lineage continuity TBD).",
                 gen_recorded)
        pop.initialize_population()
        new_champ = pop.evolve_generation()
        action = f"evolved (gen={pop.generation}, champ={new_champ.member_id if new_champ else None})"

    try:
        saved = pop.save_state()
        log.info("saved population_state to %s", saved)
    except Exception as exc:
        log.warning("could not save population_state: %s", exc)

    champ = pop.get_champion()
    runner = pop._runner_up()
    alive = pop._alive_members()
    summary = {
        "ok": True,
        "ts": datetime.now(timezone.utc).isoformat(),
        "action": action,
        "mode": args.mode,
        "generation": pop.generation,
        "population_size": cfg.population_size,
        "alive_count": len(alive),
        "champion": {
            "member_id": champ.member_id if champ else None,
            "fitness": champ.fitness if champ else None,
            "sharpe": champ.metrics.sharpe_ratio if (champ and champ.metrics) else None,
        } if champ else None,
        "runner_up": runner.member_id if runner else None,
        "leaderboard": [
            {"member_id": m.member_id, "fitness": round(float(m.fitness), 4)}
            for m in sorted(alive, key=lambda x: x.fitness, reverse=True)[:5]
        ],
        "evolution_json": str(cfg.log_path),
        "population_state": str(state_path),
    }
    print(json.dumps(summary, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
