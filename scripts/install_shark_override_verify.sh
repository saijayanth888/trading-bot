#!/usr/bin/env bash
# install_shark_override_verify.sh — copies the in-tree verifier script
# and registers the Hermes cron entry on the host.
#
# Idempotent. Safe to re-run after pulling new code.
#
#   bash scripts/install_shark_override_verify.sh
#
# Steps:
#   1. Copy .hermes/scripts/shark_override_verify.sh → ~/.hermes/scripts/
#   2. Append shark_override_verify cron to ~/.hermes/cron/jobs.json
#      if not already registered (creates a timestamped backup first).
#   3. Print verification of both.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SRC="$REPO_ROOT/.hermes/scripts/shark_override_verify.sh"
DST_DIR="$HOME/.hermes/scripts"
DST="$DST_DIR/shark_override_verify.sh"
JOBS="$HOME/.hermes/cron/jobs.json"

if [[ ! -f "$SRC" ]]; then
    echo "FATAL: source not found at $SRC" >&2
    exit 1
fi

# 1. Install the script
mkdir -p "$DST_DIR"
if [[ -f "$DST" ]] && ! cmp -s "$SRC" "$DST"; then
    backup="$DST.bak.$(date -u +%Y%m%dT%H%M%SZ)"
    cp -p "$DST" "$backup"
    echo "Backed up existing $DST → $backup"
fi
install -m 0755 "$SRC" "$DST"
echo "Installed $SRC → $DST"

# 2. Register the cron job
if [[ ! -f "$JOBS" ]]; then
    echo "WARN: $JOBS does not exist — skipping cron registration" >&2
    exit 0
fi

python3 - <<PY
import json, shutil, sys
from datetime import datetime, timezone
from pathlib import Path

JOBS = Path("$JOBS")
data = json.loads(JOBS.read_text())
existing = [j for j in data["jobs"] if j.get("name") == "shark_override_verify"]
if existing:
    print(f"cron already registered: id={existing[0].get('id')} (no changes)")
    sys.exit(0)

backup = JOBS.with_suffix(
    f".json.backup-pre-shark-override-verifier-"
    f"{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"
)
shutil.copy2(JOBS, backup)
print(f"backup: {backup}")

new_job = {
    "id": "shark_override_verify_b1",
    "name": "shark_override_verify",
    "prompt": "",
    "skills": [],
    "skill": None,
    "model": None,
    "provider": None,
    "base_url": None,
    "script": "shark_override_verify.sh",
    "no_agent": True,
    "context_from": None,
    "schedule": {"kind": "cron", "expr": "45 9 * * 1-5",
                 "display": "45 9 * * 1-5"},
    "schedule_display": "45 9 * * 1-5",
    "repeat": {"times": None, "completed": 0},
    "enabled": True,
    "state": "scheduled",
    "paused_at": None,
    "paused_reason": None,
    "created_at": datetime.now(timezone.utc).astimezone().isoformat(),
    "next_run_at": None,
    "last_run_at": None,
    "last_status": None,
    "last_error": None,
    "last_delivery_error": None,
    "deliver": "local",
    "origin": None,
    "enabled_toolsets": None,
    "workdir": "/home/saijayanthai/Documents/trading-bot",
}
data["jobs"].append(new_job)
data["updated_at"] = datetime.now(timezone.utc).astimezone().isoformat()

tmp = JOBS.with_suffix(".json.tmp")
tmp.write_text(json.dumps(data, indent=2))
tmp.replace(JOBS)
print(f"appended cron: id=shark_override_verify_b1 schedule='45 9 * * 1-5'")
PY

# 3. Verification
echo "── verification ──"
ls -la "$DST"
python3 -c "
import json
d = json.load(open('$JOBS'))
j = [x for x in d['jobs'] if x['name'] == 'shark_override_verify']
print(f'  registered: {len(j)} job(s) named shark_override_verify')
for job in j:
    print(f'    id={job[\"id\"]} schedule={job[\"schedule_display\"]} no_agent={job[\"no_agent\"]} script={job[\"script\"]}')
"
echo "── done ──"
