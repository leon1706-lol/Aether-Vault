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
| Contracts for agents | `docs/for-agents.md` |

## Conventions

- Commands: click, module-per-feature, lazy imports inside function bodies.
- Agent-facing output: use `emit_json/fail` from core; never print human text in json mode.
- DB changes: append an Alembic revision (`0003…`), update `test_migrations.py` heads.
- Docs move with code: README CLI reference, architecture.md contract section,
  infrastructure.md env vars, CHANGELOG phase entry, Probleme.md only for real bugs.
- Wrap-up checklist: `Aether-vault-Obsidian-Vault/Essential-Tasks.md`.
