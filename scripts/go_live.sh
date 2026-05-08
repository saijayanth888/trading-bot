#!/usr/bin/env bash
#
# Go-live driver with graduated capital exposure.
#
# Subcommands
# -----------
#   ./scripts/go_live.sh init      — first-time activation: validate, switch
#                                    dry_run=false, set ratio to 0.10, start.
#   ./scripts/go_live.sh advance   — graduate to the next stage if eligible.
#   ./scripts/go_live.sh status    — print current stage + state.
#   ./scripts/go_live.sh set RATIO — manually pin the ratio (operator override).
#
# Stages (capital exposure by tradable_balance_ratio)
# ---------------------------------------------------
#   stage 1: 0.10  (≈$1,900)  — week 1
#   stage 2: 0.30  (≈$5,700)  — week 2 only if week-1 PnL > 0
#   stage 3: 0.50  (≈$9,500)  — week 3 only if week-2 PnL > 0
#   stage 4: 0.99  (≈$19,000) — week 4+ only if 30-day PnL > 0
#
# State file: ~/.trading-bot/go_live.json (kept outside the repo on purpose).

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CONFIG="${ROOT_DIR}/user_data/config.json"
STATE_DIR="${HOME}/.trading-bot"
STATE_FILE="${STATE_DIR}/go_live.json"
LOG="${ROOT_DIR}/user_data/logs/go_live.log"

mkdir -p "$STATE_DIR" "$(dirname "$LOG")"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

log() {
    local msg="$*"
    echo "$(date -u '+%Y-%m-%dT%H:%M:%SZ') ${msg}" | tee -a "$LOG"
}

die() {
    log "ERROR  $*"
    exit 1
}

require() {
    command -v "$1" >/dev/null 2>&1 || die "missing required command: $1"
}

require python3
require docker

# ---------------------------------------------------------------------------
# State helpers (delegated to Python — no jq dependency)
# ---------------------------------------------------------------------------

