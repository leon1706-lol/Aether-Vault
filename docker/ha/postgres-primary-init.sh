#!/bin/bash
# Runs once, via Postgres' docker-entrypoint-initdb.d convention, only on a genuinely
# fresh data volume. Creates the dedicated replication role the streaming replica
# (postgres-replica-entrypoint.sh) connects as, and opens pg_hba.conf to accept
# replication connections from anywhere on the compose network (fine for a local drill;
# a real deployment would scope this to the replica's actual subnet).
set -euo pipefail

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" <<-EOSQL
    CREATE ROLE replicator WITH REPLICATION LOGIN PASSWORD 'replicator_password';
EOSQL

echo "host replication replicator all md5" >> "$PGDATA/pg_hba.conf"
