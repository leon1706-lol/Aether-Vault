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
#   F-K. SDK run lifecycle, event stream, promotion policy, signed commits, audit trail
#   L-N. chaos drills (v1.3.0) — gated behind AV_E2E_CHAOS=1, see their own section below:
#        real redis outage, unwritable storage, server SIGKILLed mid-push
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
# v1.2.5: repoB already advanced "main" on the server (above), so this push now loses the
# ref-race compare-and-swap -- it queues locally instead of silently overwriting repoB's
# push (see Probleme.md). This means the divergence to discover/resolve is now on
# repoA's side (its local ref points at a commit the server never accepted), not
# repoB's -- repoB already matches the server exactly, so a pull there would report
# "Already up to date". Everything below operates from repoA instead of repoB.
av "$WORK/repoA" push >/dev/null

set +e
PULL_OUT="$(av "$WORK/repoA" pull 2>&1)"
set -e
grep -q "have diverged" <<<"$PULL_OUT" || die "pull should report divergence, got: $PULL_OUT"
REMOTE_TIP7="$(grep -oE '\[[0-9a-f]{7}\]' <<<"$PULL_OUT" | head -1 | tr -d '[]')"
[[ -n "$REMOTE_TIP7" ]] || die "divergence message should contain the remote tip hash"

set +e
CONFLICT_OUT="$(av "$WORK/repoA" merge "$REMOTE_TIP7" 2>&1)"
set -e
grep -qi "conflict" <<<"$CONFLICT_OUT" || die "merge without policy should abort listing conflicts, got: $CONFLICT_OUT"

MERGE_OUT="$(av "$WORK/repoA" merge "$REMOTE_TIP7" --theirs 2>&1)"
grep -q "auto-resolved via --theirs" <<<"$MERGE_OUT" || die "--theirs resolution failed: $MERGE_OUT"
grep -q "repoB's divergent line" "$WORK/repoA/shared.txt" || die "shared.txt should hold THEIRS after --theirs"

av "$WORK/repoA" push >/dev/null
TIP_B="$(cat "$WORK/repoA/.av/refs/heads/main")"
PARENTS="$(curl -sf "$API/api/commits/$TIP_B" | jsonget "len(d['parents'])")"
[[ "$PARENTS" == "2" ]] || die "merge commit should carry TWO parents over the wire, got $PARENTS"
# The merge's own ref update must actually land (not re-race against "ours", which never
# reached the server) -- confirming the core.py::_finalize_commit fix above.
SERVER_TIP="$(curl -sf "$API/api/refs/$PROJ_A/main" | jsonget "d['commit_hash']")"
[[ "$SERVER_TIP" == "$TIP_B" ]] || die "merge push should have landed on the server ref, got $SERVER_TIP (expected $TIP_B — still queued?)"
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
[[ "$(psqlq "SELECT version_num FROM alembic_version")" == "0005" ]] || die "legacy boot did not stamp chain head (0005)"
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

# ============================================================================
log "Phase I-note — engine smoke runs as the dedicated CI job"
log "(building/starting the engine image needs Docker; see e2e-engine-smoke)"

# ============================================================================
log "Phase J — signed commits end-to-end: keygen → auto-sign → verify → tamper"

command -v docker >/dev/null 2>&1 || true  # no-op; phase J itself is stack-free
"$PY" -c "import cryptography" 2>/dev/null || {
  log "Phase J SKIPPED — cryptography not installed ([sign] extra missing)"
  SKIP_J=1
}
if [[ "${SKIP_J:-0}" != "1" ]]; then
  mkdir -p "$WORK/repoSign" && cd "$WORK/repoSign"
  av . init --mode local --yes --no-repl >/dev/null
  av . registry keygen >/dev/null
  [[ -f ".av/keys/signing.pem" && -f ".av/keys/signing.pub" ]] \
    || die "keygen did not create the ed25519 keypair under .av/keys/"

  echo "signed payload" > signed.txt
  av . add signed.txt >/dev/null
  av . commit -m "signed commit" >/dev/null
  SIGNED_HASH="$(cat .av/refs/heads/main)"
  STORED="$("$PY" -c "import json;d=json.load(open('.av/commits/$SIGNED_HASH.json'));print(d.get('signature',{}).get('algo',''))")"
  [[ "$STORED" == "ed25519" ]] || die "commit was not auto-signed (algo=$STORED)"

  VERIFY_OUT="$(av . registry verify "$SIGNED_HASH" 2>&1)" || true
  grep -q "VERIFIED" <<<"$VERIFY_OUT" || die "verify should pass pre-tamper: $VERIFY_OUT"

  # Tamper AFTER signing — exactly what signing exists to catch:
  "$PY" - <<PYJ
