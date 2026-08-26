# `docker/` — Engine Image Runtime Files

Runtime assets for the consolidated **Aether-Vault Engine** image (v1.2.2): ONE image,
ONE container running all subservices.

| File | Purpose |
|---|---|
| `engine-entrypoint.sh` | Container entrypoint/supervisor. Dispatches on `AV_ENGINE_ROLE`: `all` (default) runs the uvicorn registry (:8000) and the Next.js standalone server (:3000) together — either child dying takes the container down for restart; `server` / `webui` run a single subservice (legacy alias support). When the role is UNSET, it auto-detects from container env (`DATABASE_URL` set → server; `NEXT_PUBLIC_API_URL` set without it → webui), which is exactly what pre-1.2.2 pinned compose files produce against the aliased legacy image names. |

The multi-stage Dockerfile lives at the repo root; the compose files are
`../docker-compose.yml` (dev, build-based) and
`../python/av_cli/docker/docker-compose.release.yml` (pulls GHCR images).

Healthchecks use what is already in the image: python-urllib for :8000, node fetch for
:3000. No extra packages are installed for healthchecking.
