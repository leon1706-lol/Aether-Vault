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
# `host.docker.internal` (phase 3's webhook probe target) is resolvable from every
# engine container on any Docker Engine (Linux CI runners included, not just Docker
# Desktop) via `docker-compose.ha.yml`'s own `extra_hosts: host.docker.internal:
# host-gateway` on the engine service -- v1.3.3.6 fix, found live: this comment used to
# claim Linux "would need" that mapping without it actually being present, which is
# exactly why phase 3 silently delivered zero webhooks in CI (ubuntu-latest) every time.
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
  # v1.3.4 (W0.5): $WORK (the mktemp -d dir holding every phase's error/code logs) was
  # never removed — every drill run, local or CI, leaked one /tmp/av-ha-drill-XXXXXX
  # directory forever. Harmless on a CI runner that's destroyed after the job, but a real
  # leak on a long-lived machine running this locally.
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
  # v1.3.3.7 fix (found live): no timeout on this curl meant a single request that
  # connects but never responds (a real hang, not a clean refuse-fast failure -- e.g.
  # the LB proxying to a container whose IP it has stale-cached) could block this WHOLE
  # loop indefinitely on ONE of its 60 tries, rather than cycling through retries and
  # failing cleanly via the `return 1` below. `--connect-timeout 5 --max-time 10` bounds
  # every single attempt; `timeout -k 5 12` is a second, OS-level backstop outside
  # curl's own process. Progress is logged every 10 tries (~24s) so a genuine hang here
  # leaves a visible trail instead of silence.
  #
  # v1.3.3.7-3.3.10 investigated a suspected hang in THIS function via `set -x` tracing
  # (since removed) -- the trace proved wait_ready was never actually the problem; it
  # always returned within one or two tries. The real bug, found via the SAME kind of
  # tracing applied further down the script, was an unrelated unscoped `wait` in phase
  # 4's rate-limit probe block (see v1.3.3.13 there). Left here as a pointer in case this
  # function is ever blamed again: it wasn't, twice, with hard evidence both times.
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
  # v1.3.3.5 fix (found live, right after the hash fix above actually let uploads
  # succeed): `curl -f`/`--fail` treats ANY response >= 400 as a failure, 409 included --
  # but 409 ("already exists") is this repo's own documented, CORRECT idempotent outcome
  # for a CAS re-upload of identical content, exactly like the commit call two lines
  # below already tolerates. This function's own SECOND (sequential) call site below
  # deliberately re-pushes the SAME 20 objects the first (concurrent) pass just
  # uploaded, specifically to prove a retry of already-landed work is harmless -- with
  # `-f`, that retry pass turned "already landed, harmless" into 20 hard failures on
  # every single run, every time, unconditionally (not a race, not a flake).
  # v1.3.3.13 (found live, code review): unlike wait_ready and phase 4's rate-limit
  # probes, these two calls -- by far the most-invoked curl site in the whole drill (70+
  # calls across phases 1-3) -- had never been given any timeout protection at all.
  # `--connect-timeout 5 --max-time 10` matches every other curl call site now.
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

# v1.3.3.13 (found live, code review): the CONCURRENT pass below is the thing this
# phase's own name and pass-message actually claim to test -- but its results used to
# be thrown away entirely (`wait || true` discards every job's exit status), so a real
# failure in the concurrent run would print to stderr and nothing else: no `FAILS`
# increment, no `die`, `pass` still fires unconditionally. Only the SECOND, sequential
# pass below (re-pushing the same 20 objects, expected to get harmless 409s) was ever
# actually checked -- a materially easier bar to clear than genuine concurrency. Fixed
# to match phase 2's own already-correct pattern just below: redirect the concurrent
# batch's output to a log file and check it's empty before declaring success.
# v1.3.4 (W0.4): this was still a bare, unscoped `wait` — the exact same class of bug
# root-caused in phase 4 below (v1.3.3.13). It happened to never hang HERE because at
# this point in the script nothing long-lived has been backgrounded yet (webhook_probe.py
# only starts in phase 3) -- but that's incidental, not a guarantee, and it discarded
# every job's own exit status on top (a push killed by a signal rather than printing to
# stderr would pass silently). Scoped to exactly the PIDs this loop launched, matching
# phase 4's own fix and phase 2's pattern below.
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

