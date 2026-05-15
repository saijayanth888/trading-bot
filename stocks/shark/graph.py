"""
LangGraph-style two-tier debate graph — async DAG with parallel candidate fan-out.

Architecture:

    [market]  [sentiment]  [news]  [fundamentals]   ← grunts (8b), parallel
        \\        |          |          /
         \\_______|__________|_________/
                       │
                  Msg Clear  (drop working notes, inject "Continue")
                       │
                  ┌────┴────┐
                  │   bull  │ ← grunt (8b)
                  └────┬────┘
                       │
                  ┌────┴────┐
                  │   bear  │ ← grunt (8b)
                  └────┬────┘
                       │
              Research Manager   ← JUDGE (70b)
                       │
                    Trader       ← grunt (8b) — produces TraderProposal
                       │
              ┌────────┼────────┐
              │        │        │     ← critics (8b), round-robin
        conservative neutral aggressive
              └────────┼────────┘
                       │
              Portfolio Manager  ← JUDGE (70b) — sees only TraderProposal + critic verdicts
                       │
                      END

Each candidate runs the full graph independently. Multiple candidates fan out
via asyncio.gather() with a Semaphore — Ollama on the DGX has the headroom to
keep both 8b and 70b resident concurrently (~46 GB / 128 GB).

Why minimal-async-DAG instead of langgraph: the dep is heavy (~25 MB + LangChain),
the existing codebase already speaks dicts everywhere, and our DAG is fixed
(no conditional edges except the Msg Clear placeholder). A 200-line custom
runner is easier to reason about than the LangGraph machinery.

Operator note: requires `ollama pull hermes3:70b` (~40 GB Q4) before live use.
The 8b model is already pulled by the existing crypto sentiment pipeline.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Model-tier config
# ---------------------------------------------------------------------------

_MODEL_TIERS_PATH = Path(__file__).parent / "model_tiers.json"

# Cached on first read so we don't hit the disk per node per candidate.
_MODEL_TIERS_CACHE: dict[str, str] | None = None


def _load_model_tiers() -> dict[str, str]:
    """Load the per-node model assignment from model_tiers.json.

    Falls back to a hardcoded default if the file is missing or unreadable —
    we'd rather degrade to 8b-everywhere than crash the trade pipeline.
    """
    global _MODEL_TIERS_CACHE
    if _MODEL_TIERS_CACHE is not None:
        return _MODEL_TIERS_CACHE

    default: dict[str, str] = {
        "market_analyst": "hermes3:8b",
        "sentiment_analyst": "hermes3:8b",
        "news_analyst": "hermes3:8b",
        "fundamentals_analyst": "hermes3:8b",
        "bull": "hermes3:8b",
        "bear": "hermes3:8b",
        "research_manager": "hermes3:70b",
        "trader": "hermes3:8b",
        "conservative_critic": "hermes3:8b",
        "aggressive_critic": "hermes3:8b",
        "neutral_critic": "hermes3:8b",
        "portfolio_manager": "hermes3:70b",
    }
    try:
        if _MODEL_TIERS_PATH.is_file():
            raw = json.loads(_MODEL_TIERS_PATH.read_text())
            # Strip metadata keys (anything starting with `_`) and keep only known nodes.
            loaded = {k: v for k, v in raw.items() if not k.startswith("_")}
            default.update(loaded)
    except Exception as exc:
        logger.warning("model_tiers.json unreadable (%s) — using defaults", exc)

    _MODEL_TIERS_CACHE = default
    return default


def model_for_node(node_name: str) -> str:
    """Return the Ollama model name configured for *node_name*."""
    tiers = _load_model_tiers()
    return tiers.get(node_name, "hermes3:8b")


# ---------------------------------------------------------------------------
# LLM call shim — uses chat_structured from sibling branch when available,
# else falls back to chat_json. Tests inject their own mock via patch.
# ---------------------------------------------------------------------------

def _resolve_llm_call() -> Callable[..., Any]:
    """Pick the best available LLM call helper.

    Order of preference:
      1. ``shark.llm.structured.chat_structured`` — sibling branch
         stage/9-pydantic-schemas (tighter pydantic-validated output).
      2. ``shark.llm.client.chat_json`` — current main, JSON-string output.

    Returning a callable rather than the bare module lets tests patch
    ``shark.shark.graph._resolve_llm_call`` if they want full control.
    """
    try:
        from shark.llm.structured import chat_structured  # type: ignore
        return chat_structured
    except ImportError:
        from shark.llm.client import chat_json
        return chat_json


def _invoke_llm(
    *,
    system_prompt: str,
    user_message: str,
    model: str,
    max_tokens: int,
    temperature: float,
    role: str,
    agent: str,
) -> str:
    """Call the LLM and return a content string.

    Pins the model by overriding the env-var the failover client reads. We
    use SHARK_<ROLE>_LLM_MODEL for the call window so other concurrent calls
    aren't disturbed (the tier resolver already reads role-specific vars).
    """
    call = _resolve_llm_call()

    # Pin the model for this single call by setting the role-scoped env var.
    # We restore the old value on the way out so concurrent callers in
    # different roles aren't affected.
    env_key = f"SHARK_{role.upper()}_LLM_MODEL"
    saved = os.environ.get(env_key)
    os.environ[env_key] = model
    try:
        result = call(
            system_prompt=system_prompt,
            user_message=user_message,
            max_tokens=max_tokens,
            temperature=temperature,
            role=role,
            tier="deep" if "70b" in model else "fast",
            agent=agent,
        )
    finally:
        if saved is None:
            os.environ.pop(env_key, None)
        else:
            os.environ[env_key] = saved

    # chat_json returns (content, usage, model); chat_structured may return
    # a pydantic instance or a tuple. Normalise to a string.
    if isinstance(result, tuple):
        return str(result[0]) if result else ""
    if hasattr(result, "model_dump_json"):
        return result.model_dump_json()
    return str(result)


# ---------------------------------------------------------------------------
# State: shared dict carried through the DAG, with named slots per agent
# ---------------------------------------------------------------------------

@dataclass
class GraphState:
    """Shared state passed between nodes.

    Inspired by TradingAgents' AgentState — every analyst gets a named slot
    so downstream nodes know exactly where to find each input. The
    ``working_messages`` field is the scratchpad the Msg Clear placeholder
    drops between analyst phases.
    """

    symbol: str
    market_data: dict[str, Any] = field(default_factory=dict)
    perplexity_intel: dict[str, Any] = field(default_factory=dict)
    risk_check: dict[str, Any] = field(default_factory=dict)

    # Analyst outputs (populated by grunt nodes)
    market_report: dict[str, Any] = field(default_factory=dict)
    sentiment_report: dict[str, Any] = field(default_factory=dict)
    news_report: dict[str, Any] = field(default_factory=dict)
    fundamentals_report: dict[str, Any] = field(default_factory=dict)

    # Research debate
    bull_argument: dict[str, Any] = field(default_factory=dict)
    bear_argument: dict[str, Any] = field(default_factory=dict)
    research_manager_verdict: dict[str, Any] = field(default_factory=dict)

    # Trade proposal + risk debate
    trader_proposal: dict[str, Any] = field(default_factory=dict)
    critic_verdicts: dict[str, dict[str, Any]] = field(default_factory=dict)

    # Final
    portfolio_decision: dict[str, Any] = field(default_factory=dict)

    # Working scratch — cleared by Msg Clear between phases.
    working_messages: list[dict[str, str]] = field(default_factory=list)

    # Bookkeeping
    errors: dict[str, str] = field(default_factory=dict)
    timings: dict[str, float] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _safe_json(content: str) -> dict[str, Any]:
    """Best-effort JSON parse. Strips ```json fences, returns {} on failure."""
    raw = (content or "").strip()
    if raw.startswith("```"):
        raw = "\n".join(l for l in raw.splitlines() if not l.startswith("```")).strip()
    try:
        out = json.loads(raw)
        return out if isinstance(out, dict) else {"value": out}
    except Exception:
        return {}


