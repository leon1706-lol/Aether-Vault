"""V1.6.3: every shipped compose file carries the footprint knobs as `${VAR:-default}`
interpolation (no overlay file -- `av`'s own `docker compose -f <file>` calls would drop
one), memory limits on every service, Postgres/Redis memory tuning, and the fork-free
healthcheck script."""
from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")

ROOT = Path(__file__).resolve().parents[1]
COMPOSE_FILES = {
    "dev": ROOT / "docker-compose.yml",
    "release": ROOT / "python" / "av_cli" / "docker" / "docker-compose.release.yml",
    "ha": ROOT / "docker-compose.ha.yml",
}


def _load(name):
    return yaml.safe_load(COMPOSE_FILES[name].read_text(encoding="utf-8"))


def _env(service: dict) -> dict:
    env = service.get("environment") or {}
    if isinstance(env, list):
        out = {}
        for item in env:
            k, _, v = str(item).partition("=")
            out[k] = v
        return out
    return {str(k): str(v) for k, v in env.items()}


@pytest.mark.parametrize("name", list(COMPOSE_FILES))
def test_every_service_has_a_memory_limit(name):
    doc = _load(name)
    missing = [svc for svc, spec in doc["services"].items()
               if svc != "lb" and not (spec.get("deploy") or {}).get("resources", {}).get("limits", {}).get("memory")]
    assert missing == [], missing


@pytest.mark.parametrize("name", list(COMPOSE_FILES))
def test_postgres_memory_tunables_are_interpolated(name):
    doc = _load(name)
    for svc, spec in doc["services"].items():
        if not str(spec.get("image", "")).startswith("postgres") or "entrypoint" in spec:
            continue  # the HA replica runs a custom entrypoint that streams the primary's config
        command = " ".join(str(spec.get("command", "")).split())
        for flag in ("shared_buffers=${AV_PG_SHARED_BUFFERS:-64MB}", "work_mem=${AV_PG_WORK_MEM:-4MB}",
                     "max_connections=${AV_PG_MAX_CONNECTIONS:-100}"):
            assert flag in command, (name, svc, flag)
        if svc == "db-primary":
            assert "wal_level=replica" in command  # replication flags survive the memory ones


@pytest.mark.parametrize("name", list(COMPOSE_FILES))
def test_redis_has_maxmemory_with_volatile_lru(name):
    doc = _load(name)
    redis_services = [svc for svc, spec in doc["services"].items() if "redis-stack" in str(spec.get("image", ""))]
    assert redis_services
    for svc in redis_services:
        args = _env(doc["services"][svc]).get("REDIS_ARGS", "")
        assert "--maxmemory ${AV_REDIS_MAXMEMORY:-128mb}" in args, (name, svc, args)
        assert "--maxmemory-policy volatile-lru" in args, (name, svc, args)
        if svc == "redis-replica":
            assert "--replicaof redis-primary 6379" in args


@pytest.mark.parametrize("name", ["dev", "release"])
def test_engine_role_and_knobs_are_env_overridable(name):
    env = _env(_load(name)["services"]["aether-vault-engine"])
    assert env["AV_ENGINE_ROLE"] == "${AV_ENGINE_ROLE:-all}"
    for key, default in (("AV_UVICORN_WORKERS", "1"), ("AV_WEBUI_NODE_HEAP_MB", "256"),
                         ("AV_DB_POOL_SIZE", "10"), ("AV_DB_MAX_OVERFLOW", "20"), ("AV_MAX_UPLOAD_BYTES", "0")):
        assert env[key] == "${" + key + ":-" + default + "}", (key, env.get(key))
    assert env["AV_UVICORN_LIMIT_CONCURRENCY"] == "${AV_UVICORN_LIMIT_CONCURRENCY:-}"


@pytest.mark.parametrize("name", list(COMPOSE_FILES))
def test_engine_healthcheck_uses_the_script_not_a_python_node_fork(name):
    doc = _load(name)
    engines = [svc for svc in doc["services"] if svc.startswith("aether-vault-engine") or svc.startswith("engine-")]
    assert engines
    for svc in engines:
        hc = doc["services"][svc]["healthcheck"]
        assert hc["test"] == ["CMD", "/engine-healthcheck.sh"], (name, svc, hc["test"])
        assert hc["interval"] == "${AV_HEALTHCHECK_INTERVAL:-30s}"
        assert "python -c" not in str(hc) and "node -e" not in str(hc)


def test_dockerfile_bakes_the_healthcheck_script_into_every_target():
    text = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert text.count("COPY docker/engine-healthcheck.sh /engine-healthcheck.sh") == 3
    assert text.count("CMD /engine-healthcheck.sh") == 3
    assert "urllib.request.urlopen" not in text and "node -e" not in text


def test_no_overlay_compose_file_exists():
    """The knobs live in the three real files; an overlay would be dropped by every
    `docker compose -f <file>` call av itself makes (docker_runtime.py)."""
    assert not list(ROOT.glob("docker-compose.lowmem*.yml"))
    assert not list(ROOT.glob("docker-compose.override*.yml"))


def test_scripts_copied_into_images_are_lf_and_pinned_by_gitattributes():
    """A CRLF shebang in docker/engine-entrypoint.sh made the rebuilt engine crash-loop
    with "exec /engine-entrypoint.sh: no such file or directory" (V1.6.3, found live on a
    Windows checkout). The image COPYs raw working-tree bytes, so git's own normalization
    doesn't protect it -- `.gitattributes` must force LF for these files."""
    attrs = (ROOT / ".gitattributes").read_text(encoding="utf-8")
    for pattern in ("*.sh", "Dockerfile", "*.conf"):
        assert f"{pattern}" in attrs and "eol=lf" in attrs, pattern
    for rel in ("docker/engine-entrypoint.sh", "docker/engine-healthcheck.sh", "Dockerfile",
                "docker/ha/nginx/nginx.conf", "scripts/ha_drill.sh"):
        assert b"\r\n" not in (ROOT / rel).read_bytes(), f"{rel} has CRLF line endings"