import json
p = ".av/commits/$SIGNED_HASH.json"
d = json.load(open(p))
d["message"] = "tampered after signing"
json.dump(d, open(p, "w"))
PYJ
  set +e
  TAMPER_RC="$(av . registry verify "$SIGNED_HASH" >/dev/null 2>&1; echo $?)"
  set -e
  [[ "$TAMPER_RC" == "15" ]] || die "tampered commit must exit 15, got $TAMPER_RC"

  # Unsigned commits remain VALID (unsigned-ok):
  mkdir -p "$WORK/repoPlain" && cd "$WORK/repoPlain"
  av . init --mode local --yes --no-repl >/dev/null
  echo plain > plain.txt; av . add plain.txt >/dev/null; av . commit -m "plain" >/dev/null
  PLAIN_HASH="$(cat .av/refs/heads/main)"
  PLAIN_OUT="$(av . registry verify "$PLAIN_HASH" 2>&1)" || true
  grep -qi "UNSIGNED" <<<"$PLAIN_OUT" || die "unsigned verdict missing: $PLAIN_OUT"
  pass "Phase J: ed25519 roundtrip verified, tamper detected (exit 15), unsigned-ok"
fi

# ============================================================================
log "Phase K — audit trail query with filters over the LIVE protected server"

AUTH="Authorization: Bearer owner-secret-xyz"
AUDIT_JSON="$(curl -sf -H "$AUTH" "$API/api/admin/audit?action=commit.push&limit=20")"
COUNT_PUSH="$(<<<"$AUDIT_JSON" jsonget "d['total']")"
[[ "$COUNT_PUSH" -ge 1 ]] || die "action filter returned no commit.push rows"
FIRST_STATUS="$(<<<"$AUDIT_JSON" jsonget "[e['status_code'] for e in d['entries'] if e['details'].get('hash')][0]")"
[[ "$FIRST_STATUS" == "201" ]] || die "outcome capture missing (got $FIRST_STATUS)"

# Wrong-shape timestamp must be a 422, never a silent match-all:
BAD_TS_CODE="$(api_status -H "$AUTH" "$API/api/admin/audit?since=yesterday-ish")"
[[ "$BAD_TS_CODE" == "422" ]] || die "invalid since must 422, got $BAD_TS_CODE"

# CLI read path (repoC carries alice's still-valid token):
AV_AUDIT_OUT="$(cd "$WORK/repoC" && command av audit list --action commit.push --limit 5 2>&1)" \
  || true
grep -q "commit.push" <<<"$AV_AUDIT_OUT" || die "av audit list failed: $AV_AUDIT_OUT"
pass "Phase K: audit outcome capture + filters live (server + CLI)"

# ============================================================================
# Phases L/M/N — chaos drills (v1.3.0, todo.md item 28). Gated behind AV_E2E_CHAOS=1
# (default off) so the plain `e2e-suite` CI job / a local run is unaffected; the
# dedicated `chaos-drills` CI job sets it. Runnable locally the same way:
#   AV_E2E_CHAOS=1 AV_TEST_DATABASE_URL=... AV_TEST_REDIS_URL=... bash scripts/e2e_scenario.sh
# ============================================================================
if [[ "${AV_E2E_CHAOS:-0}" == "1" ]]; then

stop_server

