#!/bin/bash
# Real streaming-replication bootstrap for the HA topology's Postgres standby -- the
# official postgres image has no built-in "run as a replica of X" mode, so this replaces
# its entrypoint entirely with the standard pg_basebackup-based bootstrap: on first boot
# (empty $PGDATA), wait for the primary then `pg_basebackup -R` (copies the data
# directory and writes standby.signal + primary_conninfo for us); every boot, fix
# ownership and exec postgres via `gosu`, same as the official entrypoint.
#
# A genuine hot standby: reads against db-replica succeed once it catches up, and
# promoting it (`pg_ctl promote`) is what scripts/ha_drill.sh's primary-failure step exercises.
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
