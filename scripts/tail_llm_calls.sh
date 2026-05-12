#!/usr/bin/env bash
#
# tail_llm_calls.sh — operator terminal viewer for the LLM-call log.
#
# Tails ``stocks/memory/llm-calls.jsonl`` and pretty-prints each line as
# a compact one-row summary. Optionally shows the prompt/response previews
# (--full), filters by agent (--agent), and back-fills the last N records
# at startup (--since 1h).
#
# This is the CLI complement to the dashboard's LLMCallsLive card —
# operator uses one or the other; both read the same file. Cleaning up the
# "very ugly" raw cat experience was the whole point of this branch.
#
# Usage:
#     bash scripts/tail_llm_calls.sh
#     bash scripts/tail_llm_calls.sh --full
#     bash scripts/tail_llm_calls.sh --agent reflector --since 2h
#     bash scripts/tail_llm_calls.sh --help
#
# Dependencies: jq, tail, awk (all standard on Linux/macOS).

set -euo pipefail

# ── Resolve the log path. Honors $SHARK_TRACKER_LOG so the operator
# can point at a non-default file (matches stocks/shark/llm/tracker.py).
LOG_PATH="${SHARK_TRACKER_LOG:-}"
if [[ -z "$LOG_PATH" ]]; then
    # Prefer the worktree's own copy; fall back to the main repo's.
    candidates=(
        "$(cd "$(dirname "$0")/.." && pwd)/stocks/memory/llm-calls.jsonl"
        "/home/saijayanthai/Documents/trading-bot/stocks/memory/llm-calls.jsonl"
        "/freqtrade/stocks/memory/llm-calls.jsonl"
    )
    for c in "${candidates[@]}"; do
        if [[ -f "$c" ]]; then LOG_PATH="$c"; break; fi
    done
fi

FULL=0
AGENT_FILTER=""
SINCE=""

# Cheap getopts (--long-only). We tolerate -h / --help for discoverability.
while [[ $# -gt 0 ]]; do
    case "$1" in
        --full) FULL=1; shift ;;
        --agent) AGENT_FILTER="$2"; shift 2 ;;
        --since) SINCE="$2"; shift 2 ;;
        -h|--help)
            cat <<'EOF'
tail_llm_calls.sh — live viewer for stocks/memory/llm-calls.jsonl

Options:
  --full           Show prompt + response previews (200 char truncation)
  --agent NAME     Only show rows whose agent field == NAME
  --since DUR      Back-fill records from the last DUR (e.g. 1h, 30m, 2d)
                   before starting the live tail
  -h, --help       This message

Environment:
  SHARK_TRACKER_LOG  Override the log path (matches the tracker)

Format (default):
  HH:MM:SS  AGENT                MODEL          LATs  PROMPT/COMPL
  14:32:11  reflector            qwen3:30b       8.2  421/180

With --full each row is followed by indented "system:", "user:" and
"reply:" excerpts (≤200 chars each).
EOF
            exit 0
            ;;
        *) echo "unknown option: $1 — try --help" >&2; exit 2 ;;
    esac
done

if [[ -z "$LOG_PATH" || ! -f "$LOG_PATH" ]]; then
    echo "log not found. Searched:" >&2
    echo "  \$SHARK_TRACKER_LOG=${SHARK_TRACKER_LOG:-<unset>}" >&2
    echo "  $(cd "$(dirname "$0")/.." && pwd)/stocks/memory/llm-calls.jsonl" >&2
    echo "  /home/saijayanthai/Documents/trading-bot/stocks/memory/llm-calls.jsonl" >&2
    echo "  /freqtrade/stocks/memory/llm-calls.jsonl" >&2
    exit 1
fi

if ! command -v jq >/dev/null 2>&1; then
    echo "jq is required. Install with: apt-get install jq" >&2
    exit 1
fi

