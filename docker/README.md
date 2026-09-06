# docker

Owns the runtime assets of the consolidated Aether-Vault ENGINE image: ONE image, ONE
container running all subservices.

- `engine-entrypoint.sh` - container entrypoint/supervisor. Dispatches on
  `AV_ENGINE_ROLE`: `all` (default) runs the uvicorn registry (:8000) AND the Next.js
  standalone server (:3000). A dead subservice is restarted INDEPENDENTLY
  (`record_restart`'s sliding-window budget,
  `AV_ENGINE_MAX_RESTARTS`/`AV_ENGINE_RESTART_WINDOW_SECS`, default 5 restarts/300s); only
  exceeding that budget (or `AV_ENGINE_RESTART_SUBSERVICE=0`) shuts the whole container
  down, at which point `restart: unless-stopped` brings the whole engine back. `server` /
  `webui` run one subservice each (legacy alias support). When the role is UNSET it
  auto-detects from container env - `DATABASE_URL` set -> server-only,
  `NEXT_PUBLIC_API_URL` set without it -> webui-only - which is exactly what a pinned
  two-container compose file produces against the aliased legacy image names.

The multi-stage Dockerfile lives at the repo root; compose files are
`../docker-compose.yml` (dev, build-based) and
`../python/av_cli/docker/docker-compose.release.yml` (pulls GHCR images). Healthchecks
use what the image already ships - python-urllib for :8000, node fetch for :3000 - no
extra packages installed for healthchecking.

## Build targets

The Dockerfile has THREE named build targets, not one - `docker build --target <name>`
(or compose's `build.target`):

- `engine` (default target, and what `docker-compose.yml` builds) - the consolidated,
  ONE-image-does-everything image this whole document describes.
- `server` / `webui` - slim, single-role images (no Node in `server`, no Python in
  `webui`) for operators who genuinely want two containers. Publishing these under
  the OLD `aether-vault-server`/`aether-vault-webui` alias names has stopped (see
  `VERSIONING.md`'s "Removed" entry) - they now publish as `server-*`/`webui-*`
  tags instead. Already-pulled legacy-named images keep working unmodified via the
  role auto-detect above; only NEW pulls under the old alias names 404. Migrating a
  pinned two-container compose file onto the consolidated `engine` image (or rewriting
  it to the new slim tag names)? `av doctor --compose PATH --write` does it for you -
  dry-run by default; see [`docs/migrate-engine-image.md`](../docs/migrate-engine-image.md).

Never build without an explicit `--target`/`target:` on a Dockerfile with more than one
runtime stage - Docker defaults to the LAST stage in the file, which silently changed
once when a new stage was appended after the original one (`development/Probleme.md`
has the incident). `docker-compose.yml`, `release.yml`, `docker-edge.yml`, and
`tests.yml`'s `e2e-engine-smoke` job all pin `target: engine`/`--target engine`
explicitly for exactly this reason.

Orchestrator liveness (`/api/health`) vs. readiness (`/api/ready`) probe stanzas live in
`development/infrastructure.md`.
