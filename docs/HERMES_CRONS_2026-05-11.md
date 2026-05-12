# Hermes crons — 2026-05-11 deterministic conversion

**Status:** 8 LLM-driven Hermes crons converted to `no_agent=true` shell wrappers
+ `shark_pre_execute` registered + `shark_briefing_alerts` added.
Coordinator must `hermes cron reload` (or `systemctl reload hermes`) after
merging this doc — this branch does NOT restart Hermes.

**Why the conversion was needed:** the LLM-driven crons hallucinated numbers
(see POST_CUTOVER_FIXES_2026-05-11.md §3). Examples observed live:

- `risk_monitor_15min` 2026-05-11 12:47 claimed `drawdown −6.2%, governor
  paused` — reality: `−1.23%, governor not paused, breaker_active=false`.
- `daily_pnl_report` 2026-05-11 00:26 claimed `−$1,205.87, Sharpe −0.93,
  win-rate 40% (4/10)` — reality: `−$66.39, 2 trades total, no Sharpe possible
  (n<5)`.

The fix replaces each LLM prompt with a Python block that reads the
deterministic source of truth (`unified_risk`, `mcp_local`, Postgres) and
posts a terse 4-question Slack message: WHAT happened, GOOD/BAD severity,
WHAT changed vs last run, DO I need to act NOW.

The scripts live out-of-tree at `~/.hermes/scripts/` (chmod +x). Cron
registration lives at `~/.hermes/cron/jobs.json`. This doc is the
canonical reproducible spec — if `~/.hermes/` is wiped, copy the 9 scripts
below back into `~/.hermes/scripts/` and apply the jobs.json patch in §3.

---

## 1. The 9 crons

| Name | Schedule (cron expr) | Data source | Output channel | Severity icons used |
|---|---|---|---|---|
| `risk_monitor_15min`       | `*/15 * * * *`     | `unified_risk.get_combined_risk_status()` | Slack webhook + Hermes `deliver:local` | `:bell: :warning: :rotating_light:` |
| `daily_pnl_report`         | `0 0 * * *`        | Postgres `trade_journal` + `regime_log`   | Slack webhook                          | `:bell: :warning: :rotating_light:` |
| `weekly_evolution_report`  | `0 0 * * 0`        | `mcp_local.get_evolution_status()` + `get_champion_genome()` | Slack webhook | `:bell: :warning:` |
| `sentiment_accuracy_audit` | `0 6 * * *`        | Postgres `trade_journal` joined to `sentiment_log` | Slack webhook | `:bell: :warning: :rotating_light:` |
| ~~`ept_eval_breeding`~~     | ~~`every 2160m`~~ | **RETIRED 2026-05-12** — paused; ModelForge supersedes (see `docs/MODELFORGE_INTEGRATION_PLAN.md` §EPT retirement) | — | — |
| ~~`ept_training_daily`~~    | ~~`0 2 * * *`~~   | **RETIRED 2026-05-12** — paused; was emitting deterministic mock-mode output (champion `gen0-011`, fitness `0.7540`) every night | — | — |
| `post_mortem_weekly`       | `0 1 * * 0`        | Postgres `trade_journal` clustered by (regime, exit_reason) | Slack webhook | `:bell: :warning: :rotating_light:` |
| `market_research_30min`    | `*/30 * * * *`     | Postgres `sentiment_log`, `news_headlines`, `fear_greed_log`, `regime_log` — divergence score | Slack webhook (suppressed unless actionable) | `:bell: :warning:` |
| `shark_briefing_alerts`    | `15 9 * * 1-5`     | `stocks/memory/DAILY-HANDOFF.md` last phase block | Slack webhook | `:bell: :warning: :rotating_light:` |
| `shark_pre_execute`        | `30 9 * * 1-5`     | `python shark/run.py pre-execute` | Hermes `deliver:telegram` | shark phase script handles internally |

**Severity convention** (project memory `feedback_dashboard_design.md` & operator
preferences): `:bell:` = INFO, `:warning:` = SOFT/alert, `:rotating_light:` =
CRITICAL. Every Slack message is tagged `[<cron_name>]` so the operator knows
which job fired, plus a current ET timestamp.

Each script's message answers four operator questions:
1. WHAT happened — terse one-line summary of current state
2. GOOD or BAD — encoded via severity emoji
3. WHAT changed — delta vs previous-run state file (`~/.hermes/state-snapshots/`)
4. DO I need to act NOW — explicit `act:` line per script

Sample output of each script verified manually (2026-05-11 ~14:00 ET):

```
:rotating_light: [risk_monitor_15min] CRITICAL 13:57 ET
dd combined=+0.00% thresh=10.0% | breaker=True
crypto_dd=+0.00% stocks_dd=+0.00% eq=$119,000 pos=0
:warning: stocks snapshot stale (age=1365s)
changed: no material change | act: INSPECT: trading should be paused. Check governor.

:warning: [daily_pnl_report] 2026-05-10 ET
day P&L: $-66.39 (2 trades, 0W/2L, wr=0%)
vs prev (2026-05-09): $+0.00 (0t, 0W/0L) → Δ $-66.39
cum P&L: $-66.39 over 2 trades | regime now: trending_up (1.00)
regime mix today: trending_up:2
worst: SOL/USD $-43.02 (freqai_down_regime)
act: review losing trades; check sentiment/regime alignment

:bell: [weekly_evolution_report] 2026-05-11 13:58 ET
gen=1 champion=gen0-011
alive members: 12
top 3:
  gen0-011 fit=0.7540 sharpe=0.88 pf=1.64 n=66 <- champ
  gen1-r02 fit=0.7283 sharpe=0.90 pf=1.65 n=68
  gen1-c00 fit=0.6392 sharpe=0.95 pf=1.67 n=59
act: none — keep evolving

:bell: [sentiment_accuracy_audit] 2026-05-11 13:58 ET (INSUFFICIENT_DATA)
closed trades (3d): 2 | w/ sentiment: 0 | neutral: 0
directional accuracy: — (no labeled trades)
market sent avg (3d): +0.01 over 358 samples
F&G now: 48 (Neutral)
act: no labeled trades — sentiment likely not in trade_journal yet

:bell: [ept_eval_breeding] 2026-05-11 13:59 ET (SOME_WEAK)
gen=1 alive=12 champion=gen0-011
strong (sharpe>=0.5 & n>=5): 8 | weak: 4
flagged for demotion:
  gen0-001 fit=0.243 sharpe=0.35 n=43
  gen0-007 fit=0.237 sharpe=0.44 n=66
  …
act: none — next generation will breed past these

:warning: [post_mortem_weekly] 7-day window @ 2026-05-11 13:59 ET
closed: 2 (0W / 2L, wr=0%)
total loss (losers only): $-66.39
worst trade: SOL/USD $-43.02 (trending_up, freqai_down_regime)
top loss clusters (regime, exit_reason):
  trending_up / freqai_down_regime: n=2 loss=$-66.39 (worst pair: SOL/USD $-43.02)
recs (manual review):
  • check FreqAI down-regime threshold in trending_up
act: review losers manually before next week

:bell: [market_research_30min] 14:00 ET
LLM sent: +0.15 | Reddit comm: +0.74 | F&G: 48 (Neutral)
divergence: 0.39 (>0.5 = actionable)
regime: trending_up (1.00)
headlines last 30m: 3
trending: OSMO, ZANO, VVV, WOJAK, SUI
changed: no material change | act: none
  (this run is suppressed from Slack — only actionable runs post)

:rotating_light: [shark_briefing_alerts] CRITICAL 14:02 ET
phase: pre-market @ 09:14 EDT
confirmed: NVDA
skipped: GOOGL, AMD, CCJ, CRDO, XOM
regime: BEAR_VOLATILE | macro: ELEVATED | breadth: bullish=9 bearish=1 of 30
changed: new phase posted
act: BEAR + extreme macro — verify no new longs queued
```

---

## 2. Rebuild instructions (from clean Hermes install)

```bash
# 1. Ensure ~/.hermes/scripts/ exists
mkdir -p ~/.hermes/scripts ~/.hermes/state-snapshots

# 2. Copy each of the 9 scripts in §4 below to ~/.hermes/scripts/<name>.sh
#    (or extract from the inline blocks below)

chmod +x ~/.hermes/scripts/risk_monitor_15min.sh \
         ~/.hermes/scripts/daily_pnl_report.sh \
         ~/.hermes/scripts/weekly_evolution_report.sh \
         ~/.hermes/scripts/sentiment_accuracy_audit.sh \
         ~/.hermes/scripts/ept_eval_breeding.sh \
         ~/.hermes/scripts/post_mortem_weekly.sh \
         ~/.hermes/scripts/market_research_30min.sh \
         ~/.hermes/scripts/shark_briefing_alerts.sh
# shark_pre_execute.sh already lives at ~/.hermes/scripts/ (committed
# previously; just needs the cron entry in jobs.json).

# 3. Patch ~/.hermes/cron/jobs.json (see §3 for the exact diff)

# 4. Reload Hermes
hermes cron reload   # or `systemctl reload hermes` depending on install

# 5. Verify
hermes cron list | grep -E "risk_monitor_15min|daily_pnl_report|weekly_evolution_report|sentiment_accuracy_audit|ept_eval_breeding|post_mortem_weekly|market_research_30min|shark_briefing_alerts|shark_pre_execute"
# All 9 should show with `script:` set and `no_agent: true`

# 6. Manually fire one to sanity-check
bash ~/.hermes/scripts/risk_monitor_15min.sh
```

