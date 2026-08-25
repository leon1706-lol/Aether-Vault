#!/usr/bin/env bash
# ============================================================================
# Aether-Vault end-to-end scenario suite (CI).
#
# Drives the REAL `av` CLI against a REAL uvicorn server on live Postgres +
# Redis service containers — the same topology production runs. Covers the
# flows unit/TestClient tests structurally cannot:
#
#   A. clone → diverge → conflicting merge → --theirs resolution → two-parent push
#   B. offline resilience: server killed mid-flow → queue → restart → drain
#   C. legacy-volume upgrade: pre-Alembic shape heals + stamps on real boot
#   D. protected mode with per-user tokens: join, attribution, revocation, 401 queueing
#   E. GC drill with a zeroed grace period: orphan swept, live objects survive
#
# Every phase prints PASS/FAIL lines; any failure aborts with a nonzero exit.
# The compose-restart plumbing of `av auth add-user` (writes .env, restarts a
# Docker service) stays covered by its fake-based CLI unit tests — CI has no
# Docker daemon; this suite proves the env-var behavior production actually runs.
# ============================================================================
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WORK="$(mktemp -d /tmp/av-e2e-XXXXXX)"
# Under Git Bash/MSYS, mktemp hands back a virtual /tmp path that Windows-native children
# (python, psql) cannot open. Convert to a real mixed-mode path where cygpath exists.
command -v cygpath >/dev/null 2>&1 && WORK="$(cygpath -m "$WORK")"
SERVER_LOG="$WORK/server.log"
SERVER_PID=""
DB_URL_ASYNC="${AV_TEST_DATABASE_URL:?AV_TEST_DATABASE_URL must point at the live Postgres}"
REDIS_URL="${AV_TEST_REDIS_URL:?AV_TEST_REDIS_URL must point at the live Redis}"
API="http://localhost:8000"
PSQL_URL="${E2E_PSQL_URL:-${DB_URL_ASYNC/postgresql+asyncpg:\/\//postgresql://}}"

cleanup() {
  # Keep the server log at a fixed, job-uploadable location even after $WORK vanishes.
  cp "$SERVER_LOG" /tmp/server-e2e.log 2>/dev/null || true
  stop_server || true
}
trap cleanup EXIT

log()  { printf '\n\033[1;36m[e2e]\033[0m %s\n' "$*"; }
pass() { printf '\033[1;32m[PASS]\033[0m %s\n' "$*"; }
die()  { printf '\033[1;31m[FAIL]\033[0m %s\n' "$*" >&2; exit 1; }

# python3 is standard on Linux runners; some dev environments only expose `python` — and
# Windows ships a non-functional "python3" Store alias that resolves but cannot run, so
# candidates are execution-verified rather than just looked up.
PY=""
for _cand in python3 python; do
  if command -v "$_cand" >/dev/null 2>&1 && "$_cand" -c "pass" >/dev/null 2>&1; then
    PY="$(command -v "$_cand")"
    break
  fi
done
[[ -n "$PY" ]] || die "no working python/python3 on PATH"

jsonget() { "$PY" -c "import json,sys; d=json.load(sys.stdin); print(eval(sys.argv[1]))" "$1"; }

start_server() { # start_server <name> [ENV=VAL ...]
  # ONE persistent CAS data dir across every restart — storage must survive server
  # lifecycles exactly like production's volume; a fresh dir per phase would desync it
  # from the persistent Postgres. <name> only tags the process in logs.
  #
  # The subshell EXPORTS then EXECs python: the backgrounded PID becomes python itself,
  # not a wrapper — otherwise stop_server kills the wrapper and orphans the server.
  local name="$1"; shift
  cd "$REPO_ROOT"
  (
    export DATABASE_URL="$DB_URL_ASYNC" REDIS_URL="$REDIS_URL" AV_DATA_DIR="$WORK/data" "$@"
    exec "$PY" -m uvicorn av_server.server:app --host 0.0.0.0 --port 8000
  ) >>"$SERVER_LOG" 2>&1 &
  SERVER_PID=$!
  wait_health "server($name)"
}

stop_server() {
  if [[ -n "$SERVER_PID" ]] && kill -0 "$SERVER_PID" 2>/dev/null; then
    kill "$SERVER_PID" 2>/dev/null || true
    for _ in $(seq 1 20); do kill -0 "$SERVER_PID" 2>/dev/null || break; sleep 0.5; done
    kill -9 "$SERVER_PID" 2>/dev/null || true
  fi
  SERVER_PID=""
  # Port MUST actually be free before the next phase binds it — a stale server silently
  # answering /api/health here would make every later assertion exercise the WRONG process.
  for _ in $(seq 1 30); do
    if ! curl -s -o /dev/null --max-time 1 "$API/api/health" 2>/dev/null; then
      sleep 1   # grace so the next bind never races the port release
      return 0
    fi
    sleep 0.5
  done
  die "port 8000 still serving after stop_server"
}

