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


def test_run_av_commit_with_no_changes_raises_nothing_to_commit(tmp_path):
    repo_root = _init_repo(tmp_path)
    ckpt = repo_root / "model.pt"
    ckpt.write_text("dummy weights")
    run_av(repo_root, ["add", str(ckpt)])
    run_av(repo_root, ["commit", "-m", "first"])

    # v1.2.5: nothing staged the second time around now hits fail("nothing_to_commit")
    # -> SystemExit(11) (see tests/test_exit_codes.py), matching the documented registry
    # — pre-1.2.5 this exited 0/didn't raise, which is what this test used to assert.
    # run_av's cli.main(standalone_mode=False) doesn't catch SystemExit (unlike
    # CliRunner), so it propagates here exactly as it would to any real caller.
    with pytest.raises(SystemExit) as exc:
        run_av(repo_root, ["commit", "-m", "second"])
    assert exc.value.code == 11

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

    # v1.2.2: commit_scoped takes message/tags/metrics directly — it drives the internal
    # seam (core.commit_scoped_paths), not the CLI.
    commit_scoped(repo_root, [str(ckpt)], "imported checkpoint")

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

    # v1.2.2 seam semantics: a missing path is SKIPPED (Lightning's before-write hook
    # ordering, Probleme.md #76) — so this neither raises nor commits anything, and the
    # user's staging area is byte-identical to before.
    commit_scoped(repo_root, [str(repo_root / "does-not-exist.pt")], "x")

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

    commit_scoped(repo_root, [str(ckpt)], "imported checkpoint", tags=("lightning-import",))
    commit_scoped(repo_root, [str(ckpt)], "imported checkpoint", tags=("lightning-import",))

    trees = _commit_trees(repo_root)
    assert len(trees) == 1, "re-importing unchanged content created a second commit"

    # A content CHANGE under the same path must still produce exactly one new commit.
    ckpt.write_text("updated weights v2")
    commit_scoped(repo_root, [str(ckpt)], "imported checkpoint v2")
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
        "Imported Transformers checkpoint checkpoint-5",
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
        # Lightning auto-adds its own ModelCheckpoint when none is supplied, writing to
        # default_root_dir/checkpoints — which defaults to CWD (the checkout root, where
        # there is no .av repo). Root the trainer INSIDE the av repo so the callback's
        # resolve_repo_root finds it.
        default_root_dir=str(repo_root),
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


# ---------------------------------------------------------------------------
# v1.2.2 seam migration: parity between the plugin seam, the SDK, and the CLI,
# plus AV_RUN_ID / metrics flow through core.commit_scoped_paths.
# ---------------------------------------------------------------------------

def test_commit_scoped_paths_resolves_ambient_run_id(tmp_path, monkeypatch):
    """AV_RUN_ID must flow into machine commits exactly like CLI commits (zero
    integration required for training loops orchestrated by agents)."""
    from python.av_cli.core import commit_scoped_paths

    repo_root = _init_repo(tmp_path)
    ckpt = repo_root / "model.pt"
    ckpt.write_text("weights")

    monkeypatch.setenv("AV_RUN_ID", "run-abc-123")
    commit_scoped_paths(repo_root, [str(ckpt)], "scoped under ambient run")

    trees = _commit_trees(repo_root)
    assert len(trees) == 1
    assert "run:run-abc-123" in trees[0]["tags"], \
        f"ambient AV_RUN_ID not applied by the seam: {trees[0]['tags']}"


def test_explicit_run_id_beats_env_in_the_seam(tmp_path, monkeypatch):
    from python.av_cli.core import commit_scoped_paths

    repo_root = _init_repo(tmp_path)
    ckpt = repo_root / "model.pt"
    ckpt.write_text("w")
    monkeypatch.setenv("AV_RUN_ID", "from-env")

    commit_scoped_paths(repo_root, [str(ckpt)], "explicit", run_id="explicit-run")
    trees = _commit_trees(repo_root)
    assert any(t.startswith("run:explicit-run") for t in trees[0]["tags"])
    assert not any(t == "run:from-env" for t in trees[0]["tags"])


def test_metrics_flow_through_plugin_seam(tmp_path):
    """Numeric metrics ride machine commits as first-class metrics (not tags), matching
    what the real Lightning/Transformers callbacks attach."""
    from python.av_plugins._shared import commit_scoped

    repo_root = _init_repo(tmp_path)
    ckpt = repo_root / "model.pt"
    ckpt.write_text("w")

    commit_scoped(repo_root, [str(ckpt)], "epoch=3",
                  tags=("unit-test",), metrics={"train_loss": 0.25, "epoch": 3})

    trees = _commit_trees(repo_root)
    assert trees[0]["metrics"] == {"train_loss": 0.25, "epoch": 3}
    assert "unit-test" in trees[0]["tags"]


