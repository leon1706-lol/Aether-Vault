# RSI operator guide: one continuous human + agent path (v1.3.1)

A single walkthrough through the whole RSI control plane added in v1.3.1 — the same
continuous-path convention `docs/tutorial.md` established for the substrate, applied to
the surfaces that version the IMPROVER (the agent's own code/prompts/tools/policy)
rather than the model it produces. Every command below is real; `tests/test_docs_commands.py`
parses every fenced `av ...` line on this page and asserts the command AND every flag it
uses actually exist in the live CLI. See `examples/rsi_loop/` for the same narrative
driven end-to-end by a deterministic scripted agent through `av_sdk.Repo` instead of the
CLI, and `development/architecture.md`'s per-surface "Contract" sections for the design
reasoning behind each piece below.

Every RSI surface here is **server-authoritative** — no offline queue exists for
improver versions, change sets, canary results, policy packs, budgets, etc. (unlike
ordinary commits, which always queue). You need a reachable registry
(`docker compose up -d db redis aether-vault-engine`, or your own deployment) for
everything past `av improver init`.

## 1. Register the baseline improver

```bash
av improver init
```

Registers the FIRST improver version — no parent, no files, a root to build lineage
from. A real improver typically registers with its actual agent code, prompts, and tool
schemas instead:

```bash
av improver register --code agent/train_loop.py --prompt prompts/system.md --tool-schema tools/schema.json
```

`av improver current` shows the locally active pointer; `av improver show <id>` shows one
version's full manifest (files, hashes, parent, signature status).

## 2. Propose, review, and apply a self-edit

A self-edit is a structured proposal — a diff, a rationale, and a predicted risk level —
not a silent in-place rewrite:

```bash
av improver propose --diff change.diff --rationale "lower lr to unblock the val_loss plateau" --risk low
```

Every proposal needs an explicit approval before it can be applied:

```bash
av improver review <change-set-id> --approve
av improver apply <change-set-id>
```

`apply` mints the next improver version (parented on the one the change set targeted)
and records the PREVIOUS version as `.av/improver/last_good` — the pointer
`av improver rollback` restores from with no arguments. Applying records the version
transition and its audit trail; running the actual diff happens in an isolated sandbox
(step 3) before or alongside this call, not automatically inside it.

## 3. Execute the change in a sandbox

```bash
av sandbox run --driver local --improver <improver-id> python train_loop.py
```

If the underlying command itself takes flags (e.g. `python train_loop.py --lr 1e-4`),
put a bare `--` before it so click stops parsing `av sandbox run`'s own options at that
point: `av sandbox run --driver local --improver <id> -- python train_loop.py --lr 1e-4`.

`--driver docker` runs the same job in a real container instead (`--network none` by
default, explicit `--mount host:container:ro` mounts, `--cpu`/`--memory-mb` caps) — the
same one protocol either way. A command that violates the improver's tool permission
manifest aborts before anything runs:

```bash
av tools manifest set <improver-id> --writable-path "checkpoints/**" --network none
av tools manifest verify <improver-id> python train_loop.py --network bridge
```

The `verify` call above is a pure dry run — it touches nothing, so you can check a
prospective job against a manifest before ever submitting it.

## 4. Run a capability canary

A canary is a small, fixed set of metric-threshold checks that must not regress:

```bash
av canary register core-capability canary-suite.json
av canary run core-capability --improver <improver-id>
```

`av canary status --improver <improver-id>` shows the most recent recorded result.

## 5. Arm the dual-gate promotion policy

The MODEL gate (`av promote`, unchanged since v1.2.0) and the IMPROVER gate are separate
concerns — one file each (`.av/policies.json` vs. `.av/improver_policy.json`), so arming
one never touches the other:

```bash
av improver policy set main --require-canaries --require-signature --require-review
```

## 6. Request promotion — expect a denial

```bash
av improver promote <improver-id> --dry-run
```

`--dry-run` evaluates the decision without landing anything, exiting 0 either way — a
script branches on `data.decision`, not the exit code. Without `--dry-run`, a real denial
exits **19** (`review_required`) when that's the deciding rule, or **16**
(`policy_denied`) for every other armed rule:

```bash
av improver promote <improver-id>
```

## 7. Get it reviewed

In a real deployment this is submitted by a **second** identity or token — a self-review
is rejected server-side (422: "You proposed this — another identity must review it"):

```bash
av review approve <improver-id> --target-type improver --comment "diff is minimal and well-tested"
```

Any open, unresolved objection also blocks promotion:

```bash
av critique add <improver-id> "no regression test for the lr change" --target-type improver
av critique resolve <critique-id> --resolution "added test_lr_schedule.py"
```

Waiving (unlike resolving) means the objection STANDS but is deliberately overridden —
always audited:

```bash
av critique waive <critique-id> --resolution "accepted risk, ships behind a flag"
```

## 8. Promote

```bash
av improver promote <improver-id>
```

## 9. Record what was learned

```bash
av blackboard post "Lowering lr to 1e-4 unblocks the val_loss plateau" --evidence "change_set:<change-set-id>"
av lineage link --cause-type change_set --cause <change-set-id> --metric accuracy --delta 0.05 --verified
av strategy add lower_lr_on_plateau --outcome worked --hyperparameters '{"lr": 1e-4}'
av lessons update lessons.json
```

`av search runs --metric val_loss --direction down` finds every run whose metric moved
that direction relative to its parent — deterministic, structured, no LLM involved.

## 10. Budgets and auto-stop — the loop stops itself

```bash
av budget set <run-id> --compute-seconds 3600 --steps 10000
av budget consume <budget-id> --compute-seconds 900 --steps 2500
```

Exceeding any dimension exits **17** (`budget_exhausted`) — the spend is recorded either
way, never lost. `av run auto-stop-check <run-id> --metric val_loss --patience 5 --stop`
detects plateau/divergence/NaN against a run's own metric history and can stop the run
directly with `--stop`; `av run branch-policy set --abandon-if "val_loss > 2.0"` +
`av run branch-policy check <run-id>` gives the same recommendation for a scheduler to
act on instead.

## 11. If something goes wrong: freeze and roll back

```bash
av freeze on --reason "candidate regressed a held-out eval overnight"
```

While frozen, only reads and rollback remain permitted — `av improver rollback` and
`av freeze off` are exempt by construction, so you can never freeze yourself out of the
recovery path. One command does both at once:

```bash
av incident rollback --reason "regression caught by the nightly canary"
```

## 12. The held-out eval vault

A separate, frozen, content-addressed suite the training agent's token cannot write to —
the actual enforcement mechanism, not a convention:

```bash
av eval register held-out-v1 eval-suite.json --blind
av eval freeze <suite-id>
av eval score <suite-id> --run <run-id> --metric accuracy=0.94
av eval reveal <result-id>
```

A `--blind` suite hides scores from non-`scorer` readers until revealed; a token without
the `scorer` scope is rejected with 403 (`scope_denied`, exit 20) on the score-recording
call itself, not merely discouraged by convention.

## Reference material

- `av <group> --help` at any point in the tree lists every flag this guide didn't spell
  out — `av improver --help`, `av sandbox --help`, `av eval --help`, and so on.
- `docs/for-agents.md` — the shared error/exit-code registry (17-20 are the RSI
  additions: `budget_exhausted`, `frozen`, `review_required`, `scope_denied`) and the
  `av_sdk.Repo` Python calls each command above has an SDK equivalent for.
- `docs/contracts.md` — the JSON Schemas each new artifact type (`improver-1.0`,
  `policy-pack-1.0`, `eval-suite-1.0`, `change-set-1.0`, `tool-manifest-1.0`,
  `action-log-1.0`) validates against.
- `examples/rsi_loop/README.md` — this same narrative, scripted and deterministic,
  runnable stack-free in CI or for real against a live registry.
