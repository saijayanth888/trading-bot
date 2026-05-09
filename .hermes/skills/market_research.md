---
name: market_research
trigger: "Every 30 minutes via cron, or when sentiment confidence drops below 0.3"
tools: [get_current_regime, get_sentiment_scores, get_onchain_signals,
        get_latest_headlines, get_fear_greed_index, get_reddit_buzz,
        get_source_agreement]
priority: normal
---

# Autonomous market research protocol

Every 30 minutes, scan the multi-source sentiment + on-chain + regime feeds
for **divergences** — places where the bot's existing signals disagree with
each other, or where a narrative is forming that the LLM-scored sentiment
hasn't picked up yet. Store findings in Hermes memory; alert on Telegram
when actionable.

## Step 1 — Calibrate research depth

Call `get_sentiment_scores` first. The latest row's `confidence` field
controls the cycle's depth budget:

| Confidence  | Cycle | Time budget |
|-------------|-------|-------------|
| ≥ 0.70      | light | ≤ 5 min     |
| 0.30 – 0.70 | normal | ≤ 10 min   |
| < 0.30      | deep  | ≤ 15 min    |

In light mode, just check Fear & Greed + trending; skip the per-pair scan.
In deep mode, also pull `get_onchain_signals` and run all SQL queries below.

## Step 2 — Per-pair scan (BTC, ETH, SOL only — ADA gets the light-cycle skip)

For each pair:

1. `get_latest_headlines(pair, limit=20)` — read titles + community_sentiment
2. `get_reddit_buzz(pair)` — top posts + avg attention over 24h
3. Note any single post with **attention_score > 0.5** OR **community_sentiment
   absolute > 0.7** (= strong upvote skew, ≥ 5 score floor) — those are
   single-source signals worth flagging.

## Step 3 — Cross-source divergence detection

Call `get_source_agreement` and look for these patterns:

| Pattern | What it means | Action |
|---|---|---|
| Fear & Greed = "Extreme Greed" but `reddit_community_avg < 0` for top pair | Crowd is bearish even though headline mood is euphoric | Flag potential reversal — recommend reducing long bias for 4h |
| Reddit attention spiking (>0.7) on a pair but `llm_market_impact == "neutral"` | Narrative forming faster than headlines catch up | Flag as early-stage pattern; suggest tightening stop on long position if open |
| Reddit `community_avg` strongly bearish but price held in last 4h | Short-squeeze setup | Flag for the operator; do NOT auto-act |
| `trending == true` for a pair NOT in our pair_weights or with weight < 0.05 | We're missing a momentum opportunity in the universe of tradeable pairs | Recommend adding the pair via capital_allocation rebalance |
| Fear & Greed history_7d trending DOWN ≥ 15 points but `llm_market_impact == "bullish"` | LLM scoring lagging market mood collapse | Recommend dropping `tft_min_confidence` floor temporarily so signals get tougher to fire |

## Step 4 — Persist findings

Write a research note to ``~/.hermes/state-snapshots/market_research_<UTC stamp>.json``
with this shape:

```json
{
  "ts": "2026-05-09T01:30:00Z",
  "type": "market_research",
  "cycle": "normal",
  "confidence_at_trigger": 0.45,
  "fear_greed": {"value": 64, "classification": "Greed", "trend_7d": "rising"},
  "findings": [
    {
      "pair": "BTC",
      "signal": "narrative_forming",
      "evidence": ["reddit attention 0.82", "llm sentiment neutral"],
      "confidence": 0.6
    }
  ],
  "divergences_detected": [
    {"pattern": "fng_greed_vs_reddit_bearish", "pair": "ETH", "magnitude": 0.4}
  ],
  "actionable_signal": false,
  "recommended_pairs": [],
  "reasoning": "Mild divergence on ETH but no clear actionable signal yet."
}
```

Actionability rule: set `actionable_signal: true` ONLY when at least one
divergence has `magnitude > 0.5` AND the pair has weight > 0 in
`config.json[capital_allocation][pair_weights]`. Below that, this is
data-collection only.

## Step 5 — Alert if actionable

If `actionable_signal == true`, send a Telegram message via
`$TELEGRAM_BOT_TOKEN` (sourced from `~/Documents/trading-bot/.env`) with
the format:

```
:warning: Market research signal — <pair>
Pattern: <divergence name>
Evidence: <2-3 bullets>
Recommendation: <one sentence — what the operator could consider, NOT auto-applied>
Confidence: <0.0-1.0>
```

If `$TELEGRAM_BOT_TOKEN` isn't configured, fall back to the Slack webhook
in the same `.env` (use `slack_reporting` skill conventions).

## Step 6 — Feed into next sentiment cycle

The `sentiment_engine.py` poll cycle reads the most-recent research note
on startup (best-effort — file missing = ignore). The note's `findings`
list is appended to the LLM-scoring user prompt as additional context
under a header `## Recent autonomous research`. This lets the next
Hermes-3 70B/8B scoring round reason over both raw headlines AND prior
Hermes synthesis.

## Hard rules

1. **Never auto-trade.** This skill recommends; the operator (or an
   explicit `pause_trading` / capital-rebalance script) acts.
2. **Never write to config.json.** Recommendations only — the dashboard's
   regime-params editor is the human-supervised commit path.
3. **Cap report at 3 findings.** If 5 divergences are detected, pick the 3
   with the highest `magnitude`. Operator attention is finite.
4. **Time-box deep cycles.** If the deep cycle is approaching 15 min,
   abandon remaining pair scans and persist what's been found so far.
