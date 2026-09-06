#!/usr/bin/env bash
# ============================================================================
# HA drill — the actual proof docker-compose.ha.yml is more than YAML.
#
# Brings up the real HA topology (nginx LB + 2 engine replicas + Postgres primary/
# streaming-replica + Redis primary/replica), drives concurrent pushes through the LB,
# kills one engine replica mid-run, and asserts:
#   1. Zero failed pushes across the whole run (the LB's passive failover covers the kill).
#   2. Zero double webhook delivery — the SKIP LOCKED fix (server.py::
#      process_due_webhook_deliveries) actually prevents both replicas' retry-worker
#      loops from re-delivering the same due row.
#   3. The Redis-backed rate limiter enforces ONE global limit across both replicas, not
#      2x (what the in-process WindowRateLimiter would silently do under this topology).
#
# `host.docker.internal` (phase 3's webhook probe target) is resolvable from every
# engine container via `docker-compose.ha.yml`'s own `extra_hosts:
# host.docker.internal: host-gateway` on the engine service.
#
# Usage: ./scripts/ha_drill.sh            # full drill, tears the stack down after
#        KEEP_HA_STACK=1 ./scripts/ha_drill.sh   # leaves it up for manual poking
# ============================================================================
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

COMPOSE="docker compose -f docker-compose.ha.yml -p aether-vault-ha"
API="http://localhost:8000"
PROBE_PORT=8099
WORK="$(mktemp -d /tmp/av-ha-drill-XXXXXX)"
command -v cygpath >/dev/null 2>&1 && WORK="$(cygpath -m "$WORK")"
PROBE_LOG="$WORK/webhook_hits.log"
PROBE_PID=""

log()  { printf '\n\033[1;36m[ha-drill]\033[0m %s\n' "$*"; }
pass() { printf '\033[1;32m[PASS]\033[0m %s\n' "$*"; }
die()  { printf '\033[1;31m[FAIL]\033[0m %s\n' "$*" >&2; exit 1; }

