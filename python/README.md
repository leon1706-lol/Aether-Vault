# python

Owns every installable Aether-Vault Python package built from this directory by
`setup.py`: the **CLI** (`av_cli`), the FastAPI registry server (`av_server`), and the
optional framework plugins (`av_plugins`). All three import flat (`import av_cli`) via
`package_dir`, while tests import them as `python.av_cli.*` from a checkout - that
split is why `[tool.pytest.ini_options] pythonpath = ["."]` exists in `pyproject.toml`.

- `av_cli/` - the `av` command line interface plus ALL client-side logic: local DAG,
  CAS, staging, sync/clone/pull, merge, chunking, signing, doctor. See `av_cli/README.md`.
- `av_server/` - the Dockerized content-addressable registry backed by PostgreSQL
  (Merkle DAG) + RedisBloom. See `av_server/README.md`.
- `av_plugins/` - optional Lightning / Transformers / MLflow auto-commit callbacks.
  See `av_plugins/README.md`.

New CLI features put their logic in a dedicated module (`history.py`, `sync.py`,
`merge.py`, `attributes.py`, `signing.py` are prior art) and keep the Click wrapper in
`main.py` thin - see `av_cli/README.md` for the invariants that enforce it.
