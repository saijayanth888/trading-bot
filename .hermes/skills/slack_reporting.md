---
name: slack_reporting
trigger: "When a Hermes cron job is told to 'Send to Slack' or 'send the report to Slack', or when generating a structured operator report. Also when posting a real-time risk warning or critical alert during 24x7 trading operation."
---

# How to send a report to Slack — operator-grade reporting for 24×7 trading

The bot runs unattended around the clock. Operator may be asleep, in a meeting, or in another timezone. Every Slack message must answer four questions in the time it takes to read it:

1. **What happened?** (the fact)
2. **Is it good or bad?** (severity)
3. **What changed since last time?** (trend)
4. **Do I need to do anything right now?** (action)

If a message doesn't answer all four, rewrite it before sending.

## Webhook loading

```bash
set -a; source "$HOME/Documents/trading-bot/.env"; set +a
[ -n "$SLACK_WEBHOOK_URL" ] || { echo "SLACK_WEBHOOK_URL missing"; exit 1; }
```

Never echo `$SLACK_WEBHOOK_URL` into chat output, logs, or report bodies. Treat it as opaque.

## Severity tagging — emoji as scan-filter

Prepend a leading emoji so the operator can triage from a phone glance:

| Emoji | Severity | When |
|---|---|---|
| `:rotating_light:` | CRITICAL | risk governor tripped, circuit breaker active, flash-crash detected, exchange auth failed, DB down. Operator must act within 5 min. |
| `:warning:` | WARNING | drawdown approaching threshold, sentiment-accuracy degrading, model retrain failed, regime shift detected mid-position, MCP wire flapping. Operator should look within an hour. |
| `:bar_chart:` | REPORT | daily/weekly summaries, evolution updates, scheduled audits. Routine; review on next coffee. |
| `:bulb:` | INSIGHT | newly-created skill, pattern noticed across runs, parameter recommendation. No action required; archival value. |
| `:moneybag:` | TRADE | per-trade entry/exit alerts (sent by trading-bot itself, NOT by Hermes cron — listed here only for completeness). |

Heartbeat-style noise (every-15-min "all clear") MUST NOT be sent. Slack is for things the operator should read.

---

# Templates by cron job

Each Hermes cron should emit one of these. Build the JSON via heredoc; do not concatenate strings.

## 1. Risk monitor (every 15 min) — only fires when something is off

**No-news rule:** if drawdown < 5% AND no circuit breaker AND no governor pause, emit nothing. Silent is the goal. Slack channels die from heartbeat spam.

**WARNING template** (5% < DD < 8%):
```bash
curl -sS -X POST -H 'Content-Type: application/json' --data @- "$SLACK_WEBHOOK_URL" <<JSON
{
  "blocks": [
    {"type": "header", "text": {"type": "plain_text", "text": ":warning: Risk WARNING — drawdown approaching threshold"}},
    {"type": "section", "fields": [
      {"type": "mrkdwn", "text": "*Drawdown now:* ${DD_NOW}% (limit: 8%)"},
      {"type": "mrkdwn", "text": "*Δ vs 1h ago:* ${DD_DELTA_1H}%"},
      {"type": "mrkdwn", "text": "*Open positions:* ${OPEN}/${MAX_OPEN}"},
      {"type": "mrkdwn", "text": "*Daily P&L:* ${DAILY_PNL_USD} (${DAILY_PNL_PCT}%)"}
    ]},
    {"type": "section", "text": {"type": "mrkdwn", "text": "*Worst position:* ${WORST_PAIR} ${WORST_SIDE} ${WORST_PNL}% (held ${WORST_DURATION_MIN}m, regime: ${REGIME})"}},
    {"type": "section", "text": {"type": "mrkdwn", "text": "*Action:* monitor. Risk governor will auto-pause at 8%. If you want to pause now, run \`hermes chat -m hermes3:8b -q 'Call pause_trading reason=\"manual operator pre-emptive\"' -Q --yolo\`"}},
    {"type": "context", "elements": [{"type": "mrkdwn", "text": "regime: ${REGIME} (${REGIME_PROB}, ${REGIME_DURATION_H}h) · sentiment: ${SENT_SCORE} (conf ${SENT_CONF}) · tradable: ${TRADABLE_BAL_RATIO}"}]}
  ]
}
JSON
```

