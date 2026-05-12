#!/usr/bin/env bash
# test_gpu_gate.sh — exercise the GPU reservation gate's exit codes.
#
# Run: bash tests/test_gpu_gate.sh
# (or: ./tests/test_gpu_gate.sh after chmod +x)
#
# All assertions use HERMES_GPU_GATE_NOW to inject a deterministic "now"
# and HERMES_GPU_GATE_CONFIG to point at fixtures. The live config and
# the operator's runtime lease are NOT touched.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
GATE_SCRIPT="${HERMES_GPU_GATE_SCRIPT:-$HOME/.hermes/scripts/gpu_gate.sh}"

if [[ ! -x "$GATE_SCRIPT" ]]; then
    echo "FAIL: $GATE_SCRIPT not executable (install gpu_gate.sh first)"
    exit 2
fi

TMPDIR_TEST=$(mktemp -d)
trap 'rm -rf "$TMPDIR_TEST"' EXIT

# Build a fixture config: Sunday 14:00 America/New_York, 4h window, 5m pre, 30m grace
FIXTURE_CONFIG="$TMPDIR_TEST/gpu_reservation.yaml"
cat > "$FIXTURE_CONFIG" <<'YAML'
reservations:
  - holder: modelforge-weekly-lora-training
    description: Test fixture — Sunday 14:00 EST window
    schedule_cron: "0 14 * * 0"
    tz: America/New_York
    duration_hours: 4
    pre_drain_minutes: 5
    grace_minutes: 30
YAML

# Empty runtime lease file (point to tmp so we don't touch operator state)
FIXTURE_RUNTIME="$TMPDIR_TEST/gpu_lease_runtime.json"
FIXTURE_LOG="$TMPDIR_TEST/gpu_gate.log"

pass=0
fail=0
total=0

assert_exit() {
    local desc="$1"; shift
    local want="$1"; shift
    total=$((total + 1))
    "$@"
    local got=$?
    if [[ "$got" == "$want" ]]; then
        echo "  PASS  $desc (exit=$got)"
        pass=$((pass + 1))
    else
        echo "  FAIL  $desc — wanted exit=$want, got=$got"
        fail=$((fail + 1))
    fi
}

run_gate() {
    HERMES_GPU_GATE_CONFIG="$FIXTURE_CONFIG" \
    HERMES_GPU_GATE_RUNTIME="$FIXTURE_RUNTIME" \
    HERMES_GPU_GATE_LOG="$FIXTURE_LOG" \
    "$@" 2>/dev/null
}

run_gate_now() {
    local fake_now="$1"; shift
    HERMES_GPU_GATE_CONFIG="$FIXTURE_CONFIG" \
    HERMES_GPU_GATE_RUNTIME="$FIXTURE_RUNTIME" \
    HERMES_GPU_GATE_LOG="$FIXTURE_LOG" \
    HERMES_GPU_GATE_NOW="$fake_now" \
    "$@" 2>/dev/null
}

echo "── GPU reservation gate tests ──"

# Test 1: window in the future (Friday afternoon) → gate returns 0 (open)
# Friday 2026-05-15 12:00 UTC is well before Sunday 14:00 EST = Sunday 18:00 UTC
assert_exit "open outside window (Fri 12:00 UTC)" 0 \
    run_gate_now "2026-05-15T12:00:00+00:00" \
        "$GATE_SCRIPT" check --caller sentiment-engine

# Test 2: current time inside reservation → gate returns 1 (blocked)
# Sunday 2026-05-17 19:00 UTC = 15:00 EST, inside 14:00-18:00 EST window
assert_exit "blocked inside window (Sun 19:00 UTC = 15:00 EST)" 1 \
    run_gate_now "2026-05-17T19:00:00+00:00" \
        "$GATE_SCRIPT" check --caller sentiment-engine

# Test 3: caller name == holder name → returns 0 even during window
assert_exit "holder-self pass inside window" 0 \
    run_gate_now "2026-05-17T19:00:00+00:00" \
        "$GATE_SCRIPT" check --caller modelforge-weekly-lora-training

# Test 4: missing config → fail-OPEN (exit 2 from check, callers proceed)
assert_exit "missing config → fail-OPEN (exit 2)" 2 \
    env HERMES_GPU_GATE_CONFIG="/tmp/nonexistent-$$.yaml" \
        HERMES_GPU_GATE_RUNTIME="$FIXTURE_RUNTIME" \
        HERMES_GPU_GATE_LOG="$FIXTURE_LOG" \
        "$GATE_SCRIPT" check --caller sentiment-engine

# Bonus: pre-drain window (5 min before start) → blocked
# Sun 17:56 UTC = 13:56 EST = 4 min before 14:00 start, inside pre-drain
assert_exit "blocked inside pre-drain (Sun 17:56 UTC = 13:56 EST)" 1 \
    run_gate_now "2026-05-17T17:56:00+00:00" \
        "$GATE_SCRIPT" check --caller sentiment-engine

# Bonus: grace window (29 min after end) → blocked
# Sun 22:29 UTC = 18:29 EST = 29 min after 18:00 end, inside 30-min grace
assert_exit "blocked inside grace (Sun 22:29 UTC = 18:29 EST)" 1 \
    run_gate_now "2026-05-17T22:29:00+00:00" \
        "$GATE_SCRIPT" check --caller sentiment-engine

# Bonus: just after grace ends (31 min after end) → open
assert_exit "open just after grace (Sun 22:31 UTC = 18:31 EST)" 0 \
    run_gate_now "2026-05-17T22:31:00+00:00" \
        "$GATE_SCRIPT" check --caller sentiment-engine

# Bonus: emergency override
assert_exit "HERMES_GPU_GATE_DISABLE=1 bypass" 0 \
    env HERMES_GPU_GATE_DISABLE=1 \
        HERMES_GPU_GATE_CONFIG="$FIXTURE_CONFIG" \
        HERMES_GPU_GATE_RUNTIME="$FIXTURE_RUNTIME" \
        HERMES_GPU_GATE_LOG="$FIXTURE_LOG" \
        HERMES_GPU_GATE_NOW="2026-05-17T19:00:00+00:00" \
        "$GATE_SCRIPT" check --caller sentiment-engine

echo
echo "── Results: $pass/$total passed, $fail failed ──"

if [[ "$fail" -gt 0 ]]; then
    exit 1
fi
exit 0