def msg_clear(state: GraphState, next_phase: str) -> None:
    """Drop the working scratchpad and seed the next phase with a Continue marker.

    This is the TradingAgents "Msg Clear" placeholder trick: each analyst
    phase fills working_messages with verbose tool-use traffic that the next
    phase doesn't need (and that would blow the context window if kept). We
    truncate it down to a single HumanMessage('Continue') so the next node
    starts fresh.

    Stores the cleared count under timings[next_phase + ':cleared'] for
    observability.
    """
    cleared = len(state.working_messages)
    state.working_messages = [{"role": "user", "content": f"Continue: {next_phase}"}]
    state.timings[f"{next_phase}:cleared_msgs"] = float(cleared)


# ---------------------------------------------------------------------------
# Grunt nodes — one per analyst type. Each runs on its assigned 8b model.
# ---------------------------------------------------------------------------

def _build_analyst_prompt(symbol: str, perspective: str, market_data: dict) -> tuple[str, str]:
    system = (
        f"You are the {perspective} analyst on a disciplined trading desk. "
        "Read the data, identify the 2-3 most actionable signals, and return "
        "ONLY valid JSON. Do not pad. Do not editorialise."
    )
    user = f"""Analyse {symbol} from a {perspective} perspective.

## Market Data
```json
{json.dumps(market_data, indent=2, default=str)[:3000]}
```

Return ONLY this JSON:
{{
  "summary": "<2-3 sentence summary of what you see>",
  "signals": ["<signal 1>", "<signal 2>"],
  "confidence": <0.0-1.0>
}}"""
    return system, user