# ── Convert SINCE (e.g. "1h", "30m") to epoch seconds.
since_epoch=0
if [[ -n "$SINCE" ]]; then
    n="${SINCE%[a-zA-Z]*}"
    u="${SINCE: -1}"
    case "$u" in
        s) mult=1 ;;
        m) mult=60 ;;
        h) mult=3600 ;;
        d) mult=86400 ;;
        *) echo "bad --since unit '$u' (use s/m/h/d)" >&2; exit 2 ;;
    esac
    if ! [[ "$n" =~ ^[0-9]+$ ]]; then
        echo "bad --since count '$n'" >&2; exit 2
    fi
    since_epoch=$(( $(date -u +%s) - n * mult ))
fi

# ── jq filter — one place defines the row format.
# Input: one JSON object per line. Output: one printf-friendly line.
# We compute a colour code for latency in jq itself so awk doesn't
# have to re-parse the latency_seconds value.
JQ_ROW='
def to_secs(ts): ts | sub("Z$"; "+00:00") | fromdate;
def fmtnum(n; w): (n | tostring) | (if length < w then (" " * (w - length)) + . else . end);

# ANSI colour codes — green <2s, yellow 2-5s, orange 5-15s, red >15s
def lat_color(s):
  if s < 2 then "[32m"
  elif s < 5 then "[33m"
  elif s < 15 then "[38;5;208m"
  else "[31m"
  end;
def reset: "[0m";

(.timestamp // "") as $ts |
(.timestamp | sub("Z$"; "+00:00") | fromdate? // 0) as $epoch |
(.agent // "?") as $agent |
(.model // "?") as $model |
(.tier // "?") as $tier |
(.latency_seconds // 0) as $lat |
(.prompt_tokens // 0) as $ptok |
(.completion_tokens // 0) as $ctok |

# Time of day from the ISO timestamp ("YYYY-MM-DDTHH:MM:SS...")
($ts | split("T")[1] | .[0:8]) as $hms |

# Agent gets padded to 20 chars; model gets padded to 14
(($agent + (if ($agent | length) < 20 then " " * (20 - ($agent | length)) else " " end))[0:20]) as $agp |
(($model + (if ($model | length) < 14 then " " * (14 - ($model | length)) else " " end))[0:14]) as $mdp |

($epoch >= ($SINCE_EPOCH | tonumber)) as $in_window |
(if ($AGENT_FILTER == "") then true else $agent == $AGENT_FILTER end) as $matches_agent |

if ($in_window and $matches_agent) then
  "\($hms)  \($agp) \($mdp) \(lat_color($lat))\(($lat * 10 | floor) / 10)s\(reset)   \($ptok)/\($ctok)" +
  (if ($FULL_FLAG | tonumber) == 1 then
    (if .system_message then "\n          system: " + ((.system_message | tostring)[0:200] | gsub("\n"; " ")) else "" end) +
    (if .prompt        then "\n          user:   " + ((.prompt        | tostring)[0:200] | gsub("\n"; " ")) else "" end) +
    (if .response_text then "\n          reply:  " + ((.response_text | tostring)[0:200] | gsub("\n"; " ")) else "" end)
  else "" end)
else empty end
'

# Print a header row once.
printf "\033[2m%s  %-20s %-14s %-7s %s\033[0m\n" "TIME    " "AGENT" "MODEL" "  LAT" "P_TOK/C_TOK"

# Two-stage pipeline:
#   1. Back-fill: print everything in the file matching --since + --agent.
#   2. Live tail: keep streaming new lines as they're appended.
#
# tail -F follows the file across truncations (the rotator at
# stocks/shark/llm/rotate.py truncates the live file rather than
# renaming, so the descriptor stays valid).
tail -n +1 -F "$LOG_PATH" 2>/dev/null \
    | jq --unbuffered -r \
         --arg AGENT_FILTER "$AGENT_FILTER" \
         --arg SINCE_EPOCH "$since_epoch" \
         --arg FULL_FLAG "$FULL" \
         "$JQ_ROW"
