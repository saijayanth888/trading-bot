# HANDOFF — GPU Reservation Phase 1

Branch: `feat/gpu-reservation-phase1` (8 commits, **NOT pushed**)
Worktree: `/home/saijayanthai/Documents/trading-bot/.claude/worktrees/agent-ab87934521e5d7f4c`

## What was built (one paragraph)

A GPU reservation system that lets the trading bot yield cleanly to ModelForge's
Sunday 14:00-18:00 EST LoRA training window. A YAML schedule file is the Phase-1
single source of truth (Phase 2 will swap it for a live `/api/forge/gpu_lease`
read). A bash gate (`gpu_gate.sh`) parses the schedule, computes TZ-aware cron
windows, and returns exit-code-driven gating decisions: 0 = open or self-held,
1 = blocked (caller skips cleanly), 2 = config error (fail-OPEN — never block on
infra). An eviction hook (`gpu_yield_now.sh`) force-evicts Ollama models via
`/api/generate keep_alive=0` 5 minutes before each reservation. A resume hook
(`gpu_resume.sh`) clears the yielded marker, posts Slack, and pre-warms
hermes3:8b. Six cron-driven scripts (sentiment / risk_debate / reflector /
market_research / post_mortem / shark_briefing) are wrapped with a 6-line
gate-check at the top. Probes (`ollama_health`), deterministic P&L
(`daily_pnl_report`), and ModelForge's own EPT scripts are intentionally NOT
wrapped. An 8-case bash test suite passes 8/8.

## File tree (new/modified)

```
.gitignore                                                  [M] +3 lines (allowlist gpu_*.sh)
.hermes/scripts/gpu_gate.sh                                 [A] +493
.hermes/scripts/gpu_yield_now.sh                            [A] +161
.hermes/scripts/gpu_resume.sh                               [A] +107
.hermes/scripts/nightly_reflector.sh                        [M] +7  (gate wrap)
tests/test_gpu_gate.sh                                      [A] +135
user_data/config/gpu_reservation.example.yaml               [A] +37
user_data/config/recommended_crons_gpu_reservation.txt      [A] +20
user_data/config/gpu_gate_live_wraps_applied.md             [A] +57

Live filesystem (gitignored, edited in-place — see manifest doc):
  ~/.hermes/scripts/refresh_sentiment.sh        (caller=sentiment-engine)
  ~/.hermes/scripts/risk_monitor_15min.sh       (caller=risk-monitor)
  ~/.hermes/scripts/market_research_30min.sh    (caller=market-research)
  ~/.hermes/scripts/post_mortem_weekly.sh       (caller=post-mortem)
  ~/.hermes/scripts/shark_briefing_alerts.sh    (caller=shark-briefing)
  ~/.hermes/config/gpu_reservation.yaml         (copy of repo example)
  ~/.hermes/scripts/gpu_gate.sh                 (installed)
  ~/.hermes/scripts/gpu_yield_now.sh            (installed)
  ~/.hermes/scripts/gpu_resume.sh               (installed)

Memory (out-of-tree):
  ~/.claude/projects/-home-saijayanthai-Documents-trading-bot/memory/reference_gpu_reservation.md
  ~/.claude/projects/-home-saijayanthai-Documents-trading-bot/memory/MEMORY.md (index updated)
```

## Commits

| SHA       | Subject |
|-----------|---------|
| d79e6a8   | schedule file — Sunday 14:00 EST 4h LoRA window |
| 10ba1f2   | gpu_gate.sh — check/status/next/acquire/release |
| d8b7b93   | gpu_yield_now.sh — Ollama VRAM eviction hook |
| 11bcc57   | gpu_resume.sh — close-out + prewarm hook |
| 1ba6beb   | recommended crontab entries for operator |
| 2ac0b72   | tests/test_gpu_gate.sh — 8/8 pass |
| f7c6cd5   | wrap nightly_reflector with gpu_gate |
| fee77ef   | record live-script wraps in manifest doc |

8 atomic commits. Diff summary: `+1020 lines, 9 files`.

## Schedule format

File: `~/.hermes/config/gpu_reservation.yaml`
Reference: `user_data/config/gpu_reservation.example.yaml`

```yaml
reservations:
  - holder: modelforge-weekly-lora-training
    description: Weekly LoRA training for trading agent adapters
    schedule_cron: "0 14 * * 0"           # min hour dom mon dow (Sun=0)
    tz: America/New_York                   # IANA tz, cron evaluated in this tz
    duration_hours: 4
    pre_drain_minutes: 5                   # blocked window starts 5 min before
    grace_minutes: 30                      # blocked window extends 30 min after end
```

To edit live schedule: `nano ~/.hermes/config/gpu_reservation.yaml`. No restart
needed — `gpu_gate.sh` re-reads on every invocation.

To add an ad-hoc one-off hold:
```bash
~/.hermes/scripts/gpu_gate.sh acquire <holder-name> <duration-minutes>
~/.hermes/scripts/gpu_gate.sh release <holder-name>
```

## Recommended cron lines (paste into `crontab -e`)