def market_analyst_node(state: GraphState) -> GraphState:
    t0 = time.monotonic()
    sys_p, usr_p = _build_analyst_prompt(state.symbol, "technical/market", state.market_data)
    try:
        raw = _invoke_llm(
            system_prompt=sys_p, user_message=usr_p,
            model=model_for_node("market_analyst"),
            max_tokens=400, temperature=0.3,
            role="default", agent="graph.market_analyst",
        )
        state.market_report = _safe_json(raw) or {"summary": raw[:300]}
        state.working_messages.append({"role": "assistant", "content": raw[:500]})
    except Exception as exc:
        state.errors["market_analyst"] = str(exc)
        state.market_report = {"summary": f"market analyst failed: {exc}", "confidence": 0.0}
    state.timings["market_analyst"] = time.monotonic() - t0
    return state


def sentiment_analyst_node(state: GraphState) -> GraphState:
    t0 = time.monotonic()
    sys_p, usr_p = _build_analyst_prompt(state.symbol, "sentiment/positioning", state.perplexity_intel)
    try:
        raw = _invoke_llm(
            system_prompt=sys_p, user_message=usr_p,
            model=model_for_node("sentiment_analyst"),
            max_tokens=400, temperature=0.3,
            role="default", agent="graph.sentiment_analyst",
        )
        state.sentiment_report = _safe_json(raw) or {"summary": raw[:300]}
        state.working_messages.append({"role": "assistant", "content": raw[:500]})
    except Exception as exc:
        state.errors["sentiment_analyst"] = str(exc)
        state.sentiment_report = {"summary": f"sentiment analyst failed: {exc}", "confidence": 0.0}
    state.timings["sentiment_analyst"] = time.monotonic() - t0
    return state


def news_analyst_node(state: GraphState) -> GraphState:
    t0 = time.monotonic()
    news_payload = {
        "intel": state.perplexity_intel,
        "earnings_within_days": state.perplexity_intel.get("earnings_within_days"),
        "catalyst": state.perplexity_intel.get("catalyst"),
    }
    sys_p, usr_p = _build_analyst_prompt(state.symbol, "news/catalyst", news_payload)
    try:
        raw = _invoke_llm(
            system_prompt=sys_p, user_message=usr_p,
            model=model_for_node("news_analyst"),
            max_tokens=400, temperature=0.3,
            role="default", agent="graph.news_analyst",
        )
        state.news_report = _safe_json(raw) or {"summary": raw[:300]}
        state.working_messages.append({"role": "assistant", "content": raw[:500]})
    except Exception as exc:
        state.errors["news_analyst"] = str(exc)
        state.news_report = {"summary": f"news analyst failed: {exc}", "confidence": 0.0}
    state.timings["news_analyst"] = time.monotonic() - t0
    return state


def fundamentals_analyst_node(state: GraphState) -> GraphState:
    t0 = time.monotonic()
    sys_p, usr_p = _build_analyst_prompt(state.symbol, "fundamentals/valuation", state.market_data)
    try:
        raw = _invoke_llm(
            system_prompt=sys_p, user_message=usr_p,
            model=model_for_node("fundamentals_analyst"),
            max_tokens=400, temperature=0.3,
            role="default", agent="graph.fundamentals_analyst",
        )
        state.fundamentals_report = _safe_json(raw) or {"summary": raw[:300]}
        state.working_messages.append({"role": "assistant", "content": raw[:500]})
    except Exception as exc:
        state.errors["fundamentals_analyst"] = str(exc)
        state.fundamentals_report = {"summary": f"fundamentals analyst failed: {exc}", "confidence": 0.0}
    state.timings["fundamentals_analyst"] = time.monotonic() - t0
    return state


