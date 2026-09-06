#!/usr/bin/env bash
# ============================================================================
# v1.3.4 (todo.md item 28, W5c): rollback drill — deploy the PREVIOUS release image,
# assert healthy → deploy the NEW one (this release), assert healthy → roll BACK to the
# previous image, assert healthy AND that data written under the new image is still
# readable. Proves the operational mechanics of a rollback (same compose project, same
# named volumes, swapping only the image reference) actually work, not just that each
# image independently boots — `scripts/release_smoke.sh` already proves the latter, and
# is reused here for the per-step health/push/pull checks; this script's OWN job is
# keeping the SAME stack (same volumes, never torn down between swaps) across all three
# deploys, which release_smoke.sh's own `down -v` cleanup deliberately does not allow.
#
# Usage: ./scripts/rollback_drill.sh <new-image-ref> <previous-image-ref>
#   e.g. ./scripts/rollback_drill.sh \
#          ghcr.io/leon1706-lol/aether-vault-engine:v1.3.4 \
#          ghcr.io/leon1706-lol/aether-vault-engine:v1.3.3
#
# See docs/runbooks/upgrade-rollback.md for the operator-facing procedure this drill
# proves (rather than restates in a workflow comment — the workflow this runs from
# links there in prose, exactly as it must, not as a literal `git push` command).
# ============================================================================
set -euo pipefail

NEW_IMAGE="${1:?Usage: rollback_drill.sh <new-image-ref> <previous-image-ref>}"
PREV_IMAGE="${2:?Usage: rollback_drill.sh <new-image-ref> <previous-image-ref>}"

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
COMPOSE_FILE="$REPO_ROOT/python/av_cli/docker/docker-compose.release.yml"
PROJECT="av-rollback-drill"
WORK="$(mktemp -d /tmp/av-rollback-drill-XXXXXX)"
command -v cygpath >/dev/null 2>&1 && WORK="$(cygpath -m "$WORK")"
OVERRIDE="$WORK/override.yml"
API="http://localhost:8000"

log()  { printf '\n\033[1;36m[rollback-drill]\033[0m %s\n' "$*"; }
pass() { printf '\033[1;32m[PASS]\033[0m %s\n' "$*"; }
die()  { printf '\033[1;31m[FAIL]\033[0m %s\n' "$*" >&2; exit 1; }

PY=""
for _cand in python3 python; do
  if command -v "$_cand" >/dev/null 2>&1 && "$_cand" -c "pass" >/dev/null 2>&1; then
    PY="$(command -v "$_cand")"
    break
  fi
done
[[ -n "$PY" ]] || die "no working python/python3 on PATH"

_compose_with_image() {
  cat > "$OVERRIDE" <<EOF
services:
  aether-vault-engine:
    image: $1
EOF
  echo "docker compose -f $COMPOSE_FILE -f $OVERRIDE -p $PROJECT"
}

cleanup() {
  $(_compose_with_image "$PREV_IMAGE") down -v >/dev/null 2>&1 || true
  rm -rf "$WORK" 2>/dev/null || true
}
trap cleanup EXIT

wait_for() {
  local url="$1" tries=60
  while (( tries-- > 0 )); do
    timeout -k 5 12 curl -sf --connect-timeout 5 --max-time 10 -o /dev/null "$url" && return 0
    sleep 2
  done
  return 1
}

deploy() {
  local image="$1" label="$2"
  log "deploying $label ($image)"
  $(_compose_with_image "$image") up -d
  wait_for "$API/api/health" || die "$label never became live (/api/health)"
  wait_for "$API/api/ready" || die "$label never became ready (/api/ready)"
  pass "$label is up and ready"
}

# ---------------------------------------------------------------------------
deploy "$PREV_IMAGE" "step 1/3: previous image"

log "writing a marker commit under the PREVIOUS image, before any upgrade"
REPO_DIR="$WORK/repo"
mkdir -p "$REPO_DIR"
AV_REMOTE_URL="$API" bash -c "
  cd '$REPO_DIR' &&
  av init --mode local --yes --no-repl &&
  echo 'rollback-drill marker' > marker.txt &&
  av add marker.txt &&
  av commit -m rollback-drill-marker &&
  av push
" >/dev/null || die "could not write the marker commit under the previous image"
pass "marker commit landed under the previous image"

# ---------------------------------------------------------------------------
deploy "$NEW_IMAGE" "step 2/3: new image (this release)"

log "confirming the marker commit is still visible after upgrading to the new image"
MARKER_OUT="$(AV_REMOTE_URL="$API" bash -c "cd '$REPO_DIR' && av log" 2>&1)"
grep -q "rollback-drill-marker" <<<"$MARKER_OUT" || die "marker commit written under the previous image is missing after upgrading to the new image"
pass "marker commit survived the upgrade"

log "writing a SECOND marker commit under the NEW image"
AV_REMOTE_URL="$API" bash -c "
  cd '$REPO_DIR' &&
  echo 'rollback-drill new-image marker' > marker-new.txt &&
  av add marker-new.txt &&
  av commit -m rollback-drill-new-image-marker &&
  av push
" >/dev/null || die "could not write the second marker commit under the new image"
pass "second marker commit landed under the new image"

# ---------------------------------------------------------------------------
deploy "$PREV_IMAGE" "step 3/3: rolled back to the previous image"

log "confirming BOTH marker commits are still visible after rolling back"
ROLLED_BACK_OUT="$(AV_REMOTE_URL="$API" bash -c "cd '$REPO_DIR' && av log" 2>&1)"
grep -q "rollback-drill-marker" <<<"$ROLLED_BACK_OUT" || die "first marker commit is missing after rolling back — rollback lost data"
grep -q "rollback-drill-new-image-marker" <<<"$ROLLED_BACK_OUT" || die "second marker commit (written under the NEW image) is missing after rolling back to the PREVIOUS image — rollback lost data written by a newer version, exactly the case this drill exists to catch"
pass "both marker commits survived the full deploy -> upgrade -> rollback cycle — no data loss"

# ---------------------------------------------------------------------------
log "ALL ROLLBACK DRILL CHECKS PASSED ($PREV_IMAGE -> $NEW_IMAGE -> $PREV_IMAGE)"
