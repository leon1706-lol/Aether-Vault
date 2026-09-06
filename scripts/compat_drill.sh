#!/usr/bin/env bash
# ============================================================================
# v1.3.4 (todo.md item 39, W3g): migration compatibility drill — an OLDER server binary
# vs a database a NEWER binary has already migrated to head. Proves (or disproves)
# `development/Probleme.md` #136's fix: before that fix, ANY older replica's next
# restart during a rolling upgrade crashed outright once a newer replica advanced the
# schema past what the older binary's own alembic script directory recognizes.
#
# What this actually does:
#   1. `git worktree add` a real PREVIOUS release tag (defaults to the most recent one)
#      into a throwaway directory — the exact old CODE, not a simulation of it.
#   2. Boots CURRENT code against a fresh Postgres, migrating it all the way to HEAD.
#   3. Stops it, boots the OLD code's server against that SAME now-fully-migrated
#      database.
#   4. Asserts the OLD binary comes up healthy (not a crash-loop) — proving
#      Probleme.md #136's fix, not just that it LOOKS plausible from reading the code.
#
# Usage: DATABASE_URL=postgresql+asyncpg://... [OLD_TAG=v1.2.4] bash scripts/compat_drill.sh
# Needs a real, disposable Postgres (same contract as scripts/migrations_drill.py) and a
# git checkout with tag history (`git fetch --tags` / `actions/checkout` with
# `fetch-depth: 0`).
#
# HONEST CURRENT LIMITATION (as of the v1.3.4 CI/CD pass that added this script): the
# only tag that exists right now, v1.2.4, predates Probleme.md #136's fix entirely --
# running this drill against it PROVES THE ORIGINAL BUG, not the fix (v1.2.4's own code
# has no `_schema_is_ahead_of_this_binary` at all, so it crashes exactly as #136
# describes). That is expected, not a regression in this drill. This script only starts
# proving the FIX holds once run with `OLD_TAG` set to some FUTURE tag that includes
# #136's fix, against a database migrated further ahead by an even-newer checkout — which
# is why this is NOT yet wired into any CI workflow (a gating job that predictably fails
# today, for a reason unrelated to any regression, trains reviewers to ignore it). Wire it
# in once the next tag past this fix exists; until then it's available for manual/local
# use and as the ready mechanism this backlog item asked for.
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
OLD_TAG="${OLD_TAG:-$(git -C "$REPO_ROOT" describe --tags --abbrev=0 2>/dev/null)}"
[[ -n "$OLD_TAG" ]] || die "no OLD_TAG given and no tag reachable from HEAD (git fetch --tags first?)"
log "old code: $OLD_TAG"

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
OLD_HEAD="$(cd "$WORKTREE" && "$PY" -c "
from alembic.script import ScriptDirectory
from av_server.database import _alembic_config
print(ScriptDirectory.from_config(_alembic_config()).get_current_head())
" 2>/dev/null || echo unknown)"
log "old code's own migration head: $OLD_HEAD (repo currently at: $NEW_HEAD)"
[[ "$OLD_HEAD" != "$NEW_HEAD" ]] || die "old tag $OLD_TAG's migration head equals the current head ($NEW_HEAD) -- this drill needs a REAL gap between them to prove anything; pick an older OLD_TAG"

log "installing $OLD_TAG into a clean venv"
"$PY" -m venv "$OLD_VENV"
"$OLD_VENV/bin/pip" install -q "$WORKTREE"[dev] 2>>"$SERVER_LOG" \
  || die "could not install $OLD_TAG into a clean venv -- see $SERVER_LOG"

# Does $OLD_TAG's own checkout actually contain Probleme.md #136's fix? Sets the
# EXPECTED outcome below honestly instead of treating any boot failure as this drill's
# own failure — a tag that predates the fix is SUPPOSED to crash here (that's the
# original bug, reproduced faithfully), not a regression in this script.
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
  [[ "$BOOTED" == 1 ]] || die "old code ($OLD_TAG) INCLUDES Probleme.md #136's fix but still failed to boot against a schema newer than it recognizes -- the fix has regressed"
  pass "old code ($OLD_TAG, includes the fix) booted healthy against a schema migrated past its own head — Probleme.md #136's fix holds"
else
  [[ "$BOOTED" == 0 ]] || die "old code ($OLD_TAG) does NOT include Probleme.md #136's fix but booted anyway -- either the fix isn't needed after all, or this drill's own database-ahead setup is wrong; investigate before trusting this drill's result either way"
  pass "old code ($OLD_TAG, predates the fix) failed to boot as expected — faithfully reproduces the ORIGINAL bug, not a drill failure. Re-run with OLD_TAG set to a tag that includes the fix to actually prove it holds."
fi

log "ALL COMPAT DRILL CHECKS PASSED ($OLD_TAG vs head $NEW_HEAD)"