# ---------------------------------------------------------------------------
# Bull / Bear — read the consolidated analyst slots, debate one round
# ---------------------------------------------------------------------------

def _analyst_digest(state: GraphState) -> dict[str, Any]:
    return {
        "market": state.market_report.get("summary", ""),
        "sentiment": state.sentiment_report.get("summary", ""),
        "news": state.news_report.get("summary", ""),
        "fundamentals": state.fundamentals_report.get("summary", ""),
    }


def bull_node(state: GraphState) -> GraphState:
    t0 = time.monotonic()
    digest = _analyst_digest(state)
    sys_p = (
        "You are an aggressive bullish analyst. You've read the four analyst reports. "
        "Build the strongest possible BUY case. Return ONLY JSON."
    )
    usr_p = f"""Build a bull case for {state.symbol}.

## Analyst Digest
```json
{json.dumps(digest, indent=2)}
```

Return ONLY this JSON:
{{
  "argument": "<3-4 sentence bull case>",
  "key_catalysts": ["<catalyst 1>", "<catalyst 2>"],
  "target_price": <float>,
  "confidence": <0.0-1.0>
}}"""
    try:
        raw = _invoke_llm(
            system_prompt=sys_p, user_message=usr_p,
            model=model_for_node("bull"),
            max_tokens=600, temperature=0.4,
            role="debate", agent="graph.bull",
        )
        state.bull_argument = _safe_json(raw) or {"argument": raw[:400]}
        state.working_messages.append({"role": "assistant", "content": raw[:500]})
    except Exception as exc:
        state.errors["bull"] = str(exc)
        state.bull_argument = {"argument": f"bull failed: {exc}", "confidence": 0.0}
    state.timings["bull"] = time.monotonic() - t0
    return state


def bear_node(state: GraphState) -> GraphState:
    t0 = time.monotonic()
    digest = _analyst_digest(state)
    sys_p = (
        "You are a skeptical bearish analyst. You've read the four analyst reports "
        "AND the bull's argument. Find every reason the trade fails. Return ONLY JSON."
    )
    usr_p = f"""Counter the bull case for {state.symbol}.

## Analyst Digest
```json
{json.dumps(digest, indent=2)}
```

## Bull Argument
{json.dumps(state.bull_argument, indent=2)}

Return ONLY this JSON:
{{
  "argument": "<3-4 sentence bear counter>",
  "key_risks": ["<risk 1>", "<risk 2>"],
  "downside_target": <float>,
  "stop_recommended": <float>,
  "confidence": <0.0-1.0>
}}"""
    try:
        raw = _invoke_llm(
            system_prompt=sys_p, user_message=usr_p,
            model=model_for_node("bear"),
            max_tokens=600, temperature=0.4,
            role="debate", agent="graph.bear",
        )
        state.bear_argument = _safe_json(raw) or {"argument": raw[:400]}
        state.working_messages.append({"role": "assistant", "content": raw[:500]})
    except Exception as exc:
        state.errors["bear"] = str(exc)
        state.bear_argument = {"argument": f"bear failed: {exc}", "confidence": 0.0}
    state.timings["bear"] = time.monotonic() - t0
    return state


# ---------------------------------------------------------------------------
# Research Manager — JUDGE (70b). Decides which side won.
# ---------------------------------------------------------------------------

def research_manager_node(state: GraphState) -> GraphState:
    t0 = time.monotonic()
    sys_p = (
        "You are the Research Manager — head of the equity research desk. "
        "You read the bull and bear arguments and decide which side is better-supported "
        "by the analyst data. You are the last quality gate before a trade is proposed. "
        "Return ONLY JSON."
    )
    usr_p = f"""Render a verdict on the {state.symbol} debate.

## Bull
{json.dumps(state.bull_argument, indent=2)}

## Bear
{json.dumps(state.bear_argument, indent=2)}

Return ONLY this JSON:
{{
  "winning_side": "<BULL | BEAR | DRAW>",
  "verdict": "<2-3 sentence synthesis>",
  "go_to_trader": <true | false>,
  "confidence": <0.0-1.0>
}}"""
    try:
        raw = _invoke_llm(
            system_prompt=sys_p, user_message=usr_p,
            model=model_for_node("research_manager"),
            max_tokens=600, temperature=0.2,
            role="arbiter", agent="graph.research_manager",
        )
        state.research_manager_verdict = _safe_json(raw) or {"verdict": raw[:400], "go_to_trader": False}
    except Exception as exc:
        state.errors["research_manager"] = str(exc)
        state.research_manager_verdict = {"verdict": f"manager failed: {exc}", "go_to_trader": False}
    state.timings["research_manager"] = time.monotonic() - t0
    return state


