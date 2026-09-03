import sys
from setuptools import setup

try:
    from pybind11.setup_helpers import Pybind11Extension, build_ext
except ImportError:
    import subprocess
    subprocess.check_call([sys.executable, "-m", "pip", "install", "pybind11"])
    from pybind11.setup_helpers import Pybind11Extension, build_ext

ext_modules = [
    Pybind11Extension(
        "aether_core",
        ["src/core.cpp", "src/sha256.cpp"],
        include_dirs=["src/"],
        cxx_std=17,
        extra_compile_args=["/O2"] if sys.platform == "win32" else ["-O3"],
    ),
]

setup(
    ext_modules=ext_modules,
    cmdclass={"build_ext": build_ext},
    # v1.3.0 fix (Probleme.md): "av_server.migrations.versions" MUST be listed explicitly.
    # setuptools' build_py (the command that decides what .py files land in a WHEEL) only
    # descends into a package's OWN directory for each name in this list — it does not
    # recurse into an undeclared subdirectory just because the parent package is listed,
    # regardless of include-package-data/setuptools-scm (those affect the SDIST's MANIFEST,
    # a completely different mechanism, which is why `python -m build --sdist` correctly
    # included every migrations/versions/*.py file while `pip wheel .` silently dropped
    # them all — proven live: a wheel built before this fix had 0001-0004 but never 0005,
    # even after `git add`ing it). alembic's own versions/ directories don't need
    # __init__.py (ScriptDirectory finds .py files via its own walk, not Python imports),
    # so this line existing is the ONLY thing that makes them real, wheel-visible packages.
    packages=["av_cli", "av_server", "av_server.migrations", "av_server.migrations.versions",
              "av_plugins", "av_sdk"],
    package_dir={"": "python"},
    package_data={
        "av_server.migrations": ["script.py.mako"],
        # .avh v2 contract artifact: shipped so external validators (jsonschema CLI,
        # editors, other languages) can validate handoff documents against the exact
        # schema the project enforces structurally via handoff.validate_handoff().
        "av_cli": ["schemas/*.schema.json"],
    },
)
