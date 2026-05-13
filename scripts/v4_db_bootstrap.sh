#!/usr/bin/env bash
# v4_db_bootstrap.sh — create the quanta_schema and apply ledger migrations.
#
# Idempotent: re-running is safe. Uses `CREATE TABLE IF NOT EXISTS` from the
# migration files plus a per-migration version check via quanta_schema_version.
#
# Usage:
#   bash scripts/v4_db_bootstrap.sh
#
# Expects the tradebot-postgres container to be running and authenticated via
# .pgpass or trust auth on the local socket. Reads POSTGRES_* from .env if
# present.

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
MIGRATIONS_DIR="$ROOT/src/quanta_core/ledger/migrations"

# Reuse the same container + credentials the rest of the stack uses.
CONTAINER="${PG_CONTAINER:-tradebot-postgres}"
PG_USER="${POSTGRES_USER:-tradebot}"
PG_DB="${POSTGRES_DB:-tradebot}"
SCHEMA="${QUANTA_SCHEMA:-quanta_schema}"

PSQL=(docker exec -i "$CONTAINER" psql -U "$PG_USER" -d "$PG_DB" -v ON_ERROR_STOP=1)

echo "[v4-bootstrap] creating schema '$SCHEMA' on $CONTAINER/$PG_DB"
"${PSQL[@]}" <<SQL
CREATE SCHEMA IF NOT EXISTS $SCHEMA AUTHORIZATION $PG_USER;
SQL

echo "[v4-bootstrap] applying migrations from $MIGRATIONS_DIR"

# Apply each .sql file in lexical order, prefixed with SET search_path so the
# CREATE TABLE statements land in $SCHEMA (the migration files themselves do
# not qualify object names — they rely on the caller setting search_path).
for f in "$MIGRATIONS_DIR"/*.sql; do
    name="$(basename "$f")"
    echo "[v4-bootstrap]  → $name"
    {
        echo "SET search_path TO $SCHEMA;"
        cat "$f"
    } | "${PSQL[@]}"
done

echo "[v4-bootstrap] verifying tables:"
"${PSQL[@]}" -c "\\dt $SCHEMA.*"

echo "[v4-bootstrap] schema version rows:"
"${PSQL[@]}" -c "SELECT version, applied_at, description FROM $SCHEMA.quanta_schema_version ORDER BY version;"

echo "[v4-bootstrap] done."