# ---------------------------------------------------------------------------
# Trader — produces a structured TraderProposal. This is the ONLY thing the
# Portfolio Manager sees from the analyst pipeline (no raw analyst noise).
# ---------------------------------------------------------------------------

def trader_node(state: GraphState) -> GraphState:
    t0 = time.monotonic()
    sys_p = (
        "You are a senior trader. The research manager handed you a verdict. "
        "Translate it into a clean trade proposal with explicit entry/stop/target. "
        "Return ONLY JSON. NO prose."
    )
    usr_p = f"""Build a trade proposal for {state.symbol}.

## Research Manager Verdict
{json.dumps(state.research_manager_verdict, indent=2)}

## Bull (for context only)
{json.dumps(state.bull_argument, indent=2)}

## Bear (for context only)
{json.dumps(state.bear_argument, indent=2)}

## Current Price
{state.market_data.get("current_price", "unknown")}

Return ONLY this JSON:
{{
  "decision": "<BUY | NO_TRADE | WAIT>",
  "symbol": "{state.symbol}",
  "entry_price": <float>,
  "stop_loss": <float>,
  "target_price": <float>,
  "position_size_pct": <float 0-20>,
  "confidence": <0.0-1.0>,
  "thesis_summary": "<one line>",
  "reasoning": "<2 sentences>"
}}"""
    try:
        raw = _invoke_llm(
            system_prompt=sys_p, user_message=usr_p,
            model=model_for_node("trader"),
            max_tokens=700, temperature=0.2,
            role="debate", agent="graph.trader",
        )
        proposal = _safe_json(raw)
        if not proposal:
            proposal = {"decision": "NO_TRADE", "symbol": state.symbol,
                        "reasoning": f"unparseable trader output: {raw[:200]}"}
        proposal.setdefault("symbol", state.symbol)
        proposal.setdefault("decision", "NO_TRADE")
        state.trader_proposal = proposal
    except Exception as exc:
        state.errors["trader"] = str(exc)
        state.trader_proposal = {
            "decision": "NO_TRADE", "symbol": state.symbol,
            "reasoning": f"trader failed: {exc}", "confidence": 0.0,
        }
    state.timings["trader"] = time.monotonic() - t0
    return state


# ---------------------------------------------------------------------------
# Critics (3, round-robin) — each takes one pass at the trader proposal.
# Output is a tight verdict only. Portfolio Manager sees these, NOT raw notes.
# ---------------------------------------------------------------------------

_CRITIC_SYSTEMS = {
    "conservative": (
        "You are the conservative risk critic. Your job: find what could go wrong. "
        "Veto if the trade has unbounded downside or a fragile thesis. Return ONLY JSON."
    ),
    "aggressive": (
        "You are the aggressive opportunity critic. Your job: defend the trade if size is too small "
        "or stop too tight. Push for conviction. Return ONLY JSON."
    ),
    "neutral": (
        "You are the neutral critic. Your job: weigh both views and give the balanced read. "
        "Return ONLY JSON."
    ),
}


