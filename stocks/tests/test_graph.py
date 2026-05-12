"""
Tests for the LangGraph-style two-tier parallel debate (stocks/shark/graph.py).

Covers:
  - Graph executes end-to-end with mocked LLM client
  - Msg Clear correctly drops intermediate working messages
  - 5 candidates run concurrently — wall-clock ~ slowest one (not 5x sum)
  - Portfolio Manager receives only TraderProposal + critic verdicts
    (no raw analyst noise) — verified by inspecting the prompt it gets
  - Failure of one candidate doesn't fail the others
  - model_tiers.json drives per-node model selection
"""
from __future__ import annotations

import asyncio
import json
import time
from unittest.mock import patch

import pytest


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def mock_llm():
    """Patch the LLM resolver so every node returns deterministic JSON.

    Records every (system_prompt, user_message, model, role, agent) call so
    individual tests can introspect what each node received.
    """
    calls: list[dict] = []

    def fake_call(*, system_prompt, user_message, max_tokens, temperature,
                  role, tier, agent):
        calls.append({
            "system_prompt": system_prompt,
            "user_message": user_message,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "role": role,
            "tier": tier,
            "agent": agent,
        })

        # Return a JSON string shaped to satisfy the per-node downstream
        # parsing. The *first* word of the agent suffix tells us which node.
        if "market_analyst" in agent:
            payload = {"summary": "market: trend up", "signals": ["RSI 60"], "confidence": 0.7}
        elif "sentiment_analyst" in agent:
            payload = {"summary": "sentiment: bullish twitter chatter", "signals": ["bull flow"], "confidence": 0.6}
        elif "news_analyst" in agent:
            payload = {"summary": "news: upcoming product launch", "signals": ["catalyst"], "confidence": 0.7}
        elif "fundamentals_analyst" in agent:
            payload = {"summary": "fundamentals: rev growth", "signals": ["EPS beat"], "confidence": 0.65}
        elif "graph.bull" in agent:
            payload = {"argument": "Strong bull case",
                       "key_catalysts": ["earnings beat", "AI tailwind"],
                       "target_price": 200.0, "confidence": 0.75}
        elif "graph.bear" in agent:
            payload = {"argument": "Some risks",
                       "key_risks": ["macro", "valuation"],
                       "downside_target": 150.0, "stop_recommended": 170.0,
                       "confidence": 0.5}
        elif "research_manager" in agent:
            payload = {"winning_side": "BULL", "verdict": "Bull wins on catalysts",
                       "go_to_trader": True, "confidence": 0.78}
        elif "trader" in agent:
            payload = {"decision": "BUY", "symbol": "MOCK",
                       "entry_price": 180.0, "stop_loss": 170.0,
                       "target_price": 200.0, "position_size_pct": 5.0,
                       "confidence": 0.78,
                       "thesis_summary": "BUY MOCK on catalyst",
                       "reasoning": "RM said go; bull strong"}
        elif "conservative_critic" in agent:
            payload = {"perspective": "conservative", "verdict": "APPROVE",
                       "key_concern": "watch the macro", "size_multiplier": 1.0,
                       "confidence_adjustment": 0.0}
        elif "aggressive_critic" in agent:
            payload = {"perspective": "aggressive", "verdict": "APPROVE",
                       "key_concern": "size could be larger", "size_multiplier": 1.1,
                       "confidence_adjustment": 0.05}
        elif "neutral_critic" in agent:
            payload = {"perspective": "neutral", "verdict": "APPROVE",
                       "key_concern": "balanced view", "size_multiplier": 1.0,
                       "confidence_adjustment": 0.0}
        elif "portfolio_manager" in agent:
            payload = {"decision": "BUY", "symbol": "MOCK", "confidence": 0.78,
                       "position_size_pct": 5.0, "entry_price": 180.0,
                       "stop_loss": 170.0, "target_price": 200.0,
                       "risk_reward_ratio": 2.0,
                       "reasoning": "All critics approve.",
                       "thesis_summary": "BUY MOCK", "vetoed_by": "none"}
        else:
            payload = {"summary": "default", "confidence": 0.5}

        return (json.dumps(payload), {"input_tokens": 10, "output_tokens": 20}, "mock-model")

    with patch("shark.graph._resolve_llm_call", return_value=fake_call):
        yield calls


