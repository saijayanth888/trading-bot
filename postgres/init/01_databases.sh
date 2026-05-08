#!/usr/bin/env bash
# Runs once on the postgres container's first startup. Creates the
# freqtrade DB (separate from the application's `tradebot` DB) and
# enables the TimescaleDB extension on both.

set -euo pipefail

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" <<-SQL
    CREATE EXTENSION IF NOT EXISTS timescaledb CASCADE;
SQL

# Spawn the freqtrade DB
psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "postgres" <<-SQL
    CREATE DATABASE freqtrade OWNER "$POSTGRES_USER";
SQL

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "freqtrade" <<-SQL
    CREATE EXTENSION IF NOT EXISTS timescaledb CASCADE;
SQL

echo "[init] postgres ready: tradebot + freqtrade DBs, timescaledb enabled"
