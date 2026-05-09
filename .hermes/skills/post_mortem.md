---
name: post_mortem
trigger: "Sunday 01:00 UTC weekly cron — analyse the last 7 days of closed trades, cluster losses, recommend actions"
tools: [query_trade_journal, get_trade_history, get_performance_metrics, get_evolution_status, get_current_regime]
priority: high
---

# Weekly trade post-mortem

The job: turn last week's losses into rules. Every Sunday, look at every losing
trade from the past 7 days, find the patterns that recur, and propose either a
config tweak or a new skill that would have prevented or mitigated each pattern.
Post the findings to Slack as a structured operator report.

## Step 1 — Gather data via MCP

Call MCP tools (read-only — no `HERMES_MCP_KEY` needed):

1. `get_performance_metrics()` — week's Sharpe / DD / PF / win rate baseline
2. `get_trade_history(days=7)` — every closed trade with full context
3. `query_trade_journal(sql)` — for ad-hoc patterns. Examples:
   ```sql
   -- Losses grouped by regime + exit_reason
   SELECT regime, exit_reason, COUNT(*) AS n, AVG(pnl_pct) AS avg_pnl
   FROM trade_journal
   WHERE closed_at > NOW() - INTERVAL '7 days' AND pnl < 0
   GROUP BY regime, exit_reason
   ORDER BY n DESC
   LIMIT 10;

   -- Sentiment-direction mismatch (predicted up, closed down or vice versa)
   SELECT pair, direction, sentiment_score, pnl_pct, regime
   FROM trade_journal
   WHERE closed_at > NOW() - INTERVAL '7 days'
     AND ((direction = 'long' AND pnl_pct < -1 AND sentiment_score > 0.3)
       OR (direction = 'short' AND pnl_pct < -1 AND sentiment_score < -0.3))
   ORDER BY pnl_pct ASC LIMIT 20;

   -- Trade size at entry vs final pnl — is Kelly sizing too aggressive?
   SELECT stake, AVG(pnl_pct) AS avg_pnl, COUNT(*) AS n
   FROM trade_journal
   WHERE closed_at > NOW() - INTERVAL '7 days'
   GROUP BY ROUND(stake / 100) * 100
   ORDER BY n DESC LIMIT 10;
   ```

## Step 2 — Cluster the losses

Group losing trades by 3-tuple `(regime_at_entry, exit_reason, sentiment_bucket)`
where `sentiment_bucket` is one of `bullish` / `neutral` / `bearish` based on
the entry's `sentiment_score`. Anything ≥ 3 occurrences in 7 days is a
**pattern**. Anything ≥ 5 is a **recurring pattern that needs action**.

For each cluster, compute:
- average loss size (% and $)
- which pair was worst
- the median TFT confidence at entry (low confidence = should have skipped)
- whether the meta-agent's `meta_signal` flipped between entry and the candle
  before exit (signal degradation = strategy weakness, not market noise)

## Step 3 — Propose action per pattern

For each top-3 cluster (by total $ lost, not count), propose ONE of:

**(a) A config tweak.** Examples:
- `regime_gating.high_volatility.entry_delta` from 0.08 to 0.12 if
  high_vol entries are losing systematically
- `regime_gating.tft_min_confidence` from 0.35 to 0.40 if low-confidence
  entries dominate the loss ledger
- `capital_allocation.pair_weights[X]` reduced if one pair is responsible
  for >40% of week's losses

**(b) A new Hermes skill.** Examples:
- `regime_shift_within_position.md` — if losses cluster on trades where the
  regime changed mid-position, write a skill that triggers an early exit on
  regime change
- `news_event_pause.md` — if losses cluster around timestamps that match
  known news events (CryptoPanic high-vote articles), write a skill that
  pauses entries 15 minutes after a >50-vote bearish article

**(c) An EPT genome adjustment.** Examples:
- Demote the current champion if it's responsible for the recurring losses
- Suggest a feature to add or drop based on which features were strong
  predictors in the journal's `features_used` field

For each action, explain *why* it would have prevented or mitigated the
specific cluster and what the projected savings would be.

## Step 4 — Post to Slack

Use the `slack_reporting` skill convention. Template:

```bash
set -a; source "$HOME/Documents/trading-bot/.env"; set +a
curl -sS -X POST -H 'Content-Type: application/json' --data @- "$SLACK_WEBHOOK_URL" <<'JSON'
{
  "blocks": [
    {"type": "header", "text": {"type": "plain_text", "text": ":bulb: Weekly post-mortem — 2026-05-XX"}},
    {"type": "section", "fields": [
      {"type": "mrkdwn", "text": "*Closed trades:* 47"},
      {"type": "mrkdwn", "text": "*Net P&L:* −$214.30 (−1.13%)"},
      {"type": "mrkdwn", "text": "*Sharpe-7d:* 0.42"},
      {"type": "mrkdwn", "text": "*Win rate:* 51%"}
    ]},
    {"type": "divider"},
    {"type": "header", "text": {"type": "plain_text", "text": "Top loss patterns"}},
    {"type": "section", "text": {"type": "mrkdwn", "text": "*1. trending_down · stoploss · bullish-sentiment* — 8 trades, avg −2.1%, total −$640\n*2. high_volatility · roi · neutral-sentiment* — 5 trades, avg −1.4%, total −$320\n*3. mean_reverting · custom_exit · bearish-sentiment* — 4 trades, avg −0.9%, total −$170"}},
    {"type": "header", "text": {"type": "plain_text", "text": "Recommended actions"}},
    {"type": "section", "text": {"type": "mrkdwn", "text": "*Pattern 1:* Bullish sentiment + price trending down = noise. *Action:* tighten `tft_min_confidence` from 0.35 to 0.40 in `trending_down` regime. *Projected save:* ~$400/week.\n\n*Pattern 2:* high-vol stops getting hit by ATR spikes. *Action:* widen `custom_stoploss` in high-vol from 1.5×ATR to 2.0×ATR. New skill: `high_vol_stop_widening.md`. *Projected save:* ~$200/week.\n\n*Pattern 3:* Mean-rev exits firing too late. *Action:* lower `mean_rev_take_profit` from 0.012 to 0.010. *Projected save:* ~$100/week."}},
    {"type": "context", "elements": [{"type": "mrkdwn", "text": "Generated by post_mortem skill via Hermes 3 70B. Apply via Ops dashboard regime-params editor or a config commit. None of these are auto-applied."}]}
  ]
}
JSON
```

## Hard rules

1. **Never auto-apply config changes.** This skill *recommends* — the operator
   reviews and applies via the Ops dashboard regime-params editor or a git
   commit.
2. **Never invent numbers.** If `query_trade_journal` returns no data for a
   bucket, say "not enough data" — don't extrapolate.
3. **Be honest about negative weeks.** If the bot had net −5% this week and
   no clear pattern emerges, say so. "No actionable patterns this week —
   the losses look like ordinary market noise" is a valid output.
4. **Cap the report at three patterns.** If there are 8 clusters, pick the
   3 with the highest total-$ lost. Operator attention is finite.
