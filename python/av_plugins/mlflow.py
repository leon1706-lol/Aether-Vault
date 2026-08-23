"""MLflow compatibility layer: import existing MLflow runs into Aether-Vault.

Usage:
    from av_plugins.mlflow import import_run
    import_run("a1b2c3d4e5f6")
"""
import shutil
from pathlib import Path

from av_cli.exceptions import AetherVaultException

from ._shared import build_metric_args, commit_scoped, resolve_repo_root

try:
    from mlflow.tracking import MlflowClient
except ImportError as exc:
    raise ImportError(
        "import_run requires MLflow. Install it with `pip install aether-vault[mlflow]`."
    ) from exc


def import_run(
    run_id: str,
    repo_root: Path | None = None,
    tracking_uri: str | None = None,
    tag: str | None = None,
) -> None:
    """Downloads an existing MLflow run's artifacts and commits them into Aether-Vault.

    Numeric metrics are attached via `--metric`; simple string params are attached as
    additional `--tag key=value` labels. Raises if the run has no artifacts to import.

    Artifacts are downloaded into `<repo_root>/mlflow_imports/<run_id>/` rather than a system
    temp directory — `av add` requires every staged path to live under the repo root, and
    MLflow's own artifact store (e.g. `~/mlruns/...`) is typically outside it.
    """
    resolved_root = repo_root if repo_root is not None else resolve_repo_root(Path.cwd())

    client = MlflowClient(tracking_uri=tracking_uri)
    run = client.get_run(run_id)

    # `download_artifacts` itself raises an internal MlflowException (rather than returning
    # an empty directory) when the run has zero artifacts, so check via list_artifacts first
    # and fail with our own clear error instead of letting that internal exception leak through.
    if not client.list_artifacts(run_id):
        raise AetherVaultException(f"MLflow run {run_id} has no artifacts to import.")

    dest_dir = resolved_root / "mlflow_imports" / run_id
    if dest_dir.exists():
        shutil.rmtree(dest_dir)
    dest_dir.mkdir(parents=True)

    local_path = client.download_artifacts(run_id, ".", dst_path=str(dest_dir))
    artifact_paths = [str(p) for p in Path(local_path).rglob("*") if p.is_file()]
    if not artifact_paths:
        raise AetherVaultException(f"MLflow run {run_id} has no artifacts to import.")

    metrics = dict(run.data.metrics)
    commit_args = [
        "commit",
        "-m",
        f"Imported MLflow run {run_id}",
        "--tag",
        "mlflow-import",
        *build_metric_args(metrics),
    ]
    for key, value in run.data.params.items():
        if isinstance(value, str):
            commit_args.extend(["--tag", f"{key}={value}"])
    if tag:
        commit_args.extend(["--tag", tag])
    # Scoped so the import commits exactly the run's artifacts — unrelated staged files
    # keep their pending state (Probleme.md #38).
    commit_scoped(resolved_root, artifact_paths, commit_args)
