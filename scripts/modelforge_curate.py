#!/usr/bin/env python3
"""
ModelForge curate — Stage 2 of the trading-bot ↔ ModelForge data pipeline.

Reads raw JSONL files written by ``modelforge_ingest.py`` from
``~/.dgx-train/raw/<role>/*.jsonl``, applies per-role deterministic curation
filters, and writes a Hugging Face ``datasets`` Arrow shard to
``~/.dgx-train/datasets/<role>/curated/`` that exactly matches the schema
ModelForge's ``HuggingFaceDataCurator`` consumes (``category``, ``source``,
``dataset_name``, ``instruction``, ``response``) plus an ``mf_meta.json``
sidecar.

The filters are pure, deterministic, and offline -- no LLM calls. That keeps
the cron cheap (~1s per role for a typical day) and the accept/reject ledger
reproducible.

Scheduling
----------
Target cron slot: nightly 21:30 ET via Hermes -- 30 minutes after Stage 1.

Failure mode
------------
Fail-soft. The Slack alert path fires only when ``accept_rate`` falls outside
the operator-set band ``[ACCEPT_RATE_LO, ACCEPT_RATE_HI]`` (default 10%-90%)
and a Slack notifier is importable from the repo; otherwise the alert is
written to stdout and the per-day stats file.

CLI
---
::

    python scripts/modelforge_curate.py             # curate everything new
    python scripts/modelforge_curate.py 2026-05-11  # curate one date

See ``docs/MODELFORGE_DATA_PIPELINE.md`` for the schema + flow diagram.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import os
import re
import sys
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

logger = logging.getLogger("modelforge_curate")


# --------------------------------------------------------------------------- #
# Module layout & roles
# --------------------------------------------------------------------------- #

_HERE = Path(__file__).resolve()
_REPO_ROOT = _HERE.parents[1]

ALL_ROLES: tuple[str, ...] = (
    "trading-reflector",
    "trading-bull",
    "trading-bear",
    "trading-arbiter",
    "trading-regime-tagger",
    "trading-indicator-selector",
)

# --------------------------------------------------------------------------- #
# Regime-tagger baseline indicators (Section B, Section I)
# Deterministic fallback for indicator-selector when the LLM is unreachable.
# Also used as baseline_output for indicator-selector test-set rows.
# --------------------------------------------------------------------------- #

REGIME_BASELINE_INDICATORS: dict[str, list[str]] = {
    "trending_up":    ["ema", "macd", "atr", "adx", "rsi"],
    "trending_down":  ["ema", "macd", "atr", "adx", "rsi"],
    "ranging":        ["bbands", "rsi", "stoch", "mfi", "atr"],
    "high_volatility":["atr", "bbands", "rsi", "vwap", "obv"],
    "low_volatility": ["bbands", "rsi", "sma", "mfi", "stoch"],
    "breakout_up":    ["vwap", "obv", "atr", "macd", "adx"],
    "breakout_down":  ["vwap", "obv", "atr", "macd", "adx"],
}

#: Strategy column → RegimeLabel mapping (Section A). Used in curate for
#: regime-tagger test-set row baseline_output derivation. Keep in sync with
#: modelforge_ingest_decisions.py::STRATEGY_TO_REGIME.
_STRATEGY_TO_REGIME_CURATE: dict[str, str] = {
    "meta_up_regime":   "trending_up",
    "meta_down_regime": "trending_down",
    "bb_squeeze":       "ranging",
    "bb_breakout":      "breakout_up",
    "bb_revert":        "ranging",
}


# --------------------------------------------------------------------------- #
# Minimum-records-to-train thresholds (Section D, rev2 numbers)
# Sources: HuggingFace PEFT instruction-tuning ablations (Mangrulkar et al.
# 2023); QLoRA paper (Dettmers et al. 2023). Hermes-3-Llama-3.1-8B is an 8B
# base — practical floor for measurable generalization at rank 16 is 100-200.
# We set N_MIN_TRAIN at 100 and accept that early generations will be
# undertrained; the first-gen min-score gate (evolution_graph.py) ensures only
# adapters that beat the base advance to production.
# --------------------------------------------------------------------------- #

N_MIN_TRAIN: dict[str, int] = {
    "trading-reflector":          100,  # post-mortem prose; 100 closed trades for predictive_hit_rate_30d SNR >= 0.2
    "trading-bull":               100,  # debate prose with stock-specific vocabulary
    "trading-bear":               100,
    "trading-arbiter":            100,  # structured output + PnL labels
    "trading-regime-tagger":       40,  # 7-class JSON classifier; smaller because base already knows JSON
    "trading-indicator-selector":  40,  # 20 indicators, top-k selection; deterministic baseline is the anchor
}

N_MIN_TEST: dict[str, int] = {
    "trading-reflector":          20,
    "trading-bull":               20,
    "trading-bear":               20,
    "trading-arbiter":            25,
    "trading-regime-tagger":      15,
    "trading-indicator-selector": 15,
}

# --------------------------------------------------------------------------- #
# Crypto-term contamination blocklist (Section A, Section D)
# Applied ONLY to bull/bear/arbiter prose roles. Case-insensitive substring
# match. regime-tagger uses strategy column label strings (not prose), so it
# is exempt. Reflector is exempt (stock-only source by design).
# --------------------------------------------------------------------------- #

#: Roles to which the crypto-term blocklist applies.
CRYPTO_BLOCKLIST_ROLES: frozenset[str] = frozenset({
    "trading-bull", "trading-bear", "trading-arbiter",
})

#: Crypto-specific terms that contaminate stock-trader training data.
CRYPTO_TERMS: tuple[str, ...] = (
    "funding rate", "on-chain", "USDT", "USDC", "BTC", "ETH", "LTC",
    "SOL", "ADA", "perpetual", "leverage", "staking", "mempool",
    "gas fee", "tokenomics", "airdrop", "whale", "24/7", "mining",
    "validator", "halving",
)

#: Known exit reasons emitted by the trading bot. Anything outside this set is
#: a reflector reject. Keep in sync with `freqtrade` strategy exit signals.
KNOWN_EXIT_REASONS: frozenset[str] = frozenset({
    "ROI", "stop_loss", "trailing_stop_loss", "stoploss_on_exchange",
    "freqai_long", "freqai_short", "freqai_exit",
    "meta_up_regime", "meta_down_regime", "meta_exit",
    "bb_breakout", "bb_revert", "bb_squeeze",
    "force_exit", "exit_signal",
})

#: Slack alert band -- accept rates outside this window trip the notifier.
ACCEPT_RATE_LO_DEFAULT = 0.10
ACCEPT_RATE_HI_DEFAULT = 0.90


# --------------------------------------------------------------------------- #
# On-disk layout
# --------------------------------------------------------------------------- #

def _dgx_train_root() -> Path:
    """Resolve the root of the on-disk training-data layout.

    The same env var (``DGX_TRAIN_ROOT``) is honoured here and in
    :mod:`modelforge_ingest` so the two scripts stay agreed on the bind-mount
    path.
    """
    override = os.environ.get("DGX_TRAIN_ROOT", "").strip()
    if override:
        return Path(override)
    return Path.home() / ".dgx-train"


def _curate_log_path() -> Path:
    """Where fail-soft errors land for the cron job to tail."""
    override = os.environ.get("MODELFORGE_CURATE_LOG", "").strip()
    if override:
        return Path(override)
    return _REPO_ROOT / "stocks" / "memory" / "cron-modelforge-curate.log"


def _state_path(root: Path) -> Path:
    """JSON file holding the last-curated source-file per role."""
    return root / "curate" / "state.json"


# --------------------------------------------------------------------------- #
# Reject-reason taxonomy -- one stable code per filter clause so the operator
# can grep the curate/<role>_<date>.json files for trends.
# --------------------------------------------------------------------------- #

class Reject:
    """Namespace of stable reject-reason codes.

    Using a class rather than an Enum because we serialize these to JSON keys
    and reading them downstream as plain strings is cheaper to grep.
    """

    PENDING                 = "pending_outcome"          # reflector: trade hasn't closed
    EMPTY_RESPONSE          = "empty_response"
    LENGTH_OUT_OF_BAND      = "length_out_of_band"
    ALPHA_REGEX_MISMATCH    = "alpha_regex_mismatch"
    UNKNOWN_EXIT_REASON     = "unknown_exit_reason"
    EVIDENCE_TOO_THIN       = "evidence_too_thin"
    STRUCTURED_INVALID      = "structured_output_invalid"
    CRYPTO_TERM_CONTAMINATION = "crypto_term_contamination"


# --------------------------------------------------------------------------- #
# Per-role filters -- each returns (kept: bool, reject_code: str | None)
# --------------------------------------------------------------------------- #

_ALPHA_PCT_RE = re.compile(r"([+\-]?\d+(?:\.\d+)?)\s*%")
_EVIDENCE_RE = re.compile(
    r"\$\d|\d+(?:\.\d+)?\s*%|\b(?:RSI|MACD|EMA|SMA|ATR|BB|VWAP|ADX|SAR)\b|"
    r"\b(?:20\d\d-\d{2}-\d{2})\b"
)


def filter_reflector(example: dict[str, Any]) -> tuple[bool, str | None]:
    """Reflector curation rules per integration plan § Stage 2.

    Keep iff:
      * trade is realized (``pending_outcome=False``)
      * response cites alpha_pct that's within ±5% of the ledger's value
      * ledger's ``exit_reason`` (if present) is in :data:`KNOWN_EXIT_REASONS`
        -- when absent we don't gate on it, the reflector log doesn't always
        carry it
      * response length is in [80, 1200] characters. The original cap of 600
        targeted a "2-4 sentence" reflection, but real-world Shark
        trade_reviewer outputs from hermes3:8b/70b run 600-1000 chars when
        the trade has multiple lessons to call out -- 1200 captures those
        without admitting model-rambling.
    """
    if example.get("pending_outcome", False):
        return False, Reject.PENDING

    response = example.get("response") or ""
    if not response.strip():
        return False, Reject.EMPTY_RESPONSE
    if not (80 <= len(response) <= 1200):
        return False, Reject.LENGTH_OUT_OF_BAND

    ledger = example.get("ledger") or {}
    ledger_alpha_str = str(ledger.get("alpha_pct") or "")
    m = _ALPHA_PCT_RE.search(ledger_alpha_str)
    if m:
        try:
            ledger_alpha = float(m.group(1))
        except ValueError:
            ledger_alpha = None
        if ledger_alpha is not None:
            cited = [float(g) for g in _ALPHA_PCT_RE.findall(response) if _try_float(g) is not None]
            if cited:
                # Pass if any cited number is within ±5 pp of the realized alpha.
                # Bot citations are noisy (raw_pct vs alpha_pct vs holding_days
                # all carry %) so we look for "any plausible match" rather than
                # demanding the model parrot back the exact value.
                if not any(abs(c - ledger_alpha) <= 5.0 for c in cited):
                    return False, Reject.ALPHA_REGEX_MISMATCH

    exit_reason = ledger.get("exit_reason")
    if exit_reason is not None and exit_reason not in KNOWN_EXIT_REASONS:
        return False, Reject.UNKNOWN_EXIT_REASON

    # TODO(v2): hindsight relabeling -- check whether the *next* same-ticker
    # trade within 30 days is consistent with this reflection's lesson. Needs
    # cross-day state which is more than this branch should take on.

    return True, None


def _try_float(s: str) -> float | None:
    """``float`` that returns ``None`` on parse error -- helper for filters."""
    try:
        return float(s)
    except ValueError:
        return None


def filter_bull_bear(example: dict[str, Any]) -> tuple[bool, str | None]:
    """Bull/bear analyst curation rules.

    Keep iff:
      * response length in [200, 1500] characters
      * response cites ≥2 specific numeric/indicator evidence items, where
        "evidence" matches :data:`_EVIDENCE_RE` (dollar values, percentages,
        common indicators, or ISO dates).
    """
    response = example.get("response") or ""
    if not response.strip():
        return False, Reject.EMPTY_RESPONSE
    if not (200 <= len(response) <= 1500):
        return False, Reject.LENGTH_OUT_OF_BAND

    matches = _EVIDENCE_RE.findall(response)
    if len(matches) < 2:
        return False, Reject.EVIDENCE_TOO_THIN
    return True, None


def filter_structured(example: dict[str, Any]) -> tuple[bool, str | None]:
    """Arbiter / regime-tagger / indicator-selector curation rules.

    These roles already pass through a Pydantic validator upstream
    (``chat_json`` rejects on schema failure and the tracker records the
    ``valid`` field). Curation simply enforces that flag.
    """
    ledger = example.get("ledger") or {}
    valid = ledger.get("valid", True)
    if not valid:
        return False, Reject.STRUCTURED_INVALID

    response = example.get("response") or ""
    if not response.strip():
        return False, Reject.EMPTY_RESPONSE
    # Defence-in-depth: confirm the response parses as JSON. Structured roles
    # only emit JSON; a non-JSON response means upstream `valid` was wrong.
    try:
        json.loads(response)
    except (json.JSONDecodeError, TypeError):
        return False, Reject.STRUCTURED_INVALID
    return True, None


def _has_crypto_term(text: str) -> bool:
    """Return True if ``text`` contains any crypto-specific contamination term.

    Case-insensitive substring match per spec Section A. This catches both
    pure crypto debates (all 29k quanta_schema.decisions rows reference crypto)
    and any stock debate that accidentally mentions crypto concepts (e.g. a
    debate citing "BTC correlation" for a macro analysis).
    """
    lower = text.lower()
    return any(term.lower() in lower for term in CRYPTO_TERMS)


ROLE_FILTERS = {
    "trading-reflector":          filter_reflector,
    "trading-bull":               filter_bull_bear,
    "trading-bear":               filter_bull_bear,
    "trading-arbiter":            filter_structured,
    "trading-regime-tagger":      filter_structured,
    "trading-indicator-selector": filter_structured,
}


# --------------------------------------------------------------------------- #
# Curation result + stats
# --------------------------------------------------------------------------- #

@dataclass
class RoleCurationResult:
    """Per-role outcome of one curate pass."""

    role: str
    accept_count: int = 0
    reject_count: int = 0
    test_set_count: int = 0
    reject_reasons: dict[str, int] = field(default_factory=dict)
    source_files: list[str] = field(default_factory=list)
    out_path: str | None = None
    test_set_path: str | None = None
    #: "ok" | "insufficient_data" | "error"
    status: str = "ok"

    @property
    def total(self) -> int:
        return self.accept_count + self.reject_count

    @property
    def accept_rate(self) -> float:
        if self.total == 0:
            return 0.0
        return self.accept_count / self.total

    def as_dict(self) -> dict[str, Any]:
        return {
            "status":         self.status,
            "track_id":       self.role,
            "accept_count":   self.accept_count,
            "reject_count":   self.reject_count,
            "test_set_count": self.test_set_count,
            "accept_rate":    round(self.accept_rate, 4),
            "reject_reasons": dict(sorted(self.reject_reasons.items())),
            "source_files":   list(self.source_files),
            "out_path":       self.out_path,
            "test_set_path":  self.test_set_path,
            "timestamp_utc":  dt.datetime.now(dt.UTC).isoformat(),
        }


# --------------------------------------------------------------------------- #
# State (last-curated-file per role) for idempotent re-runs
# --------------------------------------------------------------------------- #

def _load_state(root: Path) -> dict[str, dict[str, Any]]:
    """Read the curation state JSON; returns ``{}`` if missing/corrupt."""
    path = _state_path(root)
    if not path.exists():
        return {}
    try:
        with path.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data, dict):
            return data
    except (OSError, json.JSONDecodeError):
        pass
    return {}


def _save_state(root: Path, state: dict[str, dict[str, Any]]) -> None:
    """Persist the curation state atomically; never raises on disk error."""
    path = _state_path(root)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.partial")
        with tmp.open("w", encoding="utf-8") as fh:
            json.dump(state, fh, indent=2, sort_keys=True)
        os.replace(tmp, path)
    except OSError:
        pass


# --------------------------------------------------------------------------- #
# Iteration over raw JSONL inputs
# --------------------------------------------------------------------------- #

def _raw_files_for_role(root: Path, role: str, *, target_date: dt.date | None) -> list[Path]:
    """Return the raw JSONL files for ``role`` to consider this run.

    When ``target_date`` is set, only files matching ``<YYYYMMDD>.jsonl`` are
    returned; otherwise every JSONL in the role's raw dir is returned, sorted
    by filename so curation is deterministic.
    """
    raw_dir = root / "raw" / role
    if not raw_dir.is_dir():
        return []
    if target_date is not None:
        candidate = raw_dir / f"{target_date.strftime('%Y%m%d')}.jsonl"
        return [candidate] if candidate.exists() else []
    return sorted(p for p in raw_dir.glob("*.jsonl") if p.is_file())


def _iter_jsonl(path: Path) -> Iterable[dict[str, Any]]:
    """Yield decoded dicts from a JSONL file; skip broken lines silently."""
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            if not line.strip():
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue


# --------------------------------------------------------------------------- #
# Curation core
# --------------------------------------------------------------------------- #

def _to_hf_row(role: str, example: dict[str, Any]) -> dict[str, Any]:
    """Project a raw ingest example onto ModelForge's HF Arrow row schema.

    Mirrors the columns built by
    ``HuggingFaceDataCurator.curate`` (see
    ``apps/api/src/services/data_curator.py:208-216``):

        {category, source, dataset_name, instruction, response}

    ``category`` doubles as the per-role track_id, so a downstream weakness
    report can target a single role cleanly.
    """
    user_msg = example.get("user_message") or ""
    system_msg = example.get("system_message") or ""
    if system_msg:
        # Concatenate system + user so the trainer's single `text` column
        # carries both; the trainer template glues them with the tokenizer's
        # default chat template anyway.
        instruction = f"[SYSTEM]\n{system_msg}\n[USER]\n{user_msg}"
    else:
        instruction = user_msg
    return {
        "category":     role,
        "source":       "trading-bot",
        "dataset_name": role,
        "instruction":  instruction,
        "response":     example.get("response") or "",
    }


_PNL_PCT_RE = re.compile(r'"pnl_pct"\s*:\s*([+\-]?\d+(?:\.\d+)?)')
_EXIT_REASON_RE = re.compile(r'"exit_reason"\s*:\s*"([^"]+)"')
_EXIT_PRICE_RE = re.compile(r'"exit_price"\s*:\s*([+\-]?\d+(?:\.\d+)?|null)')
_ENTRY_PRICE_RE = re.compile(r'"entry_price"\s*:\s*([+\-]?\d+(?:\.\d+)?|null)')
_QTY_RE = re.compile(r'"(?:qty|quantity|shares)\s*"\s*:\s*([+\-]?\d+(?:\.\d+)?|null)')
_SYMBOL_RE = re.compile(r'"symbol"\s*:\s*"([^"]+)"')


def _build_eval_test_set_row(role: str, example: dict[str, Any]) -> dict[str, Any] | None:
    """Project a raw ingest example onto ModelForge's eval test-set JSONL row.

    The trading evals at ``apps/api/src/agents/evals/eval_*.py`` consume one
    JSONL record per held-out example. Required across the family:

      * ``prompt`` -- the same prompt the role saw at runtime
      * role-specific gold-truth fields (e.g. ``realized_pnl_usd`` for reflector)

    For ``trading-reflector`` we compute ``realized_pnl_usd`` from dollar-side
    fields only. The formula is ``(exit_price - entry_price) * qty``.  When
    those three fields are not all present (i.e. the ledger only carries a
    percent), we write ``realized_pnl_usd: null`` rather than fabricating a
    notional.  Null records are kept in the test set (the prompt + gold_response
    still train the prose quality) but are skipped by ``faithfulness_regex``
    eval (which checks dollar-sign citations).

    Returns ``None`` when the example carries no usable prompt (drop it).
    """
    user_msg = example.get("user_message") or ""
    system_msg = example.get("system_message") or ""
    if system_msg:
        prompt = f"[SYSTEM]\n{system_msg}\n[USER]\n{user_msg}"
    else:
        prompt = user_msg
    if not prompt.strip():
        return None

    row: dict[str, Any] = {
        "prompt":     prompt,
        "track_id":   role,
        # Carry response so eval LLM-as-judge fallback can compare against
        # the curator's ground-truth answer when no live runner is wired.
        "gold_response": example.get("response") or "",
    }
    # Reflector-specific enrichment from the trade_reviewer trade JSON.
    if role == "trading-reflector":
        # Attempt dollar-amount derivation: requires entry_price + exit_price + qty.
        # Only write realized_pnl_usd when all three are present and non-null.
        # NEVER fabricate a notional from pct alone.
        realized_pnl_usd: float | None = None
        ledger = example.get("ledger") or {}

        # Prefer structured ledger fields first (set by reflector_example()).
        entry_price_raw = ledger.get("entry_price")
        exit_price_raw = ledger.get("exit_price")
        qty_raw = ledger.get("qty") or ledger.get("quantity") or ledger.get("shares")

        # Fall back to regex extraction from the embedded user message JSON.
        if entry_price_raw is None:
            m_ep = _ENTRY_PRICE_RE.search(user_msg)
            if m_ep and m_ep.group(1) != "null":
                try:
                    entry_price_raw = float(m_ep.group(1))
                except ValueError:
                    pass
        if exit_price_raw is None:
            m_xp = _EXIT_PRICE_RE.search(user_msg)
            if m_xp and m_xp.group(1) != "null":
                try:
                    exit_price_raw = float(m_xp.group(1))
                except ValueError:
                    pass
        if qty_raw is None:
            m_qty = _QTY_RE.search(user_msg)
            if m_qty and m_qty.group(1) != "null":
                try:
                    qty_raw = float(m_qty.group(1))
                except ValueError:
                    pass

        if (entry_price_raw is not None and exit_price_raw is not None
                and qty_raw is not None):
            try:
                realized_pnl_usd = (float(exit_price_raw) - float(entry_price_raw)) * float(qty_raw)
            except (TypeError, ValueError):
                realized_pnl_usd = None

        # Always write the field (even as None) so downstream code can rely on
        # key presence rather than .get() with a default.
        row["realized_pnl_usd"] = realized_pnl_usd

        m_reason = _EXIT_REASON_RE.search(user_msg)
        if m_reason:
            row["exit_reason"] = m_reason.group(1)
        m_sym = _SYMBOL_RE.search(user_msg)
        if m_sym:
            row["symbol"] = m_sym.group(1)
        m_xp = _EXIT_PRICE_RE.search(user_msg)
        if m_xp and m_xp.group(1) != "null":
            try:
                row["exit_price"] = float(m_xp.group(1))
            except ValueError:
                pass

    # Bull/bear test-set rows: add opponent_strongest_point + prior_response
    # per Section B. These are populated from the debate JSONB when present
    # (e.g. from decisions bootstrap ETL rows). For llm-calls rows they will
    # typically be empty strings; the eval can handle that gracefully.
    elif role in ("trading-bull", "trading-bear"):
        ledger = example.get("ledger") or {}
        debate = ledger.get("debate") or {}
        # For bull: opponent is bear; for bear: opponent is bull.
        if role == "trading-bull":
            opponent_key = "bear_strongest_point"
            # Fallback: extract bear text from debate dict if structured
            if not debate.get(opponent_key):
                bear_raw = debate.get("bear") or ""
                if isinstance(bear_raw, dict):
                    bear_raw = bear_raw.get("text") or bear_raw.get("response") or ""
                # Take last sentence as "strongest point" heuristic.
                sentences = [s.strip() for s in str(bear_raw).split(". ") if s.strip()]
                debate[opponent_key] = sentences[-1] if sentences else ""
        else:  # trading-bear
            opponent_key = "bull_strongest_point"
            if not debate.get(opponent_key):
                bull_raw = debate.get("bull") or ""
                if isinstance(bull_raw, dict):
                    bull_raw = bull_raw.get("text") or bull_raw.get("response") or ""
                sentences = [s.strip() for s in str(bull_raw).split(". ") if s.strip()]
                debate[opponent_key] = sentences[-1] if sentences else ""

        row["opponent_strongest_point"] = str(debate.get(opponent_key) or "")
        # prior_response: the gold_response is the response that was already
        # logged (the "current champion equivalent" at curate time). Embed it so
        # evals don't need to run inference for this field at eval time (Section B).
        row["prior_response"] = example.get("response") or ""
        row["ticker"] = example.get("ticker") or ""
        row["ts"] = example.get("ts") or ""

    # Regime-tagger test-set rows: add baseline_output per Section B.
    # baseline_output.regime is derived from the response JSON (which should
    # contain {"regime": "<label>"}). If the response is not valid JSON or
    # doesn't carry a regime, fall back to the strategy→regime mapping.
    elif role == "trading-regime-tagger":
        baseline_regime: str | None = None
        response_text = example.get("response") or ""
        try:
            parsed = json.loads(response_text)
            if isinstance(parsed, dict) and "regime" in parsed:
                baseline_regime = str(parsed["regime"])
        except (json.JSONDecodeError, TypeError):
            pass
        if baseline_regime is None:
            # Fall back: extract strategy from user_message and map.
            strategy_m = re.search(r"(?i)strategy\s*[:=]\s*(\S+)", user_msg)
            if strategy_m:
                strategy_val = strategy_m.group(1).strip().lower()
                baseline_regime = _STRATEGY_TO_REGIME_CURATE.get(strategy_val)
        row["baseline_output"] = {"regime": baseline_regime or "unknown"}
        row["symbol"] = example.get("ticker") or ""
        row["date"] = (example.get("ts") or "")[:10]

    # Indicator-selector test-set rows: add baseline_output per Section B.
    # baseline_output.indicators is deterministic from REGIME_BASELINE_INDICATORS.
    elif role == "trading-indicator-selector":
        regime_val: str = ""
        response_text = example.get("response") or ""
        try:
            parsed = json.loads(response_text)
            if isinstance(parsed, dict) and "regime" in parsed:
                regime_val = str(parsed["regime"])
        except (json.JSONDecodeError, TypeError):
            pass
        baseline_inds = REGIME_BASELINE_INDICATORS.get(regime_val, [])
        row["baseline_output"] = {"indicators": baseline_inds}
        row["symbol"] = example.get("ticker") or ""
        row["regime"] = regime_val
        row["date"] = (example.get("ts") or "")[:10]

    return row


def _write_eval_test_set(
    *,
    role: str,
    target_dir: Path,
    raw_examples: list[dict[str, Any]],
) -> Path | None:
    """Write a JSONL test set sibling to the HF Arrow ``curated/`` dir.

    The mf-api workflow's ``eval_set_path`` config originally pointed at the
    curated dir, but the trading eval modules' ``load_test_set`` opens the
    path as a JSONL file -- pointing at a directory throws ``IsADirectoryError``
    which is silently swallowed into an empty record list, which then
    short-circuits the eval to all-zero scores. This file is the JSONL the
    workflow's ``eval_set_path`` should point at:
    ``/app/data/dgx-train/datasets/<role>/test_set.jsonl`` (host: same under
    ``~/.dgx-train/...``).
    """
    rows = [r for r in (_build_eval_test_set_row(role, ex) for ex in raw_examples) if r]
    if not rows:
        return None

    target_dir.mkdir(parents=True, exist_ok=True)
    out_path = target_dir / "test_set.jsonl"
    tmp = out_path.with_suffix(".jsonl.partial")
    with tmp.open("w", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r, sort_keys=True) + "\n")
    os.replace(tmp, out_path)
    return out_path


def _write_hf_dataset(
    *,
    role: str,
    generation: int,
    out_dir: Path,
    rows: list[dict[str, Any]],
    weakness_report: str,
    max_samples: int,
) -> Path:
    """Write the HF Arrow shard + ``mf_meta.json`` sidecar.

    Args:
        role: track_id of the role being curated.
        generation: 0-indexed evolution generation -- starts at 0 for first
            curate. Stage 3 (when it lands) will increment on each evolve.
        out_dir: ``<root>/datasets/<role>/curated``. Created on demand.
        rows: HF-schema-aligned dicts ready to ``Dataset.from_list``.
        weakness_report: free-text blurb persisted in ``mf_meta.json``; for
            the first pass it's just the curator's run summary.
        max_samples: stored in ``mf_meta.json``; informational only.

    Returns the directory the dataset was written to.

    Raises:
        ImportError: if ``datasets`` is not installed. The caller catches and
        records the error in the fail-soft log.
    """
    from datasets import Dataset  # lazy -- doc the dep in HANDOFF.md

    out_dir.mkdir(parents=True, exist_ok=True)
    ds = Dataset.from_list(rows)
    ds.save_to_disk(str(out_dir))

    meta_path = out_dir / "mf_meta.json"
    meta = {
        # ModelForge's own data_curator writes num_samples + categories +
        # sources + weakness_report + max_samples + generation. We additionally
        # write track_id + source_split + timestamp_utc per the integration
        # plan's "curate" stage contract, all in the same file so a single
        # `cat mf_meta.json` is the operator's source of truth.
        "track_id":         role,
        "generation":       int(generation),
        "source_split":     "train",
        "sample_count":     int(len(rows)),
        "timestamp_utc":    dt.datetime.now(dt.UTC).isoformat(),
        "num_samples":      int(len(rows)),
        "categories":       [role],
        "sources":          ["trading-bot"],
        "weakness_report":  weakness_report[:500],
        "max_samples":      int(max_samples),
    }
    tmp_meta = meta_path.with_suffix(".json.partial")
    with tmp_meta.open("w", encoding="utf-8") as fh:
        json.dump(meta, fh, indent=2, sort_keys=True)
    os.replace(tmp_meta, meta_path)
    return out_dir


def _write_curator_result_json(role_dir: Path, result: "RoleCurationResult") -> None:
    """Write ``curator_result.json`` next to the Arrow shard (or where it would be).

    Matches the schema from spec Section D. Never raises — disk errors are
    logged as warnings so the caller's return path is unaffected.
    """
    try:
        role_dir.mkdir(parents=True, exist_ok=True)
        out = role_dir / "curator_result.json"
        tmp = out.with_suffix(".json.partial")
        with tmp.open("w", encoding="utf-8") as fh:
            json.dump(result.as_dict(), fh, indent=2, sort_keys=True)
        os.replace(tmp, out)
    except OSError as exc:
        logger.warning("[curate] failed to write curator_result.json for role=%s: %s", result.role, exc)


def curate_role(
    role: str,
    *,
    raw_files: list[Path],
    out_root: Path,
    generation: int = 0,
) -> RoleCurationResult:
    """Run the per-role filter over ``raw_files`` and emit one HF shard.

    Returns the :class:`RoleCurationResult` even on partial failure; on
    ``ImportError`` for ``datasets`` we record the reject reason in stats
    instead of crashing so the cron-wide summary still surfaces.
    """
    filt = ROLE_FILTERS.get(role)
    if filt is None:
        # Defensive — shouldn't happen because ROLE_FILTERS covers ALL_ROLES.
        return RoleCurationResult(role=role)

    result = RoleCurationResult(role=role, source_files=[str(p) for p in raw_files])
    kept_rows: list[dict[str, Any]] = []
    kept_raw: list[dict[str, Any]] = []
    # test_set: temporal/ID split applied after the full accept pass
    test_set: list[dict[str, Any]] = []

    apply_crypto_blocklist = role in CRYPTO_BLOCKLIST_ROLES

    for path in raw_files:
        for example in _iter_jsonl(path):
            # --- Crypto-term contamination check (bull/bear/arbiter only) ---
            if apply_crypto_blocklist:
                response_text = example.get("response") or ""
                if _has_crypto_term(response_text):
                    result.reject_count += 1
                    key = Reject.CRYPTO_TERM_CONTAMINATION
                    result.reject_reasons[key] = result.reject_reasons.get(key, 0) + 1
                    continue

            ok, code = filt(example)
            if ok:
                result.accept_count += 1
                kept_rows.append(_to_hf_row(role, example))
                kept_raw.append(example)
            else:
                result.reject_count += 1
                key = code or "unknown"
                result.reject_reasons[key] = result.reject_reasons.get(key, 0) + 1

    # --- N_MIN gate (Section D) ---
    role_dir = out_root / "datasets" / role
    n_min_train = N_MIN_TRAIN.get(role, 0)
    n_min_test = N_MIN_TEST.get(role, 0)

    # We use 20% split approximation for test size estimation here.
    # The actual test set is computed from kept_raw below.
    # For the gate: check accept_count against N_MIN_TRAIN.
    if result.accept_count < n_min_train:
        result.reject_reasons["below_min_records_gate"] = result.accept_count
        result.status = "insufficient_data"
        logger.error(
            "[curate] role=%s INSUFFICIENT DATA: accept_count=%d < N_MIN=%d. "
            "No shard written. Evolution will be blocked.",
            role, result.accept_count, n_min_train,
        )
        # Write curator_result.json alongside where the shard WOULD be so
        # callers can programmatically inspect the failure without parsing logs.
        _write_curator_result_json(role_dir, result)
        return result  # out_path remains None — callers check this

    if not kept_rows:
        # Should not reach here after the N_MIN check above, but keep as safety.
        return result

    out_dir = role_dir / "curated"
    try:
        _write_hf_dataset(
            role=role,
            generation=generation,
            out_dir=out_dir,
            rows=kept_rows,
            weakness_report=f"trading-bot curate role={role} accept={result.accept_count}",
            max_samples=len(kept_rows),
        )
        result.out_path = str(out_dir)
        # Sibling test-set JSONL the eval scorers load via load_test_set().
        # Best-effort — failure here does not invalidate the training shard.
        try:
            test_path = _write_eval_test_set(
                role=role,
                target_dir=role_dir,
                raw_examples=kept_raw,
            )
            if test_path is not None:
                result.test_set_path = str(test_path)
                result.test_set_count = len(kept_raw)
                logger.info("[curate] wrote eval test set %s (%d rows)", test_path, len(kept_raw))

            # --- N_MIN test gate (after test set is known) ---
            if result.test_set_count < n_min_test:
                logger.error(
                    "[curate] role=%s test_set_count=%d < N_MIN_TEST=%d. "
                    "Shard written but flagged insufficient for eval.",
                    role, result.test_set_count, n_min_test,
                )
                result.status = "insufficient_data"
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning("[curate] eval test set write failed for role=%s: %s", role, exc)
    except ImportError as exc:
        result.reject_reasons["datasets_import_failed"] = result.accept_count
        result.reject_count += result.accept_count
        result.accept_count = 0
        result.status = "error"
        logger.warning("datasets library not importable for role=%s: %s", role, exc)

    # Always write curator_result.json alongside the shard (or error state).
    _write_curator_result_json(role_dir, result)
    return result


# --------------------------------------------------------------------------- #
# Slack notifier shim — best-effort, NEVER blocks the cron
# --------------------------------------------------------------------------- #

def _notify(msg: str) -> None:
    """Try the bot's notifier; fall back to stdout. Never raises."""
    # Prefer the project's own notify shim if importable so alerts land in the
    # operator's existing Slack channel rather than a new one.
    for module_path in (
        "shark.notify",
        "stocks.shark.notify",
        "user_data.dashboard.notify",
    ):
        try:
            module = __import__(module_path, fromlist=["send"])
        except Exception:
            continue
        send = getattr(module, "send", None) or getattr(module, "notify", None)
        if callable(send):
            try:
                send(msg)
                return
            except Exception:  # noqa: BLE001 - we genuinely don't care why
                continue
    print(f"[modelforge-curate ALERT] {msg}", file=sys.stderr)