def test_seam_parity_plugin_vs_sdk_vs_cli(tmp_path, monkeypatch):
    """All three surfaces drive ONE writer: the resulting commit payloads carry the same
    structural fields regardless of which entry point produced them."""
    import json as json_mod

    from av_sdk import Repo
    from python.av_cli.core import commit_scoped_paths

    def _tree_of(commit_file):
        data = json_mod.loads(commit_file.read_text())
        return {
            "parents": data["parents"],
            "has_hash": bool(data.get("hash")),
            "tree_keys": sorted(data["tree"]),
            "metrics": data.get("metrics") or {},
            "tags": sorted(t for t in data.get("tags", []) if not t.startswith("run:")),
            "author": data.get("author"),
        }

    # Plugin seam:
    seam_repo = _init_repo(_mkdir(tmp_path / "seam"))
    (seam_repo / "artifact.bin").write_bytes(b"x")
    commit_scoped_paths(seam_repo, ["artifact.bin"], "via seam",
                        tags=("parity",), metrics={"loss": 0.1})
    seam_tree = _tree_of(next(iter((seam_repo / ".av" / "commits").glob("*.json"))))

    # SDK:
    sdk_repo = _init_repo(_mkdir(tmp_path / "sdk"))
    with Repo(sdk_repo) as r:
        (r.path / "artifact.bin").write_bytes(b"x")
        r.add("artifact.bin")
        r.commit("via sdk", tags=["parity"], metrics={"loss": 0.1})
    sdk_tree = _tree_of(next(iter((sdk_repo / ".av" / "commits").glob("*.json"))))

    # CLI:
    cli_repo = _mkdir(tmp_path / "cli")
    _init_repo(cli_repo)
    (cli_repo / "artifact.bin").write_bytes(b"x")
    run_av(cli_repo, ["add", "artifact.bin"])
    run_av(cli_repo, ["commit", "-m", "via cli", "--tag", "parity",
                      "--metric", "loss=0.1"])
    cli_tree = _tree_of(next(iter((cli_repo / ".av" / "commits").glob("*.json"))))

    for field in ("has_hash", "tree_keys", "metrics", "tags"):
        assert seam_tree[field] == sdk_tree[field] == cli_tree[field], \
            f"parity broken on {field}: seam={seam_tree[field]} " \
            f"sdk={sdk_tree[field]} cli={cli_tree[field]}"

    # v1.2.5: schema parity — not just matching VALUES on a hand-picked field subset,
    # but the same PAYLOAD KEY SET across all three surfaces (catches one surface
    # silently gaining/losing a top-level field the others don't have).
    def _payload_keys(repo):
        raw = json_mod.loads(next(iter((repo / ".av" / "commits").glob("*.json"))).read_text())
        return set(raw.keys())

    seam_keys = _payload_keys(seam_repo)
    sdk_keys = _payload_keys(sdk_repo)
    cli_keys = _payload_keys(cli_repo)
    assert seam_keys == sdk_keys == cli_keys, \
        f"payload schema drift: seam={seam_keys ^ sdk_keys ^ cli_keys}"


def test_seam_parity_run_id_linkage(tmp_path, monkeypatch):
    """v1.2.5: the same AV_RUN_ID produces the same run: tag + run_id field across all
    three surfaces (previously only asserted for the seam alone)."""
    import json as json_mod

    from av_sdk import Repo
    from python.av_cli.core import commit_scoped_paths

    monkeypatch.setenv("AV_RUN_ID", "parity-run-xyz")

    def _run_fields(commit_file):
        data = json_mod.loads(commit_file.read_text())
        return {"run_id": data.get("run_id"),
                "run_tag": next((t for t in data.get("tags", []) if t.startswith("run:")), None)}

    seam_repo = _init_repo(_mkdir(tmp_path / "seam-run"))
    (seam_repo / "artifact.bin").write_bytes(b"x")
    commit_scoped_paths(seam_repo, ["artifact.bin"], "via seam")
    seam_run = _run_fields(next(iter((seam_repo / ".av" / "commits").glob("*.json"))))

    sdk_repo = _init_repo(_mkdir(tmp_path / "sdk-run"))
    with Repo(sdk_repo) as r:
        (r.path / "artifact.bin").write_bytes(b"x")
        r.add("artifact.bin")
        r.commit("via sdk")
    sdk_run = _run_fields(next(iter((sdk_repo / ".av" / "commits").glob("*.json"))))

    cli_repo = _mkdir(tmp_path / "cli-run")
    _init_repo(cli_repo)
    (cli_repo / "artifact.bin").write_bytes(b"x")
    run_av(cli_repo, ["add", "artifact.bin"])
    run_av(cli_repo, ["commit", "-m", "via cli"])
    cli_run = _run_fields(next(iter((cli_repo / ".av" / "commits").glob("*.json"))))

    assert seam_run == sdk_run == cli_run == {
        "run_id": "parity-run-xyz", "run_tag": "run:parity-run-xyz",
    }


