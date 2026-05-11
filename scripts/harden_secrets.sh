#!/usr/bin/env bash
#
# P0-W: tighten permissions on ./secrets and its contents.
#
# Why a script and not a one-off `chmod`: this needs to run on the host
# *after* the operator drops a real Coinbase key file into secrets/. Cron
# can re-run it idempotently to catch any new file with a too-loose mode.
#
# - secrets/ itself  → 700 (owner read+write+exec only)
# - any *.json key   → 600 (owner read+write)
# - any other file   → 600 (defensive default)
#
# Idempotent: re-running on already-tight modes is a no-op.

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SECRETS_DIR="${ROOT_DIR}/secrets"

if [[ ! -d "$SECRETS_DIR" ]]; then
    echo "[harden_secrets] $SECRETS_DIR does not exist; nothing to do"
    exit 0
fi

current_dir_mode="$(stat -c '%a' "$SECRETS_DIR")"
if [[ "$current_dir_mode" != "700" ]]; then
    echo "[harden_secrets] chmod 700 $SECRETS_DIR (was $current_dir_mode)"
    chmod 700 "$SECRETS_DIR"
else
    echo "[harden_secrets] $SECRETS_DIR already 700"
fi

# Loop files; touch only those that need tightening.
shopt -s nullglob dotglob
for f in "$SECRETS_DIR"/*; do
    [[ -f "$f" ]] || continue
    current="$(stat -c '%a' "$f")"
    if [[ "$current" != "600" ]]; then
        echo "[harden_secrets] chmod 600 $f (was $current)"
        chmod 600 "$f"
    fi
done

# Also harden .env in the repo root if present — it contains the same
# class of secrets (API keys, DB password, MCP key).
ENV_FILE="${ROOT_DIR}/.env"
if [[ -f "$ENV_FILE" ]]; then
    current="$(stat -c '%a' "$ENV_FILE")"
    if [[ "$current" != "600" ]]; then
        echo "[harden_secrets] chmod 600 $ENV_FILE (was $current)"
        chmod 600 "$ENV_FILE"
    fi
fi

echo "[harden_secrets] done"
