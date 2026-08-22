# Pull Request

## What does this change?

<!-- One or two sentences: the problem/feature and the approach. Reference issues with #NN. -->

## Related phase / issue

- Issue: #
- CHANGELOG Phase entry: `development/CHANGELOG.md` → `## Phase N — <title>` appended?

## Checklist (see CONTRIBUTING.md & Essential-Tasks.md)

- [ ] Tests added/extended for every touched surface (CLI / core / server / plugins)
- [ ] Full suite green: `pytest tests/ -q` (Docker-dependent tests may skip locally)
- [ ] webui suite green if `webui/` touched: `cd webui && npm test`
- [ ] **Manual debugging session** done in a scratch repo with the real `av` binary
      (what did it catch? note it below — this step has caught real bugs unit tests missed)
- [ ] README updated: CLI Reference entry, roadmap flips, Mermaid diagram edges
- [ ] `development/CHANGELOG.md` phase entry appended; `Probleme.md` only if a real bug surfaced
- [ ] No new top-level imports that slow cold-start (`client`/`aether_core`/`ui` stay lazy)
- [ ] Backwards compatibility considered per VERSIONING.md (additive > breaking)

## Manual verification notes

<!-- Commands you ran against a live repo and what they printed. -->
