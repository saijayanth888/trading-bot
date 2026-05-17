#!/usr/bin/env bash
# Runs once on the postgres container's first startup. Enables the
# TimescaleDB extension on the application's `tradebot` DB.
#
# Freqtrade was decommissioned 2026-05-14 (memory `freqtrade_decommissioned`).
# The freqtrade database creation step was removed on 2026-05-16 after the
# DB audit confirmed no live code reads from it. Existing freqtrade DBs are
# left in place on already-initialised volumes; this script only governs
# fresh-volume bootstrap.

set -euo pipefail

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" <<-SQL
    CREATE EXTENSION IF NOT EXISTS timescaledb CASCADE;
SQL

echo "[init] postgres ready: tradebot DB, timescaledb enabled"