def test_seam_parity_env_snapshot_id(tmp_path):
    """v1.2.5: env_snapshot_id, when a snapshot exists, rides the commit identically
    across all three surfaces."""
    import json as json_mod

    from av_sdk import Repo
    from python.av_cli.core import commit_scoped_paths

    def _write_snapshot(repo):
        from python.av_cli.core import env_snapshot_file

        env_snapshot_file(repo).write_text(json_mod.dumps({
            "snapshot_version": 2, "captured_at": "2026-01-01T00:00:00+00:00",
            "python": "3.12.0", "env": {"python": "3.12.0", "pins": {}, "seeds": {}},
        }))

    def _sid(commit_file):
        return json_mod.loads(commit_file.read_text()).get("env_snapshot_id")

    seam_repo = _init_repo(_mkdir(tmp_path / "seam-env"))
    _write_snapshot(seam_repo)
    (seam_repo / "artifact.bin").write_bytes(b"x")
    commit_scoped_paths(seam_repo, ["artifact.bin"], "via seam")
    seam_sid = _sid(next(iter((seam_repo / ".av" / "commits").glob("*.json"))))
    assert seam_sid  # sanity: the snapshot actually rode the commit

    sdk_repo = _init_repo(_mkdir(tmp_path / "sdk-env"))
    _write_snapshot(sdk_repo)
    with Repo(sdk_repo) as r:
        (r.path / "artifact.bin").write_bytes(b"x")
        r.add("artifact.bin")
        r.commit("via sdk")
    sdk_sid = _sid(next(iter((sdk_repo / ".av" / "commits").glob("*.json"))))

    cli_repo = _mkdir(tmp_path / "cli-env")
    _init_repo(cli_repo)
    _write_snapshot(cli_repo)
    (cli_repo / "artifact.bin").write_bytes(b"x")
    run_av(cli_repo, ["add", "artifact.bin"])
    run_av(cli_repo, ["commit", "-m", "via cli"])
    cli_sid = _sid(next(iter((cli_repo / ".av" / "commits").glob("*.json"))))

    assert seam_sid == sdk_sid == cli_sid


def test_seam_parity_queued_when_server_unreachable(tmp_path):
    """v1.2.5: all three surfaces queue (never fail/lose the commit) identically when
    the registry is unreachable — matches the queued/queued_reason contract."""
    from av_sdk import Repo
    from python.av_cli.core import commit_scoped_paths, load_pending_push

    seam_repo = _init_repo(_mkdir(tmp_path / "seam-q"))
    (seam_repo / "artifact.bin").write_bytes(b"x")
    commit_scoped_paths(seam_repo, ["artifact.bin"], "via seam")
    assert load_pending_push(seam_repo), "seam commit should have queued (server unreachable)"

    sdk_repo = _init_repo(_mkdir(tmp_path / "sdk-q"))
    with Repo(sdk_repo) as r:
        (r.path / "artifact.bin").write_bytes(b"x")
        r.add("artifact.bin")
        result = r.commit("via sdk")
    assert result.get("queued") is True
    assert load_pending_push(sdk_repo)

    cli_repo = _mkdir(tmp_path / "cli-q")
    _init_repo(cli_repo)
    (cli_repo / "artifact.bin").write_bytes(b"x")
    run_av(cli_repo, ["add", "artifact.bin"])
    run_av(cli_repo, ["commit", "-m", "via cli"])
    assert load_pending_push(cli_repo)


def test_seam_parity_error_codes_not_a_repo_and_nothing_staged(tmp_path):
    """v1.2.5: the same failure conditions map to the documented codes across the SDK
    (SDKError.code/.exit_code) and the seam (return-value contract). The CLI side of this
    same parity (exit 10/11 via fail()) is proven directly in tests/test_exit_codes.py —
    this test is the plugin/SDK-side half of the WP-7 exit-code registry fix.
    """
    from av_sdk import Repo
    from av_sdk.exceptions import SDKError
    from python.av_cli.core import EXIT_NOT_A_REPO, EXIT_NOTHING_TO_COMMIT, commit_scoped_paths

    # not_a_repo:
    not_a_repo_dir = _mkdir(tmp_path / "not-a-repo")
    with pytest.raises(SDKError) as sdk_exc:
        Repo(not_a_repo_dir)
    assert sdk_exc.value.code == "not_a_repo"
    assert sdk_exc.value.exit_code == EXIT_NOT_A_REPO

    # nothing_to_commit: the seam returns None (its documented no-op contract) — it's
    # intentionally scoped to only ever commit what was just staged, so "nothing to
    # commit" is a normal, silent no-op there, not a failure — while the SDK raises with
    # the dedicated code, matching what av commit now does (see test_exit_codes.py).
    seam_repo = _init_repo(_mkdir(tmp_path / "seam-empty"))
    result = commit_scoped_paths(seam_repo, [], "empty")
    assert result is None

    sdk_repo = _init_repo(_mkdir(tmp_path / "sdk-empty"))
    with Repo(sdk_repo) as r:
        with pytest.raises(SDKError) as exc:
            r.commit("empty")
    assert exc.value.code == "nothing_to_commit"
    assert exc.value.exit_code == EXIT_NOTHING_TO_COMMIT


def _mkdir(p):
    p.mkdir(parents=True, exist_ok=True)
    return p
