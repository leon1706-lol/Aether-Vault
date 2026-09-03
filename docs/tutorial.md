# Tutorial: one continuous operator + agent path

A single walkthrough covering the flows README's Quick Start only samples individually:
`init` → train under a run → snapshot the environment → commit each checkpoint → promote
past a policy gate → hand off to the next agent. Every command below is real and runs
against a local repo with no server required (Anonymous mode, no Docker) unless noted.
`tests/test_docs_commands.py` parses every fenced `av ...` command on this page and
asserts it (and every flag) actually exists in the live CLI — this page can't rot silently.

See `docs/for-agents.md` for the equivalent `av_sdk.Repo` Python calls and the shared
error/exit-code registry, and `docs/contracts.md` for the JSON Schemas each `--output
json` payload below validates against.

## 1. Init

```bash
av init --mode local --yes --no-repl
```

Creates `.av/` (objects, refs, commits, config) in the current directory. `--mode local`
skips the remote-registry prompt (Anonymous mode, `http://localhost:8000` by default —
commits queue locally when nothing is listening there, never lost, never blocked on);
`--yes --no-repl` skip the interactive setup wizard entirely, for a scripted/agent start.

## 2. Snapshot the environment

```bash
av env snapshot
```

Captures a hashed, reproducible environment id (Python version, OS family, curated
package pins, seeds) — the id is what `av replay` reconstructs a matching environment
from later, and what a commit's `env_snapshot_id` field points back to. Do this once per
distinct environment, not per commit.

## 3. Start a run, train, commit each checkpoint

```bash
av --output json run start baseline-tune
```

Every commit made from here carries a `run:<id>` tag and a linked-run pointer
automatically — `run start` prints the run id in its JSON envelope's `data.run_id`.

```bash
av add checkpoint-epoch1.safetensors
av --output json commit -m "epoch 1" --metric val_loss=0.52
av add checkpoint-epoch2.safetensors
av --output json commit -m "epoch 2" --metric val_loss=0.41 --tag candidate
```

`--metric key=value` (repeatable) attaches numeric tracking data directly into the atomic
commit — no separate tracking store to keep in sync. `.safetensors` checkpoints are
automatically layer-split (only the layers that actually changed re-upload); other large
files use content-defined chunking — see `docs/avattributes.md` if you need to force or
suppress either for a specific path.

```bash
av --output json context note "val_loss plateaued around epoch 2 — try a lower LR next"
```

A freeform note filed under the active run (`context note` stamps whichever run is
currently open), searchable later with `av context search <query>` and included in the
`.avh` handoff this run eventually produces.

```bash
av --output json run finish --metric final_val_loss=0.41
```

Marks the run completed and regenerates the local `handoff.avh` so its lineage/metrics/
semantic-summary fields are guaranteed present — publishing (step 6) stays opt-in.

## 4. Arm a promotion policy

```bash
av policy set main val_loss "<" --threshold 0.45
```

Denies promoting a candidate onto `main` unless its `val_loss` metric is below `0.45` —
see `examples/policies/` for worked metric-gate, signature-gate, and combined-gate
policies you can copy directly.

## 5. Promote

```bash
av promote --into main --dry-run
```

Previews the decision (`allow`/`deny` plus which rule decided it) without landing
anything — safe to run repeatedly while iterating. Once satisfied:

```bash
av promote --into main
```

Lands the candidate as a merge onto `main` if the policy allows it; exits with the
`policy_denied` code (16) otherwise, no partial state either way.

## 6. Hand off to the next agent

```bash
av --output json handoff --publish
```

Regenerates `handoff.avh` (branch, commit, tags, metrics, model/dataset lineage,
freeform notes) one more time and — because `--publish` was passed — uploads it as a
normal content-addressed object, recording a pointer (`runs.avh_object_id`) the next
agent (or the WebUI's run-detail view) can resolve. Without `--publish`, the document is
written locally only; notes can hold private reasoning, so nothing about this step is
automatic.

A different agent picking this up later, in a different process or machine entirely,
starts from:

```bash
av context show          # every note left so far, oldest first
av handoff show          # the current handoff.avh, rendered human-readable
av env replay            # reconstruct the exact environment this work was done under
```

That closes the loop: everything the first agent learned — metrics, notes, the exact
environment, which promotion policy gated the work — is discoverable by the second agent
without re-deriving any of it.