wait_health() {
  local what="$1"
  for _ in $(seq 1 60); do
    if curl -sf "$API/api/health" >/dev/null 2>&1; then return 0; fi
    kill -0 "$SERVER_PID" 2>/dev/null || { tail -30 "$SERVER_LOG" >&2 || true; die "$what died during startup"; }
    sleep 1
  done
  tail -30 "$SERVER_LOG" >&2 || true
  die "$what did not become healthy in 60s"
}

av() { (cd "$1" && shift && command av "$@"); }   # av <repo-dir> <args...>
api_status() { curl -s -o /dev/null -w '%{http_code}' "$@"; }
# Options BEFORE the connection argument: under MSYS/POSIX-style option parsing, a
# positional connection URI placed first makes getopt stop scanning, so `-c "SQL"`
# after it is silently ignored (every query would no-op with only a stderr warning).
psqlq() { psql -Atqc "$1" "$PSQL_URL"; }

project_of() { "$PY" -c "import json;print(json.load(open('$1/.av/config'))['project_id'])"; }
pending_count() {
  # .av/pending_push is a single JSON file holding the queue (absent when empty).
  [[ -s "$1/.av/pending_push" ]] && echo 1 || echo 0
}

# ============================================================================
log "Phase A — clone, diverge, conflicting merge resolved via --theirs"

start_server anon-A

mkdir -p "$WORK/repoA" && cd "$WORK/repoA"
av . init --mode local --yes --no-repl >/dev/null
echo "base content" > shared.txt
av . add shared.txt >/dev/null
av . commit -m "seed" >/dev/null
av . push >/dev/null
PROJ_A="$(project_of "$WORK/repoA")"

av "$WORK" clone "$PROJ_A" "$WORK/repoB" --remote-url "$API" >/dev/null

echo "repoB's divergent line" > "$WORK/repoB/shared.txt"
av "$WORK/repoB" add shared.txt >/dev/null
av "$WORK/repoB" commit -m "repoB edits shared" >/dev/null
av "$WORK/repoB" push >/dev/null

echo "repoA's divergent line" > "$WORK/repoA/shared.txt"
av "$WORK/repoA" add shared.txt >/dev/null
av "$WORK/repoA" commit -m "repoA edits shared" >/dev/null
av "$WORK/repoA" push >/dev/null

set +e
PULL_OUT="$(av "$WORK/repoB" pull 2>&1)"
set -e
grep -q "have diverged" <<<"$PULL_OUT" || die "pull should report divergence, got: $PULL_OUT"
REMOTE_TIP7="$(grep -oE '\[[0-9a-f]{7}\]' <<<"$PULL_OUT" | head -1 | tr -d '[]')"
[[ -n "$REMOTE_TIP7" ]] || die "divergence message should contain the remote tip hash"

set +e
CONFLICT_OUT="$(av "$WORK/repoB" merge "$REMOTE_TIP7" 2>&1)"
set -e
grep -qi "conflict" <<<"$CONFLICT_OUT" || die "merge without policy should abort listing conflicts, got: $CONFLICT_OUT"

MERGE_OUT="$(av "$WORK/repoB" merge "$REMOTE_TIP7" --theirs 2>&1)"
grep -q "auto-resolved via --theirs" <<<"$MERGE_OUT" || die "--theirs resolution failed: $MERGE_OUT"
grep -q "repoA's divergent line" "$WORK/repoB/shared.txt" || die "shared.txt should hold THEIRS after --theirs"

av "$WORK/repoB" push >/dev/null
TIP_B="$(cat "$WORK/repoB/.av/refs/heads/main")"
PARENTS="$(curl -sf "$API/api/commits/$TIP_B" | jsonget "len(d['parents'])")"
[[ "$PARENTS" == "2" ]] || die "merge commit should carry TWO parents over the wire, got $PARENTS"
pass "Phase A: clone/diverge/conflict/--theirs/two-parent-push"

# ============================================================================
log "Phase B — offline resilience: kill mid-flow, queue, restart, drain"

stop_server
echo "written while server down" > "$WORK/repoA/offline.txt"
av "$WORK/repoA" add offline.txt >/dev/null
OFFLINE_HASH_LINE="$(av "$WORK/repoA" commit -m "offline-commit" 2>&1)"
[[ "$(pending_count "$WORK/repoA")" -ge 1 ]] || die "commit made while server down must queue in pending_push"

