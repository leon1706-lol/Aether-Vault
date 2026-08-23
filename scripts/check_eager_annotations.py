"""CI-parity guard: flags module-level annotations that reference names whose import
appears LATER in the file (or not at all).

Why this exists: on Python <=3.12 function/parameter annotations evaluate EAGERLY at def
time, so `def f(repo: Path)` placed above `from pathlib import Path` dies at collection
with NameError — but on Python 3.14 dev machines PEP 649 defers annotation evaluation,
so the bug is invisible locally and only explodes in CI (exactly what broke
tests/test_merge.py in GitHub Actions, see development/Probleme.md).

Usage:  python scripts/check_eager_annotations.py [paths...]   # default: tests/
Exit 1 if any problem found. Wire into pre-commit or CI whenever convenient.
"""
import ast
import builtins
import sys
from pathlib import Path

_BUILTIN_NAMES = set(dir(builtins))


def _star_surface(node: ast.ImportFrom, path: Path) -> set[str]:
    """Public top-level names exported by a star-imported sibling module.

    The av_cli command modules use `from .core import *` as their shared prelude; core
    binds `Path`/`click`/`json`/... publicly, so annotations referencing them are safe on
    every Python version. Without this resolution the checker false-positives on them.
    """
    if node.module is None or node.level != 1:
        return set()
    target = (path.parent / f"{node.module.replace('.', '/')}.py")
    if not target.exists():
        return set()
    try:
        tree = ast.parse(target.read_text(encoding="utf-8"))
    except SyntaxError:
        return set()
    names: set[str] = set()
    for sub in tree.body:
        if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            if not sub.name.startswith("_"):
                names.add(sub.name)
        elif isinstance(sub, ast.Assign):
            for t in sub.targets:
                if isinstance(t, ast.Name) and not t.id.startswith("_"):
                    names.add(t.id)
        elif isinstance(sub, ast.Import):
            for a in sub.names:
                n = (a.asname or a.name).split(".")[0]
                if not n.startswith("_"):
                    names.add(n)
        elif isinstance(sub, ast.ImportFrom):
            if sub.module and sub.module != "__future__":
                for a in sub.names:
                    n = a.asname or a.name
                    if not n.startswith("_"):
                        names.add(n)
    return names


def check_file(path: Path) -> list[str]:
    problems: list[str] = []
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))

    available: dict[str, int] = {}
    future_annotations = False
    body = list(tree.body)
    star_names: set[str] = set()
    for node in body:
        if isinstance(node, ast.ImportFrom) and node.module == "__future__":
            future_annotations = True
    if future_annotations:
        return problems  # stringified annotations defer everything

    # Pre-resolve star-imports so shared-prelude names count as available everywhere.
    for node in body:
        if isinstance(node, ast.ImportFrom) and any(a.name == "*" for a in node.names):
            star_names |= _star_surface(node, path)

    def ann_names(node) -> None:
        for sub in ast.walk(node):
            if isinstance(sub, ast.Name) and sub.id not in _BUILTIN_NAMES \
                    and sub.id not in available and sub.id not in star_names:
                problems.append(
                    f"{path}:{node.lineno}: annotation uses '{sub.id}' before its "
                    f"import/definition (eager NameError on Python <=3.12)"
                )

    def defined_after(node) -> set[str]:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            return {node.name}
        if isinstance(node, ast.Assign):
            return {t.id for t in node.targets if isinstance(t, ast.Name)}
        return set()

    for node in body:
        if isinstance(node, ast.Import):
            for a in node.names:
                available[(a.asname or a.name).split(".")[0]] = node.lineno
            continue
        if isinstance(node, ast.ImportFrom):
            for a in node.names:
                available[a.asname or a.name] = node.lineno
            continue

        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            args = node.args
            anns = [a.annotation for a in list(args.args) + list(args.kwonlyargs)]
            anns += [args.vararg.annotation if args.vararg else None,
                     args.kwarg.annotation if args.kwarg else None,
                     node.returns]
            for ann in anns:
                if ann is not None:
                    ann_names(ann)

        for name in defined_after(node):
            available[name] = getattr(node, "lineno", 0)
    return problems


def main() -> int:
    paths = [Path(a) for a in sys.argv[1:]] or [Path("tests")]
    all_problems: list[str] = []
    for p in paths:
        files = sorted(p.rglob("*.py")) if p.is_dir() else [p]
        for f in files:
            if "__pycache__" in f.parts:
                continue
            try:
                all_problems.extend(check_file(f))
            except SyntaxError as exc:
                all_problems.append(f"{f}: SyntaxError: {exc}")
    for line in all_problems:
        print(line)
    print(f"checked {sum(len(list(p.rglob('*.py'))) if p.is_dir() else 1 for p in paths)} "
          f"file(s), {len(all_problems)} problem(s)")
    return 1 if all_problems else 0


if __name__ == "__main__":
    sys.exit(main())
