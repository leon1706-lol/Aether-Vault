#!/bin/bash
# Role-aware container healthcheck for the engine image (V1.6.3).
#
# Replaces the previous CMD-SHELL probe, which forked a full Python interpreter AND a
# Node process every 10 s just to issue two GETs -- a permanent CPU/RSS blip on a box
# where the whole point of the container is to sit quietly. This does the same two
# checks with bash's /dev/tcp and no child processes at all:
#   all    -> :8000 /api/ready must be 200 AND :3000 / must be 200
#   server -> :8000 /api/ready only
#   webui  -> :3000 / only
#
# /api/ready (not /api/health) on purpose: ready also checks DB/Redis/data-dir
# writability, which is what "this container is actually usable" should mean.
#
# The role is read from /run/av-engine-role, written by engine-entrypoint.sh AFTER its
# legacy auto-detection, so a container that inferred "server" from DATABASE_URL is
# probed as "server" here too. AV_HC_API_PORT / AV_HC_WEBUI_PORT exist for the unit
# test (tests/test_engine_healthcheck.py) that drives this script against a stub.
set -u

role="$(cat /run/av-engine-role 2>/dev/null || true)"
role="${role:-${AV_ENGINE_ROLE:-all}}"
api_port="${AV_HC_API_PORT:-8000}"
webui_port="${AV_HC_WEBUI_PORT:-${WEBUI_PORT:-3000}}"

probe() {
  # $1 = port, $2 = path. HTTP/1.0 + Connection: close so the server ends the stream
  # itself; -t 5 keeps a wedged process from hanging the probe past the docker timeout.
  local port="$1" path="$2" status
  exec 3<>"/dev/tcp/127.0.0.1/${port}" 2>/dev/null || return 1
  printf 'GET %s HTTP/1.0\r\nHost: localhost\r\nConnection: close\r\n\r\n' "$path" >&3 || { exec 3<&-; return 1; }
  IFS= read -r -t 5 status <&3 || status=""
  exec 3<&-
  [[ "$status" == *" 200 "* ]]
}

case "$role" in
  server) probe "$api_port" /api/ready ;;
  webui)  probe "$webui_port" / ;;
  *)      probe "$api_port" /api/ready && probe "$webui_port" / ;;
esac
