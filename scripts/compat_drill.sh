#!/usr/bin/env bash
# ============================================================================
# Migration compatibility drill — an OLDER server binary vs a database a NEWER binary
# has already migrated to head. Proves an older replica's next restart during a rolling
# upgrade comes up healthy rather than crash-looping once a newer replica has advanced
# the schema past what the older binary's alembic script directory recognizes.
#
# Steps: checkout a real previous release tag into a worktree, migrate a fresh Postgres
# to HEAD with current code, then boot the OLD code against that same database and
# assert it comes up healthy.
#
# Usage: DATABASE_URL=postgresql+asyncpg://... [OLD_TAG=v1.2.4] bash scripts/compat_drill.sh
# Needs a real, disposable Postgres and a git checkout with tag history.
#
# HONEST CURRENT LIMITATION: the only tag that exists right now, v1.2.4, predates the fix
# this drill proves, so running it against v1.2.4 reproduces the original bug rather than
# proving the fix -- expected, not a regression. Not yet wired into CI for that reason;
# wire it in once a tag past the fix exists.
# ============================================================================
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

DATABASE_URL="${DATABASE_URL:?DATABASE_URL must point at a real, disposable Postgres}"
API="http://localhost:8000"
WORK="$(mktemp -d /tmp/av-compat-drill-XXXXXX)"
command -v cygpath >/dev/null 2>&1 && WORK="$(cygpath -m "$WORK")"
WORKTREE="$WORK/old-code"
OLD_VENV="$WORK/old-venv"
SERVER_PID=""
SERVER_LOG="$WORK/server.log"

log()  { printf '\n\033[1;36m[compat-drill]\033[0m %s\n' "$*"; }
pass() { printf '\033[1;32m[PASS]\033[0m %s\n' "$*"; }
die()  { printf '\033[1;31m[FAIL]\033[0m %s\n' "$*" >&2; exit 1; }

stop_server() {
  if [[ -n "$SERVER_PID" ]] && kill -0 "$SERVER_PID" 2>/dev/null; then
    kill "$SERVER_PID" 2>/dev/null || true
    for _ in $(seq 1 20); do kill -0 "$SERVER_PID" 2>/dev/null || break; sleep 0.5; done
    kill -9 "$SERVER_PID" 2>/dev/null || true
  fi
  SERVER_PID=""
}

