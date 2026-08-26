# AGENTS.md — guidance for AI coding agents working in this repository

You are working on **Aether-Vault**, a high-performance artifact version-control system
for ML (models/datasets) with an autonomous-training focus. Read this before changing
anything.

## Non-negotiables

1. **Single commit writer.** Every commit — CLI, SDK, watch, plugins — must funnel
   through `python/av_cli/core.py::commit_staged()` → `_finalize_commit()`. Never build
   a second payload/persist path.
2. **Single restore/materialization path.** Working-tree writes go through
   `_materialize_tree()`.
3. **Offline resilience is sacred.** Any network failure must queue work
   (`.av/pending_push`), never lose it. `unreachable_queued` = safe.
4. **Contracts are versioned.** JSON envelope shapes, exit codes 10–16, `.avh`
   (`avh_version`), and HTTP payloads are user-facing contracts. Additive changes only
   without a MINOR bump + CHANGELOG entry.
5. **Tests travel with code.** New behavior lands together with its tests; the CI map
   lives in `development/infrastructure.md`. Run `pytest tests/ -q` before declaring done.

## Where things live

| Area | Path |
|---|---|
| CLI commands | `python/av_cli/cmd_*.py` (registered in `main.py`) |
| Shared logic | `python/av_cli/core.py` (incl. `commit_staged`, envelope helpers, exit codes) |
| Semantic diffs | `python/av_cli/semdiff.py` |
| Agent SDK | `python/av_sdk/` (`from av_sdk import Repo`) |
| Server | `python/av_server/server.py`, models in `models.py`, migrations in `migrations/versions/` |

## Conventions

- Commands: click, module-per-feature, lazy imports inside function bodies.
- Agent-facing output: use `emit_json/fail` from core; never print human text in json mode.
- DB changes: append an Alembic revision (`0003…`), update `test_migrations.py` heads.
- Docs move with code: README CLI reference, architecture.md contract section,
  infrastructure.md env vars, CHANGELOG phase entry, Probleme.md only for real bugs.
- Wrap-up checklist: `Aether-vault-Obsidian-Vault/Essential-Tasks.md`.

---

## Agent contracts (stable, versioned)

Aether-Vault treats agents as first-class operators. Everything below is a stable,
versioned contract: breaking changes follow the same MINOR-grace policy as the CLI
(see VERSIONING.md).

### JSON envelopes + exit codes

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

### Python SDK — `from av_sdk import Repo`

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

### Runs & lineage

`av run start` → every commit is filed under the run server-side (lazy-created if the
server hasn't seen it yet). `AV_RUN_ID=<id>` joins ANY process' commits with zero
integration. Lineage: `--parent <run-id>`; code provenance captured automatically
(git remote/sha/dirty) when available.

### Event stream + webhooks

```bash
curl "http://localhost:8000/api/events?since=0&project_id=…&kinds=commit&wait=25"
```

Ordered, resumable by event `id`; long-poll with `wait`. Webhooks: POST signed
`X-AV-Signature: hex(hmac-sha256(secret, body))`; manage via the
`av webhooks add/list/remove/test` CLI (v1.2.1). Since v1.2.2 failed deliveries persist
in a server-side ledger with automatic retry + dead-lettering — observe via
`GET /api/admin/webhook-deliveries`.

### Signed commits + audit (v1.2.2)

`av registry keygen` (needs `[sign]`) → commits auto-signed (ed25519 over the canonical
payload; the signature rides clone/pull) and `av verify <hash>` checks them anywhere —
tamper evidence, not a trust network. Query who-did-what-with-what-outcome via
`GET /api/admin/audit?action=…&since=…` or `av audit list --action commit.push`.

### `.avh` v2 — context memory

`av handoff` writes `handoff.avh` containing: lineage (run + git code pointer),
semantic_summary of the latest change, replay recipe, metric trend tail, and the
append-only `context_memory.notes`. Read it to inherit predecessor intent; extend it
with `av context note`. Validate any document: `av context validate`.

### Guardrails you should arm

Autonomous loops must not self-promote blindly:

```bash
av policy set main val_loss "<" --baseline-ref "main~1"
av promote <candidate> --into main     # exit 16 on DENY
```

### Quick reference

| Task | Command | SDK |
|---|---|---|
| inspect | `av --output json status` | `r.status()` |
| stage | `av add p/` | `r.add("p/")` |
| persist | `av --output json commit -m m [--no-upload]` | `r.commit(...)` |
| drain | `av push` | `r.push()` |
| what moved | `av diff v2` | `r.diff_semantic("v2")` |
| group work | `av run start/finish` | `r.run_start/run_finish` |
| remember | `av context note …` | `r.context_note(...)` |

---

## Operational guardrails for agents working on this codebase

### Context — what to read before changing anything

- **README + sub-readmes**: every folder has a short sub readme for introduction and overview — read the one for whatever you're touching.
- **`development/` folder**: read every `.md` file; they all hold important context for structure and plan.
- **`Aether-vault-Obsidian-Vault/`**: contains a generated graph of all dependencies and functions etc. to get a general overview — but always verify against source, as it can sometimes be out of date.

### Verification tasks — do these before declaring done

1. **Add test scripts** for everything you added.
2. **Run them** and debug if needed.
3. **Do manual debugging** — automated tests alone are not sufficient.
4. **Update documentation**: README + sub-readmes, test via `av test` suite, development folder, `Probleme.md`, `CHANGELOG.md`, `architecture.md`, `infrastructure.md`.
5. **Update `av --help`** if you added or changed a CLI command.
6. **Run vault scripts + handoff** (`av graph`, `av handoff`) if relevant.
7. **Tell the user** if the Docker image needs rebuilding, or if a git commit should be saved.
8. **Complete all essential tasks** from `Aether-vault-Obsidian-Vault/Essential-Tasks.md` at the end if not already done.
