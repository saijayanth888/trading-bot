# GPU gate wraps applied to live ~/.hermes/scripts/ (Phase 1)

The five scripts below are intentionally gitignored per `.gitignore` lines
138-143 (Hermes-generated/operator-local cron wrappers). They live ONLY at
`~/.hermes/scripts/`. This file captures the wrap-block that was inserted at
the top of each, so the change is reviewable in the PR and reproducible if the
live filesystem is ever rebuilt.

## Insertion pattern

Inserted immediately after the script's `set -uo pipefail` (or `set -euo pipefail`)
line, before any other logic:

```bash
# GPU reservation gate — yield to ModelForge training windows.
if [[ -x "$HOME/.hermes/scripts/gpu_gate.sh" ]] && \
   ! "$HOME/.hermes/scripts/gpu_gate.sh" check --caller <CALLER_NAME>; then
    echo "[$(date -Is)] gpu_gate: skipping <CALLER_NAME> (GPU reserved)" >> "$HOME/.hermes/logs/gpu_gate.log"
    exit 0
fi
```

The `-x` guard ensures the script keeps running on hosts where the gate
isn't installed yet (dev boxes, CI).

## Files modified + caller names

| Live script                                  | Caller name        | Insertion point          |
|----------------------------------------------|--------------------|--------------------------|
| `~/.hermes/scripts/refresh_sentiment.sh`     | `sentiment-engine` | after `set -euo pipefail` (line 14) |
| `~/.hermes/scripts/risk_monitor_15min.sh`    | `risk-monitor`     | after `set -uo pipefail` (line 19)  |
| `~/.hermes/scripts/market_research_30min.sh` | `market-research`  | after `set -uo pipefail` (line 8)   |
| `~/.hermes/scripts/post_mortem_weekly.sh`    | `post-mortem`      | after `set -uo pipefail` (line 8)   |
| `~/.hermes/scripts/shark_briefing_alerts.sh` | `shark-briefing`   | after `set -uo pipefail` (line 8)   |

## Verification

```
$ HERMES_GPU_GATE_NOW="2026-05-17T19:00:00+00:00" \
    ~/.hermes/scripts/refresh_sentiment.sh
$ echo $?
0     # gate triggered → script skipped cleanly

$ tail -1 ~/.hermes/logs/gpu_gate.log
[…] gpu_gate: skipping sentiment-engine (GPU reserved)
```

Confirmed for all 5 scripts at wrap time (2026-05-12).

## Explicitly NOT wrapped

- `ollama_health.sh` — probe that should ALWAYS run regardless of reservation.
- `daily_pnl_report.sh` — operator wants this no matter what.
- `ept_*.sh` — those are GPU-using but ModelForge owns them.

The tracked `.hermes/scripts/nightly_reflector.sh` is wrapped in the same
commit chain as a normal git-tracked edit.