# v1.3.4 (W0.5): `wait "$PUSH_BATCH_PID" || true` discarded the subshell's own exit
# status (itself just the LAST job in its internal bare `wait`, not an aggregate anyway)
# -- the log-emptiness check below was already the real assertion; this just stops
# silently swallowing a genuine subshell-level failure (e.g. it never reaching its own
# `wait` at all) on top of it.
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
# engine containers with it, leaving Postgres/Redis (and their state — the replication/
# webhook proof phases 0b/3 already established) completely undisturbed.
export AV_RATE_LIMIT_DEFAULT="6/minute"
$COMPOSE up -d
# v1.3.3.7 fix (found live: the job hung 25+ minutes here and hit CI's own 30-minute
# job timeout, not a clean FAIL — confirmed via the actual log timestamps, a 25-minute
# gap with zero script output between "engine-1/2 Recreated" and the timeout kill).
# `docker compose up -d` RECREATES engine-1/engine-2 (a genuinely new container, not
# the same one restarted -- unlike phase 2's `docker kill`+`docker start`, which reuses
# the SAME container and therefore the SAME IP) -- Docker assigns a NEW IP to a
# recreated container. nginx's `upstream` directive (docker/ha/nginx/nginx.conf)
# resolves `engine-1`/`engine-2` to an IP ONCE, at its own worker startup, with no
# dynamic re-resolution configured -- so the `lb` container kept trying the OLD, now
# nonexistent IPs for the rest of the run, and every request past this point either hung
# or failed depending on exactly how Docker's bridge network handled the stale address.
# Restarting `lb` forces nginx to re-resolve both upstream hostnames fresh, immediately
# after the one operation in this whole drill that actually changes their IPs.
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
# v1.3.3.13 fix (found live, ROOT CAUSE): every prior fix here (v1.3.3.7-12: curl
# timeouts, `timeout -k`, `set -x` tracing, a bounded poll with `kill -9`) was chasing
# an innocent target -- a heartbeat trace finally proved all 20 of these curls exit
# correctly within 2 SECONDS, every single time. The real bug was a bare `wait` (no
# arguments) right here, present since before this investigation started: bash's
# argument-less `wait` blocks for EVERY background job the shell has ever started, not
# just the ones this block cares about -- and phase 3 started `webhook_probe.py` as a
# deliberately long-lived listener (`PROBE_PID`), only meant to be killed later in
# `cleanup()`'s EXIT trap. That job is still running at this point BY DESIGN, so the
# bare `wait` was blocking on a process that was never supposed to exit yet, for as
# long as the surrounding job/step timeout allowed. Scoping `wait` to exactly the PIDs
# this block launched fixes it outright, with no diagnostic machinery needed -- each
# already has its own `timeout -k 5 12` bound, so this cannot reintroduce the hang.
wait "${_rl_pids[@]}" 2>/dev/null || true

OK_COUNT="$(grep -c '^200$' "$CODES" || true)"
LIMITED_COUNT="$(grep -c '^429$' "$CODES" || true)"
log "of 20 rapid /api/refs requests through the LB: $OK_COUNT succeeded, $LIMITED_COUNT hit 429"
[[ "$LIMITED_COUNT" -gt 0 ]] || die "expected at least one 429 out of 20 requests against a 6/minute global limit — Redis-backed limiting did not engage (or each replica is enforcing its own independent 6/minute, allowing up to 12 through)"
[[ "$OK_COUNT" -le 6 ]] || die "expected at most 6 successes against a 6/minute GLOBAL limit, got $OK_COUNT — replicas are enforcing independent limits (the exact N-replica bug WP-24 exists to fix), not a shared one"
pass "rate limit enforced globally across both replicas ($OK_COUNT allowed, capped at the configured 6/minute — not 2x)"

# v1.3.4 (W0.5): phase 4 is the last phase to mutate the stack, and it deliberately
# applies a restrictive AV_RATE_LIMIT_DEFAULT=6/minute. Under KEEP_HA_STACK=1 (this job's
# own default) the drill used to just exit here, leaving anyone who then pokes at the kept
# stack silently rate-limited to 6/minute with no indication why. Recreate the engines one
# more time with the env var unset so "kept for inspection" means the stack's NORMAL
# (unlimited-by-default) state, not phase 4's fault-injected one.
unset AV_RATE_LIMIT_DEFAULT
log "resetting engines to their normal (non-rate-limited) config before finishing"
$COMPOSE up -d
$COMPOSE restart lb
wait_ready "$API/api/ready" || die "engines never became ready again after resetting AV_RATE_LIMIT_DEFAULT"

# ---------------------------------------------------------------------------
log "ALL HA DRILL PHASES PASSED"
