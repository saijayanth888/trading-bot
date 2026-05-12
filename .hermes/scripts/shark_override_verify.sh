#!/usr/bin/env bash
# shark_override_verify.sh — verifies the shark BEAR_VOLATILE paper-mode override
# is actually firing (or correctly skipping) after each market_open phase.
#
# Why no LLM: the shark_market_open phase already produces deterministic,
# greppable cron output. We just need to parse it, count candidates that
# cleared the 0.85 confidence override, count trades placed, and surface
# a green/yellow/red signal on the operator dashboard.
#
# Schedule: 45 9 * * 1-5  (15 min after market_open at 09:30 ET so the
#                         python phase has had time to emit decisions)
#
# Reads the latest cron output from the shark_market_open job, parses for
#   - market regime line
#   - per-candidate "rejected" / "EXECUTE" / TFT-gate lines
#   - PAPER MODE override fired vs blocked
# and writes a JSON status file at stocks/memory/override_verify.json.
#
# State file at ~/.hermes/state-snapshots/shark_override_verify_last.json
# tracks consecutive-stalled-runs so the dashboard card can warn after
# 1 stalled run and alert after 3 (≈ 3 trading days with no override fire
# when override was expected).

set -uo pipefail

REPO=/home/saijayanthai/Documents/trading-bot
LOG=$REPO/user_data/logs/cron-shark-override-verify.log
mkdir -p "$(dirname "$LOG")"

set -a
# shellcheck disable=SC1091
[[ -f "$REPO/.env" ]] && source "$REPO/.env"
set +a

cd "$REPO"

ts=$(date -u +%Y-%m-%dT%H:%M:%SZ)
echo "── shark_override_verify $ts ──" >>"$LOG"

# Shark market-open cron job id from ~/.hermes/cron/jobs.json (immutable
# once a job is created; never changes unless the job is deleted/recreated).
SHARK_MARKET_OPEN_JOB_ID="${SHARK_MARKET_OPEN_JOB_ID:-da38c6eb6673}"
CRON_OUT_DIR="${SHARK_OVERRIDE_VERIFY_CRON_OUT_DIR:-$HOME/.hermes/cron/output/$SHARK_MARKET_OPEN_JOB_ID}"
STATE_FILE="${SHARK_OVERRIDE_VERIFY_STATE_FILE:-$HOME/.hermes/state-snapshots/shark_override_verify_last.json}"
OUT_FILE="${SHARK_OVERRIDE_VERIFY_OUT_FILE:-$REPO/stocks/memory/override_verify.json}"
SUPPRESS_SLACK="${SHARK_OVERRIDE_VERIFY_SUPPRESS_SLACK:-0}"

export CRON_OUT_DIR STATE_FILE OUT_FILE SUPPRESS_SLACK

python3 - <<'PY' 2>>"$LOG"
import json, os, sys, glob, re, urllib.request
from datetime import datetime, timezone, date
from pathlib import Path

CRON_OUT_DIR = os.environ["CRON_OUT_DIR"]
STATE_PATH = Path(os.environ["STATE_FILE"])
OUT_PATH = Path(os.environ["OUT_FILE"])
SUPPRESS_SLACK = os.environ.get("SUPPRESS_SLACK", "0") == "1"
STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
OUT_PATH.parent.mkdir(parents=True, exist_ok=True)

# ---- Find latest cron output file ---------------------------------------
files = sorted(glob.glob(f"{CRON_OUT_DIR}/*.md"))
if not files:
    payload = {
        "date": date.today().isoformat(),
        "regime": None,
        "candidates_evaluated": 0,
        "candidates_passing_override": 0,
        "trades_placed": 0,
        "status": "unknown",
        "stalled_runs": 0,
        "last_trade_at": None,
        "reason": "no shark_market_open cron output found yet",
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "source_file": None,
    }
    OUT_PATH.write_text(json.dumps(payload, indent=2))
    print(f":bell: *[shark_override_verify]* no cron output at {CRON_OUT_DIR} — wrote unknown status")
    sys.exit(0)

