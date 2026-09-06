# RSI reference loop

`agent.py` is a deterministic, scripted reference agent that drives the full RSI
control-plane loop end to end through `av_sdk.Repo` alone — no LLM key, no hidden state.
It is the concrete answer to "close the loop end-to-end, in-repo, with a reference agent
that actually runs it": it touches every write-capable RSI surface `av_sdk.Repo` exposes
(see
`development/architecture.md`'s "RSI SDK Surface Contract" section for the full list and
what's deliberately left off the SDK).

## What it does

1. Registers a baseline improver version.
2. Proposes a self-edit (a diff + rationale + predicted risk).
3. Gets the change set approved and applies it — minting the next improver version.
4. Executes the change for real inside a local sandbox.
5. Runs a capability canary against the new candidate.
6. Arms the improver-promotion gate to require a reviewer's sign-off.
7. Requests promotion — **denied** (`review_required`, exit 19).
8. Gets the candidate reviewed.
9. Requests promotion again — **allowed**.
10. Posts a claim to the shared blackboard, records a causal link and a strategy-memory
    entry, and distills a lesson from what was learned.
11. Sets a tiny compute budget and drives it to exhaustion — **stops itself**
    (`budget_exhausted`, exit 17) rather than needing an external kill switch.
12. Confirms the causal graph is queryable via cross-run search.

Every denial above is a real, typed `SDKError` the script catches and reports — nothing
is faked or narrated; if the gate doesn't actually deny, the script says so loudly
(`UNEXPECTEDLY ALLOWED` / `UNEXPECTEDLY NOT EXHAUSTED`) instead of silently passing.

## Running it for real

Every RSI surface here is server-authoritative by design (no offline queue for improver
versions, change sets, canary results, budgets, etc. — see each surface's own
`architecture.md` section for why). You need a real, reachable aether-vault registry:

```bash
docker compose up -d db redis aether-vault-engine   # or your own deployment
av init --mode remote --remote-url http://localhost:8000 --yes /path/to/demo-repo
python examples/rsi_loop/agent.py /path/to/demo-repo
```

A real deployment would also register a `core-capability` canary suite
(`av canary register core-capability suite.json`) before step 5 runs — the reference
loop assumes one already exists, matching how a real team would set this up once per
project rather than re-registering it inside every scripted run.

In a real multi-agent deployment, step 8 (the reviewer approval) is submitted by a
**second** identity/token — a self-review is rejected server-side (422). This script runs
single-identity for simplicity; the self-review rejection itself is proven directly
against the live server in `tests/test_review.py` and `tests/test_server.py`.

## Running it stack-free (CI, no Docker)

`tests/test_rsi_loop.py` calls `run_rsi_loop()` — the same function `__main__` calls
above — against an in-memory fake registry, proving the narrative's logic (every step
happens in the right order, every denial is the right one) without any live
infrastructure. This is the test that actually runs in CI; the live run above is the
real-infrastructure counterpart.
