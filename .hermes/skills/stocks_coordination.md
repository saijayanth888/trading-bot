---
name: stocks_coordination
trigger: "Before any stocks LLM-heavy phase (market-open, debate-driven combined-analyst). Also before kicking off crypto EPT training when stocks may be active."
---

# Cross-system resource coordination

The Spark's GPU is shared between crypto sentiment polling (~5 min cadence,
Hermes-3 70B), stock multi-agent analysis (per market-open phase, also
Hermes-3 70B), and EPT genome evolution (heavy training bursts every
24 h). Without coordination they collide and the slowest path wins.

## Priority order (highest first)

1. **EPT training** — kill-or-defer everything LLM while a generation is
   training. EPT can run for 30-60 min and doesn't tolerate eviction.
2. **Stock analysis** — debate / arbiter calls during market-open are
   trade-relevant; should not be paused for sentiment polls.
3. **Sentiment polling** — every 5 min, but each call is short. Easy to
   defer if a heavier consumer is active.

## Pre-flight checks

### Before running a stocks LLM-heavy phase

1. Call `get_evolution_status()` MCP tool. If `state == "training"`:
   - **Defer** the phase. Re-check every 60s.
   - If the phase is `pre-market` and we miss the window entirely (after
     09:25 ET), log a warning and let the next session pick up.
2. Touch `stocks/memory/.agent-running.lock` with the phase name + start
   timestamp so EPT training will defer to us if it's about to start.
3. After the phase completes, remove the lock file.

### Before running crypto EPT training

1. Check for `stocks/memory/.agent-running.lock`. If present and not stale
   (age < 30 min):
   - **Defer** training start. EPT trainer logs "EPT_DEFERRED:
     stocks_running" and re-checks in 5 min.
2. If the lock is older than 30 min it's almost certainly stale (a
   crashed agent that didn't clean up). Log and proceed.

## Sentiment polling

The 5-min sentiment cron is fine to run alongside other things; it
acquires a *short* GPU window per call. No coordination required unless
the model is being unloaded (keep_alive expired). If the deep-model call
times out, the existing fallback (single-source mode) handles it.

## When to break the rules

- **Risk events** trump everything. If `unified_risk.check_and_trip()`
  fires, run the kill-switch path immediately even if EPT is mid-training.
- **Operator-triggered phases** (`hermes cron run <name>`) take
  precedence over scheduled ones. The operator knows what they're doing.

## Reference

- Lock file: `stocks/memory/.agent-running.lock`
- EPT status: `get_evolution_status` MCP tool
- Combined risk: `get_combined_portfolio` MCP tool
- Skill last updated: 2026-05-10
