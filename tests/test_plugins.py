import importlib.util
import json

import pytest

from python.av_plugins._shared import build_metric_args, resolve_repo_root, run_av


def _init_repo(tmp_path):
    run_av(tmp_path, ["init"])
    return tmp_path


def test_resolve_repo_root_finds_av_dir(tmp_path):
    repo_root = _init_repo(tmp_path)
    nested = repo_root / "checkpoints" / "epoch1"
    nested.mkdir(parents=True)
    assert resolve_repo_root(nested) == repo_root


def test_resolve_repo_root_raises_outside_repo(tmp_path):
    from av_cli.exceptions import AetherVaultException

    with pytest.raises(AetherVaultException):
        resolve_repo_root(tmp_path)


def test_build_metric_args_filters_non_numeric_and_bools():
    args = build_metric_args({"loss": 0.42, "epoch": 3, "is_best": True, "name": "run-1"})
    assert args == ["--metric", "loss=0.42", "--metric", "epoch=3"]


def test_run_av_add_and_commit_checkpoint(tmp_path):
    repo_root = _init_repo(tmp_path)
    ckpt = repo_root / "model.pt"
    ckpt.write_text("dummy weights")

    run_av(repo_root, ["add", str(ckpt)])
    run_av(repo_root, ["commit", "-m", "step=1", "--metric", "loss=0.5", "--tag", "auto"])

    commits_dir = repo_root / ".av" / "commits"
    commit_files = list(commits_dir.glob("*.json"))
    assert len(commit_files) == 1
    commit_data = json.loads(commit_files[0].read_text())
    assert commit_data["message"] == "step=1"
    assert commit_data["metrics"] == {"loss": 0.5}
    assert commit_data["tags"] == ["auto"]
    assert "model.pt" in commit_data["tree"]


def test_run_av_commit_with_no_changes_does_not_raise(tmp_path):
    repo_root = _init_repo(tmp_path)
    ckpt = repo_root / "model.pt"
    ckpt.write_text("dummy weights")
    run_av(repo_root, ["add", str(ckpt)])
    run_av(repo_root, ["commit", "-m", "first"])

    # Nothing staged the second time around -- must hit the "Nothing to
    # commit" path in av_cli/main.py, not raise.
    run_av(repo_root, ["commit", "-m", "second"])

    commits_dir = repo_root / ".av" / "commits"
    assert len(list(commits_dir.glob("*.json"))) == 1


_HAS_LIGHTNING = importlib.util.find_spec("lightning") is not None or importlib.util.find_spec("pytorch_lightning") is not None
_HAS_TRANSFORMERS = importlib.util.find_spec("transformers") is not None


@pytest.mark.skipif(_HAS_LIGHTNING, reason="lightning is installed; ImportError path not exercised")
def test_lightning_plugin_raises_clear_importerror_when_missing():
    with pytest.raises(ImportError, match="aether-vault\\[lightning\\]"):
        import python.av_plugins.lightning  # noqa: F401


@pytest.mark.skipif(_HAS_TRANSFORMERS, reason="transformers is installed; ImportError path not exercised")
def test_transformers_plugin_raises_clear_importerror_when_missing():
    with pytest.raises(ImportError, match="aether-vault\\[transformers\\]"):
        import python.av_plugins.transformers  # noqa: F401


@pytest.mark.skipif(not _HAS_LIGHTNING, reason="lightning not installed")
def test_lightning_callback_commits_checkpoint(tmp_path):
    from python.av_plugins.lightning import AetherVaultCallback

    repo_root = _init_repo(tmp_path)
    ckpt_path = repo_root / "best.ckpt"
    ckpt_path.write_text("dummy")

    class FakeCheckpointCallback:
        best_model_path = str(ckpt_path)
        last_model_path = ""

    class FakeTrainer:
        checkpoint_callback = FakeCheckpointCallback()
        current_epoch = 0
        global_step = 10
        callback_metrics = {"loss": 0.1}

    cb = AetherVaultCallback(tag="unit-test")
    cb.on_save_checkpoint(FakeTrainer(), None, {})

    commits_dir = repo_root / ".av" / "commits"
    assert len(list(commits_dir.glob("*.json"))) == 1


@pytest.mark.skipif(not _HAS_TRANSFORMERS, reason="transformers not installed")
def test_transformers_callback_commits_checkpoint(tmp_path):
    from python.av_plugins.transformers import AetherVaultTrainerCallback

    repo_root = _init_repo(tmp_path)
    ckpt_dir = repo_root / "out" / "checkpoint-5"
    ckpt_dir.mkdir(parents=True)
    (ckpt_dir / "model.safetensors").write_text("dummy")

    class FakeArgs:
        output_dir = str(repo_root / "out")

    class FakeState:
        global_step = 5
        log_history = [{"loss": 0.2, "epoch": 1.0}]

    cb = AetherVaultTrainerCallback(tag="unit-test")
    cb.on_save(FakeArgs(), FakeState(), None)

    commits_dir = repo_root / ".av" / "commits"
    assert len(list(commits_dir.glob("*.json"))) == 1
