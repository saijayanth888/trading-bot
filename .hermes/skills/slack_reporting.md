---
name: slack_reporting
trigger: "When a Hermes cron job is told to 'Send to Slack' or 'send the report to Slack', or when generating a structured operator report"
---

# How to send a report to Slack

The operator's Slack channel is reachable via an incoming webhook. The URL lives in `~/Documents/trading-bot/.env` as `SLACK_WEBHOOK_URL`. **Do not copy it into other env files** — source it on demand inside the cron job's shell.

## Loading the webhook

Every Slack-posting bash invocation must source the trading-bot env first:

```bash
set -a; source "$HOME/Documents/trading-bot/.env"; set +a
[ -n "$SLACK_WEBHOOK_URL" ] || { echo "SLACK_WEBHOOK_URL missing"; exit 1; }
```

Do not echo `$SLACK_WEBHOOK_URL` back into chat output, log files, or report bodies. Treat it as opaque.

## Two formats — pick the right one

**A) Quick text** (alerts, single-line status):
```
{"text": "<plain text — supports *bold*, _italic_, and Slack mrkdwn links>"}
```

**B) Block Kit** (structured reports — daily P&L, weekly evolution, multi-section):
```
{"blocks": [
  {"type": "header", "text": {"type": "plain_text", "text": "Daily P&L — 2026-05-08"}},
  {"type": "section", "fields": [
    {"type": "mrkdwn", "text": "*Trades:* 7"},
    {"type": "mrkdwn", "text": "*Net P&L:* +$142.18 (+0.74%)"}
  ]},
  {"type": "divider"},
  {"type": "section", "text": {"type": "mrkdwn", "text": "*Best:* SOL/USD long +2.3% (regime: trending_up)\n*Worst:* ETH/USD short −0.8% (regime: mean_reverting)"}},
  {"type": "context", "elements": [{"type": "mrkdwn", "text": "Sharpe-30d: 1.42 · MaxDD-30d: −4.1% · WinRate: 58%"}]}
]}
```

## How to post

Quick text:
```bash
set -a; source "$HOME/Documents/trading-bot/.env"; set +a
curl -sS -X POST -H 'Content-Type: application/json' \
  --data '{"text": "Risk WARNING: portfolio drawdown 5.4% (threshold 5%). 4 open positions."}' \
  "$SLACK_WEBHOOK_URL"
```

Block Kit (build the JSON with a heredoc to avoid quoting hell):
```bash
set -a; source "$HOME/Documents/trading-bot/.env"; set +a
curl -sS -X POST -H 'Content-Type: application/json' --data @- "$SLACK_WEBHOOK_URL" <<'JSON'
{
  "blocks": [
    {"type": "header", "text": {"type": "plain_text", "text": "Weekly Evolution — Gen 14"}},
    {"type": "section", "text": {"type": "mrkdwn", "text": "*Champion:* agent-7 (lineage: 7→4→1)\n*Generation P&L:* +$687.40 (+3.6% on $19k)\n*Best Sharpe:* 1.78 (agent-7) · *Worst:* −0.42 (agent-3, flagged)"}},
    {"type": "section", "fields": [
      {"type": "mrkdwn", "text": "*Trades:* 41"},
      {"type": "mrkdwn", "text": "*Win rate:* 56%"},
      {"type": "mrkdwn", "text": "*MaxDD:* −5.8%"},
      {"type": "mrkdwn", "text": "*PF:* 1.62"}
    ]}
  ]
}
JSON
```

A successful POST returns `ok` with HTTP 200. Anything else: report curl exit code + HTTP status to the operator and stop — do not retry-loop on 4xx (the webhook URL is misconfigured).

## Severity tagging convention

Prepend a leading emoji so the operator can scan-filter:
- `:rotating_light:` CRITICAL — risk governor tripped, circuit breaker active, flash-crash detected
- `:warning:` WARNING — drawdown approaching threshold, sentiment accuracy degrading, model retrain failed
- `:bar_chart:` REPORT — daily/weekly summaries, evolution updates, scheduled audits
- `:bulb:` INSIGHT — newly-created skill, pattern noticed across runs

Do NOT add emoji for routine heartbeat-style logging. Slack is for things the operator should read.

## When Telegram is also available

Risk monitoring (real-time) and trade alerts go to **Telegram** when configured (see `hermes-mcp/setup_telegram.sh`). Slack stays for **structured reports** (daily/weekly summaries, evolution, audits). If both targets apply, Telegram first (lower latency to the operator's pocket), Slack second (durable channel record).