**CRITICAL template** (DD ≥ 8% — governor should have paused):
- Header: `:rotating_light: CRITICAL — drawdown ≥ 8%, governor pause expected`
- Required: confirm `dry_run=true` flipped (governor's signal). If FALSE, escalate harder — either the governor failed or the operator hadn't enabled it.
- Include: which position triggered, last 5 closed trades summary, 24h regime stability.
- Action line: "VERIFY governor paused. If not, run `hermes chat -m hermes3:8b -q 'Call pause_trading reason=manual-emergency' -Q --yolo`."

**Circuit breaker active template:**
- Header: `:warning: Circuit breaker active — ${LOSSES} losses in row`
- Show: cooldown_remaining_min, last 5 losing trades (pair/side/pnl/regime/sentiment at entry).
- Action: "wait for cooldown OR investigate signal degradation in `trade_journal`."

## 2. Daily P&L report (00:00 UTC)

`:bar_chart:` REPORT. Fires every day. Must be substantive even when P&L is zero (paper trading early days).

```
Header: :bar_chart: Daily P&L — ${DATE}
Section fields (4 columns):
  *Net P&L:*    ${NET_USD} (${NET_PCT}%)        — color: green if >0, red if <0, grey if 0
  *Trades:*     ${TRADES_CLOSED} closed (${TRADES_OPEN} still open)
  *Win rate:*   ${WIN_PCT}% (${WIN}W / ${LOSS}L)
  *Sharpe-30d:* ${SHARPE_30D} (Δ ${SHARPE_DELTA} vs prior)
Section (regime + sentiment):
  Regime distribution: trending_up ${TU_PCT}% · trending_down ${TD_PCT}% · mean_reverting ${MR_PCT}% · high_vol ${HV_PCT}%
  Sentiment avg: ${SENT_AVG} (${SENT_DIRECTION}) · agreement rate: ${SENT_AGREEMENT_PCT}%
Section (top trades):
  *Best:*  ${BEST_PAIR}  ${BEST_SIDE}  +${BEST_PCT}% · regime ${BEST_REGIME} · sent ${BEST_SENT} · entry confidence ${BEST_CONF}
  *Worst:* ${WORST_PAIR} ${WORST_SIDE} ${WORST_PCT}% · regime ${WORST_REGIME} · sent ${WORST_SENT} · entry confidence ${WORST_CONF}
Section (model health):
  TFT retrain: ${TFT_LAST_RETRAIN} · val_sharpe ${TFT_VAL_SHARPE}
  EPT champion: ${CHAMP_ID} (gen ${GEN}, lineage ${LINEAGE})
  Sentiment polls: ${SENT_POLLS_TODAY} (${SENT_FAIL_PCT}% failed)
Section (action / next):
  ${ACTION_LINE}     — see below
Context line:
  drawdown ${DD_NOW}% · max_dd_30d ${MAX_DD_30D}% · profit_factor_30d ${PF_30D} · readiness: ${READINESS_STATUS}
```

**Action line** rules — always one of:
- "*Action:* none. Continue paper trading. Readiness: ${TRADES_TO_GATE} trades to validation gate."
- "*Action:* review the top loss above; pattern matches ${SIMILAR_HISTORY_COUNT} prior losses in regime ${REGIME}."
- "*Action:* sentiment accuracy down ${ACCURACY_DROP}% week-on-week — see audit job."
- "*Action:* validation gate eligible (${TRADES_OUT_OF_200} trades, Sharpe ${SHARPE} ≥ 1.5). Consider go-live stage 1."

## 3. Weekly evolution + performance (Sun 00:00 UTC)

`:bar_chart:` REPORT. Comprehensive — operator reads this with their Sunday coffee.

```
Header: :bar_chart: Weekly report — week ending ${SUNDAY_DATE}
Section: This week
  P&L:        ${WK_NET_USD} (${WK_NET_PCT}%)   trend: ${TREND_VS_LAST_WK}
  Trades:     ${WK_TRADES} (${WK_WINS}W ${WK_LOSSES}L = ${WK_WINRATE}%)
  Sharpe:     ${WK_SHARPE} (4-wk avg: ${SHARPE_4W})
  MaxDD:      ${WK_MAXDD}% (4-wk max: ${MAXDD_4W}%)
  Profit factor: ${WK_PF} (4-wk: ${PF_4W})
Section: EPT evolution
  Generations completed: ${GEN_THIS_WEEK} (cumulative ${GEN_TOTAL})
  Champion changes:      ${CHAMP_CHANGES} (current: ${CURRENT_CHAMP}, lineage ${LINEAGE})
  Avg fitness:           ${AVG_FITNESS} (Δ ${FITNESS_DELTA} vs last wk)
  Demoted agents:        ${DEMOTED_LIST}
Section: Regime distribution (this week)
  trending_up    ${TU_HRS}h (${TU_PCT}%)
  trending_down  ${TD_HRS}h (${TD_PCT}%)
  mean_reverting ${MR_HRS}h (${MR_PCT}%)
  high_volatility ${HV_HRS}h (${HV_PCT}%)
  Most-traded regime: ${TOP_REGIME} (${TOP_REGIME_TRADES} trades, ${TOP_REGIME_PNL} P&L)
Section: Pair performance (sorted by P&L)
  ${PAIR1}: ${PAIR1_TRADES}t  ${PAIR1_PNL_USD}  (${PAIR1_PNL_PCT}%)  win ${PAIR1_WIN}%
  ${PAIR2}: ${PAIR2_TRADES}t  ${PAIR2_PNL_USD}  (${PAIR2_PNL_PCT}%)  win ${PAIR2_WIN}%
  (... all whitelisted pairs)
Section: Sentiment accuracy (entries vs outcomes)
  Predicted-up trades that closed positive:  ${SENT_UP_HIT}/${SENT_UP_TOTAL} (${SENT_UP_ACC}%)
  Predicted-down trades that closed positive: ${SENT_DN_HIT}/${SENT_DN_TOTAL} (${SENT_DN_ACC}%)
  Trend vs last week: ${SENT_ACC_DELTA}
Section: Patterns to remember
  ${NEW_SKILLS_LIST}    — any auto-created skills this week (file paths)
  ${PATTERN_NOTES}      — any 2+ recurring loss/win patterns Hermes noticed
Section: Readiness scorecard
  Sharpe ≥ 1.5:   ${READINESS_SHARPE_ICON}   (${SHARPE_4W})
  MaxDD < 12%:    ${READINESS_DD_ICON}       (${MAXDD_4W}%)
  PF > 1.4:       ${READINESS_PF_ICON}       (${PF_4W})
  WinRate > 55%:  ${READINESS_WR_ICON}       (${WR_4W}%)
  Trades ≥ 200:   ${READINESS_TRADES_ICON}   (${TRADES_TOTAL})
  Overall: ${READINESS_OVERALL}    — READY / NOT READY (gap: ${GAP})
Context: bot uptime ${UPTIME_PCT}% · governor pauses: ${GOV_PAUSES} · MCP errors: ${MCP_ERR_COUNT}
```

## 4. EPT training daily (02:00 UTC)

`:bar_chart:` REPORT. Brief unless champion changed.

```
Header: :bar_chart: EPT training cycle — ${DATE}
Section:
  Cycle duration:   ${CYCLE_MIN}m
  Generation:       ${GEN}  (cumulative ${GEN_TOTAL})
  Champion:         ${CHAMP_ID}  (lineage ${LINEAGE})
  Champion change:  ${CHAMP_CHANGED}    — if YES, prepend :rotating_light: to header
Section: All 8 agents (sorted by fitness, with lineage)
  agent-1  fitness ${F1}  sharpe ${S1}  drawdown ${D1}%  pf ${P1}  trades ${T1}  ← (lineage)
  agent-2  ...
  ...
  agent-8  ...
Section: Action
  - none: keep paper trading
  - champion changed: explain why (e.g. "agent-7 took over from agent-4: 22% higher Sharpe over last 50 trades, lower DD")
  - any agent flagged for demotion: list them with reason
```

## 5. EPT eval+breeding (every 2 days, 02:00 UTC)

`:bar_chart:` REPORT. Always included — this is the meta-loop checkpoint.

```
Header: :bar_chart: EPT eval+breeding — generation ${GEN}
Section: Population
  Elites kept:   ${ELITE_LIST} (top 3 by fitness)
  Children bred: ${CHILD_LIST} (parents shown)
  Newcomers:     ${NEW_LIST} (random init)
  Demotion candidates (3-day rolling Sharpe < 0.5): ${DEMOTE_LIST}
Section: Fitness trend
  Generation avg: ${AVG_NOW} (Δ ${AVG_DELTA} vs ${GEN_PREV})
  Top fitness:    ${TOP_NOW} (Δ ${TOP_DELTA})
  Bottom fitness: ${BOT_NOW} (Δ ${BOT_DELTA})
Section: Champion lineage tree (last 10 generations)
  ${LINEAGE_TREE}    — text tree, e.g. agent-7 ← agent-4 ← agent-1 ← agent-3 ← ...
Section: Action
  ${ACTION_LINE}     — usually "none"; "champion stale" if no change in 7 days; "convergence detected" if std-dev of fitness <0.1
```

## 6. Sentiment accuracy audit (06:00 UTC)

`:bar_chart:` REPORT only when accuracy degrades 3 days in a row OR jumps ±10%; otherwise silent.

```
Header: :warning: Sentiment accuracy degraded 3d in a row — OR — :bulb: Sentiment accuracy improved
Section: Yesterday
  Predictions made:           ${N_PREDS}
  Aligned with closed trades: ${ALIGNED}/${N_PREDS} (${ACC_PCT}%)
  By direction: bullish ${BULL_ACC}% (${BULL_N}) · bearish ${BEAR_ACC}% (${BEAR_N})
  Per pair:    ${PER_PAIR_TABLE}
Section: Trend
  Last 3 days: ${ACC_3D_LIST}     — e.g. 62% / 58% / 51%
  Last 7 days avg: ${ACC_7D_AVG}
Section: Diagnosis
  Likely cause:  ${CAUSE}    — e.g. "news drift: Perplexity returning increasingly low-confidence headlines (avg conf ${CONF_AVG} vs ${CONF_BASELINE} baseline)"
  Affected models: ${MODEL_LIST}    — fast / deep / both?
Section: Action
  ${ACTION_LINE}    — e.g. "skill created: lower sentiment weight in regime_gating.trending_down for next 48h" with file path
```

---

## Failure handling — when the post itself fails

A successful POST returns HTTP 200 with body `ok`. Anything else:
- 4xx (auth/format) → log to `user_data/logs/hermes_mcp.log` with the response body, do NOT retry. Likely the webhook URL is misconfigured or revoked. Surface "slack: ${HTTP_CODE} — webhook misconfigured" in the next agent reply.
- 5xx / network → retry once after 5 s. If second attempt also fails, log and stop. Don't retry-loop; Slack outage shouldn't keep the agent alive.
- Body too large → Slack rejects messages > 40 KB. If the report is > 35 KB, split into a header message + replies (use `thread_ts` from the parent response).

## When Telegram is also configured

Risk monitoring (real-time) and per-trade alerts go to **Telegram** when the operator has run `hermes-mcp/setup_telegram.sh` — lower latency to the operator's pocket.
Slack stays for **structured reports** (daily / weekly / EPT / sentiment audit) — durable channel record.
If both targets apply (e.g. CRITICAL drawdown alert), send to **Telegram first** with the action line, then Slack with full context — so the operator can act from their phone before opening the laptop.

## Operator-friendly conventions

- **Numbers**: always show units. `+0.74%` not `0.0074`. `$142.18` not `142.18`. `4h 12m` not `252min` or `0.176d`.
- **Times**: always UTC, ISO-8601 short form `2026-05-08 14:12 UTC`. Never local time (operator might be traveling).
- **Pair side**: `BTC/USD long` / `BTC/USD short` — never bare ticker. Direction matters.
- **Deltas**: prefix `+` for gains, `−` (real minus, not hyphen) for losses, `Δ` for generic deltas. Color-code if Slack supports it (it doesn't well — use emoji instead).
- **Truncation**: long lists → top-3 + "(${N} more, see /trade_journal table)". Slack messages over a screen of phone = unread.
