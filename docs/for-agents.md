# For agents

A minimal, working recipe for driving Aether-Vault from an autonomous loop — plus the
exit-code registry every command and the SDK share. See `docs/contracts.md` for the
published JSON schemas each payload below validates against, and `docs/tutorial.md` for
the full narrative walkthrough (init → train → run → snapshot → promote → handoff → next
agent).

## Two equivalent surfaces

Every operation below works identically as a CLI subprocess call (`av --output json ...`)
or as a direct Python import (`from av_sdk import Repo`). Pick whichever fits your
runtime — a shell-based agent and a Python-native one get the same payload shapes, the
same error codes, and the same single-writer commit path underneath
(`python/av_cli/core.py::commit_staged()`). `tests/test_av_sdk.py`'s parity tests pin
this equivalence for `status`/`add`/`commit`/`push`/`log`/`diff_semantic`/`context_note`/
`handoff_dict` — if a payload ever drifts between the two, that's a test failure there,
not a spec you have to read case-by-case.

## Minimal recipe

**Shell:**

```bash
av init --mode local --yes --no-repl
av add checkpoint.safetensors
av --output json run start my-experiment
av --output json commit -m "epoch 12" --metric val_loss=0.31
av --output json context note "LR 3e-4 diverged at step 9k — keep 1e-4"
av --output json run finish --metric final_loss=0.29
av --output json handoff --publish
```

**Python (`av_sdk`):**

```python
from av_sdk import Repo, SDKError

with Repo("/path/to/repo") as r:
    r.add("checkpoint.safetensors")
    started = r.run_start("my-experiment")   # commits from here auto-tag run:<id>
    try:
        c = r.commit("epoch 12", metrics={"val_loss": 0.31})
    except SDKError as e:
        # e.code is one of the registry below; e.exit_code is the matching CLI exit code
        raise
    r.context_note("LR 3e-4 diverged at step 9k — keep 1e-4")
    r.run_finish(metrics={"final_loss": 0.29})
    r.publish_handoff()   # opt-in — notes can hold private reasoning, nothing publishes
                          # them without this call
```

Both snippets do the same thing: stage a file, start a run, commit under it, leave a
note for the next agent, finish the run, and publish a `.avh` handoff document so
whoever (or whatever) picks this up next inherits intent without re-deriving it.

## Error / exit-code registry

Every failure — CLI exit code, JSON envelope `error.code`, and (v1.3.0+) a matching
`av_sdk.exceptions` subclass — comes from this one table:

| `error.code` | exit code | SDK exception | Meaning |
|---|---|---|---|
| `not_a_repo` | 10 | `NotARepoError` | No `.av/` at the given path |
| `nothing_to_commit` | 11 | `NothingToCommitError` | Nothing staged |
| `auth_failed` | 12 | `AuthFailedError` | Registry rejected the request (401) — see `av auth doctor` |
| `unreachable_queued` | 13 | `UnreachableQueuedError` | **Not a failure of your work** — see below |
| `merge_conflict` | 14 | `MergeConflictError` | Conflicting changes; `error.data` carries remediation |
| `validation` | 15 | `ValidationError` | Bad input (unknown ref, malformed flag, policy misconfiguration) |
| `policy_denied` | 16 | `PolicyDeniedError` | `av promote`/`av merge` blocked by an armed policy |
| `budget_exhausted` | 17 | `BudgetExhaustedError` | v1.3.1: `av budget consume` reports a dimension now over its limit — the spend is recorded either way, never lost |
| `frozen` | 18 | `FrozenError` | v1.3.1: project is frozen (`av freeze on`) — `av promote`/`av improver register\|propose\|apply`/`av policy pack publish` are paused |
| `review_required` | 19 | `ReviewRequiredError` | v1.3.1: `av improver promote`'s `require_review` gate denied — nobody has approved this candidate yet (distinct from `policy_denied`: "get it reviewed" is a different remediation than "the metrics/signature don't qualify") |
| `scope_denied` | 20 | `ScopeDeniedError` | v1.3.1: token authenticated but lacks the required scope — the server returned 403 (e.g. `av freeze on/off` needs the `admin` scope) |
| `tenant_denied` | 22 | `TenantDeniedError` | v1.3.2: your credential authenticated fine but doesn't own the target `project_id` — the server returned 403 (`AV_TENANCY_ENFORCE=1` deployments only; enforcement is off by default) |
| — | 0 | — | Success, **including a queued commit** — see below |
| — | 2 | — | Click's own usage error (missing/bad CLI argument) — not part of this registry |

```python
from av_sdk import Repo, SDKError
from av_sdk.exceptions import NotARepoError, PolicyDeniedError

try:
    with Repo(path) as r:
        r.commit("...")
except NotARepoError:
    ...  # branch on the specific failure
except SDKError as e:
    ...  # or catch everything and read e.code / e.exit_code
```

**`unreachable_queued` is not an error to react to as a failure.** AGENTS.md
non-negotiable #3: any network failure queues the commit locally
(`.av/pending_push`) rather than losing it — `av commit`/`av push` (and
`Repo.commit()`/`Repo.push()`) exit **0** when this happens, with
`data.queued: true` / `data.queued_reason` telling you why. Treat it as "safely
persisted, will sync when the registry is reachable," not as something to retry
in a loop — `av push` (or the next successful commit) drains the queue on its own.

## Guardrails an autonomous loop should arm

Before letting a loop self-promote, set a policy so a regression can't land unattended:

```bash
av policy set main val_loss "<" --baseline-ref "main~1"
av promote --dry-run --into main   # preview the decision without touching anything
av promote --into main             # exit 16 (policy_denied) on DENY
```

Three worked `.av/policies.json` shapes (metric gate, signature gate, both combined) live
in `examples/policies/` — copy one in directly or use it as a reference for `av policy set`.

## RSI control plane (v1.3.1)

Versioning the IMPROVER (the agent's own code/prompts/tools/policy), not just the model
it produces, is a separate surface — `av_sdk.Repo` gained one method per write-capable
RSI operation (`improver_propose`/`improver_apply`/`improver_promote`, `canary_run`,
`review_submit`, `budget_consume`, `lessons_update`, `sandbox_run`, …), each raising the
same typed `SDKError` subclasses as the substrate:

```python
from av_sdk import Repo, SDKError

with Repo(".") as repo:
    try:
        repo.improver_promote(candidate_id)
    except SDKError as e:
        if e.code == "review_required":       # exit 19 — get it reviewed, don't retry
            ...
        elif e.code == "budget_exhausted":     # exit 17 — the spend was still recorded
            ...
```

`docs/rsi-operator-guide.md` is this page's RSI counterpart — the same continuous-path
walkthrough, covering propose→apply→canary→dual-gate-promote→review→promote→lessons→
budget-stop end to end; `examples/rsi_loop/` is the same narrative as a deterministic,
runnable reference agent with no LLM key.

## Where to go next

- `docs/contracts.md` — every published JSON schema and the stability policy.
- `docs/rsi-operator-guide.md` — the RSI control plane's continuous operator+agent path.
- `docs/avattributes.md` — per-path staging directives (`no-chunk`, `chunk`, `no-layer-split`).
- `docs/tutorial.md` — the full operator+agent walkthrough this page is the quick-reference for.
- `AGENTS.md` — guidance for agents *contributing to this repo's own source*, not for
  agents *using* Aether-Vault as a dependency (this page is the latter).
