"""V1.6.0: `setup.py`'s `WheelWithNativeLauncher` injects the native `av` launcher directly
into the built wheel's `.data/scripts/` directory, bypassing setuptools' `scripts=`/
`build_scripts` mechanism entirely (a real attempt via `scripts=` hard-crashed
`python -m build --wheel` on this toolchain -- see `src/launcher/README.md`). This drives a
REAL `python -m build --wheel` (the same command a release actually runs), not a unit test
of extracted logic -- the whole point is proving the wheel that comes out the other end is
correct, since that's exactly where the crash this replaced was only ever caught by actually
building. Slow (a real build); skips cleanly when the native launcher itself didn't build
(no C++ toolchain available at all).
"""
from __future__ import annotations

import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]

_EXE_SUFFIX = ".exe" if sys.platform == "win32" else ""
# Windows PE starts "MZ"; ELF (Linux) starts b"\x7fELF"; Mach-O (macOS) starts with one of a
# few magic numbers depending on architecture/fat-binary-ness.
_MACHO_MAGICS = (b"\xcf\xfa\xed\xfe", b"\xce\xfa\xed\xfe", b"\xca\xfe\xba\xbe", b"\xbe\xba\xfe\xca")


def _looks_like_a_real_executable(payload: bytes) -> bool:
    if sys.platform == "win32":
        return payload[:2] == b"MZ"
    if sys.platform == "darwin":
        return payload[:4] in _MACHO_MAGICS
    return payload[:4] == b"\x7fELF"


@pytest.fixture(scope="module")
def built_wheel(tmp_path_factory):
    out_dir = tmp_path_factory.mktemp("wheel-packaging-test")
    result = subprocess.run(
        [sys.executable, "-m", "build", "--wheel", "--no-isolation", "--outdir", str(out_dir)],
        cwd=str(REPO_ROOT), capture_output=True, timeout=300,
        # A compiler's own diagnostic text (locale-dependent -- e.g. German MSVC warning
        # strings on this project's own dev box) isn't guaranteed valid in the platform's
        # default codepage `text=True` would decode with -- explicit UTF-8 with replacement
        # never raises over build OUTPUT, which this test only inspects for a marker
        # substring, not byte-exact content.
        encoding="utf-8", errors="replace",
    )
    assert result.returncode == 0, (
        f"python -m build --wheel failed:\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    wheels = list(out_dir.glob("*.whl"))
    assert len(wheels) == 1, f"expected exactly one wheel, found {wheels}"
    return wheels[0], result.stdout + result.stderr


@pytest.fixture(scope="module")
def native_build_succeeded(built_wheel):
    """Whether BuildExtWithLauncher actually produced a real compiled exe for THIS build --
    as opposed to falling back to the pure-Python shim (see setup.py's own docstring on that
    trade-off). The remaining tests below only make sense for a real binary; module-skip
    them rather than fail when a toolchain genuinely couldn't produce one."""
    _wheel_path, build_output = built_wheel
    return "will be injected into the wheel as the real `av` command" in build_output


def test_build_reports_native_launcher_injection(built_wheel):
    _wheel_path, build_output = built_wheel
    assert "native av launcher: injected as" in build_output, (
        "the build didn't report injecting av into the wheel at all -- "
        "WheelWithNativeLauncher didn't run"
    )


def test_wheel_contains_av_exe_not_a_console_script_stub(built_wheel, native_build_succeeded):
    if not native_build_succeeded:
        pytest.skip("native launcher build fell back to the pure-Python shim on this runner "
                    "-- nothing binary to check here")
    wheel_path, _ = built_wheel
    with zipfile.ZipFile(wheel_path) as z:
        names = z.namelist()
        av_entries = [n for n in names if n.endswith(f".data/scripts/av{_EXE_SUFFIX}")]
        assert len(av_entries) == 1, f"expected exactly one injected av{_EXE_SUFFIX}, found {av_entries}"
        av_exe = av_entries[0]

        info = z.getinfo(av_exe)
        assert info.file_size > 50_000, (
            "the injected av binary is suspiciously small -- looks like a console-script "
            "stub, not the real compiled launcher"
        )
        # Executable bit set (owner/group/other) -- pip's installer only chmods +x a
        # script-scheme file that already carries this in its zip external_attr.
        assert (info.external_attr >> 16) & 0o111, "injected av binary is missing the executable bit"

        payload = z.read(av_exe)
        assert _looks_like_a_real_executable(payload), (
            f"injected av binary doesn't look like a real {sys.platform} executable "
            f"(first bytes: {payload[:8]!r})"
        )


def test_wheel_entry_points_never_register_av(built_wheel):
    """The one invariant the whole mechanism depends on (see pyproject.toml's comment on
    `av-py`): `av` must never ALSO be a console_scripts entry point, or pip's own installer
    silently drops the injected binary in favor of its generated wrapper. True regardless of
    whether the native build itself succeeded this run -- the pure-Python shim fallback
    depends on the exact same invariant."""
    wheel_path, _ = built_wheel
    with zipfile.ZipFile(wheel_path) as z:
        ep_path = next(n for n in z.namelist() if n.endswith(".dist-info/entry_points.txt"))
        content = z.read(ep_path).decode("utf-8")
    assert "av-py" in content
    assert "\nav " not in content and not content.strip().startswith("av ") and "\nav=" not in content


def test_wheel_record_lists_av_with_a_real_hash(built_wheel, native_build_succeeded):
    if not native_build_succeeded:
        pytest.skip("native launcher build fell back to the pure-Python shim on this runner")
    wheel_path, _ = built_wheel
    with zipfile.ZipFile(wheel_path) as z:
        record_path = next(n for n in z.namelist() if n.endswith(".dist-info/RECORD"))
        record = z.read(record_path).decode("utf-8")
    marker = f".data/scripts/av{_EXE_SUFFIX},"
    av_lines = [line for line in record.splitlines() if marker in line]
    assert av_lines, "RECORD has no entry for the injected av binary"
    line = av_lines[0]
    fields = line.split(",")
    assert len(fields) == 3
    assert fields[1].startswith("sha256=")
    assert int(fields[2]) > 50_000
