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
        # Small, focused snapshot. Trade journal + on-chain + sentiment
        # + regime tables now live in Postgres → use pg_dump.
        SOURCES=(
            "user_data/config.json"
            "user_data/data/regime_hmm.json"
            "user_data/freqaimodels"
            "user_data/models"
            "user_data/logs/evolution.json"
        )
        # Honor a caller-supplied DUMP_PG override so test runners (which
        # have no docker container) can disable the pg_dump step that would
        # otherwise hang. Defaults to 1 in cron.
        DUMP_PG="${DUMP_PG:-1}"
        ;;
    weekly)
        DEST_DIR="${DEST_ROOT}/weekly"
        KEEP=12
        # Everything under user_data — minus pycache and freqai cache so
        # the archive doesn't balloon with reproducible-from-source content.
        SOURCES=("user_data")
        DUMP_PG="${DUMP_PG:-1}"
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

if [[ ${#EXISTING[@]} -eq 0 ]] && [[ "${DUMP_PG:-0}" -eq 0 ]]; then
    log "no sources exist — nothing to back up"
    exit 0
fi

# Snapshot Hermes Agent state (config + secrets + chat sessions + cron jobs +
# kanban + skills) into user_data/data/hermes-state.tar.gz so it ends up in
# the same archive as the file-based artefacts. Excludes hermes-agent/ (1.8 GB
# of source code, reinstallable from upstream) and the bin/ + logs/ dirs.
HERMES_HOME="${HERMES_HOME:-${HOME}/.hermes}"
HERMES_SNAPSHOT="${ROOT_DIR}/user_data/data/hermes-state.tar.gz"
if [[ -d "$HERMES_HOME" ]]; then
    log "snapshotting hermes state from ${HERMES_HOME} → ${HERMES_SNAPSHOT}"
    HERMES_INCLUDES=(
        "config.yaml"
        ".env"
        "state.db" "state.db-wal" "state.db-shm"
        "sessions"
        "kanban.db"
        "state-snapshots"
        "skills"
        "cron/jobs.json"
        "webhook_subscriptions.json"
    )
    HERMES_EXISTING=()
    for h in "${HERMES_INCLUDES[@]}"; do
        [[ -e "${HERMES_HOME}/${h}" ]] && HERMES_EXISTING+=("$h")
    done
    if [[ ${#HERMES_EXISTING[@]} -gt 0 ]]; then
        ( cd "$HERMES_HOME" && tar -czf "$HERMES_SNAPSHOT" "${HERMES_EXISTING[@]}" ) \
            && log "hermes snapshot ok ($(du -h "$HERMES_SNAPSHOT" | cut -f1))" \
            && EXISTING+=("user_data/data/hermes-state.tar.gz") \
            || { log "hermes snapshot failed (rc=$?)"; rm -f "$HERMES_SNAPSHOT"; }
    else
        log "no hermes state files found — skipping hermes snapshot"
    fi
else
    log "no \$HERMES_HOME at ${HERMES_HOME} — skipping hermes snapshot"
fi

# Dump the Postgres tradebot database into user_data/data/pg_tradebot.dump
# so it ends up in the same archive as the file-based artefacts.
if [[ "${DUMP_PG:-0}" -eq 1 ]]; then
    PG_DUMP_PATH="${ROOT_DIR}/user_data/data/pg_tradebot.dump"
    log "dumping postgres tradebot db → ${PG_DUMP_PATH}"
    if docker compose -f "${ROOT_DIR}/docker-compose.yml" ps -q postgres 2>/dev/null | grep -q .; then
        docker compose -f "${ROOT_DIR}/docker-compose.yml" exec -T postgres \
            pg_dump -U "${POSTGRES_USER:-tradebot}" -d "${POSTGRES_DB:-tradebot}" -Fc \
            > "$PG_DUMP_PATH" 2>>"$LOG"
        rc=$?
        if [[ $rc -eq 0 ]] && [[ -s "$PG_DUMP_PATH" ]]; then
            log "pg_dump ok ($(du -h "$PG_DUMP_PATH" | cut -f1))"
            EXISTING+=("user_data/data/pg_tradebot.dump")
        else
            log "pg_dump failed (rc=$rc) — continuing with file sources only"
            rm -f "$PG_DUMP_PATH"
        fi
    else
        log "postgres container not running — skipping pg_dump"
    fi
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
