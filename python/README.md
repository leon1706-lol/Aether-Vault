# python

Owns every installable Aether-Vault Python package built from this directory by
`setup.py`: the **CLI** (`av_cli`), the FastAPI registry server (`av_server`), the
optional framework plugins (`av_plugins`), and the agent SDK (`av_sdk`). All import flat
(`import av_cli`) via `package_dir`, while tests import them as `python.av_cli.*` from a
checkout - that split is why `[tool.pytest.ini_options] pythonpath = ["."]` exists in
`pyproject.toml`.

- `av_cli/` - the `av` command line interface plus ALL client-side logic: local DAG,
  CAS, staging, sync/clone/pull, merge, chunking, signing, doctor. See `av_cli/README.md`.
- `av_server/` - the Dockerized content-addressable registry backed by PostgreSQL
  (Merkle DAG) + RedisBloom. See `av_server/README.md`.
- `av_plugins/` - optional Lightning / Transformers / MLflow auto-commit callbacks.
  See `av_plugins/README.md`.
- `av_sdk/` - `from av_sdk import Repo`, the in-process alternative to shelling out to
  the CLI for an agent driving Aether-Vault directly. `repo.py` mirrors the CLI's own
  surface (add/commit/push/log/status/diff_semantic/context_note/handoff_dict/
  publish_handoff) with the same payload shapes, run linkage, and queued semantics
  (pinned by `tests/test_av_sdk.py`'s parity matrix). `exceptions.py` has one typed
  subclass of `SDKError` per exit code (`NotARepoError`, `PolicyDeniedError`, ...) plus
  `sdk_error_for()`, so `except SDKError` still catches everything while callers who want
  to branch on a specific failure can. See [`docs/for-agents.md`](../docs/for-agents.md)
  for the minimal recipe.

New CLI features put their logic in a dedicated module (`history.py`, `sync.py`,
`merge.py`, `attributes.py`, `signing.py` are prior art) and keep the Click wrapper in
`main.py` thin - see `av_cli/README.md` for the invariants that enforce it.