# ----------------------------------------------------------------------------
log "Phase L — Redis unreachable: readiness degrades, pushes still succeed, restart recovers"
# NOT "the client queues" (todo.md's shorthand doesn't match this codebase's actual
# design): redis_cache.py's check_hash_exists()/add_hash() deliberately catch their own
# connection errors and degrade to DB-only checks (see that module's own docstring) — a
# commit push is NOT redis-dependent at all, only the dedup-shortcut optimization is. The
# real, verified contract this phase proves instead: /api/ready correctly reports the
# outage (used by orchestrators to stop routing traffic) while /api/health and the write
# path both keep working — exactly the liveness/readiness split
# development/infrastructure.md documents, under a REAL unreachable Redis rather than a
# hypothetical one.
start_server chaos-L-noredis REDIS_URL="redis://this-host-is-intentionally-absent:6379/0"

READY_CODE="$(api_status "$API/api/ready")"
[[ "$READY_CODE" == "503" ]] || die "Phase L: expected /api/ready 503 with redis unreachable, got $READY_CODE"
# NOT curl -f: -f discards the response body on any non-2xx status, and /api/ready's
# whole point here is a 503 body we need to actually read (found live: -f made this line
# fail every time, on a correctly-behaving server, since /api/ready correctly 503s).
curl -s "$API/api/ready" | grep -q '"redis": *false' || die "Phase L: /api/ready did not report the redis check as failing"
curl -sf "$API/api/health" >/dev/null 2>&1 || die "Phase L: /api/health must stay up regardless of readiness"

mkdir -p "$WORK/repoL" && cd "$WORK/repoL"
av . init --mode local --yes --no-repl >/dev/null
echo "pushed with redis down" > redis-down.txt
av . add redis-down.txt >/dev/null
av . commit -m "commit while redis unreachable" >/dev/null
[[ "$(pending_count "$WORK/repoL")" -eq 0 ]] \
  || die "Phase L: commit should have pushed successfully (redis outage is non-fatal to the write path), but it queued"
LREF="$(cat .av/refs/heads/main)"
[[ "$(api_status "$API/api/commits/$LREF")" == "200" ]] || die "Phase L: commit made during the redis outage never reached the server"

stop_server
start_server chaos-L-recovered   # real REDIS_URL restored (start_server's own default)
READY_CODE2="$(api_status "$API/api/ready")"
[[ "$READY_CODE2" == "200" ]] || die "Phase L: /api/ready should recover once redis is reachable again, got $READY_CODE2"
pass "Phase L: /api/ready degraded independently of /api/health under a real redis outage; writes kept working; recovered cleanly"

# ----------------------------------------------------------------------------
log "Phase M — storage write failure: upload fails honestly, nothing partial lands, client queues"
# A genuinely FULL disk isn't reliably producible/safe in CI — a read-only AV_DATA_DIR
# produces the identical observable failure (the storage layer's write call fails), which
# is what this phase actually needs to prove: the failure surfaces honestly (no silent
# data loss, no partially-written object) and the client's offline-queue path takes over,
# exactly as it does for any other unreachable-server case (Phase B).
stop_server
READONLY_DATA="$WORK/data-readonly"
# Pre-create the three top-level dirs CASStorage.__init__ itself creates at import time
# (objects/commits/refs) BEFORE locking the tree down — its own `mkdir(exist_ok=True)`
# calls only need to STAT an already-existing path, not write a new one, so the server can
# still boot against a read-only data dir. Locking down the FULL tree (-R), not just the
# top level, is what then makes the real write this phase cares about — a NEW per-object
# shard subdirectory created during an actual upload — fail honestly instead of quietly
# succeeding into an already-writable subdirectory. (Found live: the non-recursive,
# subdirs-not-precreated version of this crashed the whole server at import/startup with
# an uncaught PermissionError instead of the intended "server's up, one write fails"
# scenario — see development/Probleme.md.)
mkdir -p "$READONLY_DATA/objects" "$READONLY_DATA/commits" "$READONLY_DATA/refs"
chmod -R 555 "$READONLY_DATA"
if ! ( : > "$READONLY_DATA/write-probe" ) 2>/dev/null; then
  start_server chaos-M-readonly AV_DATA_DIR="$READONLY_DATA"

  mkdir -p "$WORK/repoM" && cd "$WORK/repoM"
  av . init --mode local --yes --no-repl >/dev/null
  echo "should fail to land" > readonly-target.txt
  av . add readonly-target.txt >/dev/null
  set +e
  M_COMMIT_OUT="$(av . commit -m "commit against a read-only data dir" 2>&1)"
  set -e
  [[ "$(pending_count "$WORK/repoM")" -ge 1 ]] \
    || die "Phase M: commit against an unwritable AV_DATA_DIR must queue locally, not silently succeed. Output: $M_COMMIT_OUT"
  MOBJ_COUNT="$(find "$READONLY_DATA" -type f ! -name write-probe 2>/dev/null | wc -l | tr -d ' ')"
  [[ "$MOBJ_COUNT" == "0" ]] || die "Phase M: a partial object landed in the unwritable data dir ($MOBJ_COUNT file(s)) — should be zero"

  stop_server
  chmod -R 755 "$READONLY_DATA"
  start_server chaos-M-recovered
  av . push >/dev/null
  [[ "$(pending_count "$WORK/repoM")" -eq 0 ]] || die "Phase M: queued commit did not drain once storage was writable again"
  pass "Phase M: write failure against an unwritable AV_DATA_DIR queued honestly; nothing partial landed; recovered cleanly"
