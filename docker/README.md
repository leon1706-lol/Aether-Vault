# docker

Owns the runtime assets of the consolidated Aether-Vault ENGINE image (v1.2.2): ONE
image, ONE container running all subservices.

- `engine-entrypoint.sh` - container entrypoint/supervisor. Dispatches on
  `AV_ENGINE_ROLE`: `all` (default) runs the uvicorn registry (:8000) AND the Next.js
  standalone server (:3000); either child dying takes the container down so
  `restart: unless-stopped` brings the whole engine back. `server` / `webui` run one
  subservice each (legacy alias support). When the role is UNSET it auto-detects from
  container env - `DATABASE_URL` set -> server-only, `NEXT_PUBLIC_API_URL` set without
  it -> webui-only - which is exactly what pre-1.2.2 pinned compose files produce
  against the aliased legacy image names.

The multi-stage Dockerfile lives at the repo root; compose files are
`../docker-compose.yml` (dev, build-based) and
`../python/av_cli/docker/docker-compose.release.yml` (pulls GHCR images). Healthchecks
use what the image already ships - python-urllib for :8000, node fetch for :3000 - no
extra packages installed for healthchecking.