def _critic_node(state: GraphState, perspective: str, prior_verdicts: dict[str, dict]) -> GraphState:
    t0 = time.monotonic()
    sys_p = _CRITIC_SYSTEMS[perspective]
    usr_p = f"""Evaluate this trade proposal as the {perspective} critic.

## Trade Proposal
{json.dumps(state.trader_proposal, indent=2)}

## Prior Critics
{json.dumps(prior_verdicts, indent=2)}

Return ONLY this JSON:
{{
  "perspective": "{perspective}",
  "verdict": "<APPROVE | VETO | ADJUST>",
  "key_concern": "<one sentence>",
  "size_multiplier": <float 0.0-2.0>,
  "confidence_adjustment": <float -0.3 to 0.3>
}}"""
    node_key = f"{perspective}_critic"
    try:
        raw = _invoke_llm(
            system_prompt=sys_p, user_message=usr_p,
            model=model_for_node(node_key),
            max_tokens=400, temperature=0.3,
            role="risk", agent=f"graph.{node_key}",
        )
        verdict = _safe_json(raw) or {"verdict": "APPROVE", "key_concern": raw[:200]}
        verdict.setdefault("perspective", perspective)
        verdict.setdefault("verdict", "APPROVE")
        verdict.setdefault("size_multiplier", 1.0)
        verdict.setdefault("confidence_adjustment", 0.0)
        state.critic_verdicts[perspective] = verdict
    except Exception as exc:
        state.errors[node_key] = str(exc)
        state.critic_verdicts[perspective] = {
            "perspective": perspective, "verdict": "VETO",
            "key_concern": f"{perspective} critic failed: {exc}",
            "size_multiplier": 0.5, "confidence_adjustment": -0.2,
        }
    state.timings[node_key] = time.monotonic() - t0
    return state


def conservative_critic_node(state: GraphState) -> GraphState:
    return _critic_node(state, "conservative", {})


def aggressive_critic_node(state: GraphState) -> GraphState:
    return _critic_node(state, "aggressive", {"conservative": state.critic_verdicts.get("conservative", {})})


def neutral_critic_node(state: GraphState) -> GraphState:
    prior = {
        "conservative": state.critic_verdicts.get("conservative", {}),
        "aggressive": state.critic_verdicts.get("aggressive", {}),
    }
    return _critic_node(state, "neutral", prior)


# ---------------------------------------------------------------------------
# Portfolio Manager — JUDGE (70b). Final go/no-go.
# Sees ONLY the structured TraderProposal + critic verdicts (not raw notes).
# ---------------------------------------------------------------------------

def portfolio_manager_node(state: GraphState) -> GraphState:
    t0 = time.monotonic()
    sys_p = (
        "You are the Portfolio Manager — final decision-maker for the fund. "
        "You receive a clean trade proposal and three critic verdicts. "
        "Decide GO or NO-GO. If any critic raised a dealbreaker, veto. "
        "Return ONLY JSON."
    )
    # Deliberately project a SLIM payload — no analyst raw output, no debate
    # transcript. Just the trader proposal and the three critic verdicts.
    usr_p = f"""Final decision for {state.symbol}.

## Trade Proposal
{json.dumps(state.trader_proposal, indent=2)}

## Critic Verdicts
{json.dumps(state.critic_verdicts, indent=2)}

Return ONLY this JSON:
{{
  "decision": "<BUY | NO_TRADE | WAIT>",
  "symbol": "{state.symbol}",
  "confidence": <0.0-1.0>,
  "position_size_pct": <float 0-20>,
  "entry_price": <float>,
  "stop_loss": <float>,
  "target_price": <float>,
  "risk_reward_ratio": <float>,
  "reasoning": "<2-3 sentences referencing critic concerns>",
  "thesis_summary": "<one line>",
  "vetoed_by": "<critic name or 'none'>"
}}"""
    try:
        raw = _invoke_llm(
            system_prompt=sys_p, user_message=usr_p,
            model=model_for_node("portfolio_manager"),
            max_tokens=900, temperature=0.2,
            role="arbiter", agent="graph.portfolio_manager",
        )
        decision = _safe_json(raw)
        if not decision:
            decision = {
                "decision": "NO_TRADE", "symbol": state.symbol,
                "reasoning": f"unparseable PM output: {raw[:200]}",
                "confidence": 0.0,
            }
        decision.setdefault("symbol", state.symbol)
        decision.setdefault("decision", "NO_TRADE")
        decision.setdefault("confidence", 0.0)
        # Hard floor — defense in depth on top of market_open's own gate.
        if decision["decision"] == "BUY" and float(decision.get("confidence", 0)) < 0.70:
            decision["decision"] = "NO_TRADE"
            decision["reasoning"] = (
                f"Downgraded by PM gate: confidence "
                f"{decision.get('confidence', 0):.2f} < 0.70 floor. "
                + str(decision.get("reasoning", ""))
            )
        state.portfolio_decision = decision
    except Exception as exc:
        state.errors["portfolio_manager"] = str(exc)
        state.portfolio_decision = {
            "decision": "NO_TRADE", "symbol": state.symbol,
            "reasoning": f"portfolio manager failed: {exc}",
            "confidence": 0.0,
        }
    state.timings["portfolio_manager"] = time.monotonic() - t0
    return state


