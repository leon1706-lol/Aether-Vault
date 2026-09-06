# AGENTS.md — guidance for AI coding agents working in this repository

**Aether-Vault**: high-performance artifact version-control for ML (models/datasets),
autonomous-training focus. Read this before changing anything.
`Aether-vault-Obsidian-Vault/Essential-Tasks.md` is the wrap-up checklist this file
defers to — part of the contract, not optional.

## Before touching anything

- **`todo.md`** (repo root) — owner's live planning canvas, current objective(s) in plain
  language. Check first. Not a generated/permanent backlog — gets rewritten/cleared as
  objectives change, so an empty file isn't "nothing to do": ask, or fall back to
  `development/CHANGELOG.md`'s latest phase for context.
- **Sub-readmes** — every folder has one; read it before touching that folder.
- **`development/*.md`** — structure/plan context, read all of them.
- **`Aether-vault-Obsidian-Vault/`** — generated dependency/function graph for
  orientation; can be stale, cross-check against source.

## Non-negotiables

1. **Single commit writer.** Every commit (CLI, SDK, watch, plugins) funnels through
   `python/av_cli/core.py::commit_staged()` → `_finalize_commit()`. No second payload/persist path.
2. **Single restore path.** Working-tree writes go through `_materialize_tree()`.
3. **Offline resilience is sacred.** A network failure queues work (`.av/pending_push`),
   never loses it. `unreachable_queued` = safe, not an error.
4. **Contracts are versioned.** JSON envelope shapes, exit codes 10–22 (17-20 are the RSI
   additions, 22 is tenancy, 21 is reserved-then-activated — see `docs/for-agents.md`),
   `.avh` (`avh_version`), and HTTP payloads are user-facing. Additive only, MINOR bump +
   CHANGELOG entry.
5. **Nothing is done until verified.** New behavior needs its own tests (`pytest tests/ -q`
   green) *and* a manual real-CLI repro in a scratch repo outside this checkout — unit
   tests alone have missed real bugs here before. Full sequence (debug → tests → docs →
   vault regen → sanity check) is in `Aether-vault-Obsidian-Vault/Essential-Tasks.md`; run
   it before declaring done.

## Where things live

| Area | Path |
|---|---|
| CLI commands | `python/av_cli/cmd_*.py` (registered in `main.py`) |
| Shared logic | `python/av_cli/core.py` (`commit_staged`, envelope helpers, exit codes) |
| Semantic diffs | `python/av_cli/semdiff.py` |
| Agent SDK | `python/av_sdk/` (`from av_sdk import Repo`) |
| Server | `python/av_server/server.py`, models in `models.py`, migrations in `migrations/versions/` |

## Conventions

- Commands: click, module-per-feature, lazy imports inside function bodies.
- Agent-facing output: `emit_json/fail` from core; never print human text in json mode.
- DB changes: append an Alembic revision (`0003…`), update `test_migrations.py` heads.
- Docs move with code: README CLI reference, architecture.md contracts,
  infrastructure.md env vars, CHANGELOG.md phase entry, Probleme.md only for real bugs.
- **Probleme.md entries: condensed, not narrated.** Every entry is title + severity/status
  line + **Problem**/**Fix**/**Verification**, each section **~1-3 sentences** — the
  finding, the change, the proof, nothing else. No blow-by-blow of hypotheses tried,
  investigation narrative, or restated code. This is a MUST going forward, not a style
  preference: condense at write time, don't write long and condense later.
- **Comments: short and precise.** A comment earns its place by telling a
  reader something the code can't — a non-obvious *why*, an invariant, a real gotcha.
  Useless (restates the code, or narrates process — "found live", "see Probleme.md #N")
  → delete; that history belongs in git log/CHANGELOG/Probleme.md. Somewhat useful →
  state the fact in ≤2 sentences. Genuinely important (safety/security/concurrency
  invariants, a bug class that recurs without the reasoning, a cross-file contract) →
  keep in full — use judgment, don't cap by rule. Applies to every non-`.md` file;
  never touch a vendored file's own comments (e.g. `src/json.hpp`).

---

## Agent contracts (stable, versioned)

Agents are first-class operators. Everything below is a stable, versioned contract —
breaking changes follow the CLI's MINOR-grace policy (see VERSIONING.md).

### JSON envelopes + exit codes

Prefix any agent-surface command with `--output json`:

```json
{"ok": true, "data": {…}, "error": null,
 "meta": {"command": "commit", "version": "1.2.0"}}
```

