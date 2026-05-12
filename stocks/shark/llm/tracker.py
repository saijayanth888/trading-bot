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

import errno
import fcntl
import json
import logging
import os
import threading
from collections import deque
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from shark.llm.redaction import redact, redact_messages

logger = logging.getLogger(__name__)


def _full_text_enabled() -> bool:
    """`SHARK_LLM_LOG_FULL_TEXT=1` opts in to persisting prompt/response text.

    Read on every call (not cached) so tests + the operator can toggle the
    flag at runtime without re-importing.
    """
    return os.environ.get("SHARK_LLM_LOG_FULL_TEXT", "0").strip().lower() in (
        "1", "true", "yes", "on",
    )

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

    # Optional full-text payload (only persisted when SHARK_LLM_LOG_FULL_TEXT=1)
    # — all fields default to None so the in-memory ring stays compact for the
    # 99% of consumers (dashboard /api/ops/llm-stats) that only need metadata.
    prompt: str | None = None
    system_message: str | None = None
    response_text: str | None = None
    messages: list[dict] | None = None
    redacted_count: int | None = None

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
        *,
        # Optional full-text payload — only persisted when
        # SHARK_LLM_LOG_FULL_TEXT=1. Callers pass these unconditionally;
        # the flag check happens inside so caller code stays uniform.
        system_message: str | None = None,
        user_message: str | None = None,
        response_text: str | None = None,
        messages: list[dict] | None = None,
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

        # Full-text fields are gated AND redacted before they touch the
        # record so the in-memory ring also never holds raw secrets.
        if _full_text_enabled():
            total_redactions = 0

            if system_message is not None:
                rec.system_message, n = redact(system_message)
                total_redactions += n
            if user_message is not None:
                rec.prompt, n = redact(user_message)
                total_redactions += n
            if response_text is not None:
                rec.response_text, n = redact(response_text)
                total_redactions += n
            if messages is not None:
                rec.messages, n = redact_messages(messages)
                total_redactions += n

            rec.redacted_count = total_redactions

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
        """Append a single record as one JSON line.

        Concurrency model
        -----------------
        Multiple shark crons can be writing this file at the same time
        (pre-market + midday + sentiment refresh overlap). We:

          1. Build the full JSON payload + trailing newline in memory.
          2. Hold an fcntl LOCK_EX on the open file descriptor for the
             write — this serialises writers within and across processes
             so two records can't interleave.
          3. Seek to end (O_APPEND would also work; the explicit seek is
             belt-and-braces in case the OS doesn't honour O_APPEND
             atomically for >PIPE_BUF writes).
          4. fsync so a crash mid-cron doesn't leave us with kernel-
             buffered-but-not-on-disk records on the next read.

        Falling back to disabled on OSError keeps the trading loop alive
        if the disk fills up — the in-memory ring continues to work.
        """
        if self._log_disabled:
            return
        # asdict serialises nested dataclasses + dicts fine; None-valued
        # optional fields are kept so the schema stays stable (consumers
        # can rely on key presence).
        payload = json.dumps(asdict(rec), default=str) + "\n"
        try:
            _LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(
                str(_LOG_PATH),
                os.O_WRONLY | os.O_APPEND | os.O_CREAT,
                0o644,
            )
            try:
                # Block until we own the file. fcntl flock is advisory
                # but every writer in this codebase routes through this
                # function so the advisory contract holds.
                fcntl.flock(fd, fcntl.LOCK_EX)
                try:
                    os.lseek(fd, 0, os.SEEK_END)
                    data = payload.encode("utf-8")
                    written = 0
                    while written < len(data):
                        n = os.write(fd, data[written:])
                        if n <= 0:
                            raise OSError(errno.EIO, "short write to llm-calls.jsonl")
                        written += n
                    os.fsync(fd)
                finally:
                    fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)
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
                    # Optional full-text fields — only present in lines
                    # written while SHARK_LLM_LOG_FULL_TEXT was set. Older
                    # lines simply leave these as None.
                    prompt=obj.get("prompt"),
                    system_message=obj.get("system_message"),
                    response_text=obj.get("response_text"),
                    messages=obj.get("messages"),
                    redacted_count=obj.get("redacted_count"),
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
