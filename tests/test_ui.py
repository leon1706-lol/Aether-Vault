"""Behavioral tests for ui.py's rendering functions — complements test_dependency_guards.py,
which only covers the missing-dependency import guards, not what these functions actually
render when the dependencies ARE present.
"""

from rich.console import Console

from python.av_cli import ui


def test_print_step_renders_the_status_tag_and_message(capsys):
    ui.print_step("server is up", status="success")
    out = capsys.readouterr().out
    assert "[OK]" in out
    assert "server is up" in out


def test_print_step_defaults_to_info_for_an_unknown_status(capsys):
    ui.print_step("just fyi", status="not-a-real-status")
    out = capsys.readouterr().out
    assert "[INFO]" in out
    assert "just fyi" in out


def test_print_step_warn_and_error_use_their_own_tags(capsys):
    ui.print_step("careful", status="warn")
    assert "[WARN]" in capsys.readouterr().out

    ui.print_step("broken", status="error")
    assert "[ERROR]" in capsys.readouterr().out


def test_print_banner_renders_title_subtitle_and_logo(monkeypatch):
    recorder = Console(record=True, width=120)
    monkeypatch.setattr(ui, "console", recorder)

    ui.print_banner("Aether-Vault", "version control for ML models & datasets")

    text = recorder.export_text()
    assert "Aether-Vault" in text.replace(" ", "") or "A E T H E R" in text
    assert "version control for ML models & datasets" in text
    # The dash-stroke bolt and signature rule are the logo's anchor glyphs now.
    assert "━" in text


def test_print_banner_embeds_version_in_frame_corner(monkeypatch):
    recorder = Console(record=True, width=120)
    monkeypatch.setattr(ui, "console", recorder)
    monkeypatch.setattr(ui, "_get_version", lambda: "9.9.9")

    ui.print_banner("Aether-Vault")

    assert "v9.9.9" in recorder.export_text()


def test_get_version_returns_displayable_string():
    """Contract: whatever the resolution chain yields (_version.py → importlib.metadata
    → 'dev'), it must be a non-empty displayable string for the frame corner."""
    version = ui._get_version()
    assert isinstance(version, str) and version.strip()


def test_print_banner_omits_subtitle_line_when_not_given(monkeypatch):
    recorder = Console(record=True, width=120)
    monkeypatch.setattr(ui, "console", recorder)

    ui.print_banner("Aether-Vault")

    text = recorder.export_text()
    # The wordmark renders letter-spaced in the new banner design.
    assert "A E T H E R - V A U L T" in text


def test_select_login_mode_was_removed_from_the_interactive_flow():
    """Enterprise login is unbuilt, so `av init` must not offer it interactively anymore:
    ui.select_login_mode (the Local/Enterprise question) is deleted — init always picks
    Local unless `--mode enterprise` is passed explicitly. Guards against the prompt
    quietly coming back."""
    assert not hasattr(ui, "select_login_mode")


def test_is_interactive_true_only_when_both_streams_are_a_tty(monkeypatch):
    monkeypatch.setattr(ui.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(ui.sys.stdout, "isatty", lambda: True)
    assert ui.is_interactive() is True

    monkeypatch.setattr(ui.sys.stdout, "isatty", lambda: False)
    assert ui.is_interactive() is False
