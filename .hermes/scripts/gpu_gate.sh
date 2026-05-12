#!/usr/bin/env bash
# gpu_gate.sh — GPU reservation gate for the trading bot.
#
# Phase 1: reads ~/.hermes/config/gpu_reservation.yaml and tells callers whether
# the GPU is currently reserved by another holder. Heavy GPU consumers (sentiment,
# risk_debate, reflector, market_research, post_mortem, shark_briefing) call this
# at the top of their cron entry to skip cleanly during ModelForge training
# windows. Phase 2 will swap the YAML for a live read from ModelForge's
# /api/forge/gpu_lease endpoint.
#
# Subcommands
# -----------
#   check --caller <name>   — exit 0 if open OR caller is the holder, 1 if blocked, 2 on infra error
#   status                  — human-readable: open/reserved + time-remaining
#   next                    — when's the next reservation
#   acquire <holder>        — runtime override: claim the GPU until release
#   release <holder>        — clear the runtime override
#
# Exit codes for `check`
# ----------------------
#   0 = GPU available, caller may proceed
#   1 = GPU reserved by another holder, caller MUST skip (NOT an error)
#   2 = config missing or parse error — fail-OPEN, log + proceed
#
# Emergency override
# ------------------
#   HERMES_GPU_GATE_DISABLE=1 — bypass the gate for the current shell
#
# Runtime override (ad-hoc holds)
# -------------------------------
#   gpu_gate.sh acquire <holder>  writes ~/.hermes/state-snapshots/gpu_lease_runtime.json
#   gpu_gate.sh release <holder>  removes it
#
# Logs to ~/.hermes/logs/gpu_gate.log on block/error.

set -uo pipefail

CONFIG_FILE="${HERMES_GPU_GATE_CONFIG:-$HOME/.hermes/config/gpu_reservation.yaml}"
RUNTIME_LEASE_FILE="${HERMES_GPU_GATE_RUNTIME:-$HOME/.hermes/state-snapshots/gpu_lease_runtime.json}"
LOG_FILE="${HERMES_GPU_GATE_LOG:-$HOME/.hermes/logs/gpu_gate.log}"
NOW_OVERRIDE="${HERMES_GPU_GATE_NOW:-}"   # ISO ts override for tests

mkdir -p "$(dirname "$LOG_FILE")" "$(dirname "$RUNTIME_LEASE_FILE")"

_log() {
    echo "[$(date -Is)] gpu_gate: $*" >> "$LOG_FILE"
}

