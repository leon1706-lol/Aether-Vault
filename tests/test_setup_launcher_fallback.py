"""Unit tests for `setup.py`'s native-launcher fallback helpers -- the shim shebang
rewrite and the `AV_REQUIRE_LAUNCHER` hard-fail gate (V1.6.0, found live via the
`launcher-native-posix` CI job: a raw `shutil.copy2` of the `#!python` placeholder shim
left it non-invokable outside a real wheel install, and a native-compile failure fell back
to it silently with no way to catch the regression in CI).

`setup.py` calls `setup()` unconditionally at module scope (no `if __name__ ==
"__main__":` guard -- it's meant to run as a script, never be imported), so these tests
load it as a module with `setuptools.setup` patched to a no-op first.
"""
import importlib.util
import sys
import zipfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SETUP_PY = REPO_ROOT / "setup.py"


@pytest.fixture
def setup_module(monkeypatch):
    import setuptools

    monkeypatch.setattr(setuptools, "setup", lambda **kwargs: None)
    spec = importlib.util.spec_from_file_location("_setup_under_test", SETUP_PY)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ---------------------------------------------------------------------------
# _write_shim -- rewrites the `#!python` placeholder to the real interpreter path
# ---------------------------------------------------------------------------

def test_write_shim_rewrites_python_shebang_to_real_interpreter(setup_module, tmp_path):
    src = tmp_path / "av_shim.py"
    src.write_text(setup_module._PY_SHIM, encoding="utf-8")
    dest = tmp_path / "av"

    setup_module.BuildExtWithLauncher._write_shim(src, dest)

    text = dest.read_text(encoding="utf-8")
    first_line = text.splitlines()[0]
    assert first_line == f"#!{sys.executable}"
    assert first_line != "#!python"


def test_write_shim_preserves_the_rest_of_the_file_verbatim(setup_module, tmp_path):
    src = tmp_path / "av_shim.py"
    src.write_text(setup_module._PY_SHIM, encoding="utf-8")
    dest = tmp_path / "av"

    setup_module.BuildExtWithLauncher._write_shim(src, dest)

    original_body = setup_module._PY_SHIM.split("\n", 1)[1]
    rewritten_body = dest.read_text(encoding="utf-8").split("\n", 1)[1]
    assert rewritten_body == original_body


def test_write_shim_leaves_a_non_shebang_first_line_untouched(setup_module, tmp_path):
    """Defensive: only ever rewrites the exact `#!python` placeholder -- never mangles a
    file that happens to not be that shim (this helper is only ever called with it in
    practice, but shouldn't corrupt something else if that ever changes)."""
    src = tmp_path / "not_a_shim.py"
    src.write_text("#!/usr/bin/env something-else\nprint('hi')\n", encoding="utf-8")
    dest = tmp_path / "out"

    setup_module.BuildExtWithLauncher._write_shim(src, dest)

    assert dest.read_text(encoding="utf-8").splitlines()[0] == "#!/usr/bin/env something-else"


# ---------------------------------------------------------------------------
# AV_REQUIRE_LAUNCHER -- a native-compile failure re-raises instead of silently
# falling back to the (possibly broken, pre-this-fix) shim. `.run()` is exercised with
# `__new__` (no `__init__`) and the real pybind11 `build_ext.run()` patched to a no-op --
# only the try/except around `_build_launcher()` is under test here, not a real compile.
# ---------------------------------------------------------------------------

def test_av_require_launcher_unset_falls_back_to_shim_on_build_failure(setup_module, monkeypatch):
    monkeypatch.delenv("AV_REQUIRE_LAUNCHER", raising=False)
    monkeypatch.setattr(setup_module._pybind11_build_ext, "run", lambda self: None)

    class _Failing(setup_module.BuildExtWithLauncher):
        def _build_launcher(self):
            raise RuntimeError("simulated compiler failure")

    shim_calls = []
    monkeypatch.setattr(_Failing, "_record_shim_artifact", lambda self: shim_calls.append(1))

    _Failing.__new__(_Failing).run()  # must not raise -- falls back instead

    assert shim_calls == [1]