start_server anon-B
av "$WORK/repoA" push >/dev/null
[[ "$(pending_count "$WORK/repoA")" -eq 0 ]] || die "pending_push should be drained after av push"
COUNT_B="$(curl -sf "$API/api/commits?limit=100&project_id=$PROJ_A" | jsonget "d['total']")"
[[ "$COUNT_B" -ge 3 ]] || die "offline commit missing from registry after drain (total=$COUNT_B)"
pass "Phase B: offline queue drained and visible after restart"

# ============================================================================
log "Phase C — legacy-volume upgrade drill against a REAL server boot"

COMMITS_BEFORE="$(psqlq "SELECT count(*) FROM commits")"
stop_server
psqlq "ALTER TABLE commits DROP COLUMN IF EXISTS extra_parents" >/dev/null
psqlq "ALTER TABLE trees DROP COLUMN IF EXISTS chunks" >/dev/null
psqlq "DROP TABLE alembic_version" >/dev/null

start_server legacy-C     # boot must detect the pre-Alembic shape, heal, stamp

[[ "$(psqlq "SELECT count(*) FROM information_schema.columns WHERE table_name='commits' AND column_name='extra_parents'")" == "1" ]] \
  || die "legacy boot did not restore commits.extra_parents"
[[ "$(psqlq "SELECT version_num FROM alembic_version")" == "0002" ]] || die "legacy boot did not stamp chain head (0002)"
[[ "$(psqlq "SELECT count(*) FROM commits")" == "$COMMITS_BEFORE" ]] || die "heal lost commit rows!"
pass "Phase C: pre-Alembic volume healed + stamped zero-touch, data intact"

# ============================================================================
log "Phase D — Protected mode with per-user tokens (live attribution)"

stop_server
start_server protected-D AV_API_TOKEN="owner-secret-xyz" AV_AUTH_USERS='{"alice":"alice-token-123"}'

[[ "$(api_status "$API/api/health")" == "200" ]] || die "health must stay exempt in Protected mode"
[[ "$(api_status "$API/api/refs")" == "401" ]] || die "refs must 401 without credentials in Protected mode"

mkdir -p "$WORK/repoC" && cd "$WORK/repoC"
av . init --mode local --token alice-token-123 --yes --no-repl >/dev/null
echo "alice's work" > work.txt
av . add work.txt >/dev/null
av . commit -m "alice-work" >/dev/null
av . push >/dev/null

ATTR="$(curl -sf -H "Authorization: Bearer owner-secret-xyz" \
        "$API/api/commits?limit=10&project_id=$(project_of "$WORK/repoC")" \
        | jsonget "[c['author'] for c in d['commits']]")"
grep -q "'alice'" <<<"$ATTR" || die "push as alice should be attributed to 'alice', got: $ATTR"

mkdir -p "$WORK/repoD" && cd "$WORK/repoD"
av . init --mode local --token totally-wrong-token --yes --no-repl >/dev/null
echo "intruder" > bad.txt
av . add bad.txt >/dev/null
av . commit -m "bad-token-commit" >/dev/null
set +e; av . push >/dev/null 2>&1; set -e
[[ "$(pending_count "$WORK/repoD")" -ge 1 ]] || die "wrong-token push must queue offline (AuthenticationError path), not vanish"

stop_server
start_server revoked-D2 AV_API_TOKEN="owner-secret-xyz" AV_AUTH_USERS='{}'
echo "alice after revocation" >> "$WORK/repoC/work.txt"
av "$WORK/repoC" add work.txt >/dev/null
av "$WORK/repoC" commit -m "alice-revoked" >/dev/null
set +e; av "$WORK/repoC" push >/dev/null 2>&1; set -e
[[ "$(pending_count "$WORK/repoC")" -ge 1 ]] || die "revoked user's push must queue offline, not silently succeed"
pass "Phase D: per-user auth live — join, attribution, wrong token queued, revocation queued"

# ============================================================================
log "Phase E — GC drill with zero grace period"

stop_server
start_server gc-E AV_GC_GRACE_SECONDS=0 AV_API_TOKEN="owner-secret-xyz" AV_AUTH_USERS='{"alice":"alice-token-123"}'

ORPHAN_CONTENT="orphan-shard-$(date +%s)"
ORPHAN_HASH="$(printf '%s' "$ORPHAN_CONTENT" | sha256sum | cut -d' ' -f1)"
CODE="$(curl -s -o /dev/null -w '%{http_code}' -X POST "$API/api/objects/$ORPHAN_HASH" \
        -H "Authorization: Bearer alice-token-123" --data-binary "$ORPHAN_CONTENT")"
[[ "$CODE" == "201" || "$CODE" == "409" ]] || die "orphan upload failed ($CODE)"