def _maybe_alert(result: RoleCurationResult, *, lo: float, hi: float) -> None:
    """Fire a notifier alert when accept_rate is out of band."""
    if result.total == 0:
        return
    if result.accept_rate < lo or result.accept_rate > hi:
        _notify(
            f"role={result.role} accept_rate={result.accept_rate:.1%} "
            f"out of band [{lo:.0%}, {hi:.0%}] (accept={result.accept_count} "
            f"reject={result.reject_count})"
        )


# --------------------------------------------------------------------------- #
# Top-level orchestration
# --------------------------------------------------------------------------- #

@dataclass
class CurateStats:
    """Aggregate stats for one cron invocation."""

    target_date: dt.date | None
    by_role: dict[str, RoleCurationResult] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)

    def render(self) -> str:
        date_tag = f"date={self.target_date.isoformat()}" if self.target_date else "date=*"
        parts = [f"modelforge-curate {date_tag}"]
        for role in ALL_ROLES:
            r = self.by_role.get(role)
            if r is None:
                continue
            parts.append(f"{role}={r.accept_count}/{r.total}({r.accept_rate:.0%})")
        if self.errors:
            parts.append(f"errors={len(self.errors)}")
        return " ".join(parts)


def curate(
    target_date: dt.date | None,
    *,
    root: Path,
    accept_rate_lo: float = ACCEPT_RATE_LO_DEFAULT,
    accept_rate_hi: float = ACCEPT_RATE_HI_DEFAULT,
    role_filter: str | None = None,
) -> CurateStats:
    """Run a full curate pass; idempotent thanks to the state-file gate.

    Args:
        target_date: if set, only files matching that day are considered;
            otherwise we curate every raw file we haven't seen before.
        root: ``~/.dgx-train`` root.
        role_filter: when set, only curate this track_id. Used by
            BuildTradingDataset action for per-track Sunday workflows.
        accept_rate_lo/hi: out-of-band thresholds for the Slack notifier.
    """
    state = _load_state(root)
    stats = CurateStats(target_date=target_date)

    active_roles = (role_filter,) if role_filter else ALL_ROLES

    for role in active_roles:
        raw_files = _raw_files_for_role(root, role, target_date=target_date)
        already_seen = set(state.get(role, {}).get("source_files", []))
        new_files = [p for p in raw_files if str(p) not in already_seen]

        # If `new_files` is empty (either the date was already curated OR no
        # raw files exist for this role yet), emit an authoritative
        # `curator_result.json` so downstream readers (e.g. the
        # ``dataset.build_trading`` action) see a fresh `insufficient_data`
        # signal instead of a stale prior result. Without this, an action
        # that runs curate and finds zero new files would read a leftover
        # success-or-failure file from a previous run and act on stale data.
        if not new_files:
            empty_result = RoleCurationResult(
                role=role,
                accept_count=0,
                reject_count=0,
                test_set_count=0,
                source_files=[],
                out_path=None,
                test_set_path=None,
                status="insufficient_data",
                reject_reasons={"no_new_raw_files": 0},
            )
            stats.by_role[role] = empty_result
            role_dir = root / "datasets" / role
            try:
                role_dir.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                logger.warning("[curate] mkdir failed for %s: %s", role_dir, exc)
            _write_curator_result_json(role_dir, empty_result)
            continue

        try:
            result = curate_role(role, raw_files=new_files, out_root=root)
        except Exception as exc:  # pragma: no cover
            stats.errors.append(f"{role}: {exc}")
            continue

        stats.by_role[role] = result

        if result.accept_count > 0:
            # Update state so a re-run skips these source files.
            state.setdefault(role, {})
            seen = set(state[role].get("source_files", []))
            seen.update(str(p) for p in new_files)
            state[role]["source_files"] = sorted(seen)
            state[role]["last_curate_utc"] = dt.datetime.now(dt.UTC).isoformat()

        # Persist per-day stats next to the state file so the operator can
        # cat the day's accept/reject breakdown at a glance.
        _write_role_day_stats(root, role, target_date, result)

        _maybe_alert(result, lo=accept_rate_lo, hi=accept_rate_hi)

    _save_state(root, state)
    return stats