cleanup() {
  [[ -n "$PROBE_PID" ]] && kill "$PROBE_PID" >/dev/null 2>&1 || true
  if [[ "${KEEP_HA_STACK:-0}" != "1" ]]; then
    log "tearing down HA stack"
    $COMPOSE down -v >/dev/null 2>&1 || true
  else
    log "KEEP_HA_STACK=1 — leaving the stack up for inspection (docker compose -f docker-compose.ha.yml -p aether-vault-ha down -v to clean up later)"
  fi
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

wait_ready() {
  # No timeout on this curl would let a single hung request (e.g. the LB proxying to a
  # stale-cached container IP) block the WHOLE loop on one of its 60 tries instead of
  # cycling to the `return 1` below; `--connect-timeout`/`--max-time` bound each attempt,
  # `timeout -k` is a second OS-level backstop. (Investigated and cleared of blame twice
  # for an unrelated hang that was actually phase 4's rate-limit probe block below.)
  local url="$1" tries=60 start_tries=60
  while (( tries-- > 0 )); do
    if timeout -k 5 12 curl -sf --connect-timeout 5 --max-time 10 -o /dev/null "$url"; then
      return 0
    fi
    if (( tries % 10 == 0 )); then
      log "  ...still waiting on $url ($((start_tries - tries)) attempts so far)"
    fi
    sleep 2
  done
  return 1
}

# ---------------------------------------------------------------------------
log "phase 0: bring up the HA stack (build if needed)"
touch "$PROBE_LOG"

# AV_RATE_LIMIT_DEFAULT must NOT be exported here: phases 1-3's traffic shares the same
# "default" rate-limit bucket as phase 4's probe route, so applying the limit this early
# would fail every baseline push before phase 4 ever runs. It's applied only right before
# phase 4 below, once phases 0-3 are well past their own 60s window.
$COMPOSE up -d --build

log "waiting for the LB + both engine replicas to report ready"
wait_ready "$API/api/ready" || die "HA stack never became ready"
pass "stack is up and ready"

log "phase 0b: confirm the Postgres replica actually caught up (real streaming replication, not just a container that started)"
tries=30
until [[ "$($COMPOSE exec -T db-replica psql -U av_user -d aether_vault -tAc "SELECT pg_is_in_recovery()" 2>/dev/null)" == "t" ]]; do
  (( tries-- > 0 )) || die "db-replica never entered recovery mode (streaming replication did not start)"
  sleep 2
done
pass "db-replica is a live streaming standby (pg_is_in_recovery() = true)"

# ---------------------------------------------------------------------------
log "phase 1: baseline concurrent pushes through the LB (no faults injected)"

push_object_and_commit() {
  local n="$1"
  # The hash must be computed over the EXACT bytes sent as the body ("ha-drill-object-$n"),
  # not the commit message string -- a mismatch here fails every upload with a real,
  # correct 400 (server-side hash verification working as designed).
  local body="ha-drill-object-$n"
  local hash
  hash="$($PY -c "import hashlib,sys; print(hashlib.sha256(sys.argv[1].encode()).hexdigest())" "$body")"
  # NOT curl -f: 409 ("already exists") is the correct idempotent outcome for a CAS
  # re-upload of identical content, which this function's sequential re-push call site
  # deliberately exercises -- -f would turn that harmless retry into a hard failure.
  local obj_code
  obj_code="$(curl -s --connect-timeout 5 --max-time 10 -o /dev/null -w '%{http_code}' -X POST "$API/api/objects/$hash" \
    -H "Content-Type: application/octet-stream" --data-binary "$body")"
  [[ "$obj_code" == "201" || "$obj_code" == "409" ]] || { echo "push $n object upload got HTTP $obj_code" >&2; return 1; }
  local code
  code="$(curl -s --connect-timeout 5 --max-time 10 -o /dev/null -w '%{http_code}' -X POST "$API/api/commits" \
    -H "Content-Type: application/json" \
    -d "{\"hash\": \"$hash\", \"message\": \"ha-drill-$n\", \"root_tree_hash\": \"$hash\", \"project_id\": \"ha-drill\", \"project_name\": \"ha-drill\"}")"
  [[ "$code" == "201" || "$code" == "409" ]] || { echo "push $n commit got HTTP $code" >&2; return 1; }
}
export -f push_object_and_commit
export API PY

# The concurrent pass's own exit status is checked explicitly (not a bare `wait`, which
# would block on any other long-lived background job and silently discard a signal-killed
# push): output goes to a log file, checked empty, and each launched PID is waited on by name.
BASELINE_ERRORS="$WORK/baseline_errors.log"
: > "$BASELINE_ERRORS"
declare -a _baseline_pids=()
for i in $(seq 1 20); do
  push_object_and_commit "baseline-$i" >>"$BASELINE_ERRORS" 2>&1 &
  _baseline_pids+=("$!")
done
BASELINE_FAILS=0
for pid in "${_baseline_pids[@]}"; do
  wait "$pid" || BASELINE_FAILS=$((BASELINE_FAILS + 1))
done
if [[ "$BASELINE_FAILS" -ne 0 || -s "$BASELINE_ERRORS" ]]; then
  cat "$BASELINE_ERRORS" >&2
  die "one or more concurrent baseline pushes failed with no fault injected ($BASELINE_FAILS non-zero exit(s))"
fi

FAILS=0
for i in $(seq 1 20); do push_object_and_commit "baseline-$i" >/dev/null 2>&1 || FAILS=$((FAILS+1)); done
[[ "$FAILS" -eq 0 ]] || die "baseline pushes: $FAILS unexpected failures on the idempotent-retry pass"
pass "20 concurrent pushes through the LB with 2 healthy replicas: zero failures"

# ---------------------------------------------------------------------------
log "phase 2: kill one engine replica mid-push, prove the LB fails over cleanly"

(
  for i in $(seq 1 30); do
    push_object_and_commit "failover-$i" >>"$WORK/failover_errors.log" 2>&1 &
  done
  wait
) &
PUSH_BATCH_PID=$!

sleep 1
log "docker kill aether-vault-ha-engine-2 (mid-batch)"
docker kill aether-vault-ha-engine-2 >/dev/null

wait "$PUSH_BATCH_PID" || log "push batch subshell exited non-zero (checking its error log next)"
if [[ -s "$WORK/failover_errors.log" ]]; then
  cat "$WORK/failover_errors.log" >&2
  die "one or more pushes failed while engine-2 was down — LB failover did not cover them"
fi
pass "30 concurrent pushes survived a mid-batch replica kill: zero failures"

log "bringing engine-2 back and waiting for it to rejoin healthy"
docker start aether-vault-ha-engine-2 >/dev/null
tries=30
until [[ "$(docker inspect -f '{{.State.Health.Status}}' aether-vault-ha-engine-2 2>/dev/null)" == "healthy" ]]; do
  (( tries-- > 0 )) || die "engine-2 never became healthy again after restart"
  sleep 5
done
pass "engine-2 rejoined the pool healthy"

# ---------------------------------------------------------------------------
log "phase 3: webhook double-delivery proof (the SKIP LOCKED fix, WP-24)"

"$PY" "$REPO_ROOT/docker/ha/webhook_probe.py" --port "$PROBE_PORT" --fail-count 2 --log "$PROBE_LOG" &
PROBE_PID=$!
sleep 1

HOOK_RESP="$(curl -sf -X POST "$API/api/webhooks" -H "Content-Type: application/json" \
  -d "{\"url\": \"http://host.docker.internal:$PROBE_PORT/hook\", \"secret\": \"ha-drill-secret\", \"kinds\": [\"commit\"]}")"
echo "$HOOK_RESP"

# Trigger the event that fires the webhook — a fresh commit under a project this hook
# is not scoped to still fires (kinds=["commit"], project_id=None means "all projects").
push_object_and_commit "webhook-trigger" || die "failed to push the webhook-triggering commit"

log "waiting for the initial delivery attempt (fails by design) + up to 2 periodic retries to land"
# AV_WEBHOOK_RETRY_INTERVAL_SECS is the compose's default (30s); allow margin for 2
# retries plus their own exponential backoff.
tries=40
while (( tries-- > 0 )); do
  hits="$(wc -l < "$PROBE_LOG" | tr -d ' ')"
  [[ "$hits" -ge 3 ]] && break
  sleep 5
done

hits="$(wc -l < "$PROBE_LOG" | tr -d ' ')"
log "webhook probe recorded $hits total incoming requests"
cat "$PROBE_LOG"
[[ "$hits" -ge 3 ]] || die "expected at least 3 delivery attempts (1 immediate + 2 retries), got $hits — retry worker may not be running on either replica"
[[ "$hits" -le 3 ]] || die "expected EXACTLY 3 delivery attempts, got $hits — both replicas' retry workers double-delivered the same due row (SKIP LOCKED regression)"
pass "exactly 3 webhook deliveries across 2 replicas' retry workers — no double delivery"

# ---------------------------------------------------------------------------
log "phase 4: Redis-backed rate limiter enforces ONE global limit across both replicas"

# Applied here, not from phase 0, since phases 1-3's legitimate traffic must never share
# this budget. `docker compose up -d` recreates engine-1/engine-2 with new IPs, but
# nginx's `upstream` directive resolves hostnames once at its own worker startup with no
# dynamic re-resolution -- restarting `lb` forces it to re-resolve fresh.
export AV_RATE_LIMIT_DEFAULT="6/minute"
$COMPOSE up -d
$COMPOSE restart lb
log "waiting for both engine replicas to report ready again after the rate-limit env change"
wait_ready "$API/api/ready" || die "engines never became ready again after applying AV_RATE_LIMIT_DEFAULT"

CODES="$WORK/ratelimit_codes.txt"
: > "$CODES"
declare -a _rl_pids=()
for i in $(seq 1 20); do
  timeout -k 5 12 curl -s --connect-timeout 5 --max-time 10 -o /dev/null -w '%{http_code}\n' "$API/api/refs" >> "$CODES" &
  _rl_pids+=("$!")
done
# A bare, argument-less `wait` here would block on EVERY background job the shell has
# ever started, including phase 3's deliberately long-lived webhook_probe.py listener --
# scoped to exactly the PIDs this block launched instead.
wait "${_rl_pids[@]}" 2>/dev/null || true

OK_COUNT="$(grep -c '^200$' "$CODES" || true)"
LIMITED_COUNT="$(grep -c '^429$' "$CODES" || true)"
log "of 20 rapid /api/refs requests through the LB: $OK_COUNT succeeded, $LIMITED_COUNT hit 429"
[[ "$LIMITED_COUNT" -gt 0 ]] || die "expected at least one 429 out of 20 requests against a 6/minute global limit — Redis-backed limiting did not engage (or each replica is enforcing its own independent 6/minute, allowing up to 12 through)"
[[ "$OK_COUNT" -le 6 ]] || die "expected at most 6 successes against a 6/minute GLOBAL limit, got $OK_COUNT — replicas are enforcing independent limits (the exact N-replica bug WP-24 exists to fix), not a shared one"
pass "rate limit enforced globally across both replicas ($OK_COUNT allowed, capped at the configured 6/minute — not 2x)"

# Phase 4 is the last phase to mutate the stack and deliberately applies a restrictive
# limit -- reset it so "kept for inspection" (KEEP_HA_STACK=1) means the stack's normal,
# unlimited-by-default state, not phase 4's fault-injected one.
unset AV_RATE_LIMIT_DEFAULT
log "resetting engines to their normal (non-rate-limited) config before finishing"
$COMPOSE up -d
$COMPOSE restart lb
wait_ready "$API/api/ready" || die "engines never became ready again after resetting AV_RATE_LIMIT_DEFAULT"

# ---------------------------------------------------------------------------
log "ALL HA DRILL PHASES PASSED"
