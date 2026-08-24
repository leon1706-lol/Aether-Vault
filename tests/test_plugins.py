import importlib.util
import json

import pytest

from python.av_plugins._shared import build_metric_args, resolve_repo_root, run_av


def _init_repo(tmp_path):
    run_av(tmp_path, ["init", "--mode", "local", "--yes", "--no-repl"])
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
_HAS_MLFLOW = importlib.util.find_spec("mlflow") is not None


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


@pytest.mark.skipif(not _HAS_LIGHTNING, reason="lightning not installed")
def test_lightning_callback_commits_dataset_on_train_start(tmp_path):
    from python.av_plugins.lightning import AetherVaultCallback

    repo_root = _init_repo(tmp_path)
    dataset_path = repo_root / "train.parquet"
    dataset_path.write_text("dummy dataset")

    cb = AetherVaultCallback(dataset_paths=str(dataset_path), tag="unit-test")
    cb.on_train_start(trainer=None, pl_module=None)

    commits_dir = repo_root / ".av" / "commits"
    commit_files = list(commits_dir.glob("*.json"))
    assert len(commit_files) == 1
    commit_data = json.loads(commit_files[0].read_text())
    assert commit_data["tags"] == ["dataset"]
    assert "train.parquet" in commit_data["tree"]


@pytest.mark.skipif(not _HAS_TRANSFORMERS, reason="transformers not installed")
def test_transformers_callback_commits_dataset_on_train_begin(tmp_path):
    from python.av_plugins.transformers import AetherVaultTrainerCallback

    repo_root = _init_repo(tmp_path)
    dataset_path = repo_root / "train.csv"
    dataset_path.write_text("dummy dataset")

    cb = AetherVaultTrainerCallback(dataset_paths=str(dataset_path), tag="unit-test")
    cb.on_train_begin(args=None, state=None, control=None)

    commits_dir = repo_root / ".av" / "commits"
    commit_files = list(commits_dir.glob("*.json"))
    assert len(commit_files) == 1
    commit_data = json.loads(commit_files[0].read_text())
    assert commit_data["tags"] == ["dataset"]
    assert "train.csv" in commit_data["tree"]


@pytest.mark.skipif(not _HAS_LIGHTNING, reason="lightning not installed")
def test_lightning_import_checkpoint(tmp_path):
    from python.av_plugins.lightning import import_checkpoint

    repo_root = _init_repo(tmp_path)
    ckpt_path = repo_root / "old_run" / "epoch=4.ckpt"
    ckpt_path.parent.mkdir(parents=True)
    ckpt_path.write_text("dummy checkpoint bytes")

    import_checkpoint(str(ckpt_path), repo_root=repo_root, tag="backfill", metrics={"loss": 0.3})

    commits_dir = repo_root / ".av" / "commits"
    commit_files = list(commits_dir.glob("*.json"))
    assert len(commit_files) == 1
    commit_data = json.loads(commit_files[0].read_text())
    assert "lightning-import" in commit_data["tags"]
    assert "backfill" in commit_data["tags"]
    assert commit_data["metrics"] == {"loss": 0.3}

    # Re-importing the same, unchanged checkpoint must be a no-op (hits "Nothing to commit"),
    # not raise and not create a second, empty commit.
    import_checkpoint(str(ckpt_path), repo_root=repo_root, tag="backfill", metrics={"loss": 0.3})
    assert len(list(commits_dir.glob("*.json"))) == 1


@pytest.mark.skipif(not _HAS_TRANSFORMERS, reason="transformers not installed")
def test_transformers_import_checkpoint(tmp_path):
    from python.av_plugins.transformers import import_checkpoint

    repo_root = _init_repo(tmp_path)
    ckpt_dir = repo_root / "old_run" / "checkpoint-100"
    ckpt_dir.mkdir(parents=True)
    (ckpt_dir / "model.safetensors").write_text("dummy")
    (ckpt_dir / "trainer_state.json").write_text(json.dumps({"log_history": [{"loss": 0.7}]}))

    import_checkpoint(str(ckpt_dir), repo_root=repo_root, tag="backfill")

    commits_dir = repo_root / ".av" / "commits"
    commit_files = list(commits_dir.glob("*.json"))
    assert len(commit_files) == 1
    commit_data = json.loads(commit_files[0].read_text())
    assert "transformers-import" in commit_data["tags"]
    assert commit_data["metrics"] == {"loss": 0.7}


