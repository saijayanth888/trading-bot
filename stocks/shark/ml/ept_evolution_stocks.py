"""
Stocks EPT evolution scaffold (ALPHA).

⚠️  This is a fitness tracker, NOT a real EPT evolution loop yet. The
real version (8 agents, weight crossover, mutation) needs:
  - Multiple TFT configs trained in parallel
  - Per-agent paper-trade evaluation over a full week
  - Crossover that actually mixes weights without breaking the model
  - Diversity penalty so agents don't collapse to one configuration

What THIS module does today
  - Records weekly fitness for the single TFT model we're shipping
  - Persists to stocks/kb/models/evolution_log.json so the dashboard
    can show generation history
  - Provides a `run_generation()` entry point for the Friday cron that
    will become the real loop in phase 2

Once the real loop ships, the existing `record_fitness` records become
generation-0 baseline.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

_DEFAULT_LOG = (
    Path(__file__).resolve().parents[2] / "kb" / "models" / "evolution_log.json"
)


@dataclass
class GenerationRecord:
    generation: int
    week_ending: str  # ISO date
    members: list[dict]   # [{member_id, agent_type, val_acc, paper_sharpe, ...}]
    champion_id: Optional[str]
    runner_up_id: Optional[str]
    notes: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


def _load_history(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return []


def _save_history(path: Path, history: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(history, indent=2, default=str))
    tmp.replace(path)


def record_generation(
    members: list[dict],
    *,
    log_path: Optional[Path] = None,
    notes: str = "",
) -> dict:
    """Append one generation summary to the evolution log.

    Members are dicts with at minimum:
      member_id (str), agent_type (str), val_acc (float), paper_sharpe (float)

    Champion = highest paper_sharpe (with val_acc as tiebreak).
    """
    log_path = log_path or _DEFAULT_LOG
    history = _load_history(log_path)
    gen = len(history) + 1
    week_ending = datetime.now(timezone.utc).date().isoformat()

    if members:
        ranked = sorted(
            members,
            key=lambda m: (m.get("paper_sharpe", 0), m.get("val_acc", 0)),
            reverse=True,
        )
        champion = ranked[0]["member_id"]
        runner_up = ranked[1]["member_id"] if len(ranked) > 1 else None
    else:
        champion = runner_up = None

    rec = GenerationRecord(
        generation=gen,
        week_ending=week_ending,
        members=members,
        champion_id=champion,
        runner_up_id=runner_up,
        notes=notes or "scaffold v0.1 — real EPT loop deferred to phase 2",
    )
    history.append(rec.to_dict())
    _save_history(log_path, history)
    logger.info("[STOCKS_ML_ALPHA] EPT gen %d recorded: champion=%s",
                gen, champion)
    return rec.to_dict()


def run_generation(
    weights_path: Optional[Path] = None,
    *,
    paper_sharpe_lookup: Optional[dict] = None,
) -> dict:
    """Friday-cron entry point. Reads the trained TFT model + paper-trade
    sharpe (from the past week's trades.jsonl, when populated) and
    records a generation row.

    For phase 1 we have one TFT — record it as the only "member" of
    generation N. As multi-config training comes online (phase 2) the
    members list grows to 8.
    """
    weights_path = weights_path or (
        Path(__file__).resolve().parents[2] / "kb" / "models" / "tft" / "stock_tft_v1.pt"
    )
    if not weights_path.is_file():
        return {"error": "no trained TFT — run train_tft first"}

    # Load the TFT summary for val_acc
    summary_path = weights_path.parent / "stock_tft_v1_summary.json"
    val_acc = 0.0
    if summary_path.is_file():
        try:
            val_acc = float(json.loads(summary_path.read_text()).get("best_val_acc", 0.0))
        except (OSError, json.JSONDecodeError):
            pass

    # Paper sharpe — caller can pass this in; default 0.0 until real
    # paper-trade history exists
    sharpe = 0.0
    if paper_sharpe_lookup:
        sharpe = float(paper_sharpe_lookup.get("stock_tft_v1", 0.0))

    member = {
        "member_id": "stock_tft_v1",
        "agent_type": "tft",
        "val_acc": val_acc,
        "paper_sharpe": sharpe,
        "trained_at_utc": time.time(),
    }
    return record_generation([member])
