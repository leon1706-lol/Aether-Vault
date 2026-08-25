# For Agents — driving Aether-Vault without a human

Aether-Vault treats agents as first-class operators. Everything below is a **stable,
versioned contract**: breaking changes follow the same MINOR-grace policy as the CLI
(see VERSIONING.md).

## 1. JSON envelopes + exit codes

Prefix any agent-surface command with `--output json`:

```json
{"ok": true, "data": {…}, "error": null,
 "meta": {"command": "commit", "version": "1.2.0"}}
```

Failures return `ok:false` with `error.code ∈ {not_a_repo, nothing_to_commit,
auth_failed, unreachable_queued, merge_conflict, validation, policy_denied}` and exit
codes `10–16` respectively (`0` ok, `2` usage). `unreachable_queued` means the work is
SAFE — persisted locally and queued for `av push`.

Supported commands (v1.2): status · add · commit · push · diff · run start/finish/list/
show · context note/show/validate/export · env snapshot/replay · policy set/list/remove ·
registry export/keygen/attest/verify.

## 2. Python SDK — `from av_sdk import Repo`

Drives the same single commit path as the CLI; returns the same dict payloads.

```python
from av_sdk import Repo

with Repo("/path/to/repo") as r:
    r.add("checkpoints/")            # stages via hashing core (layers/CDC)
    started = r.run_start("sweep-7") # commits now auto-tag run:<id>
    c = r.commit("epoch 12", metrics={"val_loss": 0.31}, no_upload=True)
    r.run_finish(metrics={"final_loss": 0.29})
    summary = r.diff_semantic()["summary"]
    r.context_note("LR 3e-4 diverged at step 9k — keep 1e-4")
```

Errors raise `SDKError` with `.code/.message/.exit_code`.

## 3. Runs & lineage

`av run start` → every commit is filed under the run server-side (lazy-created if the
server hasn't seen it yet). `AV_RUN_ID=<id>` joins ANY process' commits with zero
integration. Lineage: `--parent <run-id>`; code provenance captured automatically
(git remote/sha/dirty) when available.

## 4. Event stream + webhooks

```bash
curl "http://localhost:8000/api/events?since=0&project_id=…&kinds=commit&wait=25"
```

Ordered, resumable by event `id`; long-poll with `wait`. Webhooks: POST signed
`X-AV-Signature: hex(hmac-sha256(secret, body))`; manage via `/api/webhooks`
(`av webhooks` CLI planned; API stable now).

## 5. `.avh` v2 — context memory

`av handoff` writes `handoff.avh` containing: lineage (run + git code pointer),
semantic_summary of the latest change, replay recipe, metric trend tail, and the
append-only `context_memory.notes`. Read it to inherit predecessor intent; extend it
with `av context note`. Validate any document: `av context validate`.

## 6. Guardrails you should arm

Autonomous loops must not self-promote blindly:

```bash
av policy set main val_loss "<" --baseline-ref "main~1"
av promote <candidate> --into main     # exit 16 on DENY
```

## 7. Quick reference

| Task | Command | SDK |
|---|---|---|
| inspect | `av --output json status` | `r.status()` |
| stage | `av add p/` | `r.add("p/")` |
| persist | `av --output json commit -m m [--no-upload]` | `r.commit(...)` |
| drain | `av push` | `r.push()` |
| what moved | `av diff v2` | `r.diff_semantic("v2")` |
| group work | `av run start/finish` | `r.run_start/run_finish` |
| remember | `av context note …` | `r.context_note(...)` |
