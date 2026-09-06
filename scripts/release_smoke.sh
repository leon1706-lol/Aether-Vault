#!/usr/bin/env bash
# ============================================================================
# Smokes a REAL published/local engine image against the SAME compose file real `pip
# install aether-vault` end users get -- never exercised by e2e-engine-smoke (builds
# straight from the Dockerfile) or ha-drill (separate topology). One script, five call
# sites: docker-edge's staging smoke, a PR preview environment, release.yml's post-push
# verification, the rollback drill, and local ad-hoc use.
#
# Usage: ./scripts/release_smoke.sh <image-ref>
#   e.g. ./scripts/release_smoke.sh ghcr.io/leon1706-lol/aether-vault-engine:edge
#        ./scripts/release_smoke.sh aether-vault-engine:local
#
# Asserts, against a freshly-booted stack:
#   1. /api/health (DB-free liveness) returns 200.
#   2. /api/ready (DB/Redis/data-dir-backed readiness) returns 200.
#   3. A real `av push` + `av pull` (separate clone) round trip lands correctly.
#   4. Protected mode: with AV_API_TOKEN set, /api/refs is 401 without a token and 200
#      with one — proves auth is actually wired in THIS compose topology, not just in
#      the dev one tests/test_server.py already exercises directly.
# Tears the stack down (`down -v`) on exit either way; dumps logs on failure.
# ============================================================================
set -euo pipefail

IMAGE_REF="${1:?Usage: release_smoke.sh <image-ref>}"

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
COMPOSE_FILE="$REPO_ROOT/python/av_cli/docker/docker-compose.release.yml"
PROJECT="av-release-smoke"
WORK="$(mktemp -d /tmp/av-release-smoke-XXXXXX)"
command -v cygpath >/dev/null 2>&1 && WORK="$(cygpath -m "$WORK")"
OVERRIDE="$WORK/override.yml"
API="http://localhost:8000"

log()  { printf '\n\033[1;36m[release-smoke]\033[0m %s\n' "$*"; }
pass() { printf '\033[1;32m[PASS]\033[0m %s\n' "$*"; }
die()  { printf '\033[1;31m[FAIL]\033[0m %s\n' "$*" >&2; exit 1; }

# Pins the compose file's image to whatever ref we're smoking without hand-editing the
# shipped file -- `docker compose -f a -f b` merges b's services over a's by key.
cat > "$OVERRIDE" <<EOF
services:
  aether-vault-engine:
    image: ${IMAGE_REF}
EOF

COMPOSE="docker compose -f $COMPOSE_FILE -f $OVERRIDE -p $PROJECT"

cleanup() {
  $COMPOSE down -v >/dev/null 2>&1 || true
  rm -rf "$WORK" 2>/dev/null || true
}
trap cleanup EXIT

PY=""
for _cand in python3 python; do
  if command -v "$_cand" >/dev/null 2>&1 && "$_cand" -c "pass" >/dev/null 2>&1; then
    PY="$(command -v "$_cand")"
    break
  fi
done
[[ -n "$PY" ]] || die "no working python/python3 on PATH"

wait_for() {
  # Same bounded-retry shape as ha_drill.sh's wait_ready(): curl timeouts plus an
  # OS-level `timeout` backstop, so a hang cancels cleanly instead of burning the job.
  local url="$1" tries=60
  while (( tries-- > 0 )); do
    timeout -k 5 12 curl -sf --connect-timeout 5 --max-time 10 -o /dev/null "$url" && return 0
    sleep 2
  done
  return 1
}

# ---------------------------------------------------------------------------
log "pulling/starting the release compose stack with image=${IMAGE_REF}"
$COMPOSE up -d

log "waiting for /api/health (liveness, DB-free)"
wait_for "$API/api/health" || die "container never became live (/api/health)"
pass "/api/health is live"

log "waiting for /api/ready (readiness, DB/Redis/data-dir-backed)"
wait_for "$API/api/ready" || die "container never became ready (/api/ready) -- DB/Redis/data-dir not usable"
pass "/api/ready is ready"

HEALTH_VERSION="$(curl -sf "$API/api/health" | "$PY" -c "import json,sys; print(json.load(sys.stdin)['version'])")"
log "image reports version: $HEALTH_VERSION"
[[ "$HEALTH_VERSION" != "0.0.0.dev0" ]] || die "/api/health reports the fallback version 0.0.0.dev0 -- AV_VERSION was not baked into this image at build time (see Dockerfile's ARG AV_VERSION / Probleme.md #69)"
pass "image reports a real, non-fallback version"

# ---------------------------------------------------------------------------
log "real av push + pull round trip (Anonymous mode)"
# `av init` has no --remote-url of its own -- a "local"-mode repo pushes to whatever
# AV_REMOTE_URL resolves to; exported explicitly so this never silently depends on the default.
export AV_REMOTE_URL="$API"
REPO_A="$WORK/repoA"
REPO_B="$WORK/repoB"
mkdir -p "$REPO_A"
( cd "$REPO_A" \
  && av init --mode local --yes --no-repl \
  && echo "release-smoke $(date -u +%s)" > f.txt \
  && av add f.txt \
  && av commit -m "release-smoke" \
  && av push ) || die "push from a fresh repo against this image failed"
pass "push landed"

PROJ="$("$PY" -c "import json; print(json.load(open('$REPO_A/.av/config'))['project_id'])")"
av clone "$PROJ" "$REPO_B" --remote-url "$API" >/dev/null || die "clone against this image failed"
[[ -f "$REPO_B/f.txt" ]] || die "cloned repo is missing the pushed file"
pass "pull/clone round trip landed the pushed commit"

# ---------------------------------------------------------------------------
log "protected mode: restarting with AV_API_TOKEN set, asserting 401 -> 200"
TOKEN="release-smoke-$(date -u +%s)"
AV_API_TOKEN="$TOKEN" $COMPOSE up -d --force-recreate aether-vault-engine
wait_for "$API/api/health" || die "container never came back up after enabling Protected mode"
wait_for "$API/api/ready" || die "container never became ready after enabling Protected mode"

UNAUTH_CODE="$(curl -s -o /dev/null -w '%{http_code}' "$API/api/refs")"
[[ "$UNAUTH_CODE" == "401" ]] || die "expected 401 from /api/refs with no token once AV_API_TOKEN is set, got $UNAUTH_CODE"
pass "unauthenticated request correctly rejected (401)"

AUTH_CODE="$(curl -s -o /dev/null -w '%{http_code}' -H "Authorization: Bearer $TOKEN" "$API/api/refs")"
[[ "$AUTH_CODE" == "200" ]] || die "expected 200 from /api/refs with the correct bearer token, got $AUTH_CODE"
pass "authenticated request correctly accepted (200)"

# ---------------------------------------------------------------------------
log "ALL RELEASE SMOKE CHECKS PASSED for ${IMAGE_REF}"
