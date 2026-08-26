#!/bin/bash
# ============================================================================
# Aether-Vault Engine entrypoint — one container, all subservices.
#
# AV_ENGINE_ROLE dispatch:
#   all    (default) uvicorn registry (:8000) AND Next.js webui (:3000) in this
#          container; either child dying takes the container down so
#          `restart: unless-stopped` brings the whole engine back.
#   server uvicorn only (legacy alias containers from pre-1.2.2 compose files).
#   webui  node standalone server only (same legacy path).
#
# Legacy auto-detect: old two-service compose files never set AV_ENGINE_ROLE but
# shape each container's environment differently — DATABASE_URL only exists in
# the server container, NEXT_PUBLIC_API_URL only in the webui one. Detecting on
# those keeps the aliased legacy images behaving exactly like the split images
# they replace, with zero changes to pinned installs.
# ============================================================================
set -u

ROLE="${AV_ENGINE_ROLE:-}"
if [ -z "$ROLE" ]; then
  if [ -n "${DATABASE_URL:-}" ]; then
    ROLE="server"
  elif [ -n "${NEXT_PUBLIC_API_URL:-}" ]; then
    ROLE="webui"
  else
    ROLE="all"
  fi
fi

WEBUI_PID=""
SERVER_PID=""

shutdown() {
  trap - TERM INT
  [ -n "$WEBUI_PID" ] && kill "$WEBUI_PID" 2>/dev/null
  [ -n "$SERVER_PID" ] && kill "$SERVER_PID" 2>/dev/null
  wait
  exit 0
}
trap shutdown TERM INT

start_webui() {
  echo "[engine] starting webui (node /webui/server.js) on :${WEBUI_PORT:-3000}"
  (
    cd /webui || exit 1
    exec node server.js
  ) &
  WEBUI_PID=$!
}

start_server() {
  echo "[engine] starting server (uvicorn av_server.server:app) on :8000"
  python -m uvicorn av_server.server:app --host 0.0.0.0 --port 8000 &
  SERVER_PID=$!
}

case "$ROLE" in
  webui)
    start_webui
    wait "$WEBUI_PID"
    ;;
  server)
    start_server
    wait "$SERVER_PID"
    ;;
  all)
    start_webui
    start_server
    # Either subservice dying is fatal for the container: half an engine is
    # worse than a restart loop that brings both back under supervision.
    wait -n "$WEBUI_PID" "$SERVER_PID"
    code=$?
    echo "[engine] subservice exited (status $code) — shutting down the engine" >&2
    shutdown
    ;;
  *)
    echo "[engine] unknown AV_ENGINE_ROLE '$ROLE' (expected all|server|webui)" >&2
    exit 1
    ;;
esac
