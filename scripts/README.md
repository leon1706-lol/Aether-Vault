# scripts

Owns standalone developer tooling that isn't part of the installed package - things
you run by hand, occasionally, while working on the codebase.

- `run_benchmark_comparison.py` - convenience wrapper running the full cross-tool
  benchmark suite (`benchmarks/`) and writing the Markdown report - what
  `av benchmark --markdown` does, usable without an editable install's console script.
- `check_eager_annotations.py` - AST guard flagging annotations that reference names
  imported later in the file: the py3.10-vs-3.14 eager-annotation trap that once broke
  CI collection while dev machines (PEP 649) never saw it. Resolves the cmd modules'
  `from .core import *` preludes one level deep.

Anything that graduates into a user- or CI-facing command belongs in
`python/av_cli/` (or `benchmarks/`) instead, so it ships with the package and gets
test coverage. Keep this folder for one-off, checkout-local tooling.
