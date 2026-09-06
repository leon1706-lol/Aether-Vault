#!/bin/bash
# ============================================================================
# Aether-Vault Engine entrypoint — one container, all subservices.
#
# AV_ENGINE_ROLE dispatch:
#   all    (default) uvicorn registry (:8000) AND Next.js webui (:3000) in this
#          container. A dying subservice no longer always takes the whole
#          container down — see "Supervision" below.
#   server uvicorn only (legacy alias containers from pre-1.2.2 compose files).
#   webui  node standalone server only (same legacy path).
#
# Legacy auto-detect: old two-service compose files never set AV_ENGINE_ROLE but shape
# each container's environment differently (DATABASE_URL only in the server container,
# NEXT_PUBLIC_API_URL only in the webui one) — detecting on those keeps aliased legacy
# images behaving like the split images they replace, with a deprecation warning.
#
# Supervision: shutdown (TERM/INT) forwards the signal to both children and waits up to
# AV_ENGINE_STOP_GRACE_SECS (default 25s) before SIGKILL (both compose files set
# `stop_grace_period: 30s` so this window is actually honored). In role=all, one
# subservice dying restarts just that subservice, bounded by a restart budget
# (AV_ENGINE_MAX_RESTARTS within AV_ENGINE_RESTART_WINDOW_SECS) so a genuinely broken
# build still shuts the engine down instead of crash-looping forever.
# ============================================================================
set -u

ROLE="${AV_ENGINE_ROLE:-}"
if [ -z "$ROLE" ]; then
  if [ -n "${DATABASE_URL:-}" ]; then
    ROLE="server"
    echo "[engine] DEPRECATED: AV_ENGINE_ROLE not set, inferred 'server' from DATABASE_URL" \
         "(legacy aether-vault-server alias). Set AV_ENGINE_ROLE=all and point at the" \
         "consolidated aether-vault-engine image — see VERSIONING.md for the removal timeline." >&2
  elif [ -n "${NEXT_PUBLIC_API_URL:-}" ]; then
    ROLE="webui"
    echo "[engine] DEPRECATED: AV_ENGINE_ROLE not set, inferred 'webui' from NEXT_PUBLIC_API_URL" \
         "(legacy aether-vault-webui alias). Set AV_ENGINE_ROLE=all and point at the" \
         "consolidated aether-vault-engine image — see VERSIONING.md for the removal timeline." >&2
  else
    ROLE="all"
  fi
fi

STOP_GRACE_SECS="${AV_ENGINE_STOP_GRACE_SECS:-25}"
RESTART_SUBSERVICE="${AV_ENGINE_RESTART_SUBSERVICE:-1}"
MAX_RESTARTS="${AV_ENGINE_MAX_RESTARTS:-5}"
RESTART_WINDOW_SECS="${AV_ENGINE_RESTART_WINDOW_SECS:-300}"

WEBUI_PID=""
SERVER_PID=""

shutdown() {
  trap - TERM INT
  echo "[engine] shutting down (grace ${STOP_GRACE_SECS}s)..." >&2
  [ -n "$WEBUI_PID" ] && kill -TERM "$WEBUI_PID" 2>/dev/null
  [ -n "$SERVER_PID" ] && kill -TERM "$SERVER_PID" 2>/dev/null

  local waited=0
  while [ "$waited" -lt "$STOP_GRACE_SECS" ]; do
    local still_alive=0
    [ -n "$WEBUI_PID" ] && kill -0 "$WEBUI_PID" 2>/dev/null && still_alive=1
    [ -n "$SERVER_PID" ] && kill -0 "$SERVER_PID" 2>/dev/null && still_alive=1
    [ "$still_alive" -eq 0 ] && break
    sleep 1
    waited=$((waited + 1))
  done

  if [ -n "$WEBUI_PID" ] && kill -0 "$WEBUI_PID" 2>/dev/null; then
    echo "[engine] webui did not exit within ${STOP_GRACE_SECS}s — SIGKILL" >&2
    kill -KILL "$WEBUI_PID" 2>/dev/null
  fi
  if [ -n "$SERVER_PID" ] && kill -0 "$SERVER_PID" 2>/dev/null; then
    echo "[engine] server did not exit within ${STOP_GRACE_SECS}s — SIGKILL" >&2
    kill -KILL "$SERVER_PID" 2>/dev/null
  fi
  wait 2>/dev/null
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

# Sliding-window restart budget shared by both subservices. Sets a global instead of
# `echo`ing a return value: `count=$(record_restart)` would fork a subshell, so the
# RESTART_TIMES mutation would vanish the instant it exited and the budget would never
# accumulate across restarts.
RESTART_TIMES=()
RESTART_COUNT=0
record_restart() {
  local now pruned=() t
  now=$(date +%s)
  for t in "${RESTART_TIMES[@]:-}"; do
    [ -z "$t" ] && continue
    if [ $((now - t)) -lt "$RESTART_WINDOW_SECS" ]; then
      pruned+=("$t")
    fi
  done
  pruned+=("$now")
  RESTART_TIMES=("${pruned[@]}")
  RESTART_COUNT=${#RESTART_TIMES[@]}
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
    while true; do
      wait -n "$WEBUI_PID" "$SERVER_PID"
      exit_code=$?

      dead=""
      if ! kill -0 "$WEBUI_PID" 2>/dev/null; then
        dead="webui"
      elif ! kill -0 "$SERVER_PID" 2>/dev/null; then
        dead="server"
      fi
      if [ -z "$dead" ]; then
        # Spurious wake (e.g. a signal delivered to the shell itself) — the trap handles
        # real termination; both children are still alive, so just keep waiting.
        continue
      fi
      echo "[engine] subservice '$dead' exited (status $exit_code)" >&2

      if [ "$RESTART_SUBSERVICE" != "1" ]; then
        echo "[engine] AV_ENGINE_RESTART_SUBSERVICE is off — shutting down the engine" >&2
        shutdown
      fi

      record_restart
      if [ "$RESTART_COUNT" -gt "$MAX_RESTARTS" ]; then
        echo "[engine] '$dead' exceeded $MAX_RESTARTS restarts within ${RESTART_WINDOW_SECS}s" \
             "— giving up, shutting down the engine" >&2
        shutdown
      fi
      echo "[engine] restarting '$dead' ($RESTART_COUNT/$MAX_RESTARTS restarts within ${RESTART_WINDOW_SECS}s)..." >&2
      if [ "$dead" = "webui" ]; then
        start_webui
      else
        start_server
      fi
    done
    ;;
  *)
    echo "[engine] unknown AV_ENGINE_ROLE '$ROLE' (expected all|server|webui)" >&2
    exit 1
    ;;
esac