def _write_role_day_stats(
    root: Path,
    role: str,
    target_date: dt.date | None,
    result: RoleCurationResult,
) -> None:
    """Drop a small ``curate/<role>_<date>.json`` for human inspection."""
    date_tag = target_date.isoformat() if target_date else dt.date.today().isoformat()
    out = root / "curate" / f"{role}_{date_tag}.json"
    try:
        out.parent.mkdir(parents=True, exist_ok=True)
        tmp = out.with_suffix(".json.partial")
        with tmp.open("w", encoding="utf-8") as fh:
            json.dump(result.as_dict(), fh, indent=2, sort_keys=True)
        os.replace(tmp, out)
    except OSError:
        pass


# --------------------------------------------------------------------------- #
# Fail-soft logging
# --------------------------------------------------------------------------- #

def _log_error(msg: str) -> None:
    """Append a timestamped line to the curate log; never raises."""
    try:
        path = _curate_log_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(f"{dt.datetime.now(dt.UTC).isoformat()} {msg}\n")
    except OSError:
        pass


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def main(argv: list[str] | None = None) -> int:
    """CLI entry point; fail-soft (always exits 0).

    Default behaviour: curate everything new since the last state-file checkpoint.
    With a date arg: curate only the matching day's raw file (still respects
    the state file for idempotency).
    """
    parser = argparse.ArgumentParser(description="ModelForge Stage 2 curate")
    parser.add_argument("date", nargs="?", default=None, help="YYYY-MM-DD; defaults to every-new")
    parser.add_argument("--root", default=None, help="Override ~/.dgx-train")
    parser.add_argument("--accept-lo", type=float, default=ACCEPT_RATE_LO_DEFAULT)
    parser.add_argument("--accept-hi", type=float, default=ACCEPT_RATE_HI_DEFAULT)
    parser.add_argument("--quiet", action="store_true")
    # --role-filter restricts curation to a single trading track (used by
    # BuildTradingDataset action to run per-track Sunday workflows).
    parser.add_argument(
        "--role-filter", default=None,
        metavar="TRACK_ID",
        help="Only curate this track_id (e.g. trading-arbiter). Default: curate all roles.",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")

    target: dt.date | None = None
    if args.date and args.date.lower() != "all":
        try:
            target = dt.date.fromisoformat(args.date)
        except ValueError as exc:
            _log_error(f"bad date arg: {exc}")
            print(f"modelforge-curate ERROR bad-date {exc}", file=sys.stderr)
            return 0

    root = Path(args.root) if args.root else _dgx_train_root()
    role_filter = args.role_filter or None

    if role_filter is not None and role_filter not in ALL_ROLES:
        print(
            f"modelforge-curate ERROR unknown role_filter={role_filter!r}. "
            f"Valid: {sorted(ALL_ROLES)}",
            file=sys.stderr,
        )
        return 1

    try:
        stats = curate(
            target,
            root=root,
            accept_rate_lo=args.accept_lo,
            accept_rate_hi=args.accept_hi,
            role_filter=role_filter,
        )
    except Exception:  # pragma: no cover - defensive top-level catch
        _log_error("curate crashed:\n" + traceback.format_exc())
        return 0

    for err in stats.errors:
        _log_error(err)

    if not args.quiet:
        print(stats.render())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