```
# Pre-drain at Sunday 13:55 EST (5 min before reservation)
55 13 * * 0 TZ=America/New_York /home/saijayanthai/.hermes/scripts/gpu_yield_now.sh >> /home/saijayanthai/.hermes/logs/gpu_gate.log 2>&1

# Resume at Sunday 18:30 EST (4h + 30min grace)
30 18 * * 0 TZ=America/New_York /home/saijayanthai/.hermes/scripts/gpu_resume.sh >> /home/saijayanthai/.hermes/logs/gpu_gate.log 2>&1
```

Reference copy: `user_data/config/recommended_crons_gpu_reservation.txt`.

## Test results — 8/8 pass

```
── GPU reservation gate tests ──
  PASS  open outside window (Fri 12:00 UTC) (exit=0)
  PASS  blocked inside window (Sun 19:00 UTC = 15:00 EST) (exit=1)
  PASS  holder-self pass inside window (exit=0)
  PASS  missing config → fail-OPEN (exit 2) (exit=2)
  PASS  blocked inside pre-drain (Sun 17:56 UTC = 13:56 EST) (exit=1)
  PASS  blocked inside grace (Sun 22:29 UTC = 18:29 EST) (exit=1)
  PASS  open just after grace (Sun 22:31 UTC = 18:31 EST) (exit=0)
  PASS  HERMES_GPU_GATE_DISABLE=1 bypass (exit=0)

── Results: 8/8 passed, 0 failed ──
```

Required 4 cases (per spec) + 4 bonus edge cases (pre-drain, grace, just-after-grace, emergency override).

Run: `bash tests/test_gpu_gate.sh`

## Emergency override

```bash
HERMES_GPU_GATE_DISABLE=1 <any-wrapped-script>.sh
```

Bypasses the gate for the current shell only. Useful for emergency manual runs
during a training window when operator knowingly accepts the GPU contention.

Other env hooks:
- `HERMES_GPU_GATE_CONFIG=<path>` — override config path (default `~/.hermes/config/gpu_reservation.yaml`)
- `HERMES_GPU_GATE_NOW=<iso-ts>` — pretend "now" is this timestamp (test hook)
- `HERMES_GPU_GATE_RUNTIME=<path>` — override runtime-lease JSON path
- `HERMES_GPU_GATE_LOG=<path>` — override log path

## Operator verification steps (post-merge)

1. `~/.hermes/scripts/gpu_gate.sh status` → prints `GPU: OPEN — no active reservation` + next window
2. `~/.hermes/scripts/gpu_gate.sh acquire test-holder 10` → status shows `RESERVED_BY=test-holder`
3. `~/.hermes/scripts/gpu_gate.sh check --caller sentiment-engine` → exit 1
4. `~/.hermes/scripts/gpu_gate.sh check --caller test-holder` → exit 0 (holder itself)
5. `~/.hermes/scripts/gpu_gate.sh release test-holder` → status back to OPEN
6. Paste the two cron lines via `crontab -e`
7. Smoke test eviction: `~/.hermes/scripts/gpu_yield_now.sh` → `/api/ps` shows no models; Slack `:zzz:` message
8. Smoke test resume: `~/.hermes/scripts/gpu_resume.sh` → Slack `:vertical_traffic_light:` message
9. Spot-check each wrapped script has the gate-check on line 16-23 (after shebang + comments + `set`).

## Phase 2 punchlist (deferred)

- **ModelForge weekly LoRA training workflow** at 14:00 EST — the actual GPU consumer.
- **`/api/forge/gpu_lease` endpoint** — exposes ModelForge's lease state so trading bot can replace the static YAML.
- **Dashboard "GPU Reservation" card** — held back per spec; parallel agent owns `user_data/dashboard/` files until they merge.
- Once Phase 2 lands, `gpu_gate.sh` should grow a `--source modelforge` flag (or auto-detect) that prefers the live endpoint with YAML fallback.

## Known limits

- **No US-market holiday calendar** — the cron fires every Sunday regardless. If
  training is paused for a holiday, operator must manually skip the cron run.
- **Lease overrun is a fixed 30-min grace** — not adaptive to actual training
  duration. Phase 2 should switch to ModelForge's live "done" signal.
- **YAML parser is hand-rolled** to avoid PyYAML dep. It only understands the
  narrow schema in `gpu_reservation.example.yaml`; do NOT add nested objects /
  anchors / multiline strings without extending `parse_yaml()` in gpu_gate.sh.
- **No tracked git copy of 5 cron wrappers** — they're in `.gitignore`. The
  wrap-block is documented in `user_data/config/gpu_gate_live_wraps_applied.md`
  for reproducibility but the live files themselves are not under version
  control.

## Constraints honored

- [x] NO ModelForge changes (Phase 2 territory)
- [x] NO dashboard changes (`user_data/dashboard/` untouched — parallel agent owns it)
- [x] NO freqtrade restart, no docker rebuild
- [x] NO new heavy containers
- [x] Branch `feat/gpu-reservation-phase1`, NOT pushed
- [x] Isolated worktree