@pytest.mark.skipif(_HAS_MLFLOW, reason="mlflow is installed; ImportError path not exercised")
def test_mlflow_plugin_raises_clear_importerror_when_missing():
    with pytest.raises(ImportError, match="aether-vault\\[mlflow\\]"):
        import python.av_plugins.mlflow  # noqa: F401


@pytest.mark.skipif(not _HAS_MLFLOW, reason="mlflow not installed")
def test_mlflow_import_run(tmp_path, monkeypatch):
    import mlflow

    from python.av_plugins.mlflow import import_run

    # The sqlite tracking URI below only covers run *metadata* -- MLflow still defaults
    # *artifact* storage to "./mlruns" relative to the process cwd, which would otherwise
    # pollute the real repo root (pytest's cwd) instead of staying inside tmp_path.
    monkeypatch.chdir(tmp_path)

    repo_root = _init_repo(tmp_path)
    # MLflow 3.x put the legacy filesystem ("file:///...") backend into maintenance mode and
    # refuses it by default; a sqlite-backed tracking store is the supported path going forward.
    tracking_uri = f"sqlite:///{tmp_path / 'mlflow.db'}"
    mlflow.set_tracking_uri(tracking_uri)

    artifact_src = tmp_path / "artifact_source"
    artifact_src.mkdir()
    (artifact_src / "model.txt").write_text("dummy model artifact")

    with mlflow.start_run() as run:
        mlflow.log_metric("accuracy", 0.91)
        mlflow.log_param("optimizer", "adam")
        mlflow.log_artifacts(str(artifact_src))
        run_id = run.info.run_id

    import_run(run_id, repo_root=repo_root, tracking_uri=tracking_uri, tag="backfill")

    commits_dir = repo_root / ".av" / "commits"
    commit_files = list(commits_dir.glob("*.json"))
    assert len(commit_files) == 1
    commit_data = json.loads(commit_files[0].read_text())
    assert "mlflow-import" in commit_data["tags"]
    assert "backfill" in commit_data["tags"]
    assert "optimizer=adam" in commit_data["tags"]
    assert commit_data["metrics"] == {"accuracy": 0.91}


@pytest.mark.skipif(not _HAS_MLFLOW, reason="mlflow not installed")
def test_mlflow_import_run_raises_when_no_artifacts(tmp_path, monkeypatch):
    import mlflow

    from python.av_plugins.mlflow import import_run

    monkeypatch.chdir(tmp_path)

    repo_root = _init_repo(tmp_path)
    tracking_uri = f"sqlite:///{tmp_path / 'mlflow.db'}"
    mlflow.set_tracking_uri(tracking_uri)

    with mlflow.start_run() as run:
        mlflow.log_metric("accuracy", 0.5)
        run_id = run.info.run_id

    from av_cli.exceptions import AetherVaultException

    with pytest.raises(AetherVaultException):
        import_run(run_id, repo_root=repo_root, tracking_uri=tracking_uri)


# ---------------------------------------------------------------------------
# Scoped plugin commits (Probleme.md #38 fix): imports/callbacks must commit ONLY
# their own paths — unrelated staged work neither enters the commit nor loses its
# pending state.
# ---------------------------------------------------------------------------

def _stage_unrelated(repo_root):
    """A file the user staged for their OWN next commit, before any import runs."""
    unrelated = repo_root / "notes.py"
    unrelated.write_text("user's in-progress work")
    run_av(repo_root, ["add", str(unrelated)])
    return unrelated


def _commit_trees(repo_root):
    commits_dir = repo_root / ".av" / "commits"
    return [json.loads(p.read_text()) for p in commits_dir.glob("*.json")]


def test_commit_scoped_commits_only_target_paths(tmp_path):
    from python.av_plugins._shared import commit_scoped

    repo_root = _init_repo(tmp_path)
    unrelated = _stage_unrelated(repo_root)
    ckpt = repo_root / "model.pt"
    ckpt.write_text("dummy weights")

    commit_scoped(repo_root, [str(ckpt)], ["commit", "-m", "imported checkpoint"])

    trees = _commit_trees(repo_root)
    assert len(trees) == 1
    assert "model.pt" in trees[0]["tree"]
    assert "notes.py" not in trees[0]["tree"], \
        "the import swept the user's unrelated staged file into its commit (#38)"

    # ...and the unrelated file keeps its pending state for the user's own commit.
    from av_cli.index import Index
    idx = Index(repo_root)
    assert idx.get_entry("notes.py")["staged"] is True


