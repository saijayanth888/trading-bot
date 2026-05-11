#!/usr/bin/env bash
# install_hermes_script.sh — copy the in-tree stocks_ml_train.sh into ~/.hermes/scripts/.
#
# Background (P0-O from REVIEW_2026-05-11.md): the Hermes cron in
# ~/.hermes/scripts/stocks_ml_train.sh on the host was the OLD blocking version
# that ran the TFT training in the foreground. Hermes' 120s cron-wrapper
# timeout killed it after ~4 of 25 epochs on 2026-05-10. The corrected
# detached/setsid version lives in this repo at
# .hermes/scripts/stocks_ml_train.sh and survives the wrapper's group-kill.
#
# This installer copies the correct script onto the host, preserving the
# executable bit. Idempotent — overwrites any existing copy.
#
# Run on the main tree (NOT inside a worktree) so $REPO points at the
# canonical checkout, e.g.:
#
#     bash /home/saijayanthai/Documents/trading-bot/scripts/install_hermes_script.sh
#
# Or, after the agent's worktree is merged to main, the coordinator runs it
# from the merged checkout.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SRC="$REPO_ROOT/.hermes/scripts/stocks_ml_train.sh"
DST_DIR="$HOME/.hermes/scripts"
DST="$DST_DIR/stocks_ml_train.sh"

if [[ ! -f "$SRC" ]]; then
    echo "FATAL: source not found at $SRC" >&2
    exit 1
fi

mkdir -p "$DST_DIR"

# Back up any existing copy so we can compare / roll back.
if [[ -f "$DST" ]] && ! cmp -s "$SRC" "$DST"; then
    backup="$DST.bak.$(date -u +%Y%m%dT%H%M%SZ)"
    cp -p "$DST" "$backup"
    echo "Backed up existing $DST → $backup"
fi

install -m 0755 "$SRC" "$DST"
echo "Installed $SRC → $DST"
echo "Verifying executable bit:"
ls -la "$DST"
