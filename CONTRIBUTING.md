# Contributing to Aether-Vault

Thank you for helping make ML version control better! This document explains how to set
up a development environment, the conventions that keep the codebase healthy, and what a
complete contribution looks like.

**Licensing note:** Aether-Vault is licensed under the
[PolyForm Noncommercial License 1.0.0](LICENSE). By contributing, you agree that your
contributions are licensed under the same terms (noncommercial use free, commercial use
requires the copyright holder's separate license).

---

## Development setup

Prerequisites: **Python ≥ 3.10**, **C++ build tools + CMake** (the C++ core compiles from
source in a dev install), and optionally **Docker Desktop** for the registry/webui stack.

```bash
git clone https://github.com/leon1706-lol/Aether-Vault
cd Aether-Vault
pip install -e .[dev]          # compiles the C++ core, installs `av` + pytest

pytest tests/ -q               # full suite (Docker-dependent tests skip cleanly without a
                                # stack; current count tracked by README's own test badge,
                                # not restated here — see scripts/check_readme_test_freshness.py)
av doctor                      # repo/environment health check
```

For webui work:

```bash
cd webui
npm ci
npm test                       # Vitest unit/component suite
docker compose up -d db redis aether-vault-server   # only needed for Playwright E2E
python e2e/seed_data.py && npm run dev
npx playwright test
```

> **Import-path gotcha:** tests import `python.av_cli...` from the checkout — this works
> because `[tool.pytest.ini_options] pythonpath = ["."]` is set in `pyproject.toml`. Run
> pytest from anywhere; don't "fix" imports to relative paths.

## How we work (the wrap-up checklist)

Every feature/fix follows the checklist in
[`Aether-vault-Obsidian-Vault/Essential-Tasks.md`](Aether-vault-Obsidian-Vault/Essential-Tasks.md)
— summarized:

1. **Manual debugging session** — drive the real `av` binary end-to-end in a scratch repo
   outside the checkout. Unit tests alone have repeatedly missed real bugs.
2. **Tests grow with behavior** — every new command/branch/endpoint gets coverage in the
   matching surface (`tests/test_cli.py`, `test_core.py`, `test_server.py`, ...).
3. **Docs move with code, in order**: README (CLI reference + roadmap + diagrams) →
   `development/CHANGELOG.md` (next sequential Phase entry) → `development/Probleme.md`
   (only if a real bug was found) → handoff notes.
4. **Full suite green + clean `git status`** before considering it done.

## Code conventions

- **Modular by feature**: logic lives in dedicated modules (`history.py`, `sync.py`,
  `merge.py`, `attributes.py`), Click command bodies in `main.py` stay thin orchestration.
- **Latency discipline**: heavy imports are lazy (`client`, `aether_core`, `ui`);
  multi-object network operations batch-check first, then parallelize.
- **Single-code-path invariants** — do not add parallel implementations:
  - one working-tree restore path: `_materialize_tree()` (checkout/clone/pull/merge),
  - one commit-creation path: `_finalize_commit()` (commit/merge),
  - canonical content addressing is always plain SHA-256 (`hash_file_safe`);
    layer/chunk shards are additional objects, never replacements.
- **No comments narrating the obvious**; comments explain *why* and record invariants.

## Commit & PR style

- Commits follow the existing history's tone: short imperative summaries ("av stash &
  flags", "Benchmark optimisations"); PRs may bundle related phases but should say so.
- A complete PR includes: code + tests + CHANGELOG phase entry + README updates where the
  CLI surface or docs changed. The [PR template](.github/PULL_REQUEST_TEMPLATE.md)
  walks you through it.

## Where to ask questions

- Bug reports & feature ideas → [GitHub Issues](https://github.com/leon1706/aether-vault/issues)
  (use the templates).
- Security-sensitive findings → **never** in public issues; see
  [`SECURITY.md`](SECURITY.md).
