"""v1.3.4 (W0.3): the Helm chart's default `image.repository` pointed at
`ghcr.io/leon1706/aether-vault-engine` -- an org with no such image. Every real publisher
(Dockerfile, release.yml, docker-edge.yml, the release compose file) uses `leon1706-lol`.
`helm-lint` (tests.yml) only schema-verifies the rendered chart (`helm template | kubeconform
-strict`) -- it has no opinion on whether the *values* point at something real, so this drift
was invisible to CI. This module is the stack-free guard against it recurring; PyYAML is
already a `[dev]`/`[docker]` extra (pyproject.toml), never a hard dependency.
"""
import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
VALUES_PATH = REPO_ROOT / "deploy" / "helm" / "aether-vault" / "values.yaml"
CHART_PATH = REPO_ROOT / "deploy" / "helm" / "aether-vault" / "Chart.yaml"
RELEASE_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "release.yml"

# The one real publisher of this image, read straight from the workflow that pushes it --
# so this test fails the moment release.yml's own tags ever change instead of drifting
# silently the way values.yaml just did.
_IMAGE_TAG_RE = re.compile(r"ghcr\.io/([\w.-]+)/aether-vault-engine:latest")


def _published_org() -> str:
    text = RELEASE_WORKFLOW.read_text(encoding="utf-8")
    match = _IMAGE_TAG_RE.search(text)
    assert match, f"{RELEASE_WORKFLOW} has no 'ghcr.io/<org>/aether-vault-engine:latest' tag to compare against"
    return match.group(1)


def _load_values() -> dict:
    yaml = pytest.importorskip("yaml")
    return yaml.safe_load(VALUES_PATH.read_text(encoding="utf-8"))


class TestHelmChartImageMatchesPublisher:
    def test_default_image_repository_is_the_real_published_org(self):
        values = _load_values()
        repository = values["image"]["repository"]
        expected_org = _published_org()
        assert repository == f"ghcr.io/{expected_org}/aether-vault-engine", (
            f"values.yaml's default image.repository ({repository!r}) does not match the "
            f"org release.yml actually publishes to (ghcr.io/{expected_org}/...) — this chart's "
            f"default image would not exist"
        )

    def test_repository_field_is_not_the_stale_org(self):
        # A direct regression guard for the specific incident, independent of whichever org
        # release.yml resolves to today.
        values = _load_values()
        assert values["image"]["repository"] != "ghcr.io/leon1706/aether-vault-engine"


def test_chart_yaml_is_valid_yaml_with_version_fields():
    yaml = pytest.importorskip("yaml")
    chart = yaml.safe_load(CHART_PATH.read_text(encoding="utf-8"))
    assert chart.get("version"), "Chart.yaml is missing a chart 'version'"
    assert chart.get("appVersion"), "Chart.yaml is missing an 'appVersion'"


# --- V1.6.3: per-role resource presets + footprint env knobs ----------------------------

CHART_DIR = VALUES_PATH.parent


def _values():
    yaml = pytest.importorskip("yaml")
    return yaml.safe_load(VALUES_PATH.read_text(encoding="utf-8"))


def test_values_have_per_role_resources_and_footprint_knobs():
    values = _values()
    assert values["resources"] == {}  # empty = use the preset for engine.role
    presets = values["resourcesByRole"]
    assert set(presets) == {"all", "server", "webui"}
    for role, spec in presets.items():
        assert spec["limits"]["memory"] and spec["requests"]["memory"], role
    assert presets["server"]["limits"]["memory"] != presets["all"]["limits"]["memory"]
    engine = values["engine"]
    assert engine["uvicornWorkers"] == 1
    assert engine["limitConcurrency"] == ""
    assert engine["nodeHeapMb"] == 256
    assert engine["maxUploadBytes"] == 0
    assert engine["role"] in presets


def test_deployment_template_wires_the_knobs_and_the_role_preset():
    text = (CHART_DIR / "templates" / "deployment.yaml").read_text(encoding="utf-8")
    for name in ("AV_UVICORN_WORKERS", "AV_UVICORN_LIMIT_CONCURRENCY", "AV_WEBUI_NODE_HEAP_MB", "AV_MAX_UPLOAD_BYTES"):
        assert f"- name: {name}" in text, name
    assert "index .Values.resourcesByRole .Values.engine.role" in text
    assert "{{- if .Values.engine.limitConcurrency }}" in text  # unset stays unset, not ""