py_state() {
    python3 - "$STATE_FILE" "$@" <<'PY'
import json, os, sys
from datetime import datetime, timezone
state_path = sys.argv[1]
op = sys.argv[2]
args = sys.argv[3:]

def load():
    if not os.path.exists(state_path):
        return {"first_live_at": None, "stage": 0, "ratio": 0.0, "history": []}
    with open(state_path) as f:
        return json.load(f)

def save(s):
    tmp = state_path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(s, f, indent=2)
    os.replace(tmp, state_path)

state = load()

if op == "get":
    print(json.dumps(state, indent=2))
elif op == "first_live_at":
    print(state.get("first_live_at") or "")
elif op == "stage":
    print(state.get("stage") or 0)
elif op == "ratio":
    print(state.get("ratio") or 0.0)
elif op == "days_live":
    f = state.get("first_live_at")
    if not f:
        print("0")
    else:
        d = datetime.fromisoformat(f.replace("Z", "+00:00"))
        print(int((datetime.now(timezone.utc) - d).total_seconds() // 86400))
elif op == "set_stage":
    stage = int(args[0])
    ratio = float(args[1])
    state["stage"] = stage
    state["ratio"] = ratio
    if state.get("first_live_at") is None:
        state["first_live_at"] = datetime.now(timezone.utc).isoformat()
    state.setdefault("history", []).append({
        "ts": datetime.now(timezone.utc).isoformat(),
        "stage": stage, "ratio": ratio,
        "trigger": args[2] if len(args) > 2 else "manual",
    })
    save(state)
elif op == "record_event":
    state.setdefault("history", []).append({
        "ts": datetime.now(timezone.utc).isoformat(),
        "event": args[0],
        "detail": args[1] if len(args) > 1 else "",
    })
    save(state)
else:
    print(f"unknown op {op}", file=sys.stderr)
    sys.exit(1)
PY
}

# Updates the config.json atomically, modifying tradable_balance_ratio and dry_run.
patch_config() {
    local ratio="$1" dry_run="$2"
    python3 - "$CONFIG" "$ratio" "$dry_run" <<'PY'
import json, os, sys
path, ratio, dry_run = sys.argv[1], float(sys.argv[2]), sys.argv[3].lower() == "true"
with open(path) as f:
    cfg = json.load(f)
cfg["tradable_balance_ratio"] = ratio
cfg["dry_run"] = dry_run
tmp = path + ".tmp"
with open(tmp, "w") as f:
    json.dump(cfg, f, indent=4)
os.replace(tmp, path)
print(f"[config] tradable_balance_ratio={ratio}  dry_run={dry_run}")
PY
}

# Returns the PnL over the last N days from the trade journal (Postgres).
window_pnl() {
    local days="$1"
    python3 - "$days" <<'PY'
import os, sys
from datetime import datetime, timedelta, timezone
days = int(sys.argv[1])
dsn = os.environ.get("DATABASE_URL",
    "postgresql://tradebot:tradebot-change-me@localhost:5433/tradebot")
cutoff = datetime.now(timezone.utc) - timedelta(days=days)
try:
    import psycopg
    with psycopg.connect(dsn, connect_timeout=5) as c:
        with c.cursor() as cur:
            cur.execute(
                "SELECT COALESCE(SUM(pnl),0) FROM trade_journal "
                "WHERE closed_at IS NOT NULL AND closed_at >= %s",
                (cutoff,),
            )
            row = cur.fetchone()
            print(f"{float(row[0]):.6f}")
except Exception as exc:
    print("0.000000")
    print(f"# error: {exc}", file=sys.stderr)
PY
}

restart_bot() {
    log "restarting freqtrade container"
    if docker compose -f "${ROOT_DIR}/docker-compose.yml" ps -q freqtrade >/dev/null 2>&1; then
        docker compose -f "${ROOT_DIR}/docker-compose.yml" restart freqtrade
    else
        log "freqtrade container not running — starting it"
        docker compose -f "${ROOT_DIR}/docker-compose.yml" up -d freqtrade
    fi
}

# ---------------------------------------------------------------------------
# Subcommands
# ---------------------------------------------------------------------------

cmd_status() {
    echo "Go-live state:"
    py_state get
    echo
    echo "Live age (days): $(py_state days_live)"
    echo "Window PnL (7d):  $(window_pnl 7)"
    echo "Window PnL (30d): $(window_pnl 30)"
}

cmd_init() {
    log "init: validating readiness"
    if ! python3 "${ROOT_DIR}/scripts/validate_readiness.py"; then
        die "validate_readiness.py failed — aborting init"
    fi
    log "init: validation passed; flipping dry_run=false, ratio=0.10"
    patch_config 0.10 false
    py_state set_stage 1 0.10 init
    py_state record_event "INIT" "stage=1 ratio=0.10"
    restart_bot
    log "init: live trading started at stage 1 (ratio 0.10)"
}

cmd_advance() {
    local cur_stage cur_days
    cur_stage="$(py_state stage)"
    cur_days="$(py_state days_live)"

    if [[ "$cur_stage" == "0" ]]; then
        die "not yet live — run 'init' first"
    fi

    case "$cur_stage" in
        1)
            (( cur_days >= 7 )) || die "stage 1 requires 7 days live (have $cur_days)"
            local pnl; pnl="$(window_pnl 7)"
            python3 -c "import sys; sys.exit(0 if float('$pnl') > 0 else 1)" \
                || die "stage 1 -> 2 requires last-7d PnL > 0 (have $pnl)"
            log "advance: stage 1 → 2; ratio 0.30  (week-1 PnL=$pnl)"
            patch_config 0.30 false
            py_state set_stage 2 0.30 "week1_pnl=$pnl"
            ;;
        2)
            (( cur_days >= 14 )) || die "stage 2 requires 14 days live (have $cur_days)"
            local pnl; pnl="$(window_pnl 7)"
            python3 -c "import sys; sys.exit(0 if float('$pnl') > 0 else 1)" \
                || die "stage 2 -> 3 requires last-7d PnL > 0 (have $pnl)"
            log "advance: stage 2 → 3; ratio 0.50  (week-2 PnL=$pnl)"
            patch_config 0.50 false
            py_state set_stage 3 0.50 "week2_pnl=$pnl"
            ;;
        3)
            (( cur_days >= 30 )) || die "stage 3 requires 30 days live (have $cur_days)"
            local pnl; pnl="$(window_pnl 30)"
            python3 -c "import sys; sys.exit(0 if float('$pnl') > 0 else 1)" \
                || die "stage 3 -> 4 requires last-30d PnL > 0 (have $pnl)"
            log "advance: stage 3 → 4; ratio 0.99  (30d PnL=$pnl)"
            patch_config 0.99 false
            py_state set_stage 4 0.99 "month1_pnl=$pnl"
            ;;
        4)
            log "already at stage 4 (max ratio 0.99) — nothing to advance"
            return 0
            ;;
        *)
            die "unknown stage: $cur_stage"
            ;;
    esac
    restart_bot
    log "advance: complete"
}

cmd_set() {
    local ratio="${1:-}"
    [[ -n "$ratio" ]] || die "usage: $0 set <ratio>"
    log "set: pinning ratio=$ratio (operator override)"
    patch_config "$ratio" false
    py_state record_event "OVERRIDE" "ratio=$ratio"
    restart_bot
}

# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------

case "${1:-status}" in
    init)    cmd_init ;;
    advance) cmd_advance ;;
    status)  cmd_status ;;
    set)     shift; cmd_set "$@" ;;
    *)       echo "usage: $0 {init|advance|status|set <ratio>}" >&2; exit 2 ;;
esac