latest = files[-1]
text = Path(latest).read_text(errors="replace")

# ---- Parse the cron output ----------------------------------------------
# Regime line examples:
#   "Market regime: BEAR_VOLATILE | trend_score=-3 atr_pct=1.52% ..."
#   "Market regime: BULL_QUIET — ..."
regime = None
m = re.search(r"Market regime:\s*([A-Z_]+)", text)
if m:
    regime = m.group(1)

# Override-applied marker from market_regime.py:
#   "PAPER MODE: overriding BEAR_VOLATILE rules — 1 trade/day at 0.5x size (confidence ≥ 0.85)"
override_applied = bool(re.search(r"PAPER MODE.*overriding BEAR.*confidence", text))
# Or the guardrails confirmation:
#   "OK — PAPER MODE override: BEAR_VOLATILE allows limited trades ..."
override_applied = override_applied or bool(re.search(r"PAPER MODE override.*BEAR", text))

# Candidates evaluated — count lines emitted by market_open.py for each
# symbol in the candidate loop. Common rejection / acceptance markers:
#   "<SYM> rejected — confidence ..."
#   "<SYM> rejected — derived R:R ..."
#   "<SYM> rejected — invalid stop/target ..."
#   "<SYM> EXECUTE qty=N confidence=..."
#   "<SYM> — Claude decided NO_TRADE"
#   "[TFT_GATE] <SYM> ..."
#   "Guardrails APPROVED — <SYM> ..."
#   "Guardrails REJECTED — <SYM> ..."
SYM = r"\b([A-Z]{1,5})\b"
candidate_symbols = set()
for pat in [
    rf"{SYM}\s+rejected\s+—",
    rf"{SYM}\s+EXECUTE\s+qty=",
    rf"{SYM}\s+—\s+Claude decided",
    rf"\[TFT_GATE\]\s+{SYM}",
    rf"Guardrails (?:APPROVED|REJECTED)\s+—\s+{SYM}",
]:
    for m in re.finditer(pat, text):
        candidate_symbols.add(m.group(1))

candidates_evaluated = len(candidate_symbols)

# Candidates that passed the override = at least made it past the
# 0.85 confidence floor (line: "<SYM> EXECUTE qty=...") OR were green-lit
# by the TFT gate. We use EXECUTE as the canonical "passed all gates".
passed_override = set()
for m in re.finditer(rf"{SYM}\s+EXECUTE\s+qty=", text):
    passed_override.add(m.group(1))
candidates_passing_override = len(passed_override)

# Trades actually placed — look for "Bracket order placed" or
# the broker confirmation lines emitted by execution.alpaca_client.
# Falls back to counting EXECUTE lines minus dry-run lines.
trades_placed = 0
for m in re.finditer(r"Bracket order placed.*\b([A-Z]{1,5})\b", text):
    trades_placed += 1
if trades_placed == 0:
    # Fallback heuristic: an EXECUTE not followed by [DRY RUN]
    dry_runs = len(re.findall(r"\[DRY RUN\]", text))
    trades_placed = max(0, len(passed_override) - dry_runs)

# Was the override expected to fire?
# Yes when: regime contains BEAR (override only applies in BEAR regimes).
override_expected = bool(regime and "BEAR" in regime)

# ---- Determine status ---------------------------------------------------
# stalled_runs increments when:
#   - regime was BEAR_VOLATILE (override expected)
#   - AND no candidates passed the override
#   - AND no trades were placed
# It resets when any trade is placed OR when regime is non-BEAR (override
# not expected, so stalling is not a real issue).
prev_state = {}
try:
    prev_state = json.loads(STATE_PATH.read_text())
except Exception:
    pass

prev_stalled = int(prev_state.get("stalled_runs") or 0)
prev_last_trade_at = prev_state.get("last_trade_at")