def test_av_require_launcher_1_reraises_instead_of_falling_back(setup_module, monkeypatch):
    monkeypatch.setenv("AV_REQUIRE_LAUNCHER", "1")
    monkeypatch.setattr(setup_module._pybind11_build_ext, "run", lambda self: None)

    class _Failing(setup_module.BuildExtWithLauncher):
        def _build_launcher(self):
            raise RuntimeError("simulated compiler failure")

    monkeypatch.setattr(_Failing, "_record_shim_artifact",
                         lambda self: pytest.fail("must not fall back when AV_REQUIRE_LAUNCHER=1"))

    with pytest.raises(RuntimeError, match="simulated compiler failure"):
        _Failing.__new__(_Failing).run()


@pytest.mark.parametrize("value", ["true", "yes", "TRUE", " 1 "])
def test_av_require_launcher_truthy_spellings_all_reraise(setup_module, monkeypatch, value):
    monkeypatch.setenv("AV_REQUIRE_LAUNCHER", value)
    monkeypatch.setattr(setup_module._pybind11_build_ext, "run", lambda self: None)

    class _Failing(setup_module.BuildExtWithLauncher):
        def _build_launcher(self):
            raise RuntimeError("boom")

    monkeypatch.setattr(_Failing, "_record_shim_artifact",
                         lambda self: pytest.fail(f"{value!r} must be treated as truthy"))

    with pytest.raises(RuntimeError, match="boom"):
        _Failing.__new__(_Failing).run()


# ---------------------------------------------------------------------------
# _rewrite_wheel_with_extra_script -- the injected `av`/`av.exe` entry must actually be
# executable once pip extracts it, not just carry permission bits pip's own installer
# doesn't recognize as such.
# ---------------------------------------------------------------------------

def _build_fake_wheel(tmp_path, name="aether_vault-0.0.0-py3-none-any") -> Path:
    """A minimal wheel: one real file plus the RECORD `_rewrite_wheel_with_extra_script`
    needs to find and rewrite."""
    wheel_path = tmp_path / f"{name}.whl"
    with zipfile.ZipFile(wheel_path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("av_cli/__init__.py", "")
        zf.writestr(f"{name}.dist-info/METADATA", "Metadata-Version: 2.1\n")
        zf.writestr(f"{name}.dist-info/RECORD", "av_cli/__init__.py,,\n")
    return wheel_path


def test_injected_script_entry_is_recognized_as_executable_by_pip(setup_module, tmp_path):
    """Real bug (found live via `smoke-wheel-linux`'s "Permission denied", not a shebang
    problem): `external_attr` must encode a full Unix `st_mode` (file-type bits included),
    not just the permission bits -- pip's own `zip_item_is_executable()` requires
    `stat.S_ISREG(mode)`, which `0o755` alone (no `S_IFREG`) fails."""
    pytest.importorskip("pip", reason="pip internals used only to mirror its own check")
    from pip._internal.utils.unpacking import zip_item_is_executable

    wheel_path = _build_fake_wheel(tmp_path)
    injector = setup_module.WheelWithNativeLauncher.__new__(setup_module.WheelWithNativeLauncher)
    injector.dist_dir = str(tmp_path)

    injector._rewrite_wheel_with_extra_script(wheel_path, "av", b"#!/usr/bin/env python\nprint(1)\n",
                                               is_binary=False)

    with zipfile.ZipFile(wheel_path) as zf:
        script_entries = [i for i in zf.infolist() if i.filename.endswith("/scripts/av")]
        assert len(script_entries) == 1, zf.namelist()
        assert zip_item_is_executable(script_entries[0])


def test_av_require_launcher_only_matters_on_failure(setup_module, monkeypatch):
    """A successful build never touches the shim path regardless of the env var."""
    monkeypatch.setenv("AV_REQUIRE_LAUNCHER", "1")
    monkeypatch.setattr(setup_module._pybind11_build_ext, "run", lambda self: None)
    build_calls = []

    class _Succeeding(setup_module.BuildExtWithLauncher):
        def _build_launcher(self):
            build_calls.append(1)

    monkeypatch.setattr(_Succeeding, "_record_shim_artifact",
                         lambda self: pytest.fail("shim fallback must not run on success"))

    _Succeeding.__new__(_Succeeding).run()

    assert build_calls == [1]
