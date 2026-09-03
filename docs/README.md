# Docs index

| Doc | What it covers |
|---|---|
| [`tutorial.md`](tutorial.md) | One continuous operator + agent path: init → train under a run → env snapshot → commit → promote past a policy gate → publish a handoff → the next agent picks it up. |
| [`for-agents.md`](for-agents.md) | The minimal recipe for driving Aether-Vault from an autonomous loop (CLI subprocess or `av_sdk.Repo` — both equivalent), plus the shared error/exit-code registry. |
| [`contracts.md`](contracts.md) | Every published, versioned JSON Schema (envelope, semantic diff, `.avh`, event, run, webhook payload) and where each is produced. |
| [`avattributes.md`](avattributes.md) | `.avattributes` staging directives — forcing or suppressing chunking/layer-splitting for a specific path. |
| [`migrate-engine-image.md`](migrate-engine-image.md) | Moving a pinned two-container (`aether-vault-server`/`aether-vault-webui`) compose file onto the consolidated `aether-vault-engine` image, or onto the new slim single-role images. |

For everything else — install, CLI reference, architecture, CI, benchmarks, the full
build history — see the top-level [`README.md`](../README.md) and `development/` (start
with [`architecture.md`](../development/architecture.md) and
[`infrastructure.md`](../development/infrastructure.md)).

`tests/test_docs_commands.py` parses every fenced `av ...` command on every page in this
directory and asserts the command and each flag actually exist in the live Click tree —
these docs can't silently drift from the real CLI.
