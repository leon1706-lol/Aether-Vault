#!/usr/bin/env python
"""Run the pytest suite file-by-file in fresh subprocesses with a free-RAM floor -- the way
to get a full green run on a memory-constrained machine where one `pytest tests/` process
gets OOM-killed (see development/MEMORY.md). Resumable: a killed run continues from the
last passed unit.

    python scripts/run_tests_lowmem.py                       # everything, resume by default
    python scripts/run_tests_lowmem.py --reset               # forget previous progress first
    python scripts/run_tests_lowmem.py --files tests/test_server.py --chunk-size 40
    python scripts/run_tests_lowmem.py -k "gc or audit" --min-free-mb 300
    python scripts/run_tests_lowmem.py -- -x --maxfail=3     # extra pytest args after --

Thin wrapper over `av_cli.lowmem_tests.run_lowmem` (also behind `av test --lowmem`).
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "python"))

from av_cli import lowmem_tests  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    extra: list[str] = []
    if "--" in argv:
        i = argv.index("--")
        argv, extra = argv[:i], argv[i + 1:]
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--min-free-mb", type=int, default=350)
    ap.add_argument("--chunk-size", type=int, default=0,
                    help="node ids per subprocess for EVERY file (0 = whole files, except the known-heavy ones)")
    ap.add_argument("--state", type=Path, default=None)
    ap.add_argument("--no-resume", action="store_true")
    ap.add_argument("--reset", action="store_true", help="delete the state file before starting")
    ap.add_argument("-k", dest="k_expr", default=None)
    ap.add_argument("--files", nargs="+", default=None)
    ap.add_argument("--timeout", type=float, default=900.0, help="seconds per subprocess")
    args = ap.parse_args(argv)

    state_path = args.state or lowmem_tests.default_state_path()
    if args.reset and state_path.exists():
        state_path.unlink()
    summary = lowmem_tests.run_lowmem(
        REPO_ROOT / "tests", min_free_mb=args.min_free_mb, chunk_size=args.chunk_size,
        state_path=state_path, resume=not args.no_resume, k_expr=args.k_expr, files=args.files,
        per_file_timeout=args.timeout, extra_args=tuple(extra),
    )
    return 0 if summary.ok else 1


if __name__ == "__main__":
    sys.exit(main())
