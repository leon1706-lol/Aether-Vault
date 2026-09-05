#!/bin/bash
# Real streaming-replication bootstrap for the HA topology's Postgres standby
# (docker-compose.ha.yml's db-replica service) -- NOT a second independent database.
# The official postgres image has no built-in "run as a replica of X" mode the way
# Redis' own image supports `replicaof`, so this replaces its entrypoint entirely with
# the standard pg_basebackup-based bootstrap:
#
#   1. First boot only (empty $PGDATA -- the idempotence guard below): wait for the
#      primary to accept connections, then `pg_basebackup -R`, which both COPIES the
#      primary's current data directory AND writes standby.signal + primary_conninfo
#      into postgresql.auto.conf for us (the -R flag, PG12+) -- no manual
#      recovery.conf hand-authoring needed.
#   2. Every boot (fresh or already-replicated): fix ownership (pg_basebackup here
#      runs as root, matching how the official entrypoint itself starts as root before
#      dropping privileges) and exec postgres as the `postgres` user via `gosu`, the
#      exact mechanism the official entrypoint uses -- this container ships it already.
#
# This is a genuine hot standby: `pg_isready`/SQL reads against db-replica succeed once
# it catches up, and promoting it (`pg_ctl promote`) is what scripts/ha_drill.sh's
# primary-failure step actually exercises.
set -euo pipefail

PGDATA="${PGDATA:-/var/lib/postgresql/data}"

if [ -z "$(ls -A "$PGDATA" 2>/dev/null)" ]; then
    echo "[replica-entrypoint] empty PGDATA -- waiting for primary then pg_basebackup"
    until PGPASSWORD="replicator_password" pg_isready -h db-primary -U replicator -d aether_vault; do
        sleep 1
    done
    PGPASSWORD="replicator_password" pg_basebackup \
        -h db-primary -U replicator -D "$PGDATA" -Fp -Xs -P -R -W
    echo "[replica-entrypoint] base backup complete, standby.signal written"
fi

chown -R postgres:postgres "$PGDATA"
chmod 700 "$PGDATA"

exec gosu postgres postgres