# ---------------------------------------------------------------------------
# Async DAG runner — drives one candidate through all 12 nodes.
# Grunt analysts run in parallel; the rest run sequentially.
# ---------------------------------------------------------------------------

async def _run_in_thread(fn: Callable[[GraphState], GraphState], state: GraphState) -> GraphState:
    """Wrap a sync node in asyncio.to_thread so it doesn't block the loop.

    The node itself does blocking HTTP to Ollama; running them in threads
    lets gather() fan out concurrently.
    """
    return await asyncio.to_thread(fn, state)


async def run_candidate_graph(
    symbol: str,
    market_data: dict[str, Any],
    perplexity_intel: dict[str, Any],
    risk_check: dict[str, Any],
) -> dict[str, Any]:
    """Run the full 12-node debate graph for ONE candidate.

    Returns a dict shaped like the existing debate_orchestrator output so
    market_open.py can swap us in without touching downstream code.
    """
    overall_t0 = time.monotonic()

    # Risk-check short circuit — same contract as run_debate.
    if not risk_check.get("approved", False):
        violations = risk_check.get("violations", ["risk check failed"])
        return _no_trade_result(symbol, f"Risk check failed: {'; '.join(violations)}")

    state = GraphState(
        symbol=symbol,
        market_data=market_data,
        perplexity_intel=perplexity_intel,
        risk_check=risk_check,
    )

    # ── Phase 1: 4 grunt analysts in parallel ──────────────────────────
    analyst_tasks = [
        _run_in_thread(market_analyst_node, state),
        _run_in_thread(sentiment_analyst_node, state),
        _run_in_thread(news_analyst_node, state),
        _run_in_thread(fundamentals_analyst_node, state),
    ]
    # NB: each task mutates the same state object. Asyncio.to_thread + GIL
    # makes the writes safe for our use (each writes a different slot), but
    # we still gather() so all four complete before the next phase.
    await asyncio.gather(*analyst_tasks, return_exceptions=True)

    # Msg Clear — drop the verbose analyst working messages, inject Continue
    msg_clear(state, "research")

    # ── Phase 2: bull → bear (sequential — bear reads bull) ────────────
    state = await _run_in_thread(bull_node, state)
    state = await _run_in_thread(bear_node, state)

    msg_clear(state, "research_manager")

    # ── Phase 3: research manager (judge, 70b) ─────────────────────────
    state = await _run_in_thread(research_manager_node, state)

    msg_clear(state, "trader")

    # ── Phase 4: trader proposal ───────────────────────────────────────
    state = await _run_in_thread(trader_node, state)

    msg_clear(state, "critics")

    # ── Phase 5: 3 critics round-robin (each reads prior verdicts) ─────
    state = await _run_in_thread(conservative_critic_node, state)
    state = await _run_in_thread(aggressive_critic_node, state)
    state = await _run_in_thread(neutral_critic_node, state)

    msg_clear(state, "portfolio_manager")

    # ── Phase 6: portfolio manager (judge, 70b) ────────────────────────
    state = await _run_in_thread(portfolio_manager_node, state)

    elapsed = time.monotonic() - overall_t0
    state.timings["__total__"] = elapsed
    logger.info(
        "graph %s done in %.1fs decision=%s confidence=%.2f errors=%d",
        symbol, elapsed,
        state.portfolio_decision.get("decision", "?"),
        float(state.portfolio_decision.get("confidence", 0) or 0),
        len(state.errors),
    )

    return _to_legacy_shape(state)


def _to_legacy_shape(state: GraphState) -> dict[str, Any]:
    """Convert the graph's GraphState into the dict shape downstream
    code (market_open.py, journaling, dashboards) already understands.
    Mirrors debate_orchestrator.run_debate's return contract.
    """
    bull_thesis = {
        "symbol": state.symbol,
        "thesis": state.bull_argument.get("argument", ""),
        "catalysts": state.bull_argument.get("key_catalysts", []),
        "target_price": state.bull_argument.get("target_price", 0.0),
        "entry_zone": {"low": 0.0, "high": 0.0},
        "timeframe_days": 5,
        "confidence": state.bull_argument.get("confidence", 0.0),
        "supporting_data": "",
    }
    bear_thesis = {
        "symbol": state.symbol,
        "counter_thesis": state.bear_argument.get("argument", ""),
        "risks": state.bear_argument.get("key_risks", []),
        "downside_target": state.bear_argument.get("downside_target", 0.0),
        "stop_recommended": state.bear_argument.get("stop_recommended", 0.0),
        "invalidation_signal": "",
        "confidence": state.bear_argument.get("confidence", 0.0),
    }
    return {
        "bull": bull_thesis,
        "bear": bear_thesis,
        "decision": state.portfolio_decision,
        "trader_proposal": state.trader_proposal,
        "research_manager": state.research_manager_verdict,
        "critic_verdicts": state.critic_verdicts,
        "analyst_reports": {
            "market": state.market_report,
            "sentiment": state.sentiment_report,
            "news": state.news_report,
            "fundamentals": state.fundamentals_report,
        },
        "errors": state.errors,
        "timings": state.timings,
        "graph_version": "two-tier-parallel-v1",
        "combined": True,
    }


