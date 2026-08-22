# `.github/` — Repository Automation

GitHub Actions workflows and repository-level configuration. See the
[main README](../README.md) for the project overview.

## Contents

| File | Purpose |
|---|---|
| [`workflows/tests.yml`](workflows/tests.yml) | CI on every push/PR — five jobs: full suite on Windows (`test`), framework-plugin extras on Ubuntu (`plugin-tests`), webui Vitest (`webui-tests`), live Postgres+Redis server suite (`server-tests`), and a real-browser Playwright E2E against a freshly built dashboard (`webui-e2e`) |
| [`workflows/release.yml`](workflows/release.yml) | Tag push (`vX.Y.Z`) → sdist + cibuildwheel wheels (cp310–cp312, Windows/Linux/macOS) published to PyPI via trusted publishing |
| [`workflows/docker-edge.yml`](workflows/docker-edge.yml) | Every push to master → `:edge` images of server and webui on GHCR |

## Notes for maintainers

- All pinned actions are on Node-24 runtime majors (`checkout@v5`, `setup-python@v6`,
  `setup-node@v6`, `upload-artifact@v7`, `download-artifact@v7`) — do not downgrade;
  GitHub force-deprecated Node-20 actions in June 2026.
- The two Docker-service jobs set `AV_DATA_DIR` for their bare-metal uvicorn processes:
  without a writable CAS directory, object uploads fail and seeded/pushed data silently
  never lands (see the comments in the workflow).