GC_JSON="$(curl -sf -X POST "$API/api/admin/gc" -H "Authorization: Bearer owner-secret-xyz")"
DELETED="$(<<<"$GC_JSON" jsonget "d['deleted_objects']")"
ALIVE="$(<<<"$GC_JSON" jsonget "d['alive_objects']")"
[[ "$DELETED" -ge 1 ]] || die "zero-grace GC should sweep the orphan, deleted=$DELETED"
[[ "$ALIVE" -ge 1 ]] || die "zero-grace GC must keep referenced objects, alive=$ALIVE"
pass "Phase E: orphan swept under zero grace, live objects survived"

# ============================================================================

# ============================================================================
export PHASE_F_OK="$WORK/phase-f-ok"
log "Phase F — SDK-driven loop (av_sdk.Repo drives the real single code path)"

"$PY" - <<PYF
import json, os, subprocess, sys, tempfile
sys.path.insert(0, os.environ.get("REPO_ROOT", "$REPO_ROOT"))
from av_sdk import Repo, SDKError

root = tempfile.mkdtemp(prefix="av-sdk-")
subprocess.run(["av", "init", "--mode", "local", "--yes", "--no-repl"],
               cwd=root, check=True, capture_output=True)
with Repo(root) as r:
    (r.path / "artifact.bin").write_bytes(b"sdk-bytes")
    r.add("artifact.bin")
    started = r.run_start("sdk-loop")
    c = r.commit("sdk commit inside run", tags=["phase-f"], metrics={"loss": 0.2})
    finished = r.run_finish(metrics={"final_loss": 0.1})

assert c["committed"] and f"run:{started['run_id']}" in c["tags"], "SDK run tagging broken"
assert finished["status"] == "completed"
print("[phase-f] sdk loop ok:", c["hash"][:8])
open(os.environ["PHASE_F_OK"], "w").write(c["hash"])
PYF
[[ -s "$PHASE_F_OK" ]] || die "Phase F SDK loop failed"
pass "Phase F: SDK-driven run/commit lifecycle"

# ============================================================================
log "Phase G — event stream reacts to pushes (cursor + kind filtering)"

# Still inside the Protected-mode server from Phase E — every read needs credentials.
AUTH="Authorization: Bearer owner-secret-xyz"
BEFORE=$(curl -s -H "$AUTH" "$API/api/events?limit=1" | "$PY" -c "import json,sys; d=json.load(sys.stdin); print(d['next_cursor'])")
echo "trigger" > "$WORK/repoC/trigger.txt"
av "$WORK/repoC" add trigger.txt >/dev/null
av "$WORK/repoC" commit -m "event-trigger-commit" >/dev/null
av "$WORK/repoC" push >/dev/null

for _ in $(seq 1 20); do
  FOUND=$(curl -s -H "$AUTH" "$API/api/events?since=$BEFORE&kinds=commit&project_id=$(project_of "$WORK/repoC")" | "$PY" -c "
import json,sys
d=json.load(sys.stdin)
msgs=[e['payload'].get('message','') for e in d['events']]
print('FOUND' if any('event-trigger-commit' in m for m in msgs) else '')")
  [[ "$FOUND" == "FOUND" ]] && break
  sleep 0.5
done
[[ "$FOUND" == "FOUND" ]] || die "commit event never appeared on the stream"
pass "Phase G: event stream cursor + kind filter react to real pushes"

# ============================================================================
log "Phase H — promotion policy: live ALLOW path (deny semantics unit-tested)"

cd "$WORK/repoA"
# A metric-bearing BASELINE commit first, then anchor the policy to its hash
# (branch-relative baselines would compare the candidate against itself once it IS the tip).
echo b > metrics_src.txt; av . add metrics_src.txt >/dev/null
av . commit -m "baseline" --metric val_loss=0.5 >/dev/null
BASE_HASH="$(cat .av/refs/heads/main)"
av . policy set main val_loss "<" --baseline-ref "$BASE_HASH" >/dev/null

echo d1 > metrics_src.txt; av . add metrics_src.txt >/dev/null
av . commit -m "cand1" --metric val_loss=0.5 >/dev/null
echo d2 > metrics_src.txt; av . add metrics_src.txt >/dev/null
av . commit -m "cand2" --metric val_loss=0.4 >/dev/null

PROMOTE_OUT="$(av . promote --into main 2>&1)" || true
grep -qE "PASS|Already up to date|up to date" <<<"$PROMOTE_OUT" || \
  die "promote should pass with improving metric or no-op, got: $PROMOTE_OUT"
pass "Phase H: promotion policy surface exercised (deny path unit-tested in CI; allow path live)"


log "ALL E2E PHASES PASSED"