---

## 3. `~/.hermes/cron/jobs.json` patch

For each of the 7 existing LLM jobs, set:
```json
"no_agent": true,
"script": "<name>.sh"
```
(leave `prompt` as-is for paper trail — it's ignored when `no_agent=true`).

Then **append** these two new job entries (template from
`shark_market_open` entry — schedule.kind=cron, schedule_display, etc.):

```json
{
  "id": "shark_briefing_alerts_b1",
  "name": "shark_briefing_alerts",
  "prompt": "",
  "skills": [],
  "skill": null,
  "model": null,
  "provider": null,
  "base_url": null,
  "script": "shark_briefing_alerts.sh",
  "no_agent": true,
  "context_from": null,
  "schedule": { "kind": "cron", "expr": "15 9 * * 1-5", "display": "15 9 * * 1-5" },
  "schedule_display": "15 9 * * 1-5",
  "repeat": { "times": null, "completed": 0 },
  "enabled": true,
  "state": "scheduled",
  "paused_at": null,
  "paused_reason": null,
  "created_at": "2026-05-11T14:00:00-04:00",
  "next_run_at": null,
  "last_run_at": null,
  "last_status": null,
  "last_error": null,
  "last_delivery_error": null,
  "deliver": "local",
  "origin": null,
  "enabled_toolsets": null,
  "workdir": "/home/saijayanthai/Documents/trading-bot"
},
{
  "id": "shark_pre_execute_b1",
  "name": "shark_pre_execute",
  "prompt": "",
  "skills": [],
  "skill": null,
  "model": null,
  "provider": null,
  "base_url": null,
  "script": "shark_pre_execute.sh",
  "no_agent": true,
  "context_from": null,
  "schedule": { "kind": "cron", "expr": "30 9 * * 1-5", "display": "30 9 * * 1-5" },
  "schedule_display": "30 9 * * 1-5",
  "repeat": { "times": null, "completed": 0 },
  "enabled": true,
  "state": "scheduled",
  "paused_at": null,
  "paused_reason": null,
  "created_at": "2026-05-11T14:00:00-04:00",
  "next_run_at": null,
  "last_run_at": null,
  "last_status": null,
  "last_error": null,
  "last_delivery_error": null,
  "deliver": "telegram",
  "origin": null,
  "enabled_toolsets": null,
  "workdir": "/home/saijayanthai/Documents/trading-bot/stocks"
}
```

A backup of the pre-change file is saved at
`~/.hermes/cron/jobs.json.backup-pre-cron-conversion-<UTC-timestamp>`
each time this patch is applied.

---

## 4. Script source (full text)

Each script:
- Sources `/home/saijayanthai/Documents/trading-bot/.env` for `POSTGRES_PASSWORD`,
  `SLACK_WEBHOOK_URL`, `HERMES_MCP_KEY`.
- Overrides `POSTGRES_HOST=localhost` and `POSTGRES_PORT=5434` (TimescaleDB
  host-port-forward) because `ops_db.py` defaults to `postgres` (docker DNS).
- Writes a state file under `~/.hermes/state-snapshots/<name>_last.json` so
  the next run can compute deltas / suppress unchanged OK runs.
- Logs to `user_data/logs/cron-<name>.log` for post-mortem.
- Always exits 0 on internal failures (logging the error to Slack) so the
  Hermes scheduler keeps trying.

### 4.1 `risk_monitor_15min.sh`

Calls `user_data.modules.unified_risk.get_combined_risk_status()` directly.
That function returns `combined_drawdown_pct` (already in percent — the
`pnl_pct fraction-vs-percent` bug from §5 B-2 does NOT apply here),
`circuit_breaker_active`, and per-side equity. Severity ladder:
- `breaker_active=true` or `|dd| >= threshold_pct` → `:rotating_light:` CRITICAL
- `|dd| >= threshold_pct/2` or `stocks_data_untrusted` → `:warning:` SOFT
- `stocks_data_stale` (but trusted) → `:warning:` STALE
- otherwise → `:bell:` OK (suppressed from Slack unless breaker just flipped)

Suppressing OK runs avoids 96 Slack posts/day; SOFT/CRITICAL/STALE always post.

Full source in **Appendix A.4.1** below — paste into
`~/.hermes/scripts/risk_monitor_15min.sh` and `chmod +x` to reproduce.

### 4.2 `daily_pnl_report.sh`

Queries `trade_journal` for two windows (yesterday in ET, day-before-yesterday)
plus cumulative all-time. Reports day P&L, count, wins/losses, win-rate, regime
mix, worst/best trade, regime-now. No Sharpe (need n>=5 — was the LLM's
favourite hallucination). Severity by absolute day P&L.

### 4.3 `weekly_evolution_report.sh`

Reads `user_data/models/evolution/` via `mcp_local.get_evolution_status()`.
Reports generation, champion, alive count, top-3 leaderboard with
sharpe/pf/num_trades, and week-over-week fitness delta from state file.
Severity bumps to `:warning:` when champion changes (operator must verify
before adopting).

### 4.4 `sentiment_accuracy_audit.sh`

Joins `trade_journal.sentiment_score` (entry signal) to `pnl` (outcome) over
last 3 days. Directional accuracy = positive_sent→positive_pnl OR
negative_sent→negative_pnl. Auxiliary: 3-day sentiment_log average + current
F&G. Severity:
- `acc < 40%` → `:rotating_light:` POOR (worse than coin-flip)
- `acc < 55%` → `:warning:` WEAK
- otherwise → `:bell:` OK

### 4.5 `ept_eval_breeding.sh`

Walks the EPT population from `mcp_local.get_evolution_status()`. Flags any
member with `sharpe<0.5` OR `num_trades<5` for demotion (informational —
demotion happens in the next training run). Reports strong vs weak count and
the first 5 weak members.

### 4.6 `post_mortem_weekly.sh`

Reads 7 days of closed trades, clusters losing trades by `(regime, exit_reason)`,
sorts by total $-loss, shows top 3 clusters with their worst pair. Provides
text recommendations (loosen stop / re-eval entry / investigate) — NEVER
auto-applies. Severity by total weekly loss.

### 4.7 `market_research_30min.sh`

Pulls last row of `sentiment_log` (LLM market score, Reddit community avg,
F&G, trending_pairs), `regime_log` (current regime), and counts headlines in
the last 30 min. Computes a divergence score = `mean(|llm − fg_normalised|,
|llm − comm|)`. Posts to Slack only when:
- divergence > 0.5, OR
- F&G crossed an extreme threshold (25 or 75) since last run, OR
- regime changed since last run.

This is the only cron with hard Slack suppression — 48 runs/day would spam.

### 4.8 `shark_briefing_alerts.sh`

Parses `stocks/memory/DAILY-HANDOFF.md` last phase block (regex on
`## phase | HH:MM EDT` headers). Extracts confirmed, skipped, regime, macro,
breadth, lessons. Severity:
- `regime starts with BEAR` AND `macro in {ELEVATED, EXTREME, HIGH}` → `:rotating_light:`
- either alone → `:warning:` BEAR_OVERRIDE
- otherwise → `:bell:` OK

Slack post only when new phase block appears OR severity != OK.

### 4.9 `shark_pre_execute.sh` (§6 — registration only)

This script already existed in the tree (`~/.hermes/scripts/shark_pre_execute.sh`,
created 2026-05-10 14:38). The audit found it was orphaned: no
`~/.hermes/cron/jobs.json` entry pointed at it, so the 09:30 ET pre-execute
phase was silently skipped. The cron registration in §3 above wires it up.

It is a thin shell wrapper that activates the stocks venv and runs
`python shark/run.py pre-execute` (the Python phase IS the agent — Ollama-only
LLM calls via `shark.llm.client`, no Anthropic spend). Output filtered for
`DECISION|ENTRY|EXIT|TRADE|ERROR|FAILED|stop|circuit|KILL` lines goes to both
Hermes telegram delivery and the Slack webhook.

---

## 5. Files NOT committed but described here

Per coordinator's instructions, the actual `.sh` files live OUTSIDE this repo
at `~/.hermes/scripts/`. Each is reproducible from the inline blocks above
plus the audit-as-you-write commits on branch `agent-b/llm-cron-conversion`.
If `~/.hermes/` is wiped, the operator can regenerate by reading this doc and
the post-cutover spec in `POST_CUTOVER_FIXES_2026-05-11.md` §3.

Backup files preserved during this rollout:
- `~/.hermes/cron/jobs.json.backup-pre-cron-conversion-20260511T140248` (pre-edit)

---

## 6. Coordinator next steps

1. Merge `agent-b/llm-cron-conversion` into the integration branch (or main).
2. `hermes cron reload` (or `systemctl reload hermes`).
3. Watch `~/.hermes/cron/output/` for the next firing of each cron — confirm
   numbers match `unified_risk.get_combined_risk_status()` for risk_monitor and
   the dashboard for daily_pnl.
4. Optional: prune the old `prompt` field on the 7 converted crons after the
   first successful cycle (operator preference — keeping for now as a paper trail).

The 8 LLM-driven crons no longer hallucinate. Operator will now see real risk
alerts on schedule for the first time since the cutover.


---

## Appendix A — Full script text (reproducible)

If `~/.hermes/scripts/` is wiped, paste each block below into the named file,
chmod +x, and the cron will resume. Each script is self-contained.

### Appendix 4.1 — `risk_monitor_15min.sh`

```bash
#!/usr/bin/env bash
# risk_monitor_15min.sh — deterministic replacement for LLM-driven risk cron.
#
# Why no LLM: the previous prompt asked Hermes-3 to "check risk status via MCP"
# but the model hallucinated numbers (e.g. claimed "drawdown -6.2%" on
# 2026-05-11 12:47 when reality was -1.23%, governor not paused). Operator
# never saw real alerts.
#
# This script calls user_data.modules.unified_risk.get_combined_risk_status()
# directly and posts a terse 4-question Slack message.
#
# Severity ladder:
#   |dd| < threshold/2  → :bell: INFO (only post if breaker_active or stale)
#   |dd| >= threshold/2 → :warning: SOFT
#   |dd| >= threshold   → :rotating_light: CRITICAL
#   breaker_active=true → :rotating_light: CRITICAL regardless of dd
#   stocks_data_untrusted → :warning: SOFT

set -uo pipefail

REPO=/home/saijayanthai/Documents/trading-bot
LOG=$REPO/user_data/logs/cron-risk-monitor.log
mkdir -p "$(dirname "$LOG")"

set -a
# shellcheck disable=SC1091
[[ -f "$REPO/.env" ]] && source "$REPO/.env"
set +a

# Host-side script: ops_db.py defaults to host='postgres' (docker network).
# Override to localhost:5434 (TimescaleDB host-port-forward).
export POSTGRES_HOST="${POSTGRES_HOST:-localhost}"
export POSTGRES_PORT="${POSTGRES_PORT:-5434}"

cd "$REPO"

ts=$(date -u +%Y-%m-%dT%H:%M:%SZ)
echo "── risk_monitor_15min $ts ──" >>"$LOG"

python3 - <<'PY' 2>>"$LOG"
import json, os, sys, urllib.request
from datetime import datetime
from zoneinfo import ZoneInfo

sys.path.insert(0, "/home/saijayanthai/Documents/trading-bot")
try:
    from user_data.modules.unified_risk import get_combined_risk_status
except Exception as exc:
    msg = f":rotating_light: [risk_monitor_15min] FAILED to import unified_risk: {exc}"
    print(msg)
    url = os.environ.get("SLACK_WEBHOOK_URL", "").strip()
    if url:
        try:
            req = urllib.request.Request(url, data=json.dumps({"text": msg}).encode(),
                                         headers={"Content-Type": "application/json"})
            urllib.request.urlopen(req, timeout=10).read()
        except Exception:
            pass
    sys.exit(0)

try:
    r = get_combined_risk_status()
except Exception as exc:
    msg = f":rotating_light: [risk_monitor_15min] get_combined_risk_status raised: {exc}"
    print(msg)
    url = os.environ.get("SLACK_WEBHOOK_URL", "").strip()
    if url:
        try:
            req = urllib.request.Request(url, data=json.dumps({"text": msg}).encode(),
                                         headers={"Content-Type": "application/json"})
            urllib.request.urlopen(req, timeout=10).read()
        except Exception:
            pass
    sys.exit(0)

dd = float(r.get("combined_drawdown_pct") or 0.0)       # already in percent
threshold = float(r.get("threshold_pct") or 10.0)        # in percent
breaker = bool(r.get("circuit_breaker_active", False))
crypto_dd = float(r.get("crypto_drawdown_pct") or 0.0)
stocks_dd = float(r.get("stocks_drawdown_pct") or 0.0)
crypto_eq = float(r.get("crypto_equity") or 0.0)
stocks_eq = float(r.get("stocks_equity") or 0.0)
total_eq = float(r.get("total_equity") or 0.0)
open_pos = int(r.get("combined_open_positions") or 0)
stale = bool(r.get("stocks_data_stale", False))
untrusted = bool(r.get("stocks_data_untrusted", False))
snap_age = r.get("snapshot_age_seconds")

# Severity
abs_dd = abs(dd)
if breaker or abs_dd >= threshold:
    icon = ":rotating_light:"
    sev = "CRITICAL"
elif abs_dd >= threshold / 2 or untrusted:
    icon = ":warning:"
    sev = "SOFT"
elif stale:
    icon = ":warning:"
    sev = "STALE"
else:
    icon = ":bell:"
    sev = "OK"

# Delta vs previous run (state file)
STATE = "/home/saijayanthai/.hermes/state-snapshots/risk_monitor_last.json"
os.makedirs(os.path.dirname(STATE), exist_ok=True)
prev_dd = None
prev_breaker = None
try:
    with open(STATE) as f:
        prev = json.load(f)
    prev_dd = prev.get("combined_drawdown_pct")
    prev_breaker = prev.get("circuit_breaker_active")
except Exception:
    pass

delta_str = ""
if prev_dd is not None:
    diff = dd - float(prev_dd)
    if abs(diff) >= 0.05:
        delta_str = f" (Δ{diff:+.2f}pp)"

# Save current state
try:
    with open(STATE, "w") as f:
        json.dump({
            "ts": datetime.utcnow().isoformat() + "Z",
            "combined_drawdown_pct": dd,
            "circuit_breaker_active": breaker,
        }, f)
except Exception:
    pass

et_now = datetime.now(ZoneInfo("America/New_York")).strftime("%H:%M ET")

# Only suppress posting on plain-OK runs to avoid spam (every 15 min = 96/day).
# Always post on SOFT/CRITICAL/STALE, or when breaker state flipped.
breaker_flipped = (prev_breaker is not None and bool(prev_breaker) != breaker)
should_post = sev != "OK" or breaker_flipped

# Build compact phone-friendly message
lines = [
    f"{icon} [risk_monitor_15min] {sev} {et_now}",
    f"dd combined={dd:+.2f}%{delta_str} thresh={threshold:.1f}% | breaker={breaker}",
    f"crypto_dd={crypto_dd:+.2f}% stocks_dd={stocks_dd:+.2f}% eq=${total_eq:,.0f} pos={open_pos}",
]
if untrusted:
    lines.append(f":warning: stocks snapshot UNTRUSTED (age={snap_age}s) — combined-dd is crypto-only")
elif stale:
    lines.append(f":warning: stocks snapshot stale (age={snap_age}s)")

# 4-question framing
what_changed = "breaker FLIPPED" if breaker_flipped else (f"dd moved {delta_str.strip()}" if delta_str else "no material change")
if breaker or abs_dd >= threshold:
    do_now = "INSPECT: trading should be paused. Check governor."
elif abs_dd >= threshold / 2:
    do_now = "WATCH: dd at half threshold, no auto-action yet."
elif untrusted:
    do_now = "CHECK wheel_snapshot cron — stocks data dark."
else:
    do_now = "none"
lines.append(f"changed: {what_changed} | act: {do_now}")

msg = "\n".join(lines)
print(msg)

if not should_post:
    print("[risk_monitor_15min] state OK and unchanged — skipping Slack post", file=sys.stderr)
    sys.exit(0)

url = os.environ.get("SLACK_WEBHOOK_URL", "").strip()
if not url:
    print("[risk_monitor_15min] no SLACK_WEBHOOK_URL — skipping Slack post", file=sys.stderr)
    sys.exit(0)
try:
    req = urllib.request.Request(url, data=json.dumps({"text": msg}).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=10) as r:
        print(f"[risk_monitor_15min] slack status={r.status}", file=sys.stderr)
except Exception as exc:
    print(f"[risk_monitor_15min] slack post failed: {exc}", file=sys.stderr)
PY

echo "── risk_monitor_15min done $(date -u +%Y-%m-%dT%H:%M:%SZ) ──" >>"$LOG"
```

### Appendix 4.2 — `daily_pnl_report.sh`

```bash
#!/usr/bin/env bash
# daily_pnl_report.sh — deterministic replacement for LLM-driven P&L cron.
#
# Why no LLM: previous Hermes-3 70B prompt hallucinated "$1,205.87 down,
# Sharpe -0.93, win rate 40% (4/10)" when reality was -$66.39 cumulative
# with only 2 closed trades total — no Sharpe possible (n<5).
#
# This script reads trade_journal + regime_log directly from Postgres,
# computes the exact day-over-day numbers, and posts terse Slack.

set -uo pipefail

REPO=/home/saijayanthai/Documents/trading-bot
LOG=$REPO/user_data/logs/cron-daily-pnl.log
mkdir -p "$(dirname "$LOG")"

set -a
# shellcheck disable=SC1091
[[ -f "$REPO/.env" ]] && source "$REPO/.env"
set +a

export POSTGRES_HOST="${POSTGRES_HOST:-localhost}"
export POSTGRES_PORT="${POSTGRES_PORT:-5434}"

cd "$REPO"

ts=$(date -u +%Y-%m-%dT%H:%M:%SZ)
echo "── daily_pnl_report $ts ──" >>"$LOG"

python3 - <<'PY' 2>>"$LOG"
import json, os, sys, statistics, urllib.request
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import psycopg

dsn = dict(
    host=os.environ.get("POSTGRES_HOST", "localhost"),
    port=int(os.environ.get("POSTGRES_PORT", "5434")),
    user=os.environ.get("POSTGRES_USER", "tradebot"),
    password=os.environ.get("POSTGRES_PASSWORD", ""),
    dbname=os.environ.get("POSTGRES_DB", "tradebot"),
    connect_timeout=5,
)

et = ZoneInfo("America/New_York")
now_et = datetime.now(et)
# "Today" boundaries in ET (this cron fires at 00:00 ET, so "yesterday" = the
# day that just ended; cover both same-day and the closed day).
day_end = now_et.replace(hour=0, minute=0, second=0, microsecond=0)
day_start = day_end - timedelta(days=1)            # closed day
prev_start = day_end - timedelta(days=2)            # day before that

def to_utc(d):
    return d.astimezone(timezone.utc)

day_pnl = day_count = day_wins = day_losses = 0
prev_pnl = prev_count = prev_wins = prev_losses = 0
regime_mix = {}
worst = best = None
err = None

try:
    with psycopg.connect(**dsn) as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT pair, pnl, pnl_pct, regime, exit_reason, closed_at
            FROM trade_journal
            WHERE closed_at >= %s AND closed_at < %s
            ORDER BY closed_at
            """,
            (to_utc(day_start), to_utc(day_end)),
        )
        rows = cur.fetchall()
        for pair, pnl, pnl_pct, regime, exit_reason, closed_at in rows:
            pnl = float(pnl or 0)
            day_pnl += pnl
            day_count += 1
            if pnl > 0:
                day_wins += 1
            elif pnl < 0:
                day_losses += 1
            regime_mix[regime or "unknown"] = regime_mix.get(regime or "unknown", 0) + 1
            if worst is None or pnl < worst[0]:
                worst = (pnl, pair, exit_reason)
            if best is None or pnl > best[0]:
                best = (pnl, pair, exit_reason)

        cur.execute(
            """
            SELECT COUNT(*) AS n,
                   COALESCE(SUM(pnl), 0) AS s,
                   COUNT(*) FILTER (WHERE pnl > 0) AS w,
                   COUNT(*) FILTER (WHERE pnl < 0) AS l
            FROM trade_journal
            WHERE closed_at >= %s AND closed_at < %s
            """,
            (to_utc(prev_start), to_utc(day_start)),
        )
        row = cur.fetchone()
        prev_count = int(row[0] or 0)
        prev_pnl = float(row[1] or 0)
        prev_wins = int(row[2] or 0)
        prev_losses = int(row[3] or 0)

        # Cumulative (all-time closed)
        cur.execute("SELECT COUNT(*), COALESCE(SUM(pnl), 0) FROM trade_journal WHERE closed_at IS NOT NULL")
        cum_n, cum_pnl = cur.fetchone()
        cum_n = int(cum_n or 0)
        cum_pnl = float(cum_pnl or 0)

        # Current regime
        cur.execute("SELECT regime, probability FROM regime_log ORDER BY ts DESC LIMIT 1")
        row = cur.fetchone()
        cur_regime = row[0] if row else "unknown"
        cur_prob = float(row[1] or 0) if row else 0.0
except Exception as exc:
    err = exc

if err is not None:
    msg = f":rotating_light: [daily_pnl_report] DB error: {err}"
    print(msg)
    url = os.environ.get("SLACK_WEBHOOK_URL", "").strip()
    if url:
        try:
            req = urllib.request.Request(url, data=json.dumps({"text": msg}).encode(),
                                         headers={"Content-Type": "application/json"})
            urllib.request.urlopen(req, timeout=10).read()
        except Exception:
            pass
    sys.exit(0)

date_label = day_start.strftime("%Y-%m-%d")
prev_label = prev_start.strftime("%Y-%m-%d")
day_wr = (day_wins / day_count * 100) if day_count else 0.0
prev_wr = (prev_wins / prev_count * 100) if prev_count else 0.0
pnl_delta = day_pnl - prev_pnl

# Severity by day P&L
if day_pnl < -100:
    icon = ":rotating_light:"
elif day_pnl < 0:
    icon = ":warning:"
else:
    icon = ":bell:"

mix_str = ", ".join(f"{k}:{v}" for k, v in sorted(regime_mix.items())) or "—"

lines = [
    f"{icon} [daily_pnl_report] {date_label} ET",
    f"day P&L: ${day_pnl:+,.2f} ({day_count} trades, {day_wins}W/{day_losses}L, wr={day_wr:.0f}%)",
    f"vs prev ({prev_label}): ${prev_pnl:+,.2f} ({prev_count}t, {prev_wins}W/{prev_losses}L) → Δ ${pnl_delta:+,.2f}",
    f"cum P&L: ${cum_pnl:+,.2f} over {cum_n} trades | regime now: {cur_regime} ({cur_prob:.2f})",
]
if regime_mix:
    lines.append(f"regime mix today: {mix_str}")
if worst:
    lines.append(f"worst: {worst[1]} ${worst[0]:+,.2f} ({worst[2]})")
if best and day_count > 1:
    lines.append(f"best:  {best[1]} ${best[0]:+,.2f} ({best[2]})")

# 4-question framing
if day_count == 0:
    do_now = "no closes today — verify trading not silently paused"
elif day_pnl < 0:
    do_now = "review losing trades; check sentiment/regime alignment"
else:
    do_now = "none — keep monitoring"
lines.append(f"act: {do_now}")

msg = "\n".join(lines)
print(msg)

url = os.environ.get("SLACK_WEBHOOK_URL", "").strip()
if not url:
    print("[daily_pnl_report] no SLACK_WEBHOOK_URL — skipping Slack post", file=sys.stderr)
    sys.exit(0)
try:
    req = urllib.request.Request(url, data=json.dumps({"text": msg}).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=10) as r:
        print(f"[daily_pnl_report] slack status={r.status}", file=sys.stderr)
except Exception as exc:
    print(f"[daily_pnl_report] slack post failed: {exc}", file=sys.stderr)
PY

echo "── daily_pnl_report done $(date -u +%Y-%m-%dT%H:%M:%SZ) ──" >>"$LOG"
```

### Appendix 4.3 — `weekly_evolution_report.sh`

```bash
#!/usr/bin/env bash
# weekly_evolution_report.sh — deterministic replacement for LLM-driven weekly cron.
#
# Reads user_data.dashboard.mcp_local.get_evolution_status() (which reads
# user_data/models/evolution/<gen>/*.json directly) and posts the EPT
# champion + leaderboard top 3 + week-over-week fitness delta.

set -uo pipefail

REPO=/home/saijayanthai/Documents/trading-bot
LOG=$REPO/user_data/logs/cron-weekly-evolution.log
mkdir -p "$(dirname "$LOG")"

set -a
# shellcheck disable=SC1091
[[ -f "$REPO/.env" ]] && source "$REPO/.env"
set +a

export POSTGRES_HOST="${POSTGRES_HOST:-localhost}"
export POSTGRES_PORT="${POSTGRES_PORT:-5434}"

cd "$REPO"

ts=$(date -u +%Y-%m-%dT%H:%M:%SZ)
echo "── weekly_evolution_report $ts ──" >>"$LOG"

python3 - <<'PY' 2>>"$LOG"
import json, os, sys, urllib.request
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

sys.path.insert(0, "/home/saijayanthai/Documents/trading-bot")

try:
    from user_data.dashboard.mcp_local import get_evolution_status, get_champion_genome
except Exception as exc:
    msg = f":rotating_light: [weekly_evolution_report] import failed: {exc}"
    print(msg)
    url = os.environ.get("SLACK_WEBHOOK_URL", "").strip()
    if url:
        try:
            req = urllib.request.Request(url, data=json.dumps({"text": msg}).encode(),
                                         headers={"Content-Type": "application/json"})
            urllib.request.urlopen(req, timeout=10).read()
        except Exception:
            pass
    sys.exit(0)

try:
    evo = get_evolution_status()
    champ = get_champion_genome()
except Exception as exc:
    msg = f":rotating_light: [weekly_evolution_report] eval failed: {exc}"
    print(msg)
    url = os.environ.get("SLACK_WEBHOOK_URL", "").strip()
    if url:
        try:
            req = urllib.request.Request(url, data=json.dumps({"text": msg}).encode(),
                                         headers={"Content-Type": "application/json"})
            urllib.request.urlopen(req, timeout=10).read()
        except Exception:
            pass
    sys.exit(0)

generation = evo.get("generation")
alive = evo.get("alive") or []
# Sort by fitness desc
alive_sorted = sorted(alive, key=lambda m: float(m.get("fitness") or 0), reverse=True)
top3 = alive_sorted[:3]
champ_id = evo.get("champion") or champ.get("member_id") or "—"

# Track week-over-week champion change
STATE = "/home/saijayanthai/.hermes/state-snapshots/weekly_evolution_last.json"
os.makedirs(os.path.dirname(STATE), exist_ok=True)
prev_champ = None
prev_top_fitness = None
try:
    with open(STATE) as f:
        prev = json.load(f)
    prev_champ = prev.get("champion")
    prev_top_fitness = prev.get("top_fitness")
except Exception:
    pass

try:
    with open(STATE, "w") as f:
        json.dump({
            "ts": datetime.utcnow().isoformat() + "Z",
            "champion": champ_id,
            "top_fitness": float(top3[0].get("fitness") or 0) if top3 else None,
            "generation": generation,
        }, f)
except Exception:
    pass

et_now = datetime.now(ZoneInfo("America/New_York")).strftime("%Y-%m-%d %H:%M ET")
champ_changed = prev_champ is not None and prev_champ != champ_id
fitness_delta = None
if prev_top_fitness is not None and top3:
    fitness_delta = float(top3[0].get("fitness") or 0) - float(prev_top_fitness)

icon = ":bell:"
if champ_changed:
    icon = ":warning:"  # operator-relevant: champion rotated

lines = [
    f"{icon} [weekly_evolution_report] {et_now}",
    f"gen={generation} champion={champ_id}" + (" (CHANGED)" if champ_changed else ""),
]
if fitness_delta is not None:
    lines.append(f"top fitness Δ vs last week: {fitness_delta:+.4f}")
lines.append(f"alive members: {len(alive)}")
if top3:
    lines.append("top 3:")
    for m in top3:
        mid = m.get("member_id", "?")
        fit = float(m.get("fitness") or 0)
        metrics = m.get("metrics") or {}
        sharpe = metrics.get("sharpe_ratio")
        pf = metrics.get("profit_factor")
        nt = metrics.get("num_trades")
        crown = " <- champ" if mid == champ_id else ""
        sharpe_s = f"sharpe={sharpe:.2f}" if isinstance(sharpe, (int, float)) else "sharpe=—"
        pf_s = f"pf={pf:.2f}" if isinstance(pf, (int, float)) else "pf=—"
        lines.append(f"  {mid} fit={fit:.4f} {sharpe_s} {pf_s} n={nt}{crown}")
else:
    lines.append("(no live members — evolution may not have run)")

# 4-question framing
if champ_changed:
    do_now = f"new champ {champ_id} — verify backtest before adopting"
elif not top3:
    do_now = "investigate: no live genome data"
else:
    do_now = "none — keep evolving"
lines.append(f"act: {do_now}")

msg = "\n".join(lines)
print(msg)

url = os.environ.get("SLACK_WEBHOOK_URL", "").strip()
if not url:
    print("[weekly_evolution_report] no SLACK_WEBHOOK_URL — skipping Slack post", file=sys.stderr)
    sys.exit(0)
try:
    req = urllib.request.Request(url, data=json.dumps({"text": msg}).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=10) as r:
        print(f"[weekly_evolution_report] slack status={r.status}", file=sys.stderr)
except Exception as exc:
    print(f"[weekly_evolution_report] slack post failed: {exc}", file=sys.stderr)
PY

echo "── weekly_evolution_report done $(date -u +%Y-%m-%dT%H:%M:%SZ) ──" >>"$LOG"
```

### Appendix 4.4 — `sentiment_accuracy_audit.sh`

```bash
#!/usr/bin/env bash
# sentiment_accuracy_audit.sh — deterministic replacement for LLM sentiment cron.
#
# Joins trade_journal (sentiment_score at entry, pnl outcome) over the last
# 3 days and computes the directional-accuracy rate: did positive
# sentiment_score correlate with positive pnl? Pulls auxiliary stats from
# sentiment_log to show what the broad-market sentiment has been saying.

set -uo pipefail

REPO=/home/saijayanthai/Documents/trading-bot
LOG=$REPO/user_data/logs/cron-sentiment-audit.log
mkdir -p "$(dirname "$LOG")"

set -a
# shellcheck disable=SC1091
[[ -f "$REPO/.env" ]] && source "$REPO/.env"
set +a

export POSTGRES_HOST="${POSTGRES_HOST:-localhost}"
export POSTGRES_PORT="${POSTGRES_PORT:-5434}"

cd "$REPO"

ts=$(date -u +%Y-%m-%dT%H:%M:%SZ)
echo "── sentiment_accuracy_audit $ts ──" >>"$LOG"

python3 - <<'PY' 2>>"$LOG"
import json, os, sys, urllib.request
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo

import psycopg

dsn = dict(
    host=os.environ.get("POSTGRES_HOST", "localhost"),
    port=int(os.environ.get("POSTGRES_PORT", "5434")),
    user=os.environ.get("POSTGRES_USER", "tradebot"),
    password=os.environ.get("POSTGRES_PASSWORD", ""),
    dbname=os.environ.get("POSTGRES_DB", "tradebot"),
    connect_timeout=5,
)

n_total = n_with_sent = n_correct = 0
positive_sent_pos = positive_sent_neg = 0
negative_sent_pos = negative_sent_neg = 0
neutral_n = 0
sent_avg = None
sent_n = 0
fg_now = fg_class = None
err = None

try:
    with psycopg.connect(**dsn) as conn, conn.cursor() as cur:
        # Closed trades in last 3 days
        cutoff = datetime.now(timezone.utc) - timedelta(days=3)
        cur.execute(
            """
            SELECT pair, sentiment_score, pnl
            FROM trade_journal
            WHERE closed_at >= %s AND closed_at IS NOT NULL
            """,
            (cutoff,),
        )
        for pair, sent, pnl in cur.fetchall():
            n_total += 1
            if sent is None:
                continue
            sent = float(sent)
            pnl = float(pnl or 0)
            n_with_sent += 1
            if abs(sent) < 0.05:
                neutral_n += 1
                continue
            # directional accuracy: positive sent should predict positive pnl
            if sent > 0:
                if pnl > 0:
                    n_correct += 1; positive_sent_pos += 1
                else:
                    positive_sent_neg += 1
            else:
                if pnl < 0:
                    n_correct += 1; negative_sent_neg += 1
                else:
                    negative_sent_pos += 1

        # Recent sentiment_log signal
        cur.execute(
            """
            SELECT AVG(sentiment_score) AS s, COUNT(*) AS n,
                   MAX(fear_greed_value) AS fg, MAX(fear_greed_classification) AS fgc
            FROM sentiment_log
            WHERE ts >= NOW() - INTERVAL '3 days'
            """
        )
        row = cur.fetchone()
        if row:
            sent_avg = float(row[0]) if row[0] is not None else None
            sent_n = int(row[1] or 0)
            fg_now = row[2]
            fg_class = row[3]
except Exception as exc:
    err = exc

if err is not None:
    msg = f":rotating_light: [sentiment_accuracy_audit] DB error: {err}"
    print(msg)
    url = os.environ.get("SLACK_WEBHOOK_URL", "").strip()
    if url:
        try:
            req = urllib.request.Request(url, data=json.dumps({"text": msg}).encode(),
                                         headers={"Content-Type": "application/json"})
            urllib.request.urlopen(req, timeout=10).read()
        except Exception:
            pass
    sys.exit(0)

et_now = datetime.now(ZoneInfo("America/New_York")).strftime("%Y-%m-%d %H:%M ET")
directional_n = n_with_sent - neutral_n
acc_pct = (n_correct / directional_n * 100) if directional_n else None

# Three-day trailing accuracy → severity
if acc_pct is None:
    icon = ":bell:"
    sev = "INSUFFICIENT_DATA"
elif acc_pct < 40:
    icon = ":rotating_light:"
    sev = "POOR"
elif acc_pct < 55:
    icon = ":warning:"
    sev = "WEAK"
else:
    icon = ":bell:"
    sev = "OK"

lines = [
    f"{icon} [sentiment_accuracy_audit] {et_now} ({sev})",
    f"closed trades (3d): {n_total} | w/ sentiment: {n_with_sent} | neutral: {neutral_n}",
]
if acc_pct is not None:
    lines.append(f"directional accuracy: {acc_pct:.0f}% ({n_correct}/{directional_n})")
    lines.append(f"  bull→win {positive_sent_pos} | bull→loss {positive_sent_neg}")
    lines.append(f"  bear→loss {negative_sent_neg} | bear→win {negative_sent_pos}")
else:
    lines.append("directional accuracy: — (no labeled trades)")
if sent_avg is not None:
    lines.append(f"market sent avg (3d): {sent_avg:+.2f} over {sent_n} samples")
if fg_now is not None:
    lines.append(f"F&G now: {fg_now} ({fg_class})")

# 4-question framing
if acc_pct is None:
    do_now = "no labeled trades — sentiment likely not in trade_journal yet"
elif acc_pct < 40:
    do_now = "REVIEW: sentiment WORSE than coin-flip → consider downweighting"
elif acc_pct < 55:
    do_now = "watch — close to coin-flip"
else:
    do_now = "none — sentiment is informative"
lines.append(f"act: {do_now}")

msg = "\n".join(lines)
print(msg)

url = os.environ.get("SLACK_WEBHOOK_URL", "").strip()
if not url:
    print("[sentiment_accuracy_audit] no SLACK_WEBHOOK_URL — skipping Slack post", file=sys.stderr)
    sys.exit(0)
try:
    req = urllib.request.Request(url, data=json.dumps({"text": msg}).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=10) as r:
        print(f"[sentiment_accuracy_audit] slack status={r.status}", file=sys.stderr)
except Exception as exc:
    print(f"[sentiment_accuracy_audit] slack post failed: {exc}", file=sys.stderr)
PY

echo "── sentiment_accuracy_audit done $(date -u +%Y-%m-%dT%H:%M:%SZ) ──" >>"$LOG"
```

### Appendix 4.5 — `ept_eval_breeding.sh`

```bash
#!/usr/bin/env bash
# ept_eval_breeding.sh — deterministic replacement for LLM-driven EPT evaluation cron.
#
# Calls user_data.dashboard.mcp_local.get_evolution_status() to inspect
# the current population, flags any member with rolling-Sharpe < 0.5 for
# demotion (informational; demotion happens in the next training run),
# and reports a clean Slack summary.

set -uo pipefail

REPO=/home/saijayanthai/Documents/trading-bot
LOG=$REPO/user_data/logs/cron-ept-eval.log
mkdir -p "$(dirname "$LOG")"

set -a
# shellcheck disable=SC1091
[[ -f "$REPO/.env" ]] && source "$REPO/.env"
set +a

export POSTGRES_HOST="${POSTGRES_HOST:-localhost}"
export POSTGRES_PORT="${POSTGRES_PORT:-5434}"

cd "$REPO"

ts=$(date -u +%Y-%m-%dT%H:%M:%SZ)
echo "── ept_eval_breeding $ts ──" >>"$LOG"

python3 - <<'PY' 2>>"$LOG"
import json, os, sys, urllib.request
from datetime import datetime
from zoneinfo import ZoneInfo

sys.path.insert(0, "/home/saijayanthai/Documents/trading-bot")

try:
    from user_data.dashboard.mcp_local import get_evolution_status, get_champion_genome
except Exception as exc:
    msg = f":rotating_light: [ept_eval_breeding] import failed: {exc}"
    print(msg)
    sys.exit(0)

try:
    evo = get_evolution_status()
    champ = get_champion_genome()
except Exception as exc:
    msg = f":rotating_light: [ept_eval_breeding] eval failed: {exc}"
    print(msg)
    sys.exit(0)

generation = evo.get("generation")
alive = evo.get("alive") or []
champ_id = evo.get("champion") or champ.get("member_id") or "—"

# Flag weak: sharpe < 0.5 OR num_trades < 5 (insufficient data)
weak = []
strong = []
for m in alive:
    metrics = m.get("metrics") or {}
    sharpe = metrics.get("sharpe_ratio")
    nt = int(metrics.get("num_trades") or 0)
    mid = m.get("member_id", "?")
    fit = float(m.get("fitness") or 0)
    if not isinstance(sharpe, (int, float)) or sharpe < 0.5 or nt < 5:
        weak.append((mid, fit, sharpe, nt))
    else:
        strong.append((mid, fit, sharpe, nt))

et_now = datetime.now(ZoneInfo("America/New_York")).strftime("%Y-%m-%d %H:%M ET")

# Severity
if len(weak) >= len(alive) * 0.5:
    icon = ":warning:"
    sev = "MANY_WEAK"
elif len(weak) == 0:
    icon = ":bell:"
    sev = "OK"
else:
    icon = ":bell:"
    sev = "SOME_WEAK"

lines = [
    f"{icon} [ept_eval_breeding] {et_now} ({sev})",
    f"gen={generation} alive={len(alive)} champion={champ_id}",
    f"strong (sharpe>=0.5 & n>=5): {len(strong)} | weak: {len(weak)}",
]
if weak:
    sample = weak[:5]
    lines.append("flagged for demotion:")
    for mid, fit, sharpe, nt in sample:
        sh_s = f"sharpe={sharpe:.2f}" if isinstance(sharpe, (int, float)) else "sharpe=—"
        lines.append(f"  {mid} fit={fit:.3f} {sh_s} n={nt}")
    if len(weak) > 5:
        lines.append(f"  …and {len(weak) - 5} more")

# 4-question framing
if not weak:
    do_now = "none — all members healthy"
elif len(weak) >= len(alive) * 0.5:
    do_now = "INVESTIGATE: >50% of population is weak — diversity collapse?"
else:
    do_now = "none — next generation will breed past these"
lines.append(f"act: {do_now}")

msg = "\n".join(lines)
print(msg)

url = os.environ.get("SLACK_WEBHOOK_URL", "").strip()
if not url:
    print("[ept_eval_breeding] no SLACK_WEBHOOK_URL — skipping Slack post", file=sys.stderr)
    sys.exit(0)
try:
    req = urllib.request.Request(url, data=json.dumps({"text": msg}).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=10) as r:
        print(f"[ept_eval_breeding] slack status={r.status}", file=sys.stderr)
except Exception as exc:
    print(f"[ept_eval_breeding] slack post failed: {exc}", file=sys.stderr)
PY

echo "── ept_eval_breeding done $(date -u +%Y-%m-%dT%H:%M:%SZ) ──" >>"$LOG"
```

### Appendix 4.6 — `post_mortem_weekly.sh`

```bash
#!/usr/bin/env bash
# post_mortem_weekly.sh — deterministic replacement for LLM post-mortem cron.
#
# Reads the last 7 days of trade_journal + regime_log, clusters losses by
# (regime, exit_reason), surfaces the top 3 patterns by total $-loss, and
# posts a Slack report. Recommendations only — no auto-applies.

set -uo pipefail

REPO=/home/saijayanthai/Documents/trading-bot
LOG=$REPO/user_data/logs/cron-post-mortem.log
mkdir -p "$(dirname "$LOG")"

set -a
# shellcheck disable=SC1091
[[ -f "$REPO/.env" ]] && source "$REPO/.env"
set +a

export POSTGRES_HOST="${POSTGRES_HOST:-localhost}"
export POSTGRES_PORT="${POSTGRES_PORT:-5434}"

cd "$REPO"

ts=$(date -u +%Y-%m-%dT%H:%M:%SZ)
echo "── post_mortem_weekly $ts ──" >>"$LOG"

python3 - <<'PY' 2>>"$LOG"
import json, os, sys, urllib.request
from collections import defaultdict
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo

import psycopg

dsn = dict(
    host=os.environ.get("POSTGRES_HOST", "localhost"),
    port=int(os.environ.get("POSTGRES_PORT", "5434")),
    user=os.environ.get("POSTGRES_USER", "tradebot"),
    password=os.environ.get("POSTGRES_PASSWORD", ""),
    dbname=os.environ.get("POSTGRES_DB", "tradebot"),
    connect_timeout=5,
)

err = None
losses = []           # tuples (regime, exit_reason, pnl)
total_loss = 0.0
total_trades = 0
total_winners = 0
worst_trade = None

try:
    cutoff = datetime.now(timezone.utc) - timedelta(days=7)
    with psycopg.connect(**dsn) as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT regime, exit_reason, pair, pnl
            FROM trade_journal
            WHERE closed_at >= %s AND closed_at IS NOT NULL
            """,
            (cutoff,),
        )
        for regime, exit_reason, pair, pnl in cur.fetchall():
            total_trades += 1
            pnl = float(pnl or 0)
            if pnl > 0:
                total_winners += 1
            if pnl < 0:
                losses.append(((regime or "unknown", exit_reason or "unknown"), pair, pnl))
                total_loss += pnl
                if worst_trade is None or pnl < worst_trade[2]:
                    worst_trade = (regime, pair, pnl, exit_reason)
except Exception as exc:
    err = exc

if err is not None:
    msg = f":rotating_light: [post_mortem_weekly] DB error: {err}"
    print(msg)
    url = os.environ.get("SLACK_WEBHOOK_URL", "").strip()
    if url:
        try:
            req = urllib.request.Request(url, data=json.dumps({"text": msg}).encode(),
                                         headers={"Content-Type": "application/json"})
            urllib.request.urlopen(req, timeout=10).read()
        except Exception:
            pass
    sys.exit(0)

# Cluster
clusters = defaultdict(lambda: {"n": 0, "loss": 0.0, "pairs": defaultdict(float)})
for key, pair, pnl in losses:
    c = clusters[key]
    c["n"] += 1
    c["loss"] += pnl
    c["pairs"][pair] += pnl

ranked = sorted(clusters.items(), key=lambda x: x[1]["loss"])  # most negative first
top3 = ranked[:3]

et_now = datetime.now(ZoneInfo("America/New_York")).strftime("%Y-%m-%d %H:%M ET")
loser_n = len(losses)
wr = (total_winners / total_trades * 100) if total_trades else 0.0

if total_loss < -500:
    icon = ":rotating_light:"
elif total_loss < 0:
    icon = ":warning:"
else:
    icon = ":bell:"

lines = [
    f"{icon} [post_mortem_weekly] 7-day window @ {et_now}",
    f"closed: {total_trades} ({total_winners}W / {loser_n}L, wr={wr:.0f}%)",
    f"total loss (losers only): ${total_loss:,.2f}",
]
if worst_trade is not None:
    lines.append(f"worst trade: {worst_trade[1]} ${worst_trade[2]:+,.2f} ({worst_trade[0]}, {worst_trade[3]})")

if top3:
    lines.append("top loss clusters (regime, exit_reason):")
    for (regime, exit_reason), c in top3:
        worst_pair = min(c["pairs"].items(), key=lambda p: p[1]) if c["pairs"] else (None, 0)
        lines.append(f"  {regime} / {exit_reason}: n={c['n']} loss=${c['loss']:+,.2f} (worst pair: {worst_pair[0]} ${worst_pair[1]:+,.2f})")

# Recommendations (informational only)
recs = []
for (regime, exit_reason), c in top3:
    if exit_reason and "stop" in exit_reason.lower():
        recs.append(f"loosen stop / re-eval entry in {regime}")
    elif exit_reason and "down" in exit_reason.lower():
        recs.append(f"check FreqAI down-regime threshold in {regime}")
    elif c["n"] >= 3:
        recs.append(f"investigate {regime}/{exit_reason} — repeat losses")
if recs:
    lines.append("recs (manual review):")
    for r in recs[:3]:
        lines.append(f"  • {r}")

# 4-question framing
if total_trades == 0:
    do_now = "no closes this week — trading paused or no signals firing"
elif total_loss < -500:
    do_now = "PRIORITISE: review top cluster; may need config change"
elif total_loss < 0:
    do_now = "review losers manually before next week"
else:
    do_now = "none — week was profitable"
lines.append(f"act: {do_now}")

msg = "\n".join(lines)
print(msg)

url = os.environ.get("SLACK_WEBHOOK_URL", "").strip()
if not url:
    print("[post_mortem_weekly] no SLACK_WEBHOOK_URL — skipping Slack post", file=sys.stderr)
    sys.exit(0)
try:
    req = urllib.request.Request(url, data=json.dumps({"text": msg}).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=10) as r:
        print(f"[post_mortem_weekly] slack status={r.status}", file=sys.stderr)
except Exception as exc:
    print(f"[post_mortem_weekly] slack post failed: {exc}", file=sys.stderr)
PY

echo "── post_mortem_weekly done $(date -u +%Y-%m-%dT%H:%M:%SZ) ──" >>"$LOG"
```

### Appendix 4.7 — `market_research_30min.sh`

```bash
#!/usr/bin/env bash
# market_research_30min.sh — deterministic replacement for LLM market-research cron.
#
# Reads sentiment_log, news_headlines, fear_greed_log, and regime_log
# directly. Computes a simple "cross-source divergence" score and posts
# only when actionable (divergence > 0.5 OR F&G crossed an extreme).

set -uo pipefail

REPO=/home/saijayanthai/Documents/trading-bot
LOG=$REPO/user_data/logs/cron-market-research.log
mkdir -p "$(dirname "$LOG")"

set -a
# shellcheck disable=SC1091
[[ -f "$REPO/.env" ]] && source "$REPO/.env"
set +a

export POSTGRES_HOST="${POSTGRES_HOST:-localhost}"
export POSTGRES_PORT="${POSTGRES_PORT:-5434}"

cd "$REPO"

ts=$(date -u +%Y-%m-%dT%H:%M:%SZ)
echo "── market_research_30min $ts ──" >>"$LOG"

python3 - <<'PY' 2>>"$LOG"
import json, os, sys, urllib.request
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import psycopg

dsn = dict(
    host=os.environ.get("POSTGRES_HOST", "localhost"),
    port=int(os.environ.get("POSTGRES_PORT", "5434")),
    user=os.environ.get("POSTGRES_USER", "tradebot"),
    password=os.environ.get("POSTGRES_PASSWORD", ""),
    dbname=os.environ.get("POSTGRES_DB", "tradebot"),
    connect_timeout=5,
)

err = None
llm_score = comm_score = fg_value = fg_class = regime = regime_prob = None
n_recent_headlines = 0
trending = []

try:
    with psycopg.connect(**dsn) as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT sentiment_score, market_impact, fear_greed_value,
                   fear_greed_classification, community_score_avg,
                   reddit_attention_avg, trending_pairs
            FROM sentiment_log ORDER BY ts DESC LIMIT 1
            """
        )
        row = cur.fetchone()
        if row:
            llm_score = float(row[0]) if row[0] is not None else None
            llm_impact = row[1]
            fg_value = row[2]
            fg_class = row[3]
            comm_score = float(row[4]) if row[4] is not None else None
            att_score = float(row[5]) if row[5] is not None else None
            trending = row[6] or []
            if isinstance(trending, str):
                try:
                    trending = json.loads(trending)
                except Exception:
                    trending = []

        cur.execute(
            "SELECT regime, probability FROM regime_log ORDER BY ts DESC LIMIT 1"
        )
        row = cur.fetchone()
        if row:
            regime = row[0]
            regime_prob = float(row[1]) if row[1] is not None else None

        cur.execute(
            """
            SELECT COUNT(*) FROM news_headlines
            WHERE ts > NOW() - INTERVAL '30 minutes'
            """
        )
        row = cur.fetchone()
        n_recent_headlines = int(row[0] or 0) if row else 0
except Exception as exc:
    err = exc

if err is not None:
    msg = f":rotating_light: [market_research_30min] DB error: {err}"
    print(msg)
    sys.exit(0)

et_now = datetime.now(ZoneInfo("America/New_York")).strftime("%H:%M ET")

# Cross-source divergence: |LLM - F&G normalised| + |LLM - community|
def fg_normalise(v):
    # F&G 0-100; normalise to -1..+1 (0→-1 extreme fear, 100→+1 extreme greed)
    if v is None:
        return None
    return (float(v) - 50.0) / 50.0

fg_norm = fg_normalise(fg_value)
div = 0.0
n_components = 0
if llm_score is not None and fg_norm is not None:
    div += abs(llm_score - fg_norm)
    n_components += 1
if llm_score is not None and comm_score is not None:
    div += abs(llm_score - comm_score)
    n_components += 1
mean_div = (div / n_components) if n_components else 0.0

# F&G extreme transition tracking
STATE = "/home/saijayanthai/.hermes/state-snapshots/market_research_last.json"
os.makedirs(os.path.dirname(STATE), exist_ok=True)
prev_fg = prev_regime = None
try:
    with open(STATE) as f:
        prev = json.load(f)
    prev_fg = prev.get("fg_value")
    prev_regime = prev.get("regime")
except Exception:
    pass

regime_changed = prev_regime is not None and prev_regime != regime
fg_crossed_extreme = False
if prev_fg is not None and fg_value is not None:
    # crossing 25 (Extreme Fear) or 75 (Extreme Greed) in either direction
    for threshold in (25, 75):
        if (prev_fg < threshold <= fg_value) or (prev_fg > threshold >= fg_value):
            fg_crossed_extreme = True

try:
    with open(STATE, "w") as f:
        json.dump({
            "ts": datetime.utcnow().isoformat() + "Z",
            "fg_value": fg_value,
            "regime": regime,
            "llm_score": llm_score,
        }, f)
except Exception:
    pass

# Actionable?
actionable = mean_div > 0.5 or fg_crossed_extreme or regime_changed

if actionable:
    if mean_div > 0.8 or fg_crossed_extreme:
        icon = ":warning:"
    else:
        icon = ":bell:"
else:
    icon = ":bell:"

lines = [
    f"{icon} [market_research_30min] {et_now}",
    f"LLM sent: {llm_score if llm_score is None else f'{llm_score:+.2f}'} | "
    f"Reddit comm: {comm_score if comm_score is None else f'{comm_score:+.2f}'} | "
    f"F&G: {fg_value} ({fg_class})",
    f"divergence: {mean_div:.2f} (>0.5 = actionable)",
    f"regime: {regime} ({regime_prob:.2f})" if regime_prob is not None else f"regime: {regime}",
    f"headlines last 30m: {n_recent_headlines}",
]
if trending:
    lines.append(f"trending: {', '.join(str(x) for x in trending[:5])}")

# What changed
changes = []
if regime_changed:
    changes.append(f"regime {prev_regime} → {regime}")
if fg_crossed_extreme:
    changes.append(f"F&G crossed extreme threshold ({prev_fg} → {fg_value})")
if mean_div > 0.5:
    changes.append(f"sources diverge by {mean_div:.2f}")
if not changes:
    changes.append("no material change")
lines.append("changed: " + " | ".join(changes))

# Act
if regime_changed:
    do_now = "regime flip — verify position sizing matches new regime"
elif mean_div > 0.8:
    do_now = "HEAVY divergence — pause sentiment-driven entries"
elif fg_crossed_extreme:
    do_now = "F&G extreme crossed — review risk exposure"
elif mean_div > 0.5:
    do_now = "moderate divergence — watch for follow-through"
else:
    do_now = "none"
lines.append(f"act: {do_now}")

msg = "\n".join(lines)
print(msg)

# Suppress non-actionable to avoid 48 Slack posts/day. Always log to stdout
# (Hermes captures it) but only Slack on actionable.
if not actionable:
    print("[market_research_30min] non-actionable — skipping Slack", file=sys.stderr)
    sys.exit(0)

url = os.environ.get("SLACK_WEBHOOK_URL", "").strip()
if not url:
    print("[market_research_30min] no SLACK_WEBHOOK_URL — skipping Slack", file=sys.stderr)
    sys.exit(0)
try:
    req = urllib.request.Request(url, data=json.dumps({"text": msg}).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=10) as r:
        print(f"[market_research_30min] slack status={r.status}", file=sys.stderr)
except Exception as exc:
    print(f"[market_research_30min] slack post failed: {exc}", file=sys.stderr)
PY

echo "── market_research_30min done $(date -u +%Y-%m-%dT%H:%M:%SZ) ──" >>"$LOG"
```

### Appendix 4.8 — `shark_briefing_alerts.sh`

```bash
#!/usr/bin/env bash
# shark_briefing_alerts.sh — deterministic shark BEAR-override alert cron.
#
# Reads stocks/memory/DAILY-HANDOFF.md (most-recent phase block) and posts
# a Slack alert when the regime is BEAR* or macro is ELEVATED/EXTREME —
# operator's standing rule is "show me the BEAR override candidates".

set -uo pipefail

REPO=/home/saijayanthai/Documents/trading-bot
STOCKS=$REPO/stocks
HANDOFF=$STOCKS/memory/DAILY-HANDOFF.md
LOG=$REPO/user_data/logs/cron-shark-briefing.log
mkdir -p "$(dirname "$LOG")"

set -a
# shellcheck disable=SC1091
[[ -f "$REPO/.env" ]] && source "$REPO/.env"
set +a

cd "$REPO"

ts=$(date -u +%Y-%m-%dT%H:%M:%SZ)
echo "── shark_briefing_alerts $ts ──" >>"$LOG"

export HANDOFF_PATH="$HANDOFF"

python3 - <<'PY' 2>>"$LOG"
import json, os, sys, re, urllib.request
from datetime import datetime
from zoneinfo import ZoneInfo

HANDOFF = os.environ.get("HANDOFF_PATH", "")

if not os.path.isfile(HANDOFF):
    print(f":warning: [shark_briefing_alerts] DAILY-HANDOFF.md missing at {HANDOFF}")
    sys.exit(0)

try:
    with open(HANDOFF) as f:
        text = f.read()
except Exception as exc:
    print(f":rotating_light: [shark_briefing_alerts] read failed: {exc}")
    sys.exit(0)

# Find all phase blocks (## phase | HH:MM EDT). Take the most recent.
phase_re = re.compile(r"^##\s+([^|\n]+)\s*\|\s*([\d:]+\s+\w+)\s*$", re.MULTILINE)
matches = list(phase_re.finditer(text))
if not matches:
    print(":bell: [shark_briefing_alerts] no phase blocks in DAILY-HANDOFF — nothing to report")
    sys.exit(0)

last = matches[-1]
block_start = last.start()
# Block actually runs from this match to the next top-level ## or EOF
next_top = re.search(r"^##\s+", text[last.end():], re.MULTILINE)
block_end = last.end() + next_top.start() if next_top else len(text)
block = text[block_start:block_end]

phase = last.group(1).strip()
phase_ts = last.group(2).strip()

def pull(field):
    m = re.search(rf"^{re.escape(field)}\s*:\s*(.+)$", block, re.MULTILINE | re.IGNORECASE)
    return m.group(1).strip() if m else None

confirmed = pull("confirmed") or "—"
skipped = pull("skipped") or "—"
market = pull("market") or "—"
regime = (pull("regime") or "").upper()
macro = (pull("macro") or "").upper()
lessons = pull("lessons") or "—"

# Severity
bear = bool(re.search(r"BEAR", regime))
extreme_macro = macro in {"ELEVATED", "EXTREME", "HIGH"}

if bear and extreme_macro:
    icon = ":rotating_light:"
    sev = "CRITICAL"
elif bear or extreme_macro:
    icon = ":warning:"
    sev = "BEAR_OVERRIDE"
else:
    icon = ":bell:"
    sev = "OK"

et_now = datetime.now(ZoneInfo("America/New_York")).strftime("%H:%M ET")

lines = [
    f"{icon} [shark_briefing_alerts] {sev} {et_now}",
    f"phase: {phase} @ {phase_ts}",
    f"confirmed: {confirmed}",
    f"skipped: {skipped}",
    f"regime: {regime or '—'} | macro: {macro or '—'} | breadth: {market}",
]
if lessons and lessons.lower() != "none":
    lines.append(f"lessons: {lessons}")

# Track regime change
STATE = "/home/saijayanthai/.hermes/state-snapshots/shark_briefing_last.json"
os.makedirs(os.path.dirname(STATE), exist_ok=True)
prev_regime = prev_phase = None
try:
    with open(STATE) as f:
        prev = json.load(f)
    prev_regime = prev.get("regime")
    prev_phase = prev.get("phase")
except Exception:
    pass

regime_changed = prev_regime is not None and prev_regime != regime
new_phase = prev_phase != phase + "@" + phase_ts

try:
    with open(STATE, "w") as f:
        json.dump({
            "ts": datetime.utcnow().isoformat() + "Z",
            "regime": regime,
            "phase": phase + "@" + phase_ts,
        }, f)
except Exception:
    pass

# 4-question framing
if regime_changed:
    changed = f"regime {prev_regime} → {regime}"
elif new_phase:
    changed = "new phase posted"
else:
    changed = "no new phase block"
lines.append(f"changed: {changed}")

if sev == "CRITICAL":
    do_now = "BEAR + extreme macro — verify no new longs queued"
elif sev == "BEAR_OVERRIDE":
    do_now = "BEAR regime — confirm only override-grade candidates traded"
else:
    do_now = "none"
lines.append(f"act: {do_now}")

msg = "\n".join(lines)
print(msg)

# Only Slack when a new phase block is present OR severity != OK
if not new_phase and sev == "OK":
    print("[shark_briefing_alerts] no new phase + OK regime — skipping Slack", file=sys.stderr)
    sys.exit(0)

url = os.environ.get("SLACK_WEBHOOK_URL", "").strip()
if not url:
    print("[shark_briefing_alerts] no SLACK_WEBHOOK_URL — skipping Slack", file=sys.stderr)
    sys.exit(0)
try:
    req = urllib.request.Request(url, data=json.dumps({"text": msg}).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=10) as r:
        print(f"[shark_briefing_alerts] slack status={r.status}", file=sys.stderr)
except Exception as exc:
    print(f"[shark_briefing_alerts] slack post failed: {exc}", file=sys.stderr)
PY

echo "── shark_briefing_alerts done $(date -u +%Y-%m-%dT%H:%M:%SZ) ──" >>"$LOG"
```

### Appendix 4.9 — `shark_pre_execute.sh`

```bash
#!/usr/bin/env bash
# shark_pre_execute.sh — fires shark phase 'pre-execute'
#
# Wired as a Hermes cron with --no-agent (the Python phase IS the agent;
# Hermes just drives the schedule). All LLM calls go through local Ollama
# via shark.llm.client (zero $/call). Slack mirror on actions/errors.

set -euo pipefail

REPO=/home/saijayanthai/Documents/trading-bot
STOCKS=$REPO/stocks
LOG=$STOCKS/memory/cron-shark-pre_execute.log

cd "$STOCKS"

# shellcheck disable=SC1091
source venv/bin/activate

# Pull SLACK_WEBHOOK_URL etc from the unified .env
set -a
# shellcheck disable=SC1091
source "$REPO/.env"
set +a

output=$(
    {
        echo "── shark pre-execute started $(date -u +%Y-%m-%dT%H:%M:%SZ) ──"
        python shark/run.py pre-execute
        rc=$?
        echo "── exit=$rc ──"
    } 2>&1
)
echo "$output" | tee -a "$LOG" >/dev/null

# Telegram (via Hermes --deliver) only when we have something to say.
# Slack (via SLACK_WEBHOOK_URL) gets the same content.
summary=$(echo "$output" | tail -40 | grep -iE "DECISION|ENTRY|EXIT|TRADE|ERROR|FAILED|stop|circuit|KILL" | head -10)
if [[ -n "$summary" ]]; then
    echo "shark pre-execute:"
    echo "$summary"
    if [[ -n "${SLACK_WEBHOOK_URL:-}" ]]; then
        payload=$(printf '{"text": "shark pre-execute: %s"}' "${summary//\"/\\\"}")
        curl -fsS --max-time 10 -X POST -H 'Content-Type: application/json' \
            -d "$payload" "$SLACK_WEBHOOK_URL" >/dev/null 2>&1 || true
    fi
fi
```