cleanup() {
  stop_server
  git worktree remove --force "$WORKTREE" >/dev/null 2>&1 || true
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

wait_health() {
  local what="$1" tries=60
  while (( tries-- > 0 )); do
    curl -sf "$API/api/health" >/dev/null 2>&1 && return 0
    [[ -n "$SERVER_PID" ]] && ! kill -0 "$SERVER_PID" 2>/dev/null && { tail -40 "$SERVER_LOG" >&2 || true; die "$what died during startup"; }
    sleep 1
  done
  tail -40 "$SERVER_LOG" >&2 || true
  return 1
}

# ---------------------------------------------------------------------------
# Highest-numbered migration file a git ref carries -- this repo numbers revisions
# sequentially (0001_..., 0017_...), so the filename prefix IS the head. Cheap (no venv,
# no import) and, crucially, evaluated against THAT ref's tree, not the installed code.
migration_head_of() {
  git -C "$REPO_ROOT" ls-tree -r --name-only "$1" -- python/av_server/migrations/versions/ 2>/dev/null \
    | sed 's|.*/||' | grep -E '^[0-9]{4}_' | sort | tail -1 | cut -c1-4
}
CURRENT_FILE_HEAD="$(migration_head_of HEAD)"

if [[ -z "${OLD_TAG:-}" ]]; then
  # Newest tag whose migrations differ from HEAD's. The latest tag alone is not enough: a
  # release that added no migration would leave the drill with nothing to prove, and that
  # is exactly the case this used to die on every night.
  for tag in $(git -C "$REPO_ROOT" tag --sort=-v:refname); do
    if [[ -n "$(migration_head_of "$tag")" && "$(migration_head_of "$tag")" != "$CURRENT_FILE_HEAD" ]]; then
      OLD_TAG="$tag"
      break
    fi
  done
fi
[[ -n "${OLD_TAG:-}" ]] || die "no OLD_TAG given and no tag with a migration head different from HEAD's ($CURRENT_FILE_HEAD) -- git fetch --tags first?"
log "old code: $OLD_TAG (migration files head $(migration_head_of "$OLD_TAG") vs HEAD's $CURRENT_FILE_HEAD)"

log "phase 1: migrating a fresh database to CURRENT head with THIS checkout's code"
DATABASE_URL="$DATABASE_URL" AV_DATA_DIR="$WORK/data-new" \
  "$PY" -m uvicorn av_server.server:app --host 0.0.0.0 --port 8000 >>"$SERVER_LOG" 2>&1 &
SERVER_PID=$!
wait_health "current-code boot (to migrate to head)" || die "current code never became healthy while migrating the database to head"
NEW_HEAD="$("$PY" -c "
from alembic.script import ScriptDirectory
from av_server.database import _alembic_config
print(ScriptDirectory.from_config(_alembic_config()).get_current_head())
")"
pass "database migrated to head ($NEW_HEAD) by current code"
stop_server

# ---------------------------------------------------------------------------
log "phase 2: checking out $OLD_TAG into a throwaway worktree"
git worktree add --detach "$WORKTREE" "$OLD_TAG" >/dev/null

log "installing $OLD_TAG into a clean venv"
"$PY" -m venv "$OLD_VENV"
"$OLD_VENV/bin/pip" install -q "$WORKTREE"[dev] 2>>"$SERVER_LOG" \
  || die "could not install $OLD_TAG into a clean venv -- see $SERVER_LOG"

# The old code's head must come from the OLD install. This used to run `$PY -c "from
# av_server.database import ..."` -- the CURRENT venv, with the current code -- so "old
# head" always equalled the new head and the drill died at the gap check every single
# night (Sept 2026) without ever booting anything.
OLD_HEAD="$("$OLD_VENV/bin/python" -c "
from alembic.script import ScriptDirectory
from av_server.database import _alembic_config
print(ScriptDirectory.from_config(_alembic_config()).get_current_head())
" 2>/dev/null || echo unknown)"
log "old code's own migration head: $OLD_HEAD (repo currently at: $NEW_HEAD)"
[[ "$OLD_HEAD" != "unknown" ]] || die "could not read $OLD_TAG's migration head from its own venv -- see $SERVER_LOG"
[[ "$OLD_HEAD" != "$NEW_HEAD" ]] || die "old tag $OLD_TAG's migration head equals the current head ($NEW_HEAD) -- this drill needs a REAL gap between them to prove anything; pick an older OLD_TAG"

# Does $OLD_TAG actually contain the fix? A tag that predates it is SUPPOSED to crash
# here (the original bug, reproduced faithfully), not a regression in this script.
OLD_HAS_FIX=0
grep -q "_schema_is_ahead_of_this_binary" "$WORKTREE/python/av_server/database.py" 2>/dev/null && OLD_HAS_FIX=1
log "does $OLD_TAG include Probleme.md #136's fix? $([[ "$OLD_HAS_FIX" == 1 ]] && echo yes || echo no)"

# ---------------------------------------------------------------------------
log "phase 3: booting OLD code ($OLD_TAG) against the database CURRENT code migrated to $NEW_HEAD"
DATABASE_URL="$DATABASE_URL" AV_DATA_DIR="$WORK/data-old" \
  "$OLD_VENV/bin/python" -m uvicorn av_server.server:app --host 0.0.0.0 --port 8000 >>"$SERVER_LOG" 2>&1 &
SERVER_PID=$!

if wait_health "old code ($OLD_TAG) against a newer-than-it-recognizes schema"; then
  BOOTED=1
else
  BOOTED=0
fi

if [[ "$OLD_HAS_FIX" == 1 ]]; then
  [[ "$BOOTED" == 1 ]] || die "old code ($OLD_TAG) INCLUDES the fix but still failed to boot against a schema newer than it recognizes -- the fix has regressed"
  pass "old code ($OLD_TAG, includes the fix) booted healthy against a schema migrated past its own head"
else
  [[ "$BOOTED" == 0 ]] || die "old code ($OLD_TAG) does NOT include the fix but booted anyway -- investigate before trusting this drill's result"
  pass "old code ($OLD_TAG, predates the fix) failed to boot as expected -- faithfully reproduces the original bug, not a drill failure"
fi

log "ALL COMPAT DRILL CHECKS PASSED ($OLD_TAG vs head $NEW_HEAD)"
