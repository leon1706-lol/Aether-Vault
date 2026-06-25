from python.av_cli.main import load_registry, update_registry, load_config, save_config


def test_load_registry_defaults_when_missing(repo):
    reg = load_registry(repo)
    assert reg == {"tags": [], "metrics": []}


def test_update_registry_merges_and_dedupes(repo):
    update_registry(repo, ["v1"], {"sharpe": 1.5})
    update_registry(repo, ["v1", "v2"], {"sharpe": 2.0, "drawdown": 0.1})

    reg = load_registry(repo)
    assert reg["tags"] == ["v1", "v2"]
    assert reg["metrics"] == ["drawdown", "sharpe"]


def test_load_config_backfills_project_id(repo):
    cfg_path = repo / ".av" / "config"
    cfg_path.write_text('{"lfs_threshold_mb": 50, "remote_url": "http://localhost:8000"}')

    cfg = load_config(repo)
    assert "project_id" in cfg
    assert cfg["project_name"] == repo.name

    # Backfill must persist — a second load shouldn't generate a *different* project_id.
    cfg2 = load_config(repo)
    assert cfg2["project_id"] == cfg["project_id"]


def test_save_config_atomic_no_tmp_file_left_behind(repo):
    cfg = load_config(repo)
    save_config(repo, cfg)

    leftovers = list((repo / ".av").glob("*.tmp.*"))
    assert leftovers == []
