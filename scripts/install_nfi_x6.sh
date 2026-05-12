#!/usr/bin/env bash
# install_nfi_x6.sh — one-command operator-side activation of NFI X6 paper-trading.
#
# This is the single command the operator runs after reviewing this branch:
#
#     bash scripts/install_nfi_x6.sh              # verify gates + activate
#     bash scripts/install_nfi_x6.sh --dry-run    # verify gates only (no activate)
#     bash scripts/install_nfi_x6.sh --uninstall  # stop NFI + disable cron job
#
# What it does (in order):
#
#   Gate 1 — strategy byte-identical to upstream
#               via scripts/nfi_x6_gate_check.sh --dry-run (which runs 1+2)
#   Gate 2 — rapidjson / pandas_ta / talib importable
#   Gate 2.5 — 4h JSON candles exist on disk and ≥ MIN_BARS bars
#               via scripts/nfi_x6_4h_smoke.sh
#   ----- gates 1+2+2.5 OK -----
#   Step A — enable the Hermes cron job 'resample_4h_b1' in ~/.hermes/cron/jobs.json
#   Step B — docker compose --profile nfi up -d freqtrade-nfi
#   Step C — wait ≤120s for healthcheck → emit one-line activation result
#
# Why ALL gates before ANY mutation: NFI X6 stays OFF unless we know
# we can produce signals. Failing fast at gate 2.5 is the prior agent's
# explicit recommendation (NFI_X6_HANDOFF.md "Next-session decisions").

set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
JOBS_JSON="${HERMES_JOBS_JSON:-$HOME/.hermes/cron/jobs.json}"
JOB_ID="${RESAMPLE_4H_JOB_ID:-resample_4h_b1}"

DRY_RUN=0
UNINSTALL=0
case "${1:-}" in
    --dry-run) DRY_RUN=1 ;;
    --uninstall) UNINSTALL=1 ;;
    "") ;;
    *) echo "usage: $0 [--dry-run|--uninstall]" >&2; exit 2 ;;
esac

bold() { printf "\033[1m%s\033[0m\n" "$*"; }
ok()   { printf "\033[32m✓\033[0m %s\n" "$*"; }
fail() { printf "\033[31m✗\033[0m %s\n" "$*"; }
warn() { printf "\033[33m!\033[0m %s\n" "$*"; }

# ── uninstall path ──────────────────────────────────────────────────────────
if [[ "$UNINSTALL" == "1" ]]; then
    bold "Uninstall: stopping freqtrade-nfi + disabling resample_4h cron"
    cd "$REPO_ROOT"
    docker compose --profile nfi down 2>&1 | tail -5 || warn "compose down returned non-zero"
    if [[ -f "$JOBS_JSON" ]]; then
        python3 - "$JOBS_JSON" "$JOB_ID" <<'PY'
import json, sys
from datetime import datetime, timezone
path, jid = sys.argv[1], sys.argv[2]
d = json.loads(open(path).read())
for j in d["jobs"]:
    if j["id"] == jid:
        j["enabled"] = False
        j["paused_at"] = datetime.now(timezone.utc).isoformat()
        j["paused_reason"] = "operator-uninstalled-nfi-x6"
        print(f"disabled job {jid}")
        break
else:
    print(f"job {jid} not found — nothing to disable")
d["updated_at"] = datetime.now(timezone.utc).isoformat()
open(path, "w").write(json.dumps(d, indent=2))
PY
    fi
    ok "uninstall complete"
    exit 0
fi

# ── Gates 1 + 2 ─────────────────────────────────────────────────────────────
bold "Gates 1 + 2 (file integrity + dependencies)"
if ! bash "$REPO_ROOT/scripts/nfi_x6_gate_check.sh" --dry-run; then
    fail "gates 1+2 FAILED — fix before retrying"
    exit 1
fi
ok "gates 1+2 PASS"

# ── Gate 2.5 (4h JSON candles on disk) ──────────────────────────────────────
bold "Gate 2.5 (smoke-fetch 4h JSON candles)"
if ! bash "$REPO_ROOT/scripts/nfi_x6_4h_smoke.sh"; then
    fail "gate 2.5 FAILED — 4h JSON files missing or stale"
    echo
    echo "Seed them with:"
    echo "    bash ~/.hermes/scripts/resample_4h.sh"
    echo "Or (for the first-ever run, fetches 90 days):"
    echo "    REPO=$REPO_ROOT RESAMPLE_4H_DAYS=90 bash ~/.hermes/scripts/resample_4h.sh"
    exit 1
fi
ok "gate 2.5 PASS"

if [[ "$DRY_RUN" == "1" ]]; then
    bold "--dry-run: all gates PASS; not activating"
    exit 0
fi

# ── Step A: enable the cron job ─────────────────────────────────────────────
bold "Step A: enable Hermes cron 'resample_4h_b1'"
if [[ ! -f "$JOBS_JSON" ]]; then
    fail "jobs file missing: $JOBS_JSON"
    exit 1
fi
python3 - "$JOBS_JSON" "$JOB_ID" <<'PY'
import json, sys
from datetime import datetime, timezone
path, jid = sys.argv[1], sys.argv[2]
d = json.loads(open(path).read())
found = False
for j in d["jobs"]:
    if j["id"] == jid:
        j["enabled"] = True
        j["paused_at"] = None
        j["paused_reason"] = None
        found = True
        print(f"enabled job {jid}")
        break
if not found:
    print(f"FAIL: job {jid} not in {path}", file=sys.stderr)
    sys.exit(2)
d["updated_at"] = datetime.now(timezone.utc).isoformat()
open(path, "w").write(json.dumps(d, indent=2))
PY
ok "cron resample_4h enabled (schedule 5 */4 * * *)"

# ── Step B: bring up the container ──────────────────────────────────────────
bold "Step B: docker compose --profile nfi up -d freqtrade-nfi"
cd "$REPO_ROOT"
if ! docker compose --profile nfi up -d freqtrade-nfi; then
    fail "docker compose up returned non-zero — aborting"
    exit 1
fi

# ── Step C: wait for healthcheck ────────────────────────────────────────────
bold "Step C: waiting up to 120s for healthcheck"
deadline=$(( $(date +%s) + 120 ))
while [[ $(date +%s) -lt $deadline ]]; do
    status="$(docker ps --filter "name=^freqtrade-nfi$" --format '{{.Status}}' || true)"
    if [[ "$status" == *"healthy"* ]]; then
        ok "freqtrade-nfi is healthy ($status)"
        echo
        echo "Next: tail logs for ~5 min and verify no 'Empty candle (OHLCV) data'"
        echo "or 'KeyError(date)' lines:"
        echo "    docker logs -f freqtrade-nfi 2>&1 | grep -iE 'error|warn|trade|signal'"
        echo
        echo "Rollback (if any indicator-pass error fires):"
        echo "    bash $REPO_ROOT/scripts/install_nfi_x6.sh --uninstall"
        exit 0
    fi
    sleep 5
done
fail "freqtrade-nfi did not become healthy in 120s"
echo "Last container status: $status"
echo "Inspect:  docker logs --tail 60 freqtrade-nfi"
exit 1