_python_query() {
    # Args: subcommand caller_name
    # Outputs (stdout) a single line:
    #   OPEN
    #   BLOCKED holder=<h> end_iso=<ts> remaining_seconds=<n>
    #   ERROR <message>
    # Always returns 0 from python; caller reads stdout to decide exit code.
    local subcmd="$1"
    local caller="${2:-}"
    HERMES_GPU_GATE_NOW_PY="$NOW_OVERRIDE" \
    HERMES_GPU_GATE_CONFIG_PY="$CONFIG_FILE" \
    HERMES_GPU_GATE_RUNTIME_PY="$RUNTIME_LEASE_FILE" \
    HERMES_GPU_GATE_SUBCMD="$subcmd" \
    HERMES_GPU_GATE_CALLER="$caller" \
    python3 - <<'PY'
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

CONFIG = os.environ.get("HERMES_GPU_GATE_CONFIG_PY", "")
RUNTIME = os.environ.get("HERMES_GPU_GATE_RUNTIME_PY", "")
SUBCMD = os.environ.get("HERMES_GPU_GATE_SUBCMD", "check")
CALLER = os.environ.get("HERMES_GPU_GATE_CALLER", "")
NOW_OVERRIDE = os.environ.get("HERMES_GPU_GATE_NOW_PY", "").strip()


def now_utc():
    if NOW_OVERRIDE:
        # Accept ISO8601 with optional 'Z' or offset
        ts = NOW_OVERRIDE.replace("Z", "+00:00")
        try:
            dt = datetime.fromisoformat(ts)
        except ValueError:
            sys.stdout.write(f"ERROR bad-now-override:{NOW_OVERRIDE}\n")
            sys.exit(0)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    return datetime.now(timezone.utc)


def parse_yaml(path):
    """Minimal YAML parser for our schema (list of dict entries under 'reservations').

    We avoid pulling in PyYAML so the gate has zero non-stdlib dependencies.
    Supports: top-level 'reservations:' list, '- key: value' / '  key: value'
    syntax, quoted/unquoted strings, ints, floats, inline '#' comments.
    """
    try:
        with open(path, "r") as f:
            lines = f.readlines()
    except FileNotFoundError:
        return None, f"missing-config:{path}"
    except OSError as exc:
        return None, f"read-error:{exc}"

    reservations = []
    current = None
    in_reservations = False
    for raw in lines:
        # Strip inline comments BUT respect quoted strings
        line = raw.rstrip("\n")
        # Find first unquoted '#'
        in_quote = None
        stripped = []
        for ch in line:
            if in_quote:
                stripped.append(ch)
                if ch == in_quote:
                    in_quote = None
                continue
            if ch in ('"', "'"):
                in_quote = ch
                stripped.append(ch)
                continue
            if ch == "#":
                break
            stripped.append(ch)
        line = "".join(stripped).rstrip()
        if not line.strip():
            continue
        if line.startswith("reservations:"):
            in_reservations = True
            continue
        if not in_reservations:
            continue
        # New entry
        m = re.match(r"^\s*-\s+(\w+)\s*:\s*(.*)$", line)
        if m:
            if current is not None:
                reservations.append(current)
            current = {}
            key, val = m.group(1), m.group(2).strip()
            current[key] = _coerce(val)
            continue
        # Continuation field on current entry
        m = re.match(r"^\s+(\w+)\s*:\s*(.*)$", line)
        if m and current is not None:
            key, val = m.group(1), m.group(2).strip()
            current[key] = _coerce(val)
            continue
    if current is not None:
        reservations.append(current)
    return reservations, None


def _coerce(val):
    if val == "":
        return ""
    if (val.startswith('"') and val.endswith('"')) or (val.startswith("'") and val.endswith("'")):
        return val[1:-1]
    try:
        if "." in val:
            return float(val)
        return int(val)
    except ValueError:
        return val


# --- cron parsing -----------------------------------------------------------

def _expand(field, lo, hi):
    """Expand one cron field to a sorted list of ints in [lo, hi]."""
    if field == "*":
        return list(range(lo, hi + 1))
    out = set()
    for part in field.split(","):
        step = 1
        if "/" in part:
            base, step_str = part.split("/", 1)
            step = int(step_str)
        else:
            base = part
        if base == "*":
            start, end = lo, hi
        elif "-" in base:
            a, b = base.split("-", 1)
            start, end = int(a), int(b)
        else:
            start = end = int(base)
        for v in range(start, end + 1, step):
            if lo <= v <= hi:
                out.add(v)
    return sorted(out)


def cron_matches(dt_local, spec):
    """Does `dt_local` (TZ-aware) match the 5-field cron `spec`?"""
    parts = spec.split()
    if len(parts) != 5:
        raise ValueError(f"bad-cron:{spec}")
    minute, hour, dom, month, dow = parts
    minutes = _expand(minute, 0, 59)
    hours = _expand(hour, 0, 23)
    doms = _expand(dom, 1, 31)
    months = _expand(month, 1, 12)
    # cron dow: 0 = Sunday; Python weekday() Mon=0..Sun=6, isoweekday() Mon=1..Sun=7
    # Convert isoweekday to cron-style (Sun=0): (iso % 7)
    dows = _expand(dow, 0, 7)  # allow both 0 and 7 for Sunday
    cur_dow = dt_local.isoweekday() % 7
    return (
        dt_local.minute in minutes
        and dt_local.hour in hours
        and dt_local.day in doms
        and dt_local.month in months
        and (cur_dow in dows or (cur_dow == 0 and 7 in dows))
    )


def find_window_for(now_utc_dt, res):
    """Return (start_utc, end_utc) if `now_utc_dt` is currently inside the
    pre-drain..end+grace window for this reservation, else None. Also returns
    the closest future window for `next` queries.
    """
    tz = ZoneInfo(res.get("tz", "UTC"))
    spec = res.get("schedule_cron", "")
    duration = float(res.get("duration_hours", 0))
    pre = int(res.get("pre_drain_minutes", 0))
    grace = int(res.get("grace_minutes", 0))
    now_local = now_utc_dt.astimezone(tz)
    # Walk back up to 2 days, forward up to 14 days. Cron is minute-precision;
    # we only need to find candidate START times (cron-match instants) and then
    # check if now is in [start - pre, start + duration + grace].
    # Iterate at minute granularity over a bounded window.
    start_search = (now_local - timedelta(days=2)).replace(second=0, microsecond=0)
    end_search = now_local + timedelta(days=14)
    cur = start_search
    active = None
    next_start = None
    while cur <= end_search:
        try:
            if cron_matches(cur, spec):
                win_start = cur - timedelta(minutes=pre)
                win_end = cur + timedelta(hours=duration) + timedelta(minutes=grace)
                if win_start <= now_local <= win_end and active is None:
                    active = (cur.astimezone(timezone.utc), win_end.astimezone(timezone.utc))
                if cur > now_local and next_start is None:
                    next_start = cur.astimezone(timezone.utc)
                if active and next_start:
                    break
        except ValueError as exc:
            return None, f"bad-cron:{exc}", None
        cur += timedelta(minutes=1)
    return active, None, next_start


def read_runtime_lease(now_utc_dt):
    """Return (holder, end_utc) or (None, None). Expired leases are ignored."""
    try:
        with open(RUNTIME, "r") as f:
            data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None, None
    holder = data.get("holder")
    end_str = data.get("end_utc")
    if not holder:
        return None, None
    end_dt = None
    if end_str:
        try:
            end_dt = datetime.fromisoformat(end_str.replace("Z", "+00:00"))
            if end_dt.tzinfo is None:
                end_dt = end_dt.replace(tzinfo=timezone.utc)
            if end_dt < now_utc_dt:
                return None, None
        except ValueError:
            return None, None
    return holder, end_dt


def main():
    now = now_utc()
    reservations, err = parse_yaml(CONFIG)
    if err:
        sys.stdout.write(f"ERROR {err}\n")
        return
    if reservations is None:
        reservations = []

    # Runtime override takes precedence (operator-set ad-hoc lease)
    runtime_holder, runtime_end = read_runtime_lease(now)
    if runtime_holder:
        if SUBCMD == "next":
            sys.stdout.write(
                f"RUNTIME holder={runtime_holder} end_iso={runtime_end.isoformat() if runtime_end else 'open'}\n"
            )
            return
        if CALLER and CALLER == runtime_holder:
            sys.stdout.write("OPEN holder-self\n")
            return
        end_iso = runtime_end.isoformat() if runtime_end else "open"
        rem = int((runtime_end - now).total_seconds()) if runtime_end else -1
        sys.stdout.write(
            f"BLOCKED holder={runtime_holder} end_iso={end_iso} remaining_seconds={rem} source=runtime\n"
        )
        return

    # Scheduled reservations
    next_starts = []
    for res in reservations:
        active, errspec, nxt = find_window_for(now, res)
        if errspec:
            sys.stdout.write(f"ERROR {errspec}\n")
            return
        if nxt:
            next_starts.append((nxt, res.get("holder", "?")))
        if active is None:
            continue
        start_utc, end_utc = active
        holder = res.get("holder", "?")
        if CALLER and CALLER == holder:
            sys.stdout.write("OPEN holder-self\n")
            return
        rem = int((end_utc - now).total_seconds())
        sys.stdout.write(
            f"BLOCKED holder={holder} end_iso={end_utc.isoformat()} remaining_seconds={rem} source=schedule\n"
        )
        return

    if SUBCMD == "next":
        if next_starts:
            nxt, holder = sorted(next_starts)[0]
            sys.stdout.write(f"NEXT holder={holder} start_iso={nxt.isoformat()}\n")
        else:
            sys.stdout.write("NEXT none\n")
        return

    sys.stdout.write("OPEN\n")


main()
PY
}