else
  rm -f "$READONLY_DATA/write-probe"
  log "Phase M SKIPPED — this environment does not honor chmod 555 as unwritable (root, or a filesystem that ignores POSIX perms)"
fi
chmod -R 755 "$READONLY_DATA" 2>/dev/null || true

# ----------------------------------------------------------------------------
log "Phase N — server killed mid-push (SIGKILL, not graceful): pending_push survives intact, later push drains cleanly"

stop_server 2>/dev/null || true
start_server chaos-N-before

mkdir -p "$WORK/repoN" && cd "$WORK/repoN"
av . init --mode local --yes --no-repl >/dev/null
for i in 1 2 3 4 5; do
  echo "chaos payload $i" > "n-file-$i.txt"
  av . add "n-file-$i.txt" >/dev/null
  av . commit -m "chaos commit $i" --no-upload >/dev/null   # guaranteed queued, regardless of kill timing
done
[[ "$(pending_count "$WORK/repoN")" -ge 1 ]] || die "Phase N: setup commits should be queued before the push even starts"

# Push in the background, then SIGKILL the server almost immediately — no graceful
# shutdown, no drain window, proving atomic local writes (core.py's temp-file+fsync+
# os.replace pattern) rather than a clean-stop path this suite already covers (Phase B).
( av . push >/dev/null 2>&1 || true ) &
PUSH_PID=$!
sleep 0.05
kill -9 "$SERVER_PID" 2>/dev/null || true
wait "$PUSH_PID" 2>/dev/null || true
SERVER_PID=""

# pending_push must be well-formed JSON no matter how much of the push landed before the
# kill — a torn/partial write here would be the real bug this phase exists to catch.
if [[ -s "$WORK/repoN/.av/pending_push" ]]; then
  "$PY" -c "import json; json.load(open('$WORK/repoN/.av/pending_push'))" \
    || die "Phase N: .av/pending_push is corrupted after the server was killed mid-push"
fi

start_server chaos-N-after
av "$WORK/repoN" push >/dev/null
[[ "$(pending_count "$WORK/repoN")" -eq 0 ]] || die "Phase N: pending_push did not fully drain after the server came back"
N_COUNT="$(curl -sf "$API/api/commits?limit=100&project_id=$(project_of "$WORK/repoN")" | jsonget "d['total']")"
[[ "$N_COUNT" -ge 5 ]] || die "Phase N: not all 5 chaos commits reached the server after recovery (total=$N_COUNT)"
pass "Phase N: pending_push survived a SIGKILL mid-push intact; full drain on the next push"

fi  # AV_E2E_CHAOS