def test_commit_scoped_restore_survives_add_failure(tmp_path):
    from python.av_plugins._shared import commit_scoped

    repo_root = _init_repo(tmp_path)
    unrelated = _stage_unrelated(repo_root)

    with pytest.raises(Exception):
        commit_scoped(repo_root, [str(repo_root / "does-not-exist.pt")], ["commit", "-m", "x"])

    # Nothing committed, and the user's staging area is byte-identical to before.
    assert len(_commit_trees(repo_root)) == 0
    from av_cli.index import Index
    idx = Index(repo_root)
    assert idx.get_entry("notes.py")["staged"] is True


@pytest.mark.skipif(not _HAS_LIGHTNING, reason="lightning not installed")
def test_lightning_checkpoint_commit_does_not_sweep_staged_files(tmp_path):
    from python.av_plugins.lightning import AetherVaultCallback

    repo_root = _init_repo(tmp_path)
    unrelated = _stage_unrelated(repo_root)
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

    trees = _commit_trees(repo_root)
    assert len(trees) == 1
    assert "best.ckpt" in trees[0]["tree"]
    assert "notes.py" not in trees[0]["tree"]

    from av_cli.index import Index
    idx = Index(repo_root)
    assert idx.get_entry("notes.py")["staged"] is True


def test_commit_scoped_reimport_is_a_noop(tmp_path):
    """Regression for Probleme.md #71: scoping must not destroy the change-detection
    baseline. The first version of commit_scoped emptied the index before running
    `add`, so a re-import of unchanged content looked brand-new and produced a
    duplicate commit instead of the documented "Nothing to commit" no-op. Caught by
    CI's plugin job (extras installed); framework-free here so it always runs.
    """
    from python.av_plugins._shared import commit_scoped

    repo_root = _init_repo(tmp_path)
    ckpt = repo_root / "model.pt"
    ckpt.write_text("dummy weights")

    args = ["commit", "-m", "imported checkpoint", "--tag", "lightning-import"]
    commit_scoped(repo_root, [str(ckpt)], args)
    commit_scoped(repo_root, [str(ckpt)], args)

    trees = _commit_trees(repo_root)
    assert len(trees) == 1, "re-importing unchanged content created a second commit"

    # A content CHANGE under the same path must still produce exactly one new commit.
    ckpt.write_text("updated weights v2")
    commit_scoped(repo_root, [str(ckpt)], ["commit", "-m", "imported checkpoint v2"])
    trees = _commit_trees(repo_root)
    assert len(trees) == 2
    assert any("model.pt" in t["tree"] and "v2" in t["message"] for t in trees)


def test_commit_scoped_keeps_directory_targets_together(tmp_path):
    """Transformers imports stage whole checkpoint DIRECTORIES — every file inside must
    land in one scoped commit while unrelated staged files stay out."""
    from python.av_plugins._shared import commit_scoped

    repo_root = _init_repo(tmp_path)
    unrelated = _stage_unrelated(repo_root)
    ckpt_dir = repo_root / "checkpoint-5"
    ckpt_dir.mkdir()
    (ckpt_dir / "model.safetensors").write_text("weights")
    (ckpt_dir / "trainer_state.json").write_text("{}")

    commit_scoped(
        repo_root,
        [str(ckpt_dir)],
        ["commit", "-m", "Imported Transformers checkpoint checkpoint-5"],
    )

    trees = _commit_trees(repo_root)
    assert len(trees) == 1
    assert any(p.startswith("checkpoint-5") for p in trees[0]["tree"])
    assert not any(p == "notes.py" for p in trees[0]["tree"])

    from av_cli.index import Index
    idx = Index(repo_root)
    assert idx.get_entry("notes.py")["staged"] is True