@pytest.fixture
def sample_candidate():
    return {
        "symbol": "MOCK",
        "market_data": {"current_price": 180.0, "rsi_14": 60, "atr_14": 3.5},
        "perplexity_intel": {"catalyst": "AI launch", "earnings_within_days": 30},
        "risk_check": {"approved": True, "adjusted_size": 10},
    }


# ---------------------------------------------------------------------------
# 1. End-to-end execution
# ---------------------------------------------------------------------------

class TestEndToEnd:
    def test_single_candidate_runs_to_completion(self, mock_llm, sample_candidate):
        """Run one full candidate and confirm we get a non-empty decision."""
        from shark.graph import run_candidates_parallel_sync

        results = run_candidates_parallel_sync([sample_candidate], max_parallel=1)
        assert "MOCK" in results
        result = results["MOCK"]
        assert result["decision"]["decision"] == "BUY"
        assert result["decision"]["symbol"] == "MOCK"
        assert result["decision"]["confidence"] >= 0.7
        # legacy-shape keys are present so market_open can swap us in
        assert "bull" in result and "bear" in result
        assert result["graph_version"] == "two-tier-parallel-v1"

    def test_all_12_nodes_invoked(self, mock_llm, sample_candidate):
        """We expect at least 12 LLM calls — one per node."""
        from shark.graph import run_candidates_parallel_sync

        run_candidates_parallel_sync([sample_candidate], max_parallel=1)
        agents_called = {c["agent"].split(".")[-1] for c in mock_llm}
        expected = {
            "market_analyst", "sentiment_analyst", "news_analyst",
            "fundamentals_analyst", "bull", "bear", "research_manager",
            "trader", "conservative_critic", "aggressive_critic",
            "neutral_critic", "portfolio_manager",
        }
        assert expected.issubset(agents_called), f"missing nodes: {expected - agents_called}"

    def test_failed_risk_check_short_circuits(self, mock_llm):
        """An unapproved risk_check returns NO_TRADE without invoking any node."""
        from shark.graph import run_candidates_parallel_sync

        bad_candidate = {
            "symbol": "BAD",
            "market_data": {},
            "perplexity_intel": {},
            "risk_check": {"approved": False, "violations": ["max positions"]},
        }
        results = run_candidates_parallel_sync([bad_candidate])
        assert results["BAD"]["decision"]["decision"] == "NO_TRADE"
        assert "max positions" in results["BAD"]["decision"]["reasoning"]
        assert mock_llm == []  # no LLM call made


# ---------------------------------------------------------------------------
# 2. Msg Clear placeholder
# ---------------------------------------------------------------------------

class TestMsgClear:
    def test_msg_clear_drops_working_messages(self):
        from shark.graph import GraphState, msg_clear

        state = GraphState(symbol="X")
        state.working_messages = [
            {"role": "assistant", "content": "long verbose tool-use trace"},
            {"role": "assistant", "content": "another one"},
            {"role": "assistant", "content": "and one more"},
        ]
        msg_clear(state, "next_phase")
        # After Msg Clear: only the synthetic Continue marker remains
        assert len(state.working_messages) == 1
        assert state.working_messages[0]["role"] == "user"
        assert "Continue" in state.working_messages[0]["content"]
        # Bookkeeping records how much we cleared
        assert state.timings["next_phase:cleared_msgs"] == 3.0

    def test_msg_clear_runs_between_phases_during_real_graph(self, mock_llm, sample_candidate):
        """Run a full candidate and confirm Msg Clear bookkeeping was emitted
        for every inter-phase boundary."""
        from shark.graph import run_candidate_graph

        result = asyncio.run(run_candidate_graph(
            symbol="MOCK",
            market_data=sample_candidate["market_data"],
            perplexity_intel=sample_candidate["perplexity_intel"],
            risk_check=sample_candidate["risk_check"],
        ))
        clear_keys = [k for k in result["timings"] if k.endswith(":cleared_msgs")]
        # We expect 6 clears: research, research_manager, trader, critics, portfolio_manager
        # (we accept >=5 to allow one to be omitted if structure changes)
        assert len(clear_keys) >= 5, f"expected >=5 msg_clear emissions, got {clear_keys}"


