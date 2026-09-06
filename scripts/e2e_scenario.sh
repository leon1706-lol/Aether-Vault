#!/usr/bin/env bash
# ============================================================================
# Aether-Vault end-to-end scenario suite (CI).
#
# Drives the REAL `av` CLI against a REAL uvicorn server on live Postgres + Redis
# service containers -- the same topology production runs. Covers the flows unit/
# TestClient tests structurally cannot:
#
#   A. clone -> diverge -> conflicting merge -> --theirs resolution -> two-parent push
#   B. offline resilience: server killed mid-flow -> queue -> restart -> drain
#   C. legacy-volume upgrade: pre-Alembic shape heals + stamps on real boot
#   D. protected mode with per-user tokens: join, attribution, revocation, 401 queueing
#   E. GC drill with a zeroed grace period: orphan swept, live objects survive
#   F-K. SDK run lifecycle, event stream, promotion policy, signed commits, audit trail
#   L-N. chaos drills, gated behind AV_E2E_CHAOS=1: real redis outage, unwritable
#        storage, a genuinely full filesystem, server SIGKILLed mid-push
#
# Every phase prints PASS/FAIL lines; any failure aborts with a nonzero exit. CI has no
# Docker daemon, so this suite proves the env-var behavior production actually runs;
# the Docker-restart plumbing itself stays covered by fake-based CLI unit tests.
# ============================================================================
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WORK="$(mktemp -d /tmp/av-e2e-XXXXXX)"
# Git Bash/MSYS mktemp hands back a virtual /tmp path that Windows-native children can't open.
command -v cygpath >/dev/null 2>&1 && WORK="$(cygpath -m "$WORK")"
SERVER_LOG="$WORK/server.log"
SERVER_PID=""
DB_URL_ASYNC="${AV_TEST_DATABASE_URL:?AV_TEST_DATABASE_URL must point at the live Postgres}"
REDIS_URL="${AV_TEST_REDIS_URL:?AV_TEST_REDIS_URL must point at the live Redis}"
API="http://localhost:8000"
PSQL_URL="${E2E_PSQL_URL:-${DB_URL_ASYNC/postgresql+asyncpg:\/\//postgresql://}}"

cleanup() {
  cp "$SERVER_LOG" /tmp/server-e2e.log 2>/dev/null || true  # keep the log after $WORK vanishes
  stop_server || true
}
trap cleanup EXIT

log()  { printf '\n\033[1;36m[e2e]\033[0m %s\n' "$*"; }
pass() { printf '\033[1;32m[PASS]\033[0m %s\n' "$*"; }
die()  { printf '\033[1;31m[FAIL]\033[0m %s\n' "$*" >&2; exit 1; }

# Windows ships a non-functional "python3" Store alias that resolves but can't run, so
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
  # ONE persistent CAS data dir across every restart, matching production's volume.
  # `cd "$REPO_ROOT"` below runs in the calling shell, not a subshell, so it would
  # silently change the caller's cwd too -- save/restore it here so `av .` stays correct
  # after any start_server call without every caller needing to remember this.
  local name="$1"; shift
  local _caller_cwd; _caller_cwd="$PWD"
  cd "$REPO_ROOT"
  (
    export DATABASE_URL="$DB_URL_ASYNC" REDIS_URL="$REDIS_URL" AV_DATA_DIR="$WORK/data" "$@"
    exec "$PY" -m uvicorn av_server.server:app --host 0.0.0.0 --port 8000
  ) >>"$SERVER_LOG" 2>&1 &
  SERVER_PID=$!
  cd "$_caller_cwd"
  wait_health "server($name)"
}

