# aether-vault Helm chart

Deploys the Aether-Vault engine (registry API + webui, one image, `AV_ENGINE_ROLE=all`)
to Kubernetes: a Deployment, Service, optional Ingress/HPA, a PodDisruptionBudget, and a
pre-install/pre-upgrade migration Job that brings the schema to head before any new
replica serves traffic.

## Honest verification status (read this before trusting it in production)

This chart is verified by `helm template deploy/helm/aether-vault | kubeconform -strict`
(the `helm-lint` CI job) — every template renders syntactically valid Kubernetes manifests
against the Kubernetes API schema, for the default values and a representative
override set (multi-replica + ingress + autoscaling enabled).

**It has NOT been drilled against a real running Kubernetes cluster on this machine.**
That is a deliberate, stated scope decision, not an oversight — see `todo.md`'s open
items. The HA claims this
project can actually back with a real, live drill are the ones in `docker-compose.ha.yml`
+ `scripts/ha_drill.sh`, which genuinely brings up multiple replicas, kills one mid-load,
and asserts zero failed requests / zero double webhook delivery / correctly-global rate
limiting. If your target is Kubernetes specifically, this chart is a correct-by-schema
starting point, not a claim that identical behavior has been observed on a real cluster.

## Prerequisites

This chart does NOT deploy Postgres or Redis — bring your own (a managed instance, or a
separate chart such as `bitnami/postgresql` / `bitnami/redis`). Provide connection
strings via `--set database.url=... --set redis.url=...` or, for anything beyond a demo,
via `database.existingSecret`/`redis.existingSecret` (see `values.yaml`) so credentials
never pass through `helm --set` or land in a values file.

## Quickstart

```bash
helm template deploy/helm/aether-vault \
  --set database.url=postgresql+asyncpg://av_user:av_password@postgres:5432/aether_vault \
  --set redis.url=redis://redis:6379/0 \
  | kubeconform -strict -summary

helm install av deploy/helm/aether-vault \
  --set database.url=postgresql+asyncpg://av_user:av_password@postgres:5432/aether_vault \
  --set redis.url=redis://redis:6379/0
```

## Multi-replica correctness

`replicaCount > 1` requires `rateLimit.backend=redis` (and, if `AV_AUTH_SPIKE_BACKEND` is
in play for your deployment, `authSpike.backend=redis` too) for the same reason
`docker-compose.ha.yml` sets it — see `rate_limit.py`'s own module docstring. `NOTES.txt`
warns at install/upgrade time if this is misconfigured.