if not override_expected:
    # Non-BEAR regime — override doesn't apply, can't be stalled
    stalled_runs = 0
    status = "healthy"
    reason = f"regime={regime} (override only applies in BEAR regimes)"
elif trades_placed > 0:
    stalled_runs = 0
    status = "healthy"
    reason = f"override fired — {trades_placed} trade(s) placed"
    prev_last_trade_at = datetime.now(timezone.utc).isoformat()
elif candidates_evaluated == 0:
    # Nothing to evaluate (no candidates from pre_market handoff). Override
    # was expected but had nothing to fire on — not a verifier failure.
    stalled_runs = 0
    status = "healthy"
    reason = "no candidates from pre-market — nothing to evaluate"
else:
    # Candidates were present, override was expected, but nothing fired.
    stalled_runs = prev_stalled + 1
    if stalled_runs >= 3:
        status = "stalled"
    elif stalled_runs >= 1:
        status = "degraded"
    else:
        status = "healthy"
    reason = (
        f"{candidates_evaluated} candidate(s) evaluated, 0 passed override "
        f"({stalled_runs} consecutive run(s) stalled)"
    )

last_trade_at = (
    datetime.now(timezone.utc).isoformat() if trades_placed > 0
    else prev_last_trade_at
)

payload = {
    "date": date.today().isoformat(),
    "regime": regime,
    "override_expected": override_expected,
    "override_applied": override_applied,
    "candidates_evaluated": candidates_evaluated,
    "candidates_passing_override": candidates_passing_override,
    "trades_placed": trades_placed,
    "status": status,
    "stalled_runs": stalled_runs,
    "last_trade_at": last_trade_at,
    "reason": reason,
    "checked_at": datetime.now(timezone.utc).isoformat(),
    "source_file": latest,
}

OUT_PATH.write_text(json.dumps(payload, indent=2))

# Persist state for next run's stalled_runs computation
STATE_PATH.write_text(json.dumps({
    "stalled_runs": stalled_runs,
    "last_trade_at": last_trade_at,
    "regime": regime,
    "ts": datetime.now(timezone.utc).isoformat(),
}, indent=2))

# ---- Slack alert when stalled_runs > 3 ---------------------------------
should_alert = stalled_runs > 3
icon = ":rotating_light:" if status == "stalled" else (
       ":warning:" if status == "degraded" else ":white_check_mark:")
sev = "CRITICAL" if status == "stalled" else (
      "WARN" if status == "degraded" else "OK")

et_now = datetime.now(timezone.utc).strftime("%H:%MZ")
lines = [
    f"{icon} *[shark_override_verify]* · *{sev}* · {et_now}",
    f"regime={regime} candidates={candidates_evaluated} "
    f"passed_override={candidates_passing_override} trades={trades_placed}",
    f"stalled_runs={stalled_runs} last_trade={last_trade_at or 'never'}",
    f"∆ {reason}",
]
if should_alert:
    lines.append("*ACT:* INSPECT — override has not fired in >3 runs. "
                 "Check stocks/memory/CONTEXT-BRIEFING.md for candidate "
                 "quality and confidence floors in shark/config.py.")
else:
    lines.append("*ACT:* none")

msg = "\n".join(lines)
print(msg)

if should_alert and not SUPPRESS_SLACK:
    url = os.environ.get("SLACK_WEBHOOK_URL", "").strip()
    if url:
        try:
            req = urllib.request.Request(
                url,
                data=json.dumps({"text": msg}).encode(),
                headers={"Content-Type": "application/json"},
            )
            with urllib.request.urlopen(req, timeout=10) as r:
                print(f"*[shark_override_verify]* slack status={r.status}", file=sys.stderr)
        except Exception as exc:
            print(f"*[shark_override_verify]* slack post failed: {exc}", file=sys.stderr)
    else:
        print(f"*[shark_override_verify]* no SLACK_WEBHOOK_URL — skipping post", file=sys.stderr)
PY

echo "── shark_override_verify done $(date -u +%Y-%m-%dT%H:%M:%SZ) ──" >>"$LOG"