Failures: `ok:false`, `error.code ∈ {not_a_repo, nothing_to_commit, auth_failed,
unreachable_queued, merge_conflict, validation, policy_denied}`, exit codes `10–16`
respectively (`0` ok, `2` usage). `unreachable_queued` = safe, persisted locally and
queued for `av push`. RSI additions: `budget_exhausted` (17, `av budget consume` over a
limit), `frozen` (18, `av freeze on` pauses promotions/self-edits), `review_required`
(19, `av improver promote`'s reviewer gate denied), `scope_denied` (20, server-side
token-scope 403). `tenant_denied` (22, credential valid but doesn't own the target
`project_id` — `AV_TENANCY_ENFORCE=1` only). `login_required` (21, `av login`'s SSO
device-code flow timed out — distinct from `auth_failed`, a *rejected* not missing
credential). Full table: `docs/for-agents.md`.

Every CLI command supports `--output json` except `watch` (NDJSON — one envelope per
auto-commit, not per invocation) and the dev-only `test`/`benchmark`/`webui` (see
`docs/contracts.md`'s exemption list). Core: status · add · commit · push · diff · run
start/finish/list/show · context note/show/validate/export/search · env snapshot/replay ·
policy set/list/remove/promote --dry-run · registry export/keygen/attest/verify · auth
doctor/rotate · audit list/export/prune --dry-run. **RSI control plane** (see
`docs/rsi-operator-guide.md`): improver register/propose/review/apply/rollback/promote/
lineage · canary register/run/status · freeze on/off/status · incident rollback · eval
register/freeze/score/reveal/adapter · task propose/accept/reject · plan create/attach/
validate · budget set/consume · scheduler queue · review approve/reject · critique add/
resolve/waive · lineage link/show · search runs · strategy add/search · lessons update/
show · blackboard post/resolve · sandbox run/status/cancel/logs/queue · replay-actions ·
tools manifest show/set/verify · policy pack publish/show/log/verify. **Enterprise
readiness** (see `docs/enterprise-operator-guide.md`): tenant
create/list/show/update/suspend · user list/show/create/suspend/delete · role
list/show/create/grant/revoke · token create/list/revoke · admin backup
create/verify/restore. **SSO/SCIM/audit integrity**: login/logout/whoami · idp
add/list/show/test/remove · scim status/token create/revoke · audit verify.
`tests/test_contract_matrix.py` walks every command asserting `--output json` never
mixes human text with the envelope — check it before adding a new command.

### Python SDK — `from av_sdk import Repo`

Drives the same single commit path as the CLI; same dict payloads.

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

`av run start` → every commit files under the run server-side (lazy-created if unseen).
`AV_RUN_ID=<id>` joins any process' commits with zero integration. Lineage: `--parent
<run-id>`; code provenance (git remote/sha/dirty) captured automatically when available.

### Event stream + webhooks

```bash
curl "http://localhost:8000/api/events?since=0&project_id=…&kinds=commit&wait=25"
```

Ordered, resumable by event `id`; long-poll with `wait`. Webhooks: POST signed
`X-AV-Signature: hex(hmac-sha256(secret, body))`; manage via `av webhooks
add/list/remove/test`. Failed deliveries persist in a server-side ledger with automatic
retry + dead-lettering — observe via `GET /api/admin/webhook-deliveries`.

### Signed commits + audit

`av registry keygen` (needs `[sign]`) → commits auto-signed (ed25519 over the canonical
payload, rides clone/pull); `av verify <hash>` checks anywhere — tamper evidence, not a
trust network. Query who-did-what-with-what-outcome: `GET
/api/admin/audit?action=…&since=…` or `av audit list --action commit.push`.

### `.avh` v2 — context memory

`av handoff` writes `handoff.avh`: lineage (run + git code pointer), semantic_summary of
the latest change, replay recipe, metric trend tail, append-only
`context_memory.notes`. Read it to inherit predecessor intent; extend with `av context
note`. Validate any document: `av context validate`.

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

## Wrap-up checklist

Run the full sequence in `Aether-vault-Obsidian-Vault/Essential-Tasks.md` (scratch-repo
debug → tests → docs → vault regen → sanity check) — canonical detail, don't duplicate
here. Two things it doesn't say explicitly:

- Update `av --help` / README CLI reference for any added or changed command.
- Ask, don't act: tell the user if the Docker image needs rebuilding or a commit should
  be saved — don't do either unprompted.
