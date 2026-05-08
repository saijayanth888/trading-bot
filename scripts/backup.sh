#!/usr/bin/env bash
#
# Backups for the trading bot.
#
# Modes:
#   ./scripts/backup.sh daily     # checkpoint models + config + journal (small, fast)
#   ./scripts/backup.sh weekly    # full user_data archive (larger, deeper retention)
#
# Retention:
#   30 daily  + 12 weekly archives, oldest first removed.
#
# Destination:
#   ~/backups/trading-bot/{daily,weekly}/<UTC-stamp>.tar.gz
#
# Cron:
#   0 2 * * *   /path/to/scripts/backup.sh daily  >> .../backup.log 2>&1
#   0 3 * * 0   /path/to/scripts/backup.sh weekly >> .../backup.log 2>&1

set -uo pipefail

MODE="${1:-daily}"
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DEST_ROOT="${BACKUP_DIR:-${HOME}/backups/trading-bot}"
LOG="${ROOT_DIR}/user_data/logs/backup.log"
STAMP="$(date -u '+%Y-%m-%dT%H-%M-%SZ')"

mkdir -p "$(dirname "$LOG")"

log() {
    echo "$(date -u '+%Y-%m-%dT%H:%M:%SZ') [backup:${MODE}] $*" | tee -a "$LOG"
}

case "$MODE" in
    daily)
        DEST_DIR="${DEST_ROOT}/daily"
        KEEP=30
        # Small, focused snapshot — model checkpoints, config, journal DB,
        # evolution log, scheduler state. Skip caches and large parquet
        # data so the daily turnaround is fast.
        SOURCES=(
            "user_data/config.json"
            "user_data/data/onchain.db"
            "user_data/data/regime_hmm.json"
            "user_data/freqaimodels"
            "user_data/models"
            "user_data/logs/evolution.json"
        )
        ;;
    weekly)
        DEST_DIR="${DEST_ROOT}/weekly"
        KEEP=12
        # Everything under user_data — minus pycache and freqai cache so
        # the archive doesn't balloon with reproducible-from-source content.
        SOURCES=("user_data")
        ;;
    *)
        echo "usage: $0 {daily|weekly}" >&2
        exit 2
        ;;
esac

mkdir -p "$DEST_DIR"
ARCHIVE="${DEST_DIR}/${MODE}-${STAMP}.tar.gz"

log "creating archive ${ARCHIVE}"
EXCLUDES=(
    "--exclude=__pycache__"
    "--exclude=*.pyc"
    "--exclude=.DS_Store"
    "--exclude=user_data/logs/*.log"
    "--exclude=user_data/data/cache"
    "--exclude=user_data/freqai"          # freqai's working/cache dir, regen-able
)

# Build a list of sources that actually exist, so a missing optional path
# (e.g. evolution.json before the first generation) doesn't fail the run.
EXISTING=()
for s in "${SOURCES[@]}"; do
    if [[ -e "${ROOT_DIR}/${s}" ]]; then
        EXISTING+=("$s")
    else
        log "skipping missing source: $s"
    fi
done

if [[ ${#EXISTING[@]} -eq 0 ]]; then
    log "no sources exist — nothing to back up"
    exit 0
fi

# tar from the project root so paths in the archive are stable.
( cd "$ROOT_DIR" && tar -czf "$ARCHIVE" "${EXCLUDES[@]}" "${EXISTING[@]}" ) \
    || { log "tar failed (rc=$?)"; rm -f "$ARCHIVE"; exit 1; }

SIZE="$(du -h "$ARCHIVE" 2>/dev/null | cut -f1 || echo "?")"
log "wrote ${ARCHIVE} (${SIZE})"

# ---- Retention ------------------------------------------------------------
# Keep the newest $KEEP files; nuke the rest.
log "trimming to last ${KEEP} archives in ${DEST_DIR}"
mapfile -t OLD < <(
    ls -1t "${DEST_DIR}"/${MODE}-*.tar.gz 2>/dev/null | tail -n +$((KEEP + 1))
)
for f in "${OLD[@]:-}"; do
    [[ -n "$f" ]] || continue
    rm -f -- "$f" && log "removed old: $(basename "$f")"
done

# ---- Verify ---------------------------------------------------------------
if tar -tzf "$ARCHIVE" >/dev/null 2>&1; then
    log "verify OK"
else
    log "VERIFY FAILED — archive may be corrupt; keeping for inspection"
    exit 1
fi
