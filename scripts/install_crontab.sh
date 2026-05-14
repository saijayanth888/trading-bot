#!/usr/bin/env bash
#
# Install / refresh the trading-bot crontab.
#
# Idempotent: replaces only the lines tagged "trading-bot" — anything
# else in your crontab is preserved.
#
# Usage:
#   ./scripts/install_crontab.sh              — install
#   ./scripts/install_crontab.sh --uninstall  — remove the bot's lines
#   ./scripts/install_crontab.sh --print      — print what would be installed
#
# The cron runs use absolute paths so they don't depend on PATH or PWD.

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LOG_DIR="${ROOT_DIR}/user_data/logs"
SCRIPTS="${ROOT_DIR}/scripts"
TAG="trading-bot"

read -r -d '' BOT_LINES <<EOF || true
# === ${TAG} BEGIN === (managed by install_crontab.sh; do not edit by hand)
# Every 5 min safety net (P0-F): emergency stop on >3% daily loss, halve
# Post-2026-05-14: auto_rollback (freqtrade-coupled, deprecated) and the
# weekly DRL retrain (which exec'd into the freqtrade container that is
# now gone, and called scripts/train_drl.py which has been deleted) are
# both removed. Only the daily/weekly backups remain. Port the safety-net
# logic to quanta-core when ready; see memory `freqtrade_decommissioned`.
# Daily incremental backup at 02:00 UTC
0 2 * * * ${SCRIPTS}/backup.sh daily >> ${LOG_DIR}/backup.log 2>&1
# Weekly full backup Sunday 03:00 UTC
0 3 * * 0 ${SCRIPTS}/backup.sh weekly >> ${LOG_DIR}/backup.log 2>&1
# === ${TAG} END ===
EOF

if [[ "${1:-}" == "--print" ]]; then
    echo "$BOT_LINES"
    exit 0
fi

# Pull current crontab (no error if empty)
CURRENT="$(crontab -l 2>/dev/null || true)"

# Strip any existing trading-bot block (preserves user's other entries)
STRIPPED="$(printf '%s\n' "$CURRENT" \
    | awk -v tag="$TAG" '
        $0 ~ "=== "tag" BEGIN ===" { skip=1; next }
        $0 ~ "=== "tag" END ==="   { skip=0; next }
        !skip
      ')"

if [[ "${1:-}" == "--uninstall" ]]; then
    echo "$STRIPPED" | crontab -
    echo "[crontab] removed ${TAG} lines"
    exit 0
fi

# Re-attach our block at the bottom
{
    printf '%s' "$STRIPPED"
    [[ -n "$STRIPPED" ]] && [[ "${STRIPPED: -1}" != $'\n' ]] && echo
    printf '%s\n' "$BOT_LINES"
} | crontab -

echo "[crontab] installed/refreshed ${TAG} lines"
echo
crontab -l | grep -A99 "=== ${TAG} BEGIN ===" | grep -B99 "=== ${TAG} END ==="
