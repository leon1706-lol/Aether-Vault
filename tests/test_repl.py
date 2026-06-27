from python.av_cli import repl


class _FakeSession:
    """Stand-in for prompt_toolkit.PromptSession — returns scripted lines, then raises."""

    def __init__(self, lines, *args, **kwargs):
        self._lines = list(lines)

    def prompt(self, *args, **kwargs):
        if not self._lines:
            raise EOFError()
        nxt = self._lines.pop(0)
        if nxt is KeyboardInterrupt:
            raise KeyboardInterrupt()
        if nxt is EOFError:
            raise EOFError()
        return nxt


def _run_with_lines(monkeypatch, tmp_path, lines):
    monkeypatch.setattr(repl, "PromptSession", lambda *a, **k: _FakeSession(lines))
    (tmp_path / ".av").mkdir(exist_ok=True)
    repl.run_repl(tmp_path, login_mode="local")


def test_repl_exits_on_exit_word(monkeypatch, tmp_path):
    _run_with_lines(monkeypatch, tmp_path, ["exit"])  # should return without raising


def test_repl_exits_on_quit_word(monkeypatch, tmp_path):
    _run_with_lines(monkeypatch, tmp_path, ["quit"])


def test_repl_exits_on_eof(monkeypatch, tmp_path):
    _run_with_lines(monkeypatch, tmp_path, [EOFError])


def test_repl_continues_on_keyboard_interrupt(monkeypatch, tmp_path):
    # KeyboardInterrupt cancels the current line only — loop must continue to the next
    # scripted line (here: "exit") rather than propagating or stopping.
    _run_with_lines(monkeypatch, tmp_path, [KeyboardInterrupt, "exit"])


def test_repl_dispatches_real_subcommand(monkeypatch, tmp_path, capsys):
    (tmp_path / ".av").mkdir(exist_ok=True)
    import json

    (tmp_path / ".av" / "config").write_text(
        json.dumps({"lfs_threshold_mb": 50, "remote_url": "http://localhost:8000",
                    "project_id": "x", "project_name": "t"})
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(repl, "PromptSession", lambda *a, **k: _FakeSession(["av status", "exit"]))
    repl.run_repl(tmp_path, login_mode="local")
    out = capsys.readouterr().out
    assert "status" in out.lower() or out  # status command produced some output, didn't raise


def test_repl_blocks_nested_init(monkeypatch, tmp_path, capsys):
    (tmp_path / ".av").mkdir(exist_ok=True)
    monkeypatch.setattr(repl, "PromptSession", lambda *a, **k: _FakeSession(["av init", "exit"]))
    repl.run_repl(tmp_path, login_mode="local")
    out = capsys.readouterr().out
    assert "already inside" in out.lower()


def test_repl_degrades_gracefully_when_session_cannot_be_constructed(monkeypatch, tmp_path, capsys):
    # Regression test: on some terminals (e.g. Git Bash/mintty on Windows) isatty() reports
    # True but prompt_toolkit still can't get a real console handle and raises out of
    # PromptSession(...) itself. This must degrade to a warning, not crash `av`.
    def _raise(*a, **k):
        raise OSError("no console screen buffer")

    monkeypatch.setattr(repl, "PromptSession", _raise)
    (tmp_path / ".av").mkdir(exist_ok=True)
    repl.run_repl(tmp_path, login_mode="local")  # must not raise
    out = capsys.readouterr().out
    assert "isn't available in this terminal" in out.lower()
