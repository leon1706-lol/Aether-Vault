"""Regression tests for the guarded imports in ui.py/repl.py/update_check.py.

These three modules import packages (rich, questionary, prompt_toolkit, packaging) that were
added to pyproject.toml alongside the "pretty av init" work — an environment whose install
predates that should get one clear, actionable error instead of a raw ModuleNotFoundError
traceback. Simulates "package not installed" by setting the import name to `None` in
`sys.modules` (the standard way to make `import X` raise ImportError without actually
uninstalling anything), then force-reimporting the guarded module fresh.
"""

import importlib
import sys

import click
import pytest


def _simulate_missing_and_reload(module_name: str, missing_imports: list[str]):
    saved = {}
    for name in missing_imports:
        saved[name] = sys.modules.pop(name, "NOTSET")
        sys.modules[name] = None
    sys.modules.pop(module_name, None)
    try:
        return importlib.import_module(module_name)
    finally:
        for name, original in saved.items():
            sys.modules.pop(name, None)
            if original != "NOTSET":
                sys.modules[name] = original
        sys.modules.pop(module_name, None)  # force a real re-import next time it's needed


def test_ui_raises_clear_error_when_questionary_missing():
    with pytest.raises(click.ClickException) as exc_info:
        _simulate_missing_and_reload("python.av_cli.ui", ["questionary"])
    msg = str(exc_info.value)
    assert "questionary" in msg
    assert "pip install" in msg


def test_ui_raises_clear_error_when_rich_missing():
    with pytest.raises(click.ClickException) as exc_info:
        _simulate_missing_and_reload("python.av_cli.ui", ["rich", "rich.console", "rich.panel"])
    assert "rich" in str(exc_info.value)


def test_ui_lists_all_missing_deps_together_not_one_at_a_time():
    with pytest.raises(click.ClickException) as exc_info:
        _simulate_missing_and_reload(
            "python.av_cli.ui", ["questionary", "rich", "rich.console", "rich.panel"]
        )
    msg = str(exc_info.value)
    assert "questionary" in msg
    assert "rich" in msg


def test_ui_imports_cleanly_when_deps_present():
    # Sanity check the guard doesn't fire false-positive in the normal (deps installed) case.
    module = _simulate_missing_and_reload("python.av_cli.ui", [])
    assert hasattr(module, "console")


def test_repl_raises_clear_error_when_prompt_toolkit_missing():
    with pytest.raises(click.ClickException) as exc_info:
        _simulate_missing_and_reload(
            "python.av_cli.repl", ["prompt_toolkit", "prompt_toolkit.history"]
        )
    assert "prompt_toolkit" in str(exc_info.value)


def test_update_check_raises_clear_error_when_packaging_missing():
    with pytest.raises(click.ClickException) as exc_info:
        _simulate_missing_and_reload(
            "python.av_cli.update_check", ["packaging", "packaging.version"]
        )
    assert "packaging" in str(exc_info.value)