cmd="${1:-status}"
shift || true

# Emergency override
if [[ "${HERMES_GPU_GATE_DISABLE:-0}" == "1" ]]; then
    case "$cmd" in
        check)
            >&2 echo "GPU_GATE=open (HERMES_GPU_GATE_DISABLE=1)"
            exit 0
            ;;
        status)
            echo "OPEN (HERMES_GPU_GATE_DISABLE=1 — emergency override active)"
            exit 0
            ;;
    esac
fi

case "$cmd" in
    check)
        caller=""
        while [[ $# -gt 0 ]]; do
            case "$1" in
                --caller) caller="$2"; shift 2 ;;
                --caller=*) caller="${1#--caller=}"; shift ;;
                *) shift ;;
            esac
        done
        if [[ -z "$caller" ]]; then
            >&2 echo "gpu_gate: check requires --caller <name>"
            exit 2
        fi
        result=$(_python_query check "$caller")
        case "$result" in
            OPEN*)
                >&2 echo "GPU_GATE=open"
                exit 0
                ;;
            BLOCKED*)
                # Extract holder + end_iso for stderr message
                holder=$(echo "$result" | grep -oE 'holder=[^ ]+' | head -1 | cut -d= -f2)
                end_iso=$(echo "$result" | grep -oE 'end_iso=[^ ]+' | head -1 | cut -d= -f2)
                >&2 echo "RESERVED_BY=${holder} until=${end_iso}"
                _log "blocked caller=$caller $result"
                exit 1
                ;;
            ERROR*)
                >&2 echo "gpu_gate: $result (fail-OPEN)"
                _log "fail-open caller=$caller $result"
                exit 2
                ;;
            *)
                >&2 echo "gpu_gate: unexpected output: $result (fail-OPEN)"
                _log "fail-open caller=$caller unexpected:$result"
                exit 2
                ;;
        esac
        ;;
    status)
        result=$(_python_query status "")
        case "$result" in
            OPEN*)
                echo "GPU: OPEN — no active reservation"
                # Also show next upcoming
                nxt=$(_python_query next "")
                if [[ "$nxt" == NEXT* && "$nxt" != "NEXT none"* ]]; then
                    echo "  next: $(echo "$nxt" | sed 's/NEXT //')"
                fi
                exit 0
                ;;
            BLOCKED*)
                holder=$(echo "$result" | grep -oE 'holder=[^ ]+' | head -1 | cut -d= -f2)
                end_iso=$(echo "$result" | grep -oE 'end_iso=[^ ]+' | head -1 | cut -d= -f2)
                rem=$(echo "$result" | grep -oE 'remaining_seconds=[^ ]+' | head -1 | cut -d= -f2)
                src=$(echo "$result" | grep -oE 'source=[^ ]+' | head -1 | cut -d= -f2)
                echo "GPU: RESERVED_BY=$holder until=$end_iso (${rem}s remaining, source=$src)"
                exit 0
                ;;
            ERROR*)
                echo "GPU: gate-error — $result (fail-OPEN)" >&2
                exit 2
                ;;
            *)
                echo "GPU: unexpected gate output: $result" >&2
                exit 2
                ;;
        esac
        ;;
    next)
        result=$(_python_query next "")
        echo "$result"
        exit 0
        ;;
    acquire)
        holder="${1:-}"
        if [[ -z "$holder" ]]; then
            >&2 echo "gpu_gate: acquire requires <holder>"
            exit 2
        fi
        duration_min="${2:-240}"   # default 4h
        end_iso=$(date -u -d "+${duration_min} minutes" -Is)
        cat > "$RUNTIME_LEASE_FILE" <<JSON
{
  "holder": "$holder",
  "acquired_utc": "$(date -u -Is)",
  "end_utc": "$end_iso",
  "duration_minutes": $duration_min
}
JSON
        echo "GPU: ACQUIRED by $holder until $end_iso (${duration_min}m)"
        _log "acquire holder=$holder end=$end_iso"
        exit 0
        ;;
    release)
        holder="${1:-}"
        if [[ -f "$RUNTIME_LEASE_FILE" ]]; then
            cur_holder=$(python3 -c "import json; print(json.load(open('$RUNTIME_LEASE_FILE')).get('holder',''))" 2>/dev/null || echo "")
            if [[ -n "$holder" && "$holder" != "$cur_holder" ]]; then
                >&2 echo "gpu_gate: cannot release — lease held by $cur_holder, not $holder"
                exit 2
            fi
            rm -f "$RUNTIME_LEASE_FILE"
            echo "GPU: RELEASED (was held by ${cur_holder:-unknown})"
            _log "release holder=$cur_holder"
        else
            echo "GPU: no runtime lease to release"
        fi
        exit 0
        ;;
    *)
        cat >&2 <<USAGE
gpu_gate.sh — GPU reservation gate

Usage:
  gpu_gate.sh check --caller <name>     check whether <name> may use the GPU
                                        exit 0 = open, 1 = blocked, 2 = error (fail-OPEN)
  gpu_gate.sh status                    human-readable status + next reservation
  gpu_gate.sh next                      ISO timestamp of next scheduled reservation
  gpu_gate.sh acquire <holder> [min]    ad-hoc runtime lease (default 240 min)
  gpu_gate.sh release [holder]          clear runtime lease

Env:
  HERMES_GPU_GATE_DISABLE=1             emergency bypass (current shell only)
  HERMES_GPU_GATE_CONFIG=<path>         override config path (default ~/.hermes/config/gpu_reservation.yaml)
  HERMES_GPU_GATE_NOW=<iso>             pretend "now" is this timestamp (test hook)
USAGE
        exit 2
        ;;
esac
