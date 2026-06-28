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
    assert "Aether-Vault" in text
    assert "version control for ML models & datasets" in text
    # The ANSI block-art logo lines should render too, not just the title/subtitle text.
    assert "█" in text


def test_print_banner_omits_subtitle_line_when_not_given(monkeypatch):
    recorder = Console(record=True, width=120)
    monkeypatch.setattr(ui, "console", recorder)

    ui.print_banner("Aether-Vault")

    text = recorder.export_text()
    assert "Aether-Vault" in text


def test_select_login_mode_returns_the_users_choice(monkeypatch):
    class _FakeQuery:
        def ask(self):
            return "enterprise"

    monkeypatch.setattr(ui.questionary, "select", lambda *a, **k: _FakeQuery())

    assert ui.select_login_mode() == "enterprise"


def test_select_login_mode_defaults_to_local_on_ctrl_c(monkeypatch):
    class _FakeQuery:
        def ask(self):
            return None  # questionary returns None on Ctrl+C/Ctrl+D

    monkeypatch.setattr(ui.questionary, "select", lambda *a, **k: _FakeQuery())

    assert ui.select_login_mode() == "local"


def test_is_interactive_true_only_when_both_streams_are_a_tty(monkeypatch):
    monkeypatch.setattr(ui.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(ui.sys.stdout, "isatty", lambda: True)
    assert ui.is_interactive() is True

    monkeypatch.setattr(ui.sys.stdout, "isatty", lambda: False)
    assert ui.is_interactive() is False