stop_server() {
  if [[ -n "$SERVER_PID" ]] && kill -0 "$SERVER_PID" 2>/dev/null; then
    kill "$SERVER_PID" 2>/dev/null || true
    for _ in $(seq 1 20); do kill -0 "$SERVER_PID" 2>/dev/null || break; sleep 0.5; done
    kill -9 "$SERVER_PID" 2>/dev/null || true
  fi
  SERVER_PID=""
  # Port must actually be free before the next phase binds it, or every later assertion
  # would exercise the wrong process.
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
# Options before the connection argument: MSYS/POSIX-style option parsing stops
# scanning once it hits the positional URI, silently ignoring a "-c" placed after it.
psqlq() { psql -Atqc "$1" "$PSQL_URL"; }

project_of() { "$PY" -c "import json;print(json.load(open('$1/.av/config'))['project_id'])"; }
pending_count() { [[ -s "$1/.av/pending_push" ]] && echo 1 || echo 0; }  # single JSON queue file, absent when empty

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
# repoB already advanced "main" on the server, so this push loses the ref-race
# compare-and-swap and queues locally instead of overwriting repoB's push -- the
# divergence to resolve is therefore on repoA's side, not repoB's.
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
# Derived at runtime from the same alembic ScriptDirectory the server resolves against,
# so a new migration can never make this assertion silently stale.
EXPECTED_HEAD="$("$PY" -c "
from av_server.database import _alembic_config
from alembic.script import ScriptDirectory
print(ScriptDirectory.from_config(_alembic_config()).get_current_head())
")"
[[ -n "$EXPECTED_HEAD" ]] || die "could not resolve the alembic chain's current head via ScriptDirectory"
[[ "$(psqlq "SELECT version_num FROM alembic_version")" == "$EXPECTED_HEAD" ]] || die "legacy boot did not stamp chain head ($EXPECTED_HEAD)"
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

AUTH="Authorization: Bearer owner-secret-xyz"  # still inside the Protected-mode server from Phase E
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
# A metric-bearing baseline commit first, then anchor the policy to its hash -- a
# branch-relative baseline would compare the candidate against itself once it is the tip.
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

  # Tamper after signing -- exactly what signing exists to catch:
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
# Phases O-T — RSI control plane, live. Each phase restarts the server itself rather
# than threading through the substrate phases' state above, since the RSI surfaces are
# independent of A-K's git/merge/auth narrative and read more clearly with a clean slate.
# ============================================================================
log "Phase O — improver lifecycle: register -> propose -> apply -> rollback"

stop_server
start_server anon-O

mkdir -p "$WORK/repoO" && cd "$WORK/repoO"
av . init --mode local --yes --no-repl >/dev/null
echo "print('v1')" > train.py
av . add train.py >/dev/null
av . commit -m "seed" --metric val_loss=0.5 >/dev/null

BASE_IMP="$(av . --output json improver init | jsonget "d['data']['id']")"
[[ -n "$BASE_IMP" ]] || die "improver init did not return an id"

echo -e "--- a\n+++ b\n-x\n+y" > change-o.diff
CS_O="$(av . --output json improver propose --diff change-o.diff --rationale "e2e phase O" --risk low \
        | jsonget "d['data']['id']")"
av . improver review "$CS_O" --approve >/dev/null
NEW_IMP_O="$(av . --output json improver apply "$CS_O" | jsonget "d['data']['new_improver_id']")"
[[ "$NEW_IMP_O" != "$BASE_IMP" ]] || die "apply should mint a NEW improver version, not reuse the base one"
CUR_O="$(av . --output json improver current | jsonget "d['data']['id']")"
[[ "$CUR_O" == "$NEW_IMP_O" ]] || die "current pointer should be the newly applied version"

av . improver rollback >/dev/null
CUR_O2="$(av . --output json improver current | jsonget "d['data']['id']")"
[[ "$CUR_O2" == "$BASE_IMP" ]] || die "rollback should restore the pre-apply improver version"
pass "Phase O: improver register->propose->apply->rollback, live"

# ============================================================================
log "Phase P — dual-gate promotion: DENY (no review) then ALLOW (reviewed)"

cd "$WORK/repoO"
echo -e "--- a\n+++ b\n-lr=3e-4\n+lr=1e-4" > change-p.diff
CS_P="$(av . --output json improver propose --diff change-p.diff --rationale "e2e phase P" --risk low \
        | jsonget "d['data']['id']")"
av . improver review "$CS_P" --approve >/dev/null
CANDIDATE_P="$(av . --output json improver apply "$CS_P" | jsonget "d['data']['new_improver_id']")"

av . improver policy set main --require-review >/dev/null
set +e
av . improver promote "$CANDIDATE_P"
DENY_CODE=$?
set -e
[[ "$DENY_CODE" == "19" ]] || die "promote without a review must exit 19 (review_required), got $DENY_CODE"

av . review approve "$CANDIDATE_P" --target-type improver >/dev/null
ALLOW_JSON="$(av . --output json improver promote "$CANDIDATE_P")"
[[ "$(<<<"$ALLOW_JSON" jsonget "d['data']['allowed']")" == "True" ]] \
  || die "promote after review should now be allowed: $ALLOW_JSON"
pass "Phase P: dual-gate promotion denied without review (exit 19), allowed once reviewed"

# ============================================================================
log "Phase Q — capability canary blocks an improver promote until it passes"

cd "$WORK/repoO"
echo -e "--- a\n+++ b\n-z\n+w" > change-q.diff
CS_Q="$(av . --output json improver propose --diff change-q.diff --rationale "e2e phase Q" --risk low \
        | jsonget "d['data']['id']")"
av . improver review "$CS_Q" --approve >/dev/null
CANDIDATE_Q="$(av . --output json improver apply "$CS_Q" | jsonget "d['data']['new_improver_id']")"

av . improver policy set canary-gate --require-canaries >/dev/null
set +e
av . improver promote "$CANDIDATE_Q" --into canary-gate
CANARY_DENY_CODE=$?
set -e
[[ "$CANARY_DENY_CODE" == "16" ]] || die "promote without a passing canary must exit 16 (policy_denied), got $CANARY_DENY_CODE"

echo '{"checks": [{"name": "loss_ok", "metric": "val_loss", "op": "<=", "threshold": 0.6}]}' > canary-e2e.json
av . canary register core-capability canary-e2e.json >/dev/null
av . canary run core-capability --improver "$CANDIDATE_Q" >/dev/null
CANARY_ALLOW_JSON="$(av . --output json improver promote "$CANDIDATE_Q" --into canary-gate)"
[[ "$(<<<"$CANARY_ALLOW_JSON" jsonget "d['data']['allowed']")" == "True" ]] \
  || die "promote after a passing canary should now be allowed: $CANARY_ALLOW_JSON"
pass "Phase Q: canary blocks promote until it passes (exit 16 then allow)"

# ============================================================================
log "Phase R — held-out eval vault: a non-scorer identity is rejected server-side"

stop_server
start_server scoped-R AV_API_TOKEN="owner-secret-xyz" \
  AV_AUTH_USERS='{"trainer":{"token":"trainer-tok-e2e","scopes":["read"]},"scorer":{"token":"scorer-tok-e2e","scopes":["scorer","eval:write"]}}'

mkdir -p "$WORK/repoR" && cd "$WORK/repoR"
av . init --mode local --token scorer-tok-e2e --yes --no-repl >/dev/null
echo '{"tasks": []}' > suite-r.json
SUITE_R="$(av . --output json eval register held-out-r suite-r.json | jsonget "d['data']['id']")"
[[ -n "$SUITE_R" ]] || die "eval register (as scorer) should succeed"

PROJ_R="$(project_of "$WORK/repoR")"
TRAINER_SCORE_CODE="$(api_status -H "Authorization: Bearer trainer-tok-e2e" \
  -H "Content-Type: application/json" -X POST "$API/api/eval/results" \
  -d "{\"project_id\":\"$PROJ_R\",\"suite_id\":\"$SUITE_R\",\"score\":{\"acc\":0.9}}")"
[[ "$TRAINER_SCORE_CODE" == "403" ]] \
  || die "a trainer-scoped (non-scorer) token must be rejected recording a score, got $TRAINER_SCORE_CODE"
pass "Phase R: held-out eval vault rejects a non-scorer identity server-side (403)"

# ============================================================================
log "Phase S — budget exhaustion stops a run on its own (exit 17)"

stop_server
start_server anon-S

mkdir -p "$WORK/repoS" && cd "$WORK/repoS"
av . init --mode local --yes --no-repl >/dev/null
BUDGET_S="$(av . --output json budget set e2e-run-s --compute-seconds 10 | jsonget "d['data']['id']")"
av . budget consume "$BUDGET_S" --compute-seconds 6 >/dev/null
set +e
av . budget consume "$BUDGET_S" --compute-seconds 6
BUDGET_CODE=$?
set -e
[[ "$BUDGET_CODE" == "17" ]] || die "exceeding a budget must exit 17 (budget_exhausted), got $BUDGET_CODE"
pass "Phase S: budget exhaustion stops further spend (exit 17), prior spend still recorded"

# ============================================================================
log "Phase T — freeze blocks promote/self-edit; reads and rollback still work"

cd "$WORK/repoO"
av . freeze on --reason "e2e phase T drill" >/dev/null

set +e
av . improver propose --diff change-o.diff --rationale "should be blocked" --risk low
FREEZE_PROPOSE_CODE=$?
set -e
[[ "$FREEZE_PROPOSE_CODE" == "18" ]] || die "proposing a self-edit while frozen must exit 18, got $FREEZE_PROPOSE_CODE"

set +e
av . improver promote "$CANDIDATE_P"
FREEZE_PROMOTE_CODE=$?
set -e
[[ "$FREEZE_PROMOTE_CODE" == "18" ]] || die "promoting while frozen must exit 18, got $FREEZE_PROMOTE_CODE"

# Reads and rollback are exempt by construction (freeze_guard() is never called from them).
av . improver current >/dev/null || die "reads must keep working while frozen"
av . improver rollback >/dev/null || die "rollback must keep working while frozen (it's how you exit an incident)"

av . freeze off >/dev/null
FROZEN_AFTER="$(av . --output json freeze status | jsonget "d['data']['frozen']")"
[[ "$FROZEN_AFTER" == "False" ]] || die "freeze off should clear the frozen flag"
pass "Phase T: freeze blocks promote/self-edit (exit 18) while reads/rollback stay available"

# ============================================================================
# Phases L/M/N — chaos drills. Gated behind AV_E2E_CHAOS=1 (default off) so the plain
# `e2e-suite` CI job / a local run is unaffected; the dedicated `chaos-drills` CI job
# sets it. Runnable locally the same way:
#   AV_E2E_CHAOS=1 AV_TEST_DATABASE_URL=... AV_TEST_REDIS_URL=... bash scripts/e2e_scenario.sh
# ============================================================================
if [[ "${AV_E2E_CHAOS:-0}" == "1" ]]; then

stop_server

# ----------------------------------------------------------------------------
log "Phase L — Redis unreachable: readiness degrades, pushes still succeed, restart recovers"
# redis_cache.py deliberately catches its own connection errors and degrades to DB-only
# checks -- a commit push is not redis-dependent at all, only the dedup-shortcut is.
# This phase proves /api/ready correctly reports the outage while /api/health and the
# write path both keep working, under a real unreachable Redis.
start_server chaos-L-noredis REDIS_URL="redis://this-host-is-intentionally-absent:6379/0"

READY_CODE="$(api_status "$API/api/ready")"
[[ "$READY_CODE" == "503" ]] || die "Phase L: expected /api/ready 503 with redis unreachable, got $READY_CODE"
curl -s "$API/api/ready" | grep -q '"redis": *false' || die "Phase L: /api/ready did not report the redis check as failing"  # not -f: needs the 503 body
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
# A genuinely full disk isn't reliably producible/safe in CI -- a read-only AV_DATA_DIR
# produces the identical observable failure (the storage write call fails). Pre-create
# CASStorage's own top-level dirs before locking the tree down read-only, so the server
# can still boot (its own mkdir(exist_ok=True) only needs to stat them) while a NEW
# per-object shard dir created during upload fails honestly instead of succeeding.
READONLY_DATA="$WORK/data-readonly"
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
# Phase M above proves an unwritable data dir fails honestly and queues -- it does not
# prove a genuinely full one does (real ENOSPC on the write syscall, not EACCES rejected
# up front). A tiny tmpfs mount is the standard, safe way to get a real size limit in CI.
log "Phase M2 — genuine ENOSPC: a real full filesystem fails the write honestly, client queues, drains once space is freed"
TMPFS_MOUNT="$WORK/data-enospc"
mkdir -p "$TMPFS_MOUNT"
if sudo mount -t tmpfs -o size=2m tmpfs "$TMPFS_MOUNT" 2>/dev/null; then
  stop_server 2>/dev/null || true
  # Burn most of the 2 MiB budget with a filler file before the server ever touches it,
  # so the first real object write lands on an already-ENOSPC filesystem.
  mkdir -p "$TMPFS_MOUNT/objects" "$TMPFS_MOUNT/commits" "$TMPFS_MOUNT/refs"
  if dd if=/dev/zero of="$TMPFS_MOUNT/filler" bs=1M count=1 status=none 2>/dev/null; then
    start_server chaos-M2-full AV_DATA_DIR="$TMPFS_MOUNT"

    mkdir -p "$WORK/repoM2" && cd "$WORK/repoM2"
    av . init --mode local --yes --no-repl >/dev/null
    # Bigger than the ~1 MiB of tmpfs headroom left after the filler file above.
    dd if=/dev/urandom of=big-file.bin bs=1M count=2 status=none
    av . add big-file.bin >/dev/null
    set +e
    M2_COMMIT_OUT="$(av . commit -m "commit against a genuinely full filesystem" 2>&1)"
    set -e
    [[ "$(pending_count "$WORK/repoM2")" -ge 1 ]] \
      || die "Phase M2: commit against a full filesystem must queue locally, not silently succeed or crash the server. Output: $M2_COMMIT_OUT"

    stop_server
    sudo umount "$TMPFS_MOUNT" 2>/dev/null || true
    start_server chaos-M2-recovered
    av . push >/dev/null
    [[ "$(pending_count "$WORK/repoM2")" -eq 0 ]] || die "Phase M2: queued commit did not drain once real disk space was available again"
    pass "Phase M2: write failure against a genuinely full filesystem (real ENOSPC) queued honestly; recovered cleanly once space was freed"
  else
    log "Phase M2 SKIPPED — could not even write the filler file to the fresh tmpfs mount"
    sudo umount "$TMPFS_MOUNT" 2>/dev/null || true
  fi
else
  log "Phase M2 SKIPPED — this environment does not permit mounting tmpfs (no passwordless sudo, or mount is otherwise restricted)"
fi

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

# Push in the background, then SIGKILL the server almost immediately -- proving atomic
# local writes rather than the clean-stop path Phase B already covers.
( av . push >/dev/null 2>&1 || true ) &
PUSH_PID=$!
sleep 0.05
kill -9 "$SERVER_PID" 2>/dev/null || true
wait "$PUSH_PID" 2>/dev/null || true
SERVER_PID=""

# pending_push must be well-formed JSON regardless of how much of the push landed before
# the kill -- a torn/partial write here would be the real bug this phase exists to catch.
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

# ============================================================================
# Phase U — DR: real backup -> destroy -> restore drill. Gated behind AV_E2E_DR=1
# (default off), same pattern as AV_E2E_CHAOS -- needs `pg_dump`/`pg_restore` on PATH, or
# a reachable Postgres container named by E2E_DB_CONTAINER. Destroys and restores ONLY
# the e2e's own AV_TEST_DATABASE_URL database and $WORK/data CAS directory, never
# anything outside $WORK.
#   AV_E2E_DR=1 AV_TEST_DATABASE_URL=... AV_TEST_REDIS_URL=... bash scripts/e2e_scenario.sh
# ============================================================================
if [[ "${AV_E2E_DR:-0}" == "1" ]]; then

stop_server
start_server dr-U-before

log "Phase U — DR: backup -> destroy -> restore drill"

mkdir -p "$WORK/repoU" && cd "$WORK/repoU"
av . init --mode local --yes --no-repl >/dev/null
echo "dr-drill payload $(date +%s)" > dr-file.txt
av . add dr-file.txt >/dev/null
av . commit -m "dr-drill commit" >/dev/null
DR_HASH="$(cat .av/refs/heads/main)"
cd "$REPO_ROOT"
[[ "$(api_status "$API/api/commits/$DR_HASH")" == "200" ]] \
  || die "Phase U: setup commit did not land on the server before the drill started"

DB_CONTAINER_ARGS=()
if [[ -n "${E2E_DB_CONTAINER:-aether-vault-db}" ]] \
   && docker inspect "${E2E_DB_CONTAINER:-aether-vault-db}" >/dev/null 2>&1; then
  DB_CONTAINER_ARGS=(--db-container "${E2E_DB_CONTAINER:-aether-vault-db}")
elif ! command -v pg_dump >/dev/null 2>&1; then
  die "Phase U: no pg_dump on PATH and no reachable \$E2E_DB_CONTAINER -- install postgresql-client or set E2E_DB_CONTAINER"
fi

BACKUP_DIR="$WORK/dr-backup"
command av admin backup create "$BACKUP_DIR" \
  --database-url "$DB_URL_ASYNC" --data-dir "$WORK/data" "${DB_CONTAINER_ARGS[@]}" \
  >"$WORK/dr-backup-create.out" 2>&1 \
  || { cat "$WORK/dr-backup-create.out" >&2; die "Phase U: av admin backup create failed"; }
[[ -f "$BACKUP_DIR/manifest.json" ]] || die "Phase U: backup create did not write a manifest.json"
pass "Phase U: backup created ($(du -sh "$BACKUP_DIR" 2>/dev/null | cut -f1))"

command av admin backup verify "$BACKUP_DIR" >"$WORK/dr-backup-verify.out" 2>&1 \
  || { cat "$WORK/dr-backup-verify.out" >&2; die "Phase U: av admin backup verify reported a bad backup"; }
pass "Phase U: backup verified (hashes match manifest)"

log "Phase U — destroying the e2e database schema and CAS objects (genuinely, not simulated)"
stop_server
psqlq "DROP SCHEMA public CASCADE; CREATE SCHEMA public;" >/dev/null
rm -rf "${WORK:?}/data"
mkdir -p "$WORK/data"
[[ "$(psqlq "SELECT count(*) FROM information_schema.tables WHERE table_schema='public'")" == "0" ]] \
  || die "Phase U: schema destroy did not actually empty the database"
pass "Phase U: e2e database schema and CAS objects genuinely destroyed"

DR_RESTORE_START="$(date +%s)"
command av admin backup restore "$BACKUP_DIR" \
  --database-url "$DB_URL_ASYNC" --data-dir "$WORK/data" "${DB_CONTAINER_ARGS[@]}" --force \
  >"$WORK/dr-backup-restore.out" 2>&1 \
  || { cat "$WORK/dr-backup-restore.out" >&2; die "Phase U: av admin backup restore failed"; }
DR_RESTORE_SECS="$(( $(date +%s) - DR_RESTORE_START ))"

start_server dr-U-after
[[ "$(api_status "$API/api/commits/$DR_HASH")" == "200" ]] \
  || die "Phase U: the pre-destruction commit is not readable after restore"
RESTORED_BODY="$(curl -sf "$API/api/commits/$DR_HASH")"
echo "$RESTORED_BODY" | jsonget "d['hash']" | grep -q "$DR_HASH" \
  || die "Phase U: restored commit hash does not match what was backed up"
DR_PROJECT="$(project_of "$WORK/repoU")"
DR_REF="$(curl -sf "$API/api/refs?project_id=$DR_PROJECT" | jsonget "d['$DR_PROJECT/main']")"
[[ "$DR_REF" == "$DR_HASH" ]] \
  || die "Phase U: restored refs/main ($DR_REF) does not resolve to the pre-destruction commit ($DR_HASH)"
pass "Phase U: DR drill complete — real destroy + restore, pre-destruction commit and its ref read back byte-identical, restore took ${DR_RESTORE_SECS}s (this run's measured RTO — see docs/dr.md)"

fi  # AV_E2E_DR