@pytest.mark.skipif(not _HAS_LIGHTNING, reason="lightning not installed")
def test_lightning_real_training_loop_smoke(tmp_path):
    """A REAL one-epoch Lightning training run (tiny CPU model, real Trainer) through
    AetherVaultCallback — the fake-trainer tests above prove callback logic, this proves
    the actual framework wiring: on_save_checkpoint firing from Lightning's own
    checkpoint machinery, real metrics flowing into --metric flags, and the scoped
    commit landing tagged. Kept tiny (~seconds): 8 samples, batch 4, CPU, no logging.

    Ordering note (Probleme.md #76): Lightning fires on_save_checkpoint BEFORE writing
    the file, so the callback skips not-yet-existing paths and catches them on a LATER
    save event. The test therefore drives TWO explicit saves afterwards — by the second
    hook invocation the first checkpoint exists on disk and is committed."""
    import torch
    import torch.nn as nn
    from lightning.pytorch import LightningModule, Trainer
    from torch.utils.data import DataLoader, TensorDataset

    from python.av_plugins.lightning import AetherVaultCallback

    repo_root = _init_repo(tmp_path)
    ckpt_dir = tmp_path / "ckpts"
    ckpt_dir.mkdir()

    class Tiny(LightningModule):
        def __init__(self):
            super().__init__()
            self.layer = nn.Linear(4, 1)

        def forward(self, x):
            return self.layer(x).squeeze(-1)

        def training_step(self, batch, _idx):
            x, y = batch
            loss = ((self(x) - y) ** 2).mean()
            self.log("train_loss", loss)
            return loss

        def configure_optimizers(self):
            return torch.optim.SGD(self.parameters(), lr=0.01)

    ds = TensorDataset(torch.randn(8, 4), torch.randn(8))
    callback = AetherVaultCallback(tag="real-loop-smoke")
    trainer = Trainer(
        max_epochs=1,
        accelerator="cpu",
        logger=False,
        enable_progress_bar=False,
        enable_model_summary=False,
        callbacks=[callback],
        limit_train_batches=2,
    )
    model = Tiny()
    trainer.fit(model, train_dataloaders=DataLoader(ds, batch_size=4))

    # Two explicit saves: the first write lands AFTER its hook ran; the second hook
    # therefore finds a real file on disk and stages it.
    trainer.save_checkpoint(str(ckpt_dir / "manual-a.ckpt"))
    trainer.save_checkpoint(str(ckpt_dir / "manual-b.ckpt"))

    trees = _commit_trees(repo_root)
    assert len(trees) >= 1, "real training loop produced no commit via the callback"
    committed_files = [p for t in trees for p in t["tree"]]
    assert any(p.endswith(".ckpt") for p in committed_files), \
        f"no .ckpt among committed files: {committed_files}"
    smoke = [t for t in trees if "real-loop-smoke" in t["tags"]]
    assert smoke, "callback tag missing from the real-loop commit(s)"
    # Real Lightning metrics (train_loss) must ride the commit as metrics, not just tags.
    assert any("train_loss" in t["metrics"] for t in trees), \
        f"expected train_loss metric, got: {[t['metrics'] for t in trees]}"


def test_lightning_save_hook_before_file_write_is_survivable(tmp_path):
    """Regression for Probleme.md #76, framework-free so it always runs: Lightning fires
    on_save_checkpoint BEFORE the checkpoint file hits disk (the first v1.1.11 CI run
    crashed `av add` with FileNotFoundError inside the real training loop). Path
    resolution must skip not-yet-existing files — the helper lives in _shared so this
    assertion runs without framework extras."""
    from python.av_plugins._shared import filter_existing_files

    ckpt = tmp_path / "ckpts" / "epoch=0-step=2.ckpt"
    ckpt.parent.mkdir()
    ghost = tmp_path / "nope.pt"

    assert filter_existing_files([str(ckpt), str(ghost)]) == []
    ckpt.write_text("weights")
    assert filter_existing_files([str(ckpt), str(ghost)]) == [str(ckpt)]


@pytest.mark.skipif(not _HAS_LIGHTNING, reason="lightning not installed")
def test_lightning_callback_skips_unwritten_checkpoint_then_commits_it(tmp_path):
    """Callback-level half of #76 — runs in CI's plugin job where Lightning exists."""
    from python.av_plugins.lightning import AetherVaultCallback

    repo_root = _init_repo(tmp_path)
    ckpt = repo_root / "ckpts" / "epoch=0-step=2.ckpt"
    ckpt.parent.mkdir()

    class FakeCkptCb:
        best_model_path = str(ckpt)
        last_model_path = ""

    class FakeTrainer:
        checkpoint_callback = FakeCkptCb()
        current_epoch = 0
        global_step = 2
        callback_metrics = {"train_loss": 0.25}

    cb = AetherVaultCallback(tag="race-smoke")

    # Hook fires while the file does NOT exist yet (Lightning's real ordering):
    cb.on_save_checkpoint(FakeTrainer(), None, {})
    assert len(_commit_trees(repo_root)) == 0, "must not stage a not-yet-written checkpoint"

    # File lands; the NEXT save event picks it up:
    ckpt.write_text("weights")
    cb.on_save_checkpoint(FakeTrainer(), None, {})
    trees = _commit_trees(repo_root)
    assert any(
        "epoch=0-step=2.ckpt" in rel for t in trees for rel in t["tree"]
    ), f"existing checkpoint never committed: {[list(t['tree']) for t in trees]}"