def _no_trade_result(symbol: str, reason: str) -> dict[str, Any]:
    """Return a NO_TRADE shaped like a normal graph result."""
    return {
        "bull": {"symbol": symbol, "thesis": "", "catalysts": [], "target_price": 0.0,
                 "entry_zone": {"low": 0.0, "high": 0.0}, "timeframe_days": 0,
                 "confidence": 0.0, "supporting_data": "", "error": reason},
        "bear": {"symbol": symbol, "counter_thesis": "", "risks": [],
                 "downside_target": 0.0, "stop_recommended": 0.0,
                 "invalidation_signal": "", "confidence": 0.0, "error": reason},
        "decision": {"decision": "NO_TRADE", "symbol": symbol, "confidence": 0.0,
                     "position_size_pct": 0.0, "entry_price": 0.0, "stop_loss": 0.0,
                     "target_price": 0.0, "risk_reward_ratio": 0.0,
                     "reasoning": reason, "thesis_summary": f"NO_TRADE — {reason}"},
        "trader_proposal": {}, "research_manager": {}, "critic_verdicts": {},
        "analyst_reports": {}, "errors": {"pre_check": reason}, "timings": {},
        "graph_version": "two-tier-parallel-v1", "combined": True,
    }


# ---------------------------------------------------------------------------
# Top-level fan-out — run N candidates in parallel under a Semaphore
# ---------------------------------------------------------------------------

DEFAULT_MAX_PARALLEL = int(os.environ.get("SHARK_GRAPH_PARALLEL", "5"))


async def _bounded_run(
    sem: asyncio.Semaphore,
    candidate: dict[str, Any],
) -> tuple[str, dict[str, Any]]:
    """Run one candidate under the semaphore. Errors return as NO_TRADE."""
    symbol = candidate.get("symbol", "?")
    async with sem:
        try:
            result = await run_candidate_graph(
                symbol=symbol,
                market_data=candidate.get("market_data", {}),
                perplexity_intel=candidate.get("perplexity_intel", {}),
                risk_check=candidate.get("risk_check", {"approved": True}),
            )
            return symbol, result
        except Exception as exc:
            logger.error("graph crashed for %s: %s", symbol, exc, exc_info=True)
            return symbol, _no_trade_result(symbol, f"graph crashed: {exc}")


async def run_candidates_parallel(
    candidates: list[dict[str, Any]],
    max_parallel: int = DEFAULT_MAX_PARALLEL,
) -> dict[str, dict[str, Any]]:
    """Fan out N candidates concurrently via asyncio.gather + Semaphore.

    Each candidate dict is expected to contain ``symbol``, ``market_data``,
    ``perplexity_intel``, and ``risk_check`` (the same shape market_open.py
    already builds in ``_collect_candidate_data``).

    Returns ``{symbol: graph_result}``. A failure in one candidate does NOT
    fail the others — that candidate just gets a NO_TRADE result.
    """
    if not candidates:
        return {}
    sem = asyncio.Semaphore(max_parallel)
    tasks = [_bounded_run(sem, c) for c in candidates]
    pairs = await asyncio.gather(*tasks)
    return {sym: result for sym, result in pairs}


def run_candidates_parallel_sync(
    candidates: list[dict[str, Any]],
    max_parallel: int = DEFAULT_MAX_PARALLEL,
) -> dict[str, dict[str, Any]]:
    """Sync wrapper for the parallel runner — convenient from non-async code
    paths (the existing market_open phases are synchronous)."""
    return asyncio.run(run_candidates_parallel(candidates, max_parallel))