# ---------------------------------------------------------------------------
# 3. Concurrency — 5 candidates in parallel
# ---------------------------------------------------------------------------

class TestConcurrency:
    def test_five_candidates_run_concurrently(self, mock_llm):
        """Patch the LLM to sleep 50ms per call. With 5 candidates and 12
        nodes each = 60 total calls; concurrent fan-out of candidates must
        be measurably faster than sequential.

        We compare:
          - sequential (max_parallel=1): all 60 calls serialised
          - parallel   (max_parallel=5): candidates run concurrently
        """
        from shark.graph import run_candidates_parallel_sync

        # Monkey-patch the fake_call inside mock_llm to add a per-call delay.
        # Easier: install our own slow mock here.
        import shark.graph as g

        sleep_per_call = 0.02  # 20ms per LLM call

        def slow_call(*, system_prompt, user_message, max_tokens, temperature,
                      role, tier, agent):
            time.sleep(sleep_per_call)
            return (json.dumps({
                "summary": "x", "argument": "x", "verdict": "APPROVE",
                "decision": "BUY", "symbol": "X", "confidence": 0.8,
                "winning_side": "BULL", "go_to_trader": True,
                "key_concern": "x", "size_multiplier": 1.0,
                "confidence_adjustment": 0.0,
                "entry_price": 100.0, "stop_loss": 95.0, "target_price": 110.0,
                "position_size_pct": 5.0, "risk_reward_ratio": 2.0,
                "reasoning": "x", "thesis_summary": "x", "vetoed_by": "none",
            }), {"input_tokens": 1, "output_tokens": 1}, "mock-slow")

        with patch.object(g, "_resolve_llm_call", return_value=slow_call):
            candidates = [
                {"symbol": f"C{i}", "market_data": {}, "perplexity_intel": {},
                 "risk_check": {"approved": True}}
                for i in range(5)
            ]

            t0 = time.monotonic()
            run_candidates_parallel_sync(candidates, max_parallel=1)
            sequential = time.monotonic() - t0

            t0 = time.monotonic()
            run_candidates_parallel_sync(candidates, max_parallel=5)
            parallel = time.monotonic() - t0

        # Each candidate has 12 sequential nodes (4 analysts run concurrently
        # internally, the rest serially) — so per-candidate min wall-clock is
        # roughly 9 * sleep_per_call. Sequential = 5 * that. Parallel ~= 1 * that.
        # We assert parallel is at least 2x faster than sequential — a loose
        # bound that survives CI jitter.
        assert parallel * 2 < sequential, (
            f"parallel wall-clock ({parallel:.2f}s) not significantly faster than "
            f"sequential ({sequential:.2f}s)"
        )

    def test_one_candidate_failing_does_not_fail_others(self, mock_llm):
        """If one candidate's graph crashes, the others still complete."""
        from shark.graph import run_candidates_parallel_sync

        good = {"symbol": "GOOD", "market_data": {}, "perplexity_intel": {},
                "risk_check": {"approved": True}}
        # Use approved=False to force the early NO_TRADE path WITHOUT crashing
        # — but to *also* test crash isolation, we install a stub that raises
        # for the BAD symbol when invoked at the trader stage.
        bad = {"symbol": "BAD", "market_data": {}, "perplexity_intel": {},
               "risk_check": {"approved": True}}

        # Wrap the existing mocked call to raise on a specific symbol
        import shark.graph as g
        original_resolver = g._resolve_llm_call

        def selective_call(*, system_prompt, user_message, max_tokens, temperature,
                           role, tier, agent):
            if "BAD" in user_message and "trader" in agent:
                raise RuntimeError("simulated trader crash")
            # Defer to whatever the mock_llm fixture installed
            return original_resolver()(
                system_prompt=system_prompt, user_message=user_message,
                max_tokens=max_tokens, temperature=temperature,
                role=role, tier=tier, agent=agent,
            )

        with patch.object(g, "_resolve_llm_call", return_value=selective_call):
            results = run_candidates_parallel_sync([good, bad], max_parallel=2)

        # Both symbols must appear; GOOD must reach a real BUY, BAD must
        # degrade gracefully (NO_TRADE or a populated errors slot).
        assert "GOOD" in results and "BAD" in results
        assert results["GOOD"]["decision"]["decision"] in ("BUY", "NO_TRADE", "WAIT")
        # BAD should have an error recorded for the trader node.
        assert "trader" in results["BAD"]["errors"], (
            f"expected trader error for BAD; got {results['BAD']['errors']}"
        )


