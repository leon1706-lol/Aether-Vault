# Published contracts

Aether-Vault treats these shapes as stable, versioned contracts — see `VERSIONING.md` for
what "stable" means in practice (additive-only within a MINOR, breaking changes only at a
MINOR boundary with a full deprecation cycle). Every shape below has a published JSON
Schema (draft 2020-12) shipped in the installed package at `av_cli/schemas/*.schema.json`,
loadable at runtime via `python.av_cli.core.load_contract_schema(name)` (works from a
checkout or an installed wheel — `importlib.resources` under the hood).

| Contract | Schema file | Produced by |
|---|---|---|
| JSON envelope | `envelope-1.0.schema.json` | Every `av --output json <command>` invocation |
| Semantic diff | `semdiff-1.0.schema.json` | `av diff --output json`'s `data`, `.avh`'s `semantic_summary`, `GET /api/runs/{id}/summary`'s `semantic_summary` |
| `.avh` handoff document | `avh-2.0.schema.json` | `av handoff` (writes `handoff.avh`), `Repo.handoff_dict()` |
| Event | `event-1.0.schema.json` | `GET /api/events` (each row of `data.events`) |
| Run | `run-1.0.schema.json` | `GET /api/runs/{id}`, each row of `GET /api/runs` |
| Webhook delivery body | `webhook-payload-1.0.schema.json` | The exact signed bytes POSTed to a webhook URL (verify against `X-AV-Signature`) |

## Loading a schema

```python
from python.av_cli.core import load_contract_schema

schema = load_contract_schema("envelope-1.0")  # dict, ready for jsonschema.validate()
```

External tooling (not this repo) that only needs the raw files can read them directly from
an installed wheel's `av_cli/schemas/` directory, or from
`https://aether-vault.dev/schemas/<name>.json` (the `$id` in each file) once published
there.

## Stability policy

- **Additive only within a MINOR.** A new optional field, a new enum value that only
  appears in new documents, a new schema file — all fine in a PATCH or MINOR release.
- **Tightening a `required` list is additive too, IF every value the code has produced
  since the schema's own introduction already satisfies it.** The v1.3.0 tightening of
  `avh-2.0.schema.json`'s `semantic_summary`/`chunks` required-fields is an example: every
  `.avh` document generated since v1.2.5 already has those fields, and older documents are
  backfilled by `handoff.py::_upgrade_handoff()` before validation ever sees them.
- **Removing a field, changing a field's type, or tightening in a way older real documents
  fail** requires a new schema file (bump the version in the filename, e.g. `run-2.0`) and
  a full MINOR-boundary deprecation cycle for the old one, exactly like any other contract
  change in `VERSIONING.md`.
- Every schema here is validated against **live** output, not hand-written fixtures — see
  `tests/test_contracts.py` (envelope/semdiff/avh, stack-free) and the schema-matching
  tests in `tests/test_server.py` (run/event/webhook-payload, need a live registry). A
  schema that no longer matches what the code actually emits is a test failure, not a
  silent drift.

## Where the underlying contracts are described in prose

`development/architecture.md`'s per-subsystem contract sections (Commit, Merge, Runs,
Events & Webhooks, Signed Commits, `.avh` v2, Semantic Diff) are the narrative explanation
of *why* each shape looks the way it does; this page is the machine-checkable shape itself.
