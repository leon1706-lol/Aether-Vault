# `python/` — All Aether-Vault Python Packages

Three installable packages built from this directory (see `../setup.py`): the **CLI**
(`av_cli`), the **registry server** (`av_server`), and the optional **framework plugins**
(`av_plugins`). See the [main README](../README.md) for the project overview.

## Contents

| Package | Purpose | Details |
|---|---|---|
| [`av_cli/`](av_cli/README.md) | The `av` command line interface + all client-side logic (DAG, CAS, sync, merge, chunking) | ~20 modules |
| [`av_server/`](av_server/README.md) | FastAPI Content-Addressable Storage registry backed by PostgreSQL + RedisBloom | 6 modules |
| [`av_plugins/`](av_plugins/README.md) | Optional PyTorch Lightning / HuggingFace Transformers / MLflow auto-commit callbacks | 5 modules |

## Layout notes

- Packages live under `python/` but import flat (`import av_cli`), wired via `setup.py`'s
  `package_dir`. Tests, however, import them as `python.av_cli.*` from a repo checkout —
  that's why `[tool.pytest.ini_options] pythonpath = ["."]` exists in `pyproject.toml`.
- New CLI features should put their logic in a dedicated module here (`history.py`,
  `sync.py`, `merge.py`, `attributes.py` are the v1.1.1 additions) and keep the Click
  wrapper in `main.py` thin.