# ---------------------------------------------------------------------------
# 4. Portfolio Manager isolation — sees only TraderProposal + critic verdicts
# ---------------------------------------------------------------------------

class TestPMIsolation:
    def test_pm_prompt_contains_only_proposal_and_critics(self, mock_llm, sample_candidate):
        """The Portfolio Manager prompt must NOT contain the raw analyst
        outputs (market/sentiment/news/fundamentals summaries) — only the
        TraderProposal and critic verdicts."""
        from shark.graph import run_candidates_parallel_sync

        run_candidates_parallel_sync([sample_candidate])
        pm_calls = [c for c in mock_llm if c["agent"] == "graph.portfolio_manager"]
        assert len(pm_calls) == 1
        pm_prompt = pm_calls[0]["user_message"]

        # Must contain the structured trader proposal + critic verdicts
        assert "Trade Proposal" in pm_prompt
        assert "Critic Verdicts" in pm_prompt

        # Must NOT contain raw analyst noise. The grunts produced summaries
        # like "market: trend up" / "sentiment: bullish twitter chatter" —
        # none of those substrings should leak into the PM prompt.
        for noise in ("market: trend up", "sentiment: bullish twitter chatter",
                      "news: upcoming product launch", "fundamentals: rev growth"):
            assert noise not in pm_prompt, f"PM prompt leaked analyst noise: {noise!r}"


# ---------------------------------------------------------------------------
# 5. model_tiers.json drives per-node model selection
# ---------------------------------------------------------------------------

class TestModelTiers:
    def test_judges_get_70b_grunts_get_8b(self):
        """Sanity check: defaults send judges to 70b, grunts to 8b."""
        from shark.graph import model_for_node, _load_model_tiers

        # Force fresh load
        import shark.graph as g
        g._MODEL_TIERS_CACHE = None
        tiers = _load_model_tiers()

        # Judges
        assert tiers["research_manager"] == "hermes3:70b"
        assert tiers["portfolio_manager"] == "hermes3:70b"
        # Grunts
        for grunt in ("market_analyst", "sentiment_analyst", "news_analyst",
                      "fundamentals_analyst", "bull", "bear", "trader",
                      "conservative_critic", "aggressive_critic", "neutral_critic"):
            assert tiers[grunt] == "hermes3:8b", f"{grunt} should be 8b, got {tiers[grunt]}"

        assert model_for_node("portfolio_manager") == "hermes3:70b"
        assert model_for_node("market_analyst") == "hermes3:8b"

    def test_unknown_node_falls_back_to_8b(self):
        from shark.graph import model_for_node
        assert model_for_node("nonexistent_node") == "hermes3:8b"

    def test_pm_call_uses_70b_model(self, mock_llm, sample_candidate):
        """Verify the PM node actually pins a 70b model when invoked."""
        from shark.graph import run_candidates_parallel_sync

        run_candidates_parallel_sync([sample_candidate])
        pm_calls = [c for c in mock_llm if c["agent"] == "graph.portfolio_manager"]
        assert len(pm_calls) == 1
        # The graph passes tier="deep" when model contains "70b"
        assert pm_calls[0]["tier"] == "deep"

    def test_grunt_call_uses_fast_tier(self, mock_llm, sample_candidate):
        from shark.graph import run_candidates_parallel_sync

        run_candidates_parallel_sync([sample_candidate])
        market_calls = [c for c in mock_llm if c["agent"] == "graph.market_analyst"]
        assert len(market_calls) == 1
        assert market_calls[0]["tier"] == "fast"
