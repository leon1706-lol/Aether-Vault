#!/bin/bash
# Runs once, automatically, via Postgres' own docker-entrypoint-initdb.d convention --
# ONLY on a genuinely fresh data volume (the official image skips this whole directory
# if $PGDATA already has data, which is exactly the idempotence this needs for repeated
# `docker compose up` cycles against the same named volume).
#
# Creates the dedicated replication role the streaming replica
# (docker/ha/postgres-replica-entrypoint.sh) connects as, and opens pg_hba.conf to
# accept replication connections from anywhere on the compose network (fine for a local
# HA drill on an isolated compose network; a real deployment would scope this to the
# replica's actual subnet).
set -euo pipefail

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" <<-EOSQL
    CREATE ROLE replicator WITH REPLICATION LOGIN PASSWORD 'replicator_password';
EOSQL

echo "host replication replicator all md5" >> "$PGDATA/pg_hba.conf"
