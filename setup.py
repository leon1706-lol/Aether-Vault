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
        extra_compile_args=["-O2"] if sys.platform == "win32" else ["-O3"],
    ),
]

setup(
    ext_modules=ext_modules,
    cmdclass={"build_ext": build_ext},
    packages=["av_cli", "av_server"],
    package_dir={"": "python"},
)
