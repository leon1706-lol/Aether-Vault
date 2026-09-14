"""`scripts/rss_scoreboard.py` -- the before/after evidence for the V1.6.3 footprint phase."""
import importlib.util
import json
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "rss_scoreboard.py"


def _load():
    spec = importlib.util.spec_from_file_location("rss_scoreboard", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_synthetic_safetensors_parses_with_aether_core(tmp_path):
    aether_core = pytest.importorskip("aether_core")
    mod = _load()
    path = tmp_path / "m.safetensors"
    mod.write_synthetic_safetensors(path, n_layers=3, layer_bytes=64 * 1024)
    layers = aether_core.split_and_hash_safetensors(str(path))
    names = {layer["name"] for layer in layers}
    assert {"layer_0.weight", "layer_1.weight", "layer_2.weight"} <= names
    assert sum(1 for layer in layers if layer["name"] != "__header__") == 3
    assert all(layer["size"] == 64 * 1024 for layer in layers if layer["name"].startswith("layer_"))


def test_synthetic_safetensors_layers_are_unique(tmp_path):
    mod = _load()
    path = tmp_path / "m.safetensors"
    mod.write_synthetic_safetensors(path, n_layers=2, layer_bytes=4096)
    raw = path.read_bytes()
    header_len = int.from_bytes(raw[:8], "little")
    payload = raw[8 + header_len:]
    assert payload[:4096] != payload[4096:8192]


def test_docker_stats_parser():
    mod = _load()
    assert mod.parse_mem_usage("123.4MiB / 7.6GiB") == 123.4
    assert mod.parse_mem_usage("1.2GiB / 7.6GiB") == 1228.8
    assert mod.parse_mem_usage("512kB / 1GiB") == 0.5
    assert mod.parse_mem_usage("garbage") is None


def test_scoreboard_json_schema_and_markdown_render(tmp_path, monkeypatch):
    mod = _load()
    fake = mod.sysres.MeasuredRun(0, 12.5, 42.0, "", "", "fake")
    monkeypatch.setattr(mod.sysres, "run_measured", lambda *a, **k: fake)
    out_json = tmp_path / "s.json"
    out_md = tmp_path / "s.md"
    rc = mod.main(["--out-json", str(out_json), "--out-md", str(out_md), "--scenarios", "status_cold",
                   "--runs", "2", "--files", "3", "--label", "unit"])
    assert rc == 0
    doc = json.loads(out_json.read_text(encoding="utf-8"))
    assert doc["schema"] == "rss-scoreboard-1.0"
    assert doc["label"] == "unit"
    assert set(doc["scenarios"]) == {"status_cold"}
    row = doc["scenarios"]["status_cold"]
    assert row["peak_rss_mb"] == 42.0 and row["runs"] == 2 and row["source"] == "fake"
    md = out_md.read_text(encoding="utf-8")
    assert "| status_cold | 42.0 | 42.0 | 12 ms | fake |" in md


def test_unknown_scenario_is_rejected(tmp_path):
    mod = _load()
    with pytest.raises(SystemExit):
        mod.main(["--out-json", str(tmp_path / "x.json"), "--scenarios", "nope"])


def test_docker_rows_are_na_without_docker(tmp_path, monkeypatch):
    mod = _load()
    monkeypatch.setattr(mod, "docker_stats", lambda: None)
    board = mod.Scoreboard("av", 1, 1, 1, 1, echo=lambda *a: None)
    try:
        board.scenario_docker({"engine_idle", "webui_idle"})
    finally:
        board.cleanup()
    assert board.rows["engine_idle"]["peak_rss_mb"] is None
    assert board.rows["engine_idle"]["source"] == "n/a"
    assert board.rows["webui_idle"]["source"] == "n/a"


@pytest.mark.skipif(sys.platform == "win32" and not Path(sys.executable).exists(), reason="no interpreter")
def test_real_status_cold_row_in_scratch_repo(tmp_path):
    """One real `av status` measured end-to-end (small repo, one run) -- proves the script
    drives the actual CLI, not just its own stubs."""
    import shutil

    av = shutil.which("av")
    if av is None:
        pytest.skip("av not on PATH")
    mod = _load()
    out_json = tmp_path / "real.json"
    rc = mod.main(["--out-json", str(out_json), "--scenarios", "status_cold", "--runs", "1", "--files", "5",
                   "--av", av])
    assert rc == 0
    row = json.loads(out_json.read_text(encoding="utf-8"))["scenarios"]["status_cold"]
    assert row["runs"] == 1
    assert row["peak_rss_mb"] is None or row["peak_rss_mb"] > 5
