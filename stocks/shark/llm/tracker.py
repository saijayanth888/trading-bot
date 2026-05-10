"""
LLM call tracker — measures latency, token usage, and counterfactual API cost
for every shark agent call routed through `shark.llm.client.chat_json`.

Two storage layers:
  1. In-memory ring (last 1000 calls) — fast access for the same-process
     dashboard / CLI.
  2. Append-only JSONL log at `stocks/memory/llm-calls.jsonl` — persists
     across restarts so the dashboard's /api/ops/llm-stats endpoint can
     report a rolling window without sharing process state with shark.

Cost model (counterfactual): what would each call have cost on Anthropic
Claude Sonnet 4.6? Pricing is $3/M prompt + $15/M completion as of
2026-05. The "saved" number is therefore an estimate of the spend you
WOULD have had if the operator hadn't migrated to local Ollama. Update
`SONNET_PRICING_USD_PER_M` if Anthropic changes prices.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from collections import deque
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Counterfactual pricing — what the call WOULD have cost on Sonnet 4.6.
SONNET_PRICING_USD_PER_M = {"input": 3.0, "output": 15.0}

# Persistent log location. Tests set SHARK_TRACKER_LOG to /tmp/<unique>
# so they don't pollute the production dashboard.
def _resolve_log_path() -> Path:
    override = os.environ.get("SHARK_TRACKER_LOG", "").strip()
    if override:
        return Path(override)
    return Path(__file__).resolve().parents[2] / "memory" / "llm-calls.jsonl"


_LOG_PATH = _resolve_log_path()

_RING_MAX = 1000
_LOCK = threading.Lock()


@dataclass
class LLMCallRecord:
    agent: str
    model: str
    provider: str
    tier: str = "deep"
    role: str = "default"
    latency_seconds: float = 0.0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    timestamp: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens

    @property
    def estimated_api_cost_usd(self) -> float:
        """Counterfactual: what this call would cost on Sonnet 4.6."""
        return (
            self.prompt_tokens * SONNET_PRICING_USD_PER_M["input"] / 1_000_000
            + self.completion_tokens * SONNET_PRICING_USD_PER_M["output"] / 1_000_000
        )


class LLMTracker:
    """Singleton tracker. Same-process consumers go through the in-memory
    ring; cross-process consumers (the dashboard) read the JSONL log."""

    def __init__(self) -> None:
        self.calls: deque[LLMCallRecord] = deque(maxlen=_RING_MAX)
        self._log_disabled: bool = False

    def record(
        self,
        agent: str,
        model: str,
        provider: str,
        latency_seconds: float,
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
        tier: str = "deep",
        role: str = "default",
    ) -> LLMCallRecord:
        rec = LLMCallRecord(
            agent=agent,
            model=model,
            provider=provider,
            tier=tier,
            role=role,
            latency_seconds=round(float(latency_seconds), 3),
            prompt_tokens=int(prompt_tokens),
            completion_tokens=int(completion_tokens),
        )
        with _LOCK:
            self.calls.append(rec)
        self._append_jsonl(rec)
        logger.info(
            "LLM call agent=%s model=%s tier=%s latency=%.1fs tok=%d/%d saved=$%.4f",
            rec.agent, rec.model, rec.tier, rec.latency_seconds,
            rec.prompt_tokens, rec.completion_tokens,
            rec.estimated_api_cost_usd,
        )
        return rec

    def _append_jsonl(self, rec: LLMCallRecord) -> None:
        if self._log_disabled:
            return
        try:
            _LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
            with _LOG_PATH.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(asdict(rec), default=str) + "\n")
        except OSError as exc:  # pragma: no cover
            logger.warning("LLM tracker JSONL write failed: %s — disabling further writes", exc)
            self._log_disabled = True

    def get_session_stats(self) -> dict[str, Any]:
        """In-process session stats. Cross-process callers should read the JSONL."""
        with _LOCK:
            calls = list(self.calls)
        return _summarise(calls)


_singleton: LLMTracker | None = None


def get_tracker() -> LLMTracker:
    global _singleton
    if _singleton is None:
        _singleton = LLMTracker()
    return _singleton


# ---------------------------------------------------------------------------
# Cross-process: read JSONL and summarise — used by /api/ops/llm-stats
# ---------------------------------------------------------------------------


def read_log_window(
    *,
    log_path: Path | None = None,
    since_seconds: int = 86400,
) -> list[LLMCallRecord]:
    """Read the JSONL log filtered to calls within the last N seconds."""
    path = log_path or _LOG_PATH
    if not path.is_file():
        return []
    cutoff = datetime.now(timezone.utc).timestamp() - since_seconds
    out: list[LLMCallRecord] = []
    try:
        with path.open("r", encoding="utf-8") as fh:
            for line in fh:
                if not line.strip():
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                ts_str = obj.get("timestamp") or ""
                try:
                    ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00")).timestamp()
                except (ValueError, TypeError):
                    continue
                if ts < cutoff:
                    continue
                out.append(LLMCallRecord(
                    agent=obj.get("agent", "unknown"),
                    model=obj.get("model", "?"),
                    provider=obj.get("provider", "?"),
                    tier=obj.get("tier", "deep"),
                    role=obj.get("role", "default"),
                    latency_seconds=float(obj.get("latency_seconds") or 0),
                    prompt_tokens=int(obj.get("prompt_tokens") or 0),
                    completion_tokens=int(obj.get("completion_tokens") or 0),
                    timestamp=ts_str,
                ))
    except OSError:
        return []
    return out


def summarise_window(
    *, log_path: Path | None = None, since_seconds: int = 86400,
) -> dict[str, Any]:
    return _summarise(read_log_window(log_path=log_path, since_seconds=since_seconds))


def _summarise(calls: list[LLMCallRecord]) -> dict[str, Any]:
    if not calls:
        return {
            "total_calls": 0,
            "total_latency_seconds": 0.0,
            "avg_latency_seconds": 0.0,
            "total_prompt_tokens": 0,
            "total_completion_tokens": 0,
            "total_api_cost_saved_usd": 0.0,
            "by_model": {},
            "by_agent": {},
            "by_tier": {"fast": 0, "deep": 0},
        }
    by_agent: dict[str, list[LLMCallRecord]] = {}
    by_model: dict[str, int] = {}
    by_tier = {"fast": 0, "deep": 0}
    saved = 0.0
    total_latency = 0.0
    p_toks = 0
    c_toks = 0
    for rec in calls:
        by_agent.setdefault(rec.agent, []).append(rec)
        by_model[rec.model] = by_model.get(rec.model, 0) + 1
        if rec.tier in by_tier:
            by_tier[rec.tier] += 1
        saved += rec.estimated_api_cost_usd
        total_latency += rec.latency_seconds
        p_toks += rec.prompt_tokens
        c_toks += rec.completion_tokens

    agent_summary = {}
    for agent, recs in by_agent.items():
        avg_lat = sum(r.latency_seconds for r in recs) / len(recs)
        agent_summary[agent] = {
            "calls": len(recs),
            "avg_latency_seconds": round(avg_lat, 2),
            "max_latency_seconds": round(max(r.latency_seconds for r in recs), 2),
            "total_tokens": sum(r.total_tokens for r in recs),
            "models": sorted({r.model for r in recs}),
        }

    return {
        "total_calls": len(calls),
        "total_latency_seconds": round(total_latency, 2),
        "avg_latency_seconds": round(total_latency / len(calls), 2),
        "total_prompt_tokens": p_toks,
        "total_completion_tokens": c_toks,
        "total_api_cost_saved_usd": round(saved, 4),
        "by_model": dict(sorted(by_model.items(), key=lambda kv: -kv[1])),
        "by_agent": agent_summary,
        "by_tier": by_tier,
    }
