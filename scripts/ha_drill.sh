#!/usr/bin/env bash
# ============================================================================
# HA drill (v1.3.2, WP-25) — the actual proof docker-compose.ha.yml is more than YAML.
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
# Requires Docker Desktop with `host.docker.internal` reachable from containers (true by
# default on Windows/Mac; on Linux Engine this compose file would need
# `extra_hosts: ["host.docker.internal:host-gateway"]` added, not needed here).
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
  local url="$1" tries=60
  while (( tries-- > 0 )); do
    if curl -sf -o /dev/null "$url"; then return 0; fi
    sleep 2
  done
  return 1
}

# ---------------------------------------------------------------------------
log "phase 0: bring up the HA stack (build if needed)"
touch "$PROBE_LOG"

# v1.3.3.1 fix (found live): AV_RATE_LIMIT_DEFAULT used to be exported HERE, before phase
# 0 even brings the stack up — meaning it applied to EVERY "default"-bucket route
# (`bucket_class_for()`, rate_limit.py) for the ENTIRE drill, not just phase 4's own
# dedicated rate-limit test below. `/api/objects/{hash}` and `/api/commits` (phases 1-3's
# actual traffic) both fall in the SAME "default" bucket as `/api/refs` (phase 4's probe
# route) and share the SAME client_key (every request arrives from the LB's own
# connecting IP, `request.client.host` server-side) — so phase 1 alone (20 concurrent
# pushes x 2 requests each = 40+ "default"-bucket hits within seconds, all in ONE 60s
# fixed window) blew straight through a 6/minute cap before phase 4 ever ran, failing
# EVERY baseline push with no fault injected. The data plane stays genuinely UNLIMITED
# (this repo's own stated default) through phases 0-3 now; the limit is applied ONLY
# right before phase 4 below, which is temporally well past 60s from here by the time it
# runs (phase 3 alone can wait up to ~200s), so phase 4 starts its own fresh window with
# nothing already counted against it.
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
  # v1.3.3.4 fix (found live): the claimed hash was computed over "ha-drill-$n" while the
  # actual uploaded body is "ha-drill-object-$n" -- two DIFFERENT strings. Every upload
  # this function ever made was rejected with a real, correct 400 (server-side hash
  # verification, threat-model T7's own mitigation working exactly as designed) — this
  # whole drill has apparently never gotten far enough to actually exercise phase 1
  # successfully before now (masked first by the redis-replica compose bug, then by the
  # migration-race bug), so this bug in the drill script itself was never caught either.
  # The hash must be computed over the EXACT bytes sent as the body.
  local body="ha-drill-object-$n"
  local hash
  hash="$($PY -c "import hashlib,sys; print(hashlib.sha256(sys.argv[1].encode()).hexdigest())" "$body")"
  curl -sf -o /dev/null -X POST "$API/api/objects/$hash" \
    -H "Content-Type: application/octet-stream" --data-binary "$body" || return 1
  local code
  code="$(curl -s -o /dev/null -w '%{http_code}' -X POST "$API/api/commits" \
    -H "Content-Type: application/json" \
    -d "{\"hash\": \"$hash\", \"message\": \"ha-drill-$n\", \"root_tree_hash\": \"$hash\", \"project_id\": \"ha-drill\", \"project_name\": \"ha-drill\"}")"
  [[ "$code" == "201" || "$code" == "409" ]] || { echo "push $n got HTTP $code" >&2; return 1; }
}
export -f push_object_and_commit
export API PY

FAILS=0
for i in $(seq 1 20); do push_object_and_commit "baseline-$i" & done
wait || true
for i in $(seq 1 20); do push_object_and_commit "baseline-$i" >/dev/null 2>&1 || FAILS=$((FAILS+1)); done
[[ "$FAILS" -eq 0 ]] || die "baseline pushes: $FAILS unexpected failures with no fault injected"
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

wait "$PUSH_BATCH_PID" || true
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
# AV_WEBHOOK_RETRY_INTERVAL_SECS is the compose's default (30s) — allow up to 3 ticks'
# worth of margin for the 2 retries this needs (attempt 1 immediate, attempts 2-3 via the
# periodic worker) plus its own exponential backoff.
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

# Applied HERE, not from phase 0 — see that section's own comment for why: phases 1-3's
# legitimate traffic must never share this budget. `docker compose up -d` (no --build,
# images are already built) detects the changed environment and recreates ONLY the two
# engine containers with it, leaving the LB/Postgres/Redis containers (and their state —
# the replication/webhook proof phases 0b/3 already established) completely undisturbed.
export AV_RATE_LIMIT_DEFAULT="6/minute"
$COMPOSE up -d
log "waiting for both engine replicas to report ready again after the rate-limit env change"
wait_ready "$API/api/ready" || die "engines never became ready again after applying AV_RATE_LIMIT_DEFAULT"

CODES="$WORK/ratelimit_codes.txt"
: > "$CODES"
for i in $(seq 1 20); do
  curl -s -o /dev/null -w '%{http_code}\n' "$API/api/refs" >> "$CODES" &
done
wait

OK_COUNT="$(grep -c '^200$' "$CODES" || true)"
LIMITED_COUNT="$(grep -c '^429$' "$CODES" || true)"
log "of 20 rapid /api/refs requests through the LB: $OK_COUNT succeeded, $LIMITED_COUNT hit 429"
[[ "$LIMITED_COUNT" -gt 0 ]] || die "expected at least one 429 out of 20 requests against a 6/minute global limit — Redis-backed limiting did not engage (or each replica is enforcing its own independent 6/minute, allowing up to 12 through)"
[[ "$OK_COUNT" -le 6 ]] || die "expected at most 6 successes against a 6/minute GLOBAL limit, got $OK_COUNT — replicas are enforcing independent limits (the exact N-replica bug WP-24 exists to fix), not a shared one"
pass "rate limit enforced globally across both replicas ($OK_COUNT allowed, capped at the configured 6/minute — not 2x)"

# ---------------------------------------------------------------------------
log "ALL HA DRILL PHASES PASSED"
